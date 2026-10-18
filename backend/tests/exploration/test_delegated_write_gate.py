"""YIYA-36「用不改」：超级助手委派探索拒绝本体定义写，用户直连不受影响。"""
from app.exploration.toolkit import ExplorationToolRunner


class _SessionStub:
    canvas_version = 0


def test_delegated_exploration_rejects_apply_draft(db):
    runner = ExplorationToolRunner(db, _SessionStub(),
                                   origin="super_assistant")
    result = runner.run("apply_draft", {})
    assert result["error"] == "assistant_definition_write_forbidden"


def test_user_direct_exploration_keeps_apply_draft(db):
    runner = ExplorationToolRunner(db, _SessionStub(),
                                   origin="user_direct")
    result = runner.run("apply_draft", {})
    # 用户直连不做来源拦截（后续业务校验照常进行）
    assert result.get("error") != "assistant_definition_write_forbidden"
