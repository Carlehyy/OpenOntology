from app.super_assistant.kernel.artifacts import complete_business_artifact, verify_artifact
from app.super_assistant.kernel.context import ContextCandidate, ContextPackPlanner, ContextTier, SourceRef
from app.super_assistant.kernel.memory_policy import MemoryRisk, MemorySource, MemoryStatus, decide_memory


def _source(source_id: str, *, tombstoned: bool = False) -> SourceRef:
    return SourceRef("memory", source_id, "1", f"memory://{source_id}", "recipe-1", "extract-1", tombstoned)


def test_context_planner_excludes_tombstones_and_applies_relevance_order():
    planner = ContextPackPlanner()
    pack = planner.plan([
        ContextCandidate(_source("low"), "low", ContextTier.RELEVANT, relevance=0.1),
        ContextCandidate(_source("high"), "high", ContextTier.RELEVANT, relevance=0.9),
        ContextCandidate(_source("deleted", tombstoned=True), "deleted", ContextTier.REQUIRED),
    ])
    assert pack.content == "high\n\nlow"
    assert [ref["id"] for ref in pack.source_refs] == ["high", "low"]


def test_memory_risk_gate_only_auto_accepts_explicit_low_risk():
    accepted = decide_memory(source=MemorySource.EXPLICIT, risk=MemoryRisk.LOW, explicit_user_request=True)
    assert accepted.status is MemoryStatus.ACCEPTED
    pending = decide_memory(source=MemorySource.REFLECTION, risk=MemoryRisk.LOW)
    assert pending.requires_approval is True
    sensitive = decide_memory(source=MemorySource.EXPLICIT, risk=MemoryRisk.SENSITIVE, explicit_user_request=True)
    assert sensitive.status is MemoryStatus.PENDING


def test_artifact_checksum_and_business_completion_are_separate():
    data = b"hello"
    checksum = "sha256:" + __import__("hashlib").sha256(data).hexdigest()
    integrity = verify_artifact(data, expected_checksum=checksum, expected_size=5)
    assert integrity.integrity_status == "verified"
    assert complete_business_artifact(integrity, success=True).business_status == "success"
    failed = verify_artifact(data, expected_checksum="sha256:bad", expected_size=5)
    assert failed.business_status == "integrity_failed"


def test_context_planner_retains_oversized_required_source_with_provenance():
    source = _source("long-goal")
    pack = ContextPackPlanner().plan([
        ContextCandidate(source, "GOAL-START " + ("x" * 50000) + " GOAL-END", ContextTier.REQUIRED, section="working"),
    ])
    assert pack.source_refs[0]["id"] == "long-goal"
    assert "GOAL-START" in pack.content and "GOAL-END" in pack.content
    assert "context truncated" in pack.content
    assert sum(len(part.encode("utf-8")) // 4 for part in pack.content.split("\n\n")) <= pack.budget["working"]
