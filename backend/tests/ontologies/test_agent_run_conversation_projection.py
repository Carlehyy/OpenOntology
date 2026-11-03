"""Conversation list/detail run projections for refresh recovery."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.ontologies.agent_runtime import (
    conversation_service,
    run_conversations,
    run_service,
)
from app.ontologies.agent_runtime import schemas as S
from app.ontologies.agent_runtime.application_errors import (
    AgentRuntimeApplicationError,
)
from app.ontologies.agent_runtime.models import AgentConversation
from app.ontologies.agent_runtime.run_models import AgentRun
from sqlalchemy import event
from tests.conftest import QueryCounter, TestSession


def _submit(db, ontology, user, *, run_id: str, message: str = "查询订单"):
    return run_service.submit_run(
        db,
        ontology["id"],
        user,
        S.ChatRequest(message=message, run_id=run_id, stream=True),
        dispatch=False,
    )


def _execute(db, run_id: str, events: list[dict]) -> None:
    def turn(worker_db, ontology_id, user, question, **kwargs):
        yield from events

    run_service.execute_run(
        run_id,
        db=db,
        session_factory=TestSession,
        run_turn_fn=turn,
    )


def _conversation(db, ontology, user, *, sandbox: bool = False, title: str = "会话"):
    row = AgentConversation(
        ontology_id=ontology["id"],
        user_id=user.id,
        title=title,
        is_sandbox=sandbox,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _detail(db, ontology, user, conversation_id: str) -> dict:
    conversation = conversation_service.require_conversation(
        db, ontology["id"], conversation_id, user,
    )
    return conversation_service.get_conversation(db, conversation)


def _find(items: list[dict], conversation_id: str) -> dict | None:
    return next(
        (item for item in items if item["id"] == conversation_id),
        None,
    )


def _wait_until(db, predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        db.expire_all()
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached before timeout")


def test_list_batches_runs_without_n_plus_one(
    db, admin_user, ontology,
):
    first = _submit(db, ontology, admin_user, run_id="run-list-source")
    _execute(db, first["runId"], [
        {"type": "answer", "content": "完成", "citations": [], "proposals": []},
    ])
    retry = run_service.retry_run(
        db, ontology["id"], admin_user, first["runId"], dispatch=False,
    )
    empty = _conversation(db, ontology, admin_user, title="没有回合")
    sandbox = _conversation(
        db, ontology, admin_user, sandbox=True, title="[评估沙箱] 不可见",
    )

    statements: list[str] = []

    def _capture(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    bind = db.get_bind()
    event.listen(bind, "before_cursor_execute", _capture)
    try:
        with QueryCounter(db) as counter:
            items = conversation_service.list_conversations(
                db, ontology["id"], None, admin_user,
            )
        baseline = counter.count
        _conversation(db, ontology, admin_user, title="新增无回合会话")
        with QueryCounter(db) as second:
            items = conversation_service.list_conversations(
                db, ontology["id"], None, admin_user,
            )
    finally:
        event.remove(bind, "before_cursor_execute", _capture)

    # Adding a conversation must not add per-conversation queries, and the
    # run projection is one batched run-only query per list call (the
    # conversation query itself contains an activity subquery).
    assert second.count == baseline
    # Per list call: one conversation SELECT (with activity subquery), one
    # batched AgentRun window SELECT, plus the SQLite transaction BEGIN.
    assert baseline == 3
    run_only_queries = [
        statement for statement in statements
        if "fo_agent_runs" in statement
        and "fo_agent_conversations" not in statement
    ]
    assert len(run_only_queries) == 2

    source_item = _find(items, first["conversationId"])
    assert source_item is not None
    assert source_item["activeRun"]["runId"] == retry["runId"]
    assert source_item["latestRun"]["runId"] == retry["runId"]
    assert source_item["activeRun"]["retryOf"] == first["runId"]
    assert source_item["activeRun"]["status"] == "queued"
    assert source_item["updatedAt"] == source_item["latestRun"]["updatedAt"]

    empty_item = _find(items, empty.id)
    assert empty_item is not None
    assert empty_item["activeRun"] is None
    assert empty_item["latestRun"] is None
    assert _find(items, sandbox.id) is None


def _add_history_run(
    db, ontology, user, conversation_id: str, run_id: str,
    status: str, created_at,
) -> None:
    db.add(AgentRun(
        id=run_id,
        ontology_id=ontology["id"],
        release_id=None,
        conversation_id=conversation_id,
        user_id=user.id,
        user_message_id=f"message-{run_id}",
        model_id=None,
        model=None,
        status=status,
        question="历史问题",
        request_hash="history-hash",
        content="",
        steps=[],
        citations=[],
        proposals=[],
        stream_model=True,
        last_seq=0,
        created_at=created_at,
        updated_at=created_at,
    ))


def test_window_projection_returns_at_most_two_rows_per_conversation(
    db, admin_user, ontology,
):
    first = _conversation(db, ontology, admin_user, title="多历史 A")
    second = _conversation(db, ontology, admin_user, title="多历史 B")
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    for index in range(30):
        _add_history_run(
            db, ontology, admin_user, first.id,
            f"run-a-{index:02d}", "succeeded",
            base + timedelta(minutes=index),
        )
        _add_history_run(
            db, ontology, admin_user, second.id,
            f"run-b-{index:02d}", "succeeded",
            base + timedelta(minutes=index),
        )
    # Legacy shape: an older active row next to a newer terminal row.  Both
    # survive, but historical terminal rows in between must not be fetched.
    _add_history_run(
        db, ontology, admin_user, first.id,
        "run-a-active", "running",
        base + timedelta(minutes=5),
    )
    db.commit()

    rows = run_conversations.projection_rows(db, [first.id, second.id])
    assert len(rows) <= 4
    assert len(rows) == 3

    projections = run_conversations.run_projections(
        db, [first.id, second.id],
    )
    assert projections[first.id]["activeRun"]["runId"] == "run-a-active"
    assert projections[first.id]["latestRun"]["runId"] == "run-a-29"
    assert projections[second.id]["activeRun"] is None
    assert projections[second.id]["latestRun"]["runId"] == "run-b-29"


def test_activity_order_uses_max_of_conversation_and_run(
    db, admin_user, ontology,
):
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    conversation_a = _conversation(db, ontology, admin_user, title="会话较新")
    conversation_b = _conversation(db, ontology, admin_user, title="run 较新")
    _add_history_run(
        db, ontology, admin_user, conversation_a.id,
        "run-order-a", "succeeded", base,
    )
    _add_history_run(
        db, ontology, admin_user, conversation_b.id,
        "run-order-b", "succeeded", base,
    )
    run_b = db.get(AgentRun, "run-order-b")
    run_b.updated_at = base + timedelta(minutes=5)
    conversation_a_row = db.get(AgentConversation, conversation_a.id)
    conversation_a_row.updated_at = base + timedelta(minutes=10)
    conversation_b_row = db.get(AgentConversation, conversation_b.id)
    conversation_b_row.updated_at = base
    db.commit()

    items = conversation_service.list_conversations(
        db, ontology["id"], None, admin_user,
    )
    assert [item["id"] for item in items] == [
        conversation_a.id,
        conversation_b.id,
    ]
    item_a = _find(items, conversation_a.id)
    item_b = _find(items, conversation_b.id)
    assert item_a["updatedAt"] > item_b["updatedAt"]
    # conversation_a's run is older than its conversation timestamp; the
    # ordering/updatedAt must still use the newer conversation activity.
    assert item_a["updatedAt"] > item_a["latestRun"]["updatedAt"]


def test_detail_states_queued_running_terminal_retry(
    db, admin_user, ontology,
):
    run = _submit(db, ontology, admin_user, run_id="run-detail-states")
    detail = _detail(db, ontology, admin_user, run["conversationId"])
    assert detail["activeRun"]["runId"] == run["runId"]
    assert detail["activeRun"]["status"] == "queued"
    assert detail["latestRun"]["runId"] == run["runId"]

    row = db.get(AgentRun, run["runId"])
    row.status = "running"
    row.lease_owner = "test-worker"
    row.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    row.content = "部分回答"
    row.steps = [{"tool": "search_objects", "summary": "1 条"}]
    row.last_seq = 2
    db.commit()
    detail = _detail(db, ontology, admin_user, run["conversationId"])
    assert detail["activeRun"]["status"] == "running"
    assert detail["activeRun"]["content"] == "部分回答"
    assert detail["activeRun"]["steps"] == row.steps
    assert detail["activeRun"]["lastSeq"] == 2

    row.status = "succeeded"
    row.content = "最终回答"
    row.error = None
    db.commit()
    detail = _detail(db, ontology, admin_user, run["conversationId"])
    assert detail["activeRun"] is None
    assert detail["latestRun"]["status"] == "succeeded"
    assert detail["latestRun"]["content"] == "最终回答"

    retry = run_service.retry_run(
        db, ontology["id"], admin_user, run["runId"], dispatch=False,
    )
    detail = _detail(db, ontology, admin_user, retry["conversationId"])
    assert detail["activeRun"]["runId"] == retry["runId"]
    assert detail["latestRun"]["runId"] == retry["runId"]
    assert detail["latestRun"]["retryOf"] == run["runId"]


def test_owner_isolation_and_sandbox_hidden(
    db, admin_user, editor_user, ontology,
):
    own = _submit(db, ontology, admin_user, run_id="run-owner-detail")
    with pytest.raises(AgentRuntimeApplicationError) as denied:
        conversation_service.require_conversation(
            db, ontology["id"], own["conversationId"], editor_user,
        )
    assert denied.value.kind == "forbidden"
    assert conversation_service.list_conversations(
        db, ontology["id"], None, editor_user,
    ) == []

    sandbox = _conversation(
        db, ontology, admin_user, sandbox=True, title="[评估沙箱] 隐藏",
    )
    with pytest.raises(AgentRuntimeApplicationError) as hidden:
        conversation_service.require_conversation(
            db, ontology["id"], sandbox.id, admin_user,
        )
    assert hidden.value.kind == "not_found"


def test_api_refresh_recovers_running_and_terminal_projection(
    client, auth_headers, ontology, db, admin_user,
):
    release = threading.Event()
    errors: list[BaseException] = []

    def turn(worker_db, ontology_id, user, question, **kwargs):
        yield {
            "type": "step",
            "tool": "search_objects",
            "arguments": {"querySpec": {"objectType": "Order"}},
            "summary": "命中 1 条",
            "durationMs": 1,
            "result": {"kind": "metric", "value": 1},
        }
        yield {"type": "delta", "content": "进行中", "reset": True}
        release.wait(5)
        yield {
            "type": "answer",
            "content": "最终回答",
            "citations": [],
            "proposals": [],
        }

    def worker() -> None:
        try:
            run_service.execute_run(
                "run-api-refresh",
                session_factory=TestSession,
                run_turn_fn=turn,
            )
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    run = _submit(db, ontology, admin_user, run_id="run-api-refresh")
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    _wait_until(
        db,
        lambda: run_service.get_run(
            db, ontology["id"], admin_user, run["runId"],
        )["content"] == "进行中",
    )

    base = f"/api/v2/formal/ontologies/{ontology['id']}/agent/conversations"
    listed = client.get(base, headers=auth_headers)
    assert listed.status_code == 200, listed.text
    item = _find(listed.json()["data"], run["conversationId"])
    assert item is not None
    assert item["activeRun"]["runId"] == run["runId"]
    assert item["activeRun"]["status"] == "running"
    assert item["activeRun"]["content"] == "进行中"
    assert item["latestRun"]["runId"] == run["runId"]
    assert item["updatedAt"] == item["latestRun"]["updatedAt"]

    detail = client.get(
        f"{base}/{run['conversationId']}", headers=auth_headers,
    )
    assert detail.status_code == 200, detail.text
    data = detail.json()["data"]
    assert data["activeRun"]["runId"] == run["runId"]
    assert data["activeRun"]["steps"][0]["tool"] == "search_objects"
    assert data["latestRun"]["content"] == "进行中"
    assert data["updatedAt"] == data["latestRun"]["updatedAt"]

    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []

    detail = client.get(
        f"{base}/{run['conversationId']}", headers=auth_headers,
    )
    assert detail.status_code == 200, detail.text
    data = detail.json()["data"]
    assert data["activeRun"] is None
    assert data["latestRun"]["status"] == "succeeded"
    assert data["latestRun"]["content"] == "最终回答"
