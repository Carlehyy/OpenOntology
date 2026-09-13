import pytest

from app.super_assistant.kernel.connectors import (
    AgentDescriptor,
    CapabilityRegistry,
    ContextRequirements,
    DomainBindingResolver,
    SessionPolicy,
    TrustLevel,
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
