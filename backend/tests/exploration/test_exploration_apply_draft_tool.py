"""apply_draft 对话式沉淀工具（toolkit.apply_draft）：

  1. 授权守卫：当前用户消息含未被否定修饰的肯定子句才放行；
     否定修饰（先别沉淀/不要应用/不可以/暂不同意）、无关消息、
     「肯定+否定沉淀动作」混排一律拒绝并返回 confirmationRequired；
  2. 校验链：草稿不存在/跨会话/非 draft 状态/会话未绑定各有明确错误；
  3. 成功路径：与 HTTP 按钮同一 application_service 落地（版本正门合并），
     created/skipped 计数与 warnings 透出，草稿/会话锚点回填、画布不动；
  4. 服务层 HTTPException 转工具错误内容（不抛异常），Agent 可向用户解释。
"""
from __future__ import annotations

import uuid

import pytest

from app.exploration.models import ExplorationDraft, ExplorationSession
from app.exploration.toolkit import ExplorationToolRunner, _apply_draft_authorized

from tests.exploration.test_exploration import _pipeline_ready_canvas, _tool_session


# ---------------------------------------------------------------- 工具挂载

def test_apply_draft_tool_mounted_only_for_bound_sessions(db, monkeypatch):
    """apply_draft 只对已绑定本体版本的会话挂载：未绑定会话调用必然被校验链拒绝，
    常驻挂载只会白占小窗口模型的工具协议预算。"""
    from app.exploration import canvas as C
    from app.exploration import orchestrator as OR

    monkeypatch.setattr(OR, "select_llm_model_config",
                        lambda db, model_id=None: object())
    monkeypatch.setattr(OR, "llm_call_kwargs", lambda cfg: {"model": "fake"})
    seen: list[list[str]] = []

    def fake_chat(call_kwargs, messages, tools):
        seen.append([tool["name"] for tool in tools])
        return {"content": "好", "tool_calls": [], "usage": None}

    monkeypatch.setattr(OR.llm_bridge, "chat", fake_chat)

    unbound = _tool_session(db, canvas=C.empty_canvas())
    list(OR.run_exploration_turn(db, unbound.id, user=object(), message="你好"))
    assert "apply_draft" not in seen[-1]

    bound = _tool_session(db, ontology_id=str(uuid.uuid4()), canvas=C.empty_canvas())
    list(OR.run_exploration_turn(db, bound.id, user=object(), message="你好"))
    assert "apply_draft" in seen[-1]


# ---------------------------------------------------------------- 授权守卫（纯函数矩阵）


@pytest.mark.parametrize("message", [
    "可以，就按这个沉淀",
    "确认沉淀",
    "同意，应用吧",
    "好的，确认沉淀吧",
    "可以合并草稿",
    "没问题，落地吧",
    "确认, apply the draft",
    "别动那个文件，可以沉淀了",      # 否定子句未点名沉淀动作，不否决
])
def test_apply_authorization_accepts_affirmative_clause(message):
    assert _apply_draft_authorized(message) is True


@pytest.mark.parametrize("message", [
    "",
    "好的",                          # 目标绑定：与沉淀无关的肯定回答不放行
    "可以",
    "OK 就这么办",                   # 未点名沉淀动作
    "yes, go ahead",
    "帮我确认一下这个枚举值",         # 肯定词命中但宾语是枚举值，不是沉淀
    "给我讲讲这个草稿",              # 无关消息：无肯定子句
    "先别沉淀",
    "不要应用这个草稿",
    "不可以",
    "不太好吧",
    "暂不同意",
    "未确认",
    "好的，但先别沉淀",              # 肯定子句 + 点名沉淀动作的否定子句 → 否决
    "好，但先别沉淀",
    "never apply it",
])
def test_apply_authorization_rejects_negated_or_unrelated(message):
    assert _apply_draft_authorized(message) is False


# ---------------------------------------------------------------- 工具链路

def _ready_bound_runner(db, admin_user, ontology_id, message="可以，沉淀吧"):
    """绑定本体 + 十门全过画布，经工具生成文档与草稿，返回 (session, runner, draft_id)。"""
    row = _tool_session(db, ontology_id=ontology_id, canvas=_pipeline_ready_canvas())
    runner = ExplorationToolRunner(db, row, user=admin_user, user_message=message)
    assert runner.run("generate_document", {}).get("documentId")
    generated = runner.run("generate_draft", {})
    assert generated.get("draftId"), generated
    return row, runner, generated["draftId"]


