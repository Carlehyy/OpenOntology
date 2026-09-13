"""跨会话记忆的风险闸和生命周期判定。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .contracts import ContractError


class MemorySource(StrEnum):
    EXPLICIT = "explicit"
    DERIVED = "derived"
    REFLECTION = "reflection"


class MemoryRisk(StrEnum):
    LOW = "low"
    SENSITIVE = "sensitive"
    INSTRUCTION = "instruction"


class MemoryStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class MemoryDecision:
    status: MemoryStatus
    requires_approval: bool
    reason: str


def decide_memory(*, source: MemorySource, risk: MemoryRisk, explicit_user_request: bool = False) -> MemoryDecision:
    if source is MemorySource.EXPLICIT and explicit_user_request and risk is MemoryRisk.LOW:
        return MemoryDecision(MemoryStatus.ACCEPTED, False, "explicit low-risk memory")
    if risk in {MemoryRisk.SENSITIVE, MemoryRisk.INSTRUCTION}:
        return MemoryDecision(MemoryStatus.PENDING, True, "sensitive or instruction-like memory requires confirmation")
    if source in {MemorySource.DERIVED, MemorySource.REFLECTION}:
        return MemoryDecision(MemoryStatus.PENDING, True, "derived memory requires confirmation")
    return MemoryDecision(MemoryStatus.PENDING, True, "explicit confirmation required")


def apply_memory_decision(*, current_status: MemoryStatus, decision: MemoryStatus) -> MemoryStatus:
    if current_status in {MemoryStatus.SUPERSEDED, MemoryStatus.REJECTED}:
        raise ContractError("closed memory cannot be changed")
    if decision not in {MemoryStatus.ACCEPTED, MemoryStatus.REJECTED}:
        raise ContractError("memory can only be accepted or rejected")
    return decision

