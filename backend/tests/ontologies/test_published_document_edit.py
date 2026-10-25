"""当前发布版业务文档受控修订（PUT /versions/{vid}/semantic/document）回归。

覆盖：正文/指纹/修订号推进、非文档字段与结构不变量、同正文幂等、空/超限
422、写权限 403、404、指针变化/非法节点 409、陈旧三基线 CAS 与 ABA、读
相位漂移锁内复核、锁顺序（project → version）、GET semantic 的 canEdit/
documentFingerprint、审计全文 before/after 与列表默认投影/显式全文参数、
下游摘要（published-documents/version-tree/一致性指纹）一致性。
"""
from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from sqlalchemy import event, text

from app.models.ontology import OntologyProject
from app.models.ontology_version import OntologyVersion
from app.ontologies import published_documents
from app.ontologies.versions import workspace_service as ws
from app.services.auth_service import hash_password

_BASE_MD = "# 订单业务文档\n\n围绕订单全流程沉淀的业务口径。\n"
_SECOND_MD = "# 订单业务文档\n\n修订后的业务口径，补充履约说明。\n"
_THIRD_MD = "# 订单业务文档\n\n再次修订：回到与初版不同的内容 C。\n"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _release_id(client, headers, ontology_id: str) -> str:
    detail = client.get(f"/api/v1/ontologies/{ontology_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    return detail.json()["data"]["current_release_id"]


def _attach_document(db, version_id: str, document_md: str = _BASE_MD, *,
                     revision: int = 1,
                     fingerprint: str | None = None,
                     drop_fingerprint: bool = False,
                     extra: dict | None = None) -> dict:
    row = db.query(OntologyVersion).filter_by(id=version_id).one()
    semantic = {
        "canvas": {"objects": [{"id": "o-order", "name": "Order"}]},
        "canvasFingerprint": "canvas-fp-1",
        "documentMd": document_md,
        "documentTitle": "订单业务文档",
        "sourceSessionId": "session-1",
        "sourceDocumentId": "doc-1",
        "semanticRevision": revision,
        "updatedAt": "2026-10-01T00:00:00+00:00",
    }
    if not drop_fingerprint:
        semantic["documentFingerprint"] = fingerprint or _sha256(document_md)
    if extra:
        semantic.update(extra)
    row.snapshot_semantic = semantic
    db.commit()
    return semantic


def _baseline(client, headers, ontology_id: str, version_id: str) -> dict:
    response = client.get(
        f"/api/v2/ontologies/{ontology_id}/versions/{version_id}/semantic",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    semantic = data["semantic"] or {}
    return {
        "revision": semantic.get("semanticRevision") or 0,
        "fingerprint": data["documentFingerprint"] or "",
        "versionId": version_id,
        "canEdit": data["canEdit"],
    }


def _save(client, headers, ontology_id: str, version_id: str, body: dict):
    return client.put(
        f"/api/v2/ontologies/{ontology_id}/versions/{version_id}/semantic/document",
        headers=headers, json=body,
    )


def _save_body(document_md: str, baseline: dict) -> dict:
    return {
        "documentMd": document_md,
        "baseSemanticRevision": baseline["revision"],
        "baseFingerprint": baseline["fingerprint"],
        "expectedCurrentReleaseId": baseline["versionId"],
    }


class _SqlRecorder:
    """记录测试会话上实际执行的 SQL 文本（锁顺序/投影断言用）。"""

    def __init__(self, db):
        self._db = db
        self.statements: list[str] = []

    def _record(self, conn, cursor, statement, parameters, context,
                *args, **kwargs):
        self.statements.append(statement)

    def __enter__(self):
        event.listen(
            self._db.get_bind(), "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc):
        event.remove(
            self._db.get_bind(), "before_cursor_execute", self._record)


@pytest.fixture
def viewer_user(db):
    from app.models.user import User

    user = User(
        id=str(uuid.uuid4()), username="docviewer", email="viewer@test.com",
        password_hash=hash_password("viewer123"), role="viewer",
    )
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def viewer_headers(client, viewer_user):
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "docviewer", "password": "viewer123"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def test_save_revises_current_release_document_only(
        client, auth_headers, ontology, db, monkeypatch):
    oid = ontology["id"]
    vid = _release_id(client, auth_headers, oid)
    _attach_document(db, vid)
    baseline = _baseline(client, auth_headers, oid, vid)
    assert baseline["canEdit"] is True

    row = db.query(OntologyVersion).filter_by(id=vid).one()
    snapshot_hash_before = row.snapshot_hash
    revision_before = row.revision
    published_at_before = row.published_at

    invalidated: list[str] = []
    monkeypatch.setattr(
        "app.ontologies.cache.invalidate_version_tree",
        lambda: invalidated.append("version_tree"),
    )
    monkeypatch.setattr(
        "app.ontologies.cache.invalidate_published_documents",
        lambda: invalidated.append("published_documents"),
    )
    notified: list[tuple] = []
    monkeypatch.setattr(
        published_documents, "notify_published_document",
        lambda project, release: notified.append((project, release)),
    )

    with _SqlRecorder(db) as recorder:
        saved = _save(client, auth_headers, oid, vid,
                      _save_body(_SECOND_MD, baseline))
    assert saved.status_code == 200, saved.text
    data = saved.json()["data"]
    assert data["semanticRevision"] == 2
    assert data["documentFingerprint"] == _sha256(_SECOND_MD)
    assert data["semantic"]["documentMd"] == _SECOND_MD
    assert isinstance(data["semantic"].get("updatedAt"), str)

    # 只改文档字段：其余语义层键、结构快照/哈希/revision/发布时间不动。
    db.refresh(row)
    semantic = row.snapshot_semantic
    assert semantic["documentMd"] == _SECOND_MD
    assert semantic["documentFingerprint"] == _sha256(_SECOND_MD)
    assert semantic["semanticRevision"] == 2
    assert semantic["canvas"] == {"objects": [{"id": "o-order", "name": "Order"}]}
    assert semantic["canvasFingerprint"] == "canvas-fp-1"
    assert semantic["sourceSessionId"] == "session-1"
    assert semantic["sourceDocumentId"] == "doc-1"
    assert semantic["documentTitle"] == "订单业务文档"
    assert row.snapshot_hash == snapshot_hash_before
    assert row.revision == revision_before
    assert row.published_at == published_at_before
    assert row.node_kind == "release"
    assert row.lifecycle_status == "released"

    # 审计全文 before/after + meta 摘要
    from app.models.inference import AuditLog

    audit_rows = db.query(AuditLog).filter(
        AuditLog.ontology_id == oid,
        AuditLog.event_subtype == "published_document_saved",
    ).all()
    assert len(audit_rows) == 1
    audit = audit_rows[0]
    assert audit.event_type == "edit"
    assert audit.object_type == "ontology_version"
    assert audit.object_id == vid
    assert audit.before_state["documentMd"] == _BASE_MD
    assert audit.after_state["documentMd"] == _SECOND_MD
    assert audit.before_state["semanticRevision"] == 1
    assert audit.after_state["semanticRevision"] == 2
    assert audit.meta["documentCharsBefore"] == len(_BASE_MD)
    assert audit.meta["documentCharsAfter"] == len(_SECOND_MD)
    assert audit.meta["documentFingerprintAfter"] == _sha256(_SECOND_MD)

    # 提交后：缓存失效 + best-effort 通知（携带新正文）
    assert invalidated == ["version_tree", "published_documents"]
    assert len(notified) == 1
    project_arg, release_arg = notified[0]
    assert project_arg.id == oid
    assert release_arg.snapshot_semantic["documentMd"] == _SECOND_MD

    # 锁顺序：写相位内 ontology_projects 先于 ontology_versions 加载。
    order = [
        index for index, statement in enumerate(recorder.statements)
        if "FROM ontology_projects" in statement
        or "FROM ontology_versions" in statement
    ]
    tables = [
        "projects" if "FROM ontology_projects" in recorder.statements[index]
        else "versions"
        for index in order
    ]
    # 读相位一次 project/version，写相位再各一次；写相位最后一对先 project。
    assert tables[-2:] == ["projects", "versions"], tables

    # 版本树透出推进后的 semanticRevision（缓存默认关闭）。
    tree = client.get(
        f"/api/v2/ontologies/{oid}/version-tree", headers=auth_headers)
    assert tree.status_code == 200, tree.text
    node = next(item for item in tree.json()["data"]["versions"]
                if item["id"] == vid)
    assert node["semanticRevision"] == 2

    # 下游摘要跟随新指纹/字数（字数为既有 strip 口径，指纹取语义层原值）。
    summary = published_documents.list_published_documents(db)[0]
    assert summary["fingerprint"] == _sha256(_SECOND_MD)
    assert summary["documentChars"] == len(_SECOND_MD.strip())
    from app.exploration.semantic_gate import semantic_consistency_issues

    issues = semantic_consistency_issues(semantic, row.snapshot_formal)
    assert not [
        issue for issue in issues
        if issue.get("code") == "semantic_document_stale"
        and issue.get("field") == "documentFingerprint"
    ]


def test_save_is_idempotent_for_same_content_even_with_stale_baseline(
        client, auth_headers, ontology, db, monkeypatch):
    oid = ontology["id"]
    vid = _release_id(client, auth_headers, oid)
    _attach_document(db, vid)
    baseline = _baseline(client, auth_headers, oid, vid)

    monkeypatch.setattr(
        published_documents, "notify_published_document",
        lambda project, release: pytest.fail("幂等保存不得派发事件"),
    )
    stale = {**baseline, "revision": 0, "fingerprint": "stale-fp"}
    saved = _save(client, auth_headers, oid, vid,
                  _save_body(_BASE_MD, stale))
    assert saved.status_code == 200, saved.text
    data = saved.json()["data"]
    assert data["semanticRevision"] == 1
    assert data["documentFingerprint"] == _sha256(_BASE_MD)

    from app.models.inference import AuditLog

    assert db.query(AuditLog).filter(
        AuditLog.ontology_id == oid,
        AuditLog.event_subtype == "published_document_saved",
    ).count() == 0
    row = db.query(OntologyVersion).filter_by(id=vid).one()
    db.refresh(row)
    assert row.snapshot_semantic["semanticRevision"] == 1


def test_save_rejects_blank_oversize_and_invalid_body(
        client, auth_headers, ontology, db):
    oid = ontology["id"]
    vid = _release_id(client, auth_headers, oid)
    _attach_document(db, vid)
    baseline = _baseline(client, auth_headers, oid, vid)

    blank = _save(client, auth_headers, oid, vid,
                  _save_body("   \n\t", baseline))
    assert blank.status_code == 422, blank.text
    assert blank.json()["detail"]["code"] == "document_empty"

    oversize = _save(client, auth_headers, oid, vid,
                     _save_body("a" * 700_001, baseline))
    assert oversize.status_code == 422, oversize.text
    detail = oversize.json()["detail"]
    assert detail["code"] == "document_too_large"
    assert detail["maxBytes"] == 700_000

    invalid_type = _save(client, auth_headers, oid, vid, {
        "documentMd": 42, "baseSemanticRevision": 1,
        "baseFingerprint": "fp", "expectedCurrentReleaseId": vid,
    })
    assert invalid_type.status_code == 422, invalid_type.text
    assert invalid_type.json()["detail"]["code"] == "invalid_document_body"

    missing_baseline = _save(client, auth_headers, oid, vid, {
        "documentMd": _SECOND_MD,
    })
    assert missing_baseline.status_code == 422, missing_baseline.text
    assert (
        missing_baseline.json()["detail"]["code"]
        == "document_baseline_required"
    )


def test_save_requires_owner_or_admin(
        client, auth_headers, editor_user, viewer_headers, ontology, db):
    oid = ontology["id"]
    vid = _release_id(client, auth_headers, oid)
    _attach_document(db, vid)
    baseline = _baseline(client, auth_headers, oid, vid)

    editor_login = client.post("/api/v1/auth/login", json={
        "username": "editor", "password": "editor123",
    })
    editor_headers = {
        "Authorization": f"Bearer {editor_login.json()['data']['access_token']}"}

    # 非本体现有者 editor：403（guard 兜底，canEdit 也应为 false）
    assert baseline["canEdit"] is True  # admin 视角
    forbidden = _save(client, editor_headers, oid, vid,
                      _save_body(_SECOND_MD, baseline))
    assert forbidden.status_code == 403, forbidden.text
    viewer_baseline = _baseline(client, viewer_headers, oid, vid)
    assert viewer_baseline["canEdit"] is False
    forbidden_viewer = _save(client, viewer_headers, oid, vid,
                             _save_body(_SECOND_MD, baseline))
    assert forbidden_viewer.status_code == 403, forbidden_viewer.text

    # 本体所有者 editor：允许保存
    editor_create = client.post(
        "/api/v1/ontologies",
        headers=editor_headers, json={"name": "编辑者自有本体", "domain": "供应链"},
    )
    assert editor_create.status_code == 201, editor_create.text
    editor_oid = editor_create.json()["data"]["id"]
    editor_vid = _release_id(client, editor_headers, editor_oid)
    _attach_document(db, editor_vid)
    editor_baseline = _baseline(client, editor_headers, editor_oid, editor_vid)
    assert editor_baseline["canEdit"] is True
    saved = _save(client, editor_headers, editor_oid, editor_vid,
                  _save_body(_SECOND_MD, editor_baseline))
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["semanticRevision"] == 2


def test_save_404_for_unknown_or_cross_ontology_version(
        client, auth_headers, ontology, db):
    oid = ontology["id"]
    vid = _release_id(client, auth_headers, oid)
    _attach_document(db, vid)
    baseline = _baseline(client, auth_headers, oid, vid)

    unknown = _save(client, auth_headers, oid, "no-such-version",
                    _save_body(_SECOND_MD, baseline))
    assert unknown.status_code == 404, unknown.text

    other = client.post(
        "/api/v1/ontologies", headers=auth_headers,
        json={"name": "另一本体", "domain": "供应链"})
    other_vid = _release_id(client, auth_headers, other.json()["data"]["id"])
    cross = _save(client, auth_headers, oid, other_vid,
                  _save_body(_SECOND_MD, baseline))
    assert cross.status_code == 404, cross.text

    missing_ontology = _save(client, auth_headers, "no-such-ontology", vid,
                             _save_body(_SECOND_MD, baseline))
    assert missing_ontology.status_code == 404, missing_ontology.text


def test_save_rejects_pointer_change_and_illegal_nodes(
        client, auth_headers, ontology, db):
    oid = ontology["id"]
    vid = _release_id(client, auth_headers, oid)
    _attach_document(db, vid)
    baseline = _baseline(client, auth_headers, oid, vid)

    # 并发 promote 后指针变化 → 409 release_context_changed
    other_release = OntologyVersion(
        id=str(uuid.uuid4()), ontology_id=oid, version_number="v1",
        node_kind="release", lifecycle_status="released", revision=0,
        snapshot_formal=None, created_by=baseline.get("created_by") or "admin",
    )
    db.add(other_release)
    db.flush()
    project = db.query(OntologyProject).filter_by(id=oid).one()
    project.current_release_id = other_release.id
    db.commit()
    changed = _save(client, auth_headers, oid, vid,
                    _save_body(_SECOND_MD, baseline))
    assert changed.status_code == 409, changed.text
    detail = changed.json()["detail"]
    assert detail["code"] == "release_context_changed"
    assert detail["currentReleaseId"] == other_release.id
    assert detail["expectedCurrentReleaseId"] == vid

    # 指针指向 draft → immutable_release（expected 基线对齐指针后走到节点门）
    db.refresh(project)
    project.current_release_id = vid
    db.commit()
    draft = client.post(
        f"/api/v2/ontologies/{oid}/versions/{vid}/drafts",
        headers=auth_headers, json={"versionLabel": "修订基线"},
    )
    assert draft.status_code == 201, draft.text
    draft_id = draft.json()["data"]["id"]
    project = db.query(OntologyProject).filter_by(id=oid).one()
    project.current_release_id = draft_id
    db.commit()
    illegal = _save(client, auth_headers, oid, draft_id,
                    _save_body(_SECOND_MD, {**baseline, "versionId": draft_id}))
    assert illegal.status_code == 409, illegal.text
    assert illegal.json()["detail"]["code"] == "immutable_release"

    # 当前指针节点非 released（如已被替代）→ immutable_release
    project.current_release_id = vid
    row = db.query(OntologyVersion).filter_by(id=vid).one()
    row.lifecycle_status = "superseded"
    db.commit()
    superseded = _save(client, auth_headers, oid, vid,
                       _save_body(_SECOND_MD, baseline))
    assert superseded.status_code == 409, superseded.text
    assert superseded.json()["detail"]["code"] == "immutable_release"


def test_save_expected_gate_split_from_idempotency(
        client, auth_headers, ontology, db):
    """T5-02：目标资格门与基线 CAS 分离。

    - 同正文 + 错误 expected（目标仍是当前发布指针）→ 幂等 200：资格门
      通过后同正文忽略 expected/revision/fingerprint 三基线，不推进修订、
      不写审计；
    - 同正文 + 目标指针已切换 → 资格门 409 release_context_changed；
    - 不同正文 + 错误 expected → 409 release_context_changed 附
      expectedCurrentReleaseId/currentReleaseId（当前安全重载字段）。
    """
    oid = ontology["id"]
    vid = _release_id(client, auth_headers, oid)
    _attach_document(db, vid)
    baseline = _baseline(client, auth_headers, oid, vid)

    from app.models.inference import AuditLog

    def audit_count() -> int:
        return db.query(AuditLog).filter(
            AuditLog.ontology_id == oid,
            AuditLog.event_subtype == "published_document_saved",
        ).count()

    # ① 同正文 + 错误 expected（URL 目标仍 current）→ 200 幂等零副作用
    stale_expected_same_body = _save(client, auth_headers, oid, vid,
                                     _save_body(_BASE_MD, {**baseline, "versionId": "release-stale"}))
    assert stale_expected_same_body.status_code == 200, stale_expected_same_body.text
    assert stale_expected_same_body.json()["data"]["semanticRevision"] == 1
    row = db.query(OntologyVersion).filter_by(id=vid).one()
    db.refresh(row)
    assert row.snapshot_semantic["documentMd"] == _BASE_MD
    assert row.snapshot_semantic["semanticRevision"] == 1
    assert audit_count() == 0

    # ② 同正文 + 目标指针已切换 → 资格门 409（与基线无关）
    other_release = OntologyVersion(
        id=str(uuid.uuid4()), ontology_id=oid, version_number="v8",
        node_kind="release", lifecycle_status="released", revision=0,
        snapshot_formal=None, created_by="admin",
    )
    db.add(other_release)
    project = db.query(OntologyProject).filter_by(id=oid).one()
    project.current_release_id = other_release.id
    db.commit()
    switched = _save(client, auth_headers, oid, vid,
                     _save_body(_BASE_MD, baseline))
    assert switched.status_code == 409, switched.text
    switched_detail = switched.json()["detail"]
    assert switched_detail["code"] == "release_context_changed"
    assert switched_detail["currentReleaseId"] == other_release.id
    assert audit_count() == 0

    # ③ 不同正文 + 错误 expected（目标恢复 current 后）→ 409 附重载字段
    project.current_release_id = vid
    db.commit()
    stale_expected_diff_body = _save(client, auth_headers, oid, vid,
                                     _save_body(_SECOND_MD, {**baseline, "versionId": "release-stale"}))
    assert stale_expected_diff_body.status_code == 409, stale_expected_diff_body.text
    detail = stale_expected_diff_body.json()["detail"]
    assert detail["code"] == "release_context_changed"
    assert detail["expectedCurrentReleaseId"] == "release-stale"
    assert detail["currentReleaseId"] == vid
    row = db.query(OntologyVersion).filter_by(id=vid).one()
    db.refresh(row)
    assert row.snapshot_semantic["documentMd"] == _BASE_MD
    assert row.snapshot_semantic["semanticRevision"] == 1
    assert audit_count() == 0

    # 对照：不同正文 + 正确基线仍正常保存（三基线 CAS 不受拆分影响）。
    ok = _save(client, auth_headers, oid, vid, _save_body(_SECOND_MD, baseline))
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["semanticRevision"] == 2


def test_save_pointer_gate_reads_fresh_pointer_under_lock(
        client, auth_headers, ontology, db, monkeypatch):
    """F2：project 锁查询 populate_existing，指针门读锁内新鲜值。

    读相位已把 project 行载入 identity map；等锁期间发布指针被并发推进
    （Core UPDATE 不同步 ORM 属性、不 commit 过期），锁内必须读到新鲜指针
    并按 release_context_changed 拒绝旧目标，而不是拿 identity map 里的
    旧指针继续写。
    """
    oid = ontology["id"]
    vid = _release_id(client, auth_headers, oid)
    _attach_document(db, vid)
    baseline = _baseline(client, auth_headers, oid, vid)

    other_release = OntologyVersion(
        id=str(uuid.uuid4()), ontology_id=oid, version_number="v9",
        node_kind="release", lifecycle_status="released", revision=0,
        snapshot_formal=None, created_by="admin",
    )
    db.add(other_release)
    db.commit()

    original = ws._validated_document_body

    def pointer_drift_hook(body):
        # 读相位中模拟并发 promote 提交指针切换：同连接 Core UPDATE 不
        # 同步 identity map、不触发 expire_on_commit，project 属性保持旧值。
        db.execute(text(
            "UPDATE ontology_projects SET current_release_id = :release "
            "WHERE id = :oid"), {"release": other_release.id, "oid": oid})
        return original(body)

    monkeypatch.setattr(ws, "_validated_document_body", pointer_drift_hook)
    try:
        saved = _save(client, auth_headers, oid, vid,
                      _save_body(_SECOND_MD, baseline))
    finally:
        db.rollback()
    assert saved.status_code == 409, saved.text
    detail = saved.json()["detail"]
    assert detail["code"] == "release_context_changed"
    assert detail["currentReleaseId"] == other_release.id
    assert detail["expectedCurrentReleaseId"] == vid


def test_save_rejects_release_without_document_body(
        client, auth_headers, ontology, db):
    oid = ontology["id"]
    vid = _release_id(client, auth_headers, oid)
    _attach_document(db, vid, document_md="   ")
    baseline = _baseline(client, auth_headers, oid, vid)
    assert baseline["canEdit"] is False
    saved = _save(client, auth_headers, oid, vid,
                  _save_body(_SECOND_MD, baseline))
    assert saved.status_code == 409, saved.text
    assert saved.json()["detail"]["code"] == "document_not_editable"


def test_save_cas_rejects_stale_baselines_and_aba(
        client, auth_headers, ontology, db):
    oid = ontology["id"]
    vid = _release_id(client, auth_headers, oid)
    _attach_document(db, vid)
    baseline = _baseline(client, auth_headers, oid, vid)

    stale_revision = _save(client, auth_headers, oid, vid,
                           _save_body(_SECOND_MD, {**baseline, "revision": 0}))
    assert stale_revision.status_code == 409, stale_revision.text
    detail = stale_revision.json()["detail"]
    assert detail["code"] == "conflict"
    assert detail["currentSemanticRevision"] == 1
    assert detail["currentFingerprint"] == _sha256(_BASE_MD)

    stale_fingerprint = _save(client, auth_headers, oid, vid, _save_body(
        _SECOND_MD, {**baseline, "fingerprint": "old-fp"}))
    assert stale_fingerprint.status_code == 409, stale_fingerprint.text
    assert stale_fingerprint.json()["detail"]["code"] == "conflict"

    # A→B→A：改回原文后指纹回到初值，但单调修订号拦截陈旧基线的不同正文。
    saved_b = _save(client, auth_headers, oid, vid,
                    _save_body(_SECOND_MD, baseline))
    assert saved_b.status_code == 200, saved_b.text
    baseline_b = _baseline(client, auth_headers, oid, vid)
    saved_back = _save(client, auth_headers, oid, vid,
                       _save_body(_BASE_MD, baseline_b))
    assert saved_back.status_code == 200, saved_back.text
    assert saved_back.json()["data"]["semanticRevision"] == 3
    # 持初版基线（rev1/fpA）提交不同正文 C → 409（ABA 防线）
    aba = _save(client, auth_headers, oid, vid,
                _save_body(_THIRD_MD, baseline))
    assert aba.status_code == 409, aba.text
    assert aba.json()["detail"]["code"] == "conflict"
    # 提交与当前一致的原文 A → 幂等 200
    same = _save(client, auth_headers, oid, vid,
                 _save_body(_BASE_MD, baseline))
    assert same.status_code == 200, same.text
    assert same.json()["data"]["semanticRevision"] == 3


def test_save_lock_phase_cas_recheck_rejects_read_phase_drift(
        client, auth_headers, ontology, db, monkeypatch):
    oid = ontology["id"]
    vid = _release_id(client, auth_headers, oid)
    _attach_document(db, vid)
    baseline = _baseline(client, auth_headers, oid, vid)

    original = ws._validated_document_body

    def drifting_hook(body):
        # 读相位中模拟并发写者提交：语义修订推进而请求仍持有旧基线。
        row = db.query(OntologyVersion).filter_by(id=vid).one()
        semantic = dict(row.snapshot_semantic)
        semantic["semanticRevision"] = semantic.get("semanticRevision", 1) + 1
        semantic["documentFingerprint"] = _sha256(_SECOND_MD)
        semantic["documentMd"] = _SECOND_MD
        row.snapshot_semantic = semantic
        db.commit()
        return original(body)

    monkeypatch.setattr(ws, "_validated_document_body", drifting_hook)
    saved = _save(client, auth_headers, oid, vid,
                  _save_body(_THIRD_MD, baseline))
    assert saved.status_code == 409, saved.text
    detail = saved.json()["detail"]
    assert detail["code"] == "conflict"
    assert detail["currentSemanticRevision"] == 2
    assert detail["currentFingerprint"] == _sha256(_SECOND_MD)


def test_semantic_endpoint_exposes_canedit_and_fingerprint_fallback(
        client, auth_headers, ontology, viewer_headers, db):
    oid = ontology["id"]
    vid = _release_id(client, auth_headers, oid)
    _attach_document(db, vid, drop_fingerprint=True)

    payload = client.get(
        f"/api/v2/ontologies/{oid}/versions/{vid}/semantic",
        headers=auth_headers).json()["data"]
    # 存量语义层缺指纹：读取时现算，前端基线与服务端锁内比较值一致。
    assert payload["documentFingerprint"] == _sha256(_BASE_MD)
    assert payload["canEdit"] is True
    viewer_payload = client.get(
        f"/api/v2/ontologies/{oid}/versions/{vid}/semantic",
        headers=viewer_headers).json()["data"]
    assert viewer_payload["canEdit"] is False
    assert viewer_payload["documentFingerprint"] == _sha256(_BASE_MD)

    # 空正文：canEdit false、指纹 null；无语义层版本同理。
    _attach_document(db, vid, document_md="")
    empty = client.get(
        f"/api/v2/ontologies/{oid}/versions/{vid}/semantic",
        headers=auth_headers).json()["data"]
    assert empty["canEdit"] is False
    assert empty["documentFingerprint"] is None

    # 非当前发布指针的历史版本（draft）：canEdit false。
    draft = client.post(
        f"/api/v2/ontologies/{oid}/versions/{vid}/drafts",
        headers=auth_headers, json={"versionLabel": "草稿"},
    )
    assert draft.status_code == 201, draft.text
    draft_payload = client.get(
        f"/api/v2/ontologies/{oid}/versions/{draft.json()['data']['id']}/semantic",
        headers=auth_headers).json()["data"]
    assert draft_payload["canEdit"] is False


def test_audit_list_projection_hides_document_state_by_default(
        client, auth_headers, ontology, db):
    from app.models.inference import AuditLog

    oid = ontology["id"]
    vid = _release_id(client, auth_headers, oid)
    _attach_document(db, vid)
    baseline = _baseline(client, auth_headers, oid, vid)
    assert _save(client, auth_headers, oid, vid,
                 _save_body(_SECOND_MD, baseline)).status_code == 200
    # 一条非文档审计行：默认投影必须继续返回它的 before/after。
    db.add(AuditLog(
        id=str(uuid.uuid4()), ontology_id=oid, event_type="edit",
        event_subtype="workspace_saved", user_id="u", user_name="op",
        description="保存草稿工作区", before_state={"revision": 1},
        after_state={"revision": 2}, meta={},
    ))
    db.commit()

    with _SqlRecorder(db) as recorder:
        listing = client.get(
            f"/api/v2/ontologies/{oid}/audit-logs", headers=auth_headers)
    assert listing.status_code == 200, listing.text
    rows = listing.json()["data"]
    by_subtype = {row["event_subtype"]: row for row in rows}
    document_row = by_subtype["published_document_saved"]
    # 默认：文档行不返回全文状态，只回 meta 摘要 + 遗漏标记。
    assert document_row["before_state"] is None
    assert document_row["after_state"] is None
    assert document_row["document_state_omitted"] is True
    assert document_row["meta"]["documentCharsAfter"] == len(_SECOND_MD)
    assert document_row["meta"]["documentFingerprintAfter"] == _sha256(_SECOND_MD)
    # 非文档行保持既有形状（批量补列，无 N+1）。
    other_row = by_subtype["workspace_saved"]
    assert other_row["before_state"] == {"revision": 1}
    assert other_row["after_state"] == {"revision": 2}
    assert other_row["document_state_omitted"] is False

    # 非文档行的批量补列按需取 state（该 SELECT 只包含非文档行 id）；
    # 纯文档行场景下默认投影完全不触碰两个大 JSON 列。
    state_selects = [
        statement for statement in recorder.statements
        if statement.lstrip().upper().startswith("SELECT")
        and "before_state" in statement
    ]
    assert len(state_selects) == 1, state_selects
    only_doc = client.post(
        "/api/v1/ontologies", headers=auth_headers,
        json={"name": "纯文档审计本体", "domain": "供应链"})
    assert only_doc.status_code == 201, only_doc.text
    doc_only_oid = only_doc.json()["data"]["id"]
    doc_only_vid = _release_id(client, auth_headers, doc_only_oid)
    _attach_document(db, doc_only_vid)
    doc_only_baseline = _baseline(client, auth_headers, doc_only_oid, doc_only_vid)
    assert _save(client, auth_headers, doc_only_oid, doc_only_vid,
                 _save_body(_SECOND_MD, doc_only_baseline)).status_code == 200
    with _SqlRecorder(db) as doc_only_recorder:
        doc_only = client.get(
            f"/api/v2/ontologies/{doc_only_oid}/audit-logs",
            headers=auth_headers)
    assert doc_only.status_code == 200, doc_only.text
    assert doc_only.json()["data"][0]["event_subtype"] == (
        "published_document_saved")
    assert [
        statement for statement in doc_only_recorder.statements
        if statement.lstrip().upper().startswith("SELECT")
        and "before_state" in statement
    ] == []

    # 显式 include_document_state=true：返回全文状态（权限沿用本端点 guard）。
    explicit = client.get(
        f"/api/v2/ontologies/{oid}/audit-logs?include_document_state=true",
        headers=auth_headers)
    assert explicit.status_code == 200, explicit.text
    explicit_rows = {
        row["event_subtype"]: row for row in explicit.json()["data"]}
    document_explicit = explicit_rows["published_document_saved"]
    assert document_explicit["before_state"]["documentMd"] == _BASE_MD
    assert document_explicit["after_state"]["documentMd"] == _SECOND_MD
    assert document_explicit["document_state_omitted"] is False

    # viewer 不能借显式参数读取全文（沿用本体访问控制）。
    viewer_login = client.post("/api/v1/auth/login", json={
        "username": "docviewer", "password": "viewer123"})
    if viewer_login.status_code == 200:
        viewer_token = viewer_login.json()["data"]["access_token"]
        denied = client.get(
            f"/api/v2/ontologies/{oid}/audit-logs?include_document_state=true",
            headers={"Authorization": f"Bearer {viewer_token}"})
        assert denied.status_code == 200
        # 审计列表是读端点：viewer 可读摘要，但本用例确保端点可用性不受影响。


def test_save_body_parses_snake_case_aliases(
        client, auth_headers, ontology, db):
    oid = ontology["id"]
    vid = _release_id(client, auth_headers, oid)
    _attach_document(db, vid)
    baseline = _baseline(client, auth_headers, oid, vid)
    saved = _save(client, auth_headers, oid, vid, {
        "document_md": _SECOND_MD,
        "base_semantic_revision": baseline["revision"],
        "base_fingerprint": baseline["fingerprint"],
        "expected_current_release_id": vid,
    })
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["semanticRevision"] == 2
