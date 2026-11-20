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
    multica_learned_remote_ref,
    multica_ref_metadata,
    verify_multica_ref_source,
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
async def test_multica_connector_enriches_remote_ref_with_profile_source():
    """A bound connector records the profile source inside the provider ref."""

    def execute(_arguments):
        return json.dumps({
            "created": True,
            "issue": {"identifier": "MYW-11", "status": "open"},
            "remote_task_ref": '{"issue_ref":"MYW-11","task_id":null}',
            "note": "任务已创建",
        }, ensure_ascii=False)

    connector = MulticaToolConnector(
        tool_name="multica_create_task", executor=execute,
        profile_revision_id="rev-a",
        profile_base_url="http://127.0.0.1:8080", profile_workspace_id="ws-a",
    )
    result = await connector.invoke(
        run_id="r1", call_id="c1", input_ref='{"arguments":{"title":"写报告"}}', deadline=None,
    )
    ref = json.loads(result["remote_task_ref"])
    assert ref["issue_ref"] == "MYW-11" and ref["task_id"] is None
    assert ref["profile_revision_id"] == "rev-a"
    assert ref["base_url"] == "http://127.0.0.1:8080"
    assert ref["workspace_id"] == "ws-a"


@pytest.mark.asyncio
async def test_multica_connector_never_invents_metadata_for_foreign_refs():
    """Unparseable or pre-populated refs pass through without invented sources."""

    def execute(_arguments):
        return json.dumps({
            "created": True,
            "issue": {"identifier": "MYW-12", "status": "open"},
            "remote_task_ref": "not-json",
        }, ensure_ascii=False)

    connector = MulticaToolConnector(
        tool_name="multica_create_task", executor=execute, profile_revision_id="rev-a",
    )
    result = await connector.invoke(
        run_id="r1", call_id="c1", input_ref='{"arguments":{"title":"x"}}', deadline=None,
    )
    assert result["remote_task_ref"] == "not-json"


def test_multica_ref_metadata_reads_legacy_and_bound_references():
    legacy = '{"issue_ref":"MYW-1","task_id":null}'
    assert multica_ref_metadata(legacy) is None
    assert multica_ref_metadata(None) is None
    assert multica_ref_metadata("garbage") is None
    bound = multica_ref_metadata(
        '{"issue_ref":"MYW-1","task_id":null,"profile_revision_id":"rev-a",'
        '"base_url":"http://a","workspace_id":"ws-a"}'
    )
    assert bound == {"profile_revision_id": "rev-a", "base_url": "http://a", "workspace_id": "ws-a"}


def test_verify_multica_ref_source_fails_closed_on_cross_workspace_identity():
    from types import SimpleNamespace

    config = SimpleNamespace(base_url="http://a", workspace_id="ws-a")
    matching = '{"issue_ref":"MYW-1","task_id":null,"profile_revision_id":"rev-a","base_url":"http://a","workspace_id":"ws-a"}'
    # Matching source and legacy refs without metadata both pass.
    verify_multica_ref_source(matching, config, "rev-a")
    verify_multica_ref_source('{"issue_ref":"MYW-1","task_id":null}', config, "rev-a")

    wrong_workspace = '{"issue_ref":"MYW-1","task_id":null,"workspace_id":"ws-b"}'
    with pytest.raises(ContractError, match="different workspace"):
        verify_multica_ref_source(wrong_workspace, config, "rev-a")
    wrong_base = '{"issue_ref":"MYW-1","task_id":null,"base_url":"http://b"}'
    with pytest.raises(ContractError, match="different service endpoint"):
        verify_multica_ref_source(wrong_base, config, "rev-a")
    wrong_revision = '{"issue_ref":"MYW-1","task_id":null,"profile_revision_id":"rev-b"}'
    with pytest.raises(ContractError, match="different profile revision"):
        verify_multica_ref_source(wrong_revision, config, "rev-a")


