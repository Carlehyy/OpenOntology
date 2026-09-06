"""Durable change capture for the Sentinel runtime.

Object/link changes and their CDC outbox row are committed atomically.  A
bounded in-process queue only accelerates delivery; the database outbox is the
source of truth and stale claims are recovered after a process restart.

Mapping projection uses a chain-scoped synchronous barrier.  It waits only for
the root events created by that projection and every downstream event carrying
the same ``chain_id``—never for unrelated global work.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import os
import queue
import threading
import time
import uuid

from sqlalchemy import and_, event, false, func, inspect, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models.ontology_formal import LinkInstance, ObjectInstance
from app.models.ontology import OntologyProject
from app.models.ontology_version import OntologyVersion
from app.models.sentinel import Sentinel, SentinelCdcOutbox, SentinelMatchState
from app.ontologies.sentinels.cep import contract as cep_contract
from app.ontologies.sentinels.cep import event_store
from app.ontologies.sentinels.evaluator import in_sentinel_run

logger = logging.getLogger(__name__)

# Editor save performs its own synchronous full evaluation.  Mapping projection
# sets both flags so exact deltas are retained until every mapping is applied.
SUPPRESS_KEY = "_sentinel_suppress_dispatch"
CAPTURE_SUPPRESSED_KEY = "_sentinel_capture_suppressed"
_CAPTURED_KEY = "_sentinel_captured_changes"
_CAPTURED_LINK_KEY = "_sentinel_captured_link_changes"
_CAPTURED_OUTBOX_KEY = "_sentinel_captured_outbox_ids"
MAPPING_SCOPE_KEY = "_sentinel_mapping_scope_ids"

AUTO_DISPATCH = os.getenv(
    "SENTINEL_AUTO_DISPATCH", "1") not in ("0", "false", "False")
_KEY = "_sentinel_changes"
_LINK_KEY = "_sentinel_link_changes"
_OUTBOX_ROWS_KEY = "_sentinel_outbox_rows"
_OUTBOX_IDS_KEY = "_sentinel_outbox_ids"
_CHAIN_KEY = "_sentinel_cdc_chain_id"
_CONTROL_ROWS_KEY = "_sentinel_control_outbox_rows"
_RELEASE_SWITCH_SCOPES_KEY = "_sentinel_release_switch_scopes"

OBJECT_CHANGE = "object_change"
LINK_CHANGE = "link_change"
RELEASE_ACTIVATION = "release_activation"
SCHEDULED_SCAN = "scheduled_scan"
DYNAMIC_ACTIVATION = "dynamic_activation"
BUILTIN_ACTIVATION = "builtin_activation"

# Protocol-v2 in-flight states fence newly-produced work from legacy workers
# sharing the same database.  A new worker can adopt legacy rows, but every
# successful claim is immediately promoted to ``cdc_processing`` so an older
# worker can neither observe a v2 event nor recover its lease.
CDC_HELD = "cdc_held"
CDC_PENDING = "cdc_pending"
CDC_PROCESSING = "cdc_processing"
CDC_RETRY = "cdc_retry"
CDC_DEAD = "cdc_dead"

_HELD_STATUSES = ("held", CDC_HELD)
_PENDING_STATUSES = ("pending", CDC_PENDING)
_PROCESSING_STATUSES = ("processing", CDC_PROCESSING)
_RETRY_STATUSES = ("retry", CDC_RETRY)
_DEAD_STATUSES = ("dead", CDC_DEAD)
_FAILED_STATUSES = (*_RETRY_STATUSES, *_DEAD_STATUSES)
_DISCARDABLE_STATUSES = (
    *_HELD_STATUSES, *_PENDING_STATUSES, *_RETRY_STATUSES,
)
_PUBLIC_STATUS = {
    CDC_HELD: "held",
    CDC_PENDING: "pending",
    CDC_PROCESSING: "processing",
    CDC_RETRY: "retry",
    CDC_DEAD: "dead",
}


def _public_outbox_status(status: str | None) -> str:
    value = str(status or "")
    return _PUBLIC_STATUS.get(value, value)


def _positive_env_int(name: str, default: int, maximum: int) -> int:
    try:
        return min(maximum, max(1, int(os.getenv(name, str(default)))))
    except (TypeError, ValueError):
        logger.warning("%s 配置无效，回退为 %s", name, default)
        return default


_DISPATCH_QUEUE_SIZE = _positive_env_int(
    "SENTINEL_CDC_QUEUE_SIZE", 4096, 100_000)
_DISPATCH_MAX_ATTEMPTS = _positive_env_int(
    "SENTINEL_CDC_MAX_ATTEMPTS", 4, 10)
_MAX_CASCADE_DEPTH = _positive_env_int(
    "SENTINEL_MAX_CASCADE_DEPTH", 8, 32)
_CLAIM_TIMEOUT_SECONDS = _positive_env_int(
    "SENTINEL_CDC_CLAIM_TIMEOUT_SECONDS", 900, 86_400)
_BARRIER_TIMEOUT_SECONDS = _positive_env_int(
    "SENTINEL_CDC_BARRIER_TIMEOUT_SECONDS", 60, 600)
_HELD_RECOVERY_RECHECK_SECONDS = _positive_env_int(
    "SENTINEL_CDC_HELD_RECHECK_SECONDS", 15, 3600)
_COMPLETED_RETENTION_HOURS = _positive_env_int(
    "SENTINEL_CDC_COMPLETED_RETENTION_HOURS", 168, 24 * 365)
_COMPLETED_RETAIN_LIMIT = _positive_env_int(
    "SENTINEL_CDC_COMPLETED_RETAIN_LIMIT", 10_000, 1_000_000)

_dispatch_queue: queue.Queue[str] = queue.Queue(
    maxsize=_DISPATCH_QUEUE_SIZE)
_dispatch_worker: threading.Thread | None = None
_dispatch_worker_lock = threading.Lock()
_dispatch_stop_event = threading.Event()
_background_worker_enabled = False
_cascade_depth: ContextVar[int] = ContextVar(
    "sentinel_cascade_depth", default=0)
_cascade_chain_id: ContextVar[str | None] = ContextVar(
    "sentinel_cascade_chain_id", default=None)
_synchronous_chain_barrier: ContextVar[bool] = ContextVar(
    "sentinel_synchronous_chain_barrier", default=False)
_last_dispatch_error: str | None = None
_last_held_recovery_error: str | None = None
_last_prune_monotonic = 0.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _outbox_enabled_for(session: Session) -> bool:
    if not AUTO_DISPATCH:
        return False
    # Editor save owns its synchronous evaluation and must not leave a second,
    # durable event behind.  Mapping suppression explicitly opts into capture.
    return not (
        session.info.get(SUPPRESS_KEY)
        and not session.info.get(CAPTURE_SUPPRESSED_KEY)
    )


def _session_chain_id(session: Session) -> str:
    inherited = _cascade_chain_id.get()
    if inherited:
        session.info[_CHAIN_KEY] = inherited
        return inherited
    existing = session.info.get(_CHAIN_KEY)
    if existing:
        return str(existing)
    chain_id = str(uuid.uuid4())
    session.info[_CHAIN_KEY] = chain_id
    return chain_id


def _event_depth() -> int:
    current = _cascade_depth.get()
    return current + 1 if in_sentinel_run.get() else current


def _control_dedupe_key(kind: str, *parts: object) -> str:
    material = "\x00".join([kind, *(str(part or "") for part in parts)])
    return f"{kind}:{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _control_outbox_row(
        session: Session, *, kind: str, ontology_id: str,
        ontology_release_id: str, sentinel_id: str | None = None,
        dedupe_key: str, control: dict | None = None,
        suppress_acceleration: bool = False,
        ignore_session_suppression: bool = False,
) -> SentinelCdcOutbox | None:
    """Create one transaction-owned, deduplicated runtime control event."""
    if not AUTO_DISPATCH or (
        not ignore_session_suppression
        and not _outbox_enabled_for(session)
    ):
        return None
    cached = session.info.setdefault(_CONTROL_ROWS_KEY, {}).get(dedupe_key)
    if cached is not None:
        return cached
    for candidate in session.new:
        if (
            isinstance(candidate, SentinelCdcOutbox)
            and candidate.dedupe_key == dedupe_key
        ):
            session.info[_CONTROL_ROWS_KEY][dedupe_key] = candidate
            return candidate
    with session.no_autoflush:
        existing = session.query(SentinelCdcOutbox).filter(
            SentinelCdcOutbox.dedupe_key == dedupe_key,
        ).first()
    if existing is not None:
        session.info[_CONTROL_ROWS_KEY][dedupe_key] = existing
        return existing
    row = SentinelCdcOutbox(
        id=str(uuid.uuid4()),
        chain_id=_session_chain_id(session),
        ontology_id=ontology_id,
        ontology_release_id=ontology_release_id,
        event_kind=kind,
        sentinel_id=sentinel_id,
        dedupe_key=dedupe_key,
        object_type_id=None,
        changed_keys=[],
        link_change=False,
        cascade_depth=_event_depth(),
        mapping_ids=[],
        status=CDC_PENDING,
        attempts=0,
        available_at=_now(),
        result_json={"control": dict(control or {})},
    )
    session.info[_CONTROL_ROWS_KEY][dedupe_key] = row
    session.info.setdefault(_OUTBOX_IDS_KEY, set()).add(row.id)
    if suppress_acceleration:
        session.info.setdefault(
            "_sentinel_synchronous_control_ids", set()).add(row.id)
    session.add(row)
    return row


def _release_has_on_change_builtin(
        session: Session, ontology_id: str, release_id: str) -> bool:
    release = next((
        candidate for candidate in (
            *list(session.new),
            *list(session.identity_map.values()),
        )
        if (
            isinstance(candidate, OntologyVersion)
            and str(candidate.id) == release_id
            and str(candidate.ontology_id) == ontology_id
        )
    ), None)
    if release is None:
        with session.no_autoflush:
            release = session.query(OntologyVersion).filter(
                OntologyVersion.id == release_id,
                OntologyVersion.ontology_id == ontology_id,
            ).first()
    snapshot = (
        release.snapshot_formal
        if release is not None
        and isinstance(release.snapshot_formal, dict)
        else {}
    )
    return any(
        isinstance(raw, dict)
        and bool(raw.get("enabled", True))
        and bool(raw.get("onChange", True))
        for raw in (snapshot.get("sentinels") or [])
    )


def _captured_release_id(
        session: Session, ontology_id: str,
        row_release_id: str | None = None) -> str | None:
    """Resolve the immutable owner at capture time without guessing later.

    A runtime row's explicit lineage wins.  This is essential while a new
    release projection and the project pointer are committed atomically: the
    new rows already carry the candidate release id while the pointer may still
    be the prior release during ``before_flush``.  Legacy rows without lineage
    fall back to the project pointer as observed in this same transaction.
    """
    if row_release_id:
        return str(row_release_id)
    candidates = [
        *list(session.new),
        *list(session.dirty),
        *list(session.identity_map.values()),
    ]
    for candidate in candidates:
        if (
            isinstance(candidate, OntologyProject)
            and str(candidate.id) == str(ontology_id)
        ):
            return (
                str(candidate.current_release_id)
                if candidate.current_release_id else None
            )
    with session.no_autoflush:
        current = session.query(
            OntologyProject.current_release_id,
        ).filter(
            OntologyProject.id == ontology_id,
        ).scalar()
    return str(current) if current else None


def _outbox_row(
        session: Session, *, ontology_id: str,
        ontology_release_id: str | None = None,
        object_type_id: str | None = None,
        link_change: bool = False) -> SentinelCdcOutbox | None:
    if not _outbox_enabled_for(session):
        return None
    release_id = _captured_release_id(
        session, ontology_id, ontology_release_id)
    # Draft-only/legacy projects without an immutable current release have no
    # runtime owner.  They may still materialize mapping data for editing, but
    # that data must not create a retrying CDC event which can only be rejected
    # later by the Sentinel engine.  Trial execution has its own isolated
    # workspace and never enters this production outbox.
    if release_id is None:
        return None
    if str(ontology_id) in session.info.get(
            _RELEASE_SWITCH_SCOPES_KEY, set()):
        # A release activation evaluates the complete final projection.  Any
        # object/link delta written by that same pointer-switch transaction is
        # therefore part of the activation, not an independent trigger.
        return None
    key = (
        ontology_id, release_id, object_type_id, bool(link_change),
    )
    rows = session.info.setdefault(_OUTBOX_ROWS_KEY, {})
    row = rows.get(key)
    if row is not None:
        return row
    row = SentinelCdcOutbox(
        id=str(uuid.uuid4()),
        chain_id=_session_chain_id(session),
        ontology_id=ontology_id,
        ontology_release_id=release_id,
        event_kind=(LINK_CHANGE if link_change else OBJECT_CHANGE),
        object_type_id=object_type_id,
        changed_keys=[],
        link_change=bool(link_change),
        cascade_depth=_event_depth(),
        mapping_ids=sorted({
            str(item)
            for item in session.info.get(MAPPING_SCOPE_KEY, set())
            if item
        }),
        # Mapping events remain durably fenced until the mapping transaction
        # reaches ``applied`` and dispatch_captured_changes activates them.
        status=(
            CDC_HELD
            if session.info.get(SUPPRESS_KEY)
            and session.info.get(CAPTURE_SUPPRESSED_KEY)
            else CDC_PENDING
        ),
        attempts=0,
        available_at=_now(),
    )
    rows[key] = row
    session.info.setdefault(_OUTBOX_IDS_KEY, set()).add(row.id)
    session.add(row)
    return row


def _record(session: Session, target: ObjectInstance, keys: list) -> None:
    release_id = _captured_release_id(
        session, target.ontology_id, target.ontology_release_id)
    if release_id is None:
        return
    bucket = session.info.setdefault(_KEY, {})
    key = (target.ontology_id, target.object_type_id)
    bucket.setdefault(key, set()).update(keys)
    row = _outbox_row(
        session,
        ontology_id=target.ontology_id,
        ontology_release_id=release_id,
        object_type_id=target.object_type_id,
    )
    if row is not None:
        row.changed_keys = sorted(
            set(row.changed_keys or []) | {str(item) for item in keys})


def _record_link_ontology(
        session: Session, ontology_id: str | None,
        ontology_release_id: str | None = None) -> None:
    if not ontology_id:
        return
    release_id = _captured_release_id(
        session, ontology_id, ontology_release_id)
    if release_id is None:
        return
    session.info.setdefault(_LINK_KEY, set()).add(ontology_id)
    _outbox_row(
        session,
        ontology_id=ontology_id,
        ontology_release_id=release_id,
        link_change=True,
    )


def _record_link(session: Session, target: LinkInstance) -> None:
    _record_link_ontology(
        session, target.ontology_id, target.ontology_release_id)


def _record_event_log(session: Session, target: ObjectInstance,
                      change_kind: str, changes: dict) -> None:
    """实例级事件事实与 outbox 同点捕获（键级行，携带 old→new 值）。

    事实不受编辑器保存抑制影响：outbox 被抑制时评估由保存路径同步完成，
    但"发生过什么"仍需入日志供 changed_within/prev 与模式匹配消费。
    发布切换事务内的投影重建标记为 release_activation，不参与时间算子。
    """
    if not changes:
        return
    release_id = _captured_release_id(
        session, target.ontology_id, target.ontology_release_id)
    if release_id is None:
        return
    source = (
        cep_contract.EVENT_SOURCE_RELEASE_ACTIVATION
        if str(target.ontology_id) in session.info.get(
            _RELEASE_SWITCH_SCOPES_KEY, set())
        else cep_contract.EVENT_SOURCE_ORGANIC
    )
    event_store.capture_instance_changes(
        session, target,
        release_id=release_id, change_kind=change_kind, changes=changes,
        source=source, cascade_depth=_event_depth(),
        chain_id=_session_chain_id(session))


def _merge_pointer_switch_deltas(
        session: Session, ontology_id: str, release_id: str,
        activation_event_id: str | None) -> None:
    """Merge transaction-local business CDC into one release activation.

    Promotion materializes the new release before it changes the project
    pointer, so ordinary outbox rows can already have been flushed by the time
    ``before_flush`` observes the pointer switch.  Completing those rows inside
    the still-open transaction keeps an audit trail while making them
    permanently unclaimable.  Later flushes are suppressed by
    ``_RELEASE_SWITCH_SCOPES_KEY``.
    """
    session.info.setdefault(_RELEASE_SWITCH_SCOPES_KEY, set()).add(
        str(ontology_id))
    # 本事务早前 flush 已按 organic 落行的实例事件（晋级先重建投影、后切
    # 指针）回溯改标为 release_activation，与 outbox 的合并处置对齐。
    event_store.relabel_pending_as_activation(session, ontology_id)
    changes = session.info.get(_KEY, {})
    for key in list(changes):
        if str(key[0]) == str(ontology_id):
            changes.pop(key, None)
    session.info.get(_LINK_KEY, set()).discard(ontology_id)

    merged_at = _now()
    merged_ids: set[str] = set()
    for row in set(session.info.get(_OUTBOX_ROWS_KEY, {}).values()):
        if (
            str(row.ontology_id) != str(ontology_id)
            or str(row.event_kind or "") not in (
                OBJECT_CHANGE, LINK_CHANGE)
            or str(row.status or "") not in _DISCARDABLE_STATUSES
        ):
            continue
        row.status = "completed"
        row.result_json = {
            "evaluated": 0,
            "fired": 0,
            "errors": 0,
            "status": "merged",
            "outcome": "merged_into_release_activation",
            "superseded": True,
            "activatedReleaseId": release_id,
            "activationEventId": activation_event_id,
        }
        row.last_error = None
        row.processed_at = merged_at
        row.claimed_at = None
        row.claim_token = None
        row.updated_at = merged_at
        merged_ids.add(str(row.id))
    if merged_ids:
        session.info.get(_OUTBOX_IDS_KEY, set()).difference_update(merged_ids)
        session.info.get(
            _CAPTURED_OUTBOX_KEY, set()).difference_update(merged_ids)


def _before_flush(session: Session, flush_context, instances) -> None:
    for obj in list(session.dirty):
        if not isinstance(obj, OntologyProject):
            continue
        pointer_history = inspect(obj).attrs.current_release_id.history
        if not pointer_history.has_changes() or not obj.current_release_id:
            continue
        release_id = str(obj.current_release_id)
        activation = None
        if _release_has_on_change_builtin(
                session, str(obj.id), release_id):
            activation = _control_outbox_row(
                session,
                kind=RELEASE_ACTIVATION,
                ontology_id=str(obj.id),
                ontology_release_id=release_id,
                dedupe_key=_control_dedupe_key(
                    RELEASE_ACTIVATION, obj.id, release_id),
                control={
                    "activatedReleaseId": release_id,
                    "previousReleaseId": (
                        str(pointer_history.deleted[0])
                        if pointer_history.deleted
                        and pointer_history.deleted[0] else None
                    ),
                },
                # A reused request Session can retain editor-save suppression.
                # Release activation is a management control event and must
                # remain transactionally durable regardless of that marker.
                ignore_session_suppression=True,
            )
        _merge_pointer_switch_deltas(
            session,
            str(obj.id),
            release_id,
            str(activation.id) if activation is not None else None,
        )
    for obj in list(session.new):
        if isinstance(obj, ObjectInstance):
            if obj.id is None:
                obj.id = str(uuid.uuid4())
            _record(session, obj, sorted(
                set((obj.properties or {}).keys())
                | set((obj.computed or {}).keys())
            ))
            created_values = {
                **(obj.properties or {}), **(obj.computed or {})}
            _record_event_log(session, obj, cep_contract.EVENT_KIND_CREATED, {
                str(key): (None, value)
                for key, value in created_values.items()
            })
        elif isinstance(obj, LinkInstance):
            if obj.id is None:
                obj.id = str(uuid.uuid4())
            _record_link(session, obj)
    for obj in list(session.deleted):
        if isinstance(obj, ObjectInstance):
            _record(session, obj, ["__deleted__"])
            _record_event_log(session, obj, cep_contract.EVENT_KIND_DELETED, {
                cep_contract.DELETED_EVENT_KEY: (None, None)})
        elif isinstance(obj, LinkInstance):
            _record_link(session, obj)
    for obj in list(session.dirty):
        st = inspect(obj)
        if isinstance(obj, ObjectInstance):
            properties_history = st.attrs.properties.history
            computed_history = st.attrs.computed.history
            if (
                not properties_history.has_changes()
                and not computed_history.has_changes()
            ):
                continue
            changed: set[str] = set()
            key_changes: dict[str, tuple] = {}
            for history, current in (
                (properties_history, obj.properties or {}),
                (computed_history, obj.computed or {}),
            ):
                if not history.has_changes():
                    continue
                previous = (
                    history.deleted[0] if history.deleted else {})
                for key in set(previous) | set(current):
                    old_value = (previous or {}).get(key)
                    new_value = (current or {}).get(key)
                    if old_value == new_value:
                        continue
                    changed.add(str(key))
                    key_changes[str(key)] = (old_value, new_value)
            _record(session, obj, sorted(changed))
            _record_event_log(
                session, obj, cep_contract.EVENT_KIND_UPDATED, key_changes)
        elif isinstance(obj, LinkInstance):
            watched = (
                "ontology_id", "ontology_release_id", "link_type_id",
                "source_object_id", "target_object_id", "properties",
            )
            histories = [st.attrs[name].history for name in watched]
            if not any(history.has_changes() for history in histories):
                continue
            _record_link(session, obj)
            old_release_ids = list(
                st.attrs.ontology_release_id.history.deleted)
            old_release_id = (
                str(old_release_ids[0]) if old_release_ids
                and old_release_ids[0] else None
            )
            for old_ontology_id in st.attrs.ontology_id.history.deleted:
                _record_link_ontology(
                    session, old_ontology_id, old_release_id)


def _result_error_count(result: dict | None) -> int:
    raw = (result or {}).get("errors", 0)
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, int):
        return max(raw, 0)
    if isinstance(raw, (list, tuple, set, dict)):
        return len(raw)
    try:
        return max(int(raw), 0)
    except (TypeError, ValueError):
        return 1 if raw else 0


class _LogicalDispatchError(RuntimeError):
    def __init__(self, message: str, result: dict | None = None):
        super().__init__(message)
        self.result = result


class _ReleaseSuperseded(RuntimeError):
    """The outbox owner is no longer the ontology's current release."""

    def __init__(
            self, event_release_id: str | None,
            current_release_id: str | None):
        super().__init__("CDC event release has been superseded")
        self.event_release_id = event_release_id
        self.current_release_id = current_release_id

    def as_result(self) -> dict:
        return {
            "evaluated": 0,
            "fired": 0,
            "errors": 0,
            "firings": [],
            "status": "stale",
            "outcome": "superseded",
            "superseded": True,
            "skipped": "release_superseded",
            "eventReleaseId": self.event_release_id,
            "currentReleaseId": self.current_release_id,
        }


