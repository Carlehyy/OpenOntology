"""Owner routing policy: what a rule miss asks the model, and which model.

The policy is an append-only version history per owner (version 0 is the
built-in default). It is applied only to the read-only miss assessment; a rule
hit, an answered card and the watchdog never consult it.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.auth.models import User
from app.super_assistant import multica_service, provider

from . import planner, service, store

MAX_SAMPLE_BYTES = 32768
# Event sources a preview can imitate, mapped to the received fact type a real
# inbound event of that source carries.
PREVIEW_EVENT_TYPES = {"notification": "notification.received", "schedule": "schedule.received"}

DEFAULT_SUMMARY = ("默认研判策略：未命中任何已启用规则的事件由超级助手只读研判，"
                   "只能生成待确认决策卡或忽略，不会写入外部系统。")
CUSTOM_SUMMARY = ("自定义研判策略 v{version}：未命中任何已启用规则的事件按自定义提示只读研判，"
                  "只能生成待确认决策卡或忽略，不会写入外部系统。")


def normalize_prompt(text: str | None) -> str | None:
    """Trimmed text; blank means the default."""
    if text is None:
        return None
    return text.strip() or None


def _version(row) -> int:
    return row.version if row else 0


def _text(row) -> str | None:
    return row.prompt_text if row else None


def selected_model_id(db) -> str | None:
    """The model the miss assessment selects now; None when there is none.

    An unusable explicit setting is reported by routing itself.
    """
    try:
        config = planner.resolve_model(db)
    except planner.PlanningError:
        return None
    return config.id if config is not None else None


def policy_out(db, row) -> dict:
    version, text = _version(row), _text(row)
    return {"version": version, "promptText": text,
            "updatedAt": service.iso(row.created_at) if row else None,
            "updatedBy": row.created_by if row else None,
            "modelConfigId": selected_model_id(db),
            "policySummary": DEFAULT_SUMMARY if text is None
            else CUSTOM_SUMMARY.format(version=version)}


def state(db, owner_id) -> dict:
    return policy_out(db, store.latest_routing_policy(db, owner_id))


def _lock_owner(db, owner_id) -> None:
    # The same lock the Multica profile writers take: FOR NO KEY UPDATE
    # serializes this owner's policy writers without blocking inserts whose
    # foreign key only takes a key share on the user row. SQLite ignores the
    # clause, so the (owner, version) key stays the final arbiter.
    db.execute(select(User.id).where(User.id == owner_id).with_for_update(key_share=True))


def update(db, owner_id, actor_id, body) -> dict:
    """Append one version, answer a replay, or report a stale differing change.

    A retry that carries the same normalized content as the stored version
    answers the current state instead of a conflict, so it never creates a
    duplicate version. A stale request with different content is a conflict.
    """
    _lock_owner(db, owner_id)
    text = normalize_prompt(body.promptText)
    row = store.latest_routing_policy(db, owner_id)
    if body.expectedVersion == _version(row) and text != _text(row):
        previous = _version(row)
        try:
            row = store.append_routing_policy(db, owner_id=owner_id, version=previous + 1,
                                              prompt_text=text, created_by=actor_id)
        except IntegrityError:
            # Unreachable while the row lock holds; wherever the lock is
            # ignored, the version key decides and the winner's row is read.
            row = store.latest_routing_policy(db, owner_id)
            if row is None or _version(row) <= previous:
                raise
    if body.expectedVersion != _version(row) and text != _text(row):
        raise store.StoreConflict("routing_policy_version_conflict", http_status=412,
                                  current=policy_out(db, row))
    return policy_out(db, row)


def freeze(db, owner_id) -> dict:
    """What a miss will be assessed under, stored on the prepared fact."""
    return {"routingPromptVersion": _version(store.latest_routing_policy(db, owner_id)),
            "modelConfigId": selected_model_id(db)}


def frozen(db, prepared) -> dict | None:
    """Resolve a frozen miss to the immutable prompt of the version it cites.

    None for facts prepared before a policy was frozen and for rule hits: both
    keep routing exactly as they were prepared.
    """
    data = prepared.data if isinstance(prepared.data, dict) else {}
    if "routingPromptVersion" not in data:
        return None
    version = data["routingPromptVersion"]
    row = store.routing_policy_version(db, prepared.owner_id, version)
    return {"routingPromptVersion": version, "modelConfigId": data.get("modelConfigId"),
            "promptText": row.prompt_text if row else None}


def record(frozen_policy: dict | None) -> dict | None:
    """What a terminal fact cites: the version and the frozen model.

    None unless the planner was entered under a frozen policy, so facts of rule
    hits and of events prepared before any policy stay exactly as before.
    """
    if frozen_policy is None:
        return None
    return {key: frozen_policy[key] for key in ("routingPromptVersion", "modelConfigId")}


def used(frozen_record: dict | None, audit: dict) -> dict | None:
    """The record with the model the plan was actually sent to, when known."""
    if frozen_record and audit.get("modelConfigId"):
        return {**frozen_record, "modelConfigId": audit["modelConfigId"]}
    return frozen_record


def _bounded_sample(sample: dict) -> dict:
    try:
        size = len(json.dumps(sample, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise store.StoreConflict("sample_event_invalid", http_status=422) from exc
    if size > MAX_SAMPLE_BYTES:
        raise store.StoreConflict("sample_event_too_large", http_status=422)
    return sample


def preview(db, owner_id, body) -> dict:
    """Assess a sample event under a draft policy; never writes.

    A trial on a stale base is refused exactly like a stale update, so the
    owner is never shown an outcome the live policy could not produce. The
    sample is untrusted data and only ever fills the event slot of the planner
    input. The planner, its limits and its output acceptance are the ones a
    live miss uses.

    The call spends real provider quota, but it is the owner's trial and does
    not stop formal AutoLoop: a daily-cap answer is reported without setting
    the process-wide stop (see provider.daily_limit_stop_disabled).
    """
    sample = _bounded_sample(body.sampleEvent)
    text = normalize_prompt(body.promptText)
    row = store.latest_routing_policy(db, owner_id)
    version, draft = _version(row), text != _text(row)
    if draft and body.expectedVersion != version:
        raise store.StoreConflict("routing_policy_version_conflict", http_status=412,
                                  current=policy_out(db, row))
    # A live miss passes the owner's active workspace id as context only.
    config = multica_service.active_config(db, owner_id)
    audit: dict = {}
    with provider.daily_limit_stop_disabled():
        plan = planner.plan_event(db, event=SimpleNamespace(type=PREVIEW_EVENT_TYPES[body.eventType]),
            payload=sample, workspace_id=config.workspace_id if config else None,
            decision_only=True, decision_purpose="miss", routing_prompt=text, audit=audit)
    if plan.assignee or plan.issueRef:
        # The same acceptance a live miss applies: a read-only outcome never
        # carries a target or an assignee.
        raise planner.PlanningError("invalid_readonly_action")
    return {"action": plan.action, "reason": plan.reason, "modelConfigId": audit["modelConfigId"],
            "routingPromptVersion": version, "isDraft": draft}
