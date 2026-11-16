"""Owner filtering, public projections, rules and stable cursor pagination."""
from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timezone

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.orm import Session

from . import schemas, store, templates
from .models import (
    TERMINAL, UNFINISHED, AutoLoopAction, AutoLoopActionStep, AutoLoopDecision,
    AutoLoopEvent, AutoLoopReceipt, AutoLoopRule, utcnow,
)

_RULE_FIELDS = {"titleTemplate": "title_template", "descriptionTemplate": "description_template"}
_PRIORITY = case((AutoLoopDecision.priority == "high", 0),
                 (AutoLoopDecision.priority == "medium", 1), else_=2)
_EVENT_ACTIONS = {
    "accepted", "awaiting_owner", "pending", "claimed", "done", "failed", "open",
    "pending_dispatch", "sending", "writeback_unknown", "writeback_failed", "completed",
    "withdrawn", "handled", "unknown", "partial", "confirmed", "cancelled", "queued",
    "never_sent", "create_task", "append_task", "create_decision", "ignore", "kernel_fallback",
}


def iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _owned(db, model, owner_id, identity):
    column = model.id
    row = db.scalar(select(model).where(column == identity, model.owner_id == owner_id))
    if row is None:
        raise store.StoreConflict("not_found", http_status=404)
    return row


def rule_out(row: AutoLoopRule) -> dict:
    return {"id": row.id, "name": row.name, "enabled": row.enabled, "position": row.position,
        "match": row.match, "titleTemplate": row.title_template,
        "descriptionTemplate": row.description_template, "priority": row.priority,
        "assignee": row.assignee, "binding": row.binding, "version": row.version,
        "createdAt": iso(row.created_at), "updatedAt": iso(row.updated_at)}


def rules(db, owner_id):
    return [rule_out(row) for row in db.scalars(select(AutoLoopRule).where(
        AutoLoopRule.owner_id == owner_id).order_by(AutoLoopRule.position, AutoLoopRule.id))]


def get_rule(db, owner_id, identity):
    return rule_out(_owned(db, AutoLoopRule, owner_id, identity))


def _rule_values(body):
    values = body.model_dump(exclude_none=True)
    templates.validate_templates(body.titleTemplate, body.descriptionTemplate)
    templates.validate_binding(values.get("binding"))
    values["name"] = values["name"].strip()
    return {_RULE_FIELDS.get(key, key): value for key, value in values.items()}


def create_rule(db, owner_id, body):
    row = AutoLoopRule(owner_id=owner_id, **_rule_values(body))
    db.add(row)
    db.flush()
    return rule_out(row)


def patch_rule(db, owner_id, identity, body):
    row = db.scalar(select(AutoLoopRule).where(AutoLoopRule.id == identity,
        AutoLoopRule.owner_id == owner_id).with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise store.StoreConflict("not_found", http_status=404)
    current = {key: value for key, value in rule_out(row).items()
               if key in schemas.RuleCreate.model_fields}
    current.update(body.model_dump(exclude_unset=True))
    merged = schemas.RuleCreate.model_validate(current)
    values = _rule_values(merged)
    # Nullable fields must also clear when explicitly patched to null.
    for key in ("priority", "assignee", "binding"):
        values[key] = getattr(merged, key).model_dump(exclude_none=True) if key == "binding" and merged.binding else getattr(merged, key)
    result = db.execute(update(AutoLoopRule).where(AutoLoopRule.id == identity,
        AutoLoopRule.owner_id == owner_id, AutoLoopRule.version == row.version).values(
            **values, version=row.version + 1, updated_at=utcnow()))
    if result.rowcount != 1:
        raise store.StoreConflict("rule_version_conflict")
    db.refresh(row)
    return rule_out(row)


def delete_rule(db, owner_id, identity):
    db.delete(_owned(db, AutoLoopRule, owner_id, identity))
    db.flush()


def preview(body, sample, *, version=0):
    values = _rule_values(body)
    from app.shared.config import settings
    title, description = templates.render(values["title_template"], values["description_template"], sample,
        budget_ms=settings.autoloop_template_budget_ms)
    return {"renderedTitle": title, "renderedDescription": description,
        "untrustedOriginal": sample, "priority": body.priority, "assignee": body.assignee,
        "bindingTarget": templates.binding_target(values.get("binding"), sample),
        "ruleVersion": version, "warnings": []}


def preview_stored(db, owner_id, identity, sample):
    row = _owned(db, AutoLoopRule, owner_id, identity)
    value = {key: item for key, item in rule_out(row).items() if key in schemas.RuleCreate.model_fields}
    return preview(schemas.RuleCreate.model_validate(value), sample, version=row.version)


def _cursor(kind, owner_id, filters, values):
    payload = [kind, owner_id, filters, values]
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")


def _decode(cursor, kind, owner_id, filters):
    if not cursor or len(cursor) > 8192:
        raise store.StoreConflict("invalid_cursor", http_status=400)
    try:
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        payload = json.loads(raw)
        if not isinstance(payload, list) or len(payload) != 4 or payload[:3] != [kind, owner_id, filters]:
            raise ValueError
        values = payload[3]
        if not isinstance(values, list) or len(values) != (3 if kind == "decisions" else 2):
            raise ValueError
        instant = datetime.fromisoformat(values[-2])
        if instant.tzinfo is None or not isinstance(values[-1], str) or not values[-1]:
            raise ValueError
        if kind == "decisions" and (type(values[0]) is not int or values[0] not in (0, 1, 2)):
            raise ValueError
        return values[:-2] + [instant, values[-1]]
    except (ValueError, TypeError, binascii.Error, UnicodeError, IndexError) as exc:
        raise store.StoreConflict("invalid_cursor", http_status=400) from exc


def _timestamp(value):
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).isoformat(timespec="microseconds")