def _release_pointer_statement(
        ontology_id: str, *, postgresql_lock: bool):
    statement = select(
        OntologyProject.current_release_id,
    ).where(
        OntologyProject.id == ontology_id,
    )
    if postgresql_lock:
        # FOR KEY SHARE blocks promotion/rollback's FOR UPDATE pointer switch,
        # but remains compatible with the runtime Session's FOR NO KEY UPDATE
        # validation/action fence. FOR SHARE would self-deadlock because the
        # complete-run lease is held by a different connection.
        statement = statement.with_for_update(
            read=True, key_share=True)
    return statement


def _locked_current_release_id(
        db: Session, ontology_id: str) -> str | None:
    """Read and lock the release pointer for the current transaction."""
    statement = _release_pointer_statement(
        ontology_id,
        postgresql_lock=(
            db.get_bind().dialect.name == "postgresql"),
    )
    row = db.execute(statement).first()
    current = row[0] if row is not None else None
    return str(current) if current else None


def _assert_event_release(
        db: Session, ontology_id: str,
        event_release_id: str | None) -> str | None:
    """Fence one transaction to the release captured by its CDC event."""
    expected = str(event_release_id) if event_release_id else None
    current = _locked_current_release_id(db, ontology_id)
    if current != expected:
        raise _ReleaseSuperseded(expected, current)
    return current


