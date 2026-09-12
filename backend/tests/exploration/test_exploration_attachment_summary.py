"""附件索引卡：懒生成、落库缓存、失败负缓存、并发写回、来源隔离与注入形态。

fake LLM 风格与存量测试一致：monkeypatch llm_bridge.chat（其模块对象即
app.model_configs.llm_gateway，与 context_builder 的调用点相同）。
"""
import pytest

from app.exploration.canvas import empty_canvas
from app.exploration.context_builder import _attachments_block
from app.exploration.models import ExplorationAttachment, ExplorationSession

_CALL_KWARGS = {"model": "fake-card", "max_output_tokens": 4_096}


@pytest.fixture(autouse=True)
def _clear_card_failure_cache():
    from app.exploration import context_builder as CB
    CB._card_failure_cache.clear()
    yield
    CB._card_failure_cache.clear()


def _make_session(db, admin_user) -> ExplorationSession:
    session = ExplorationSession(user_id=admin_user.id, title="索引卡",
                                 canvas=empty_canvas())
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _add_attachment(db, session_id: str, name: str, text: str, *,
                    source: str = "upload", summary: str | None = None):
    row = ExplorationAttachment(
        session_id=session_id, filename=name, relative_path=name,
        source=source, editable=True, extracted_text=text,
        char_count=len(text), status="ready", summary=summary)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_attachment_card_lazy_generated_persisted_and_cached(
        db, admin_user, monkeypatch):
    from app.ontologies.agent_runtime import llm_bridge
    session = _make_session(db, admin_user)
    row = _add_attachment(
        db, session.id, "risk-policy.txt",
        "供应商风险管理规范。高风险订单阈值为 88000 元。" + "正文展开。" * 100)
    calls: list = []

    def fake_chat(call_kwargs, messages, tools):
        calls.append((call_kwargs, messages, tools))
        return {"content": "主题：供应商风险管理规范。要点：高风险订单阈值 88000 元。",
                "tool_calls": [], "usage": None}

    monkeypatch.setattr(llm_bridge, "chat", fake_chat)

    block = _attachments_block(db, session.id, "高风险阈值",
                               call_kwargs=dict(_CALL_KWARGS))
    assert len(calls) == 1
    assert calls[0][2] == []                        # 索引卡调用不带工具
    assert calls[0][0]["max_output_tokens"] == 800  # 输出预算上限
    assert "供应商风险管理规范" in calls[0][1][-1]["content"]  # 摘要输入含正文
    db.refresh(row)
    assert row.summary.startswith("主题：供应商风险管理规范")
    assert "索引卡：主题：供应商风险管理规范" in block

    # 第二次构建：summary 已落库 → 不再调 LLM
    block2 = _attachments_block(db, session.id, "高风险阈值",
                                call_kwargs=dict(_CALL_KWARGS))
    assert len(calls) == 1
    assert "索引卡：主题：供应商风险管理规范" in block2


def test_attachment_card_falls_back_when_llm_fails(db, admin_user, monkeypatch):
    from app.ontologies.agent_runtime import llm_bridge
    session = _make_session(db, admin_user)
    text = "采购制度正文开头。" + "普通条款。" * 200
    row = _add_attachment(db, session.id, "procurement.txt", text)
    calls: list = []

    def boom(call_kwargs, messages, tools):
        calls.append(True)
        raise llm_bridge.LLMError("timeout")

    monkeypatch.setattr(llm_bridge, "chat", boom)

    # LLM 失败绝不阻塞回合：注入侧回退「前 400 字」确定性索引卡，但不落库
    # （失败不落库是恢复路径：负缓存过期或正文更新后允许重试生成正式卡片）
    block = _attachments_block(db, session.id, "", call_kwargs=dict(_CALL_KWARGS))
    assert len(calls) == 1
    db.refresh(row)
    assert row.summary is None
    assert "procurement.txt" in block
    assert "索引卡：采购制度正文开头。" in block

    # 进程内负缓存：1 小时内后续回合不反复支付失败调用，注入侧持续兜底
    block2 = _attachments_block(db, session.id, "", call_kwargs=dict(_CALL_KWARGS))
    assert len(calls) == 1
    assert "索引卡：采购制度正文开头。" in block2

    # 负缓存过期后允许重试；成功才写 summary 并清除负缓存
    from app.exploration import context_builder as CB
    failed_at, version = CB._card_failure_cache[row.id]
    CB._card_failure_cache[row.id] = (
        failed_at - CB._CARD_FAILURE_TTL_SECONDS - 1, version)
    monkeypatch.setattr(llm_bridge, "chat", lambda call_kwargs, messages, tools: {
        "content": "主题：采购制度。要点：普通条款汇编。",
        "tool_calls": [], "usage": None})
    block3 = _attachments_block(db, session.id, "", call_kwargs=dict(_CALL_KWARGS))
    db.refresh(row)
    assert row.summary.startswith("主题：采购制度")
    assert row.id not in CB._card_failure_cache
    assert "索引卡：主题：采购制度" in block3


