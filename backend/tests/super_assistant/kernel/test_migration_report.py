import json
import uuid

from app.models.user import User
from app.super_assistant.models import (
    SuperAssistantConversation,
    SuperAssistantDelegation,
    SuperAssistantMcpServer,
    SuperAssistantMemory,
    SuperAssistantPalaceFile,
)
from app.super_assistant.kernel.migration_report import (
    backfill_legacy_data,
    build_legacy_migration_report,
    rollback_legacy_backfill,
)
from app.super_assistant.kernel.models import CapabilityRevision, ExecutionCall, ExecutionEvent, ExecutionRun


def test_legacy_report_is_explicitly_report_only(db):
    report = build_legacy_migration_report(db)
    assert report["mutated"] is False
    assert report["schema_version"] == "kernel.v1.legacy-disposition.v1"
    assert {row["source"] for row in report["rows"]} == {
        "delegation", "memory", "palace_file", "mcp_server", "skill", "remote_agent",
    }
    memory = next(row for row in report["rows"] if row["source"] == "memory")
    assert memory["readonly"] == memory["total"]
    assert "risk=unknown" in memory["rule"]


def _legacy_fixture(db):
    owner = User(id=str(uuid.uuid4()), username=f"legacy-{uuid.uuid4().hex[:8]}", email=f"{uuid.uuid4().hex}@test.local", password_hash="x", role="admin")
    db.add(owner); db.flush()
    conv = SuperAssistantConversation(owner_id=owner.id, title="legacy"); db.add(conv); db.flush()
    delegation = SuperAssistantDelegation(owner_id=owner.id, super_conversation_id=conv.id, assistant_key="ontology_agent", status="timeout", summary="old task", conversation_ref="legacy-session")
    mcp = SuperAssistantMcpServer(owner_id=owner.id, name="old-mcp", url="https://example.invalid", transport="streamable_http")
    memory = SuperAssistantMemory(owner_id=owner.id, content="private fact")
    palace = SuperAssistantPalaceFile(owner_id=owner.id, filename="old.txt", artifact_id="artifact-1", sha256="a" * 64)
    db.add_all([delegation, mcp, memory, palace]); db.flush()
    return owner, conv, delegation, mcp


def test_backfill_is_explicit_and_idempotent(db):
    owner, _conv, delegation, _mcp = _legacy_fixture(db)
    preview = backfill_legacy_data(db, owner_id=owner.id, migration_id="mig-preview")
    assert preview["mode"] == "dry_run" and preview["mutated"] is False
    assert db.query(ExecutionRun).count() == 0
    applied = backfill_legacy_data(db, owner_id=owner.id, migration_id="mig-1", apply=True)
    assert applied["mutated"] is True
    assert len(applied["created_runs"]) == 1
    run = db.get(ExecutionRun, applied["created_runs"][0])
    assert run.execution_version == "legacy"
    assert json.loads(run.binding_snapshot_ref)["legacy_source"]["id"] == delegation.id
    assert run.status == "waiting_external"
    assert db.query(ExecutionCall).filter_by(run_id=run.id, outcome="outcome_unknown").count() == 1
    replay = backfill_legacy_data(db, owner_id=owner.id, migration_id="mig-1", apply=True)
    assert replay["created_runs"] == []
    assert replay["skipped"]


def test_backfill_capability_revisions_and_rollback_are_scoped(db):
    owner, _conv, _delegation, mcp = _legacy_fixture(db)
    applied = backfill_legacy_data(db, owner_id=owner.id, migration_id="mig-cap", apply=True)
    assert any(key.startswith("legacy.mcp.") for key in applied["created_capabilities"])
    assert db.query(CapabilityRevision).count() >= 1
    dry = rollback_legacy_backfill(db, migration_id="mig-cap", owner_id=owner.id)
    assert dry["mutated"] is False and dry["capability_keys"]
    result = rollback_legacy_backfill(db, migration_id="mig-cap", owner_id=owner.id, apply=True)
    assert result["mutated"] is True
    assert db.query(ExecutionRun).count() == 1
    assert db.query(ExecutionEvent).filter_by(event_type="source.tombstoned").count() == 1
    assert all(cap.enabled is False for cap in db.query(CapabilityRevision).all())
    assert db.get(SuperAssistantMcpServer, mcp.id) is not None