@contextmanager
def _release_execution_lease(
        db: Session, ontology_id: str,
        event_release_id: str | None):
    """Keep promotion ordered behind the complete multi-transaction run.

    Evaluator/action durability requires intermediate commits, so a row lock
    held by the worker Session alone would be released too early.  PostgreSQL
    therefore uses a second connection and a shared row lock for the complete
    engine call.  Concurrent CDC readers can coexist, while promotion's UPDATE
    waits.  SQLite has no row-level shared lock; its per-transaction guard below
    still detects a pointer change before the next stage.
    """
    if db.get_bind().dialect.name != "postgresql":
        _assert_event_release(db, ontology_id, event_release_id)
        yield
        return

    bind = db.get_bind()
    engine = getattr(bind, "engine", bind)
    connection = engine.connect()
    transaction = connection.begin()
    expected = str(event_release_id) if event_release_id else None
    try:
        statement = _release_pointer_statement(
            ontology_id, postgresql_lock=True)
        row = connection.execute(statement).first()
        current_raw = row[0] if row is not None else None
        current = str(current_raw) if current_raw else None
        if current != expected:
            raise _ReleaseSuperseded(expected, current)
        yield
    finally:
        transaction.rollback()
        connection.close()


def _release_transaction_guard(
        ontology_id: str, event_release_id: str | None):
    """Build a Session ``after_begin`` guard for evaluator sub-transactions.

    Sentinel evaluation deliberately commits durable match/action state in
    stages.  Reacquiring the project-row lock at every new transaction closes
    the window in which a newly promoted release could otherwise be evaluated
    by an old outbox event.
    """
    expected = str(event_release_id) if event_release_id else None

    def guard(session, transaction, connection) -> None:
        if getattr(transaction, "nested", False):
            return
        statement = _release_pointer_statement(
            ontology_id,
            postgresql_lock=(
                connection.dialect.name == "postgresql"),
        )
        row = connection.execute(statement).first()
        current_raw = row[0] if row is not None else None
        current = str(current_raw) if current_raw else None
        if current != expected:
            raise _ReleaseSuperseded(expected, current)

    return guard


def _eligible_filter(now: datetime, stale_before: datetime):
    return or_(
        and_(
            SentinelCdcOutbox.status.in_(
                (*_PENDING_STATUSES, *_RETRY_STATUSES)),
            SentinelCdcOutbox.available_at <= now,
        ),
        and_(
            SentinelCdcOutbox.status.in_(_PROCESSING_STATUSES),
            SentinelCdcOutbox.claimed_at < stale_before,
        ),
    )


def _claim_one(
        db: Session, event_id: str, now: datetime,
        stale_before: datetime) -> str | None:
    """Atomic claim with PostgreSQL SKIP LOCKED and a SQLite CAS fallback."""
    eligible = _eligible_filter(now, stale_before)
    candidate = db.query(SentinelCdcOutbox.id).filter(
        SentinelCdcOutbox.id == event_id, eligible)
    if db.get_bind().dialect.name == "postgresql":
        candidate = candidate.with_for_update(skip_locked=True)
    if candidate.first() is None:
        db.rollback()
        return None
    token = str(uuid.uuid4())
    changed = db.query(SentinelCdcOutbox).filter(
        SentinelCdcOutbox.id == event_id,
        eligible,
    ).update({
        SentinelCdcOutbox.status: CDC_PROCESSING,
        SentinelCdcOutbox.claimed_at: now,
        SentinelCdcOutbox.claim_token: token,
        SentinelCdcOutbox.attempts: SentinelCdcOutbox.attempts + 1,
        SentinelCdcOutbox.updated_at: now,
    }, synchronize_session=False)
    db.commit()
    return token if changed == 1 else None


def _control_checkpoint(
        db: Session, event_row: SentinelCdcOutbox) -> dict:
    payload = (
        dict(event_row.result_json)
        if isinstance(event_row.result_json, dict) else {}
    )
    existing = payload.get("checkpoint")
    if isinstance(existing, dict):
        return existing
    query = db.query(SentinelMatchState).filter(
        SentinelMatchState.ontology_id == event_row.ontology_id,
    )
    if event_row.event_kind in (
        SCHEDULED_SCAN, DYNAMIC_ACTIVATION, BUILTIN_ACTIVATION,
    ):
        query = query.filter(
            SentinelMatchState.sentinel_id == event_row.sentinel_id)
    states: dict[str, dict] = {}
    for state in query.all():
        states.setdefault(str(state.sentinel_id), {})[str(state.id)] = {
            "executionEpoch": int(state.execution_epoch or 0),
            "runtimeStatus": str(state.runtime_status or "completed"),
        }
    checkpoint = {"states": states}
    payload["checkpoint"] = checkpoint
    event_row.result_json = payload
    db.commit()
    return checkpoint


def _superseded_control_result(
        event_row: SentinelCdcOutbox, reason: str) -> dict:
    return {
        "evaluated": 0,
        "fired": 0,
        "errors": 0,
        "firings": [],
        "status": "stale",
        "outcome": "superseded",
        "superseded": True,
        "skipped": reason,
        "eventReleaseId": event_row.ontology_release_id,
        "sentinelId": event_row.sentinel_id,
    }


