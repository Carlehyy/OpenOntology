"""Durable agent-run lifecycle: idempotency, isolation, lease, replay, retry."""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.deps import get_current_user, get_db
from app.ontologies.agent_runtime import (
    conversation_service,
    run_execution,
    run_service,
    run_submission,
)
from app.ontologies.agent_runtime import schemas as S
from app.ontologies.agent_runtime.application_errors import (
    AgentRuntimeApplicationError,
)
from app.ontologies.agent_runtime.models import AgentConversation, AgentMessage
from app.ontologies.agent_runtime.run_models import AgentRun, AgentRunEvent
from app.ontologies.agent_runtime.run_router import router as run_router
from tests.conftest import TestSession


def _submit(
    db,
    ontology,
    user,
    message: str = "查询订单",
    *,
    run_id: str | None = None,
    conversation_id: str | None = None,
    stream: bool = True,
    dispatch: bool = False,
):
    return run_service.submit_run(
        db,
        ontology["id"],
        user,
        S.ChatRequest(
            message=message,
            run_id=run_id,
            conversation_id=conversation_id,
            stream=stream,
        ),
        dispatch=dispatch,
    )


def _fake_turn(*events):
    def turn(worker_db, ontology_id, user, question, **kwargs):
        execution = kwargs["execution"]
        assert execution.user_message_id
        assert execution.stream_model is True
        yield from events

    return turn


def _execute(db, run_id: str, turn_fn, *, use_db: bool = True):
    run_service.execute_run(
        run_id,
        db=db if use_db else None,
        session_factory=TestSession,
        run_turn_fn=turn_fn,
    )


def _event_types(db, run_id: str) -> list[str]:
    db.expire_all()
    return [
        row.event_type
        for row in (
            db.query(AgentRunEvent)
            .filter(AgentRunEvent.run_id == run_id)
            .order_by(AgentRunEvent.seq.asc())
            .all()
        )
    ]


