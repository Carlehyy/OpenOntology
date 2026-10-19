#!/usr/bin/env python3
"""Action-confirmation durable concurrency acceptance on real PostgreSQL.

Exercises the YIYA-36 confirmation ledger's crash-safety guarantees against a
real PostgreSQL instead of sequential SQLite replay:

- persistent prepare idempotency under concurrent inserts
  (``uq_action_confirmation_prepare_request``);
- concurrent operation claim: exactly one winner via ``SELECT ... FOR UPDATE``
  + state gate, losers observe ``executing`` and back off;
- guard partial unique index on PG: identical active payload is rejected,
  guard is released when the operation reaches a terminal state;
- lease-expiry recovery adjudication: a local engine success/failed log with
  an unresolved external attempt must converge to ``unknown`` (guard kept),
  a clean success log converges to ``succeeded`` (guard released), and a
  pending log matches the pending-approval recovery semantics;
- concurrent reconcile on one expired operation stays consistent (row lock
  serialization, single state transition);
- write kill-switch: claim refuses while
  ``assistant_action_execution_enabled`` is off and resumes after re-enable.

Safety: requires the explicit ``OPENONTOLOGY_ONTOLOGY_ASSISTANT_E2E=1``
sentinel and ``ENVIRONMENT=test``. All fixtures live in one UUID-suffixed
private schema (``search_path``) that this script creates and always drops in
``finally``; other schemas/databases are never modified. The JSON report
contains only fixed fields, counts, booleans, and durations — no connection
strings, credentials, or schema names.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

SENTINEL = "OPENONTOLOGY_ONTOLOGY_ASSISTANT_E2E"
DEFAULT_REPORT = BACKEND_DIR.parent / ".artifacts" / "ontology-assistant" / \
    "action-postgres.json"

REPORT_STAGES = {
    "starting",
    "database_guard",
    "schema_setup",
    "fixtures",
    "prepare_idempotency",
    "prepare_concurrency",
    "decide_concurrency",
    "concurrent_claim",
    "guard_uniqueness",
    "lease_recovery",
    "concurrent_reconcile",
    "kill_switch",
    "cleanup",
    "complete",
}
REPORT_FIELDS = {
    "schema_version",
    "result",
    "stage",
    "server_version_major",
    "cases",
    "durations_ms",
    "worker_threads_started",
    "worker_threads_alive_after",
}
REPORT_CASES = {
    "prepare_single_row_under_concurrency",
    "prepare_concurrent_replay_single_confirmation",
    "decide_approve_vs_deny_single_outcome",
    "decide_same_decision_replay_consistent",
    "claim_single_winner",
    "guard_blocks_identical_active_payload",
    "guard_released_after_terminal",
    "recovery_success_log_with_attempt_unknown",
    "recovery_success_log_clean_succeeded",
    "recovery_pending_log_matches_pending_recovery",
    "concurrent_reconcile_single_transition",
    "kill_switch_blocks_claim_and_resumes",
}
THREADS = 4

# Minimal formal snapshot: one object type + one non-approval action with an
# update rule. Mirrors the shape the HTTP contract tests use, so service-level
# prepare/decide run the real release/scope resolution path on real PG.
_SNAPSHOT = {
    "objectTypes": [
        {"id": "ot-e2e", "name": "Order", "displayName": "订单",
         "primaryKey": "order_no",
         "properties": [
             {"id": "p1", "name": "order_no", "displayName": "订单号",
              "type": "string", "required": True},
             {"id": "p2", "name": "status", "displayName": "状态",
              "type": "string"},
         ], "positionX": 0, "positionY": 0},
    ],
    "linkTypes": [],
    "actions": [
        {"id": "act-e2e", "name": "mark_paid", "displayName": "标记已支付",
         "objectTypeId": "ot-e2e", "requiresApproval": False,
         "parameters": [
             {"name": "note", "displayName": "备注", "type": "string"},
         ],
         "rules": [{"type": "update_property", "name": "set-status",
                    "enabled": True, "order": 0,
                    "config": {"targetProperty": "status",
                               "valueSource": "constant",
                               "value": "\"paid\""}}]},
    ],
    "functions": [],
}


def _report_template() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "result": "fail",
        "stage": "starting",
        "server_version_major": 0,
        "cases": {name: False for name in sorted(REPORT_CASES)},
        "durations_ms": {},
        "worker_threads_started": 0,
        "worker_threads_alive_after": 0,
    }


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return {str(key): _sanitize_value(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item) for item in value]
    raise ValueError(f"unsupported report value type: {type(value)!r}")


def _assert_sanitized_report(report: dict[str, Any],
                             sensitive_values: set[str]) -> bytes:
    if set(report) != REPORT_FIELDS:
        raise ValueError("report schema mismatch")
    if report["schema_version"] != 1:
        raise ValueError("report schema version mismatch")
    if report["result"] not in {"pass", "fail"}:
        raise ValueError("invalid report result")
    if report["stage"] not in REPORT_STAGES:
        raise ValueError("invalid report stage")
    if set(report["cases"]) != REPORT_CASES:
        raise ValueError("report case set mismatch")
    payload = json.dumps(_sanitize_value(report), sort_keys=True,
                         indent=2) + "\n"
    for value in sensitive_values:
        if value and value in payload:
            raise ValueError("sensitive value reached report payload")
    return payload.encode("utf-8")


def _write_report(path: Path, report: dict[str, Any],
                  sensitive_values: set[str]) -> None:
    payload = _assert_sanitized_report(report, sensitive_values)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.replace(tmp, path)
        path.chmod(0o600)
    finally:
        if tmp.exists():
            tmp.unlink()
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ValueError("report mode must be 0600")


def _guard_database_url() -> str:
    if os.environ.get(SENTINEL) != "1":
        raise ValueError(f"{SENTINEL} must be 1")
    if os.environ.get("ENVIRONMENT") != "test":
        raise ValueError("ENVIRONMENT must be test")
    database_url = os.environ.get("DATABASE_URL", "")
    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("DATABASE_URL must use PostgreSQL")
    if parsed.query or parsed.fragment:
        raise ValueError(
            "DATABASE_URL must not contain query or fragment overrides")
    if not parsed.username or not parsed.password:
        raise ValueError("DATABASE_URL must contain credentials")
    return database_url


def _isolation_url(raw_url: str, schema: str):
    """URL with a private search_path. Returns a sqlalchemy URL object —
    ``create_engine`` then passes components as connect kwargs (not a
    percent-encoded DSN string, which libpq does not decode)."""
    from sqlalchemy.engine import make_url

    url = make_url(raw_url)
    return url.set(query={"options": f"-csearch_path={schema}"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _confirmation_row(**overrides):
    from app.ontologies.agent_confirmation import models as CM

    payload = dict(
        id=f"conf-{uuid.uuid4().hex[:12]}",
        actor_user_id="user-e2e",
        session_kind="ontology_agent",
        session_id=f"sess-{uuid.uuid4().hex[:8]}",
        root_intent_id=f"intent-{uuid.uuid4().hex[:8]}",
        action_request_slot=f"slot-{uuid.uuid4().hex[:8]}",
        prepare_request_id="",
        origin="user_direct",
        source_call_id="src-e2e",
        ontology_id="ont-e2e",
        release_id="rel-e2e",
        action_id="act-e2e",
        final_parameters={"note": "e2e"},
        parameter_sources={"note": {"kind": "user_input"}},
        requires_approval=False,
        payload_hash=f"hash-{uuid.uuid4().hex[:16]}",
        display_payload={"action": {"id": "act-e2e", "name": "e2e"}},
        state="prepared",
        expires_at=_now() + timedelta(hours=24),
    )
    payload.update(overrides)
    return CM.ActionConfirmation(**payload)


def _operation_row(confirmation_id, guard_hash, **overrides):
    from app.ontologies.agent_confirmation import models as CM

    payload = dict(
        id=f"op-{uuid.uuid4().hex[:12]}",
        confirmation_id=confirmation_id,
        initial_engine_key=f"proposal:{confirmation_id}",
        guard_hash=guard_hash,
        state="dispatch_queued",
    )
    payload.update(overrides)
    return CM.ActionOperation(**payload)


def _attempt_row(operation_id, ordinal=0, state="started"):
    from app.ontologies.agent_confirmation import models as CM

    return CM.ActionExternalAttempt(
        id=f"att-{uuid.uuid4().hex[:12]}",
        operation_id=operation_id,
        effect_ordinal=ordinal,
        stable_delivery_key=f"att-key-{uuid.uuid4().hex[:12]}",
        state=state,
    )


def _engine_log_row(cid, status, *, error_message=None, action_id="act-e2e"):
    from app.ontologies.formal_modeling.models import ActionExecutionLog

    return ActionExecutionLog(
        id=f"log-{uuid.uuid4().hex[:10]}",
        ontology_id="ont-e2e",
        action_id=action_id,
        status=status,
        parameters={},
        error_message=error_message,
        idempotency_key=f"proposal:{cid}",
    )


def _guard_hash(seed: str) -> str:
    import hashlib

    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _run_threads(fn, count=THREADS):
    barrier = threading.Barrier(count)

    def wrapped(index):
        barrier.wait(timeout=30)
        return fn(index)

    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(wrapped, i) for i in range(count)]
        return [future.result() for future in futures]


def _seed_fixtures(session_factory) -> dict:
    """One user, one released ontology (FK/scope target), one confirmation+
    operation pair, and the agent profile so decide() never mutates fixtures
    mid-transaction (get_or_create_profile would commit inside the lock)."""
    from types import SimpleNamespace

    from app.auth.models import User
    from app.ontologies.agent_runtime.models import AgentProfile
    from app.ontologies.projects.models import OntologyProject
    from app.ontologies.versions.models import OntologyVersion

    session = session_factory()
    try:
        session.add(User(id="user-e2e", username="e2e-action",
                         email="e2e-action@example.test",
                         password_hash="x", role="admin"))
        project = OntologyProject(
            id="ont-e2e", name="e2e", domain="e2e", created_by="user-e2e")
        session.add(project)
        session.flush()
        release = OntologyVersion(
            id="rel-e2e", ontology_id="ont-e2e", version_number="v1",
            node_kind="release", lifecycle_status="released",
            snapshot_formal=_SNAPSHOT, created_by="user-e2e")
        session.add(release)
        session.flush()  # projects.current_release_id FK 需先有 release 行
        project.current_release_id = release.id
        session.add(AgentProfile(ontology_id="ont-e2e", enabled=True,
                                 allowed_action_ids=["act-e2e"],
                                 allow_action_proposals=True))
        confirmation = _confirmation_row()
        session.add(confirmation)
        session.flush()
        operation = _operation_row(confirmation.id,
                                   _guard_hash("guard-main"))
        session.add(operation)
        session.commit()
        return {"confirmation_id": confirmation.id,
                "operation_id": operation.id,
                "release_id": release.id,
                "user": SimpleNamespace(id="user-e2e")}
    finally:
        session.close()


def _run(report_path: Path) -> None:
    report = _report_template()
    sensitive_values: set[str] = set()
    stage = "starting"
    engine = None
    schema = f"oac_e2e_{uuid.uuid4().hex[:12]}"
    started_threads: set[int] = set()
    try:
        _write_report(report_path, report, sensitive_values)
        stage = "database_guard"
        report["stage"] = stage
        database_url = _guard_database_url()
        sensitive_values.add(database_url)
        sensitive_values.add(schema)

        from sqlalchemy import create_engine, text

        engine = create_engine(_isolation_url(database_url, schema),
                               pool_size=THREADS + 2, max_overflow=4)
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))

        stage = "schema_setup"
        report["stage"] = stage
        from app.auth.models import User
        from app.database import Base
        from app.ontologies.agent_runtime.models import AgentProfile
        from app.ontologies.agent_confirmation import models as CM
        from app.ontologies.formal_modeling.models import ActionExecutionLog
        from app.ontologies.projects.models import OntologyProject
        from app.ontologies.versions.models import OntologyVersion

        Base.metadata.create_all(bind=engine, tables=[
            User.__table__,
            OntologyProject.__table__,
            OntologyVersion.__table__,
            AgentProfile.__table__,
            CM.ActionConfirmation.__table__,
            CM.ActionOperation.__table__,
            CM.ActionExternalAttempt.__table__,
            ActionExecutionLog.__table__,
        ])
        with engine.connect() as conn:
            report["server_version_major"] = int(conn.execute(text(
                "SELECT current_setting('server_version_num')"))
                .scalar()) // 10000

        from sqlalchemy.orm import sessionmaker

        session_factory = sessionmaker(bind=engine)

        stage = "fixtures"
        report["stage"] = stage
        world = _seed_fixtures(session_factory)

        from app.ontologies.agent_confirmation import service as confirmation_svc

        # ------------------------------------------ prepare_idempotency
        stage = "prepare_idempotency"
        report["stage"] = stage
        started = time.monotonic()
        base = dict(
            actor_user_id="user-e2e", session_kind="ontology_agent",
            session_id="sess-idem", root_intent_id="intent-idem",
            action_request_slot="slot-idem",
            prepare_request_id="prep-idem",
            payload_hash="hash-idem")

        def _insert_confirmation(_index):
            local = session_factory()
            try:
                local.add(_confirmation_row(**base))
                local.commit()
                return "inserted"
            except Exception:  # noqa: BLE001 - IntegrityError expected
                local.rollback()
                return "conflict"
            finally:
                local.close()

        outcomes = _run_threads(_insert_confirmation)
        rows = 0
        probe = session_factory()
        try:
            rows = probe.query(
                CM.ActionConfirmation).filter_by(
                prepare_request_id="prep-idem").count()
        finally:
            probe.close()
        report["cases"]["prepare_single_row_under_concurrency"] = (
            rows == 1 and outcomes.count("inserted") == 1
            and outcomes.count("conflict") == THREADS - 1)
        report["durations_ms"]["prepare_idempotency"] = round(
            (time.monotonic() - started) * 1000, 3)

        # ---------------------------------------- prepare_concurrency
        stage = "prepare_concurrency"
        report["stage"] = stage
        started = time.monotonic()
        from app.ontologies.agent_confirmation.schemas import (
            PrepareProposalRequest,
        )

        def _prepare_body():
            return PrepareProposalRequest(
                session_kind="ontology_agent", session_id="sess-decide",
                client_submission_id="sub-e2e", action_id="act-e2e",
                release_id=world["release_id"],
                parameters={"note": "pg-e2e"})

        def _service_prepare(_index):
            local = session_factory()
            try:
                confirmation, replayed = confirmation_svc.prepare(
                    local, world["user"], "ont-e2e", _prepare_body(),
                    origin="user_direct", source_call_id="sub-e2e",
                    root_intent_id="intent-decide",
                    action_request_slot="slot-decide")
                return confirmation.id, replayed
            finally:
                local.close()

        prepare_results = _run_threads(_service_prepare)
        prepare_ids = {result[0] for result in prepare_results}
        prepare_replayed = sum(1 for _, replayed in prepare_results if replayed)
        report["cases"]["prepare_concurrent_replay_single_confirmation"] = (
            len(prepare_ids) == 1 and prepare_replayed == THREADS - 1)

        # ----------------------------------------- decide_concurrency
        stage = "decide_concurrency"
        report["stage"] = stage
        started = time.monotonic()
        decide_cid = next(iter(prepare_ids))
        decide_payload_hash = ""
        probe = session_factory()
        try:
            decide_payload_hash = probe.get(
                CM.ActionConfirmation, decide_cid).payload_hash
        finally:
            probe.close()

        def _service_decide(index):
            decision, decision_id = (
                ("approve", "dec-a") if index % 2 == 0 else ("deny", "dec-d"))
            local = session_factory()
            try:
                confirmation = confirmation_svc.decide(
                    local, world["user"], "ont-e2e", decide_cid, decision,
                    decision_id, decide_payload_hash, "ontology_agent",
                    "sess-decide")
                return decision, confirmation.state, None
            except confirmation_svc.ConfirmationError as exc:
                return decision, None, exc.code
            finally:
                local.close()

        decide_results = _run_threads(_service_decide)
        approve_ok = [r for r in decide_results
                      if r[0] == "approve" and r[2] is None]
        deny_ok = [r for r in decide_results
                   if r[0] == "deny" and r[2] is None]
        losers = [r for r in decide_results if r[2] is not None]
        single_outcome = (
            (len(approve_ok) == THREADS // 2 and not deny_ok)
            or (len(deny_ok) == THREADS // 2 and not approve_ok))
        if single_outcome:
            single_outcome = all(r[2] == "decision_conflict"
                                 for r in losers)
        probe = session_factory()
        try:
            final_state = probe.get(
                CM.ActionConfirmation, decide_cid).state
            operation_count = probe.query(CM.ActionOperation).filter_by(
                confirmation_id=decide_cid).count()
        finally:
            probe.close()
        if approve_ok:
            single_outcome = single_outcome and final_state == (
                "dispatch_queued") and operation_count == 1
        else:
            single_outcome = single_outcome and final_state == "denied" \
                and operation_count == 0
        report["cases"]["decide_approve_vs_deny_single_outcome"] = (
            single_outcome)

        # 同 decision_id 重放：结果一致，不产生第二个 operation
        replay_consistent = True
        if approve_ok:
            local = session_factory()
            try:
                replayed = confirmation_svc.decide(
                    local, world["user"], "ont-e2e", decide_cid, "approve",
                    "dec-a", decide_payload_hash, "ontology_agent",
                    "sess-decide")
                replay_consistent = replayed.state == "dispatch_queued"
            except confirmation_svc.ConfirmationError:
                replay_consistent = False
            finally:
                local.close()
            probe = session_factory()
            try:
                replay_consistent = replay_consistent and probe.query(
                    CM.ActionOperation).filter_by(
                    confirmation_id=decide_cid).count() == 1
            finally:
                probe.close()
        report["cases"]["decide_same_decision_replay_consistent"] = (
            replay_consistent)
        report["durations_ms"]["decide_concurrency"] = round(
            (time.monotonic() - started) * 1000, 3)

        # ------------------------------------------- concurrent_claim
        stage = "concurrent_claim"
        report["stage"] = stage
        started = time.monotonic()

        def _claim(_index):
            started_threads.add(threading.get_ident())
            local = session_factory()
            try:
                won = confirmation_svc._claim(world["operation_id"], local)
                return won is not None
            finally:
                local.close()

        claim_results = _run_threads(_claim)
        report["worker_threads_started"] = len(started_threads)
        probe = session_factory()
        try:
            op = probe.get(CM.ActionOperation, world["operation_id"])
            claimed_ok = (sum(claim_results) == 1 and op.state == "executing"
                          and op.claimed_at is not None
                          and op.lease_expires_at is not None)
            probe.commit()
        finally:
            probe.close()
        report["cases"]["claim_single_winner"] = claimed_ok
        report["durations_ms"]["concurrent_claim"] = round(
            (time.monotonic() - started) * 1000, 3)

        # ------------------------------------------- guard_uniqueness
        stage = "guard_uniqueness"
        report["stage"] = stage
        started = time.monotonic()
        guard_main = _guard_hash("guard-main")
        session = session_factory()
        try:
            # 相同 guard_hash 的第二个活跃操作被部分唯一索引拒绝
            other = _confirmation_row()
            session.add(other)
            session.flush()
            session.add(_operation_row(other.id, guard_main))
            try:
                session.flush()
                blocked = False
            except Exception:  # noqa: BLE001 - IntegrityError expected
                session.rollback()
                blocked = True
            report["cases"]["guard_blocks_identical_active_payload"] = blocked

            # 主操作结案后 guard 释放：同 guard_hash 可再建活跃操作
            main_op = session.get(CM.ActionOperation,
                                  world["operation_id"])
            main_op.state = "succeeded"
            session.commit()
            successor = _operation_row(other.id, guard_main)
            session.add(successor)
            session.flush()
            released = successor.id is not None
            session.rollback()
            report["cases"]["guard_released_after_terminal"] = released
        finally:
            session.close()
        report["durations_ms"]["guard_uniqueness"] = round(
            (time.monotonic() - started) * 1000, 3)

        # --------------------------------------------- lease_recovery
        stage = "lease_recovery"
        report["stage"] = stage

        def _seed_expired_operation(tag, *, with_attempt, log_status):
            session = session_factory()
            try:
                confirmation = _confirmation_row()
                session.add(confirmation)
                session.flush()
                operation = _operation_row(
                    confirmation.id, _guard_hash(f"guard-{tag}"),
                    state="executing",
                    claimed_at=_now() - timedelta(minutes=5),
                    lease_expires_at=_now() - timedelta(seconds=1),
                    engine_entered_at=_now() - timedelta(minutes=5),
                    worker_ref=f"dead-worker-{tag}")
                session.add(operation)
                session.flush()
                if with_attempt:
                    session.add(_attempt_row(operation.id))
                if log_status is not None:
                    session.add(_engine_log_row(confirmation.id, log_status))
                session.commit()
                return operation.id, confirmation.id
            finally:
                session.close()

        def _reconcile(operation_id):
            local = session_factory()
            try:
                confirmation_svc.reconcile_operation(operation_id, local)
            finally:
                local.close()

        started = time.monotonic()
        # 本地 success 日志 + 未结案 attempt → unknown，guard 不释放
        op_id, cid = _seed_expired_operation("unknown", with_attempt=True,
                                             log_status="success")
        _reconcile(op_id)
        session = session_factory()
        try:
            op = session.get(CM.ActionOperation, op_id)
            conf = session.get(CM.ActionConfirmation, cid)
            case_ok = (op.state == "unknown"
                       and conf.state == "unknown"
                       and conf.failure_code == "execution_result_unknown"
                       and op.reconcile_evidence.get("reason")
                       == "local_success_with_unresolved_external_attempt")
            # guard 仍占用：同 hash 活跃操作插入被拒
            if case_ok:
                other = _confirmation_row()
                session.add(other)
                session.flush()
                session.add(_operation_row(other.id, op.guard_hash))
                try:
                    session.flush()
                    case_ok = False
                except Exception:  # noqa: BLE001
                    session.rollback()
            report["cases"]["recovery_success_log_with_attempt_unknown"] = (
                case_ok)
        finally:
            session.close()

        # 无外部效果点：本地 success 日志 → succeeded 且 guard 释放
        op_id, cid = _seed_expired_operation("clean", with_attempt=False,
                                             log_status="success")
        _reconcile(op_id)
        session = session_factory()
        try:
            op = session.get(CM.ActionOperation, op_id)
            conf = session.get(CM.ActionConfirmation, cid)
            case_ok = op.state == "succeeded" and conf.state == "succeeded"
            if case_ok:
                # 用新确认承载同 guard_hash 的后继活跃操作：唯一确认约束
                # 不应误判为 guard 仍被占用
                successor_owner = _confirmation_row()
                session.add(successor_owner)
                session.flush()
                successor = _operation_row(successor_owner.id, op.guard_hash)
                session.add(successor)
                try:
                    session.flush()
                except Exception:  # noqa: BLE001
                    session.rollback()
                    case_ok = False
                else:
                    session.rollback()
            report["cases"]["recovery_success_log_clean_succeeded"] = case_ok
        finally:
            session.close()

        # pending 日志恢复与 pending 审批恢复一致：attempt → proven_not_applied
        op_id, cid = _seed_expired_operation("pending", with_attempt=True,
                                             log_status="pending")
        _reconcile(op_id)
        session = session_factory()
        try:
            op = session.get(CM.ActionOperation, op_id)
            conf = session.get(CM.ActionConfirmation, cid)
            attempts = session.query(CM.ActionExternalAttempt).filter_by(
                operation_id=op_id).all()
            report["cases"]["recovery_pending_log_matches_pending_recovery"] = (
                op.state == "pending_approval"
                and conf.state == "pending_approval"
                and op.approval_log_id is not None
                and attempts
                and all(a.state == "proven_not_applied" for a in attempts)
                and all(a.evidence_ref
                        == "engine_returned_pending_before_effects"
                        for a in attempts))
        finally:
            session.close()
        report["durations_ms"]["lease_recovery"] = round(
            (time.monotonic() - started) * 1000, 3)

        # --------------------------------------- concurrent_reconcile
        stage = "concurrent_reconcile"
        report["stage"] = stage
        started = time.monotonic()
        op_id, cid = _seed_expired_operation("concurrent",
                                             with_attempt=True,
                                             log_status="success")

        def _reconcile_concurrent(_index):
            started_threads.add(threading.get_ident())
            _reconcile(op_id)

        _run_threads(_reconcile_concurrent)
        session = session_factory()
        try:
            op = session.get(CM.ActionOperation, op_id)
            conf = session.get(CM.ActionConfirmation, cid)
            # 行锁串行化：多次并发对账只产生一致的单一终态，不重复推进
            report["cases"]["concurrent_reconcile_single_transition"] = (
                op.state == "unknown" and conf.state == "unknown"
                and op.execution_log_id is not None)
        finally:
            session.close()
        report["durations_ms"]["concurrent_reconcile"] = round(
            (time.monotonic() - started) * 1000, 3)

        # -------------------------------------------------- kill_switch
        stage = "kill_switch"
        report["stage"] = stage
        started = time.monotonic()
        from app.config import settings

        op_id, cid = _seed_expired_operation("kill", with_attempt=False,
                                             log_status=None)
        session = session_factory()
        try:
            op = session.get(CM.ActionOperation, op_id)
            op.state = "dispatch_queued"
            op.engine_entered_at = None
            op.claimed_at = None
            op.lease_expires_at = None
            session.commit()
        finally:
            session.close()
        previous = getattr(settings, "assistant_action_execution_enabled",
                           True)
        try:
            setattr(settings, "assistant_action_execution_enabled", False)
            local = session_factory()
            try:
                won = confirmation_svc._claim(op_id, local)
            finally:
                local.close()
            blocked = won is None
            local = session_factory()
            try:
                state_off = local.get(CM.ActionOperation, op_id).state
            finally:
                local.close()
            setattr(settings, "assistant_action_execution_enabled", True)
            local = session_factory()
            try:
                won2 = confirmation_svc._claim(op_id, local)
            finally:
                local.close()
            report["cases"]["kill_switch_blocks_claim_and_resumes"] = (
                blocked and state_off == "dispatch_queued"
                and won2 is not None)
        finally:
            setattr(settings, "assistant_action_execution_enabled",
                    previous)
        report["durations_ms"]["kill_switch"] = round(
            (time.monotonic() - started) * 1000, 3)

        # ----------------------------------------------------- cleanup
        stage = "cleanup"
        report["stage"] = stage
        report["worker_threads_alive_after"] = sum(
            1 for thread in threading.enumerate()
            if thread.ident in started_threads)
        report["result"] = "pass" if all(report["cases"].values()) else "fail"
        stage = "complete"
        report["stage"] = stage
    finally:
        if engine is not None:
            try:
                from sqlalchemy import text

                with engine.begin() as conn:
                    conn.execute(
                        text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            except Exception:  # noqa: BLE001 - cleanup must not mask result
                if report["result"] == "pass":
                    report["result"] = "fail"
            finally:
                engine.dispose()
        _write_report(report_path, report, sensitive_values)
    if report["result"] != "pass":
        raise SystemExit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    _run(args.report)
    print(f"action-confirmation postgres e2e: pass "
          f"(report: {args.report})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
