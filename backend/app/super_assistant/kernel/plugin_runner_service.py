"""Durable NATS entry point for the isolated process-plugin runner.

This service is deliberately a *runner boundary*, rather than a second copy of
the API's process host.  It validates the versioned wire envelopes, journals
irreversible steps in PostgreSQL, and invokes a launcher only after an
independently provisioned rootless-sandbox attestation has been verified.  The
default launcher is absent: an unproven deployment emits ``unknown`` and never
executes user code (fail closed).

The launcher/attestation interfaces are dependency-injected so contract and
crash-recovery behavior can be tested without pretending that a normal child
process is an OS sandbox.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.shared.database import SessionLocal
from app.super_assistant.models import SuperAssistantPluginRunnerJournal

from .contracts import ContractError
from .plugin_runner import (
    PLUGIN_RUNNER_DURABLE,
    PLUGIN_RUNNER_PROTOCOL,
    PLUGIN_RUNNER_REPLY_PREFIX,
    PLUGIN_RUNNER_STREAM,
    PLUGIN_RUNNER_SUBJECT,
    PluginInvocationEnvelope,
    PluginRunnerEventEnvelope,
)

logger = logging.getLogger(__name__)

JOURNAL_ACCEPTED = "accepted"
JOURNAL_VALIDATED = "validated"
JOURNAL_SPAWNED = "spawned"
JOURNAL_FINISHED = "finished"
JOURNAL_UNKNOWN = "unknown"
JOURNAL_TERMINAL = frozenset({JOURNAL_FINISHED, JOURNAL_UNKNOWN})
_JOURNAL_STATES = frozenset({JOURNAL_ACCEPTED, JOURNAL_VALIDATED, JOURNAL_SPAWNED, *JOURNAL_TERMINAL})
_SAFE_ATTESTATION_MODE = "rootless"
_SHA256_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_ERROR = 500


class RootlessSandboxUnavailable(RuntimeError):
    """The deployment did not prove that an isolated rootless sandbox exists."""


class SandboxAttestation(Protocol):
    def verify(self, envelope: PluginInvocationEnvelope) -> None:
        """Raise :class:`RootlessSandboxUnavailable` unless execution is safe."""


class SandboxLauncher(Protocol):
    async def __call__(
        self, envelope: PluginInvocationEnvelope,
    ) -> PluginRunnerEventEnvelope | list[PluginRunnerEventEnvelope]:
        """Run one envelope inside the already-attested sandbox."""


@dataclass(frozen=True, slots=True)
class AttestationRecord:
    mode: str
    image_digest: str
    runtime: str


class FileRootlessSandboxAttestation:
    """Verify an operator-provisioned rootless-runner attestation file.

    The file is an output of deployment's sandbox probe and must be owned by
    the service user with no group/other write bits.  This check does not claim
    to construct a sandbox; if the probe or file is absent, execution remains
    disabled.  Production provisioning is responsible for generating the file
    only after testing the configured OCI runtime and rootless user namespace.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path or os.environ.get("PLUGIN_RUNNER_ROOTLESS_ATTESTATION", ""))

    def verify(self, envelope: PluginInvocationEnvelope) -> None:
        if not self.path or not self.path.is_file():
            raise RootlessSandboxUnavailable("rootless sandbox attestation is missing")
        try:
            stat = self.path.stat()
            # Group/world writable attestation files are mutable by an
            # untrusted plugin or unrelated process and cannot be trusted.
            if stat.st_mode & 0o022:
                raise RootlessSandboxUnavailable("rootless sandbox attestation is writable")
            record = json.loads(self.path.read_text(encoding="utf-8"))
        except RootlessSandboxUnavailable:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RootlessSandboxUnavailable("rootless sandbox attestation is unreadable") from exc
        if not isinstance(record, Mapping):
            raise RootlessSandboxUnavailable("rootless sandbox attestation is not an object")
        if record.get("protocol") != "plugin-runner.sandbox.v1":
            raise RootlessSandboxUnavailable("unsupported rootless sandbox attestation")
        if record.get("verified") is not True or record.get("mode") != _SAFE_ATTESTATION_MODE:
            raise RootlessSandboxUnavailable("rootless sandbox attestation is not verified")
        digest = str(record.get("image_digest") or "")
        if not _SHA256_DIGEST.fullmatch(digest):
            raise RootlessSandboxUnavailable("rootless sandbox image digest is not immutable")
        runtime = str(record.get("runtime") or "").strip()
        if not runtime or len(runtime) > 128:
            raise RootlessSandboxUnavailable("rootless sandbox runtime is missing")
        # The attestation is also the deployment's proof that the launcher
        # applies the immutable policy snapshots from the invocation.  A
        # rootless container by itself is not enough: network/workspace scope
        # must be allowlisted and credential values must come only from the
        # owner/run/call-bound Scope Broker. Missing any proof fails closed.
        if record.get("network_scope_enforced") is not True:
            raise RootlessSandboxUnavailable("rootless sandbox network scope is not enforced")
        if record.get("workspace_scope_enforced") is not True:
            raise RootlessSandboxUnavailable("rootless sandbox workspace scope is not enforced")
        if record.get("secret_broker") != "scope-broker.v1":
            raise RootlessSandboxUnavailable("rootless sandbox secret broker is unavailable")


