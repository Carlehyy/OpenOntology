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
    enabled: bool = True
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