def _statuses(value, allowed):
    if value is None:
        return []
    values = value.split(",")
    if not values or any(item not in allowed for item in values):
        raise store.StoreConflict("invalid_status", http_status=422)
    return sorted(set(values))


def step_out(row):
    return {"id": row.id, "kind": row.kind, "state": row.state, "error": row.error}


def _steps(db, action):
    return list(db.scalars(select(AutoLoopActionStep).where(
        AutoLoopActionStep.action_id == action.id).order_by(AutoLoopActionStep.ordinal))) if action else []


def event_out(db, row, *, detail=False):
    receipt = db.get(AutoLoopReceipt, row.receipt_id) if row.receipt_id else None
    ref = row.source_ref or ""
    if row.source_kind == "notification" and receipt:
        ref = receipt.payload.get("notificationId") or ref
    result = {"id": row.id, "seq": row.seq, "type": row.type,
        "source": {"kind": row.source_kind, "system": row.source_system, "ref": ref},
        "causality": {"parentEventId": row.parent_event_id, "hops": row.hops},
        "happenedAt": iso(row.happened_at), "ruleVersion": row.rule_version,
        "action": row.action, "issueRef": row.issue_ref,
        "decisionItemId": row.decision_item_id, "error": row.error}
    if detail:
        action = db.get(AutoLoopAction, row.action_id) if row.action_id else db.scalar(
            select(AutoLoopAction).where(AutoLoopAction.event_id == row.id))
        result.update(untrustedOriginal=receipt.payload if receipt else row.data.get("untrustedOriginal", row.data),
            actionSteps=[step_out(item) for item in _steps(db, action)],
            dedup={"key": receipt.dedup_key if receipt else row.correlation_id or row.id},
            scheduledTaskId=receipt.scheduled_task_id if receipt else None,
            scheduledRunId=receipt.scheduled_run_id if receipt else None,
            issueUrl=_issue_url(db, row.owner_id, row.data))
    return result


def _issue_url(db, owner_id, evidence):
    from app.super_assistant import multica_client, multica_service
    config = multica_service.get_config(db, owner_id)
    issue = evidence.get("issue")
    if not config or not config.base_url or not isinstance(issue, dict):
        return None
    try:
        return multica_client.verified_issue_url(config.base_url, issue)
    except multica_client.MulticaClientError:
        return None


def list_events(db, owner_id, *, cursor=None, limit=20, **filters):
    normalized = {key: value for key, value in filters.items() if value is not None}
    statuses = _statuses(filters.get("status"), _EVENT_ACTIONS)
    query = select(AutoLoopEvent).where(AutoLoopEvent.owner_id == owner_id)
    columns = {"source": AutoLoopEvent.source_kind, "sourceSystem": AutoLoopEvent.source_system,
               "action": AutoLoopEvent.action, "ruleId": AutoLoopEvent.rule_id}
    for key, column in columns.items():
        if filters.get(key) is not None:
            query = query.where(column == filters[key])
    if statuses:
        effect = select(AutoLoopAction.id).where(AutoLoopAction.owner_id == owner_id,
            or_(AutoLoopAction.event_id == AutoLoopEvent.id, AutoLoopAction.id == AutoLoopEvent.action_id),
            AutoLoopAction.state.in_(statuses)).exists()
        query = query.where(or_(AutoLoopEvent.action.in_(statuses), effect))
    if filters.get("scheduledTaskId") or filters.get("scheduledRunId"):
        query = query.join(AutoLoopReceipt, AutoLoopReceipt.id == AutoLoopEvent.receipt_id)
        for key, column in (("scheduledTaskId", AutoLoopReceipt.scheduled_task_id),
                            ("scheduledRunId", AutoLoopReceipt.scheduled_run_id)):
            if filters.get(key):
                query = query.where(column == filters[key])
    if cursor:
        instant, identity = _decode(cursor, "events", owner_id, normalized)
        query = query.where(or_(AutoLoopEvent.happened_at < instant,
            and_(AutoLoopEvent.happened_at == instant, AutoLoopEvent.id < identity)))
    rows = list(db.scalars(query.order_by(AutoLoopEvent.happened_at.desc(), AutoLoopEvent.id.desc()).limit(limit + 1)))
    more, rows = len(rows) > limit, rows[:limit]
    next_cursor = _cursor("events", owner_id, normalized, [_timestamp(rows[-1].happened_at), rows[-1].id]) if more else None
    return {"items": [event_out(db, row) for row in rows], "nextCursor": next_cursor, "hasMore": more}


