import pytest
import httpx
import json

from app.super_assistant.kernel.connectors import (
    AgentDescriptor,
    CapabilityRegistry,
    ContextRequirements,
    DomainBindingResolver,
    SessionPolicy,
    TrustLevel,
    ConnectorRegistry,
    RemoteAgentHttpConnector,
    MulticaToolConnector,
)
from app.super_assistant.kernel.contracts import ContractError


def test_descriptor_and_registry_freeze_capability_revision():
    descriptor = AgentDescriptor(
        agent_id="agent-1", key="external.research", revision=1,
        transport="rap.v1", capabilities=("stream", "artifact"),
        context_requirements=ContextRequirements(required_refs=("goal",)),
        session_policy=SessionPolicy.RESUMABLE, supports_stream=True, supports_push=True,
    )
    registry = CapabilityRegistry()
    registry.register(descriptor, TrustLevel.VERIFIED)
    registry.register(descriptor, TrustLevel.VERIFIED)
    assert registry.get("external.research", 1)[0] == descriptor
    with pytest.raises(ContractError, match="immutable"):
        registry.register(
            AgentDescriptor(agent_id="agent-1", key="external.research", revision=1, transport="rap.v1", capabilities=("other",)),
            TrustLevel.USER_UNTRUSTED,
        )


def test_delegated_binding_requires_editing_draft_and_permission_hash():
    resolver = DomainBindingResolver()
    with pytest.raises(ContractError):
        resolver.resolve(owner_id="u1", binding_mode="delegated", binding={"ontology_id": "o1"})
    snapshot = resolver.resolve(
        owner_id="u1", binding_mode="delegated",
        binding={"ontology_id": "o1", "draft_version_id": "v1", "lifecycle": "editing", "write_permission_hash": "h"},
    )
    assert snapshot.ontology_id == "o1"
    assert snapshot.lifecycle == "editing"


def test_direct_ui_binding_can_remain_empty():
    snapshot = DomainBindingResolver().resolve(owner_id="u1", binding_mode="direct_ui", binding=None)
    assert snapshot.ontology_id is None


@pytest.mark.asyncio
async def test_remote_agent_http_connector_is_dispatchable_and_preserves_contract():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.headers["authorization"] == "Bearer secret"
        assert json.loads(request.content) == {"message": "研究项目", "session_ref": "s1"}
        return httpx.Response(200, json={"status": "answered", "content": "完成", "session_ref": "s2", "artifacts": [{"kind": "report", "content": {"ok": True}}]})

    connector = RemoteAgentHttpConnector(
        agent_id="remote-1", key="remote.research", endpoint="https://agent.example/run",
        token="secret", transport=httpx.MockTransport(handler),
    )
    registry = ConnectorRegistry()
    registry.register(connector)
    result = await registry.invoke(
        key="remote.research", revision=1, run_id="r1", call_id="c1",
        input_ref='{"message":"研究项目","session_ref":"s1"}', deadline=None,
    )
    assert result["status"] == "answered"
    assert result["session_ref"] == "s2"
    assert result["run_id"] == "r1"
    assert result["artifacts"][0]["kind"] == "report"
    assert connector.descriptor().supports_artifact is True
    assert registry.resolve("remote.research", 1) is connector


@pytest.mark.asyncio
async def test_remote_connector_does_not_claim_unsupported_cancel_or_status():
    connector = RemoteAgentHttpConnector(
        agent_id="remote-1", key="remote.research", endpoint="https://agent.example/run",
    )
    assert (await connector.cancel(remote_task_ref="x"))["status"] == "unsupported"
    assert (await connector.query_status(remote_task_ref="x"))["status"] == "unsupported"


