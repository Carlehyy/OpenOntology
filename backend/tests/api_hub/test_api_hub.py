import json
import socket
import sqlite3
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api_hub import (
    config,
    db,
    executor,
    outbound_security,
)
from app.api_hub.outbound_security import (
    OutboundTargetError,
    request_with_safe_redirects,
    validate_outbound_url,
)
from app.api_hub.routers import backup, http_proxy, interfaces, proxy


@pytest.fixture
def hub_user():
    """A mock admin user so router-level ``Depends(get_current_user)`` resolves
    without a real JWT/DB round-trip.  Tests that need per-user isolation build
    a non-admin user and override the dependency themselves.
    """
    return SimpleNamespace(id="test-admin-id", role="admin", is_active=True)


@pytest.fixture
def hub_client(tmp_path, monkeypatch, hub_user):
    from app.deps import get_current_user
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "api_hub.db")
    monkeypatch.setattr(config, "INTERNAL_PROXY_TOKEN", "internal-proxy-test-token")
    # Most unit tests use .example placeholders and replace the actual request
    # method.  Dedicated outbound-security tests enable the guard explicitly.
    monkeypatch.setattr(config, "OUTBOUND_BLOCK_PRIVATE_NETWORKS", False)
    db.init_db()
    app = FastAPI()
    app.include_router(interfaces.router)
    app.include_router(interfaces.runs_router)
    app.include_router(backup.router)
    app.include_router(proxy.internal_router)
    app.include_router(http_proxy.admin_router)
    app.include_router(http_proxy.public_router)
    # Existing tests call /interfaces without auth headers.  Override
    # get_current_user to return a mock admin so the new created_by filter
    # stays transparent for legacy assertions (admin sees all).
    app.dependency_overrides[get_current_user] = lambda: hub_user
    return TestClient(app)


def _interface(**overrides):
    payload = {
        "name": "健康检查",
        "description": "检查上游服务是否可用",
        "group_name": "基础服务",
        "method": "GET",
        "url": "https://service.example/health",
        "query_params": [{"key": "verbose", "value": "1"}],
        "headers": [{"key": "X-Trace", "value": "test"}],
        "body_type": "none",
        "body_content": "",
        "mcp_enabled": True,
        "open_enabled": True,
    }
    payload.update(overrides)
    return payload


def test_interface_crud_group_and_auth_boundary(hub_client, client):
    created = hub_client.post("/interfaces", json=_interface())
    assert created.status_code == 200
    item = created.json()
    assert item["id"] > 0
    assert item["open_enabled"] is True

    listed = hub_client.get("/interfaces").json()
    assert [row["name"] for row in listed] == ["健康检查"]

    updated = hub_client.put(
        f"/interfaces/{item['id']}",
        json=_interface(name="上游健康检查", method="POST"),
    )
    assert updated.json()["method"] == "POST"

    moved = hub_client.post(
        "/interfaces/groups/delete", json={"group_name": "基础服务"}
    )
    assert moved.json() == {"ok": True, "count": 1}
    assert hub_client.get(f"/interfaces/{item['id']}").json()["group_name"] == ""

    # The host application restricts every management route to administrators.
    from app.main import app as platform_app
    from app.deps import get_current_user

    response = client.get("/api/api-hub/interfaces")
    assert response.status_code == 403
    assert client.get("/api/api-hub/proxy/info").status_code == 403
    try:
        platform_app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            role="viewer", is_active=True
        )
        assert client.get("/api/api-hub/interfaces").status_code == 403
        assert client.post("/api/api-hub/proxy/keys", json={}).status_code == 403

        platform_app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
            role="admin", is_active=True
        )
        assert client.get("/api/api-hub/interfaces").status_code == 200
    finally:
        platform_app.dependency_overrides.pop(get_current_user, None)