def get_event(db, owner_id, identity):
    return event_out(db, _owned(db, AutoLoopEvent, owner_id, identity), detail=True)


def decision_out(db, row, *, replay=False):
    evidence = row.current_evidence or {}
    action = db.scalar(select(AutoLoopAction).where(AutoLoopAction.decision_item_id == row.id))
    events = list(db.scalars(select(AutoLoopEvent.id).where(
        AutoLoopEvent.owner_id == row.owner_id, AutoLoopEvent.decision_item_id == row.id).order_by(AutoLoopEvent.seq)))
    if row.origin_event_id and row.origin_event_id not in events:
        events.insert(0, row.origin_event_id)
    result = {"id": row.id, "source": row.source, "status": row.status, "title": row.title,
        "question": row.question, "factsSummary": evidence.get("factsSummary") or row.question,
        "options": row.options, "recommendedOptionId": row.recommended_option_id,
        "freeTextAllowed": True, "issueRef": row.issue_key, "issueUrl": _issue_url(db, row.owner_id, evidence),
        "eventIds": events, "untrustedOriginal": evidence.get("untrustedOriginal"),
        "priority": row.priority, "contentVersion": row.content_version,
        "createdAt": iso(row.created_at), "updatedAt": iso(row.updated_at),
        "writebackSteps": [step_out(step) for step in _steps(db, action)],
        "resolution": None, "alreadyProcessed": replay}
    if row.status in TERMINAL:
        value = row.submitted_value or {}
        chosen = next((item for item in row.options if item.get("id") == value.get("optionId")), {})
        result["resolution"] = {"kind": row.status, "optionId": value.get("optionId"),
            "optionLabel": chosen.get("label"), "freeText": value.get("freeText"),
            "actor": row.closed_by or row.submitted_by or "system:watchdog",
            "at": iso(row.closed_at or row.updated_at), "reason": row.close_reason}
    return result


def decision_result(db, row, request, *, replay):
    result = decision_out(db, row, replay=replay)
    if replay:
        result["contentVersion"] = request.result["contentVersion"]
        if row.status not in TERMINAL:
            result.update({key: request.result[key] for key in ("status", "writebackSteps")})
            result["updatedAt"] = iso(datetime.fromisoformat(request.result["updatedAt"]))
    return result


def get_decision(db, owner_id, identity):
    return decision_out(db, _owned(db, AutoLoopDecision, owner_id, identity))


def list_decisions(db, owner_id, *, status=None, cursor=None, limit=20):
    statuses = _statuses(status, set(UNFINISHED + TERMINAL))
    filters = {"status": statuses}
    query = select(AutoLoopDecision).where(AutoLoopDecision.owner_id == owner_id)
    if statuses:
        query = query.where(AutoLoopDecision.status.in_(statuses))
    if cursor:
        rank, instant, identity = _decode(cursor, "decisions", owner_id, filters)
        query = query.where(or_(_PRIORITY > rank, and_(_PRIORITY == rank,
            or_(AutoLoopDecision.created_at > instant,
                and_(AutoLoopDecision.created_at == instant, AutoLoopDecision.id > identity)))))
    # Higher priority first, then oldest waiting card, finally UUID ascending.
    rows = list(db.scalars(query.order_by(_PRIORITY, AutoLoopDecision.created_at,
        AutoLoopDecision.id).limit(limit + 1)))
    more, rows = len(rows) > limit, rows[:limit]
    last = rows[-1] if rows else None
    next_cursor = _cursor("decisions", owner_id, filters,
        [{"high": 0, "medium": 1, "low": 2}[last.priority], _timestamp(last.created_at), last.id]) if more else None
    return {"items": [decision_out(db, row) for row in rows], "nextCursor": next_cursor, "hasMore": more}


def summary(db, owner_id):
    counts = dict(db.execute(select(AutoLoopDecision.status, func.count()).where(
        AutoLoopDecision.owner_id == owner_id, AutoLoopDecision.status.in_(UNFINISHED)
    ).group_by(AutoLoopDecision.status)).all())
    names = {"openCount": "open", "pendingDispatchCount": "pending_dispatch", "sendingCount": "sending",
             "unknownCount": "writeback_unknown", "failedCount": "writeback_failed"}
    result = {name: counts.get(state, 0) for name, state in names.items()}
    return {**result, "unfinishedTotal": sum(result.values())}
