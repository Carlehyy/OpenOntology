from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api_hub import config, db
from app.api_hub.assistant_mcp import (
    BUILTIN_KEY,
    ApiHubMcpError,
    execute_tool,
    tool_manifest,
)
from app.auth.models import RoleMenuPermission, User, UserEnvVar, UserPrivacyVar
from app.shared.database import Base
from app.super_assistant import mcp_server_service
from app.super_assistant.kernel.models import CapabilityRevision
from app.super_assistant.models import SuperAssistantMcpServer


@pytest.fixture
def hub_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "api_hub.db")
    monkeypatch.setattr(config, "OUTBOUND_BLOCK_PRIVATE_NETWORKS", False)
    db.init_db()
    return tmp_path


def _user(uid: str, role: str = "admin") -> SimpleNamespace:
    return SimpleNamespace(id=uid, role=role, is_active=True)


def _run(user, name, arguments=None, monkeypatch=None, allowed=True):
    if monkeypatch is not None:
        monkeypatch.setattr(
            "app.api_hub.assistant_mcp.user_has_menu_access",
            lambda *_args, **_kwargs: allowed,
        )
    raw = execute_tool(
        SimpleNamespace(),
        user=user,
        name=name,
        arguments=arguments or {},
    )
    return json.loads(raw)


def test_tool_manifest_covers_every_handler():
    names = {item["name"] for item in tool_manifest()}
    assert names == {
        "list_interfaces",
        "get_interface",
        "create_interface",
        "update_interface",
        "delete_interface",
        "call_interface",
        "list_groups",
        "rename_group",
        "delete_group",
        "move_interface",
        "set_http_publication",
        "auto_http_publication",
        "list_proxy_keys",
        "create_proxy_key",
        "update_proxy_key",
        "delete_proxy_key",
        "list_env_vars",
        "set_env_var",
        "delete_env_var",
        "list_privacy_vars",
        "get_privacy_var",
        "set_privacy_var",
        "delete_privacy_var",
    }


def test_execute_requires_interface_menu(monkeypatch):
    monkeypatch.setattr(
        "app.api_hub.assistant_mcp.user_has_menu_access",
        lambda *_args, **_kwargs: False,
    )
    with pytest.raises(ApiHubMcpError, match="接口管理"):
        execute_tool(
            SimpleNamespace(),
            user=_user("editor-1", "editor"),
            name="list_interfaces",
            arguments={},
        )


