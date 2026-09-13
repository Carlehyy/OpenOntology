from app.data_channel.pipeline_tasks.dispatch import (
    EXECUTION_DLQ_SUBJECT,
    EXECUTION_RECONCILE_SUBJECT,
    EXECUTION_RUN_SUBJECT,
    EXECUTION_STREAM,
)
from app.data_channel.pipeline_tasks.nats_executor import _execution_handler_registry


def test_execution_stream_contract_isolated_from_pipeline_stream():
    assert EXECUTION_STREAM == "SA_EXECUTION_V1"
    assert EXECUTION_RUN_SUBJECT == "sa.execution.run.*"
    assert EXECUTION_RECONCILE_SUBJECT == "sa.execution.reconcile"
    assert EXECUTION_DLQ_SUBJECT == "sa.execution.dlq"
    registry = _execution_handler_registry()
    assert {item[0] for item in registry} == {EXECUTION_RUN_SUBJECT, EXECUTION_RECONCILE_SUBJECT}
    assert {item[1] for item in registry} == {"sa-kernel-v1", "sa-reconciler-v1"}
