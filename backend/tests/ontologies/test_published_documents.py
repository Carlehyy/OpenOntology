"""本体发布态业务文档：payload 提取、事件广播、聚合读与 v0 落地埋点。"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.models import User
from app.ontologies import published_documents, release_context
from app.ontologies.projects.models import OntologyProject
from app.ontologies.versions.models import OntologyVersion
from app.shared.database import Base

_TABLES = [User.__table__, OntologyProject.__table__, OntologyVersion.__table__]


@pytest.fixture
def db_session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'pubdocs.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=_TABLES)
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with TestingSession() as db:
        db.add(User(
            id="u1", username="op", email="op@example.com",
            password_hash="x", role="admin",
        ))
        db.commit()
        yield db
    engine.dispose()


SEMANTIC = {
    "documentMd": "# 业务文档\n张三 任职 ACME",
    "documentTitle": "供应链业务文档",
    "documentFingerprint": "fp-1",
    "semanticRevision": 1,
}


def _release(semantic, *, ontology_id="o1", release_id="rel-1", number="v2"):
    return SimpleNamespace(
        ontology_id=ontology_id, id=release_id, version_number=number,
        snapshot_semantic=semantic,
        published_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# 语义层提取与 payload
# ---------------------------------------------------------------------------


def test_semantic_document_variants():
    assert published_documents._semantic_document(None) is None
    assert published_documents._semantic_document({}) is None
    assert published_documents._semantic_document({"documentMd": "   "}) is None
    # 指纹缺失时按 documentMd 现算 sha256
    document_md, title, fingerprint = published_documents._semantic_document(
        {"documentMd": "正文"},
    )
    assert (document_md, title) == ("正文", "")
    assert fingerprint == hashlib.sha256("正文".encode("utf-8")).hexdigest()


def test_document_payload_carries_content():
    payload = published_documents.document_payload("供应链本体", _release(SEMANTIC))
    assert payload == {
        "ontology_id": "o1",
        "ontology_name": "供应链本体",
        "version_id": "rel-1",
        "version_number": "v2",
        "title": "供应链业务文档",
        "document_md": SEMANTIC["documentMd"],
        "fingerprint": "fp-1",
        "published_at": "2026-09-11T00:00:00+00:00",
    }


def test_document_payload_none_without_document():
    assert published_documents.document_payload("n", _release(None)) is None
    assert published_documents.document_payload("n", _release({})) is None


# ---------------------------------------------------------------------------
# 广播：尽力而为、幂等前提、无语义 no-op、异常不外溢
# ---------------------------------------------------------------------------


def test_notify_dispatches_self_contained_event(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(
        published_documents, "dispatch_ontology_document_published",
        lambda payload: sent.append(payload),
    )
    published_documents.notify_published_document(
        SimpleNamespace(name="供应链本体"), _release(SEMANTIC),
    )
    assert len(sent) == 1
    assert sent[0]["fingerprint"] == "fp-1"
    assert sent[0]["document_md"] == SEMANTIC["documentMd"]

    # 无语义层（手工新建/导入的 v0）：不派发
    published_documents.notify_published_document(
        SimpleNamespace(name="n"), _release(None),
    )
    assert len(sent) == 1


def test_notify_swallows_dispatch_failure(monkeypatch):
    def boom(payload):
        raise RuntimeError("nats down")

    monkeypatch.setattr(
        published_documents, "dispatch_ontology_document_published", boom,
    )
    # 不抛：广播失败绝不影响发布/回滚主链路
    published_documents.notify_published_document(
        SimpleNamespace(name="n"), _release(SEMANTIC),
    )


# ---------------------------------------------------------------------------
# 聚合读：只认 current_release_id 指针，无文档的本体不出现
# ---------------------------------------------------------------------------


def _project(project_id: str, name: str, release_id: str | None, user: str = "u1") -> OntologyProject:
    return OntologyProject(
        id=project_id, name=name, domain="其他", description="",
        build_mode="manual", version="v0", created_by=user,
        current_release_id=release_id,
    )


def _version(version_id: str, ontology_id: str, number: str, semantic) -> OntologyVersion:
    return OntologyVersion(
        id=version_id, ontology_id=ontology_id, version_number=number,
        node_kind="release", lifecycle_status="released", revision=0,
        snapshot_semantic=semantic, created_by="u1",
    )


def test_list_published_documents_follows_release_pointer(db_session):
    db_session.add(_project("p1", "有文档本体", "rel-1"))
    db_session.add(_version("rel-1", "p1", "v3", SEMANTIC))
    db_session.add(_project("p2", "无文档本体", "rel-2"))
    db_session.add(_version("rel-2", "p2", "v0", None))
    db_session.commit()

    items = published_documents.list_published_documents(db_session)
    assert [item["ontologyId"] for item in items] == ["p1"]
    item = items[0]
    assert item["ontologyName"] == "有文档本体"
    assert item["versionNumber"] == "v3"
    assert item["fingerprint"] == "fp-1"
    assert item["documentChars"] == len(SEMANTIC["documentMd"])
    assert "documentMd" not in item  # 摘要不带正文


def test_create_initial_release_does_not_dispatch_pre_commit(db_session, monkeypatch):
    """v0 冻结发生在调用方事务提交之前：此处绝不能广播（提交失败会留下
    永不自愈的幻影镜像——重试落地会创建新的本体 id）。探索落地的提交后
    广播见 application_service（与 promote/rollback 同模式）。"""
    notified: list[str] = []
    monkeypatch.setattr(
        published_documents, "notify_published_document",
        lambda project, release: notified.append(project.id),
    )
    project = _project("p-new", "探索本体", None)
    db_session.add(project)
    db_session.flush()
    release = release_context.create_initial_release(
        db_session, project, snapshot=None, created_by="u1", semantic=SEMANTIC,
    )
    assert notified == []
    assert project.current_release_id == release.id