def _control_int(control: dict, key: str) -> int | None:
    value = control.get(key)
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _dynamic_control_stale_reason(
        db: Session, event_row: SentinelCdcOutbox,
        control: dict) -> str | None:
    expected_revision = _control_int(control, "definitionRevision")
    expected_generation = _control_int(control, "enableGeneration")
    dynamic = db.query(Sentinel).filter(
        Sentinel.id == event_row.sentinel_id,
        Sentinel.ontology_id == event_row.ontology_id,
    ).first()
    if dynamic is None:
        return "dynamic_sentinel_missing"
    if (
        dynamic.origin != "assistant_dynamic"
        or dynamic.status != "published"
        or not dynamic.enabled
        or bool(dynamic.muted)
        or dynamic.retired_at is not None
    ):
        return "dynamic_sentinel_inactive"
    if str(dynamic.bound_release_id or "") != str(
            event_row.ontology_release_id or ""):
        return "dynamic_sentinel_release_changed"
    if (
        expected_revision is None
        or int(dynamic.definition_revision or 0) != expected_revision
    ):
        return "dynamic_sentinel_revision_changed"
    if (
        expected_generation is None
        or int(dynamic.enable_generation or 0) != expected_generation
    ):
        return "dynamic_sentinel_enable_changed"
    if (
        dynamic.last_trial_release_id != dynamic.bound_release_id
        or dynamic.last_trial_revision != dynamic.definition_revision
        or not isinstance(dynamic.last_trial_report, dict)
        or not bool(dynamic.last_trial_report.get("passed"))
    ):
        return "dynamic_sentinel_trial_stale"
    return None


def _builtin_control_stale_reason(
        db: Session, event_row: SentinelCdcOutbox,
        control: dict, *, require_on_change: bool = False) -> str | None:
    expected_generation = _control_int(control, "enableGeneration")
    live = db.query(Sentinel).filter(
        Sentinel.id == event_row.sentinel_id,
        Sentinel.ontology_id == event_row.ontology_id,
    ).first()
    if live is None:
        return "builtin_sentinel_missing"
    if (
        live.origin != "release_builtin"
        or live.status != "published"
        or not live.enabled
        or bool(live.muted)
        or live.retired_at is not None
    ):
        return "builtin_sentinel_inactive"
    if (
        expected_generation is None
        or int(live.enable_generation or 0) != expected_generation
    ):
        return "builtin_sentinel_enable_changed"
    release = db.query(OntologyVersion).filter(
        OntologyVersion.id == event_row.ontology_release_id,
        OntologyVersion.ontology_id == event_row.ontology_id,
        OntologyVersion.node_kind == "release",
        OntologyVersion.lifecycle_status == "released",
    ).first()
    snapshot = (
        release.snapshot_formal
        if release is not None
        and isinstance(release.snapshot_formal, dict)
        else {}
    )
    member = next((
        raw for raw in (snapshot.get("sentinels") or [])
        if isinstance(raw, dict)
        and str(raw.get("id") or "") == str(event_row.sentinel_id or "")
    ), None)
    if member is None:
        return "builtin_sentinel_not_in_release"
    if require_on_change and not bool(member.get("onChange", True)):
        return "builtin_sentinel_not_on_change"
    return None


def _execute_claimed_event(
        db: Session, event_id: str, claim_token: str) -> dict:
    event_row = db.query(SentinelCdcOutbox).filter(
        SentinelCdcOutbox.id == event_id,
        SentinelCdcOutbox.status == CDC_PROCESSING,
        SentinelCdcOutbox.claim_token == claim_token,
    ).first()
    if event_row is None:
        raise _LogicalDispatchError("CDC outbox claim 已丢失")
    if int(event_row.cascade_depth or 0) > _MAX_CASCADE_DEPTH:
        raise _LogicalDispatchError(
            f"哨兵级联深度 {event_row.cascade_depth} 超过上限 "
            f"{_MAX_CASCADE_DEPTH}，已阻断潜在循环")
    event_release_id = (
        str(event_row.ontology_release_id)
        if event_row.ontology_release_id else None
    )
    event_kind = str(
        event_row.event_kind
        or (LINK_CHANGE if event_row.link_change else OBJECT_CHANGE)
    )
    from app.ontologies.sentinels.engine import (
        run_for_change,
        run_for_link_change,
        run_builtin_initialization,
        run_dynamic_initialization,
        run_release_initialization,
        run_scheduled_event,
    )

    try:
        with _release_execution_lease(
                db, event_row.ontology_id, event_release_id):
            release_guard = _release_transaction_guard(
                event_row.ontology_id, event_release_id)
            event.listen(db, "after_begin", release_guard)
            chain_token = _cascade_chain_id.set(event_row.chain_id)
            depth_token = _cascade_depth.set(
                int(event_row.cascade_depth or 0))
            try:
                checkpoint = (
                    _control_checkpoint(db, event_row)
                    if event_kind in (
                        RELEASE_ACTIVATION,
                        SCHEDULED_SCAN,
                        DYNAMIC_ACTIVATION,
                        BUILTIN_ACTIVATION,
                    )
                    else None
                )
                control_payload = (
                    event_row.result_json.get("control", {})
                    if isinstance(event_row.result_json, dict)
                    and isinstance(
                        event_row.result_json.get("control"), dict)
                    else {}
                )
                if event_kind == RELEASE_ACTIVATION:
                    result = run_release_initialization(
                        db,
                        event_row.ontology_id,
                        event_id=event_row.id,
                        checkpoint=checkpoint,
                        retry=int(event_row.attempts or 0) > 1,
                    )
                elif event_kind == SCHEDULED_SCAN:
                    if not event_row.sentinel_id:
                        raise _LogicalDispatchError(
                            "定时扫描控制事件缺少 sentinel_id")
                    scheduled_origin = control_payload.get("sentinelOrigin")
                    stale_reason = None
                    if scheduled_origin == "assistant_dynamic":
                        stale_reason = _dynamic_control_stale_reason(
                            db, event_row, control_payload)
                    elif scheduled_origin == "release_builtin":
                        stale_reason = _builtin_control_stale_reason(
                            db, event_row, control_payload)
                        if stale_reason == "builtin_sentinel_inactive":
                            stale_reason = "scheduled_sentinel_inactive"
                    else:
                        legacy_live = db.query(Sentinel).filter(
                            Sentinel.id == event_row.sentinel_id,
                            Sentinel.ontology_id == event_row.ontology_id,
                        ).first()
                        if (
                            legacy_live is not None
                            and legacy_live.origin == "assistant_dynamic"
                        ):
                            stale_reason = (
                                "dynamic_schedule_identity_missing")
                    if stale_reason is not None:
                        result = _superseded_control_result(
                            event_row, stale_reason)
                    else:
                        result = run_scheduled_event(
                            db,
                            event_row.ontology_id,
                            str(event_row.sentinel_id),
                            event_id=event_row.id,
                            checkpoint=checkpoint,
                            retry=int(event_row.attempts or 0) > 1,
                        )
                elif event_kind == DYNAMIC_ACTIVATION:
                    stale_reason = _dynamic_control_stale_reason(
                        db, event_row, control_payload)
                    if stale_reason is not None:
                        result = _superseded_control_result(
                            event_row, stale_reason)
                    else:
                        result = run_dynamic_initialization(
                            db,
                            event_row.ontology_id,
                            str(event_row.sentinel_id),
                            event_id=event_row.id,
                            checkpoint=checkpoint,
                            retry=int(event_row.attempts or 0) > 1,
                        )
                elif event_kind == BUILTIN_ACTIVATION:
                    if not event_row.sentinel_id:
                        raise _LogicalDispatchError(
                            "内置哨兵初始化事件缺少 sentinel_id")
                    stale_reason = _builtin_control_stale_reason(
                        db,
                        event_row,
                        control_payload,
                        require_on_change=True,
                    )
                    if stale_reason is not None:
                        result = _superseded_control_result(
                            event_row, stale_reason)
                    else:
                        result = run_builtin_initialization(
                            db,
                            event_row.ontology_id,
                            str(event_row.sentinel_id),
                            event_id=event_row.id,
                            checkpoint=checkpoint,
                            retry=int(event_row.attempts or 0) > 1,
                        )
                elif event_kind == LINK_CHANGE or event_row.link_change:
                    result = run_for_link_change(
                        db, event_row.ontology_id)
                elif event_kind == OBJECT_CHANGE:
                    result = run_for_change(
                        db,
                        event_row.ontology_id,
                        str(event_row.object_type_id or ""),
                        list(event_row.changed_keys or []),
                    )
                else:
                    raise _LogicalDispatchError(
                        f"不支持的 Sentinel outbox 事件类型: {event_kind}")
                # Some test/delivery implementations commit immediately before
                # returning.  Ensure a final guarded transaction is active.
                _assert_event_release(
                    db, event_row.ontology_id, event_release_id)
            finally:
                _cascade_depth.reset(depth_token)
                _cascade_chain_id.reset(chain_token)
                event.remove(db, "after_begin", release_guard)
    except _ReleaseSuperseded as exc:
        db.rollback()
        return exc.as_result()

    nested_errors = _result_error_count(result)
    if nested_errors:
        raise _LogicalDispatchError(
            f"哨兵评估返回 {nested_errors} 个执行错误",
            result=result,
        )
    return result