def _learned_call(remote_task_ref, binding=None):
    from types import SimpleNamespace

    return SimpleNamespace(remote_task_ref=remote_task_ref,
                           multica_profile_revision_id=binding)


def test_multica_learned_remote_ref_adopts_enrichment_of_missing_identity():
    call = _learned_call('{"issue_ref":"MYW-1","task_id":null}', binding="rev-a")
    enriched = json.dumps({
        "issue_ref": "MYW-1", "task_id": "task-1", "issue_id": "issue-1",
        "profile_revision_id": "rev-a", "base_url": "http://a", "workspace_id": "ws-a",
    })
    assert multica_learned_remote_ref(call, enriched) == "adopted"
    assert json.loads(call.remote_task_ref)["task_id"] == "task-1"
    # Re-delivering the same observation is a no-op, not a second mutation.
    assert multica_learned_remote_ref(call, enriched) == "noop"


def test_multica_learned_remote_ref_never_switches_pinned_task_or_issue():
    pinned = json.dumps({"issue_ref": "MYW-1", "task_id": "task-1",
                         "issue_id": "issue-1", "profile_revision_id": "rev-a"})
    # A rerun's task id must not replace the pinned original.
    call = _learned_call(pinned, binding="rev-a")
    assert multica_learned_remote_ref(call, pinned.replace("task-1", "task-9")) == "rejected"
    assert call.remote_task_ref == pinned
    # A different immutable issue must not replace the original either.
    assert multica_learned_remote_ref(
        call, pinned.replace("issue-1", "issue-2")) == "rejected"
    # Dropping a pinned task id is refused just like replacing it.
    assert multica_learned_remote_ref(
        call, '{"issue_ref":"MYW-1","task_id":null,"profile_revision_id":"rev-a"}') == "rejected"
    # Without a pinned issue_id the textual identifier is the identity.
    legacy = _learned_call('{"issue_ref":"MYW-1","task_id":null}')
    assert multica_learned_remote_ref(
        legacy, '{"issue_ref":"MYW-2","task_id":null}') == "rejected"
    # issue_id proves the same issue, so a renamed identifier alias is fine.
    aliased = _learned_call(
        '{"issue_ref":"MYW-1","task_id":"task-1","issue_id":"issue-1"}')
    assert multica_learned_remote_ref(
        aliased, '{"issue_ref":"MYW-1-RENAMED","task_id":"task-1","issue_id":"issue-1"}') == "adopted"


def test_multica_learned_remote_ref_preserves_call_source_binding():
    bound = json.dumps({"issue_ref": "MYW-1", "task_id": None, "profile_revision_id": "rev-a",
                        "base_url": "http://a", "workspace_id": "ws-a"})
    # A learned reference from another revision must not be adopted.
    call = _learned_call(bound, binding="rev-a")
    assert multica_learned_remote_ref(call, bound.replace("rev-a", "rev-b")) == "rejected"
    # Endpoint/workspace drift is refused as well.
    assert multica_learned_remote_ref(call, bound.replace("ws-a", "ws-b")) == "rejected"
    assert multica_learned_remote_ref(call, bound.replace("http://a", "http://b")) == "rejected"
    assert call.remote_task_ref == bound
    # Non-Multica references are never learned or overwritten; the
    # observation itself stays applicable (opaque refs are trusted bounds).
    foreign = _learned_call("plugin-call:c1")
    assert multica_learned_remote_ref(foreign, bound) == "noop"
    assert foreign.remote_task_ref == "plugin-call:c1"
    empty = _learned_call(None, binding="rev-a")
    assert multica_learned_remote_ref(empty, bound) == "noop"
    assert empty.remote_task_ref is None


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