def test_internal_n8n_proxy_validates_dynamic_parameters_and_revision(
    hub_client, monkeypatch,
):
    observed = {}

    def fake_request(session, method, url, **kwargs):
        observed.update({"method": method, "url": url, "kwargs": kwargs})
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps({"ok": True}).encode()
        response.encoding = "utf-8"
        return response

    monkeypatch.setattr(requests.Session, "request", fake_request)
    item = hub_client.post(
        "/interfaces",
        json=_interface(
            name="订单明细",
            method="POST",
            url="https://service.example/orders/{order_id}",
            open_enabled=False,
            body_type="json",
            body_content='{"include":false}',
            query_params=[{"key": "page", "value": "1"}],
            headers=[{"key": "X-Tenant", "value": "default"}],
            parameter_schema=[
                {"name": "order_id", "location": "path", "required": True},
                {"name": "page", "location": "query"},
                {"name": "X-Tenant", "location": "header"},
                {"name": "include", "location": "body", "value_type": "boolean"},
            ],
        ),
    ).json()
    assert item["config_revision"] == 1

    called = hub_client.post(
        f"/api-hub/internal/interfaces/{item['id']}/invoke",
        headers={"Authorization": "Bearer internal-proxy-test-token"},
        json={
            "interface_revision": 1,
            "path.order_id": "A/B",
            "query.page": 3,
            "headers.X-Tenant": "cn-01",
            "body.include": True,
        },
    )
    assert called.status_code == 200
    assert observed["method"] == "POST"
    assert observed["url"] == "https://service.example/orders/A%2FB"
    assert observed["kwargs"]["params"] == [("page", "3")]
    assert observed["kwargs"]["headers"]["X-Tenant"] == "cn-01"
    assert json.loads(observed["kwargs"]["data"]) == {"include": True}

    changed = hub_client.put(
        f"/interfaces/{item['id']}",
        json=_interface(
            name="订单明细 v2",
            method="POST",
            url="https://service.example/orders/{order_id}",
            body_type="json",
            parameter_schema=[
                {"name": "order_id", "location": "path", "required": True},
            ],
        ),
    ).json()
    assert changed["config_revision"] == 2
    stale = hub_client.post(
        f"/api-hub/internal/interfaces/{item['id']}/invoke",
        headers={"Authorization": "Bearer internal-proxy-test-token"},
        json={"interface_revision": 1, "path": {"order_id": "A100"}},
    )
    assert stale.status_code == 409
    assert "revision=1" in stale.json()["detail"]


def test_internal_n8n_proxy_rejects_personal_variable_placeholders(hub_client):
    """流水线链路无用户身份：含 {{privacy:}}/{{env:}} 的接口明确 400，不把占位符发给上游。"""
    item = hub_client.post(
        "/interfaces",
        json=_interface(headers=[{"key": "Cookie", "value": "{{env:session}}"}]),
    ).json()
    response = hub_client.post(
        f"/api-hub/internal/interfaces/{item['id']}/invoke",
        headers={"Authorization": "Bearer internal-proxy-test-token"},
        json={"interface_revision": item["config_revision"]},
    )
    assert response.status_code == 400
    assert "个人变量" in response.json()["detail"]


def test_http_publication_rejects_personal_variable_placeholders(hub_client):
    """公开代理同样无用户身份：三条发布路径对含占位符的接口一律 400。"""
    item = hub_client.post(
        "/interfaces",
        json=_interface(url="https://service.example/{{env:host}}/x"),
    ).json()
    manual = hub_client.put(
        f"/interfaces/{item['id']}/http-publication",
        json={
            "enabled": True,
            "slug": "pub",
            "query_keys": [],
            "header_keys": [],
            "body_enabled": False,
            "body_keys": [],
        },
    )
    assert manual.status_code == 400
    assert "个人变量" in manual.json()["detail"]

    auto = hub_client.post(f"/interfaces/{item['id']}/http-publication/auto")
    assert auto.status_code == 400
    assert "个人变量" in auto.json()["detail"]

    direct = hub_client.put(
        f"/interfaces/{item['id']}",
        json=_interface(
            url="https://service.example/{{privacy:host}}/x",
            http_enabled=True,
            proxy_slug="pub2",
        ),
    )
    assert direct.status_code == 400
    assert "个人变量" in direct.json()["detail"]

    # 取消发布不受占位符影响（允许下线）
    unpublished = hub_client.put(
        f"/interfaces/{item['id']}/http-publication",
        json={
            "enabled": False,
            "slug": "",
            "query_keys": [],
            "header_keys": [],
            "body_enabled": False,
            "body_keys": [],
        },
    )
    assert unpublished.status_code == 200


