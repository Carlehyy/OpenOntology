import uuid
from datetime import datetime, timezone

from app.models.user import User
from app.super_assistant.models import SuperAssistantMemory
from app.super_assistant.kernel.context_sources import collect_context_candidates
from app.super_assistant.kernel.source_registry import tombstone_source


def _owner(db):
    row = User(
        id=str(uuid.uuid4()), username=f"ctx-{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex}@test.local", password_hash="x", role="admin",
    )
    db.add(row)
    db.flush()
    return row


def test_collector_builds_provenance_and_filters_deleted_sources(db):
    owner = _owner(db)
    active_memory = SuperAssistantMemory(
        owner_id=owner.id, content="用户偏好简洁的供应链报告", zone="core",
        pinned=True, source="explicit", updated_at=datetime.now(timezone.utc),
    )
    deleted_memory = SuperAssistantMemory(
        owner_id=owner.id, content="这条已被取代", zone="general",
        source="reflection", superseded=True,
    )
    db.add_all([active_memory, deleted_memory])
    db.commit()

    candidates = collect_context_candidates(db, owner.id, "供应链")
    assert {candidate.source.kind for candidate in candidates} == {"memory"}
    memory = next(candidate for candidate in candidates if candidate.source.id == active_memory.id)
    assert memory.source.locator == f"memory://{active_memory.id}"
    assert memory.source.recipe_revision == "memory.v1"
    assert deleted_memory.id not in {candidate.source.id for candidate in candidates}


def test_tombstone_is_idempotent_and_excludes_existing_source(db):
    owner = _owner(db)
    memory = SuperAssistantMemory(owner_id=owner.id, content="待删除事实", pinned=True, source="explicit")
    db.add(memory)
    db.flush()
    first = tombstone_source(
        db, owner_id=owner.id, kind="memory", source_id=memory.id, revision="r1",
        locator=f"memory://{memory.id}", recipe_revision="memory.v1",
        extraction_id="extract-1", reason="user_deleted",
    )
    second = tombstone_source(
        db, owner_id=owner.id, kind="memory", source_id=memory.id, revision="r2",
        locator=f"memory://{memory.id}", recipe_revision="memory.v1",
        extraction_id="extract-2", reason="retry",
    )
    db.commit()
    assert first.id == second.id
    assert second.revision == "r1"
    assert collect_context_candidates(db, owner.id, "") == []


def test_tombstone_can_emit_a_kernel_source_event(db):
    from app.super_assistant.models import SuperAssistantConversation
    from app.super_assistant.kernel.models import ExecutionEvent
    from app.super_assistant.kernel.store import create_run

    owner = _owner(db)
    conversation = SuperAssistantConversation(owner_id=owner.id, title="context")
    db.add(conversation)
    db.flush()
    run, _ = create_run(
        db, owner_id=owner.id, conversation_id=conversation.id,
        goal="retire source", idempotency_key=f"run-{uuid.uuid4().hex}",
    )
    tombstone_source(
        db, owner_id=owner.id, kind="memory", source_id="mem-1", revision="r1",
        locator="memory://mem-1", recipe_revision="memory.v1", extraction_id="extract-1",
        reason="user_deleted", run=run,
    )
    db.commit()
    event = db.query(ExecutionEvent).filter_by(run_id=run.id, event_type="source.tombstoned").one()
    assert event.payload["source_ref"]["id"] == "mem-1"
    assert event.payload["reason"] == "user_deleted"
