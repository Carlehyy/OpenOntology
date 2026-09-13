"""Context Source/Pack planner；模型只接收预算内且仍然有效的来源。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .contracts import ContractError


class ContextTier(StrEnum):
    REQUIRED = "required"
    RELEVANT = "relevant"
    OPTIONAL = "optional"


@dataclass(frozen=True, slots=True)
class SourceRef:
    kind: str
    id: str
    revision: str
    locator: str
    recipe_revision: str
    extraction_id: str
    tombstoned: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "id": self.id, "revision": self.revision,
            "locator": self.locator, "recipe_revision": self.recipe_revision,
            "extraction_id": self.extraction_id,
        }


@dataclass(frozen=True, slots=True)
class ContextCandidate:
    source: SourceRef
    content: str
    tier: ContextTier = ContextTier.RELEVANT
    relevance: float = 0.0
    authority: float = 0.0
    freshness: float = 0.0
    cost: float = 0.0
    section: str = "knowledge"

    @property
    def token_estimate(self) -> int:
        return max(1, len(self.content.encode("utf-8")) // 4)


@dataclass(frozen=True, slots=True)
class ContextPack:
    content: str
    source_refs: tuple[dict[str, Any], ...]
    pack_hash: str
    budget: dict[str, int]


DEFAULT_BUDGET = {"system": 4000, "working": 8000, "knowledge": 12000, "episode": 4000, "recent": 4000}
HARD_CAP = 32000


class ContextPackPlanner:
    def __init__(self, *, budget: dict[str, int] | None = None, hard_cap: int = HARD_CAP) -> None:
        self.budget = dict(budget or DEFAULT_BUDGET)
        self.hard_cap = hard_cap
        if sum(self.budget.values()) > hard_cap:
            raise ContractError("context budget exceeds hard cap")

    def plan(self, candidates: list[ContextCandidate]) -> ContextPack:
        usable = [candidate for candidate in candidates if not candidate.source.tombstoned]
        selected: list[ContextCandidate] = []
        used = 0
        used_by_section: dict[str, int] = {}
        for tier in (ContextTier.REQUIRED, ContextTier.RELEVANT, ContextTier.OPTIONAL):
            group = [c for c in usable if c.tier is tier]
            group.sort(key=lambda c: (-c.relevance, -c.authority, -c.freshness, c.cost, c.source.id))
            for candidate in group:
                section_limit = self.budget.get(candidate.section, 0)
                section_used = used_by_section.get(candidate.section, 0)
                if section_limit <= 0 or section_used + candidate.token_estimate > section_limit or used + candidate.token_estimate > self.hard_cap:
                    continue
                selected.append(candidate)
                used += candidate.token_estimate
                used_by_section[candidate.section] = section_used + candidate.token_estimate
        content = "\n\n".join(candidate.content for candidate in selected)
        refs = tuple(candidate.source.as_dict() for candidate in selected)
        digest = hashlib.sha256(json.dumps({"content": content, "refs": refs}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return ContextPack(content, refs, f"sha256:{digest}", dict(self.budget))
