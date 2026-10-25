"""Governed write of a draft's business semantic layer.

The exploration domain owns the business-canvas/document semantics and their
consistency gate.  This ontologies-domain service receives those pure functions
(``canvas_fingerprint_fn`` / ``semantic_consistency_fn``) from the existing HTTP
adapter seam in ``versions/router.py``; it never imports ``app.exploration``.

The write is deliberately additive and narrow: it updates only
``OntologyVersion.snapshot_semantic`` and the draft ``revision`` (a CAS step),
invalidates previous trials, writes an audit row and invalidates the version-tree
cache.  ``snapshot_formal`` and ``snapshot_hash`` are never modified, no live
``fo_*`` projection is touched, and the semantic gate is only reused in its
existing strict form (no force/bypass/generation).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from app.ontologies import cache as ontology_cache
from app.ontologies.access import require_ontology_access
from app.ontologies.inference.models import AuditLog
from app.ontologies.projects.models import OntologyProject
from app.ontologies.versions import trial_service, workspace_service
from app.ontologies.versions.models import OntologyTrialRun, OntologyVersion

CANVAS_MAX_ITEMS = 5000
CANVAS_MAX_BYTES = 1_000_000
DOCUMENT_MAX_BYTES = 1_000_000
SEMANTIC_MAX_BYTES = 2_000_000
NESTED_MAX_ITEMS = 500
CANVAS_KEYS = ("objects", "actors", "behaviors", "events", "rules",
               "scenarios", "processes")
NESTED_KEYS = ("attributes", "relations", "inputs", "branches", "steps",
               "metrics")


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.title() for part in tail)


class SemanticLayerWrite(BaseModel):
    """Strict additive write body.

    Callers may only provide the CAS token, the business canvas and the
    document.  Fingerprints and the semantic revision are server-derived;
    unknown fields (for example a caller-supplied ``canvasFingerprint``) are
    rejected rather than ignored.
    """

    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    base_revision: str | None = Field(default=None, min_length=1, max_length=256)
    expected_revision: str | None = Field(default=None, min_length=1, max_length=256)
    canvas: dict[str, Any]
    document_md: str = Field(min_length=1, max_length=DOCUMENT_MAX_BYTES)
    document_title: str = Field(min_length=1, max_length=200)


def _fail(status: int, code: str, message: str, **extra: Any) -> None:
    raise HTTPException(status, detail={"code": code, "message": message, **extra})


def _validation_errors(exc: ValidationError) -> list[dict]:
    errors = []
    for item in exc.errors(include_url=False):
        errors.append({
            "loc": [str(part) for part in item.get("loc", ())],
            "type": str(item.get("type") or ""),
            "msg": str(item.get("msg") or ""),
        })
    return errors


def _semantic_revision(version: OntologyVersion) -> int:
    semantic = version.snapshot_semantic
    if isinstance(semantic, dict):
        value = semantic.get("semanticRevision")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return 0


def _cas_token(body: SemanticLayerWrite) -> str:
    base = (body.base_revision or "").strip() or None
    expected = (body.expected_revision or "").strip() or None
    if base is None and expected is None:
        _fail(422, "semantic_cas_required",
              "baseRevision (or expectedRevision) is required")
    if base is not None and expected is not None and base != expected:
        _fail(422, "semantic_cas_conflict",
              "baseRevision and expectedRevision must match")
    return base or expected  # type: ignore[return-value]


def _serialized_size(value: Any) -> int:
    try:
        return len(json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        _fail(422, "invalid_semantic_layer",
              f"payload is not JSON-serializable: {type(exc).__name__}: {exc}")


def _validate_canvas_envelope(canvas: dict[str, Any]) -> None:
    """Structural envelope checks before any fingerprint/DB work.

    Full canonical model validation stays in exploration.semantic_gate; this
    ontology-side guard rejects unknown top-level keys, non-list collections,
    element/nested-list overflows, and oversized serialized payloads without
    importing the exploration domain.
    """
    if not isinstance(canvas, dict):
        _fail(422, "invalid_semantic_layer", "canvas must be a JSON object")
    unknown = sorted(set(canvas) - set(CANVAS_KEYS))
    if unknown:
        _fail(422, "invalid_semantic_layer",
              "canvas contains unknown top-level keys",
              fields=unknown)
    total = 0
    for key in CANVAS_KEYS:
        value = canvas.get(key)
        if value is None:
            continue
        if not isinstance(value, list):
            _fail(422, "invalid_semantic_layer",
                  f"canvas.{key} must be a list")
        if len(value) > CANVAS_MAX_ITEMS:
            _fail(422, "invalid_semantic_layer",
                  f"canvas.{key} exceeds {CANVAS_MAX_ITEMS} elements")
        total += len(value)
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                _fail(422, "invalid_semantic_layer",
                      f"canvas.{key}[{index}] must be a JSON object")
            for nested in NESTED_KEYS:
                child = item.get(nested)
                if child is None:
                    continue
                if not isinstance(child, list):
                    _fail(422, "invalid_semantic_layer",
                          f"canvas.{key}[{index}].{nested} must be a list")
                if len(child) > NESTED_MAX_ITEMS:
                    _fail(422, "invalid_semantic_layer",
                          f"canvas.{key}[{index}].{nested} exceeds "
                          f"{NESTED_MAX_ITEMS} items")
    if total > CANVAS_MAX_ITEMS:
        _fail(422, "invalid_semantic_layer",
              f"canvas exceeds {CANVAS_MAX_ITEMS} total elements")
    if _serialized_size(canvas) > CANVAS_MAX_BYTES:
        _fail(422, "invalid_semantic_layer",
              f"canvas exceeds {CANVAS_MAX_BYTES} serialized bytes")


def build_semantic_payload(
    canvas: dict[str, Any],
    document_md: str,
    document_title: str,
    semantic_revision: int,
    *,
    canvas_fingerprint_fn: Callable[[dict[str, Any]], str],
) -> dict:
    """Build the canonical semantic layer with server-derived fingerprints."""
    _validate_canvas_envelope(canvas)
    if not isinstance(document_md, str) or not document_md.strip():
        _fail(422, "invalid_semantic_layer", "documentMd must not be empty")
    if len(document_md.encode("utf-8")) > DOCUMENT_MAX_BYTES:
        _fail(422, "invalid_semantic_layer",
              f"documentMd exceeds {DOCUMENT_MAX_BYTES} serialized bytes")
    if not isinstance(document_title, str) or not document_title.strip():
        _fail(422, "invalid_semantic_layer", "documentTitle must not be empty")
    if _serialized_size({"canvas": canvas, "documentMd": document_md,
                         "documentTitle": document_title}) > SEMANTIC_MAX_BYTES:
        _fail(422, "invalid_semantic_layer",
              f"semantic payload exceeds {SEMANTIC_MAX_BYTES} serialized bytes")
    try:
        canvas_fingerprint = canvas_fingerprint_fn(canvas)
    except Exception as exc:  # malformed canvas is a business validation failure
        _fail(422, "invalid_semantic_layer",
              f"canvas cannot be fingerprinted: {type(exc).__name__}: {exc}")
    if not isinstance(canvas_fingerprint, str) or not canvas_fingerprint:
        _fail(422, "invalid_semantic_layer",
              "server fingerprint function returned no fingerprint")
    document_fingerprint = hashlib.sha256(
        document_md.encode("utf-8")).hexdigest()
    payload = {
        "canvas": canvas,
        "canvasFingerprint": canvas_fingerprint,
        "documentMd": document_md,
        "documentTitle": document_title,
        "documentFingerprint": document_fingerprint,
        "semanticRevision": semantic_revision,
    }
    if _serialized_size(payload) > SEMANTIC_MAX_BYTES:
        _fail(422, "invalid_semantic_layer",
              f"semantic payload exceeds {SEMANTIC_MAX_BYTES} serialized bytes")
    return payload


def _assert_consistent(payload: dict, snapshot_formal: dict | None,
                       semantic_consistency_fn: Callable) -> None:
    try:
        issues = semantic_consistency_fn(payload, snapshot_formal)
    except HTTPException:
        raise
    except Exception as exc:
        _fail(422, "invalid_semantic_layer",
              f"semantic validation failed: {type(exc).__name__}: {exc}")
    if issues:
        _fail(422, "semantic_inconsistent",
              "business canvas/document is inconsistent with the formal snapshot",
              errors=issues)


def _running_trial(db: Session, version_id: str) -> OntologyTrialRun | None:
    return db.query(OntologyTrialRun).filter(
        OntologyTrialRun.version_id == version_id,
        OntologyTrialRun.status == "running",
    ).first()


def _ensure_draft_ready(db: Session, version: OntologyVersion) -> None:
    if version.node_kind != "draft":
        _fail(409, "immutable_release",
              "published/release versions are immutable")
    workspace_service._ensure_editable_draft(version)
    running = _running_trial(db, version.id)
    if running is not None:
        _fail(409, "trial_running",
              "a trial is currently running for this draft",
              trialRunId=running.id)


def _audit(db: Session, version: OntologyVersion, current_user: Any,
           *, base_revision: str, old_semantic_revision: int,
           payload: dict) -> None:
    user_id = getattr(current_user, "id", None)
    user_name = getattr(current_user, "username", None)
    db.add(AuditLog(
        ontology_id=version.ontology_id,
        event_type="edit",
        event_subtype="version_semantic_saved",
        user_id=user_id,
        user_name=user_name,
        description=(
            f"保存草稿 {version.version_number} 业务语义层，"
            f"semanticRevision {old_semantic_revision} -> "
            f"{payload['semanticRevision']}，revision 推进至 {version.revision}"
        ),
        object_type="ontology_version",
        object_id=version.id,
        before_state={"semanticRevision": old_semantic_revision},
        after_state={
            "semanticRevision": payload["semanticRevision"],
            "canvasFingerprint": payload["canvasFingerprint"],
            "documentFingerprint": payload["documentFingerprint"],
            "baseRevision": base_revision,
        },
        meta={
            "semanticRevision": payload["semanticRevision"],
            "canvasFingerprint": payload["canvasFingerprint"],
            "documentFingerprint": payload["documentFingerprint"],
            "issueCount": 0,
            "baseRevision": base_revision,
        },
    ))


def _commit(db: Session) -> None:
    """Single commit seam: tests can inject a failure to prove rollback."""
    db.commit()


def save_version_semantic(
    db: Session,
    ontology_id: str,
    version_id: str,
    body: dict,
    current_user: Any,
    *,
    canvas_fingerprint_fn: Callable[[dict[str, Any]], str],
    semantic_consistency_fn: Callable,
) -> dict:
    """Save a business canvas/document on an editing draft under revision CAS.

    Authorization is enforced inside the service by the canonical ontology
    write check, so direct service callers cannot bypass the router guard.  Reads/validations run first without holding row locks.  The short
    lock phase locks project -> version (the repository lock order), re-reads
    the fresh row and repeats every invariant (lifecycle, running trial,
    revision CAS, fingerprints, semantic consistency) before the mutation.
    """
    require_ontology_access(db, ontology_id, current_user, write=True)
    try:
        write = SemanticLayerWrite.model_validate(body)
    except ValidationError as exc:
        _fail(422, "invalid_semantic_layer", "invalid semantic payload",
              errors=_validation_errors(exc))
    base_revision = _cas_token(write)

    draft = db.query(OntologyVersion).filter(
        OntologyVersion.id == version_id,
        OntologyVersion.ontology_id == ontology_id,
    ).first()
    if draft is None:
        _fail(404, "VERSION_NOT_FOUND", "Version not found")
    _ensure_draft_ready(db, draft)

    old_semantic_revision = _semantic_revision(draft)
    payload = build_semantic_payload(
        write.canvas, write.document_md, write.document_title,
        old_semantic_revision + 1,
        canvas_fingerprint_fn=canvas_fingerprint_fn,
    )
    _assert_consistent(payload, draft.snapshot_formal, semantic_consistency_fn)

    with workspace_service._bounded_row_locks(db):
        project = db.query(OntologyProject).filter(
            OntologyProject.id == ontology_id).with_for_update().first()
        if project is None:
            _fail(404, "ONTOLOGY_NOT_FOUND", "Ontology not found")
        locked = db.query(OntologyVersion).filter(
            OntologyVersion.id == version_id,
            OntologyVersion.ontology_id == ontology_id,
        ).populate_existing().with_for_update().first()
        if locked is None:
            _fail(404, "VERSION_NOT_FOUND", "Version not found")
        _ensure_draft_ready(db, locked)
        current_revision = f"{locked.revision}:{locked.snapshot_hash}"
        if current_revision != base_revision:
            _fail(409, "conflict",
                  "the draft changed after this semantic payload was prepared",
                  currentRevision=current_revision)
        # Re-derive against the locked/formal snapshot; a concurrent structure
        # writer cannot be silently overwritten by this semantic write.
        current_semantic_revision = _semantic_revision(locked)
        payload = build_semantic_payload(
            write.canvas, write.document_md, write.document_title,
            current_semantic_revision + 1,
            canvas_fingerprint_fn=canvas_fingerprint_fn,
        )
        _assert_consistent(payload, locked.snapshot_formal,
                           semantic_consistency_fn)
        locked.snapshot_semantic = payload
        locked.revision = (locked.revision or 0) + 1
        trial_service._stale_previous_trials(db, locked)
        _audit(db, locked, current_user,
               base_revision=base_revision,
               old_semantic_revision=current_semantic_revision,
               payload=payload)
        try:
            _commit(db)
        except Exception:
            db.rollback()
            raise

    ontology_cache.invalidate_version_tree()
    return {"data": {
        "versionId": locked.id,
        "versionNumber": locked.version_number,
        "revision": f"{locked.revision}:{locked.snapshot_hash}",
        "semanticRevision": payload["semanticRevision"],
        "canvasFingerprint": payload["canvasFingerprint"],
        "documentFingerprint": payload["documentFingerprint"],
        "snapshotHash": locked.snapshot_hash,
        "issues": [],
    }}
