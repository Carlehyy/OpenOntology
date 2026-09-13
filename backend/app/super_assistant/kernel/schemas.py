from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class KernelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BindingInput(KernelRequest):
    binding_mode: Literal["direct_ui", "delegated", "legacy"] = "direct_ui"
    ontology_id: str | None = None
    draft_version_id: str | None = None
    lifecycle: str | None = None
    write_permission: bool | None = None


class CreateRunRequest(KernelRequest):
    goal: str = Field(min_length=1, max_length=20000)
    idempotency_key: str = Field(min_length=1, max_length=255)
    deadline: datetime | None = None
    parent_run_id: str | None = None
    join_policy: Literal["all", "any"] = "all"
    binding: BindingInput | None = None


class CancelRunRequest(KernelRequest):
    idempotency_key: str = Field(min_length=1, max_length=255)
    reason: Literal["user", "parent", "deadline"] = "user"


class RunAccepted(KernelRequest):
    run_id: str
    execution_version: Literal["kernel.v1"]
    stream_url: str
    request_id: str


class RunView(KernelRequest):
    run_id: str
    conversation_id: str
    status: str
    wait_reason: str | None
    version: int
    execution_version: str
    goal: str
    deadline: datetime | None
    current_inbox: list[dict[str, Any]] = Field(default_factory=list)
    calls: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    binding_snapshot: dict[str, Any] = Field(default_factory=dict)
