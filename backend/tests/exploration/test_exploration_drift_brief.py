"""绑定本体版本漂移简报注入：探索回合系统提示感知本体视图的人工编辑。

  1. 绑定会话 + 漂移版本 → 系统提示含一致性小节、版本锚点与按 code 统计
  2. 未绑定会话 → 不注入该小节，系统提示与之前完全一致
  3. 零差异绑定版本 → 注入「业务语义与本体结构当前一致」
  4. 简报计算抛错 → 回合照常完成且不注入小节（增强信号绝不摧毁回合）
  5. build_bound_version_brief 单测：未绑定/版本缺失 → None
  6. 近期人工保存审计（名称级 diff）进入简报
"""
from __future__ import annotations

import pytest

from app.exploration import canvas as C
from app.exploration import orchestrator
from app.exploration.drift_brief import build_bound_version_brief
from app.exploration.models import ExplorationSession
from app.ontologies.agent_runtime import llm_bridge
from app.ontologies.inference.models import AuditLog

from tests.exploration.test_exploration import _fake_model_config
from tests.exploration.test_exploration_version_semantic import (
    _order_snapshot,
    _write_release_snapshot,
)

BASE = "/api/v2/exploration"
SECTION = "# 绑定本体版本一致性（人工编辑感知）"


@pytest.fixture
def session(client, auth_headers):
    r = client.post(f"{BASE}/sessions", headers=auth_headers, json={})
    assert r.status_code == 201, r.text
    return r.json()["data"]


def _customer_canvas() -> dict:
    cv = C.empty_canvas()
    cv, _, errs = C.upsert_elements(cv, "object", [{
        "name": "Customer", "displayName": "客户", "keyAttribute": "customer_id",
        "attributes": [{"name": "customer_id", "displayName": "客户编号",
                        "typeHint": "文本", "required": True}],
    }])
    assert not errs
    return cv


def _drifted_release(db, ontology_id: str):
    """结构快照有 Order，语义层画布只有 Customer → 双向缺失 + 缺需求文档。"""
    return _write_release_snapshot(
        db, ontology_id, _order_snapshot(),
        semantic={"canvas": _customer_canvas(), "semanticRevision": 1},
    )


def _bound_session(client, auth_headers, version_id: str) -> dict:
    r = client.post(f"{BASE}/sessions", headers=auth_headers,
                    json={"ontologyVersionId": version_id})
    assert r.status_code == 201, r.text
    return r.json()["data"]


def _chat_once(client, auth_headers, session_id: str, monkeypatch) -> str:
    """跑一个无工具的聊天回合，返回 LLM 实际收到的系统提示。"""
    captured: dict[str, str] = {}

    def fake_chat(_call_kwargs, messages, _tools):
        captured["system"] = messages[0]["content"]
        return {"content": "收到，继续澄清。", "tool_calls": [], "usage": None}

    monkeypatch.setattr(llm_bridge, "chat", fake_chat)
    r = client.post(f"{BASE}/sessions/{session_id}/chat", headers=auth_headers,
                    json={"message": "继续澄清口径", "stream": False})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["error"] is None
    return captured["system"]


def test_bound_session_injects_drift_brief(client, auth_headers, db, ontology,
                                           admin_user, monkeypatch):
    _fake_model_config(db, admin_user)
    release = _drifted_release(db, ontology["id"])
    bound = _bound_session(client, auth_headers, release.id)

    system = _chat_once(client, auth_headers, bound["id"], monkeypatch)

    assert SECTION in system
    assert f"绑定版本 {release.version_number}" in system
    assert f"revision={release.revision or 0}" in system
    # 结构有 Order、画布有 Customer、语义层缺需求文档 → 共 3 项
    assert "差异共 3 项" in system
    assert "结构有、业务画布中无对应 1 项" in system
    assert "画布有、结构中缺少 1 项" in system
    assert "语义层缺少需求文档 1 项" in system
    assert "订单" in system                       # 代表差异消息
    # 引导句：回译到画布，而不是声称改了本体结构
    assert "upsert_elements/remove_elements 把对应改动回译到业务场景画布" in system
    assert "不要直接声称已修改本体结构" in system


def test_unbound_session_prompt_unchanged(client, auth_headers, session, db,
                                          admin_user, monkeypatch):
    _fake_model_config(db, admin_user)

    system = _chat_once(client, auth_headers, session["id"], monkeypatch)

    assert SECTION not in system
    assert "绑定本体版本" not in system
    # 与不带漂移参数直接渲染的系统提示逐字一致（默认 64K 窗口走首个 profile）
    from app.exploration.context_builder import _load_skills, _system_prompt
    row = db.query(ExplorationSession).filter_by(id=session["id"]).one()
    assert system == _system_prompt(row, _load_skills(), False)


def test_zero_drift_bound_version_reports_consistent(client, auth_headers, db,
                                                     ontology, admin_user,
                                                     monkeypatch):
    _fake_model_config(db, admin_user)
    release = _write_release_snapshot(
        db, ontology["id"], {},
        semantic={"canvas": C.empty_canvas(), "semanticRevision": 1},
    )
    bound = _bound_session(client, auth_headers, release.id)

    system = _chat_once(client, auth_headers, bound["id"], monkeypatch)

    assert SECTION in system
    assert "业务语义与本体结构当前一致" in system
    assert "差异共" not in system


def test_brief_failure_never_breaks_turn(client, auth_headers, db, ontology,
                                         admin_user, monkeypatch):
    _fake_model_config(db, admin_user)
    release = _drifted_release(db, ontology["id"])
    bound = _bound_session(client, auth_headers, release.id)

    def boom(_db, _session):
        raise RuntimeError("漂移计算故障")

    monkeypatch.setattr(orchestrator, "build_bound_version_brief", boom)

    system = _chat_once(client, auth_headers, bound["id"], monkeypatch)
    assert SECTION not in system


def test_brief_none_when_unbound_or_version_missing(db, session):
    row = db.query(ExplorationSession).filter_by(id=session["id"]).one()
    assert build_bound_version_brief(db, row) is None

    row.ontology_version_id = "missing-version"
    db.commit()
    assert build_bound_version_brief(db, row) is None


def test_brief_block_stays_compact(db, client, auth_headers, ontology):
    release = _drifted_release(db, ontology["id"])
    bound = _bound_session(client, auth_headers, release.id)
    row = db.query(ExplorationSession).filter_by(id=bound["id"]).one()

    brief = build_bound_version_brief(db, row)
    assert brief is not None
    assert len(brief.splitlines()) <= 15


def test_brief_includes_recent_human_edit_audit(db, client, auth_headers,
                                                ontology, admin_user):
    """近期人工保存的审计（含名称级 diff）进入简报，供引导师定位人工改动。"""
    release = _drifted_release(db, ontology["id"])
    db.add(AuditLog(
        ontology_id=ontology["id"], event_type="edit",
        event_subtype="workspace_saved", user_id=admin_user.id,
        user_name=admin_user.username,
        description="保存草稿结构工作区", object_type="ontology_version",
        object_id=release.id,
        meta={"diff": {"objectTypes": {
            "added": 1, "modified": 0, "deleted": 0,
            "addedNames": ["验证对象Tmp"], "modifiedNames": [], "deletedNames": [],
        }}},
    ))
    db.commit()
    bound = _bound_session(client, auth_headers, release.id)
    row = db.query(ExplorationSession).filter_by(id=bound["id"]).one()

    brief = build_bound_version_brief(db, row)

    assert "近期人工编辑" in brief
    assert "保存结构工作区" in brief
    assert "验证对象Tmp" in brief
