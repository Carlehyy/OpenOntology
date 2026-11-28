"""AutoLoop's public, owner-scoped API contract."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Priority = Literal["low", "medium", "high"]
DecisionStatus = Literal[
    "open", "pending_dispatch", "sending", "writeback_unknown", "writeback_failed",
    "completed", "withdrawn", "handled",
]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldEquals(Contract):
    field: str = Field(min_length=1, max_length=255)
    value: str = Field(max_length=16384)


class RuleMatch(Contract):
    sourceKind: str | None = Field(default=None, max_length=40)
    ingestKeyId: str | None = Field(default=None, max_length=36)
    sourceSystem: str | None = Field(default=None, max_length=255)
    channel: str | None = Field(default=None, max_length=255)
    titleContains: str | None = Field(default=None, max_length=16384)
    bodyContains: str | None = Field(default=None, max_length=16384)
    fieldEquals: list[FieldEquals] = Field(default_factory=list, max_length=50)


class RuleBinding(Contract):
    mode: Literal["extract"]
    pattern: str | None = Field(default=None, max_length=256)
    targetIssue: str | None = Field(default=None, max_length=255)


class RuleCreate(Contract):
    name: str = Field(min_length=1, max_length=255)
    # A rule is created paused. Enabling it, or widening an enabled rule to
    # match everything, is a deliberate act that needs explicit consent.
    enabled: bool = False
    position: int = Field(default=0, ge=0, strict=True)
    match: RuleMatch = Field(default_factory=RuleMatch)
    titleTemplate: str = Field(min_length=1, max_length=16384)
    descriptionTemplate: str = Field(max_length=16384)
    priority: Priority | None = None
    assignee: str | None = Field(default=None, max_length=255)
    binding: RuleBinding | None = None

    @model_validator(mode="after")
    def meaningful_name(self):
        if not self.name.strip():
            raise ValueError("name must not be blank")
        return self


class RulePatch(Contract):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    enabled: bool | None = None
    position: int | None = Field(default=None, ge=0, strict=True)
    match: RuleMatch | None = None
    titleTemplate: str | None = Field(default=None, min_length=1, max_length=16384)
    descriptionTemplate: str | None = Field(default=None, max_length=16384)
    priority: Priority | None = None
    assignee: str | None = Field(default=None, max_length=255)
    binding: RuleBinding | None = None


class RuleOut(RuleCreate):
    id: str
    version: int
    createdAt: str
    updatedAt: str


class RuleCreateRequest(RuleCreate):
    """POST body: request-only consent to enable a rule that matches everything.

    Consent is never stored, echoed or snapshotted; responses stay RuleOut.
    """
    confirmAllMatch: bool = False


class RulePatchRequest(RulePatch):
    """PATCH body plus request-only fields; responses stay RuleOut.

    expectedVersion binds the edit to the version the owner last read.
    confirmAllMatch is the explicit consent to move the rule into "enabled and
    matches everything", and only counts together with a current expectedVersion.
    """
    expectedVersion: int | None = Field(default=None, ge=1, strict=True)
    confirmAllMatch: bool = False


class StoredPreview(Contract):
    sampleEvent: dict[str, Any]


class DraftPreview(StoredPreview):
    rule: RuleCreate


class PreviewOut(Contract):
    renderedTitle: str
    renderedDescription: str
    untrustedOriginal: Any
    priority: Priority | None = None
    assignee: str | None = None
    bindingTarget: str | None = None
    ruleVersion: int
    warnings: list[str]


class RoutingPolicyPatch(Contract):
    """Version 0 is the built-in default; a null prompt returns to it."""
    expectedVersion: int = Field(ge=0, strict=True)
    promptText: str | None = Field(max_length=8000)


class RoutingPolicyOut(Contract):
    version: int
    promptText: str | None
    updatedAt: str | None = None
    updatedBy: str | None = None
    modelConfigId: str | None = None
    policySummary: str


class RoutingPolicyPreview(RoutingPolicyPatch):
    # The event type names the inbound source; it is mapped to the received
    # fact type a real event of that source carries.
    eventType: Literal["notification", "schedule"]
    # Untrusted data. It only ever fills the event slot of the planner's input.
    sampleEvent: dict[str, Any]


class RoutingPolicyPreviewOut(Contract):
    action: Literal["create_decision", "ignore"]
    reason: str
    modelConfigId: str
    # The stored version for a saved policy, otherwise the base that was sent.
    routingPromptVersion: int
    isDraft: bool


class StepOut(Contract):
    id: str
    kind: str
    state: Literal["never_sent", "queued", "sending", "unknown", "failed", "confirmed"]
    error: str | None = None


class EventSource(Contract):
    kind: str
    system: str
    ref: str


class Causality(Contract):
    parentEventId: str | None = None
    hops: int


class EventOut(Contract):
    id: str
    seq: int
    type: str
    source: EventSource
    causality: Causality
    happenedAt: str
    ruleVersion: int | None = None
    action: str
    issueRef: str | None = None
    decisionItemId: str | None = None
    error: str | None = None


class EventDetailOut(EventOut):
    untrustedOriginal: Any
    actionSteps: list[StepOut]
    dedup: dict[str, Any]
    scheduledTaskId: str | None = None
    scheduledRunId: str | None = None
    issueUrl: str | None = None


TraceRoute = Literal["pending", "rule", "assistant"]
TraceOutcome = Literal[
    "pending", "queued", "sending", "review", "confirmed", "recorded",
    "failed", "unknown", "ignored", "handled", "cancelled",
]


class TraceRuleRef(Contract):
    id: str
    name: str | None = None
    version: int | None = None


class EventTraceOut(Contract):
    """One inbound routing trace per receipt; rule facts come from the frozen
    prepared snapshot, so editing or deleting a rule never rewrites history."""

    id: str
    receivedAt: str
    title: str | None = None
    source: EventSource
    route: TraceRoute
    outcome: TraceOutcome
    rule: TraceRuleRef | None = None
    action: str | None = None
    decisionItemId: str | None = None
    issueRef: str | None = None
    scheduledTaskId: str | None = None
    scheduledRunId: str | None = None
    reason: str | None = None
    # The routing policy and model a miss was assessed under. Null for rule
    # routes, pending traces and facts written before the policy was frozen.
    routingPromptVersion: int | None = None
    modelConfigId: str | None = None


class TraceAuditEventOut(Contract):
    id: str
    type: str
    happenedAt: str
    action: str
    error: str | None = None


class TraceStepOut(StepOut):
    remoteObjectId: str | None = None


class EventTraceDetailOut(EventTraceOut):
    """Detail keeps the full reason; list rows carry a truncated summary."""

    untrustedOriginal: Any
    error: str | None = None
    issueUrl: str | None = None
    actionSteps: list[TraceStepOut]
    auditEvents: list[TraceAuditEventOut]


class RoutingRuleHit(Contract):
    ruleId: str
    ruleName: str | None = None
    hits: int


class RoutingStatsOut(Contract):
    days: int
    since: str
    until: str
    received: int
    matched: int
    unmatched: int
    pendingRoute: int
    outcomes: dict[str, int]
    rules: list[RoutingRuleHit]


class Option(Contract):
    id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=2000)


class Resolution(Contract):
    kind: Literal["completed", "withdrawn", "handled"]
    optionId: str | None = None
    optionLabel: str | None = None
    freeText: str | None = None
    actor: str
    at: str
    reason: str | None = None


class DecisionOut(Contract):
    id: str
    source: Literal["watchdog", "assistant", "kernel_fallback"]
    status: DecisionStatus
    title: str
    question: str
    factsSummary: str
    options: list[Option]
    recommendedOptionId: str | None = None
    freeTextAllowed: Literal[True] = True
    issueRef: str | None = None
    issueUrl: str | None = None
    eventIds: list[str]
    untrustedOriginal: Any = None
    priority: Priority
    contentVersion: int
    createdAt: str
    updatedAt: str
    writebackSteps: list[StepOut]
    resolution: Resolution | None = None
    alreadyProcessed: bool = False


class Page[T](Contract):
    items: list[T]
    nextCursor: str | None
    hasMore: bool


class DecisionSummary(Contract):
    openCount: int
    pendingDispatchCount: int
    sendingCount: int
    unknownCount: int
    failedCount: int
    unfinishedTotal: int


class Mutation(Contract):
    expectedContentVersion: int = Field(ge=1, strict=True)
    idempotencyKey: str = Field(min_length=1, max_length=255)


class Decide(Mutation):
    optionId: str | None = Field(default=None, min_length=1, max_length=100)
    freeText: str | None = Field(default=None, max_length=30000)


class MarkHandled(Mutation):
    reason: str | None = Field(default=None, max_length=4000)