def _finalize_success(
        db: Session, event_id: str, claim_token: str,
        result: dict) -> bool:
    row = db.query(SentinelCdcOutbox).filter(
        SentinelCdcOutbox.id == event_id,
        SentinelCdcOutbox.status == CDC_PROCESSING,
        SentinelCdcOutbox.claim_token == claim_token,
    ).first()
    if row is None:
        db.rollback()
        return False
    prior_payload = (
        dict(row.result_json)
        if isinstance(row.result_json, dict) else {}
    )
    if row.event_kind == SCHEDULED_SCAN:
        control = prior_payload.get("control")
        scheduled_at = (
            control.get("scheduledAt")
            if isinstance(control, dict) else None
        )
        if not isinstance(scheduled_at, str) or not scheduled_at:
            raise ValueError("定时扫描控制事件缺少 scheduledAt")
        completed_at = datetime.fromisoformat(scheduled_at)
        current_release_id = _locked_current_release_id(
            db, row.ontology_id)
        if (
            result.get("superseded") is not True
            and str(current_release_id or "") == str(
                row.ontology_release_id or "")
        ):
            watermark_query = db.query(Sentinel).filter(
                Sentinel.id == row.sentinel_id,
                Sentinel.ontology_id == row.ontology_id,
            )
            sentinel_origin = (
                control.get("sentinelOrigin")
                if isinstance(control, dict) else None
            )
            if sentinel_origin == "assistant_dynamic":
                expected_revision = _control_int(
                    control, "definitionRevision")
                expected_generation = _control_int(
                    control, "enableGeneration")
                if (
                    expected_revision is None
                    or expected_generation is None
                ):
                    watermark_query = watermark_query.filter(false())
                else:
                    watermark_query = watermark_query.filter(
                        Sentinel.origin == "assistant_dynamic",
                        Sentinel.status == "published",
                        Sentinel.enabled == True,  # noqa: E712
                        Sentinel.muted == False,  # noqa: E712
                        Sentinel.retired_at.is_(None),
                        Sentinel.bound_release_id
                        == row.ontology_release_id,
                        Sentinel.definition_revision == expected_revision,
                        Sentinel.enable_generation == expected_generation,
                    )
            elif sentinel_origin == "release_builtin":
                expected_generation = _control_int(
                    control, "enableGeneration")
                if expected_generation is None:
                    watermark_query = watermark_query.filter(false())
                else:
                    watermark_query = watermark_query.filter(
                        Sentinel.origin == "release_builtin",
                        Sentinel.status == "published",
                        Sentinel.enabled == True,  # noqa: E712
                        Sentinel.muted == False,  # noqa: E712
                        Sentinel.retired_at.is_(None),
                        Sentinel.enable_generation == expected_generation,
                    )
            else:
                # Compatibility for schedule events written before control
                # identity freezing was introduced.
                watermark_query = watermark_query.filter(
                    or_(
                        Sentinel.origin == "release_builtin",
                        and_(
                            Sentinel.origin == "assistant_dynamic",
                            Sentinel.bound_release_id
                            == row.ontology_release_id,
                        ),
                    ),
                )
            watermark_query.update(
                {Sentinel.last_scanned_at: completed_at},
                synchronize_session=False,
            )
    row.status = "completed"
    row.result_json = dict(result)
    if (
        row.event_kind in (
            RELEASE_ACTIVATION, SCHEDULED_SCAN, DYNAMIC_ACTIVATION,
            BUILTIN_ACTIVATION,
        )
        and isinstance(prior_payload.get("control"), dict)
    ):
        row.result_json["control"] = prior_payload["control"]
    row.last_error = None
    row.processed_at = _now()
    row.claimed_at = None
    row.claim_token = None
    db.commit()
    return True


def _finalize_failure(
        db: Session, event_id: str, claim_token: str,
        error: BaseException) -> str:
    db.rollback()
    row = db.query(SentinelCdcOutbox).filter(
        SentinelCdcOutbox.id == event_id,
        SentinelCdcOutbox.status == CDC_PROCESSING,
        SentinelCdcOutbox.claim_token == claim_token,
    ).first()
    if row is None:
        return "lost_claim"
    result = getattr(error, "result", None)
    if result is not None:
        if row.event_kind in (
            RELEASE_ACTIVATION, SCHEDULED_SCAN, DYNAMIC_ACTIVATION,
            BUILTIN_ACTIVATION,
        ):
            payload = (
                dict(row.result_json)
                if isinstance(row.result_json, dict) else {}
            )
            payload["lastAttempt"] = result
            row.result_json = payload
        else:
            row.result_json = result
    row.last_error = str(error)[:4000]
    row.claimed_at = None
    row.claim_token = None
    terminal = (
        int(row.attempts or 0) >= _DISPATCH_MAX_ATTEMPTS
        or int(row.cascade_depth or 0) > _MAX_CASCADE_DEPTH
    )
    if terminal:
        row.status = CDC_DEAD
        row.processed_at = _now()
    else:
        row.status = CDC_RETRY
        delay = min(
            300,
            2 ** min(max(int(row.attempts or 1) - 1, 0), 8),
        )
        row.available_at = _now() + timedelta(seconds=delay)
    db.commit()
    return _public_outbox_status(row.status)


def _candidate_ids(
        db: Session, *, event_ids: set[str] | None,
        chain_ids: set[str] | None, limit: int) -> list[str]:
    now = _now()
    stale_before = now - timedelta(seconds=_CLAIM_TIMEOUT_SECONDS)
    query = db.query(SentinelCdcOutbox.id).filter(
        _eligible_filter(now, stale_before))
    if event_ids is not None:
        if not event_ids:
            return []
        query = query.filter(SentinelCdcOutbox.id.in_(event_ids))
    if chain_ids is not None:
        if not chain_ids:
            return []
        query = query.filter(SentinelCdcOutbox.chain_id.in_(chain_ids))
    return [
        row[0] for row in query.order_by(
            SentinelCdcOutbox.created_at.asc(),
            SentinelCdcOutbox.id.asc(),
        ).limit(max(1, limit)).all()
    ]


def drain_cdc_outbox(
        *, event_ids: set[str] | None = None,
        chain_ids: set[str] | None = None,
        limit: int = 50, session_factory=None) -> dict:
    """Claim and process a bounded durable batch.

    Concurrent processes may call this safely.  A claim token plus conditional
    update is the SQLite fallback; PostgreSQL additionally skips locked rows.
    """
    if session_factory is None:
        from app.database import SessionLocal
        session_factory = SessionLocal

    result = {
        "processed": 0,
        "stale": 0,
        "retried": 0,
        "dead": 0,
        "lostClaims": 0,
        "errors": [],
        "eventIds": [],
    }
    lookup = session_factory()
    try:
        candidates = _candidate_ids(
            lookup, event_ids=event_ids,
            chain_ids=chain_ids, limit=limit)
    except Exception as exc:  # noqa: BLE001
        lookup.rollback()
        result["errors"].append({
            "stage": "read_outbox", "error": str(exc)})
        return result
    finally:
        lookup.close()

    for event_id in candidates:
        db = session_factory()
        try:
            now = _now()
            stale_before = now - timedelta(
                seconds=_CLAIM_TIMEOUT_SECONDS)
            claim_token = _claim_one(
                db, event_id, now, stale_before)
            if claim_token is None:
                result["lostClaims"] += 1
                continue
            try:
                dispatch_result = _execute_claimed_event(
                    db, event_id, claim_token)
                if _finalize_success(
                        db, event_id, claim_token, dispatch_result):
                    result["processed"] += 1
                    if dispatch_result.get("superseded") is True:
                        result["stale"] += 1
                    result["eventIds"].append(event_id)
                else:
                    result["lostClaims"] += 1
            except Exception as exc:  # noqa: BLE001
                status = _finalize_failure(
                    db, event_id, claim_token, exc)
                if status == "dead":
                    result["dead"] += 1
                elif status == "retry":
                    result["retried"] += 1
                else:
                    result["lostClaims"] += 1
                result["errors"].append({
                    "eventId": event_id,
                    "status": status,
                    "error": str(exc),
                    "result": getattr(exc, "result", None),
                })
                logger.exception(
                    "Sentinel CDC outbox 执行失败: %s", event_id)
        finally:
            db.close()
    return result


def recover_held_outbox(
        *, limit: int = 100, session_factory=None) -> dict:
    """Release mapping events stranded between ``applied`` and activation.

    A process can terminate after the mapping status commit but before
    ``dispatch_captured_changes`` runs.  The durable row records the exact
    mapping ids that owned the projection, so restart recovery can distinguish
    a safe-to-run event from one whose projection is still applying/failed.
    """
    if session_factory is None:
        from app.database import SessionLocal
        session_factory = SessionLocal

    global _last_held_recovery_error
    result = {"examined": 0, "activated": 0, "waiting": 0, "errors": []}
    db = session_factory()
    try:
        now = _now()
        rows = db.query(SentinelCdcOutbox).filter(
            SentinelCdcOutbox.status.in_(_HELD_STATUSES),
            SentinelCdcOutbox.available_at <= now,
        ).order_by(
            SentinelCdcOutbox.available_at.asc(),
            SentinelCdcOutbox.created_at.asc(),
            SentinelCdcOutbox.id.asc(),
        ).limit(max(1, limit)).all()
        if not rows:
            _last_held_recovery_error = None
            return result

        from app.ontologies.mappings.models import OntologyMapping

        for row in rows:
            result["examined"] += 1
            mapping_ids = {
                str(item) for item in (row.mapping_ids or []) if item
            }
            mapping_query = db.query(
                OntologyMapping.id, OntologyMapping.status,
            ).filter(OntologyMapping.ontology_id == row.ontology_id)
            if mapping_ids:
                mapping_query = mapping_query.filter(
                    OntologyMapping.id.in_(mapping_ids))
            statuses = {
                mapping_id: status
                for mapping_id, status in mapping_query.all()
            }
            expected = mapping_ids or set(statuses)
            ready = bool(expected) and set(statuses) == expected and all(
                status == "applied" for status in statuses.values())
            if ready:
                row.status = CDC_PENDING
                row.available_at = now
                row.last_error = None
                row.updated_at = now
                result["activated"] += 1
                continue
            missing = sorted(expected - set(statuses))
            detail = ", ".join(
                f"{mapping_id}={statuses.get(mapping_id, 'missing')}"
                for mapping_id in sorted(expected)
            ) or "no_mapping_fence"
            waiting_error = (
                "held_until_mapping_applied: "
                f"{detail}"
                + (f"; missing={','.join(missing)}" if missing else "")
            )[:4000]
            if row.last_error != waiting_error:
                row.last_error = waiting_error
            # Move blocked rows behind still-unexamined rows and avoid rewriting
            # the same waiting diagnostic every worker tick.
            row.available_at = now + timedelta(
                seconds=_HELD_RECOVERY_RECHECK_SECONDS)
            result["waiting"] += 1
        db.commit()
        _last_held_recovery_error = None
        return result
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        message = str(exc)
        result["errors"].append(message)
        if message != _last_held_recovery_error:
            logger.warning(
                "恢复 held Sentinel CDC outbox 失败（相同错误后续不重复刷屏）: %s",
                message,
            )
        _last_held_recovery_error = message
        return result
    finally:
        db.close()