def test_preview_run_resolves_env_placeholders_for_current_user(
    hub_client, monkeypatch
):
    """UI 调用链路以本人身份解析 {{env:KEY}}，明文只进上游请求。"""
    observed = {}

    def fake_request(session, method, url, **kwargs):
        observed.update({"url": url, "kwargs": kwargs})
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps({"ok": True}).encode()
        response.encoding = "utf-8"
        return response

    monkeypatch.setattr(requests.Session, "request", fake_request)
    monkeypatch.setattr(
        "app.api_hub.personal_ref._load_env_plaintext",
        lambda keys, user: {f"env:{key}": f"value-{key}" for key in keys},
    )
    response = hub_client.post(
        "/interfaces/preview-run",
        json=_interface(headers=[{"key": "X-Region", "value": "{{env:REGION}}"}]),
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert observed["kwargs"]["headers"]["X-Region"] == "value-REGION"


def test_interface_move_reorders_within_and_across_groups(hub_client):
    first = hub_client.post(
        "/interfaces", json=_interface(name="A", group_name="一组")
    ).json()
    second = hub_client.post(
        "/interfaces", json=_interface(name="B", group_name="一组")
    ).json()
    hub_client.post(
        "/interfaces", json=_interface(name="C", group_name="二组")
    ).json()

    response = hub_client.put(
        f"/interfaces/{second['id']}/move",
        json={"group_name": "一组", "target_index": 0},
    )
    assert response.json() == {"ok": True}
    assert [
        item["name"]
        for item in hub_client.get("/interfaces").json()
        if item["group_name"] == "一组"
    ] == ["B", "A"]

    response = hub_client.put(
        f"/interfaces/{first['id']}/move",
        json={"group_name": "二组", "target_index": 0},
    )
    assert response.json() == {"ok": True}
    items = hub_client.get("/interfaces").json()
    assert [item["name"] for item in items if item["group_name"] == "一组"] == ["B"]
    assert [item["name"] for item in items if item["group_name"] == "二组"] == [
        "A",
        "C",
    ]


def test_run_history_and_response_capture(hub_client, monkeypatch):
    item = hub_client.post("/interfaces", json=_interface()).json()

    def fake_request(session, method, url, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps({"ok": True, "method": method}).encode()
        response.encoding = "utf-8"
        return response

    monkeypatch.setattr(requests.Session, "request", fake_request)
    result = hub_client.post(f"/interfaces/{item['id']}/run")
    assert result.status_code == 200
    assert result.json()["ok"] is True
    assert result.json()["status_code"] == 200

    history = hub_client.get("/runs", params={"keyword": "健康"}).json()
    assert history["total"] == 1
    summary = history["items"][0]
    detail = hub_client.get(
        f"/interfaces/{item['id']}/runs/{summary['id']}"
    ).json()
    assert detail["request_snapshot"]["url"] == item["url"]
    assert json.loads(detail["response_body"])["ok"] is True

    overview = hub_client.get("/runs/overview").json()
    assert overview["total_interfaces"] == 1
    assert overview["executed_interfaces"] == 1
    assert overview["unexecuted_interfaces"] == 0
    assert overview["today_traffic"] == 1
    assert overview["seven_day_traffic"] == 1
    assert overview["seven_day_success"] == 1
    assert overview["seven_day_failed"] == 0
    assert overview["success_rate"] == 100
    assert overview["p95_elapsed_ms"] is not None
    assert overview["slow_threshold_ms"] == 500
    assert sum(item["failed"] for item in overview["daily"]) == 0


def test_run_history_filters_failures_and_slow_calls(hub_client):
    item = hub_client.post("/interfaces", json=_interface()).json()
    created_at = datetime.now(timezone.utc).isoformat()
    with db.get_conn() as conn:
        conn.executemany(
            "INSERT INTO runs(interface_id, ok, status_code, elapsed_ms, "
            "request_snapshot, response_headers, response_body, error, relogin, "
            "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            [
                (item["id"], 1, 200, 120, "{}", "{}", "{}", None, 0, created_at),
                (item["id"], 0, 503, 680, "{}", "{}", "{}", "上游不可用", 0, created_at),
                (item["id"], 1, 200, 900, "{}", "{}", "{}", None, 0, created_at),
            ],
        )

    failed = hub_client.get("/runs", params={"result": "failed"}).json()
    assert failed["total"] == 1
    assert failed["items"][0]["status_code"] == 503

    slow = hub_client.get("/runs", params={"result": "slow"}).json()
    assert slow["total"] == 2
    assert {row["elapsed_ms"] for row in slow["items"]} == {680, 900}

    success = hub_client.get("/runs", params={"result": "success"}).json()
    assert success["total"] == 2

    overview = hub_client.get("/runs/overview").json()
    assert overview["seven_day_traffic"] == 3
    assert overview["seven_day_success"] == 2
    assert overview["seven_day_failed"] == 1
    assert overview["success_rate"] == 66.7
    assert overview["p95_elapsed_ms"] == 900
    assert sum(item["failed"] for item in overview["daily"]) == 1


def test_run_history_keyword_matches_name_or_numeric_run_id(hub_client):
    """H25：纯数字关键词同时按 runs.id 精确匹配，接口名模糊匹配语义不变。"""
    first = hub_client.post("/interfaces", json=_interface()).json()
    second = hub_client.post(
        "/interfaces", json=_interface(name="2026 年度报表")
    ).json()
    created_at = datetime.now(timezone.utc).isoformat()
    with db.get_conn() as conn:
        conn.executemany(
            "INSERT INTO runs(interface_id, ok, status_code, elapsed_ms, "
            "request_snapshot, response_headers, response_body, error, relogin, "
            "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            [
                (first["id"], 1, 200, 120, "{}", "{}", "{}", None, 0, created_at),
                (second["id"], 1, 200, 90, "{}", "{}", "{}", None, 0, created_at),
            ],
        )
        run_ids = [row["id"] for row in conn.execute("SELECT id FROM runs").fetchall()]

    # 纯数字命中 runs.id（两个接口名都不含与 id 撞车的数字）
    by_id = hub_client.get("/runs", params={"keyword": str(run_ids[0])}).json()
    assert by_id["total"] == 1
    assert by_id["items"][0]["id"] == run_ids[0]

    # 纯数字同样能按接口名命中（id 仅为 1/2，与 2026 不可能撞车）
    by_digit_name = hub_client.get("/runs", params={"keyword": "2026"}).json()
    assert by_digit_name["total"] == 1
    assert by_digit_name["items"][0]["interface_id"] == second["id"]

    # 中文名模糊匹配语义不变；数字未命中返回空
    by_name = hub_client.get("/runs", params={"keyword": "健康"}).json()
    assert by_name["total"] == 1
    assert by_name["items"][0]["id"] == run_ids[0]
    numeric_miss = hub_client.get("/runs", params={"keyword": "999999"}).json()
    assert numeric_miss["total"] == 0

    # 上标数字（isdigit 为 True 但非十进制）不得进入 id 分支抛 ValueError→500
    superscript = hub_client.get("/runs", params={"keyword": "2\u00b2"}).json()
    assert superscript["total"] == 0

    # 超长数字串不触发 SQLite 整数绑定溢出，退回名称匹配
    huge = hub_client.get("/runs", params={"keyword": "9" * 40}).json()
    assert huge["total"] == 0