def _wait_until(db, predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        db.expire_all()
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached before timeout")


def _add_llm_config(db, admin_user, name: str) -> None:
    from app.models.model_config import ModelConfig

    db.add(ModelConfig(
        id=str(uuid.uuid4()),
        name=name,
        provider="openai",
        config_type="llm",
        models=[f"{name}-model"],
        created_by=admin_user.id,
    ))
    db.commit()


# ------------------------------------------------------------ submit/idempotency


def test_submit_is_idempotent_and_conflicts_on_payload_change(
    db, admin_user, editor_user, ontology,
):
    first = _submit(db, ontology, admin_user, run_id="run-idem")
    assert first["status"] == "queued"
    assert first["lastSeq"] == 1

    repeated = _submit(db, ontology, admin_user, run_id="run-idem")
    assert repeated["runId"] == first["runId"]
    assert repeated["conversationId"] == first["conversationId"]
    assert (
        db.query(AgentRun).filter(AgentRun.id == "run-idem").count() == 1
    )
    assert (
        db.query(AgentMessage)
        .filter(AgentMessage.conversation_id == first["conversationId"])
        .count()
        == 1
    )

    with pytest.raises(AgentRuntimeApplicationError) as changed:
        _submit(db, ontology, admin_user, "另一个问题", run_id="run-idem")
    assert changed.value.kind == "conflict"

    with pytest.raises(AgentRuntimeApplicationError) as owner:
        _submit(db, ontology, editor_user, run_id="run-idem")
    assert owner.value.kind == "conflict"


def test_invalid_client_run_id_is_rejected(db, admin_user, ontology):
    with pytest.raises(AgentRuntimeApplicationError) as error:
        _submit(db, ontology, admin_user, run_id="bad id!")
    assert error.value.kind == "invalid"


def test_owner_isolation_for_get_cancel_and_stream(
    db, admin_user, editor_user, ontology,
):
    run = _submit(db, ontology, admin_user, run_id="run-owner")

    with pytest.raises(AgentRuntimeApplicationError) as get_error:
        run_service.get_run(db, ontology["id"], editor_user, run["runId"])
    assert get_error.value.kind == "forbidden"

    with pytest.raises(AgentRuntimeApplicationError) as cancel_error:
        run_service.cancel_run(db, ontology["id"], editor_user, run["runId"])
    assert cancel_error.value.kind == "forbidden"

    stream = run_service.stream_run_events(
        db, ontology["id"], editor_user, run["runId"],
    )
    with pytest.raises(AgentRuntimeApplicationError) as stream_error:
        next(stream)
    assert stream_error.value.kind == "forbidden"


# ------------------------------------------------------------ single-flight


def test_one_active_run_per_conversation(
    db, admin_user, ontology,
):
    first = _submit(db, ontology, admin_user, run_id="run-flight-1")

    with pytest.raises(AgentRuntimeApplicationError) as error:
        _submit(
            db,
            ontology,
            admin_user,
            run_id="run-flight-2",
            conversation_id=first["conversationId"],
        )
    assert error.value.kind == "conflict"
    assert (
        db.query(AgentRun)
        .filter(AgentRun.conversation_id == first["conversationId"])
        .count()
        == 1
    )

    _execute(db, first["runId"], _fake_turn(
        {"type": "answer", "content": "完成", "citations": [], "proposals": []},
    ))
    second = _submit(
        db,
        ontology,
        admin_user,
        run_id="run-flight-2",
        conversation_id=first["conversationId"],
    )
    assert second["status"] == "queued"


# --------------------------------------------------- execution and progress


def test_progress_and_disconnect_do_not_cancel_worker(
    db, admin_user, ontology,
):
    release = threading.Event()
    errors: list[BaseException] = []

    def turn(worker_db, ontology_id, user, question, **kwargs):
        yield {
            "type": "step",
            "tool": "search_objects",
            "arguments": {"querySpec": {"objectType": "Order"}},
            "summary": "命中 1 条",
            "durationMs": 4,
            "result": {
                "kind": "table",
                "rows": [{"id": "o-1"}],
                "cursor": "remote-cursor",
            },
        }
        yield {"type": "delta", "content": "部分答案", "reset": True}
        release.wait(5)
        yield {
            "type": "answer",
            "content": "最终答案",
            "citations": [],
            "proposals": [],
            "usage": {"inputTokens": 1, "outputTokens": 2},
        }

    def worker() -> None:
        try:
            run_service.execute_run(
                "run-progress",
                session_factory=TestSession,
                run_turn_fn=turn,
            )
        except BaseException as error:  # noqa: BLE001 — surfaced by the test
            errors.append(error)

    run = _submit(db, ontology, admin_user, run_id="run-progress")
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        _wait_until(
            db,
            lambda: (
                run_service.get_run(
                    db, ontology["id"], admin_user, run["runId"],
                )["content"]
                == "部分答案"
            ),
        )
        snapshot = run_service.get_run(
            db, ontology["id"], admin_user, run["runId"],
        )
        assert snapshot["status"] == "running"
        assert len(snapshot["steps"]) == 1
        assert snapshot["steps"][0]["tool"] == "search_objects"
        assert "cursor" not in snapshot["steps"][0]["result"]

        stream = run_service.stream_run_events(
            db, ontology["id"], admin_user, run["runId"],
        )
        first = next(stream)
        assert first["type"] == "snapshot"
        assert first["snapshot"]["content"] == "部分答案"
        saw_delta = any(
            event.get("type") == "delta" and event.get("content") == "部分答案"
            for event in stream
        )
        assert saw_delta
        stream.close()  # client disconnect must not cancel the turn
    finally:
        release.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert errors == []
    final = run_service.get_run(db, ontology["id"], admin_user, run["runId"])
    assert final["status"] == "succeeded"
    assert final["content"] == "最终答案"
    assert final["error"] is None
    assert final["lastSeq"] == len(
        db.query(AgentRunEvent)
        .filter(AgentRunEvent.run_id == run["runId"])
        .all()
    )


def test_stream_replays_after_seq_and_closes_on_terminal(
    db, admin_user, ontology,
):
    run = _submit(db, ontology, admin_user, run_id="run-replay")
    _execute(db, run["runId"], _fake_turn(
        {
            "type": "step",
            "tool": "aggregate_objects",
            "arguments": {"querySpec": {"metric": "count"}},
            "summary": "共 2 条",
            "durationMs": 8,
            "result": {"kind": "metric", "value": 2, "cursor": "drop-me"},
        },
        {"type": "delta", "content": "答", "reset": True},
        {
            "type": "answer",
            "content": "答案",
            "citations": [{"id": "o-1"}],
            "proposals": [],
            "usage": {"inputTokens": 3, "outputTokens": 4},
        },
    ))

    all_events = list(run_service.stream_run_events(
        db, ontology["id"], admin_user, run["runId"], after_seq=0,
    ))
    assert all_events[0]["type"] == "snapshot"
    assert all_events[0]["snapshot"]["status"] == "succeeded"
    replayed = all_events[1:]
    assert [event["seq"] for event in replayed] == list(
        range(1, all_events[0]["snapshot"]["lastSeq"] + 1)
    )
    assert replayed[-1]["type"] == "done"
    assert all(
        event["id"] == f"{run['runId']}:{event['seq']}"
        for event in replayed
    )

    after_first = list(run_service.stream_run_events(
        db, ontology["id"], admin_user, run["runId"], after_seq=1,
    ))
    assert after_first[0]["type"] == "snapshot"
    assert all(event["seq"] > 1 for event in after_first[1:])


# ------------------------------------------------------------ cancel/retry


def test_cancel_queued_run_is_terminal_and_blocks_execution(
    db, admin_user, ontology,
):
    run = _submit(db, ontology, admin_user, run_id="run-cancel-queued")
    cancelled = run_service.cancel_run(
        db, ontology["id"], admin_user, run["runId"],
    )
    assert cancelled["cancelled"] is True
    assert cancelled["status"] == "cancelled"
    last_seq = cancelled["lastSeq"]
    assert _event_types(db, run["runId"]) == ["meta", "cancelled", "done"]

    _execute(db, run["runId"], _fake_turn(
        {"type": "answer", "content": "不应执行", "citations": [], "proposals": []},
    ))
    unchanged = run_service.get_run(
        db, ontology["id"], admin_user, run["runId"],
    )
    assert unchanged["status"] == "cancelled"
    assert unchanged["lastSeq"] == last_seq
    assert unchanged["content"] == ""

    again = run_service.cancel_run(
        db, ontology["id"], admin_user, run["runId"],
    )
    assert again["cancelled"] is False


def test_cancel_running_run_stops_worker_and_keeps_terminal_once(
    db, admin_user, ontology,
):
    cancel_seen = threading.Event()
    allow_finish = threading.Event()

    def turn(worker_db, ontology_id, user, question, **kwargs):
        execution = kwargs["execution"]
        yield {"type": "delta", "content": "草稿", "reset": True}
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not execution.is_cancelled():
            time.sleep(0.02)
        cancel_seen.set()
        assert allow_finish.wait(5)
        yield {"type": "cancelled"}

    errors: list[BaseException] = []

    def worker() -> None:
        try:
            run_service.execute_run(
                "run-cancel-running",
                session_factory=TestSession,
                run_turn_fn=turn,
            )
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    run = _submit(db, ontology, admin_user, run_id="run-cancel-running")
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    _wait_until(
        db,
        lambda: run_service.get_run(
            db, ontology["id"], admin_user, run["runId"],
        )["content"] == "草稿",
    )
    result = run_service.cancel_run(
        db, ontology["id"], admin_user, run["runId"],
    )
    # A running cancel only records the request; the worker still owns the
    # terminal transition (and therefore the single-flight guard).
    assert result["cancelled"] is True
    assert result["status"] == "running"
    assert result["cancelRequestedAt"] is not None
    assert cancel_seen.wait(5)

    allow_finish.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []

    types = _event_types(db, run["runId"])
    assert types.count("cancelled") == 1
    assert types.count("done") == 1
    assert "answer" not in types
    final = run_service.get_run(db, ontology["id"], admin_user, run["runId"])
    assert final["status"] == "cancelled"


def test_worker_stops_on_expired_lease_without_terminal_write(
    db, admin_user, ontology,
):
    lease_seen = threading.Event()
    proceed = threading.Event()

    def turn(worker_db, ontology_id, user, question, **kwargs):
        yield {"type": "delta", "content": "first", "reset": True}
        lease_seen.set()
        assert proceed.wait(5)
        yield {"type": "delta", "content": "second"}
        yield {
            "type": "answer",
            "content": "must not be written",
            "citations": [],
            "proposals": [],
        }

    run = _submit(db, ontology, admin_user, run_id="run-lease-self-stop")
    thread = threading.Thread(
        target=run_service.execute_run,
        kwargs={
            "run_id": run["runId"],
            "session_factory": TestSession,
            "run_turn_fn": turn,
        },
        daemon=True,
    )
    thread.start()
    assert lease_seen.wait(5)

    other = TestSession()
    try:
        row = other.get(AgentRun, run["runId"])
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        other.commit()
    finally:
        other.close()

    proceed.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    db.expire_all()
    # The worker must not forge a terminal state after losing its lease.
    assert run_service.get_run(
        db, ontology["id"], admin_user, run["runId"],
    )["status"] == "running"

    counts = run_service.reconcile_runs(
        db=db, now=datetime.now(timezone.utc),
    )
    assert counts["interrupted"] == 1
    assert run_service.get_run(
        db, ontology["id"], admin_user, run["runId"],
    )["status"] == "interrupted"


def test_incremental_writes_require_current_unexpired_lease(
    db, admin_user, ontology,
):
    from app.ontologies.agent_runtime import run_execution
    from app.ontologies.agent_runtime.run_events import DeltaAccumulator

    run = _submit(db, ontology, admin_user, run_id="run-lease-fence")
    row = db.get(AgentRun, run["runId"])
    row.status = "running"
    row.lease_owner = "new-owner"
    row.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    row.content = "new-owner-content"
    row.model = "new-model"
    db.commit()

    assert run_execution._record_step(
        db, run["runId"], "stale-owner", [{"tool": "late"}],
    ) is False
    assert run_execution._record_meta(
        db, run["runId"], "stale-owner", {"model": "late-model"},
    ) is False
    delta = DeltaAccumulator()
    delta.add("late-content", reset=True)
    assert run_execution._flush_delta(
        db, run["runId"], "stale-owner", delta,
    ) is False
    db.expire_all()
    row = db.get(AgentRun, run["runId"])
    assert row.steps == []
    assert row.model == "new-model"
    assert row.content == "new-owner-content"

    row.lease_owner = "expired-owner"
    row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    db.commit()
    assert run_execution._record_step(
        db, run["runId"], "expired-owner", [{"tool": "late"}],
    ) is False
    db.expire_all()
    assert db.get(AgentRun, run["runId"]).steps == []


def test_retry_reuses_original_user_message(db, admin_user, ontology):
    run = _submit(db, ontology, admin_user, run_id="run-retry-source")
    _execute(db, run["runId"], _fake_turn(
        {"type": "error", "message": "provider down"},
    ))
    source = run_service.get_run(db, ontology["id"], admin_user, run["runId"])
    assert source["status"] == "error"
    messages_before = (
        db.query(AgentMessage)
        .filter(AgentMessage.conversation_id == source["conversationId"])
        .count()
    )

    retry = run_service.retry_run(
        db, ontology["id"], admin_user, run["runId"], dispatch=False,
    )
    assert retry["retryOf"] == run["runId"]
    assert retry["question"] == source["question"]
    assert retry["conversationId"] == source["conversationId"]
    assert (
        db.query(AgentMessage)
        .filter(AgentMessage.conversation_id == source["conversationId"])
        .count()
        == messages_before
    )
    source_row = db.get(AgentRun, run["runId"])
    retry_row = db.get(AgentRun, retry["runId"])
    assert retry_row.user_message_id == source_row.user_message_id

    with pytest.raises(AgentRuntimeApplicationError) as active:
        run_service.retry_run(
            db, ontology["id"], admin_user, retry["runId"],
        )
    assert active.value.kind == "conflict"

    _execute(db, retry["runId"], _fake_turn(
        {"type": "answer", "content": "重试成功", "citations": [], "proposals": []},
    ))
    assert run_service.get_run(
        db, ontology["id"], admin_user, retry["runId"],
    )["status"] == "succeeded"


def test_admin_cannot_retry_another_users_run(
    db, admin_user, editor_user, ontology,
):
    editor_run = _submit(
        db, ontology, editor_user, run_id="run-editor-retry",
    )
    with pytest.raises(AgentRuntimeApplicationError) as denied:
        run_service.retry_run(
            db, ontology["id"], admin_user, editor_run["runId"],
        )
    assert denied.value.kind == "forbidden"


def test_finish_failure_rolls_back_pending_assistant(
    db, admin_user, ontology,
):
    ready = threading.Event()
    proceed = threading.Event()

    def turn(worker_db, ontology_id, user, question, **kwargs):
        conversation_id = kwargs["conversation_id"]
        # Simulate the core adding its final assistant message without commit.
        worker_db.add(AgentMessage(
            conversation_id=conversation_id,
            role="assistant",
            content="迟到回答",
        ))
        ready.set()
        assert proceed.wait(5)
        yield {
            "type": "answer",
            "content": "迟到回答",
            "citations": [],
            "proposals": [],
        }

    run = _submit(db, ontology, admin_user, run_id="run-atomic-finish")
    thread = threading.Thread(
        target=run_service.execute_run,
        kwargs={
            "run_id": run["runId"],
            "session_factory": TestSession,
            "run_turn_fn": turn,
        },
        daemon=True,
    )
    thread.start()
    assert ready.wait(5)

    other = TestSession()
    try:
        row = other.get(AgentRun, run["runId"])
        row.status = "interrupted"
        row.lease_owner = None
        row.lease_expires_at = None
        row.finished_at = datetime.now(timezone.utc)
        row.error = "simulated lease loss"
        other.commit()
    finally:
        other.close()

    proceed.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    db.expire_all()
    final = run_service.get_run(db, ontology["id"], admin_user, run["runId"])
    assert final["status"] == "interrupted"
    # The terminal CAS lost, so the pending assistant insert must roll back.
    assert (
        db.query(AgentMessage)
        .filter(AgentMessage.conversation_id == run["conversationId"])
        .count()
        == 1
    )


def test_execute_revalidates_inactive_user(db, admin_user, ontology):
    run = _submit(db, ontology, admin_user, run_id="run-inactive-user")
    admin_user.is_active = False
    db.commit()
    executed: list[bool] = []

    def turn(worker_db, ontology_id, user, question, **kwargs):
        executed.append(True)
        yield {"type": "answer", "content": "不应执行", "citations": [], "proposals": []}

    _execute(db, run["runId"], turn)
    assert executed == []
    snapshot = run_service.get_run(db, ontology["id"], admin_user, run["runId"])
    assert snapshot["status"] == "error"
    assert "停用" in snapshot["error"]


def test_execute_revalidates_menu_access(db, editor_user, ontology):
    from app.auth.models import RoleMenuPermission

    db.add(RoleMenuPermission(role="editor", menu_keys=["overview"]))
    db.commit()
    run = _submit(db, ontology, editor_user, run_id="run-menu-revoked")
    executed: list[bool] = []

    def turn(worker_db, ontology_id, user, question, **kwargs):
        executed.append(True)
        yield {"type": "answer", "content": "不应执行", "citations": [], "proposals": []}

    _execute(db, run["runId"], turn)
    assert executed == []
    snapshot = run_service.get_run(
        db, ontology["id"], editor_user, run["runId"],
    )
    assert snapshot["status"] == "error"
    assert "智能体访问权限" in snapshot["error"]


def test_real_orchestrator_history_and_no_duplicate_messages(
    db, admin_user, ontology, monkeypatch,
):
    from app.ontologies.agent_runtime import llm_bridge

    _add_llm_config(db, admin_user, "run-history-fake")
    calls: list[list[dict]] = []
    answers = iter(["第一轮回答", "第二轮回答"])

    def fake_chat(call_kwargs, messages, tools):
        calls.append([dict(message) for message in messages])
        return {
            "content": next(answers),
            "tool_calls": [],
            "usage": {"inputTokens": 5, "outputTokens": 3},
        }

    monkeypatch.setattr(llm_bridge, "chat", fake_chat)

    first = _submit(
        db, ontology, admin_user, "第一轮问题",
        run_id="run-real-1", stream=False,
    )
    run_service.execute_run(
        first["runId"], db=db, session_factory=TestSession,
        stream_model=False,
    )
    first_done = run_service.get_run(
        db, ontology["id"], admin_user, first["runId"],
    )
    assert first_done["status"] == "succeeded"
    assert first_done["content"] == "第一轮回答"
    assert (
        db.query(AgentMessage)
        .filter(AgentMessage.conversation_id == first["conversationId"])
        .count()
        == 2
    )

    second = _submit(
        db, ontology, admin_user, "第二轮问题",
        run_id="run-real-2",
        conversation_id=first["conversationId"],
        stream=False,
    )
    run_service.execute_run(
        second["runId"], db=db, session_factory=TestSession,
        stream_model=False,
    )
    second_done = run_service.get_run(
        db, ontology["id"], admin_user, second["runId"],
    )
    assert second_done["status"] == "succeeded"
    assert second_done["content"] == "第二轮回答"
    assert len(calls) == 2

    second_messages = calls[1]
    user_texts = [
        message["content"]
        for message in second_messages
        if message["role"] == "user"
    ]
    assistant_texts = [
        message["content"]
        for message in second_messages
        if message["role"] == "assistant"
    ]
    assert user_texts.count("第二轮问题") == 1
    assert "第一轮问题" in user_texts
    assert "第一轮回答" in assistant_texts
    assert (
        db.query(AgentMessage)
        .filter(AgentMessage.conversation_id == first["conversationId"])
        .count()
        == 4
    )


def test_cleanup_expired_delta_events_keeps_snapshot(
    db, admin_user, ontology,
):
    run = _submit(db, ontology, admin_user, run_id="run-retention")
    old = datetime.now(timezone.utc) - timedelta(hours=48)
    recent = datetime.now(timezone.utc)
    db.add_all([
        AgentRunEvent(
            run_id=run["runId"], seq=40, event_type="delta",
            payload={"content": "old"}, created_at=old,
        ),
        AgentRunEvent(
            run_id=run["runId"], seq=41, event_type="step",
            payload={"tool": "search_objects"}, created_at=old,
        ),
        AgentRunEvent(
            run_id=run["runId"], seq=42, event_type="delta",
            payload={"content": "new"}, created_at=recent,
        ),
    ])
    row = db.get(AgentRun, run["runId"])
    row.content = "快照内容"
    row.last_seq = 42
    db.commit()

    deleted = run_service.cleanup_expired_delta_events(db, now=recent)
    assert deleted == 1
    db.expire_all()
    remaining = (
        db.query(AgentRunEvent)
        .filter(AgentRunEvent.run_id == run["runId"])
        .order_by(AgentRunEvent.seq.asc())
        .all()
    )
    assert [(event.seq, event.event_type) for event in remaining] == [
        (1, "meta"),
        (41, "step"),
        (42, "delta"),
    ]
    snapshot = run_service.get_run(db, ontology["id"], admin_user, run["runId"])
    assert snapshot["content"] == "快照内容"


def test_cancel_queued_read_raced_with_claim_requests_running_cancel(
    db, admin_user, ontology, monkeypatch,
):
    run = _submit(db, ontology, admin_user, run_id="run-cancel-claim-race")
    row = db.get(AgentRun, run["runId"])
    row.status = "running"
    row.lease_owner = "worker-race"
    row.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    row.started_at = datetime.now(timezone.utc)
    db.commit()

    real_require = run_submission.require_run
    calls = {"count": 0}

    def stale_queued_then_real(db_session, oid, user, rid):
        calls["count"] += 1
        if calls["count"] == 1:
            # Deterministic race: cancel read the pre-claim queued state, but
            # the worker committed running before cancel's queued CAS.
            return SimpleNamespace(id=run["runId"], status="queued")
        return real_require(db_session, oid, user, rid)

    monkeypatch.setattr(
        run_submission, "require_run", stale_queued_then_real,
    )

    result = run_service.cancel_run(
        db, ontology["id"], admin_user, run["runId"],
    )
    assert calls["count"] == 2
    assert result["cancelled"] is True
    assert result["status"] == "running"
    assert result["cancelRequestedAt"] is not None

    # The worker then reaches a success/error terminal after the missed
    # cancel; the cancel flag wins and the terminal must be cancelled.
    finished = run_execution._finish_run(
        db, run["runId"], "worker-race",
        status="error",
        error="late error",
        event_type="error",
        event_payload={"message": "late error"},
        steps=[],
    )
    assert finished is True
    final = run_service.get_run(
        db, ontology["id"], admin_user, run["runId"],
    )
    assert final["status"] == "cancelled"
    assert final["cancelRequestedAt"] is not None
    assert _event_types(db, run["runId"]) == ["meta", "cancelled", "done"]


def test_cancel_before_answer_yield_wins_terminal_race(
    db, admin_user, ontology,
):
    answer_ready = threading.Event()
    proceed = threading.Event()
    errors: list[BaseException] = []

    def turn(worker_db, ontology_id, user, question, **kwargs):
        conversation_id = kwargs["conversation_id"]
        # Core has already queued its final assistant message before yielding.
        worker_db.add(AgentMessage(
            conversation_id=conversation_id,
            role="assistant",
            content="竞态回答",
        ))
        answer_ready.set()
        assert proceed.wait(5)
        yield {
            "type": "answer",
            "content": "竞态回答",
            "citations": [],
            "proposals": [],
        }

    def worker() -> None:
        try:
            run_service.execute_run(
                "run-cancel-answer-race",
                session_factory=TestSession,
                run_turn_fn=turn,
            )
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    run = _submit(db, ontology, admin_user, run_id="run-cancel-answer-race")
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert answer_ready.wait(5)

    result = run_service.cancel_run(
        db, ontology["id"], admin_user, run["runId"],
    )
    assert result["cancelled"] is True
    assert result["status"] == "running"

    proceed.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []
    final = run_service.get_run(
        db, ontology["id"], admin_user, run["runId"],
    )
    assert final["status"] == "cancelled"
    types = _event_types(db, run["runId"])
    assert "answer" not in types
    assert types.count("cancelled") == 1
    assert types.count("done") == 1
    messages = (
        db.query(AgentMessage)
        .filter(AgentMessage.conversation_id == run["conversationId"])
        .order_by(AgentMessage.created_at.asc())
        .all()
    )
    assert [message.content for message in messages] == [
        "查询订单",
        "[已取消]",
    ]


def _query_spec() -> dict:
    spec = {
        "version": 1,
        "shape": "detail",
        "objectType": "Order",
        "query": {
            "filters": [{
                "field": "status",
                "op": "eq",
                "value": "x" * 12_000,
            }],
        },
        "description": {
            "semantics": "detail",
            "text": "y" * 4_000,
        },
        "pageSize": 50,
    }
    from app.ontologies.agent_runtime.run_events import MAX_QUERY_SPEC_BYTES

    assert len(json.dumps(spec, ensure_ascii=False)) < MAX_QUERY_SPEC_BYTES
    return spec


def test_large_legal_query_spec_survives_event_run_and_replay(
    db, admin_user, ontology,
):
    spec = _query_spec()
    result = {
        "kind": "ontology_query_view",
        "display": "query_view",
        "querySpec": spec,
        "columns": [{"name": "order_no", "type": "string"}],
        "returned": 500,
        "counts": {"matched": 500},
        "hasNext": True,
        "window": {"start": "2026-01-01", "end": "2026-12-31"},
        "consistency": {"state": "final"},
        "drilldown": {"base": "token"},
        "drilldownBase": "token",
        "drilldownLabels": ["订单"],
        "rows": [
            {"order_no": str(index), "payload": "x" * 120}
            for index in range(500)
        ],
        "chart": {"series": [{"data": list(range(500))}]},
    }
    step = {
        "type": "step",
        "tool": "query_objects",
        "arguments": {"querySpec": spec, "objectType": "Order"},
        "summary": "500 条",
        "durationMs": 12,
        "result": result,
    }
    run = _submit(db, ontology, admin_user, run_id="run-large-spec")
    _execute(db, run["runId"], _fake_turn(
        step,
        {"type": "answer", "content": "完成", "citations": [], "proposals": []},
    ))

    snapshot = run_service.get_run(db, ontology["id"], admin_user, run["runId"])
    assert snapshot["status"] == "succeeded"
    persisted = snapshot["steps"][0]
    assert persisted["arguments"]["querySpec"] == spec
    assert persisted["result"]["querySpec"] == spec
    assert persisted["result"]["window"]["start"] == "2026-01-01"
    assert persisted["result"]["consistency"]["state"] == "final"
    assert persisted["result"]["drilldown"]["base"] == "token"
    assert "rows" not in persisted["result"]
    assert "chart" not in persisted["result"]

    replayed = list(run_service.stream_run_events(
        db, ontology["id"], admin_user, run["runId"], after_seq=0,
    ))
    step_event = next(event for event in replayed if event["type"] == "step")
    assert step_event["result"]["querySpec"] == spec
    assert step_event["result"]["window"] == result["window"]
    assert step_event["arguments"]["querySpec"] == spec


def _window_step(*, token_size: int = 200) -> dict:
    query_spec = {"version": 1, "shape": "objects",
                  "query": {"sources": [], "aggregates": []}}
    module_spec = {"version": 1, "shape": "objects",
                   "query": {"sources": [], "groupBy": []}}
    modules = [
        {"module": f"module-{index:03d}",
         "moduleValues": [f"module-{index:03d}"],
         "qualifyingWindows": index,
         "maxDistinctSubjects": index % 30}
        for index in range(80)
    ]
    samples = [
        {"module": f"module-{index:03d}",
         "moduleValues": [f"module-{index:03d}"],
         "windowStart": "2026-09-10T17:30:00+08:00",
         "windowEnd": "2026-09-10T18:00:00+08:00",
         "windowStartUtc": "2026-09-10T09:30:00+00:00",
         "windowEndUtc": "2026-09-10T10:00:00+00:00",
         "distinctSubjects": 7,
         "candidateRows": 9,
         "drilldown": {"token": "t" * token_size}}
        for index in range(20)
    ]
    result = {
        "kind": "sequence_analysis",
        "mode": "window_distinct",
        "objectType": "事件",
        "subjectProperty": "actor",
        "timeProperty": "occurred_at",
        "window": {"property": "occurred_at", "timezone": "Asia/Shanghai",
                   "start": "2026-09-01T00:00:00+08:00",
                   "end": "2026-10-01T00:00:00+08:00"},
        "windowMinutes": 30,
        "windowBucket": "half_hour",
        "windowTimezone": "Asia/Shanghai",
        "windowAlignment": "half-hour tumbling windows on UTC/source timestamps",
        "threshold": 5,
        "thresholdScope": "distinct_subjects_per_fixed_window",
        "qualifyingWindows": 2757,
        "qualifyingModules": 144,
        "moduleCount": 144,
        "modules": modules,
        "modulesTruncated": True,
        "moduleLimit": 10,
        "sampleWindows": samples,
        "sampleWindowsTruncated": True,
        "evaluatedWindows": None,
        "evaluatedWindowsAvailable": False,
        "exact": True,
        "partial": False,
        "completeness": "complete",
        "completenessScope": "query_execution",
        "dataMode": "remote",
        "querySpec": query_spec,
        "moduleQuerySpec": module_spec,
        "querySpecHash": "window-spec-hash",
        "moduleQuerySpecHash": "window-module-spec-hash",
        "observedAt": "2026-11-05T00:00:00+00:00",
        "consistency": {"sameSnapshot": False,
                        "mode": "window_distinct_multi_query"},
        "limitations": ["multi_query_observation_not_single_snapshot"],
        "note": "qualifyingWindows/qualifyingModules 为源端精确聚合结果",
        # Raw source detail must never be persisted by the run ledger compaction.
        "rows": [{"raw": "source-row"}],
        "sourceRows": [{"raw": "source-row"}],
        "rawRows": [{"raw": "source-row"}],
    }
    return {
        "type": "step",
        "tool": "analyze_sequences",
        "arguments": {"querySpec": query_spec,
                      "moduleQuerySpec": module_spec,
                      "objectType": "事件"},
        "summary": "窗口统计",
        "durationMs": 1,
        "result": result,
    }


def test_bounded_step_event_window_drops_raw_source_rows_below_cap(monkeypatch):
    from app.ontologies.agent_runtime.run_events import bounded_step_event

    monkeypatch.setenv("AGENT_RUN_EVENT_PAYLOAD_CAP", "100000")
    event = _window_step(token_size=10)
    assert len(json.dumps(event, ensure_ascii=False)) <= 100000

    payload = bounded_step_event(event)
    result = payload["result"]
    assert result["qualifyingWindows"] == 2757
    assert result["modulesTruncated"] is True
    assert len(result["modules"]) == 80
    assert len(result["sampleWindows"]) == 20
    for key in ("rows", "sourceRows", "rawRows", "remoteRows", "raw"):
        assert key not in result


def test_bounded_step_event_oversized_window_keeps_exact_totals_and_preview(
        monkeypatch,
):
    from app.ontologies.agent_runtime.run_events import bounded_step_event

    monkeypatch.setenv("AGENT_RUN_EVENT_PAYLOAD_CAP", "12000")
    event = _window_step()
    assert len(json.dumps(event, ensure_ascii=False)) > 12000

    payload = bounded_step_event(event)
    assert len(json.dumps(payload, ensure_ascii=False)) <= 12000
    result = payload["result"]

    assert result["mode"] == "window_distinct"
    assert result["qualifyingWindows"] == 2757
    assert result["qualifyingModules"] == 144
    assert result["moduleCount"] == 144
    assert result["modulesTruncated"] is True
    assert result["moduleLimit"] == 10
    assert result["sampleWindowsTruncated"] is True
    assert result["windowMinutes"] == 30
    assert result["windowBucket"] == "half_hour"
    assert result["windowTimezone"] == "Asia/Shanghai"
    assert result["windowAlignment"] == (
        "half-hour tumbling windows on UTC/source timestamps")
    assert result["_truncated"] is True
    assert len(result["modules"]) == 10
    assert len(result["sampleWindows"]) == 5
    assert result["omittedItems"] == {"modules": 70, "sampleWindows": 15}
    assert result["querySpec"] == event["result"]["querySpec"]
    assert result["moduleQuerySpec"] == event["result"]["moduleQuerySpec"]
    assert result["moduleQuerySpecHash"] == "window-module-spec-hash"
    assert payload["arguments"]["moduleQuerySpec"] == \
        event["result"]["moduleQuerySpec"]
    for key in ("rows", "sourceRows", "rawRows", "remoteRows"):
        assert key not in result


def test_bounded_step_event_window_shrink_keeps_totals_and_honest_omissions(
        monkeypatch,
):
    from app.ontologies.agent_runtime.run_events import bounded_step_event

    monkeypatch.setenv("AGENT_RUN_EVENT_PAYLOAD_CAP", "8000")
    event = _window_step(token_size=1200)
    assert len(json.dumps(event, ensure_ascii=False)) > 8000

    payload = bounded_step_event(event)
    assert len(json.dumps(payload, ensure_ascii=False)) <= 8000
    result = payload["result"]

    assert result["qualifyingWindows"] == 2757
    assert result["qualifyingModules"] == 144
    assert result["moduleCount"] == 144
    assert result["modulesTruncated"] is True
    assert result["sampleWindowsTruncated"] is True
    assert result["_truncated"] is True
    assert result["omittedItems"] == {"modules": 80, "sampleWindows": 20}
    assert "modules" not in result
    assert "sampleWindows" not in result
    assert result["querySpec"] == event["result"]["querySpec"]
    assert result["moduleQuerySpec"] == event["result"]["moduleQuerySpec"]
    assert result["moduleQuerySpecHash"] == "window-module-spec-hash"
    for key in ("rows", "sourceRows", "rawRows", "remoteRows"):
        assert key not in result


def test_oversized_query_spec_fails_explicitly(
    db, admin_user, ontology,
):
    from app.ontologies.agent_runtime.run_events import MAX_QUERY_SPEC_BYTES

    spec = {"description": {"text": "x" * (MAX_QUERY_SPEC_BYTES + 1_000)}}
    step = {
        "type": "step",
        "tool": "query_objects",
        "arguments": {"querySpec": spec},
        "summary": " Oversized ",
        "durationMs": 1,
        "result": {"kind": "ontology_query_view", "querySpec": spec},
    }
    run = _submit(db, ontology, admin_user, run_id="run-oversized-spec")
    _execute(db, run["runId"], _fake_turn(
        step,
        {"type": "answer", "content": "不应成功", "citations": [], "proposals": []},
    ))
    snapshot = run_service.get_run(db, ontology["id"], admin_user, run["runId"])
    assert snapshot["status"] == "error"
    assert "querySpec" in snapshot["error"]
    types = _event_types(db, run["runId"])
    assert "step" not in types
    assert "answer" not in types
    assert types[-2:] == ["error", "done"]


# ------------------------------------------------------------ reconcile/lease


def test_reconcile_redispatches_queued_without_failing_it(
    db, admin_user, ontology,
):
    run = _submit(db, ontology, admin_user, run_id="run-reconcile-queued")
    dispatched: list[str] = []
    counts = run_service.reconcile_runs(
        db=db, dispatch_run=dispatched.append,
    )
    assert counts == {"interrupted": 0, "redispatched": 1}
    assert dispatched == [run["runId"]]
    assert run_service.get_run(
        db, ontology["id"], admin_user, run["runId"],
    )["status"] == "queued"


def test_reconcile_interrupts_expired_lease_only(
    db, admin_user, ontology,
):
    expired = _submit(db, ontology, admin_user, run_id="run-lease-dead")
    row = db.get(AgentRun, expired["runId"])
    row.status = "running"
    row.lease_owner = "dead-worker"
    row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    db.commit()

    counts = run_service.reconcile_runs(
        db=db, now=datetime.now(timezone.utc),
    )
    assert counts["interrupted"] == 1
    snapshot = run_service.get_run(
        db, ontology["id"], admin_user, expired["runId"],
    )
    assert snapshot["status"] == "interrupted"
    assert "租约" in snapshot["error"]
    types = _event_types(db, expired["runId"])
    assert types[-2:] == ["error", "done"]


def test_reconcile_leaves_live_lease_running(
    db, admin_user, ontology,
):
    live = _submit(db, ontology, admin_user, run_id="run-lease-live")
    row = db.get(AgentRun, live["runId"])
    row.status = "running"
    row.lease_owner = "live-worker"
    row.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    db.commit()

    counts = run_service.reconcile_runs(
        db=db, now=datetime.now(timezone.utc),
    )
    assert counts == {"interrupted": 0, "redispatched": 0}
    assert run_service.get_run(
        db, ontology["id"], admin_user, live["runId"],
    )["status"] == "running"


# ------------------------------------------------------------ conversation delete


def test_delete_conversation_guards_active_run_and_cleans_terminal_ledger(
    db, admin_user, ontology,
):
    run = _submit(db, ontology, admin_user, run_id="run-delete")
    conversation = db.get(AgentConversation, run["conversationId"])
    with pytest.raises(AgentRuntimeApplicationError) as active:
        conversation_service.delete_conversation(db, conversation)
    assert active.value.kind == "conflict"

    run_service.cancel_run(db, ontology["id"], admin_user, run["runId"])
    conversation = db.get(AgentConversation, run["conversationId"])
    conversation_service.delete_conversation(db, conversation)

    assert db.query(AgentRun).filter(AgentRun.id == run["runId"]).count() == 0
    assert (
        db.query(AgentRunEvent)
        .filter(AgentRunEvent.run_id == run["runId"])
        .count()
        == 0
    )
    assert (
        db.query(AgentMessage)
        .filter(AgentMessage.conversation_id == run["conversationId"])
        .count()
        == 0
    )


# ------------------------------------------------------------ HTTP router


def test_run_router_submit_and_sse_frames(
    db, admin_user, ontology,
):
    app = FastAPI()
    app.include_router(
        run_router, prefix="/api/v2/formal/ontologies",
    )

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: admin_user

    with TestClient(app) as client:
        response = client.post(
            f"/api/v2/formal/ontologies/{ontology['id']}/agent/chat/runs",
            json={
                "message": "查询订单",
                "runId": "run-api",
                "stream": False,
            },
        )
        assert response.status_code == 200, response.text
        run = response.json()["data"]
        assert run["runId"] == "run-api"
        assert run["status"] == "queued"

        _execute(db, run["runId"], _fake_turn(
            {
                "type": "answer",
                "content": "接口答案",
                "citations": [],
                "proposals": [],
            },
        ))

        events = client.get(
            f"/api/v2/formal/ontologies/{ontology['id']}"
            f"/agent/chat/runs/run-api/events",
        )
        assert events.status_code == 200, events.text
        body = events.text
        assert '"type": "snapshot"' in body
        assert "id: run-api:1" in body
        assert '"type": "done"' in body
