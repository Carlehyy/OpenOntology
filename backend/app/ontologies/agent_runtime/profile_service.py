"""Agent profile persistence and capability-query application workflows."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.ontologies.agent_runtime import schemas as S
from app.ontologies.agent_runtime.models import AgentProfile


PROFILE_FIELDS = [
    "enabled",
    "allowed_object_type_ids",
    "allowed_link_type_ids",
    "allowed_action_ids",
    "allowed_properties",
    "allow_action_proposals",
    "max_rows_per_query",
    "max_steps",
    "system_prompt_extra",
    "default_model_id",
]
RESETTABLE_FIELDS = {
    "allowed_object_type_ids",
    "allowed_link_type_ids",
    "allowed_action_ids",
    "allowed_properties",
}


def profile_out(profile: AgentProfile) -> dict:
    return S.AgentProfileOut.model_validate(profile).model_dump(by_alias=True)


def update_profile(
    db: Session,
    ontology_id: str,
    body: S.AgentProfileUpdate,
    *,
    get_profile_fn: Callable[..., AgentProfile],
    profile_fields: list[str] = PROFILE_FIELDS,
    resettable_fields: set[str] = RESETTABLE_FIELDS,
) -> AgentProfile:
    """Apply one profile revision while preserving the original commit order."""
    profile = get_profile_fn(db, ontology_id)
    data = body.model_dump(exclude_unset=True, exclude={"reset_to_all"})
    for field, value in data.items():
        if field in profile_fields:
            setattr(profile, field, value)
    for field in body.reset_to_all:
        if field in resettable_fields:
            setattr(profile, field, None)
    db.commit()
    db.refresh(profile)
    return profile


def capability_summary(
    db: Session,
    ontology_id: str,
    release_id: str | None,
    *,
    build_scope_fn: Callable[..., tuple[Any, Any, Any]],
) -> dict:
    _, _, scope = build_scope_fn(db, ontology_id, release_id=release_id)
    return {
        **scope.summary(),
        "skillCard": scope.skill_card(),
        "releaseId": scope.release_id,
        "releaseVersion": (
            scope.release.version if scope.release else None
        ),
    }