def test_non_2xx_response_is_failure_in_history(
    hub_client, monkeypatch
):
    item = hub_client.post("/interfaces", json=_interface()).json()

    def fake_request(session, method, url, **kwargs):
        response = requests.Response()
        response.status_code = 567
        response.url = url
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps({"error": "blocked"}).encode()
        response.encoding = "utf-8"
        return response

    monkeypatch.setattr(requests.Session, "request", fake_request)

    response = hub_client.post(f"/interfaces/{item['id']}/run")
    assert response.status_code == 200
    result = response.json()
    assert result["ok"] is False
    assert result["status_code"] == 567
    assert result["error"] == "上游返回 HTTP 567"
    assert json.loads(result["response_body"])["error"] == "blocked"

    history = hub_client.get("/runs").json()
    assert history["total"] == 1
    summary = history["items"][0]
    assert summary["ok"] == 0
    assert summary["status_code"] == 567
    assert summary["error"] == "上游返回 HTTP 567"

    detail = hub_client.get(
        f"/interfaces/{item['id']}/runs/{summary['id']}"
    ).json()
    assert detail["ok"] is False
    assert detail["error"] == "上游返回 HTTP 567"
    assert json.loads(detail["response_body"])["error"] == "blocked"


def test_legacy_open_list_proxy_channel_is_retired(hub_client, monkeypatch):
    """旧 /api-hub/proxy/{id} 开放清单通道已随 MCP 开放退役。

    n8n 机器调用只剩 /api-hub/internal/interfaces/{id}/invoke（revision 钉定 +
    参数契约校验，见 test_internal_n8n_proxy_validates_dynamic_parameters_and_revision）。
    """
    item = hub_client.post("/interfaces", json=_interface(open_enabled=True)).json()
    monkeypatch.setattr(config, "SYSTEM_MCP_TOKEN", "proxy-token")
    unauthenticated = hub_client.post(
        f"/api-hub/proxy/{item['id']}", json={"query": {"page": 3}}
    )
    assert unauthenticated.status_code == 404
    authenticated = hub_client.post(
        f"/api-hub/proxy/{item['id']}",
        headers={"Authorization": "Bearer proxy-token"},
    )
    assert authenticated.status_code == 404

    # 内部代理保持 fail-closed：未带服务令牌一律 401。
    assert hub_client.post(
        f"/api-hub/internal/interfaces/{item['id']}/invoke", json={}
    ).status_code == 401