def prune_completed_outbox(session_factory=None) -> int:
    """Bound completed-event retention without touching fresh chain barriers."""
    if session_factory is None:
        from app.database import SessionLocal
        session_factory = SessionLocal
    db = session_factory()
    deleted = 0
    try:
        now = _now()
        cutoff = now - timedelta(hours=_COMPLETED_RETENTION_HOURS)
        deleted += db.query(SentinelCdcOutbox).filter(
            SentinelCdcOutbox.status == "completed",
            SentinelCdcOutbox.processed_at < cutoff,
        ).delete(synchronize_session=False)
        db.commit()

        completed_count = db.query(func.count(
            SentinelCdcOutbox.id)).filter(
                SentinelCdcOutbox.status == "completed").scalar() or 0
        overflow = max(
            0, int(completed_count) - _COMPLETED_RETAIN_LIMIT)
        if overflow:
            # Keep at least one hour of fresh rows so a mapping barrier can
            # always assemble its result even on a very busy installation.
            protected_after = now - timedelta(hours=1)
            old_ids = [
                row[0] for row in db.query(
                    SentinelCdcOutbox.id,
                ).filter(
                    SentinelCdcOutbox.status == "completed",
                    SentinelCdcOutbox.processed_at < protected_after,
                ).order_by(
                    SentinelCdcOutbox.processed_at.asc(),
                    SentinelCdcOutbox.id.asc(),
                ).limit(overflow).all()
            ]
            if old_ids:
                deleted += db.query(SentinelCdcOutbox).filter(
                    SentinelCdcOutbox.id.in_(old_ids),
                ).delete(synchronize_session=False)
                db.commit()
        return int(deleted)
    except Exception:
        db.rollback()
        logger.exception("清理 Sentinel CDC completed outbox 失败")
        return int(deleted)
    finally:
        db.close()


def _dispatch_loop() -> None:
    global _last_dispatch_error, _last_prune_monotonic
    while not _dispatch_stop_event.is_set():
        event_id: str | None = None
        try:
            try:
                event_id = _dispatch_queue.get(timeout=1.0)
            except queue.Empty:
                pass
            if _dispatch_stop_event.is_set() and event_id is None:
                break
            recover_held_outbox(limit=100)
            batch = drain_cdc_outbox(
                event_ids={event_id} if event_id else None,
                limit=1 if event_id else 20,
            )
            if batch["errors"]:
                _last_dispatch_error = str(batch["errors"][-1]["error"])
            elif batch["processed"]:
                _last_dispatch_error = None
            current = time.monotonic()
            if current - _last_prune_monotonic >= 60:
                prune_completed_outbox()
                event_store.prune_event_log()
                _last_prune_monotonic = current
        except Exception as exc:  # noqa: BLE001
            _last_dispatch_error = str(exc)
            logger.exception("Sentinel CDC durable worker 失败")
            time.sleep(1)
        finally:
            if event_id is not None:
                _dispatch_queue.task_done()


def _ensure_dispatch_worker() -> None:
    global _dispatch_worker
    if not AUTO_DISPATCH:
        return
    if _dispatch_worker is not None and _dispatch_worker.is_alive():
        return
    with _dispatch_worker_lock:
        if _dispatch_worker is not None and _dispatch_worker.is_alive():
            return
        _dispatch_stop_event.clear()
        _dispatch_worker = threading.Thread(
            target=_dispatch_loop,
            daemon=True,
            name="sentinel-cdc-outbox-worker",
        )
        _dispatch_worker.start()


def stop_cdc_worker(*, timeout: float = 5.0) -> bool:
    """Stop the API-owned recovery daemon during application shutdown.

    Listener registration remains process-global and idempotent.  Only the
    background consumer is stopped, so a later lifespan can safely restart it
    without duplicating SQLAlchemy event listeners.
    """
    global _dispatch_worker, _background_worker_enabled
    _background_worker_enabled = False
    _dispatch_stop_event.set()
    worker = _dispatch_worker
    if worker is None:
        return True
    if worker is threading.current_thread():
        return False
    worker.join(timeout=max(0.0, timeout))
    stopped = not worker.is_alive()
    if stopped:
        with _dispatch_worker_lock:
            if _dispatch_worker is worker:
                _dispatch_worker = None
    return stopped


def _enqueue_dispatch(event_ids: list[str] | set[str]) -> None:
    """Best-effort accelerator; a full queue never loses the durable event."""
    # Listener-only processes (notably Mapping Celery workers) own a
    # synchronous causal-chain barrier and must not create a competing daemon
    # consumer from ``after_commit``.  The row is durable, so the API process's
    # recovery worker can still repair it after a task crash.
    if not _background_worker_enabled:
        return
    _ensure_dispatch_worker()
    for event_id in sorted(set(event_ids)):
        try:
            _dispatch_queue.put_nowait(event_id)
        except queue.Full:
            logger.error(
                "Sentinel CDC 加速队列已满（%s）；事件 %s 将由数据库恢复扫描处理",
                _DISPATCH_QUEUE_SIZE, event_id)
            break


def _datetime_token(value: datetime | None) -> str:
    if value is None:
        return "never"
    normalized = (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None else value.astimezone(timezone.utc)
    )
    return normalized.isoformat()


def ensure_scheduled_scan_event(
        db: Session, *, ontology_id: str, ontology_release_id: str,
        sentinel_id: str, previous_last_scanned_at: datetime | None,
        scheduled_at: datetime, sentinel_origin: str = "release_builtin",
        definition_revision: int | None = None,
        enable_generation: int | None = None) -> str | None:
    """Durably claim one due schedule without advancing its success watermark.

    The previous successful watermark is part of the unique key. Concurrent
    scanners therefore converge on one outbox row, while a successfully
    completed cycle advances ``last_scanned_at`` and naturally receives a new
    key when the next interval becomes due.
    """
    origin = str(sentinel_origin or "")
    revision = (
        int(definition_revision)
        if definition_revision is not None else None
    )
    generation = (
        int(enable_generation)
        if enable_generation is not None else None
    )
    if origin == "assistant_dynamic" and (
        revision is None or generation is None
    ):
        db.rollback()
        return None
    if origin == "release_builtin" and generation is None:
        # Compatibility callers that do not yet provide a built-in generation
        # can only create a generation-zero event.
        generation = 0

    previous_token = _datetime_token(previous_last_scanned_at)
    dedupe_key = _control_dedupe_key(
        SCHEDULED_SCAN,
        ontology_id,
        ontology_release_id,
        sentinel_id,
        previous_token,
        origin,
        revision,
        generation,
    )
    row = _control_outbox_row(
        db,
        kind=SCHEDULED_SCAN,
        ontology_id=ontology_id,
        ontology_release_id=ontology_release_id,
        sentinel_id=sentinel_id,
        dedupe_key=dedupe_key,
        suppress_acceleration=True,
        control={
            "sentinelId": sentinel_id,
            "previousLastScannedAt": (
                None if previous_last_scanned_at is None
                else previous_token
            ),
            "scheduledAt": _datetime_token(scheduled_at),
            "sentinelOrigin": origin,
            "definitionRevision": revision,
            "enableGeneration": generation,
        },
    )
    if row is None:
        db.rollback()
        return None
    try:
        db.commit()
        return str(row.id)
    except IntegrityError:
        # Another scheduler committed the same due cycle first. The uniqueness
        # conflict rolls back only this read/claim transaction.
        db.rollback()
        winner = db.query(SentinelCdcOutbox.id).filter(
            SentinelCdcOutbox.dedupe_key == dedupe_key,
        ).first()
        return str(winner[0]) if winner is not None else None


def capture_dynamic_activation(
        db: Session, *, ontology_id: str, ontology_release_id: str,
        sentinel_id: str, definition_revision: int,
        enable_generation: int) -> SentinelCdcOutbox | None:
    """Attach a dynamic-Sentinel initialization to its enabling transaction."""
    revision = int(definition_revision)
    generation = int(enable_generation)
    return _control_outbox_row(
        db,
        kind=DYNAMIC_ACTIVATION,
        ontology_id=ontology_id,
        ontology_release_id=ontology_release_id,
        sentinel_id=sentinel_id,
        dedupe_key=_control_dedupe_key(
            DYNAMIC_ACTIVATION,
            ontology_id,
            ontology_release_id,
            sentinel_id,
            revision,
            generation,
        ),
        control={
            "sentinelId": sentinel_id,
            "definitionRevision": revision,
            "enableGeneration": generation,
        },
    )


def capture_builtin_activation(
        db: Session, *, ontology_id: str, ontology_release_id: str,
        sentinel_id: str, enable_generation: int,
) -> SentinelCdcOutbox | None:
    """Attach one exact built-in reactivation to its management transaction."""
    generation = int(enable_generation)
    return _control_outbox_row(
        db,
        kind=BUILTIN_ACTIVATION,
        ontology_id=ontology_id,
        ontology_release_id=ontology_release_id,
        sentinel_id=sentinel_id,
        dedupe_key=_control_dedupe_key(
            BUILTIN_ACTIVATION,
            ontology_id,
            ontology_release_id,
            sentinel_id,
            generation,
        ),
        control={
            "sentinelId": sentinel_id,
            "enableGeneration": generation,
        },
        # This is a management event, independent of editor-save CDC markers.
        ignore_session_suppression=True,
    )