def _error(value: object) -> str:
    return str(value or "runner error")[:_MAX_ERROR]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PluginRunnerJournal:
    """PostgreSQL-backed idempotency and crash-recovery journal."""

    _ALLOWED_TRANSITIONS = {
        JOURNAL_ACCEPTED: frozenset({JOURNAL_VALIDATED, JOURNAL_UNKNOWN}),
        JOURNAL_VALIDATED: frozenset({JOURNAL_SPAWNED, JOURNAL_UNKNOWN}),
        JOURNAL_SPAWNED: frozenset({JOURNAL_FINISHED, JOURNAL_UNKNOWN}),
        JOURNAL_FINISHED: frozenset(),
        JOURNAL_UNKNOWN: frozenset(),
    }

    def __init__(self, db: Session):
        self.db = db

    def get(self, request_id: str, *, lock: bool = False) -> SuperAssistantPluginRunnerJournal | None:
        statement = select(SuperAssistantPluginRunnerJournal).where(
            SuperAssistantPluginRunnerJournal.request_id == request_id,
        )
        if lock:
            statement = statement.with_for_update()
        return self.db.scalar(statement)

    def accept(self, envelope: PluginInvocationEnvelope) -> SuperAssistantPluginRunnerJournal:
        """Durably claim request identity; duplicate delivery is a no-op."""
        row = self.get(envelope.request_id, lock=True)
        if row is not None:
            self._assert_identity(row, envelope)
            return row
        row = SuperAssistantPluginRunnerJournal(
            request_id=envelope.request_id,
            owner_id=envelope.owner_id,
            run_id=envelope.run_id,
            call_id=envelope.call_id,
            plugin_id=envelope.plugin_id,
            revision=envelope.revision,
            manifest_hash=envelope.manifest_hash,
            capability_revision=envelope.capability_revision,
            state=JOURNAL_ACCEPTED,
            event_seq=-1,
            outcome={},
        )
        self.db.add(row)
        try:
            self.db.flush()
        except IntegrityError:
            # Another runner won the request id between SELECT and INSERT.
            self.db.rollback()
            row = self.get(envelope.request_id, lock=True)
            if row is None:
                raise
            self._assert_identity(row, envelope)
        return row

    @staticmethod
    def _assert_identity(row: SuperAssistantPluginRunnerJournal, envelope: PluginInvocationEnvelope) -> None:
        fields = ("owner_id", "run_id", "call_id", "plugin_id", "revision", "manifest_hash", "capability_revision")
        if any(getattr(row, field) != getattr(envelope, field) for field in fields):
            raise ContractError("plugin runner request_id was reused with different identity")

    def transition(
        self, row: SuperAssistantPluginRunnerJournal, state: str, *, outcome: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> SuperAssistantPluginRunnerJournal:
        if state not in _JOURNAL_STATES:
            raise ContractError("invalid plugin runner journal state")
        current = str(row.state)
        if current == state:
            return row
        if state not in self._ALLOWED_TRANSITIONS.get(current, frozenset()):
            raise ContractError(f"invalid plugin runner journal transition {current!r} -> {state!r}")
        row.state = state
        if outcome is not None:
            row.outcome = dict(outcome)
        if error is not None:
            row.error = _error(error)
        row.updated_at = _now()
        self.db.flush()
        self.db.commit()
        self.db.refresh(row)
        return row

    def record_event(self, row: SuperAssistantPluginRunnerJournal, event: PluginRunnerEventEnvelope) -> None:
        identity_fields = (
            "request_id", "owner_id", "run_id", "call_id", "plugin_id",
            "revision", "manifest_hash", "capability_revision",
        )
        if any(getattr(event, field) != getattr(row, field) for field in identity_fields):
            raise ContractError("plugin runner event identity does not match journal")
        if event.event_seq <= row.event_seq:
            # Replayed event is harmless; a sequence gap is rejected below.
            if event.event_seq == row.event_seq:
                # Sequence equality alone is insufficient: a compromised or
                # buggy runner must not be able to replace the durable
                # evidence for an already committed event.  Compare the
                # complete bounded wire event before treating it as a replay.
                prior = row.last_event or {}
                if prior and dict(prior) != event.to_payload():
                    raise ContractError("plugin runner replay payload conflicts with journal")
                return
            raise ContractError("plugin runner event sequence moved backwards")
        if event.event_seq != row.event_seq + 1:
            raise ContractError("plugin runner event sequence has a gap")
        row.event_seq = event.event_seq
        row.outcome = {
            "kind": event.kind, "status": event.status,
            "payload": dict(event.payload),
            "artifacts": [dict(item) for item in event.artifacts],
        }
        row.last_event = event.to_payload()
        self.db.flush()
        self.db.commit()
        self.db.refresh(row)


def _event_for(
    envelope: PluginInvocationEnvelope, row: SuperAssistantPluginRunnerJournal, *, kind: str,
    payload: Mapping[str, Any], event_seq: int | None = None,
) -> PluginRunnerEventEnvelope:
    return PluginRunnerEventEnvelope(
        request_id=envelope.request_id, owner_id=envelope.owner_id, run_id=envelope.run_id,
        call_id=envelope.call_id, plugin_id=envelope.plugin_id, revision=envelope.revision,
        manifest_hash=envelope.manifest_hash, capability_revision=envelope.capability_revision,
        event_seq=(row.event_seq + 1 if event_seq is None else event_seq), kind=kind,
        status={
            "progress": "running", "approval_requested": "waiting_approval",
            "artifact": "running", "completed": "completed", "failed": "failed",
            "cancelled": "cancelled", "unknown": "unknown",
        }[kind],
        payload=dict(payload), artifacts=(), reply_subject=envelope.reply_subject,
    )


def _assert_event_identity(event: PluginRunnerEventEnvelope, envelope: PluginInvocationEnvelope) -> None:
    fields = ("request_id", "owner_id", "run_id", "call_id", "plugin_id", "revision", "manifest_hash", "capability_revision", "reply_subject")
    if any(getattr(event, field) != getattr(envelope, field) for field in fields):
        raise ContractError("plugin runner event identity does not match invocation")


def _replay_last_event(row: SuperAssistantPluginRunnerJournal, envelope: PluginInvocationEnvelope) -> PluginRunnerEventEnvelope | None:
    raw = row.last_event or {}
    if not isinstance(raw, Mapping) or not raw:
        return None
    event = PluginRunnerEventEnvelope.from_payload(raw)
    _assert_event_identity(event, envelope)
    return event


async def _record_unknown(
    journal: PluginRunnerJournal,
    row: SuperAssistantPluginRunnerJournal,
    envelope: PluginInvocationEnvelope,
    *,
    reason: str,
    error: object,
    publish: Callable[[PluginRunnerEventEnvelope], Awaitable[None]] | None,
) -> None:
    """Persist the unknown event before closing the journal state.

    The event is the durable evidence consumed by Kernel reconciliation.  It
    must be committed before the terminal journal transition; otherwise a
    crash between two commits can leave a terminal row with no replayable
    evidence.  A redelivery then either emits a second event or silently loses
    the provider outcome.
    """
    event = _event_for(envelope, row, kind="unknown", payload={"reason": reason})
    journal.record_event(row, event)
    journal.transition(row, JOURNAL_UNKNOWN, outcome={"reason": reason}, error=error)
    if publish:
        await publish(event)


class PluginRunnerService:
    """One invocation processor; transport lifecycle is provided below."""

    def __init__(
        self, db_factory: Callable[[], Session] = SessionLocal, *,
        attestation: SandboxAttestation | None = None,
        launcher: SandboxLauncher | None = None,
    ) -> None:
        self.db_factory = db_factory
        self.attestation = attestation or FileRootlessSandboxAttestation()
        self.launcher = launcher

    async def handle(self, payload: Mapping[str, Any], publish: Callable[[PluginRunnerEventEnvelope], Awaitable[None]] | None = None) -> bool:
        """Process one JSON payload.  Return ``True`` for a valid/duplicate message.

        Malformed envelopes are poison messages and return ``False`` so the
        transport can acknowledge them without an infinite redelivery loop.
        """
        try:
            envelope = PluginInvocationEnvelope.from_payload(payload)
        except (ContractError, TypeError, ValueError):
            logger.error("invalid plugin runner invocation envelope")
            return False
        db = self.db_factory()
        try:
            journal = PluginRunnerJournal(db)
            row = journal.accept(envelope)
            db.commit()
            if row.state in JOURNAL_TERMINAL:
                # A publish may have failed after the terminal state was
                # committed.  Replay the exact contract event with the same
                # request/sequence Msg-Id; JetStream deduplication makes this
                # safe for consumers that already received it.
                replay = _replay_last_event(row, envelope)
                if replay is not None and publish:
                    await publish(replay)
                return True
            if row.state == JOURNAL_SPAWNED:
                replay = _replay_last_event(row, envelope)
                if replay is not None and replay.kind in {"completed", "failed", "cancelled"}:
                    # The terminal event was journaled but its transport
                    # publish may have been interrupted.  Close and replay it.
                    journal.transition(row, JOURNAL_FINISHED, outcome={"kind": replay.kind, "status": replay.status})
                    if publish:
                        await publish(replay)
                elif replay is not None and replay.kind == "unknown":
                    # The event was committed but the state transition was
                    # interrupted. Complete the transition and replay the
                    # exact event; never append a second unknown event.
                    journal.transition(row, JOURNAL_UNKNOWN, outcome={"reason": (replay.payload or {}).get("reason", "runner_unknown")}, error=(replay.payload or {}).get("reason"))
                    if publish:
                        await publish(replay)
                else:
                    # A process may have disappeared with the service.  It is
                    # unsafe to guess whether side effects happened.
                    await _record_unknown(
                        journal, row, envelope, reason="runner_restarted_after_spawn",
                        error="runner restarted after spawn", publish=publish,
                    )
                return True
            try:
                self.attestation.verify(envelope)
            except RootlessSandboxUnavailable as exc:
                await _record_unknown(
                    journal, row, envelope, reason="sandbox_unavailable",
                    error=exc, publish=publish,
                )
                return True
            row = journal.transition(row, JOURNAL_VALIDATED)
            if self.launcher is None:
                await _record_unknown(
                    journal, row, envelope, reason="sandbox_launcher_unconfigured",
                    error="sandbox launcher is not configured", publish=publish,
                )
                return True
            row = journal.transition(row, JOURNAL_SPAWNED)
            try:
                result = await self.launcher(envelope)
            except asyncio.CancelledError:
                # Cancellation leaves outcome uncertain after spawn; persist
                # unknown before letting worker shutdown propagate.
                await _record_unknown(
                    journal, row, envelope, reason="runner_cancelled",
                    error="runner task cancelled", publish=publish,
                )
                raise
            except Exception as exc:  # noqa: BLE001 - side effect outcome is unknown
                await _record_unknown(
                    journal, row, envelope, reason="launcher_error",
                    error=exc, publish=publish,
                )
                return True
            events = result if isinstance(result, list) else [result]
            for index, event in enumerate(events):
                if not isinstance(event, PluginRunnerEventEnvelope):
                    raise ContractError("sandbox launcher returned a non-contract event")
                _assert_event_identity(event, envelope)
                if event.event_seq != row.event_seq + 1:
                    raise ContractError("sandbox launcher event sequence does not continue the journal")
                if event.kind in {"completed", "failed", "cancelled"} and index != len(events) - 1:
                    raise ContractError("sandbox launcher emitted an event after terminal result")
                journal.record_event(row, event)
                if event.kind in {"completed", "failed", "cancelled"}:
                    # Commit terminal state before transport publication.  A
                    # failed publish can therefore replay ``last_event`` on a
                    # redelivery instead of being misclassified as unknown.
                    journal.transition(row, JOURNAL_FINISHED, outcome={"kind": event.kind, "status": event.status})
                if publish:
                    await publish(event)
            final = events[-1] if events else None
            if final is None or final.kind not in {"completed", "failed", "cancelled"}:
                await _record_unknown(
                    journal, row, envelope, reason="launcher_missing_terminal_event",
                    error="launcher missing terminal event", publish=publish,
                )
            else:
                journal.transition(row, JOURNAL_FINISHED, outcome={"kind": final.kind, "status": final.status})
            return True
        finally:
            db.close()


async def ensure_plugin_runner_stream(js) -> None:
    """Create/update the dedicated work-queue stream without changing policy."""
    from nats.js.api import RetentionPolicy, StreamConfig

    config = StreamConfig(
        # Replies are retained in the same bounded stream so the internal
        # transport accepts the exact subject emitted by the runner.  There
        # is no reply consumer here; the kernel observes its own reply
        # subscription and the stream's work-queue invoke durable filters only
        # the invoke subject below.
        name=PLUGIN_RUNNER_STREAM, subjects=[PLUGIN_RUNNER_SUBJECT, f"{PLUGIN_RUNNER_REPLY_PREFIX}*"],
        retention=RetentionPolicy.WORK_QUEUE, max_age=7 * 24 * 3600,
        duplicate_window=10 * 60,
    )
    try:
        await js.add_stream(config)
    except Exception as exc:
        if "already in use" not in str(exc):
            raise
        info = await js.stream_info(PLUGIN_RUNNER_STREAM)
        for field, wanted in {
            "retention": config.retention,
            "max_age": config.max_age,
            "duplicate_window": config.duplicate_window,
        }.items():
            actual = getattr(info.config, field, None)
            if actual is None:
                continue
            actual_value = getattr(actual, "value", actual)
            wanted_value = getattr(wanted, "value", wanted)
            if actual_value != wanted_value:
                raise RuntimeError(
                    f"JetStream stream {PLUGIN_RUNNER_STREAM} policy mismatch: "
                    f"{field}={actual_value!r}, expected {wanted_value!r}"
                )
        existing = set(str(item) for item in (info.config.subjects or []))
        if existing != set(config.subjects):
            config.subjects = sorted(existing | set(config.subjects))
            await js.update_stream(config)


class PluginRunnerConsumer:
    """NATS pull consumer with bounded ack/nak semantics."""

    def __init__(self, service: PluginRunnerService | None = None) -> None:
        self.service = service or PluginRunnerService()
        self._shutdown = asyncio.Event()

    def request_shutdown(self) -> None:
        self._shutdown.set()

    async def run(self, nats_url: str) -> None:
        import nats
        from nats.js.api import ConsumerConfig

        nc = await nats.connect(nats_url.strip(), connect_timeout=3)
        try:
            js = nc.jetstream()
            await ensure_plugin_runner_stream(js)
            subscription = await js.pull_subscribe(
                PLUGIN_RUNNER_SUBJECT, durable=PLUGIN_RUNNER_DURABLE,
                stream=PLUGIN_RUNNER_STREAM,
                config=ConsumerConfig(ack_wait=60),
            )
            while not self._shutdown.is_set():
                try:
                    messages = await subscription.fetch(batch=1, timeout=5)
                except asyncio.TimeoutError:
                    continue
                for msg in messages:
                    try:
                        payload = json.loads(msg.data.decode("utf-8"))
                        async def publish(event: PluginRunnerEventEnvelope) -> None:
                            await js.publish(
                                event.reply_subject,
                                json.dumps(event.to_payload(), ensure_ascii=False).encode("utf-8"),
                                headers={"Nats-Msg-Id": f"{event.request_id}:{event.event_seq}"},
                            )
                        valid = await self.service.handle(payload, publish)
                        # Invalid envelopes are poison messages.  Acking them
                        # deliberately prevents an infinite redelivery loop;
                        # valid messages are durably journaled before this ack.
                        await msg.ack()
                    except Exception:
                        logger.exception("plugin runner message failed; nak for retry")
                        await msg.nak()
        finally:
            await nc.drain()


async def _async_main() -> int:
    from app.shared.config import settings

    nats_url = str(settings.nats_url or "").strip()
    if not nats_url:
        logger.error("plugin runner requires NATS_URL")
        return 1
    consumer = PluginRunnerConsumer()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, consumer.request_shutdown)
        except (NotImplementedError, RuntimeError):
            pass
    await consumer.run(nats_url)
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from app.model_registry import import_all_models

    import_all_models()
    raise SystemExit(asyncio.run(_async_main()))


if __name__ == "__main__":
    main()
