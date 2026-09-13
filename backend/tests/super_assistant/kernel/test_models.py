from app.model_registry import import_all_models
from app.shared.database import Base


def test_kernel_models_are_registered_with_canonical_metadata():
    import_all_models()
    expected = {
        "super_assistant_execution_runs",
        "super_assistant_execution_turns",
        "super_assistant_execution_steps",
        "super_assistant_execution_calls",
        "super_assistant_execution_attempts",
        "super_assistant_execution_events",
        "super_assistant_execution_inbox",
        "super_assistant_execution_approvals",
        "super_assistant_execution_context_snapshots",
        "super_assistant_execution_artifacts",
        "super_assistant_capability_revisions",
        "super_assistant_execution_projection_cursors",
        "super_assistant_execution_dispatch_outbox",
        "super_assistant_execution_commands",
    }
    assert expected <= set(Base.metadata.tables)


def test_kernel_idempotency_constraints_are_explicit():
    import_all_models()
    run = Base.metadata.tables["super_assistant_execution_runs"]
    call = Base.metadata.tables["super_assistant_execution_calls"]
    run_constraints = {tuple(sorted(c.columns.keys())) for c in run.constraints if c.name == "uq_sa_execution_run_idempotency"}
    call_constraints = {tuple(sorted(c.columns.keys())) for c in call.constraints if c.name == "uq_sa_execution_call_idempotency"}
    assert run_constraints == {("conversation_id", "idempotency_key", "owner_id")}
    assert call_constraints == {("capability_revision", "idempotency_key", "run_id")}


def test_kernel_persists_cancellation_and_external_reconciliation_facts():
    import_all_models()
    run = Base.metadata.tables["super_assistant_execution_runs"]
    call = Base.metadata.tables["super_assistant_execution_calls"]
    inbox = Base.metadata.tables["super_assistant_execution_inbox"]
    assert {"cancel_reason", "cancel_deadline"} <= set(run.columns.keys())
    assert {"provider_event_id", "evidence_ref"} <= set(call.columns.keys())
    assert "target_ref" in inbox.columns.keys()