def test_crud_call_groups_and_owner_scope(hub_db, monkeypatch):
    admin = _user("test-admin-id")
    alice = _user("alice-id", "editor")
    bob = _user("bob-id", "editor")

    created = _run(
        alice,
        "create_interface",
        {
            "name": "Alice 订单",
            "url": "https://vendor.example/orders/{id}",
            "method": "GET",
            "group": "订单",
            "headers": {"Authorization": "Bearer secret-token", "X-Trace": "t-1"},
            "query_params": {"verbose": "1"},
        },
        monkeypatch,
    )
    alice_id = created["interface"]["id"]
    assert created["interface"]["headers"] == [
        {"key": "Authorization", "value": "Bearer secret-token"},
        {"key": "X-Trace", "value": "t-1"},
    ]
    assert "密钥" in created["interface"]["notice"]

    bob_created = _run(
        bob,
        "create_interface",
        {"name": "Bob 库存", "url": "https://vendor.example/stock", "group": "库存"},
        monkeypatch,
    )
    listed_bob = _run(bob, "list_interfaces", {}, monkeypatch)
    assert [item["name"] for item in listed_bob["interfaces"]] == ["Bob 库存"]

    with pytest.raises(ApiHubMcpError, match="不存在"):
        _run(bob, "get_interface", {"interface_id": alice_id}, monkeypatch)

    fetched = _run(alice, "get_interface", {"interface_id": alice_id}, monkeypatch)
    revision = fetched["interface"]["configRevision"]
    updated = _run(
        alice,
        "update_interface",
        {
            "interface_id": alice_id,
            "expected_revision": revision,
            "changes": {"description": "查订单", "headers": {"X-Trace": "t-2"}},
        },
        monkeypatch,
    )
    assert updated["interface"]["description"] == "查订单"
    assert updated["interface"]["headers"] == [
        {"key": "Authorization", "value": "Bearer secret-token"},
        {"key": "X-Trace", "value": "t-2"},
    ]
    with pytest.raises(ApiHubMcpError, match="revision"):
        _run(
            alice,
            "update_interface",
            {
                "interface_id": alice_id,
                "expected_revision": revision,
                "changes": {"description": "stale"},
            },
            monkeypatch,
        )

    groups = _run(alice, "list_groups", {}, monkeypatch)
    assert groups["groups"] == [{"name": "订单", "count": 1}]
    renamed = _run(
        alice,
        "rename_group",
        {"old_name": "订单", "new_name": "交易"},
        monkeypatch,
    )
    assert renamed["count"] == 1
    _run(
        alice,
        "move_interface",
        {"interface_id": alice_id, "group": "交易", "target_index": 0},
        monkeypatch,
    )

    observed = {}

    def fake_request(session, method, url, **kwargs):
        observed.update({"method": method, "url": url, "kwargs": kwargs})
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps({"order": 9}).encode()
        response.encoding = "utf-8"
        return response

    monkeypatch.setattr(requests.Session, "request", fake_request)
    called = _run(
        alice,
        "call_interface",
        {"interface_id": alice_id, "path": {"id": "A-1"}},
        monkeypatch,
    )
    assert called["run"]["ok"] is True
    assert called["run"]["responseBody"]
    assert observed["url"].endswith("/orders/A-1")
    assert called["run"]["id"]

    before_publish = _run(alice, "get_interface", {"interface_id": alice_id}, monkeypatch)
    revision = before_publish["interface"]["configRevision"]
    published = _run(
        alice,
        "auto_http_publication",
        {"interface_id": alice_id},
        monkeypatch,
    )
    assert published["interface"]["httpPublished"] is True
    assert published["interface"]["proxySlug"]
    assert published["interface"]["configRevision"] == revision
    unpublished = _run(
        alice,
        "set_http_publication",
        {"interface_id": alice_id, "enabled": False},
        monkeypatch,
    )
    assert unpublished["interface"]["httpPublished"] is False
    assert unpublished["interface"]["configRevision"] == revision
    with pytest.raises(ApiHubMcpError, match="true 或 false"):
        _run(
            alice,
            "set_http_publication",
            {"interface_id": alice_id, "enabled": "false"},
            monkeypatch,
        )

    key = _run(
        alice,
        "create_proxy_key",
        {"name": "Alice 调用方", "interface_ids": [alice_id]},
        monkeypatch,
    )
    assert key["key"]["secret"].startswith("hub_")
    with pytest.raises(ApiHubMcpError, match="授权全部"):
        _run(
            alice,
            "create_proxy_key",
            {"name": "全量", "scope_all": True},
            monkeypatch,
        )
    with pytest.raises(ApiHubMcpError, match="不属于当前用户"):
        _run(
            alice,
            "create_proxy_key",
            {
                "name": "越权",
                "interface_ids": [bob_created["interface"]["id"]],
            },
            monkeypatch,
        )

    admin_keys = _run(admin, "list_proxy_keys", {}, monkeypatch)
    assert admin_keys["count"] >= 1

    _run(alice, "delete_interface", {"interface_id": alice_id}, monkeypatch)
    listed_alice = _run(alice, "list_interfaces", {}, monkeypatch)
    assert listed_alice["interfaces"] == []


def test_delete_group_moves_owned_interfaces_only(hub_db, monkeypatch):
    alice = _user("alice-id", "editor")
    bob = _user("bob-id", "editor")
    _run(
        alice,
        "create_interface",
        {"name": "A", "url": "https://a.example/x", "group": "共享名"},
        monkeypatch,
    )
    _run(
        bob,
        "create_interface",
        {"name": "B", "url": "https://b.example/x", "group": "共享名"},
        monkeypatch,
    )
    result = _run(alice, "delete_group", {"group_name": "共享名"}, monkeypatch)
    assert result["count"] == 1
    assert _run(alice, "list_groups", {}, monkeypatch)["groups"] == [
        {"name": "默认分组", "count": 1},
    ]
    assert _run(bob, "list_groups", {}, monkeypatch)["groups"] == [
        {"name": "共享名", "count": 1},
    ]


