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
    actor=None,
    source: str = "admin_api",
) -> AgentProfile:
    """Apply one profile revision while preserving the original commit order.

    每次写入落一条审计（agent_profile_updated，含变更字段前后值）：profile
    的授权字段进入远程查询 plan 指纹，查询期间被改会触发
    query_context_changed 409 —— 审计让「是否真有并发配置变更」可直接查证。
    """
    profile = get_profile_fn(db, ontology_id)
    before = {field: getattr(profile, field, None) for field in profile_fields}
    data = body.model_dump(exclude_unset=True, exclude={"reset_to_all"})
    for field, value in data.items():
        if field in profile_fields:
            setattr(profile, field, value)
    for field in body.reset_to_all:
        if field in resettable_fields:
            setattr(profile, field, None)
    changed = {field: {"before": before[field], "after": getattr(profile, field, None)}
               for field in profile_fields
               if getattr(profile, field, None) != before[field]}
    if changed:
        from app.ontologies.inference.models import AuditLog
        db.add(AuditLog(
            ontology_id=ontology_id,
            event_type="edit", event_subtype="agent_profile_updated",
            user_id=str(actor.id) if getattr(actor, "id", None) else None,
            user_name=getattr(actor, "username", None),
            description="助手边界配置（profile）更新",
            object_type="agent_profile", object_id=str(profile.id),
            before_state={key: value["before"] for key, value in changed.items()},
            after_state={key: value["after"] for key, value in changed.items()},
            meta={"source": source, "changedFields": sorted(changed)}))
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
