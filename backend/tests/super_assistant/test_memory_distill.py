"""记忆蒸馏收敛（find_distill_clusters / apply_distill / 端点 / 运行时工具）。

沿用 test_memory_service.py 的隔离 sqlite 手法：每张用例自建引擎与会话，
provider.chat 一律伪造（fixture 默认替身，用例可再覆盖），不触网。
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.auth.models import User
from app.deps import get_current_user, get_db
from app.model_configs.models import ModelConfig
from app.shared.database import Base
from app.super_assistant import memory_service, provider, router, runtime
from app.super_assistant.memory_service import (
    MemoryDistillError,
    MemoryDistillNotFoundError,
)
from app.super_assistant.models import (
    SuperAssistantMemory,
    SuperAssistantMemoryProfile,
)


def _make_session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'distill.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[
            User.__table__,
            ModelConfig.__table__,
            SuperAssistantMemory.__table__,
            SuperAssistantMemoryProfile.__table__,
        ],
    )
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _seed_users(session: Session) -> None:
    session.add_all([
        User(
            id="owner-1", username="owner1",
            email="owner1@example.com", password_hash="unused",
            role="editor",
        ),
        User(
            id="owner-2", username="owner2",
            email="owner2@example.com", password_hash="unused",
            role="editor",
        ),
    ])
    session.add(ModelConfig(
        id="model-1", name="测试模型", config_type="llm",
        provider="openai", models=["test-model"], enabled=True,
        is_default=True, created_by="owner-1",
    ))
    session.commit()


@pytest.fixture()
def db(tmp_path, monkeypatch):
    # apply_distill 收尾会重编译画像/宫殿：默认替身避免真实 LLM 调用
    monkeypatch.setattr(
        provider,
        "chat",
        lambda *args, **kwargs: {
            "content": "编译产物", "tool_calls": [], "usage": {},
        },
    )
    engine, SessionFactory = _make_session(tmp_path)
    with SessionFactory() as session:
        _seed_users(session)
        yield session
    engine.dispose()


def _seed(
    db: Session,
    owner_id: str,
    content: str,
    **kwargs,
) -> SuperAssistantMemory:
    """种数据时绕过写入冲突检测（蒸馏输入天然是近重复内容）。"""
    return memory_service.create_memory(
        db,
        owner_id,
        content,
        conflict_check=False,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# find_distill_clusters
# ---------------------------------------------------------------------------


def test_find_distill_clusters_groups_only_similar_memories(db):
    first = _seed(db, "owner-1", "用户喜欢喝黑咖啡")
    second = _seed(db, "owner-1", "用户喜欢喝黑咖啡。")
    _seed(db, "owner-1", "今天天气不错适合出门散步")

    clusters = memory_service.find_distill_clusters(db, "owner-1")
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster["cluster_key"] == ",".join(sorted([first.id, second.id]))
    assert [member["id"] for member in cluster["members"]] == [
        first.id,
        second.id,
    ]
    member = cluster["members"][0]
    assert member["content"] == "用户喜欢喝黑咖啡"
    assert member["zone"] == "general"
    assert member["pinned"] is False
    assert member["match_count"] == 0
    assert member["reference_count"] == 0
    assert member["created_at"] is not None
    assert cluster["protected"] is False

    # owner 隔离：他人记忆不参与本主的簇计算
    assert memory_service.find_distill_clusters(db, "owner-2") == []


def test_find_distill_clusters_threshold_boundary(db):
    _seed(db, "owner-1", "用户喜欢喝黑咖啡")
    _seed(db, "owner-1", "用户喜欢喝黑咖啡加奶")

    # 用模块自身的分词/向量逻辑算出精确相似度，验证 >= threshold 的边界语义
    tokens_a = memory_service._tokenize("用户喜欢喝黑咖啡")
    tokens_b = memory_service._tokenize("用户喜欢喝黑咖啡加奶")
    idf = memory_service._idf([tokens_a, tokens_b])
    similarity = memory_service._cosine_similarity(tokens_a, tokens_b, idf)
    assert 0.0 < similarity < 1.0

    assert len(memory_service.find_distill_clusters(
        db, "owner-1", threshold=similarity,
    )) == 1
    assert memory_service.find_distill_clusters(
        db, "owner-1", threshold=similarity + 1e-9,
    ) == []


def test_find_distill_clusters_protected_by_core_zone_or_pin(db):
    core_member = _seed(db, "owner-1", "用户喜欢喝黑咖啡", zone="core")
    _seed(db, "owner-1", "用户喜欢喝黑咖啡。")
    pinned_member = _seed(db, "owner-1", "每天坚持晨跑五公里", pinned=True)
    _seed(db, "owner-1", "每天坚持晨跑五公里。")
    plain = _seed(db, "owner-1", "周末常去图书馆看书")
    _seed(db, "owner-1", "周末常去图书馆看书。")

    protected_by_id = {}
    for cluster in memory_service.find_distill_clusters(db, "owner-1"):
        for member in cluster["members"]:
            protected_by_id[member["id"]] = cluster["protected"]
    assert protected_by_id[core_member.id] is True
    assert protected_by_id[pinned_member.id] is True
    assert protected_by_id[plain.id] is False


def test_find_distill_clusters_survivor_by_effectiveness_then_recency(db):
    older = _seed(db, "owner-1", "用户喜欢喝黑咖啡")
    newer = _seed(db, "owner-1", "用户喜欢喝黑咖啡。")

    # 效果分高者胜出：reference/match 比率映射 0.5+0.5*min(ratio,1)
    older.match_count = 10
    older.reference_count = 10
    db.flush()
    cluster = memory_service.find_distill_clusters(db, "owner-1")[0]
    assert cluster["survivor_id"] == older.id

    # 效果分并列（均为 0.5）时取最新 created_at
    older.match_count = 0
    older.reference_count = 0
    db.flush()
    cluster = memory_service.find_distill_clusters(db, "owner-1")[0]
    assert cluster["survivor_id"] == newer.id


def test_distill_effectiveness_maps_reference_ratio():
    memory = SuperAssistantMemory(owner_id="owner-1", content="x")
    # 未被检索过不享受乐观初值：max(match, 1) 使比率为 0
    memory.match_count = 0
    memory.reference_count = 0
    assert memory_service._distill_effectiveness(memory) == 0.5
    memory.match_count = 10
    assert memory_service._distill_effectiveness(memory) == 0.5
    memory.reference_count = 5
    assert memory_service._distill_effectiveness(memory) == 0.75
    memory.reference_count = 10
    assert memory_service._distill_effectiveness(memory) == 1.0
    # 比率超过 1 时按 1 封顶
    memory.reference_count = 30
    assert memory_service._distill_effectiveness(memory) == 1.0


def test_find_distill_clusters_sorted_by_size_descending(db):
    pair_a = _seed(db, "owner-1", "用户喜欢喝黑咖啡")
    pair_b = _seed(db, "owner-1", "用户喜欢喝黑咖啡。")
    trio = [
        _seed(db, "owner-1", "每天坚持晨跑五公里"),
        _seed(db, "owner-1", "每天坚持晨跑五公里。"),
        _seed(db, "owner-1", "每天坚持晨跑五公里！"),
    ]

    clusters = memory_service.find_distill_clusters(db, "owner-1")
    assert [len(cluster["members"]) for cluster in clusters] == [3, 2]
    assert clusters[0]["cluster_key"] == ",".join(
        sorted(member.id for member in trio)
    )
    assert clusters[1]["cluster_key"] == ",".join(
        sorted([pair_a.id, pair_b.id])
    )


# ---------------------------------------------------------------------------
# apply_distill
# ---------------------------------------------------------------------------


def test_apply_distill_merges_cluster_and_supersedes_members(db):
    first = _seed(
        db, "owner-1", "用户喜欢喝黑咖啡",
        zone="core", confidence="high", tags=["偏好"],
    )
    second = _seed(
        db, "owner-1", "用户喜欢喝黑咖啡。", pinned=True, tags=["咖啡"],
    )
    third = _seed(db, "owner-1", "用户喜欢喝黑咖啡！", tags=["偏好", "日常"])
    # first 效果分最高 → 幸存者：zone/confidence 继承自它
    first.match_count = 10
    first.reference_count = 10
    db.flush()

    merged = memory_service.apply_distill(
        db,
        "owner-1",
        [first.id, second.id, third.id],
        merged_content="用户每天一杯黑咖啡，偏好不加糖",
    )
    assert merged.content == "用户每天一杯黑咖啡，偏好不加糖"
    assert merged.zone == "core"
    assert merged.confidence == "high"
    assert merged.source == "reflection"
    # pinned 取任一成员；tags 为成员并集（去重保序）
    assert merged.pinned is True
    assert merged.tags == ["偏好", "咖啡", "日常"]
    assert merged.supersedes == [first.id, second.id, third.id]

    for old in (first, second, third):
        assert old.superseded is True
    # 默认列表只见合并结果；include_superseded 留档审计
    assert [
        item.id for item in memory_service.list_memories(db, "owner-1")
    ] == [merged.id]
    assert len(memory_service.list_memories(
        db, "owner-1", include_superseded=True,
    )) == 4
    # 合并后不再产生近重复簇
    assert memory_service.find_distill_clusters(db, "owner-1") == []


def test_apply_distill_use_llm_merges_member_contents(db, monkeypatch):
    first = _seed(db, "owner-1", "用户喜欢喝黑咖啡", zone="core")
    second = _seed(db, "owner-1", "用户喜欢喝黑咖啡，不加糖")
    first.match_count = 10
    first.reference_count = 10
    db.flush()

    prompts: list[str] = []

    def fake_chat(_call_kwargs, messages, _tools):
        prompts.append(messages[0]["content"])
        return {
            "content": "用户每天一杯不加糖的黑咖啡",
            "tool_calls": [],
            "usage": {},
        }

    monkeypatch.setattr(provider, "chat", fake_chat)
    merged = memory_service.apply_distill(
        db, "owner-1", [first.id, second.id], use_llm=True,
    )
    assert merged.content == "用户每天一杯不加糖的黑咖啡"
    # 融合 prompt 携带全部成员内容并要求保留事实、压缩到 200 字
    merge_prompt = prompts[0]
    assert "用户喜欢喝黑咖啡" in merge_prompt
    assert "不加糖" in merge_prompt
    assert "200" in merge_prompt


def test_apply_distill_llm_failure_falls_back_to_survivor(db, monkeypatch):
    first = _seed(db, "owner-1", "用户喜欢喝黑咖啡")
    second = _seed(db, "owner-1", "用户喜欢喝黑咖啡。")
    first.match_count = 10
    first.reference_count = 10
    db.flush()

    def failing_chat(*_args, **_kwargs):
        raise RuntimeError("LLM 不可用")

    monkeypatch.setattr(provider, "chat", failing_chat)
    merged = memory_service.apply_distill(
        db, "owner-1", [first.id, second.id], use_llm=True,
    )
    # 幸存者内容兜底，合并本身仍然成功
    assert merged.content == first.content
    assert merged.supersedes == [first.id, second.id]


def test_apply_distill_explicit_content_skips_llm(db, monkeypatch):
    first = _seed(db, "owner-1", "用户喜欢喝黑咖啡")
    second = _seed(db, "owner-1", "用户喜欢喝黑咖啡。")

    prompts: list[str] = []

    def recording_chat(_call_kwargs, messages, _tools):
        prompts.append(messages[0]["content"])
        return {"content": "编译产物", "tool_calls": [], "usage": {}}

    monkeypatch.setattr(provider, "chat", recording_chat)
    merged = memory_service.apply_distill(
        db,
        "owner-1",
        [first.id, second.id],
        merged_content="  用户每日一杯黑咖啡  ",
        use_llm=True,
    )
    assert merged.content == "用户每日一杯黑咖啡"
    # 融合 prompt 未出现（LLM 仅可能用于收尾的画像/宫殿重编译）
    assert all("融合成一条更密的陈述" not in prompt for prompt in prompts)


def test_apply_distill_blank_content_uses_survivor(db):
    older = _seed(db, "owner-1", "用户喜欢喝黑咖啡")
    newer = _seed(db, "owner-1", "用户喜欢喝黑咖啡。")

    merged = memory_service.apply_distill(
        db, "owner-1", [older.id, newer.id], merged_content="   ",
    )
    # 空白 merged_content 视为未提供：use_llm=False 时回退幸存者内容
    # （效果分并列取最新 created_at，即第二条）
    assert merged.content == "用户喜欢喝黑咖啡。"


def test_apply_distill_rejects_invalid_members(db):
    first = _seed(db, "owner-1", "用户喜欢喝黑咖啡")

    with pytest.raises(MemoryDistillError, match="至少需要 2 条"):
        memory_service.apply_distill(db, "owner-1", [first.id])
    with pytest.raises(MemoryDistillError, match="至少需要 2 条"):
        memory_service.apply_distill(db, "owner-1", [first.id, first.id])
    with pytest.raises(MemoryDistillError, match="不存在"):
        memory_service.apply_distill(db, "owner-1", [first.id, "missing-id"])

    # 校验失败不产生任何副作用
    assert len(memory_service.list_memories(db, "owner-1")) == 1


def test_apply_distill_rejects_foreign_and_superseded_members(db):
    first = _seed(db, "owner-1", "用户喜欢喝黑咖啡")
    second = _seed(db, "owner-1", "用户喜欢喝黑咖啡。")
    foreign = _seed(db, "owner-2", "用户喜欢喝黑咖啡")

    with pytest.raises(MemoryDistillNotFoundError, match="不属于当前用户"):
        memory_service.apply_distill(db, "owner-1", [first.id, foreign.id])
    # 404 判定优先于其他校验，且他人记忆不受影响
    assert foreign.superseded is False

    merged = memory_service.apply_distill(db, "owner-1", [first.id, second.id])
    with pytest.raises(MemoryDistillError, match="已被取代"):
        memory_service.apply_distill(db, "owner-1", [first.id, merged.id])


# ---------------------------------------------------------------------------
# HTTP 端点
# ---------------------------------------------------------------------------


@pytest.fixture()
def client_setup(tmp_path, monkeypatch):
    monkeypatch.setattr(
        provider,
        "chat",
        lambda *args, **kwargs: {
            "content": "编译产物", "tool_calls": [], "usage": {},
        },
    )
    engine, SessionFactory = _make_session(tmp_path)
    with SessionFactory() as session:
        _seed_users(session)

    def override_db():
        session = SessionFactory()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()
    app.include_router(router.router, prefix="/api/v2/super-assistant")
    app.dependency_overrides[get_db] = override_db

    def make_client(user_id: str) -> TestClient:
        app.dependency_overrides[get_current_user] = lambda: User(
            id=user_id,
            username=f"user-{user_id}",
            email=f"{user_id}@example.com",
            password_hash="unused",
            role="editor",
        )
        return TestClient(app)

    def seed(owner_id: str, content: str, **kwargs) -> SuperAssistantMemory:
        with SessionFactory() as session:
            return memory_service.create_memory(
                session, owner_id, content, conflict_check=False, **kwargs,
            )

    yield make_client, seed
    engine.dispose()


def test_distill_endpoints_full_flow_and_owner_isolation(client_setup):
    make_client, seed = client_setup
    first = seed("owner-1", "用户喜欢喝黑咖啡", zone="core", pinned=True)
    second = seed("owner-1", "用户喜欢喝黑咖啡。")
    seed("owner-1", "今天天气不错适合出门散步")
    foreign = seed("owner-2", "每天坚持晨跑五公里")

    client = make_client("owner-1")

    report = client.get("/api/v2/super-assistant/memories/distill-report")
    assert report.status_code == 200, report.text
    clusters = report.json()["clusters"]
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster["cluster_key"] == ",".join(sorted([first.id, second.id]))
    assert [member["id"] for member in cluster["members"]] == [
        first.id,
        second.id,
    ]
    assert set(cluster["members"][0]) == {
        "id", "content", "zone", "pinned",
        "match_count", "reference_count", "created_at",
    }
    assert cluster["protected"] is True
    assert cluster["survivor_id"] in {first.id, second.id}

    # owner 隔离：owner-2 看不到 owner-1 的簇
    other = make_client("owner-2")
    assert other.get(
        "/api/v2/super-assistant/memories/distill-report"
    ).json()["clusters"] == []
    # dependency_overrides 挂在 app 上：切回 owner-1 继续全流程
    client = make_client("owner-1")

    # 非 owner 记忆混入 → 404；无效 id / 不成簇 → 400
    assert client.post(
        "/api/v2/super-assistant/memories/distill",
        json={"member_ids": [first.id, foreign.id]},
    ).status_code == 404
    assert client.post(
        "/api/v2/super-assistant/memories/distill",
        json={"member_ids": [first.id, "missing-id"]},
    ).status_code == 400
    assert client.post(
        "/api/v2/super-assistant/memories/distill",
        json={"member_ids": [first.id]},
    ).status_code == 400

    created = client.post(
        "/api/v2/super-assistant/memories/distill",
        json={
            "member_ids": [first.id, second.id],
            "merged_content": "用户每日一杯黑咖啡",
        },
    )
    assert created.status_code == 201, created.text
    merged = created.json()
    assert merged["content"] == "用户每日一杯黑咖啡"
    assert merged["source"] == "reflection"
    assert merged["pinned"] is True
    assert set(merged["supersedes"]) == {first.id, second.id}

    # 旧记忆默认列表不可见（不相似的第三条仍在）；报告收敛为空
    listed_ids = {
        item["id"]
        for item in client.get("/api/v2/super-assistant/memories").json()
    }
    assert merged["id"] in listed_ids
    assert first.id not in listed_ids
    assert second.id not in listed_ids
    assert client.get(
        "/api/v2/super-assistant/memories/distill-report"
    ).json()["clusters"] == []


# ---------------------------------------------------------------------------
# 运行时旧工具 memory_distill 已随 Hindsight 迁移退役
# （REST /memory/distill 服务端报告与合并仍由 memory_service 提供，
#  这里只覆盖 runtime 工具面的退役事实）
# ---------------------------------------------------------------------------


def test_memory_distill_tool_retired_from_catalog_and_read_only_set():
    """runtime 工具目录不再暴露 memory_distill，也不在只读白名单。"""
    tools = runtime._builtin_tools()
    names = [tool["name"] for tool in tools]
    assert "memory_distill" not in names
    assert "memory_distill" not in runtime._READ_ONLY_BUILTIN_TOOLS
    assert "memory_distill" not in runtime._CONFIRMATION_REQUIRED_BUILTIN_TOOLS


def test_memory_distill_tool_dispatch_reports_unknown_tool(db):
    """残留调用 memory_distill 只得到未知工具错误，不再产出旧库报告。"""
    _seed(db, "owner-1", "用户喜欢喝黑咖啡")
    _seed(db, "owner-1", "用户喜欢喝黑咖啡。")
    _seed(db, "owner-1", "今天天气不错适合出门散步")

    output = runtime._execute_builtin_tool(
        db,
        owner_id="owner-1",
        conversation_id="conv-1",
        assistant_message_id="msg-1",
        call_kwargs={},
        name="memory_distill",
        arguments={},
    )
    payload = json.loads(output)
    assert payload == {"error": "未知工具 memory_distill"}
    # 旧库数据不被任何"退役后重放"路径改动
    assert len(memory_service.list_memories(db, "owner-1")) == 3