@pytest.mark.asyncio
async def test_remote_pull_connector_enqueues_and_reconciles_with_truthful_controls():
    calls = []

    def enqueue(message, session_ref, timeout_seconds, call_id):
        calls.append(("enqueue", message, session_ref, timeout_seconds, call_id))
        return {"status": "running", "remote_task_ref": "rap-pull:t1"}

    def query(remote_ref):
        calls.append(("query", remote_ref))
        return {"status": "completed", "content": "完成", "provider_event_id": "evt-1"}

    def cancel(remote_ref):
        calls.append(("cancel", remote_ref))
        return {"status": "cancelled", "provider_event_id": "evt-cancel"}

    connector = RemoteAgentHttpConnector(
        agent_id="remote-pull", key="remote.pull", endpoint="", mode="pull",
        pull_enqueue=enqueue, pull_query=query, pull_cancel=cancel,
    )
    result = await connector.invoke(
        run_id="r1", call_id="c1", input_ref='{"message":"研究项目","session_ref":"s1"}', deadline=None,
    )
    assert result["status"] == "running"
    assert result["remote_task_ref"] == "rap-pull:t1"
    assert connector.descriptor().supports_query_status is True
    assert connector.descriptor().supports_cancel is True
    assert (await connector.query_status(remote_task_ref="rap-pull:t1"))["status"] == "completed"
    assert (await connector.cancel(remote_task_ref="rap-pull:t1"))["status"] == "cancelled"
    assert calls[0][0] == "enqueue"


@pytest.mark.asyncio
async def test_multica_tool_connector_wraps_existing_service_without_expanding_capabilities():
    seen = {}

    def execute(arguments):
        seen.update(arguments)
        return "created"

    connector = MulticaToolConnector(tool_name="multica_create_task", executor=execute)
    result = await connector.invoke(
        run_id="r1", call_id="c1",
        input_ref='{"arguments":{"title":"写报告"}}', deadline=None,
    )
    assert result["status"] == "answered"
    assert result["content"] == "created"
    assert seen == {"title": "写报告"}
    assert connector.descriptor().supports_query_status is False


@pytest.mark.asyncio
async def test_multica_create_connector_preserves_remote_ref_and_control_hooks():
    calls = []

    def execute(arguments):
        calls.append(("invoke", arguments))
        return json.dumps({
            "created": True,
            "issue": {"identifier": "MYW-9", "status": "open"},
            "remote_task_ref": '{"issue_ref":"MYW-9","task_id":null}',
            "note": "任务已创建",
        }, ensure_ascii=False)

    def query(remote_ref):
        calls.append(("query", remote_ref))
        return {"status": "completed", "content": "已完成", "remote_task_ref": remote_ref}

    def cancel(remote_ref):
        calls.append(("cancel", remote_ref))
        return {"status": "cancelled", "remote_task_ref": remote_ref}

    connector = MulticaToolConnector(
        tool_name="multica_create_task", executor=execute,
        query_executor=query, cancel_executor=cancel,
    )
    result = await connector.invoke(
        run_id="r1", call_id="c1", input_ref='{"arguments":{"title":"写报告"}}', deadline=None,
    )
    assert result["status"] == "running"
    assert result["remote_task_ref"] == '{"issue_ref":"MYW-9","task_id":null}'
    assert connector.descriptor().supports_query_status is True
    assert connector.descriptor().supports_cancel is True
    assert (await connector.query_status(remote_task_ref=result["remote_task_ref"]))["status"] == "completed"
    assert (await connector.cancel(remote_task_ref=result["remote_task_ref"]))["status"] == "cancelled"
    assert [item[0] for item in calls] == ["invoke", "query", "cancel"]

@pytest.mark.asyncio
async def test_remote_direct_unknown_provider_status_is_preserved_for_reconciliation():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "mystery", "content": ""})
    connector = RemoteAgentHttpConnector(agent_id="remote-unknown", key="remote.unknown", endpoint="https://agent.example/run", transport=httpx.MockTransport(handler))
    result = await connector.invoke(run_id="r1", call_id="c1", input_ref='{"message":"x"}', deadline=None)
    assert result["status"] == "unknown"


@pytest.mark.asyncio
async def test_remote_direct_response_is_bounded_before_json_materialization():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'{"status":"answered","content":"' + b"x" * (256 * 1024) + b'"}',
        )

    connector = RemoteAgentHttpConnector(
        agent_id="remote-large", key="remote.large",
        endpoint="https://agent.example/run", transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ContractError, match="256 KiB"):
        await connector.invoke(run_id="r1", call_id="c1", input_ref='{"message":"x"}', deadline=None)