def test_backup_round_trip_skips_duplicates(hub_client):
    item = hub_client.post(
        "/interfaces",
        json=_interface(
            url="https://service.example/health?token=url-secret",
            query_params=[
                {"key": "verbose", "value": "1"},
                {"key": "api_key", "value": "query-secret"},
            ],
            headers=[{"key": "Authorization", "value": "Bearer header-secret"}],
            body_type="json",
            body_content='{"password":"body-secret"}',
        ),
    ).json()
    exported = hub_client.post(
        "/backup/export",
        json={"name": "review-copy", "mode": "full", "ids": []},
    )
    assert exported.status_code == 200
    payload = exported.json()
    assert payload["app"] == "API-Hub"
    assert payload["interface_count"] == 1
    assert payload["includes_sensitive_values"] is False
    portable = payload["interfaces"][0]
    assert "url-secret" not in portable["url"]
    assert portable["query_params"][1]["value"] == ""
    assert portable["headers"] == []
    assert portable["body_content"] == ""
    assert portable["sensitive_values_omitted"] is True

    sensitive_export = hub_client.post(
        "/backup/export",
        json={
            "name": "review-copy-sensitive",
            "mode": "full",
            "ids": [],
            "include_sensitive": True,
        },
    ).json()
    assert sensitive_export["includes_sensitive_values"] is True
    sensitive_portable = sensitive_export["interfaces"][0]
    assert sensitive_portable["headers"][0]["value"] == "Bearer header-secret"
    assert "body-secret" in sensitive_portable["body_content"]

    duplicate = hub_client.post("/backup/import", json=payload).json()
    assert duplicate["imported"] == 0
    assert duplicate["skipped"] == 1

    assert hub_client.delete(f"/interfaces/{item['id']}").status_code == 200
    restored = hub_client.post("/backup/import", json=payload).json()
    assert restored["imported"] == 1
    restored_item = hub_client.get("/interfaces").json()[0]
    assert restored_item["name"] == "健康检查"
    assert restored_item["mcp_enabled"] is False
    assert restored_item["open_enabled"] is False
    assert restored_item["http_enabled"] is False