def test_attachment_card_retry_after_text_update_despite_negative_cache(
        db, admin_user, monkeypatch):
    from app.ontologies.agent_runtime import llm_bridge
    session = _make_session(db, admin_user)
    row = _add_attachment(db, session.id, "policy.txt", "旧正文。" * 100)
    calls: list = []

    def boom(call_kwargs, messages, tools):
        calls.append(True)
        raise llm_bridge.LLMError("timeout")

    monkeypatch.setattr(llm_bridge, "chat", boom)
    _attachments_block(db, session.id, "", call_kwargs=dict(_CALL_KWARGS))
    assert len(calls) == 1
    db.refresh(row)
    assert row.summary is None

    # 正文更新（version 递增）使负缓存失效：下回合立即允许重试
    row.extracted_text = "新正文。" * 100
    row.char_count = len(row.extracted_text)
    row.version = (row.version or 0) + 1
    db.commit()
    monkeypatch.setattr(llm_bridge, "chat", lambda call_kwargs, messages, tools: {
        "content": "主题：新正文索引卡。", "tool_calls": [], "usage": None})
    _attachments_block(db, session.id, "", call_kwargs=dict(_CALL_KWARGS))
    db.refresh(row)
    assert row.summary == "主题：新正文索引卡。"


def test_attachment_card_write_back_does_not_overwrite_concurrent_summary(
        db, admin_user, monkeypatch):
    """条件写回：生成期间并发回合已写入 summary 时放弃本地结果（后写不覆盖）。"""
    from sqlalchemy.orm import sessionmaker

    from app.ontologies.agent_runtime import llm_bridge
    session = _make_session(db, admin_user)
    row = _add_attachment(db, session.id, "race.txt", "竞态正文。" * 100)
    monkeypatch.setattr(llm_bridge, "chat", lambda call_kwargs, messages, tools: {
        "content": "本地生成的索引卡。", "tool_calls": [], "usage": None})

    # 模拟并发交错：先在本会话查出 summary=None 的行（进入懒生成路径），
    # 卡片 LLM 调用返回前，另一连接已把新正文卡片写回。
    rows = (db.query(ExplorationAttachment)
            .filter(ExplorationAttachment.session_id == session.id).all())
    OtherSession = sessionmaker(bind=db.get_bind())
    other = OtherSession()
    try:
        other.execute(
            ExplorationAttachment.__table__.update()
            .where(ExplorationAttachment.id == row.id)
            .values(summary="并发写入的新索引卡"))
        other.commit()
    finally:
        other.close()

    from app.exploration import context_builder as CB
    list(CB._ensure_attachment_cards(db, rows, dict(_CALL_KWARGS)))

    db.refresh(row)
    assert row.summary == "并发写入的新索引卡"  # 本地旧结果未覆盖


def test_attachment_card_per_turn_cap_and_progress_events(
        db, admin_user, monkeypatch):
    """单回合卡片生成上限 3 张，逐张发进度事件；超出留待后续回合惰性补齐。"""
    from app.ontologies.agent_runtime import llm_bridge
    session = _make_session(db, admin_user)
    for i in range(5):
        _add_attachment(db, session.id, f"cap-{i}.txt", f"第 {i} 份正文。" * 100)
    calls: list = []
    monkeypatch.setattr(
        llm_bridge, "chat",
        lambda call_kwargs, messages, tools: (
            calls.append(True),
            {"content": "索引卡。", "tool_calls": [], "usage": None})[1])

    from app.exploration import context_builder as CB
    events: list[dict] = []
    gen = CB._attachments_block_events(
        db, session.id, "", call_kwargs=dict(_CALL_KWARGS))
    while True:
        try:
            events.append(next(gen))
        except StopIteration as stop:
            block = stop.value
            break

    assert len(calls) == CB._CARD_LLM_MAX_PER_TURN == 3
    assert len(events) == 3
    for i, event in enumerate(events, start=1):
        assert event["type"] == "step"
        assert event["tool"] == CB._CARD_PROGRESS_TOOL == "attachment_card"
        assert event["arguments"]["index"] == i
        assert event["arguments"]["file"].startswith("cap-")
        assert event["durationMs"] == 0
    # 已生成的 3 张落库并注入；第 4/5 张留待后续回合，注入侧确定性兜底
    assert block.count("索引卡：索引卡。") == 3
    assert "cap-3.txt" in block  # 兜底卡片仍携带文件名与预览

    # 下一回合：剩余 2 张惰性补齐
    calls.clear()
    block2 = _attachments_block(db, session.id, "", call_kwargs=dict(_CALL_KWARGS))
    assert len(calls) == 2
    assert block2.count("索引卡：索引卡。") == 5