def cdc_dispatch_status(
        ontology_id: str | None = None, *,
        ontology_release_id: str | None = None,
        include_history: bool = False,
        session_factory=None) -> dict:
    """Return queue plus durable retry/dead-letter state.

    The default view is release-safe: for one ontology it includes only rows
    belonging to its current release; globally it joins every row to its
    owning project's current pointer.  Authenticated callers may explicitly
    request ``include_history`` (or one exact historical release id) without
    allowing old dead letters to poison current-release health.
    """
    if session_factory is None:
        from app.database import SessionLocal
        session_factory = SessionLocal
    durable: dict[str, int] = {
        status: 0 for status in (
            "held", "pending", "processing", "retry", "completed", "dead")
    }
    last_errors: list[dict] = []
    recent_events: list[dict] = []

    def control_for(row: SentinelCdcOutbox) -> dict:
        payload = row.result_json if isinstance(row.result_json, dict) else {}
        control = payload.get("control")
        return control if isinstance(control, dict) else {}

    selected_release_id = (
        str(ontology_release_id) if ontology_release_id else None
    )
    db = session_factory()
    try:
        current_release_exists = True
        if (
            ontology_id
            and selected_release_id is None
            and not include_history
        ):
            current_row = db.execute(select(
                OntologyProject.current_release_id,
            ).where(
                OntologyProject.id == ontology_id,
            )).first()
            current_release_exists = bool(
                current_row is not None and current_row[0])
            selected_release_id = (
                str(current_row[0])
                if current_release_exists else None
            )

        def scoped(query):
            if ontology_id:
                query = query.filter(
                    SentinelCdcOutbox.ontology_id == ontology_id)
            if selected_release_id is not None:
                return query.filter(
                    SentinelCdcOutbox.ontology_release_id
                    == selected_release_id)
            if include_history:
                return query
            if ontology_id:
                # A project without a valid current pointer has no current
                # release CDC scope; legacy NULL events are history only.
                return query.filter(false())
            return query.join(
                OntologyProject,
                and_(
                    OntologyProject.id
                    == SentinelCdcOutbox.ontology_id,
                    OntologyProject.current_release_id
                    == SentinelCdcOutbox.ontology_release_id,
                ),
            )

        counts = db.query(
            SentinelCdcOutbox.status,
            func.count(SentinelCdcOutbox.id),
        )
        counts = scoped(counts)
        for status, count in counts.group_by(
                SentinelCdcOutbox.status).all():
            public_status = _public_outbox_status(status)
            durable[public_status] = (
                int(durable.get(public_status, 0)) + int(count)
            )
        error_query = db.query(SentinelCdcOutbox).filter(
            SentinelCdcOutbox.last_error.is_not(None),
        )
        error_query = scoped(error_query)
        rows = error_query.order_by(
            SentinelCdcOutbox.updated_at.desc(),
            SentinelCdcOutbox.id.asc(),
        ).limit(20).all()
        last_errors = [{
            "eventId": row.id,
            "chainId": row.chain_id,
            "ontologyId": row.ontology_id,
            "ontologyReleaseId": row.ontology_release_id,
            "eventKind": row.event_kind,
            "sentinelId": row.sentinel_id,
            "definitionRevision": control_for(row).get(
                "definitionRevision"),
            "enableGeneration": control_for(row).get("enableGeneration"),
            "status": _public_outbox_status(row.status),
            "cascadeDepth": row.cascade_depth,
            "attempts": row.attempts,
            "error": row.last_error,
            "availableAt": (
                row.available_at.isoformat() if row.available_at else None),
            "claimedAt": (
                row.claimed_at.isoformat() if row.claimed_at else None),
            "updatedAt": (
                row.updated_at.isoformat() if row.updated_at else None),
        } for row in rows]
        if include_history:
            history_rows = scoped(
                db.query(SentinelCdcOutbox),
            ).order_by(
                SentinelCdcOutbox.updated_at.desc(),
                SentinelCdcOutbox.id.asc(),
            ).limit(100).all()
            recent_events = [{
                "eventId": row.id,
                "chainId": row.chain_id,
                "ontologyId": row.ontology_id,
                "ontologyReleaseId": row.ontology_release_id,
                "eventKind": row.event_kind,
                "sentinelId": row.sentinel_id,
                "definitionRevision": control_for(row).get(
                    "definitionRevision"),
                "enableGeneration": control_for(row).get(
                    "enableGeneration"),
                "status": _public_outbox_status(row.status),
                "objectTypeId": row.object_type_id,
                "linkChange": bool(row.link_change),
                "cascadeDepth": row.cascade_depth,
                "attempts": row.attempts,
                "error": row.last_error,
                "result": row.result_json,
                "updatedAt": (
                    row.updated_at.isoformat() if row.updated_at else None
                ),
                "createdAt": (
                    row.created_at.isoformat() if row.created_at else None
                ),
                "availableAt": (
                    row.available_at.isoformat()
                    if row.available_at else None
                ),
                "claimedAt": (
                    row.claimed_at.isoformat() if row.claimed_at else None
                ),
                "processedAt": (
                    row.processed_at.isoformat() if row.processed_at else None
                ),
            } for row in history_rows]
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        durable = {"schema_error": 1}
        last_errors = [{"error": str(exc)}]
    finally:
        db.close()
    active = sum(int(durable.get(status, 0)) for status in (
        "held", "pending", "processing", "retry"))
    dead = int(durable.get("dead", 0))
    retrying = int(durable.get("retry", 0))
    worker_alive = bool(
        _dispatch_worker is not None
        and _dispatch_worker.is_alive())
    return {
        "ontology_id": ontology_id,
        "ontology_release_id": selected_release_id,
        "scope": (
            "history" if include_history
            else "release" if ontology_release_id
            else "current_release"
        ),
        "healthy": (
            dead == 0
            and retrying == 0
            and "schema_error" not in durable
            and (not AUTO_DISPATCH or worker_alive)
        ),
        "quiescent": active == 0,
        "worker_alive": worker_alive,
        "queued": _dispatch_queue.qsize(),
        "max_queue_size": _DISPATCH_QUEUE_SIZE,
        "max_cascade_depth": _MAX_CASCADE_DEPTH,
        "claim_timeout_seconds": _CLAIM_TIMEOUT_SECONDS,
        "last_error": _last_dispatch_error,
        "durable": durable,
        "last_errors": last_errors,
        "recent_events": recent_events,
        "dead_letters": [
            item for item in last_errors if item.get("status") == "dead"],
        # 0051 intentionally does not infer/replay activation events for
        # releases that predate the durable control-event architecture: doing
        # so could replay arbitrary external actions. Operators can observe
        # this policy here and deliberately create a new release activation (or
        # run a reviewed manual evaluation) instead.
        "migration_policy": {
            "legacyActivationBackfill": "not_replayed",
            "reason": "avoid_unreviewed_historical_side_effect_replay",
            "operatorRecovery": (
                "确认当前发布数据与动作安全后，创建新的发布/回滚激活；"
                "或执行一次受审计的手动哨兵评估"
            ),
        },
    }


def _after_commit(session: Session) -> None:
    changes = session.info.pop(_KEY, None)
    link_changes = session.info.pop(_LINK_KEY, None)
    session.info.pop(_OUTBOX_ROWS_KEY, None)
    session.info.pop(_CONTROL_ROWS_KEY, None)
    session.info.pop(_RELEASE_SWITCH_SCOPES_KEY, None)
    event_store.discard_pending(session)
    outbox_ids = set(session.info.pop(_OUTBOX_IDS_KEY, set()))
    synchronous_ids = set(session.info.pop(
        "_sentinel_synchronous_control_ids", set()))
    if not changes and not link_changes and not outbox_ids:
        return
    if session.info.get(SUPPRESS_KEY):
        if session.info.get(CAPTURE_SUPPRESSED_KEY):
            captured = session.info.setdefault(_CAPTURED_KEY, {})
            for key, changed_keys in (changes or {}).items():
                captured.setdefault(key, set()).update(changed_keys)
            session.info.setdefault(_CAPTURED_LINK_KEY, set()).update(
                link_changes or set())
            session.info.setdefault(_CAPTURED_OUTBOX_KEY, set()).update(
                outbox_ids)
        return
    if not in_sentinel_run.get():
        session.info.pop(_CHAIN_KEY, None)
    accelerated_ids = outbox_ids - synchronous_ids
    if accelerated_ids and not _synchronous_chain_barrier.get():
        _enqueue_dispatch(accelerated_ids)


def _after_rollback(session: Session) -> None:
    """Never dispatch deltas whose business transaction rolled back."""
    session.info.pop(_KEY, None)
    session.info.pop(_LINK_KEY, None)
    session.info.pop(_OUTBOX_ROWS_KEY, None)
    session.info.pop(_CONTROL_ROWS_KEY, None)
    session.info.pop(_RELEASE_SWITCH_SCOPES_KEY, None)
    session.info.pop(_OUTBOX_IDS_KEY, None)
    session.info.pop("_sentinel_synchronous_control_ids", None)
    event_store.discard_pending(session)
    if not session.info.get(CAPTURE_SUPPRESSED_KEY):
        session.info.pop(_CHAIN_KEY, None)


def _chain_rows(
        chain_ids: set[str], session_factory=None,
) -> list[SentinelCdcOutbox]:
    if session_factory is None:
        from app.database import SessionLocal
        session_factory = SessionLocal
    db = session_factory()
    try:
        return db.query(SentinelCdcOutbox).filter(
            SentinelCdcOutbox.chain_id.in_(chain_ids),
        ).order_by(
            SentinelCdcOutbox.created_at.asc(),
            SentinelCdcOutbox.id.asc(),
        ).all()
    finally:
        db.close()


