"""本体发布态业务文档的宫殿共享镜像：摄取幂等、建图管线、图谱 UNION、HTTP。"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.models import User
from app.deps import get_current_user, get_db
from app.shared import redis_cache
from app.shared.config import settings
from app.shared.database import Base
from app.super_assistant import palace_cache, palace_graph, palace_service, palace_workspace, router
from app.super_assistant.models import (
    SuperAssistantPalaceBuild,
    SuperAssistantPalaceFile,
    SuperAssistantPalaceOntologyDocument,
)

_TABLES = [
    User.__table__,
    SuperAssistantPalaceFile.__table__,
    SuperAssistantPalaceBuild.__table__,
    SuperAssistantPalaceOntologyDocument.__table__,
]

_PREFIX = "/api/v2/super-assistant"

EVENT = {
    "ontology_id": "ont-1",
    "ontology_name": "供应链本体",
    "version_id": "rel-2",
    "version_number": "v2",
    "title": "供应链业务文档",
    "document_md": "# 供应链\n张三 任职 ACME",
    "fingerprint": "fp-1",
    "published_at": None,
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "super_assistant_palace_workspace_root", str(tmp_path / "palace"))
    # 图谱视图缓存默认关闭：单测不触碰 Redis（缓存行为有专项用例）
    monkeypatch.setattr(settings, "super_assistant_palace_graph_cache_enabled", False)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'palace-ontodocs.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=_TABLES)
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with TestingSession() as db:
        db.add(User(
            id="user-1", username="owner", email="owner@example.com",
            password_hash="x", role="editor",
        ))
        db.commit()

    def override_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(router.router, prefix=_PREFIX)
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: User(
        id="user-1", username="owner", email="owner@example.com",
        password_hash="x", role="editor",
    )
    return SimpleNamespace(
        client=TestClient(app),
        session=TestingSession,
        root=tmp_path / "palace",
    )


def _stub_build(recorder: list[str]):
    def _run(db, doc_id):
        recorder.append(doc_id)
    return _run


# ---------------------------------------------------------------------------
# 摄取幂等状态机：no-op / 失败重试 / 版本替换
# ---------------------------------------------------------------------------


def test_ingest_creates_mirror_and_builds(env, monkeypatch):
    built: list[str] = []
    monkeypatch.setattr(palace_service, "run_ontology_document_build", _stub_build(built))
    with env.session() as db:
        assert palace_service.ingest_ontology_document(db, EVENT) == {"changed": True}
        row = db.query(SuperAssistantPalaceOntologyDocument).one()
        assert built == [row.id]
        assert row.ontology_id == "ont-1"
        assert row.title == "供应链业务文档"
        assert row.fingerprint == "fp-1"
        assert row.status == "pending"
        # Markdown 落共享工作区并完成文本抽取
        scope = palace_graph.ONTOLOGY_DOCUMENTS_SCOPE
        text = palace_workspace.user_workspace(scope).extracted_text(
            palace_workspace.user_dir_id(scope), row.artifact_id, 200,
        )
        assert text.startswith("# 供应链")
        assert row.extracted_chars > 0
        artifact_id = row.artifact_id

        # 重放同指纹且已 built：no-op（每日对账的正常路径）
        row.status = "built"
        row.entity_count = 2
        db.commit()
        built.clear()
        assert palace_service.ingest_ontology_document(db, EVENT) == {"changed": False}
        assert built == []

        # 指纹变化：替换工作区文件、剥离旧贡献、重建
        stripped: list[tuple[str, str]] = []
        monkeypatch.setattr(
            palace_graph, "remove_file_graph",
            lambda owner_id, file_id, filename: stripped.append((file_id, filename)),
        )
        changed_event = {
            **EVENT, "version_id": "rel-3", "version_number": "v3",
            "fingerprint": "fp-2", "document_md": "# 新版本\n李四 属于 B 公司",
        }
        assert palace_service.ingest_ontology_document(db, changed_event) == {"changed": True}
        db.refresh(row)
        assert row.fingerprint == "fp-2"
        assert row.artifact_id != artifact_id
        # 行内计数保留到重建成功再覆盖：它是「曾完整建过图」的剥离依据
        # （重建失败置 failed 时旧图谱贡献仍在，发布新版必须据此剥离）
        assert row.entity_count == 2
        assert stripped == [(row.id, "供应链业务文档")]
        assert built == [row.id]

        # 同指纹但 failed：复用已存文件直接重试（对账自愈失败）
        row.status = "failed"
        db.commit()
        built.clear()
        assert palace_service.ingest_ontology_document(db, changed_event) == {"changed": True}
        db.refresh(row)
        assert row.artifact_id != artifact_id
        assert built == [row.id]

        # 非法 payload：跳过
        assert palace_service.ingest_ontology_document(db, {}) == {
            "changed": False, "reason": "invalid_payload",
        }


def test_ingest_only_rebuilds_when_previous_was_built(env, monkeypatch):
    """失败重试（此前未完整建图）不剥离：首次建图不存在旧贡献。"""
    stripped: list[tuple[str, str]] = []
    built: list[str] = []
    monkeypatch.setattr(palace_service, "run_ontology_document_build", _stub_build(built))
    monkeypatch.setattr(
        palace_graph, "remove_file_graph",
        lambda owner_id, file_id, filename: stripped.append((file_id, filename)),
    )
    with env.session() as db:
        palace_service.ingest_ontology_document(db, EVENT)
        row = db.query(SuperAssistantPalaceOntologyDocument).one()
        row.status = "failed"
        db.commit()
        palace_service.ingest_ontology_document(db, EVENT)
    assert stripped == []


def test_ingest_strips_even_when_rebuild_failed_after_built(env, monkeypatch):
    """对抗式审查 P1 回归：v2 已建图后重建失败（状态 failed、计数保留），
    发布 v3 时必须仍剥离 v2 的旧图谱贡献，否则新旧实体在共享层并存。"""
    built: list[str] = []
    monkeypatch.setattr(palace_service, "run_ontology_document_build", _stub_build(built))
    stripped: list[tuple[str, str]] = []
    monkeypatch.setattr(
        palace_graph, "remove_file_graph",
        lambda owner_id, file_id, filename: stripped.append((file_id, filename)),
    )
    with env.session() as db:
        palace_service.ingest_ontology_document(db, EVENT)
        row = db.query(SuperAssistantPalaceOntologyDocument).one()
        row.status = "built"
        row.entity_count = 5
        db.commit()
        # 模拟后续重建失败：状态 failed，但曾建图的计数仍在
        row.status = "failed"
        db.commit()
        next_event = {**EVENT, "fingerprint": "fp-2", "document_md": "# v3 内容"}
        assert palace_service.ingest_ontology_document(db, next_event) == {"changed": True}
    assert stripped == [(row.id, "供应链业务文档")]


def test_stale_in_flight_row_is_re_driven_not_wedged(env, monkeypatch):
    """对抗式审查 P1 回归：executor 进程中断把行卡在 building（超 30 分钟），
    重投消息/对账/手动重建都必须能重新驱动，而不是永久 no-op / 409。"""
    from datetime import datetime, timedelta, timezone

    built: list[str] = []
    monkeypatch.setattr(palace_service, "run_ontology_document_build", _stub_build(built))
    with env.session() as db:
        db.add(SuperAssistantPalaceOntologyDocument(
            ontology_id="ont-1", version_id="rel-2", version_number="v2",
            ontology_name="供应链本体", title="供应链业务文档",
            fingerprint="fp-1", artifact_id="a", status="building",
            updated_at=datetime.now(timezone.utc) - timedelta(minutes=45),
        ))
        db.commit()
        row = db.query(SuperAssistantPalaceOntologyDocument).one()
        # 同指纹重投：不再 no-op，重新驱动
        assert palace_service.ingest_ontology_document(db, EVENT) == {"changed": True}
        assert built == [row.id]
        db.refresh(row)
        assert row.status == "pending"

    # 手动重建同样放行超龄 building
    built.clear()
    with env.session() as db:
        row = db.query(SuperAssistantPalaceOntologyDocument).one()
        row.status = "building"
        row.updated_at = datetime.now(timezone.utc) - timedelta(minutes=45)
        db.commit()
        doc_id = row.id
    response = env.client.post(f"{_PREFIX}/palace/ontology-documents/{doc_id}/rebuild")
    assert response.status_code == 202
    # 新鲜 building 仍被 409 拒绝
    with env.session() as db:
        row = db.query(SuperAssistantPalaceOntologyDocument).one()
        row.status = "building"
        row.updated_at = datetime.now(timezone.utc)
        db.commit()
    assert env.client.post(
        f"{_PREFIX}/palace/ontology-documents/{doc_id}/rebuild",
    ).status_code == 409


# ---------------------------------------------------------------------------
# 建图管线：与文件抽取同链路（系统作用域）+ 失败入行 + 缓存失效
# ---------------------------------------------------------------------------


def test_run_ontology_document_build_pipeline(env, monkeypatch):
    real_build = palace_service.run_ontology_document_build
    built: list[str] = []
    monkeypatch.setattr(palace_service, "run_ontology_document_build", _stub_build(built))
    invalidations: list[int] = []
    monkeypatch.setattr(palace_cache, "invalidate_graph", lambda: invalidations.append(1))

    def fake_extract_chunk(call_kwargs, chunk):
        return {
            "entities": [
                {"name": "张三", "type": "人物", "aliases": []},
                {"name": "ACME", "type": "组织", "aliases": []},
            ],
            "relations": [{"source": "张三", "target": "ACME", "relation": "任职"}],
        }

    merged: list[tuple[str, str]] = []
    monkeypatch.setattr(palace_service, "extract_chunk", fake_extract_chunk)
    monkeypatch.setattr(palace_service, "_palace_call_kwargs", lambda db: {})
    monkeypatch.setattr(
        palace_graph, "merge_extraction",
        lambda owner_id, file_id, filename, entities, relations:
        (merged.append((owner_id, file_id)) or (len(entities), len(relations))),
    )
    with env.session() as db:
        palace_service.ingest_ontology_document(db, EVENT)
        row = db.query(SuperAssistantPalaceOntologyDocument).one()
        result = real_build(db, row.id)
        assert result.status == "built"
        assert result.entity_count == 2
        assert result.relation_count == 1
        # 合并写入系统作用域（跨本体共享层），file_id 为镜像行 id
        assert merged == [(palace_graph.ONTOLOGY_DOCUMENTS_SCOPE, row.id)]
        assert invalidations == [1]

        # 已建图的重试：先剥离旧贡献（当前标题）再合并
        removed: list[tuple[str, str]] = []
        monkeypatch.setattr(
            palace_graph, "remove_file_graph",
            lambda owner_id, file_id, filename: removed.append((file_id, filename)),
        )
        real_build(db, row.id)
        assert removed == [(row.id, "供应链业务文档")]


def test_run_ontology_document_build_failure_marks_failed(env, monkeypatch):
    real_build = palace_service.run_ontology_document_build
    built: list[str] = []
    monkeypatch.setattr(palace_service, "run_ontology_document_build", _stub_build(built))
    monkeypatch.setattr(palace_service, "_palace_call_kwargs", lambda db: {})

    def boom(call_kwargs, chunk):
        raise RuntimeError("模型超时")

    monkeypatch.setattr(palace_service, "extract_chunk", boom)
    monkeypatch.setattr(palace_cache, "invalidate_graph", lambda: None)
    with env.session() as db:
        palace_service.ingest_ontology_document(db, EVENT)
        row = db.query(SuperAssistantPalaceOntologyDocument).one()
        result = real_build(db, row.id)
        assert result.status == "failed"
        assert "模型超时" in result.error


def test_in_flight_building_is_not_reentered(env):
    with env.session() as db:
        db.add(SuperAssistantPalaceOntologyDocument(
            ontology_id="ont-1", fingerprint="fp-1", status="building",
            title="t", artifact_id="missing",
        ))
        db.commit()
        row = db.query(SuperAssistantPalaceOntologyDocument).one()
        # 30 分钟内的 building 视为在途：直接返回，不再抽取
        assert palace_service.run_ontology_document_build(db, row.id).status == "building"


# ---------------------------------------------------------------------------
# 图谱视图 UNION：用户作用域 ∪ 本体文档共享作用域
# ---------------------------------------------------------------------------


def test_graph_overview_unions_shared_scope(env, monkeypatch):
    with env.session() as db:
        db.add(SuperAssistantPalaceOntologyDocument(
            ontology_id="ont-1", fingerprint="fp-1", status="built",
            entity_count=2, relation_count=1, title="供应链业务文档", artifact_id="a",
        ))
        db.commit()

    def fake_owner_graph(owner_id, node_limit=300):
        if owner_id == palace_graph.ONTOLOGY_DOCUMENTS_SCOPE:
            return {
                "nodes": [{"id": "ont:实体", "name": "实体", "type": "概念"}],
                "edges": [],
                "totals": {"entities": 5, "relations": 3},
                "truncated": False,
            }
        return {
            "nodes": [{"id": "user:实体", "name": "实体", "type": "概念"}],
            "edges": [],
            "totals": {"entities": 1, "relations": 0},
            "truncated": False,
        }

    monkeypatch.setattr(palace_graph, "owner_graph", fake_owner_graph)
    with env.session() as db:
        payload = palace_service.graph_overview(db, "user-1")
    assert payload["available"] is True
    assert payload["totals"] == {"entities": 6, "relations": 3}
    origins = {node["id"]: node.get("origin") for node in payload["nodes"]}
    assert origins == {"user:实体": None, "ont:实体": "ontology"}
    # 统计条口径含本体文档：user-1 无文件时 built/total 仍计本体层
    assert payload["builtFiles"] == 1
    assert payload["totalFiles"] == 1


def test_graph_overview_cache_aside_and_unavailable_skip(env, monkeypatch):
    """缓存命中不重建；Neo4j 瞬态不可用结果不进缓存。"""
    monkeypatch.setattr(settings, "super_assistant_palace_graph_cache_enabled", True)
    store: dict[str, object] = {}
    monkeypatch.setattr(redis_cache, "cache_get", lambda key: store.get(key))
    monkeypatch.setattr(redis_cache, "cache_set", lambda key, value, ttl: store.__setitem__(key, value))
    monkeypatch.setattr(redis_cache, "cache_version", lambda key: "1")

    calls = {"count": 0}

    def fake_owner_graph(owner_id, node_limit=300):
        calls["count"] += 1
        return {"nodes": [], "edges": [], "totals": {"entities": 0, "relations": 0}, "truncated": False}

    monkeypatch.setattr(palace_graph, "owner_graph", fake_owner_graph)
    with env.session() as db:
        db.add(SuperAssistantPalaceOntologyDocument(
            ontology_id="ont-1", fingerprint="fp-1", status="built", artifact_id="a",
        ))
        db.commit()
        first = palace_service.graph_overview(db, "user-1")
        second = palace_service.graph_overview(db, "user-1")
        assert calls["count"] == 2  # 两个作用域各一次
        assert second == first
        assert store, "可用结果应回填缓存"

        # 瞬态不可用：available=False 不缓存
        store.clear()
        calls["count"] = 0

        def unavailable(owner_id, node_limit=300):
            raise palace_graph.PalaceGraphUnavailable("down")

        monkeypatch.setattr(palace_graph, "owner_graph", unavailable)
        payload = palace_service.graph_overview(db, "user-1")
        assert payload["available"] is False
        assert store == {}


# ---------------------------------------------------------------------------
# HTTP：列表 / 预览 / 重建
# ---------------------------------------------------------------------------


def test_http_list_preview_rebuild(env, monkeypatch):
    built: list[str] = []
    monkeypatch.setattr(palace_service, "run_ontology_document_build", _stub_build(built))
    with env.session() as db:
        palace_service.ingest_ontology_document(db, EVENT)

    listed = env.client.get(f"{_PREFIX}/palace/ontology-documents")
    assert listed.status_code == 200
    rows = listed.json()
    assert [row["ontologyId"] for row in rows] == ["ont-1"]
    assert rows[0]["title"] == "供应链业务文档"
    assert rows[0]["status"] == "pending"

    doc_id = rows[0]["id"]
    preview = env.client.get(f"{_PREFIX}/palace/ontology-documents/{doc_id}/preview")
    assert preview.status_code == 200
    assert preview.json()["content"].startswith("# 供应链")
    assert preview.json()["previewable"] is True

    # 抽取中：重建被 409 拒绝
    assert env.client.post(
        f"{_PREFIX}/palace/ontology-documents/{doc_id}/rebuild",
    ).status_code == 409

    with env.session() as db:
        db.get(SuperAssistantPalaceOntologyDocument, doc_id).status = "failed"
        db.commit()
    rebuilt = env.client.post(f"{_PREFIX}/palace/ontology-documents/{doc_id}/rebuild")
    assert rebuilt.status_code == 202
    assert rebuilt.json() == {"dispatched": False}
    # 守护线程异步执行：轮询等待 stub 收到第二次调用（首次来自摄取）
    for _ in range(100):
        if len(built) >= 2:
            break
        time.sleep(0.05)
    assert built == [doc_id, doc_id]

    assert env.client.get(
        f"{_PREFIX}/palace/ontology-documents/missing/preview",
    ).status_code == 404