def test_backup_import_silently_ignores_legacy_use_w3_field(hub_client):
    # A backup file produced before the W3 login-injection removal may still
    # carry a use_w3 field on each interface. InterfaceIn follows Pydantic
    # default extra=ignore, so the legacy field must not break import and the
    # created interface must never expose use_w3.
    legacy_payload = {
        "app": "API-Hub",
        "version": 7,
        "name": "legacy-with-use-w3",
        "exported_at": "2024-01-01T00:00:00+00:00",
        "mode": "full",
        "interface_count": 1,
        "interfaces": [
            {
                **_interface(name="legacy-w3-iface"),
                "use_w3": True,
            }
        ],
    }
    imported = hub_client.post("/backup/import", json=legacy_payload)
    assert imported.status_code == 200
    assert imported.json()["imported"] == 1

    restored = hub_client.get("/interfaces").json()
    assert len(restored) == 1
    assert restored[0]["name"] == "legacy-w3-iface"
    assert "use_w3" not in restored[0]


def test_preview_run_uses_draft_without_saving_and_keeps_full_history(
    hub_client, monkeypatch
):
    item = hub_client.post("/interfaces", json=_interface(name="已保存名称")).json()
    observed = {}

    def fake_request(session, method, url, **kwargs):
        observed["verify"] = session.verify
        observed["method"] = method
        observed["url"] = url
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(
            {
                "ok": True,
                "nested": '{"password":"upstream-secret"}',
            }
        ).encode()
        response.encoding = "utf-8"
        return response

    monkeypatch.setattr(requests.Session, "request", fake_request)
    response = hub_client.post(
        "/interfaces/preview-run",
        json={
            **_interface(name="未保存草稿", method="OPTIONS"),
            "id": item["id"],
        },
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert observed == {
        "verify": True,
        "method": "OPTIONS",
        "url": "https://service.example/health",
    }
    assert hub_client.get(f"/interfaces/{item['id']}").json()["name"] == "已保存名称"

    history = hub_client.get(f"/interfaces/{item['id']}/runs").json()
    detail = hub_client.get(
        f"/interfaces/{item['id']}/runs/{history[0]['id']}"
    ).json()
    assert "upstream-secret" in detail["response_body"]


def test_single_worker_request_gate_fails_fast_when_saturated(monkeypatch):
    gate = threading.BoundedSemaphore(1)
    assert gate.acquire(blocking=False)
    monkeypatch.setattr(executor, "_REQUEST_GATE", gate)
    monkeypatch.setattr(config, "REQUEST_QUEUE_TIMEOUT", 0)

    result = executor.run_interface(
        {
            "id": None,
            "name": "过载保护",
            "method": "GET",
            "url": "https://service.example/health",
            "query_params": [],
            "headers": [],
            "body_type": "none",
            "body_content": "",
        }
    )
    assert result["error_type"] == "overloaded"
    assert result["error"] == "接口调用繁忙，请稍后重试"


def test_sqlite_audit_contention_does_not_fail_completed_upstream_call(monkeypatch):
    def fake_request(_session, _method, url, **_kwargs):
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response.headers["Content-Type"] = "application/json"
        response._content = b'{"ok":true}'
        return response

    def busy_connection():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(requests.Session, "request", fake_request)
    monkeypatch.setattr(executor.db, "get_conn", busy_connection)
    monkeypatch.setattr(config, "OUTBOUND_BLOCK_PRIVATE_NETWORKS", False)
    result = executor.run_interface(
        {
            "id": 123,
            "name": "审计争用",
            "method": "GET",
            "url": "https://service.example/health",
            "query_params": [],
            "headers": [],
            "body_type": "none",
            "body_content": "",
        }
    )
    assert result["ok"] is True
    assert result["status_code"] == 200
    assert "run_id" not in result


def test_outbound_urls_block_private_targets_but_keep_explicit_trusted_hosts(
    hub_client, monkeypatch
):
    monkeypatch.setattr(config, "OUTBOUND_BLOCK_PRIVATE_NETWORKS", True)
    monkeypatch.setattr(config, "OUTBOUND_TRUSTED_HOSTS", ())

    def resolve(host, _port, **_kwargs):
        address = "10.8.0.12" if host in {"private.example", "intranet.example"} else "8.8.8.8"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    monkeypatch.setattr(outbound_security.socket, "getaddrinfo", resolve)
    assert validate_outbound_url("https://public.example/data").startswith("https://")
    with pytest.raises(OutboundTargetError, match="受保护的内网地址"):
        validate_outbound_url("https://private.example/data")
    assert validate_outbound_url(
        "https://intranet.example/msa/service",
        trusted_hosts=("intranet.example",),
    ).startswith("https://")
    assert validate_outbound_url(
        "http://127.0.0.1:8000/private",
        trusted_hosts=("127.0.0.1",),
    ).startswith("http://")
    with pytest.raises(OutboundTargetError):
        validate_outbound_url("file:///etc/passwd")
    with pytest.raises(OutboundTargetError):
        validate_outbound_url("https://user:password@public.example/data")

    from app.main import app as platform_app

    # MCP 开放与旧开放清单代理通道已退役：路由与中间件不再声明这些路径。
    client = TestClient(platform_app)
    assert client.post(
        "/api-hub/mcp", headers={"Content-Type": "application/json"}, json={}
    ).status_code == 404
    assert client.post(
        "/api-hub/mcp/system",
        headers={"Content-Type": "application/json"},
        json={},
    ).status_code == 404
    assert client.post("/api-hub/proxy/1").status_code == 404


def test_outbound_redirect_target_is_revalidated(monkeypatch):
    monkeypatch.setattr(config, "OUTBOUND_BLOCK_PRIVATE_NETWORKS", True)
    monkeypatch.setattr(config, "OUTBOUND_TRUSTED_HOSTS", ())

    def resolve(host, _port, **_kwargs):
        address = "10.8.0.12" if host == "private.example" else "8.8.8.8"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    class RedirectSession:
        def request(self, *_args, **_kwargs):
            response = requests.Response()
            response.status_code = 302
            response.headers["Location"] = "https://private.example/internal"
            return response

    monkeypatch.setattr(outbound_security.socket, "getaddrinfo", resolve)
    with pytest.raises(OutboundTargetError, match="受保护的内网地址"):
        request_with_safe_redirects(
            RedirectSession(), "GET", "https://public.example/start"
        )


def test_cross_origin_redirect_drops_configured_credentials(monkeypatch):
    monkeypatch.setattr(config, "OUTBOUND_BLOCK_PRIVATE_NETWORKS", True)

    def resolve(_host, _port, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    class RedirectSession:
        def __init__(self):
            self.calls = []

        def request(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            response = requests.Response()
            response.status_code = 302 if len(self.calls) == 1 else 200
            if response.status_code == 302:
                response.headers["Location"] = "https://other.example/next"
            return response

    monkeypatch.setattr(outbound_security.socket, "getaddrinfo", resolve)
    session = RedirectSession()
    request_with_safe_redirects(
        session,
        "GET",
        "https://public.example/start",
        headers={
            "Authorization": "Bearer platform-token",
            "X-Api-Key": "platform-key",
            "X-Tenant": "tenant-a",
        },
        cookies={"caller": "cookie-value"},
    )
    second_kwargs = session.calls[1][1]
    assert second_kwargs["headers"] == {"X-Tenant": "tenant-a"}
    assert "cookies" not in second_kwargs


# ---------------------------------------------------------------------------
# 接口私有可见：非 admin 只能看到/改/调自己 created_by 的接口
# ---------------------------------------------------------------------------

def _make_client_as(tmp_path, monkeypatch, user):
    """Build a hub_client whose get_current_user returns ``user``."""
    from app.deps import get_current_user
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "api_hub.db")
    monkeypatch.setattr(config, "INTERNAL_PROXY_TOKEN", "internal-proxy-test-token")
    monkeypatch.setattr(config, "OUTBOUND_BLOCK_PRIVATE_NETWORKS", False)
    db.init_db()
    app = FastAPI()
    app.include_router(interfaces.router)
    app.include_router(interfaces.runs_router)
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _user(uid, role="editor"):
    return SimpleNamespace(id=uid, role=role, is_active=True)


def test_non_admin_only_sees_own_interfaces(tmp_path, monkeypatch):
    alice = _user("alice-id")
    bob = _user("bob-id")
    # Alice creates an interface
    ca = _make_client_as(tmp_path, monkeypatch, alice)
    created = ca.post("/interfaces", json=_interface(name="Alice's API"))
    assert created.status_code == 200
    assert created.json()["created_by"] == "alice-id"

    # Bob cannot see Alice's interface
    cb = _make_client_as(tmp_path, monkeypatch, bob)
    listed = cb.get("/interfaces").json()
    assert listed == []

    # Bob creates his own
    created_b = cb.post("/interfaces", json=_interface(name="Bob's API"))
    assert created_b.json()["created_by"] == "bob-id"
    listed_b = cb.get("/interfaces").json()
    assert [r["name"] for r in listed_b] == ["Bob's API"]

    # Alice now sees only her own
    listed_a = ca.get("/interfaces").json()
    assert [r["name"] for r in listed_a] == ["Alice's API"]


def test_non_admin_cannot_get_update_delete_others_interface(tmp_path, monkeypatch):
    alice = _user("alice-id")
    bob = _user("bob-id")
    ca = _make_client_as(tmp_path, monkeypatch, alice)
    created = ca.post("/interfaces", json=_interface(name="Alice's API"))
    iid = created.json()["id"]

    cb = _make_client_as(tmp_path, monkeypatch, bob)
    # GET 404
    assert cb.get(f"/interfaces/{iid}").status_code == 404
    # PUT 404
    assert cb.put(f"/interfaces/{iid}", json=_interface(name="Hijacked")).status_code == 404
    # DELETE 404
    assert cb.delete(f"/interfaces/{iid}").status_code == 404
    # Alice still owns it
    assert ca.get(f"/interfaces/{iid}").status_code == 200


def test_admin_sees_all_interfaces(tmp_path, monkeypatch):
    admin = _user("admin-id", role="admin")
    alice = _user("alice-id")
    ca = _make_client_as(tmp_path, monkeypatch, alice)
    ca.post("/interfaces", json=_interface(name="Alice's API"))

    cadmin = _make_client_as(tmp_path, monkeypatch, admin)
    listed = cadmin.get("/interfaces").json()
    assert [r["name"] for r in listed] == ["Alice's API"]
    # Admin can GET/PUT/DELETE any interface
    iid = listed[0]["id"]
    assert cadmin.get(f"/interfaces/{iid}").status_code == 200