def _drain_chain_barrier(
        chain_ids: set[str], session_factory=None) -> dict:
    """Wait for one causal chain without joining unrelated queue work."""
    deadline = time.monotonic() + _BARRIER_TIMEOUT_SECONDS
    errors: list[dict] = []
    barrier_token = _synchronous_chain_barrier.set(True)
    try:
        while True:
            batch = drain_cdc_outbox(
                chain_ids=chain_ids, limit=100,
                session_factory=session_factory)
            if batch["errors"]:
                errors.extend(batch["errors"])
                break
            rows = _chain_rows(
                chain_ids, session_factory=session_factory)
            failed = [
                row for row in rows
                if row.status in _FAILED_STATUSES
            ]
            if failed:
                errors.extend([{
                    "eventId": row.id,
                    "status": _public_outbox_status(row.status),
                    "error": row.last_error or "CDC outbox 执行失败",
                    "result": row.result_json,
                } for row in failed])
                break
            incomplete = [
                row for row in rows
                if row.status != "completed"
            ]
            if not incomplete:
                return {
                    "completed": True,
                    "errors": [],
                    "rows": rows,
                }
            if time.monotonic() >= deadline:
                errors.append({
                    "chainIds": sorted(chain_ids),
                    "status": "timeout",
                    "error": (
                        f"Sentinel 下游级联在 {_BARRIER_TIMEOUT_SECONDS}s "
                        "内未完成"),
                    "pendingEventIds": [row.id for row in incomplete],
                })
                break
            # Another process may own a non-stale claim.  This short,
            # chain-local poll cannot deadlock on unrelated global events.
            time.sleep(0.05)
        return {
            "completed": False,
            "errors": errors,
            "rows": _chain_rows(
                chain_ids, session_factory=session_factory),
        }
    finally:
        _synchronous_chain_barrier.reset(barrier_token)


def _summary_from_outbox(rows: list[SentinelCdcOutbox]) -> dict:
    summary = {
        "evaluated": 0,
        "fired": 0,
        "errors": [],
        "runs": [],
    }
    for row in rows:
        result = row.result_json if isinstance(row.result_json, dict) else {}
        if result:
            summary["evaluated"] += int(result.get("evaluated", 0))
            summary["fired"] += int(result.get("fired", 0))
        run = {
            "event_id": row.id,
            "chain_id": row.chain_id,
            "ontology_id": row.ontology_id,
            "cascade_depth": row.cascade_depth,
            "status": _public_outbox_status(row.status),
            "result": result,
        }
        if row.link_change:
            run["link_change"] = True
        else:
            run["object_type_id"] = row.object_type_id
            run["changed_keys"] = list(row.changed_keys or [])
        summary["runs"].append(run)
        nested_errors = _result_error_count(result)
        if nested_errors:
            summary["errors"].append({
                "event_id": row.id,
                "ontology_id": row.ontology_id,
                "error": f"哨兵评估返回 {nested_errors} 个执行错误",
                "firings": result.get("firings", []),
            })
        if row.status in _FAILED_STATUSES:
            summary["errors"].append({
                "event_id": row.id,
                "ontology_id": row.ontology_id,
                "status": _public_outbox_status(row.status),
                "error": row.last_error or "CDC outbox 执行失败",
            })
    return summary


def _legacy_dispatch(
        changes: dict, link_changes: set) -> dict:
    """Compatibility for pre-migration/tests that only populated session.info."""
    from app.database import SessionLocal
    from app.ontologies.sentinels.engine import (
        run_for_change, run_for_link_change,
    )

    summary = {"evaluated": 0, "fired": 0, "errors": [], "runs": []}
    units = [
        (ontology_id, object_type_id, sorted(keys), False)
        for (ontology_id, object_type_id), keys in sorted(changes.items())
    ]
    units.extend([
        (ontology_id, None, [], True)
        for ontology_id in sorted(link_changes)
    ])
    for ontology_id, object_type_id, keys, link_change in units:
        db = SessionLocal()
        try:
            result = (
                run_for_link_change(db, ontology_id)
                if link_change
                else run_for_change(
                    db, ontology_id, str(object_type_id), keys)
            )
            summary["evaluated"] += int(result.get("evaluated", 0))
            summary["fired"] += int(result.get("fired", 0))
            run = {
                "ontology_id": ontology_id,
                "result": result,
            }
            if link_change:
                run["link_change"] = True
            else:
                run["object_type_id"] = object_type_id
                run["changed_keys"] = keys
            summary["runs"].append(run)
            nested_errors = _result_error_count(result)
            if nested_errors:
                summary["errors"].append({
                    "ontology_id": ontology_id,
                    "object_type_id": object_type_id,
                    "link_change": link_change,
                    "error": f"哨兵评估返回 {nested_errors} 个执行错误",
                    "firings": result.get("firings", []),
                })
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            summary["errors"].append({
                "ontology_id": ontology_id,
                "object_type_id": object_type_id,
                "link_change": link_change,
                "error": str(exc),
            })
        finally:
            db.close()
    return summary


def _activate_held_events(
        event_ids: set[str], *, session_factory=None) -> int:
    """Release only this applied mapping's durable root events."""
    if not event_ids:
        return 0
    if session_factory is None:
        from app.database import SessionLocal
        session_factory = SessionLocal
    db = session_factory()
    try:
        changed = db.query(SentinelCdcOutbox).filter(
            SentinelCdcOutbox.id.in_(event_ids),
            SentinelCdcOutbox.status.in_(_HELD_STATUSES),
        ).update({
            SentinelCdcOutbox.status: CDC_PENDING,
            SentinelCdcOutbox.available_at: _now(),
            SentinelCdcOutbox.updated_at: _now(),
        }, synchronize_session=False)
        db.commit()
        return int(changed)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def dispatch_captured_changes(session: Session) -> dict:
    """Synchronously dispatch a mapping delta and its complete causal cascade."""
    changes = session.info.pop(_CAPTURED_KEY, {})
    link_changes = session.info.pop(_CAPTURED_LINK_KEY, set())
    outbox_ids = set(session.info.pop(_CAPTURED_OUTBOX_KEY, set()))
    session.info.pop(CAPTURE_SUPPRESSED_KEY, None)
    session.info.pop(SUPPRESS_KEY, None)
    session.info.pop(_CHAIN_KEY, None)
    session.info.pop(MAPPING_SCOPE_KEY, None)
    if not AUTO_DISPATCH:
        return {"evaluated": 0, "fired": 0, "errors": []}
    if not outbox_ids:
        if not changes and not link_changes:
            return {"evaluated": 0, "fired": 0, "errors": []}
        return _legacy_dispatch(changes, link_changes)

    # Always open a separate Session on the caller's exact bind.  Reusing the
    # current Session would let claim commits interfere with the caller, while
    # the global SessionLocal can point at a different database in embedded
    # deployments and tests.
    session_factory = sessionmaker(
        bind=session.get_bind(), expire_on_commit=False)

    try:
        _activate_held_events(
            outbox_ids, session_factory=session_factory)
    except Exception as exc:  # noqa: BLE001
        return {
            "evaluated": 0,
            "fired": 0,
            "errors": [{
                "error": f"Mapping CDC outbox 激活失败: {exc}",
                "eventIds": sorted(outbox_ids),
            }],
            "runs": [],
            "barrierCompleted": False,
        }

    lookup = session_factory()
    try:
        chain_ids = {
            row[0] for row in lookup.query(
                SentinelCdcOutbox.chain_id,
            ).filter(
                SentinelCdcOutbox.id.in_(outbox_ids),
            ).distinct().all()
        }
    finally:
        lookup.close()
    if not chain_ids:
        return {
            "evaluated": 0,
            "fired": 0,
            "errors": [{
                "error": "Mapping CDC outbox 根事件不存在",
                "eventIds": sorted(outbox_ids),
            }],
            "runs": [],
        }

    barrier = _drain_chain_barrier(
        chain_ids, session_factory=session_factory)
    summary = _summary_from_outbox(barrier["rows"])
    if barrier["errors"]:
        summary["errors"].extend(barrier["errors"])
    summary["chainIds"] = sorted(chain_ids)
    summary["barrierCompleted"] = bool(barrier["completed"])
    return summary


def discard_captured_changes(session: Session) -> None:
    """Cancel unprocessed mapping events when projection reconciliation fails."""
    outbox_ids = set(session.info.pop(_CAPTURED_OUTBOX_KEY, set()))
    if outbox_ids:
        session.query(SentinelCdcOutbox).filter(
            SentinelCdcOutbox.id.in_(outbox_ids),
            SentinelCdcOutbox.status.in_(_DISCARDABLE_STATUSES),
        ).delete(synchronize_session=False)
    session.info.pop(_CAPTURED_KEY, None)
    session.info.pop(_CAPTURED_LINK_KEY, None)
    session.info.pop(_OUTBOX_ROWS_KEY, None)
    session.info.pop(_RELEASE_SWITCH_SCOPES_KEY, None)
    session.info.pop(_OUTBOX_IDS_KEY, None)
    session.info.pop(_CHAIN_KEY, None)
    session.info.pop(CAPTURE_SUPPRESSED_KEY, None)
    session.info.pop(SUPPRESS_KEY, None)
    session.info.pop(MAPPING_SCOPE_KEY, None)
    event_store.discard_pending(session)


_REGISTERED = False


def register_cdc(*, start_worker: bool = False) -> None:
    """Register CDC listeners and optionally start the recovery worker.

    Listener registration is safe in any process.  Starting the durable
    recovery worker is an explicit API-process responsibility: a Mapping
    Executor task owns a synchronous chain barrier for the changes it just
    projected, and a second consumer in that same process would race the task
    for its own event.
    """
    global _REGISTERED, _background_worker_enabled
    if not _REGISTERED:
        event.listen(Session, "before_flush", _before_flush)
        event.listen(Session, "after_commit", _after_commit)
        event.listen(Session, "after_rollback", _after_rollback)
        _REGISTERED = True
    # Registration also performs crash/restart recovery even when this process
    # has not yet observed a new commit.
    if start_worker:
        _background_worker_enabled = True
        _ensure_dispatch_worker()
