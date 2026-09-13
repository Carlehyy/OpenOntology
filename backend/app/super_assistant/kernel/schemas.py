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
    write_permission_hash: str | None = None


class CreateRunRequest(KernelRequest):
    goal: str = Field(min_length=1, max_length=20000)
    idempotency_key: str = Field(min_length=1, max_length=255)
    deadline: datetime | None = None
    parent_run_id: str | None = None
    join_policy: Literal["all", "any"] = "all"
    max_steps: int = Field(default=8, ge=1, le=128)
    binding: BindingInput | None = None


class CancelRunRequest(KernelRequest):
    idempotency_key: str = Field(min_length=1, max_length=255)
    reason: Literal["user", "parent", "deadline"] = "user"


class ControlRunRequest(KernelRequest):
    idempotency_key: str = Field(min_length=1, max_length=255)


class InputRequest(KernelRequest):
    kind: Literal["user_input", "question_answer", "external_event", "resume"] = "user_input"
    content: str | None = Field(default=None, max_length=262144)
    content_ref: str | None = None
    question_id: str | None = None
    idempotency_key: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def one_content(self):
        if (self.content is None) == (self.content_ref is None):
            raise ValueError("exactly one of content or content_ref is required")
        if self.kind == "question_answer" and not self.question_id:
            raise ValueError("question_id is required for question_answer")
        return self


class ApprovalDecisionRequest(KernelRequest):
    decision: Literal["approved", "denied"]
    idempotency_key: str = Field(min_length=1, max_length=255)


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
