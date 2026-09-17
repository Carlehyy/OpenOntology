import uuid
from datetime import datetime, timezone

from app.models.user import User
from app.super_assistant import palace_graph
from app.super_assistant.models import SuperAssistantMemory, SuperAssistantPalaceBuild, SuperAssistantPalaceFile
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


def test_collector_builds_provenance_and_filters_deleted_sources(db, monkeypatch):
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
    file_row = SuperAssistantPalaceFile(
        owner_id=owner.id, filename="domain.md", artifact_id="a-1", mime_type="text/markdown",
        size=20, sha256="sha-file-1", status="built",
    )
    deleted_file = SuperAssistantPalaceFile(
        owner_id=owner.id, filename="deleted.md", artifact_id="a-2", mime_type="text/markdown",
        size=20, sha256="sha-file-2", status="failed",
    )
    db.add_all([file_row, deleted_file])
    db.flush()
    db.add(SuperAssistantPalaceBuild(
        owner_id=owner.id, file_id=file_row.id, content_hash=file_row.sha256,
        status="success", chunk_count=1, entity_count=1, relation_count=0,
    ))
    db.commit()

    def fake_search(owner_id, terms):
        assert owner_id == owner.id
        assert terms == ["供应链"]
        return {
            "entities": [
                {"id": "e1", "name": "供应链", "type": "概念", "aliases": [], "file_ids": [file_row.id]},
                {"id": "e2", "name": "不应进入", "type": "概念", "aliases": [], "file_ids": [deleted_file.id]},
            ],
            "relations": [],
        }

    monkeypatch.setattr(palace_graph, "search", fake_search)
    candidates = collect_context_candidates(db, owner.id, "供应链")
    assert {candidate.source.kind for candidate in candidates} == {"memory", "palace_file"}
    memory = next(candidate for candidate in candidates if candidate.source.kind == "memory")
    assert memory.source.locator == f"memory://{active_memory.id}"
    assert memory.source.recipe_revision == "memory.v1"
    palace = next(candidate for candidate in candidates if candidate.source.kind == "palace_file")
    assert palace.source.id == file_row.id
    assert palace.source.revision == file_row.sha256
    assert palace.source.extraction_id
    assert "不应进入" not in palace.content
    assert deleted_memory.id not in {candidate.source.id for candidate in candidates}


def test_collector_tolerates_graph_outage_and_keeps_pinned_memory(db, monkeypatch):
    owner = _owner(db)
    memory = SuperAssistantMemory(owner_id=owner.id, content="固定偏好", pinned=True, source="explicit")
    db.add(memory)
    db.commit()
    monkeypatch.setattr(palace_graph, "search", lambda *_args: (_ for _ in ()).throw(RuntimeError("neo4j down")))
    candidates = collect_context_candidates(db, owner.id, "任意查询")
    assert [candidate.source.id for candidate in candidates] == [memory.id]


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
