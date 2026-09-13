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
        return httpx.Response(200, json={"status": "answered", "content": "完成", "session_ref": "s2"})

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
    assert registry.resolve("remote.research", 1) is connector


@pytest.mark.asyncio
async def test_remote_connector_does_not_claim_unsupported_cancel_or_status():
    connector = RemoteAgentHttpConnector(
        agent_id="remote-1", key="remote.research", endpoint="https://agent.example/run",
    )
    assert (await connector.cancel(remote_task_ref="x"))["status"] == "unsupported"
    assert (await connector.query_status(remote_task_ref="x"))["status"] == "unsupported"


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