def test_apply_draft_tool_authorization_matrix(db, admin_user, ontology):
    row, _, draft_id = _ready_bound_runner(db, admin_user, ontology["id"])

    for message in ("先别沉淀", "不要应用", "给我讲讲影响", ""):
        runner = ExplorationToolRunner(db, row, user=admin_user, user_message=message)
        result = runner.run("apply_draft", {"draft_id": draft_id})
        assert result.get("confirmationRequired") is True, message
        assert "授权" in result["error"] and "影响" in result["error"]
        assert result["draftId"] == draft_id
        # 守卫拒绝不产生任何落地副作用
        assert db.query(ExplorationDraft).filter_by(id=draft_id).one().status == "draft"

    # 无待应用草稿：draft_id 不存在
    runner = ExplorationToolRunner(db, row, user=admin_user, user_message="可以")
    missing = runner.run("apply_draft", {"draft_id": "draft-not-exists"})
    assert "不存在" in missing["error"]
    assert runner.run("apply_draft", {}).get("error", "").find("draft_id") >= 0


def test_apply_draft_tool_success_merges_into_bound_version(db, admin_user, ontology):
    from app.models.ontology_version import OntologyVersion

    row, runner, draft_id = _ready_bound_runner(db, admin_user, ontology["id"])
    canvas_before = row.canvas
    canvas_version_before = row.canvas_version

    result = runner.run("apply_draft", {"draft_id": draft_id})
    assert result["applied"] is True, result
    assert result["ontologyId"] == ontology["id"]
    assert result["versionId"] and result["versionNumber"]
    assert result["createdTotal"] == sum(result["created"].values())
    assert result["created"]["objectTypes"] >= 1      # Order 等对象落进版本快照
    assert result["skippedCount"] == 0                # 全新合并，无同名跳过
    assert isinstance(result["warnings"], list)       # 校验分层 warnings 透出（含休眠动作）

    # 落地走版本正门：写进草稿版本快照，不触碰 live 表/发布基线
    version = db.query(OntologyVersion).filter_by(id=result["versionId"]).one()
    assert version.node_kind == "draft" and version.lifecycle_status == "editing"

    # 草稿/会话锚点回填；画布与画布版本不受沉淀影响
    stored_draft = db.query(ExplorationDraft).filter_by(id=draft_id).one()
    assert stored_draft.status == "applied"
    assert stored_draft.applied_ontology_id == ontology["id"]
    assert stored_draft.applied_version_id == version.id
    stored_session = db.query(ExplorationSession).filter_by(id=row.id).one()
    assert stored_session.ontology_version_id == version.id
    assert stored_session.canvas == canvas_before
    assert stored_session.canvas_version == canvas_version_before

    # 重复调用：已应用草稿不再经工具落地
    runner2 = ExplorationToolRunner(db, row, user=admin_user, user_message="可以")
    again = runner2.run("apply_draft", {"draft_id": draft_id})
    assert "已应用" in again["error"]


def test_apply_draft_tool_translates_http_exception(db, admin_user, ontology):
    row, runner, draft_id = _ready_bound_runner(db, admin_user, ontology["id"])
    # selected_keys=[] → 服务层 422，转成工具错误内容而不是抛异常
    result = runner.run("apply_draft", {"draft_id": draft_id, "selected_keys": []})
    assert "应用被拒" in result["error"] and "未勾选任何草稿元素" in result["error"]
    assert result["code"] == "422"
    assert db.query(ExplorationDraft).filter_by(id=draft_id).one().status == "draft"

    # 非数组 selected_keys 在工具边界即被拒
    bad = runner.run("apply_draft", {"draft_id": draft_id, "selected_keys": "obj:order"})
    assert "字符串数组" in bad["error"]


def test_apply_draft_tool_validation_chain(db, admin_user, ontology):
    from app.exploration import canvas as C

    # 未绑定会话：先拦绑定，再谈授权
    unbound = _tool_session(db, canvas=C.empty_canvas())
    draft = ExplorationDraft(session_id=unbound.id, document_id="doc-x",
                             target_ontology_id=None, draft={}, report={})
    db.add(draft)
    db.commit()
    runner = ExplorationToolRunner(db, unbound, user=admin_user, user_message="可以")
    result = runner.run("apply_draft", {"draft_id": draft.id})
    assert result.get("bindingRequired") is True
    assert "绑定" in result["error"]

    # 跨会话草稿：即使已绑定 + 已授权也不落地
    row, _, draft_id = _ready_bound_runner(db, admin_user, ontology["id"])
    other = _tool_session(db, ontology_id=ontology["id"])
    runner = ExplorationToolRunner(db, other, user=admin_user, user_message="可以")
    cross = runner.run("apply_draft", {"draft_id": draft_id})
    assert "不属于当前会话" in cross["error"]

    # 废弃草稿不可应用
    runner = ExplorationToolRunner(db, row, user=admin_user, user_message="可以")
    stored = db.query(ExplorationDraft).filter_by(id=draft_id).one()
    stored.status = "discarded"
    db.commit()
    discarded = runner.run("apply_draft", {"draft_id": draft_id})
    assert "已废弃" in discarded["error"]