def test_attachment_card_skipped_without_llm_config(db, admin_user, monkeypatch):
    from app.ontologies.agent_runtime import llm_bridge
    session = _make_session(db, admin_user)
    text = "无模型配置时的正文。"
    row = _add_attachment(db, session.id, "note.txt", text)
    calls: list = []
    monkeypatch.setattr(llm_bridge, "chat", lambda *args: calls.append(args))

    block = _attachments_block(db, session.id, "")  # 未传 call_kwargs
    assert calls == []
    db.refresh(row)
    assert not row.summary                       # 未生成则不落库
    assert "note.txt" in block and text in block  # 注入侧兜底：标题 + 前 400 字预览


def test_agent_attachment_gets_no_card_and_no_body(db, admin_user, monkeypatch):
    from app.ontologies.agent_runtime import llm_bridge
    session = _make_session(db, admin_user)
    row = _add_attachment(db, session.id, "draft.md", "AI 草稿正文：账期 1 天。",
                          source="agent")
    calls: list = []
    monkeypatch.setattr(llm_bridge, "chat", lambda *args: calls.append(args))

    block = _attachments_block(db, session.id, "账期",
                               call_kwargs=dict(_CALL_KWARGS))
    assert calls == []                              # source=agent 不生成摘要
    db.refresh(row)
    assert not row.summary
    assert "# AI 工作草稿索引" in block
    assert "draft.md" in block
    assert "账期 1 天" not in block                 # 正文仍不作为用户证据注入


def test_injection_shape_cards_for_all_window_only_for_top1(db, admin_user):
    session = _make_session(db, admin_user)
    tail_marker = "尾部唯一口径：坏账计提比例 0.5%。"
    _add_attachment(db, session.id, "a.txt", "甲制度。" * 3_000,
                    summary="A 的索引卡")
    _add_attachment(db, session.id, "b.txt", ("乙制度。" * 2_000) + tail_marker,
                    summary="B 的索引卡")
    _add_attachment(db, session.id, "c.txt", "丙制度。" * 1_000,
                    summary="C 的索引卡")

    # 无 call_kwargs：直接用已落库的索引卡，不发生 LLM 调用
    block = _attachments_block(db, session.id, "坏账计提比例")

    assert "索引卡：A 的索引卡" in block
    assert "索引卡：B 的索引卡" in block
    assert "索引卡：C 的索引卡" in block
    # 原文窗口只给相关度最高的 b.txt；非 top-1 附件原文不注入
    sections = block.split("## 用户资料：")
    for name in ("a.txt", "c.txt"):
        section = next(s for s in sections if s.startswith(name))
        assert "### 字符 " not in section
        assert "甲制度。甲制度。甲制度。甲制度。甲制度。" not in section
    b_section = next(s for s in sections if s.startswith("b.txt"))
    assert "### 字符 " in b_section
    assert tail_marker in b_section


def test_attachment_block_total_cap_omits_overflow_cards(db, admin_user):
    session = _make_session(db, admin_user)
    for i in range(30):
        _add_attachment(db, session.id, f"doc-{i:02d}.txt",
                        f"第 {i} 号制度正文。" * 50,
                        summary=f"第 {i} 号制度索引卡。" * 30)
    block = _attachments_block(db, session.id, "")
    # 总量上限 10K：放不下的索引卡整体省略并计数
    assert "另有" in block and "未展开" in block
    assert len(block) <= 10_500


def test_card_input_samples_head_middle_tail_for_large_docs():
    from app.exploration.context_builder import _card_input_text
    text = "头" * 3_000 + "腹" * 20_000 + "尾" * 3_000
    sampled = _card_input_text(text)
    assert sampled.startswith("头" * 100)
    assert sampled.endswith("尾" * 100)
    assert "腹" in sampled
    assert len(sampled) <= 9_100
    short = "小文档正文"
    assert _card_input_text(short) == short


def test_workspace_update_invalidates_stored_card(db, admin_user, tmp_path,
                                                  monkeypatch):
    from app.config import settings
    from app.exploration.toolkit import ExplorationToolRunner
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path))
    session = _make_session(db, admin_user)
    runner = ExplorationToolRunner(db, session)
    created = runner.run("manage_workspace_file", {
        "action": "create", "path": "notes/policy.md", "content": "v1 正文"})
    row = db.query(ExplorationAttachment).filter_by(id=created["id"]).one()
    row.summary = "旧索引卡"
    db.commit()

    updated = runner.run("manage_workspace_file", {
        "action": "update", "file_id": created["id"], "content": "v2 正文",
        "expected_version": 1})
    assert updated["version"] == 2
    db.refresh(row)
    assert row.summary is None  # 正文已变，索引卡下回合懒生成重建