def test_install_platform_api_hub_mcp_is_owner_scoped(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "api_hub.db")
    db.init_db()
    engine = create_engine(f"sqlite:///{tmp_path / 'sa.db'}")
    Base.metadata.create_all(
        bind=engine,
        tables=[
            User.__table__,
            RoleMenuPermission.__table__,
            SuperAssistantMcpServer.__table__,
            CapabilityRevision.__table__,
        ],
    )
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    admin = User(
        id="admin-1",
        username="admin",
        email="admin@example.com",
        password_hash="unused",
        role="admin",
    )
    editor = User(
        id="editor-1",
        username="editor",
        email="editor@example.com",
        password_hash="unused",
        role="editor",
    )
    with Session() as session:
        session.add_all([admin, editor])
        session.commit()
        installed = mcp_server_service.install_platform_api_hub_mcp(session, admin.id)
        assert installed.builtin_key == BUILTIN_KEY
        assert installed.name == "platform_api_hub"
        assert installed.url == "builtin://api-hub"
        assert installed.require_confirmation is True
        assert {item["name"] for item in installed.tool_manifest} == {
            item["name"] for item in tool_manifest()
        }
        again = mcp_server_service.install_platform_api_hub_mcp(session, admin.id)
        assert again.id == installed.id
        with pytest.raises(mcp_server_service.McpServerValidationError, match="接口管理"):
            mcp_server_service.install_platform_api_hub_mcp(session, editor.id)


def test_env_and_privacy_vars_are_owner_scoped(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "app.api_hub.assistant_mcp.user_has_menu_access",
        lambda *_args, **_kwargs: True,
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'vars.db'}")
    Base.metadata.create_all(
        bind=engine,
        tables=[User.__table__, UserEnvVar.__table__, UserPrivacyVar.__table__],
    )
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    alice = User(
        id="alice-id",
        username="alice",
        email="alice@example.com",
        password_hash="unused",
        role="editor",
    )
    bob = User(
        id="bob-id",
        username="bob",
        email="bob@example.com",
        password_hash="unused",
        role="editor",
    )
    with Session() as session:
        session.add_all([alice, bob])
        session.commit()

        def run(user, name, arguments=None):
            return json.loads(execute_tool(session, user=user, name=name, arguments=arguments or {}))

        env = run(alice, "set_env_var", {"key": "REGION", "value": "cn-east-1"})
        assert next(iter(env)) == "notice"
        assert env["variable"]["placeholder"] == "{{env:REGION}}"
        assert env["variable"]["created"] is True
        listed = run(alice, "list_env_vars")
        assert next(iter(listed)) == "notice"
        assert [(item["key"], item["value"]) for item in listed["variables"]] == [
            ("REGION", "cn-east-1"),
        ]
        assert run(bob, "list_env_vars")["variables"] == []

        run(alice, "set_env_var", {"key": "REGION", "value": "cn-north-1"})
        assert run(alice, "list_env_vars")["variables"][0]["value"] == "cn-north-1"
        run(alice, "delete_env_var", {"key": "REGION"})
        assert run(alice, "list_env_vars")["variables"] == []
        with pytest.raises(ApiHubMcpError, match="不存在"):
            run(alice, "delete_env_var", {"key": "REGION"})
        with pytest.raises(ApiHubMcpError, match="变量名"):
            run(alice, "set_env_var", {"key": "BAD KEY", "value": "x"})

        privacy = run(alice, "set_privacy_var", {"key": "COOKIE", "value": "sid=secret"})
        assert privacy["variable"]["placeholder"] == "{{privacy:COOKIE}}"
        assert privacy["variable"]["hasValue"] is True
        assert run(bob, "list_privacy_vars")["variables"] == []
        with pytest.raises(ApiHubMcpError, match="不存在"):
            run(bob, "get_privacy_var", {"key": "COOKIE"})
        fetched = run(alice, "get_privacy_var", {"key": "COOKIE"})
        assert fetched["variable"]["value"] == "sid=secret"
        run(alice, "delete_privacy_var", {"key": "COOKIE"})
        assert run(alice, "list_privacy_vars")["variables"] == []
