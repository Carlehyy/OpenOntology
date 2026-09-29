"""世界模型域测试 — JKG 边界（execute_code）一律 mock，测试不起真实内核。"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.auth.models import RoleMenuPermission, User
from app.services.auth_service import hash_password
from app.world_model import service
from app.world_model.models import WorldModelCallRecord

BASE = "/api/v2/world-model"


# ──────────────────────────── 工具与夹具 ────────────────────────────


def _make_user(db, username: str, role: str) -> User:
    user = User(
        id=str(uuid.uuid4()),
        username=username,
        email=f"{username}@test.com",
        password_hash=hash_password("test123"),
        role=role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _login(client, username: str) -> dict:
    r = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "test123"},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['data']['access_token']}"}


@pytest.fixture
def viewer_headers(client, db):
    _make_user(db, "wm_viewer", "viewer")
    return _login(client, "wm_viewer")


@pytest.fixture
def custom_headers(client, db):
    """custom 角色默认只有 overview 菜单，用于验证 403。"""
    _make_user(db, "wm_custom", "custom")
    return _login(client, "wm_custom")


@pytest.fixture
def project(client, auth_headers):
    r = client.post(
        f"{BASE}/projects",
        json={"name": "负荷推演", "description": "台区负荷短期推演",
              "engine_type": "statistical"},
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]


class _FakeExecution:
    """execute_code 的最小替身：error 为空即成功。"""

    def __init__(self, *, stdout="", error=None, traceback="", duration_ms=12):
        self.rows = []
        self.stdout = stdout
        self.error = error
        self.traceback = traceback
        self.duration_ms = duration_ms
        self.kernel_id = "fake-kernel"


def _fake_execute_ok(code, **kwargs):
    assert "simulate" in code  # 收尾代码应注入 simulate 调用
    return _FakeExecution(
        stdout='log\n__OB_RESULT_BEGIN__\n{"trajectory": [1, 2]}\n'
               "__OB_RESULT_END__\n")


def _fake_execute_fail(code, **kwargs):
    return _FakeExecution(
        stdout="boom", error="脚本执行失败（ValueError）：bad",
        traceback="ValueError: bad")


# ──────────────────────────── 项目 CRUD ────────────────────────────


def test_project_crud_flow(client, auth_headers, project):
    pid = project["id"]
    assert project["status"] == "draft"
    assert "def simulate(" in project["script"]  # 初始化为契约模板

    r = client.get(f"{BASE}/projects", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["data"]["total"] == 1

    r = client.get(
        f"{BASE}/projects", params={"keyword": "负荷"}, headers=auth_headers)
    assert r.json()["data"]["total"] == 1
    r = client.get(
        f"{BASE}/projects", params={"keyword": "不存在"}, headers=auth_headers)
    assert r.json()["data"]["total"] == 0
    r = client.get(
        f"{BASE}/projects", params={"engine_type": "mechanistic"},
        headers=auth_headers)
    assert r.json()["data"]["total"] == 0
    r = client.get(
        f"{BASE}/projects", params={"engine_type": "bogus"},
        headers=auth_headers)
    assert r.status_code == 400

    r = client.patch(
        f"{BASE}/projects/{pid}",
        json={"name": "负荷推演-v2", "engine_type": "mechanistic"},
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert r.json()["data"]["name"] == "负荷推演-v2"
    assert r.json()["data"]["engine_type"] == "mechanistic"

    r = client.delete(f"{BASE}/projects/{pid}", headers=auth_headers)
    assert r.status_code == 200
    r = client.get(f"{BASE}/projects/{pid}", headers=auth_headers)
    assert r.status_code == 404


def test_projects_pagination(client, auth_headers):
    """列表走服务端分页：total 为筛选后总数，page/size 生效。"""
    for index in range(3):
        r = client.post(
            f"{BASE}/projects",
            json={"name": f"模型-{index}", "description": "",
                  "engine_type": "statistical"},
            headers=auth_headers,
        )
        assert r.status_code == 201, r.text

    r = client.get(
        f"{BASE}/projects", params={"page": 1, "size": 2}, headers=auth_headers)
    data = r.json()["data"]
    assert data["total"] == 3
    assert len(data["items"]) == 2

    r = client.get(
        f"{BASE}/projects", params={"page": 2, "size": 2}, headers=auth_headers)
    data = r.json()["data"]
    assert data["total"] == 3
    assert len(data["items"]) == 1

    # keyword 收窄后 total 同步收窄（服务端全量筛选，不受单页 500 条上限影响）
    r = client.get(
        f"{BASE}/projects",
        params={"keyword": "模型-2", "page": 1, "size": 2},
        headers=auth_headers,
    )
    data = r.json()["data"]
    assert data["total"] == 1
    assert data["items"][0]["name"] == "模型-2"


def test_projects_require_world_model_menu(client, custom_headers):
    r = client.get(f"{BASE}/projects", headers=custom_headers)
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "MENU_ACCESS_DENIED"


def test_editor_role_sees_world_model_by_default(client, viewer_headers):
    """无显式授权记录的角色走默认全集（除 api_hub），世界模型默认可见。"""
    r = client.get(f"{BASE}/projects", headers=viewer_headers)
    assert r.status_code == 200


def test_world_model_group_requires_child_after_normalize(db):
    """GROUP_MENU_KEYS 归一化：world_model 父 key 需至少一个子 key 才能保留。

    世界模型提升为一级导航分组（models/calls 子 key）；本体管理恢复单项，
    不再受分组归一化约束；旧 ontologies.* 子 key 已失效，归一化直接滤除。
    """
    from app.auth.permissions import normalize_menu_keys

    assert "world_model" not in normalize_menu_keys(["world_model"])
    assert "world_model" in normalize_menu_keys(
        ["world_model", "world_model.models"])
    # 只有子 key 时自动补父 key
    assert "world_model" in normalize_menu_keys(["world_model.calls"])
    assert "world_model" in normalize_menu_keys(["world_model.services"])
    assert "world_model.models" in normalize_menu_keys(["world_model.models"])
    # 本体管理恢复单项：独立保留
    assert "ontologies" in normalize_menu_keys(["ontologies"])
    # 旧 key 已不在 ALL_MENU_KEYS，归一化滤除
    assert "ontologies.library" not in normalize_menu_keys(["ontologies.library"])
    assert "ontologies.world_model" not in normalize_menu_keys(
        ["ontologies.world_model"])


def test_role_record_with_world_model_keys_grants_access(db):
    """存量授权记录持有一级 world_model 组 key 时，访问世界模型域恢复。"""
    db.add(RoleMenuPermission(
        role="editor",
        menu_keys=["ontologies", "world_model", "world_model.models",
                   "world_model.calls"],
        updated_by="test",
    ))
    db.commit()
    from app.auth.permissions import get_role_menu_keys

    keys = get_role_menu_keys(db, "editor")
    assert "ontologies" in keys
    assert "world_model" in keys
    assert "world_model.models" in keys
    assert "world_model.calls" in keys


# ──────────────────────────── 调试执行与保存 ────────────────────────────


def test_execute_returns_simulate_payload(
    client, auth_headers, project, monkeypatch,
):
    monkeypatch.setattr(service, "execute_code", _fake_execute_ok)
    r = client.post(
        f"{BASE}/projects/{project['id']}/execute",
        json={"script": "def simulate(context, actions, horizon):\n"
                        "    return {'trajectory': [1, 2]}",
              "test_input": {"context": {"current_value": 1}, "actions": [],
                             "horizon": 2}},
        headers=auth_headers,
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["ok"] is True
    assert data["payload"] == {"trajectory": [1, 2]}
    assert data["error"] is None


def test_execute_surfaces_script_error(
    client, auth_headers, project, monkeypatch,
):
    monkeypatch.setattr(service, "execute_code", _fake_execute_fail)
    r = client.post(
        f"{BASE}/projects/{project['id']}/execute",
        json={"script": "x = 1", "test_input": {}},
        headers=auth_headers,
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["ok"] is False
    assert "ValueError" in data["error"]


def test_execute_gateway_unreachable_returns_502(
    client, auth_headers, project, monkeypatch,
):
    from app.data_channel.pipelines.python_engine.client import (
        PythonEngineError,
    )

    def _boom(code, **kwargs):
        raise PythonEngineError("Python 执行网关未配置")

    monkeypatch.setattr(service, "execute_code", _boom)
    r = client.post(
        f"{BASE}/projects/{project['id']}/execute",
        json={"script": "x = 1", "test_input": {}},
        headers=auth_headers,
    )
    assert r.status_code == 502
    assert "网关" in str(r.json()["detail"])


def test_save_requires_successful_execution(
    client, auth_headers, project, monkeypatch,
):
    monkeypatch.setattr(service, "execute_code", _fake_execute_fail)
    r = client.post(
        f"{BASE}/projects/{project['id']}/save",
        json={"script": "def simulate(context, actions, horizon):\n"
                        "    return {}",
              "test_input": {}},
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert r.json()["data"]["ok"] is False

    # 保存失败不落版本
    r = client.get(
        f"{BASE}/projects/{project['id']}/versions", headers=auth_headers)
    assert r.json()["data"] == []


def test_save_freezes_version_and_prunes(
    client, auth_headers, project, db, monkeypatch,
):
    monkeypatch.setattr(service, "execute_code", _fake_execute_ok)
    pid = project["id"]
    script = "def simulate(context, actions, horizon):\n    return {}"
    for _ in range(3):
        r = client.post(
            f"{BASE}/projects/{pid}/save",
            json={"script": script, "test_input": {"horizon": 3}},
            headers=auth_headers,
        )
        assert r.json()["data"]["ok"] is True

    r = client.get(f"{BASE}/projects/{pid}", headers=auth_headers)
    assert r.json()["data"]["script"] == script

    r = client.get(f"{BASE}/projects/{pid}/versions", headers=auth_headers)
    versions = r.json()["data"]
    assert [v["version_no"] for v in versions] == [3, 2, 1]
    assert versions[0]["test_input"]["horizon"] == 3

    r = client.get(
        f"{BASE}/projects/{pid}/versions/{versions[1]['id']}",
        headers=auth_headers)
    assert r.json()["data"]["script"] == script

    r = client.get(
        f"{BASE}/projects/{pid}/versions/nonexistent", headers=auth_headers)
    assert r.status_code == 404


def test_versions_of_deleted_project_are_cascaded(
    client, auth_headers, project, db, monkeypatch,
):
    from app.world_model.models import WorldModelScriptVersion

    monkeypatch.setattr(service, "execute_code", _fake_execute_ok)
    pid = project["id"]
    client.post(
        f"{BASE}/projects/{pid}/save",
        json={"script": "def simulate(context, actions, horizon):\n"
                        "    return {}",
              "test_input": {}},
        headers=auth_headers,
    )
    assert db.query(WorldModelScriptVersion).count() == 1

    client.delete(f"{BASE}/projects/{pid}", headers=auth_headers)
    db.expire_all()
    assert db.query(WorldModelScriptVersion).count() == 0


# ──────────────────────────── 调用记录（只读） ────────────────────────────


def _seed_calls(db):
    db.add_all([
        WorldModelCallRecord(
            service_name="负荷推演", caller="agent-session-1", ok=True,
            duration_ms=120, request_payload={"horizon": 6},
            response_payload={"trajectory": [1, 2]}),
        WorldModelCallRecord(
            service_name="负荷推演", caller="agent-session-2", ok=False,
            duration_ms=40, error="超时", request_payload={}),
        WorldModelCallRecord(
            service_name="潮流仿真", caller="manual", ok=True,
            duration_ms=350),
    ])
    db.commit()


def test_call_records_list_filter_and_overview(client, auth_headers, db):
    _seed_calls(db)

    r = client.get(f"{BASE}/calls", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["data"]["total"] == 3

    r = client.get(
        f"{BASE}/calls", params={"result": "failed"}, headers=auth_headers)
    assert r.json()["data"]["total"] == 1
    assert r.json()["data"]["items"][0]["ok"] is False

    r = client.get(
        f"{BASE}/calls", params={"keyword": "潮流"}, headers=auth_headers)
    assert r.json()["data"]["total"] == 1

    r = client.get(f"{BASE}/calls/overview", headers=auth_headers)
    overview = r.json()["data"]
    assert overview == {"total": 3, "failed": 1, "avg_duration_ms": 170}

    r = client.get(f"{BASE}/calls", headers=auth_headers)
    first = r.json()["data"]["items"][0]
    r = client.get(f"{BASE}/calls/{first['id']}", headers=auth_headers)
    detail = r.json()["data"]
    assert "request_payload" in detail

    r = client.get(f"{BASE}/calls/nonexistent", headers=auth_headers)
    assert r.status_code == 404


def test_call_records_require_menu(client, custom_headers):
    r = client.get(f"{BASE}/calls", headers=custom_headers)
    assert r.status_code == 403


# ──────────────────────────── 调试执行收尾代码回归 ────────────────────────────


def test_debug_epilogue_handles_json_literals():
    """test_input 含 JSON 布尔/null 时，注入代码在内核中必须可执行。

    回归：曾把 JSON 文本直接拼进 Python 表达式（true/false/null 不是合法
    Python 标识符），此类测试入参在内核里必报 NameError。这里在进程内真实
    执行生成代码（不起内核），锁定拼接正确性。
    """
    import contextlib
    import io

    script = (
        "def simulate(context, actions, horizon):\n"
        "    return {\n"
        "        'flag': context.get('flag'),\n"
        "        'missing': context.get('nothing'),\n"
        "        'tags': context.get('tags'),\n"
        "        'horizon': horizon,\n"
        "    }\n"
    )
    code = service._build_debug_code(script, {
        "context": {"flag": True, "nothing": None, "tags": ["a", False]},
        "actions": [],
        "horizon": 2,
    })

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exec(compile(code, "<world-model-debug>", "exec"), {})
    stdout = buffer.getvalue()

    assert "__OB_RESULT_BEGIN__" in stdout
    from app.data_channel.pipelines.python_engine.client import extract_payload
    payload = extract_payload(stdout)
    assert payload == {
        "flag": True, "missing": None, "tags": ["a", False], "horizon": 2,
    }


# ──────────────────────────── 推演服务：发布 / 状态 / 调用 ────────────────────────────

_PUBLISH_BODY = {
    "name": "负荷推演服务",
    "description": "对外提供负荷推演",
    "applicable_ontology_id": "ontology-1",
    "applicable_object_type_ids": ["ot-line"],
    "preconditions": [{"object_type_id": "ot-line", "min_count": 1}],
}


def _save_version(client, auth_headers, project_id, monkeypatch):
    monkeypatch.setattr(service, "execute_code", _fake_execute_ok)
    r = client.post(
        f"{BASE}/projects/{project_id}/save",
        json={"script": "def simulate(context, actions, horizon):\n"
                        "    return {'trajectory': [1, 2]}",
              "test_input": {}},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["ok"] is True
    return r.json()["data"]["version_no"]


def test_publish_requires_saved_version(client, auth_headers, project):
    r = client.post(
        f"{BASE}/projects/{project['id']}/publish",
        json=_PUBLISH_BODY,
        headers=auth_headers,
    )
    assert r.status_code == 400
    assert "保存" in str(r.json()["detail"])


def test_publish_creates_online_service_and_marks_project(
    client, auth_headers, project, monkeypatch,
):
    version_no = _save_version(client, auth_headers, project["id"], monkeypatch)
    r = client.post(
        f"{BASE}/projects/{project['id']}/publish",
        json=_PUBLISH_BODY,
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    svc = r.json()["data"]
    assert svc["status"] == "online"
    assert svc["version_no"] == version_no
    assert svc["endpoint_path"].endswith(f"/services/{svc['id']}/invoke")
    assert svc["applicable_object_types"] == {
        "ontology_id": "ontology-1", "object_type_ids": ["ot-line"],
    }
    assert svc["preconditions"] == [{"object_type_id": "ot-line", "min_count": 1}]

    r = client.get(f"{BASE}/projects/{project['id']}", headers=auth_headers)
    detail = r.json()["data"]
    assert detail["status"] == "published"
    assert detail["service_status"] == "online"
    assert detail["version_count"] == 1

    # 列表接口同样携带 service_status（回归：曾因 schema 缺字段被静默丢弃，
    # 列表徽标永远显示「草稿」）
    r = client.get(f"{BASE}/projects", headers=auth_headers)
    listed = [i for i in r.json()["data"]["items"] if i["id"] == project["id"]]
    assert listed[0]["service_status"] == "online"

    r = client.get(f"{BASE}/projects/{project['id']}/service", headers=auth_headers)
    assert r.json()["data"]["id"] == svc["id"]


def test_republish_overwrites_same_service(
    client, auth_headers, project, monkeypatch,
):
    _save_version(client, auth_headers, project["id"], monkeypatch)
    first = client.post(
        f"{BASE}/projects/{project['id']}/publish",
        json=_PUBLISH_BODY, headers=auth_headers,
    ).json()["data"]
    version_no_2 = _save_version(client, auth_headers, project["id"], monkeypatch)
    r = client.post(
        f"{BASE}/projects/{project['id']}/publish",
        json={**_PUBLISH_BODY, "name": "负荷推演服务 v2"},
        headers=auth_headers,
    )
    assert r.status_code == 201
    second = r.json()["data"]
    assert second["id"] == first["id"]  # 同一项目覆盖更新，不产生第二个服务
    assert second["name"] == "负荷推演服务 v2"
    assert second["version_no"] == version_no_2


def test_project_list_includes_service_summary(
    client, auth_headers, project, monkeypatch,
):
    """列表条目携带已发布服务的名称/端点/冻结版本号（卡片服务快捷入口数据源）。"""
    version_no = _save_version(client, auth_headers, project["id"], monkeypatch)
    svc = client.post(
        f"{BASE}/projects/{project['id']}/publish",
        json=_PUBLISH_BODY, headers=auth_headers,
    ).json()["data"]

    r = client.get(f"{BASE}/projects", headers=auth_headers)
    listed = [i for i in r.json()["data"]["items"] if i["id"] == project["id"]]
    item = listed[0]
    assert item["service_status"] == "online"
    assert item["service_name"] == "负荷推演服务"
    assert item["service_version_no"] == version_no
    assert item["service_endpoint"].endswith(f"/services/{svc['id']}/invoke")


def test_delete_project_blocked_while_service_online(
    client, auth_headers, project, db, monkeypatch,
):
    """在线服务保护：在线时拒绝删除（409）；下线后可删，服务随项目清理。"""
    from app.world_model.models import WorldModelService

    _save_version(client, auth_headers, project["id"], monkeypatch)
    client.post(
        f"{BASE}/projects/{project['id']}/publish",
        json=_PUBLISH_BODY, headers=auth_headers,
    )

    r = client.delete(f"{BASE}/projects/{project['id']}", headers=auth_headers)
    assert r.status_code == 409
    assert "下线" in str(r.json()["detail"])
    assert db.query(WorldModelService).count() == 1

    r = client.post(
        f"{BASE}/projects/{project['id']}/service/status",
        json={"status": "offline"}, headers=auth_headers,
    )
    assert r.status_code == 200

    r = client.delete(f"{BASE}/projects/{project['id']}", headers=auth_headers)
    assert r.status_code == 200
    db.expire_all()
    # 服务显式随项目删除（不依赖 PG 外键级联，SQLite 行为一致）
    assert db.query(WorldModelService).count() == 0
    r = client.get(f"{BASE}/projects/{project['id']}", headers=auth_headers)
    assert r.status_code == 404


def test_delete_project_unlinks_call_records(
    client, auth_headers, project, db, monkeypatch,
):
    """删除项目后调用记录保留审计但解除项目/服务关联。"""
    _save_version(client, auth_headers, project["id"], monkeypatch)
    svc = client.post(
        f"{BASE}/projects/{project['id']}/publish",
        json=_PUBLISH_BODY, headers=auth_headers,
    ).json()["data"]
    r = client.post(
        f"{BASE}/services/{svc['id']}/invoke",
        json={"context": {}, "actions": [], "horizon": 1},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    client.post(
        f"{BASE}/projects/{project['id']}/service/status",
        json={"status": "offline"}, headers=auth_headers,
    )

    r = client.delete(f"{BASE}/projects/{project['id']}", headers=auth_headers)
    assert r.status_code == 200
    db.expire_all()
    record = db.query(WorldModelCallRecord).one()
    assert record.project_id is None
    assert record.service_id is None
    assert record.service_name == "负荷推演服务"  # 审计快照保留


def test_service_offline_blocks_invoke(
    client, auth_headers, project, monkeypatch,
):
    _save_version(client, auth_headers, project["id"], monkeypatch)
    svc = client.post(
        f"{BASE}/projects/{project['id']}/publish",
        json=_PUBLISH_BODY, headers=auth_headers,
    ).json()["data"]

    r = client.post(
        f"{BASE}/projects/{project['id']}/service/status",
        json={"status": "offline"}, headers=auth_headers,
    )
    assert r.json()["data"]["status"] == "offline"

    r = client.post(
        f"{BASE}/services/{svc['id']}/invoke",
        json={"context": {}, "actions": [], "horizon": 1},
        headers=auth_headers,
    )
    assert r.status_code == 409


def test_invoke_writes_call_record(
    client, auth_headers, project, monkeypatch,
):
    _save_version(client, auth_headers, project["id"], monkeypatch)
    svc = client.post(
        f"{BASE}/projects/{project['id']}/publish",
        json=_PUBLISH_BODY, headers=auth_headers,
    ).json()["data"]

    r = client.post(
        f"{BASE}/services/{svc['id']}/invoke",
        json={"context": {"current_value": 7}, "actions": [], "horizon": 2},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["ok"] is True
    assert data["payload"] == {"trajectory": [1, 2]}
    assert data["call_id"]

    r = client.get(f"{BASE}/calls", headers=auth_headers)
    items = r.json()["data"]["items"]
    assert len(items) == 1
    assert items[0]["service_name"] == "负荷推演服务"
    assert items[0]["caller"] == "wm_admin" or items[0]["caller"]

    r = client.get(f"{BASE}/calls/{data['call_id']}", headers=auth_headers)
    detail = r.json()["data"]
    assert detail["request_payload"]["context"] == {"current_value": 7}
    assert detail["response_payload"] == {"result": {"trajectory": [1, 2]}}


def test_invoke_records_script_failure(
    client, auth_headers, project, monkeypatch,
):
    _save_version(client, auth_headers, project["id"], monkeypatch)
    svc = client.post(
        f"{BASE}/projects/{project['id']}/publish",
        json=_PUBLISH_BODY, headers=auth_headers,
    ).json()["data"]

    monkeypatch.setattr(service, "execute_code", _fake_execute_fail)
    r = client.post(
        f"{BASE}/services/{svc['id']}/invoke",
        json={"context": {}, "actions": [], "horizon": 1},
        headers=auth_headers,
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["ok"] is False
    assert "ValueError" in data["error"]

    r = client.get(f"{BASE}/calls/overview", headers=auth_headers)
    assert r.json()["data"]["failed"] == 1


def test_invoke_gateway_down_returns_502_and_records(
    client, auth_headers, project, monkeypatch,
):
    from app.data_channel.pipelines.python_engine.client import (
        PythonEngineError,
    )

    _save_version(client, auth_headers, project["id"], monkeypatch)
    svc = client.post(
        f"{BASE}/projects/{project['id']}/publish",
        json=_PUBLISH_BODY, headers=auth_headers,
    ).json()["data"]

    def _boom(code, **kwargs):
        raise PythonEngineError("Python 执行网关未配置")

    monkeypatch.setattr(service, "execute_code", _boom)
    r = client.post(
        f"{BASE}/services/{svc['id']}/invoke",
        json={"context": {}, "actions": [], "horizon": 1},
        headers=auth_headers,
    )
    assert r.status_code == 502
    r = client.get(f"{BASE}/calls/overview", headers=auth_headers)
    assert r.json()["data"] == {"total": 1, "failed": 1, "avg_duration_ms": 0}


def test_publish_and_invoke_require_menu(client, custom_headers, project):
    r = client.post(
        f"{BASE}/projects/{project['id']}/publish",
        json=_PUBLISH_BODY, headers=custom_headers,
    )
    assert r.status_code == 403
    r = client.post(
        f"{BASE}/services/any/invoke",
        json={"context": {}}, headers=custom_headers,
    )
    assert r.status_code == 403


# ──────────────────────────── 推演服务注册表（跨项目） ────────────────────────────


def test_service_registry_lists_published_service_with_stats(
    client, auth_headers, project, monkeypatch,
):
    version_no = _save_version(client, auth_headers, project["id"], monkeypatch)
    svc = client.post(
        f"{BASE}/projects/{project['id']}/publish",
        json=_PUBLISH_BODY, headers=auth_headers,
    ).json()["data"]
    assert svc["status"] == "online"

    r = client.get(f"{BASE}/services", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["total"] == 1
    item = data["items"][0]
    assert item["id"] == svc["id"]
    assert item["project_name"] == "负荷推演"
    assert item["version_no"] == version_no
    assert item["name"] == "负荷推演服务"
    assert item["endpoint_path"].endswith(f"/services/{svc['id']}/invoke")
    assert item["applicable_object_types"]["ontology_id"] == "ontology-1"
    assert item["call_count"] == 0
    assert item["failed_count"] == 0

    # 调用一次后统计随之更新
    client.post(
        f"{BASE}/services/{svc['id']}/invoke",
        json={"context": {}, "actions": [], "horizon": 1},
        headers=auth_headers,
    )
    r = client.get(f"{BASE}/services", headers=auth_headers)
    item = r.json()["data"]["items"][0]
    assert item["call_count"] == 1
    assert item["failed_count"] == 0


def test_service_registry_filters_and_pagination(
    client, auth_headers, project, monkeypatch,
):
    _save_version(client, auth_headers, project["id"], monkeypatch)
    client.post(
        f"{BASE}/projects/{project['id']}/publish",
        json=_PUBLISH_BODY, headers=auth_headers,
    )

    r = client.get(
        f"{BASE}/services", params={"keyword": "不存在"}, headers=auth_headers)
    assert r.json()["data"]["total"] == 0
    r = client.get(
        f"{BASE}/services", params={"status": "offline"}, headers=auth_headers)
    assert r.json()["data"]["total"] == 0
    r = client.get(
        f"{BASE}/services", params={"status": "online"}, headers=auth_headers)
    assert r.json()["data"]["total"] == 1
    r = client.get(
        f"{BASE}/services", params={"status": "bogus"}, headers=auth_headers)
    assert r.status_code == 400
    r = client.get(
        f"{BASE}/services", params={"page": 2, "size": 1}, headers=auth_headers)
    assert r.json()["data"]["total"] == 1
    assert r.json()["data"]["items"] == []


def test_service_registry_detail_and_status_by_id(
    client, auth_headers, project, monkeypatch,
):
    _save_version(client, auth_headers, project["id"], monkeypatch)
    svc = client.post(
        f"{BASE}/projects/{project['id']}/publish",
        json=_PUBLISH_BODY, headers=auth_headers,
    ).json()["data"]

    r = client.get(f"{BASE}/services/{svc['id']}", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["data"]["id"] == svc["id"]
    assert r.json()["data"]["preconditions"] == [
        {"object_type_id": "ot-line", "min_count": 1}]

    r = client.get(f"{BASE}/services/nonexistent", headers=auth_headers)
    assert r.status_code == 404

    r = client.post(
        f"{BASE}/services/{svc['id']}/status",
        json={"status": "offline"}, headers=auth_headers,
    )
    assert r.status_code == 200
    assert r.json()["data"]["status"] == "offline"

    # 下线后调用被拒绝（服务侧状态入口与项目侧入口语义一致）
    r = client.post(
        f"{BASE}/services/{svc['id']}/invoke",
        json={"context": {}, "actions": [], "horizon": 1},
        headers=auth_headers,
    )
    assert r.status_code == 409

    r = client.post(
        f"{BASE}/services/{svc['id']}/status",
        json={"status": "online"}, headers=auth_headers,
    )
    assert r.json()["data"]["status"] == "online"


def test_calls_list_filters_by_service_id(
    client, auth_headers, project, monkeypatch,
):
    _save_version(client, auth_headers, project["id"], monkeypatch)
    svc = client.post(
        f"{BASE}/projects/{project['id']}/publish",
        json=_PUBLISH_BODY, headers=auth_headers,
    ).json()["data"]
    client.post(
        f"{BASE}/services/{svc['id']}/invoke",
        json={"context": {}, "actions": [], "horizon": 1},
        headers=auth_headers,
    )

    r = client.get(f"{BASE}/calls", headers=auth_headers)
    assert r.json()["data"]["total"] == 1
    r = client.get(
        f"{BASE}/calls", params={"service_id": svc["id"]}, headers=auth_headers)
    assert r.json()["data"]["total"] == 1
    r = client.get(
        f"{BASE}/calls", params={"service_id": "other"}, headers=auth_headers)
    assert r.json()["data"]["total"] == 0


def test_service_registry_requires_menu(client, custom_headers):
    assert client.get(f"{BASE}/services", headers=custom_headers).status_code == 403
    assert client.get(
        f"{BASE}/services/any", headers=custom_headers).status_code == 403
    r = client.post(
        f"{BASE}/services/any/status",
        json={"status": "online"}, headers=custom_headers,
    )
    assert r.status_code == 403


# ──────────────────────────── 概览统计与按日分桶（页面统计条/趋势图） ────────────────────────────


def test_services_overview_aggregates_status_and_calls(
    client, auth_headers, project, monkeypatch,
):
    _save_version(client, auth_headers, project["id"], monkeypatch)
    svc = client.post(
        f"{BASE}/projects/{project['id']}/publish",
        json=_PUBLISH_BODY, headers=auth_headers,
    ).json()["data"]
    client.post(
        f"{BASE}/services/{svc['id']}/invoke",
        json={"context": {}, "actions": [], "horizon": 1},
        headers=auth_headers,
    )

    r = client.get(f"{BASE}/services/overview", headers=auth_headers)
    assert r.status_code == 200
    overview = r.json()["data"]
    assert overview["total"] == 1
    assert overview["online"] == 1
    assert overview["offline"] == 0
    assert overview["call_total"] == 1
    assert overview["call_failed"] == 0
    assert overview["avg_duration_ms"] >= 0

    # 下线后状态计数翻转；调用统计不受状态切换影响
    client.post(
        f"{BASE}/services/{svc['id']}/status",
        json={"status": "offline"}, headers=auth_headers,
    )
    overview = client.get(
        f"{BASE}/services/overview", headers=auth_headers).json()["data"]
    assert overview["online"] == 0
    assert overview["offline"] == 1
    assert overview["call_total"] == 1


def test_call_records_daily_fills_missing_days(client, auth_headers, db):
    from datetime import datetime, time, timedelta, timezone

    today = datetime.now(timezone.utc).date()
    day_before = today - timedelta(days=2)

    def at(day, **kwargs):
        return WorldModelCallRecord(
            service_name="负荷推演", caller="agent",
            created_at=datetime.combine(day, time(hour=8), tzinfo=timezone.utc),
            **kwargs,
        )

    db.add_all([
        at(day_before, ok=True, duration_ms=200),
        at(today, ok=True, duration_ms=100),
        at(today, ok=False, duration_ms=300, error="超时"),
    ])
    db.commit()

    r = client.get(
        f"{BASE}/calls/daily", params={"days": 3}, headers=auth_headers)
    assert r.status_code == 200
    buckets = r.json()["data"]
    assert [bucket["date"] for bucket in buckets] == [
        (today - timedelta(days=2)).isoformat(),
        (today - timedelta(days=1)).isoformat(),
        today.isoformat(),
    ]
    assert buckets[0] == {
        "date": day_before.isoformat(),
        "total": 1, "failed": 0, "avg_duration_ms": 200,
    }
    # 无调用的日期补零
    assert buckets[1] == {
        "date": (today - timedelta(days=1)).isoformat(),
        "total": 0, "failed": 0, "avg_duration_ms": 0,
    }
    assert buckets[2] == {
        "date": today.isoformat(),
        "total": 2, "failed": 1, "avg_duration_ms": 200,
    }

    # 窗口外（更早）的记录不进入分桶
    r = client.get(f"{BASE}/calls/daily", params={"days": 1}, headers=auth_headers)
    assert r.json()["data"] == [buckets[2]]


def test_overview_endpoints_require_menu(client, custom_headers):
    assert client.get(
        f"{BASE}/services/overview", headers=custom_headers).status_code == 403
    assert client.get(
        f"{BASE}/calls/daily", headers=custom_headers).status_code == 403


# ──────────────────── horizon=0 透传（回归：曾被 or 1 静默改写成 1） ────────────────────

_ECHO_HORIZON_SCRIPT = (
    "def simulate(context, actions, horizon):\n"
    "    return {'trajectory': [], 'horizon': horizon}\n"
)


def _fake_execute_inprocess(code, **kwargs):
    """execute_code 替身：进程内真实执行注入代码并回传 stdout。

    _fake_execute_ok 返回固定 payload，验证不了入参是否真的流进 simulate；
    horizon 这类「入参透传」回归必须跑在真实执行路径上。
    """
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exec(compile(code, "<world-model-debug>", "exec"), {})
    return _FakeExecution(stdout=buffer.getvalue())


def test_debug_epilogue_preserves_horizon_zero():
    """入参归一与调试收尾模板都不得把 horizon=0 收成 1（0 步快照合法）。"""
    import contextlib
    import io

    from app.data_channel.pipelines.python_engine.client import extract_payload

    code = service._build_debug_code(_ECHO_HORIZON_SCRIPT, {
        "context": {}, "actions": [], "horizon": 0,
    })
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exec(compile(code, "<world-model-debug>", "exec"), {})
    assert extract_payload(buffer.getvalue()) == {"trajectory": [], "horizon": 0}

    # 缺省语义不变：不传 horizon 仍补 1
    code = service._build_debug_code(_ECHO_HORIZON_SCRIPT, {})
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exec(compile(code, "<world-model-debug>", "exec"), {})
    assert extract_payload(buffer.getvalue()) == {"trajectory": [], "horizon": 1}


def test_normalize_test_input_horizon_type_gate():
    """horizon 契约对齐（int, ge=0）：0 透传，垃圾值回退 1，负数钳 0。

    回归：is not None 判断曾把 ""/false 等 falsy 垃圾值原样放进内核，
    用户脚本 range(horizon) 直接 TypeError；负数/小数也曾与 invoke 入口
    （int, ge=0 → 422）口径不一。
    """
    normalize = service._normalize_test_input
    assert normalize({"horizon": 0})["horizon"] == 0
    assert normalize({"horizon": 3})["horizon"] == 3
    assert normalize({})["horizon"] == 1
    # 垃圾值回退 1（不透传进内核）
    for junk in ("", "3", None, False, True, [], {}, [1], 2.5, float("nan")):
        assert normalize({"horizon": junk})["horizon"] == 1, junk
    # 数值收整与钳制：与 invoke 契约（int, ge=0）同口径
    assert normalize({"horizon": 2.0})["horizon"] == 2
    assert normalize({"horizon": -3})["horizon"] == 0


def test_horizon_zero_flows_through_execute_save_and_invoke(
    client, auth_headers, project, monkeypatch,
):
    """execute、save 冻结的 test_input、invoke 三条路径都透传 horizon=0。"""
    monkeypatch.setattr(service, "execute_code", _fake_execute_inprocess)
    body = {"script": _ECHO_HORIZON_SCRIPT,
            "test_input": {"context": {}, "actions": [], "horizon": 0}}

    r = client.post(
        f"{BASE}/projects/{project['id']}/execute", json=body,
        headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["payload"]["horizon"] == 0

    r = client.post(
        f"{BASE}/projects/{project['id']}/save", json=body,
        headers=auth_headers)
    assert r.status_code == 200 and r.json()["data"]["ok"] is True
    r = client.get(
        f"{BASE}/projects/{project['id']}/versions", headers=auth_headers)
    assert r.json()["data"][0]["test_input"]["horizon"] == 0

    r = client.post(
        f"{BASE}/projects/{project['id']}/publish",
        json=_PUBLISH_BODY, headers=auth_headers)
    assert r.status_code == 201, r.text
    svc = r.json()["data"]

    r = client.post(
        f"{BASE}/services/{svc['id']}/invoke",
        json={"context": {}, "actions": [], "horizon": 0},
        headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["payload"]["horizon"] == 0


# ──────────────────── 版本修剪：不拆在线服务引用的脚本 ────────────────────


def test_prune_keeps_version_bound_to_online_service(
    client, auth_headers, project, monkeypatch,
):
    """超过保留窗口后，仍被推演服务引用的版本不被修剪（否则调用 409）。"""
    from app.world_model.models import SCRIPT_VERSION_KEEP

    monkeypatch.setattr(service, "execute_code", _fake_execute_ok)
    pid = project["id"]

    # v1：发布并绑定给在线服务，脚本带标记以证明调用仍打到这一版
    marker_script = (
        "def simulate(context, actions, horizon):\n"
        "    return {'trajectory': ['v1']}\n"
    )
    r = client.post(
        f"{BASE}/projects/{pid}/save",
        json={"script": marker_script, "test_input": {}},
        headers=auth_headers)
    assert r.json()["data"]["ok"] is True
    r = client.get(f"{BASE}/projects/{pid}/versions", headers=auth_headers)
    v1 = r.json()["data"][0]

    r = client.post(
        f"{BASE}/projects/{pid}/publish",
        json={**_PUBLISH_BODY, "version_id": v1["id"]},
        headers=auth_headers)
    assert r.status_code == 201, r.text
    svc = r.json()["data"]

    # 连续保存超出保留窗口，触发修剪分支（此前从未被测试执行过）
    for _ in range(SCRIPT_VERSION_KEEP + 5):
        assert _save_version(client, auth_headers, pid, monkeypatch)

    r = client.get(f"{BASE}/projects/{pid}/versions", headers=auth_headers)
    versions = r.json()["data"]
    nos = [v["version_no"] for v in versions]
    # 最近 KEEP 版 + 被引用的 v1 保留；v2 起未引用的旧版本被修剪
    assert len(versions) == SCRIPT_VERSION_KEEP + 1
    assert min(nos) == 1 and max(nos) == SCRIPT_VERSION_KEEP + 6

    # 在线调用仍执行被保护版本的脚本（而非 409 或新版本脚本）
    monkeypatch.setattr(service, "execute_code", _fake_execute_inprocess)
    r = client.post(
        f"{BASE}/services/{svc['id']}/invoke",
        json={"context": {}, "actions": [], "horizon": 1},
        headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["payload"] == {"trajectory": ["v1"]}


# ──────────────────── 关键路径日志（原模块日志为零） ────────────────────


def test_publish_and_invoke_emit_audit_logs(
    client, auth_headers, project, monkeypatch, caplog,
):
    """发布新建/覆盖、调用成功/脚本报错/基础设施 502 均有日志可查。"""
    import logging as std_logging

    from app.data_channel.pipelines.python_engine.client import PythonEngineError

    monkeypatch.setattr(service, "execute_code", _fake_execute_ok)
    _save_version(client, auth_headers, project["id"], monkeypatch)

    with caplog.at_level(std_logging.INFO, logger="app.world_model.service"):
        r = client.post(
            f"{BASE}/projects/{project['id']}/publish",
            json=_PUBLISH_BODY, headers=auth_headers)
        assert r.status_code == 201, r.text
        assert "新建上线" in caplog.text

        caplog.clear()
        r = client.post(
            f"{BASE}/projects/{project['id']}/publish",
            json=_PUBLISH_BODY, headers=auth_headers)
        assert r.status_code == 201, r.text
        assert "覆盖更新" in caplog.text

        svc = r.json()["data"]
        invoke_url = f"{BASE}/services/{svc['id']}/invoke"

        caplog.clear()
        r = client.post(invoke_url, json={"horizon": 1}, headers=auth_headers)
        assert r.status_code == 200
        assert "推演服务调用完成" in caplog.text

        caplog.clear()
        monkeypatch.setattr(service, "execute_code", _fake_execute_fail)
        r = client.post(invoke_url, json={"horizon": 1}, headers=auth_headers)
        assert r.status_code == 200 and r.json()["data"]["ok"] is False
        assert "推演服务调用失败" in caplog.text

        def _engine_down(code, **kwargs):
            raise PythonEngineError("模拟网关不可达")

        caplog.clear()
        monkeypatch.setattr(service, "execute_code", _engine_down)
        r = client.post(invoke_url, json={"horizon": 1}, headers=auth_headers)
        assert r.status_code == 502
        assert "推演服务调用中断" in caplog.text
        assert "内核执行失败" in caplog.text

        # 客户端 4xx（入参超大）不得误报为基础设施 ERROR，降为 WARNING 且留审计
        caplog.clear()
        monkeypatch.setattr(service, "execute_code", _fake_execute_ok)
        r = client.post(
            invoke_url,
            json={"context": {"blob": "x" * 150_000}, "horizon": 1},
            headers=auth_headers)
        assert r.status_code == 400
        assert "推演服务调用被拒绝" in caplog.text
        assert "基础设施" not in caplog.text
        r = client.get(
            f"{BASE}/calls", params={"result": "failed"}, headers=auth_headers)
        assert r.json()["data"]["total"] >= 1