@pytest.mark.asyncio
async def test_remote_direct_revalidates_endpoint_before_network_request(monkeypatch):
    from app.super_assistant.mcp_client import McpClientError

    def reject(url, **kwargs):
        raise McpClientError("private address")

    monkeypatch.setattr("app.super_assistant.mcp_client.validate_mcp_url", reject)
    connector = RemoteAgentHttpConnector(
        agent_id="remote-runtime-ssrf", key="remote.runtime_ssrf",
        endpoint="https://agent.example/run",
    )
    with pytest.raises(ContractError, match="endpoint rejected"):
        await connector.invoke(run_id="r1", call_id="c1", input_ref='{"message":"x"}', deadline=None)




@pytest.mark.asyncio
async def test_other_builtin_mcp_connector_refuses_http_dispatch(monkeypatch):
    from app.super_assistant.kernel.connectors import McpToolConnector

    async def unreachable(**kwargs):  # pragma: no cover - 分流在前
        raise AssertionError(f"should not call MCP {kwargs}")

    monkeypatch.setattr(
        "app.super_assistant.mcp_client.call_tool", unreachable,
    )
    connector = McpToolConnector(
        server_id="server-minio",
        server_name="platform_minio",
        tool_name="minio_status",
        transport="streamable_http",
        url="builtin://minio",
        builtin_key="minio",
    )
    with pytest.raises(ContractError, match="in-process channel"):
        await connector.invoke(
            run_id="r1", call_id="c1",
            input_ref='{"arguments":{}}', deadline=None,
        )


@pytest.mark.asyncio
async def test_managed_weknora_connector_routes_via_execution_and_normal_uses_mcp(monkeypatch):
    """受管 WeKnora 连接器 invoke 走 weknora.execution；普通连接器保持 mcp_client。"""
    from app.super_assistant.kernel.connectors import McpToolConnector

    managed_calls = []
    plain_calls = []

    async def fake_execution_call_tool(**kwargs):
        managed_calls.append(kwargs)
        return '{"rows":[]}'

    async def fake_plain_call_tool(**kwargs):
        plain_calls.append(kwargs)
        return "普通 MCP 结果"

    monkeypatch.setattr(
        "app.super_assistant.weknora.execution.call_tool", fake_execution_call_tool)
    monkeypatch.setattr(
        "app.super_assistant.mcp_client.call_tool", fake_plain_call_tool)

    managed = McpToolConnector(
        server_id="server-weknora",
        server_name="platform_weknora_aa11bb22",
        tool_name="search_knowledge",
        transport="streamable_http",
        url="https://weknora.example.test/mcp/wk-managed-1",
        weknora_endpoint_id="wk-managed-1",
        weknora_scope={"endpoint_id": "wk-managed-1", "knowledge_base_ids": ["kb-1"]},
        owner_id="user-1",
    )
    result = await managed.invoke(
        run_id="r1", call_id="c1",
        input_ref='{"arguments":{"q":"本体"}}', deadline=None,
    )
    assert result["status"] == "answered"
    assert result["content"] == '{"rows":[]}'
    assert managed_calls and not plain_calls
    kwargs = managed_calls[0]
    assert kwargs["server_id"] == "server-weknora"
    assert kwargs["owner_id"] == "user-1"
    assert kwargs["endpoint_id"] == "wk-managed-1"
    assert kwargs["scope"] == {"endpoint_id": "wk-managed-1", "knowledge_base_ids": ["kb-1"]}
    assert kwargs["transport"] == "streamable_http"
    assert kwargs["url"] == "https://weknora.example.test/mcp/wk-managed-1"
    assert kwargs["tool_name"] == "search_knowledge"
    assert kwargs["arguments"] == {"q": "本体"}

    normal = McpToolConnector(
        server_id="server-ext",
        server_name="ext",
        tool_name="lookup",
        transport="streamable_http",
        url="https://mcp.example.test/sse",
    )
    result = await normal.invoke(
        run_id="r2", call_id="c2", input_ref='{"arguments":{}}', deadline=None,
    )
    assert result["content"] == "普通 MCP 结果"
    assert plain_calls and plain_calls[0]["url"] == "https://mcp.example.test/sse"
    assert len(managed_calls) == 1
