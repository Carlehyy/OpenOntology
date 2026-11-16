"""Freeze rule choices and persist reviewed plans before any business write."""
from __future__ import annotations

import json

from sqlalchemy import select

from app.auth.models import User
from app.notifications.models import NotificationIngestKey
from app.super_assistant import multica_client, multica_service, provider

from . import facts, planner, schemas, service, store, templates
from .models import AutoLoopBinding, AutoLoopEvent, AutoLoopReceipt, AutoLoopRule


def _field(payload, path):
    value = payload
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def matches(rule, event, receipt):
    payload = receipt.payload if receipt else event.data
    conditions = rule.match
    actual = {"sourceKind": event.source_kind, "sourceSystem": event.source_system,
        "ingestKeyId": receipt.ingest_key_id if receipt else None,
        "channel": payload.get("channel")}
    for key, value in actual.items():
        if conditions.get(key) is not None and conditions[key] != value:
            return False
    for key, field in (("titleContains", "title"), ("bodyContains", "body")):
        if conditions.get(key) and conditions[key] not in str(payload.get(field) or ""):
            return False
    return all(_field(payload, item["field"]) == item["value"] for item in conditions.get("fieldEquals", []))


def prepare(db, event):
    existing = db.scalar(select(AutoLoopEvent).where(AutoLoopEvent.owner_id == event.owner_id,
        AutoLoopEvent.parent_event_id == event.id, AutoLoopEvent.type == "routing.prepared"))
    if existing:
        return existing
    receipt = db.get(AutoLoopReceipt, event.receipt_id) if event.receipt_id else None
    rules = db.scalars(select(AutoLoopRule).where(AutoLoopRule.owner_id == event.owner_id,
        AutoLoopRule.enabled.is_(True)).order_by(AutoLoopRule.position, AutoLoopRule.id))
    rule = next((item for item in rules if matches(item, event, receipt)), None)
    return store.append_event(db, owner_id=event.owner_id, receipt_id=event.receipt_id,
        type="routing.prepared", source_kind=event.source_kind, source_system=event.source_system,
        source_ref=event.source_ref, parent_event_id=event.id, hops=event.hops,
        action="pending", rule_id=rule.id if rule else None, rule_version=rule.version if rule else None,
        rule_snapshot=service.rule_out(rule) if rule else None, data={"originEventId": event.id})


def source_context(db, event, receipt):
    """Scope history by persisted key identity; reported source names grant no trust."""
    payload = receipt.payload if receipt else event.data
    if event.source_kind == "notification":
        key = db.get(NotificationIngestKey, receipt.ingest_key_id) if receipt and receipt.ingest_key_id else None
        if key is None or key.created_by != event.owner_id:
            return None, None
        if key.allowed_source_system and key.allowed_source_system != event.source_system:
            return None, None
        scope = "key:" + key.id
    elif event.source_kind == "schedule" and receipt and receipt.scheduled_task_id:
        scope = "schedule:" + receipt.scheduled_task_id
    elif event.type == "decision.answered":
        return None, None
    else:
        return None, None
    thread = payload.get("threadId") or payload.get("sourceThread") or (
        receipt.scheduled_task_id if event.source_kind == "schedule" and receipt else None)
    return scope, str(thread) if isinstance(thread, str) and 0 < len(thread) <= 512 else None


def verified_issue(config, token, reference):
    issue = multica_client.get_issue(config.base_url, token, config.workspace_id, reference)
    if not issue or not issue.get("id"):
        raise planner.PlanningError("issue_not_found")
    workspace = issue.get("workspace_id") or issue.get("workspaceId")
    if workspace != config.workspace_id:
        raise planner.PlanningError("issue_workspace_unverified")
    if reference not in {str(issue.get("id")), str(issue.get("identifier"))}:
        raise planner.PlanningError("issue_identity_mismatch")
    return facts.redact(issue, token)


def _trusted_target(db, event, receipt, snapshot, config, token):
    scope, thread = source_context(db, event, receipt)
    binding = snapshot.get("binding") if snapshot else None
    target = None
    provenance = None
    if binding:
        # Explicit rules constrain the actual source key when extracting input.
        if event.source_kind == "notification" and snapshot["match"].get("ingestKeyId") != receipt.ingest_key_id:
            raise planner.PlanningError("binding_requires_ingest_key_scope")
        target = templates.binding_target(binding, receipt.payload if receipt else event.data)
        provenance = {"kind": "explicit_rule", "ruleId": snapshot["id"], "ruleVersion": snapshot["version"]}
        if not target:
            raise planner.PlanningError("binding_target_missing")
    elif scope and thread:
        historical = db.scalar(select(AutoLoopBinding).where(AutoLoopBinding.owner_id == event.owner_id,
            AutoLoopBinding.workspace_id == config.workspace_id, AutoLoopBinding.source_scope == scope,
            AutoLoopBinding.source_thread == thread))
        if historical:
            if historical.provenance.get("baseUrl") != config.base_url:
                raise planner.PlanningError("binding_connector_changed")
            if historical.provenance.get("kind") in {"explicit_rule", "created_issue"}:
                target, provenance = historical.issue_key, historical.provenance
    if not target:
        return None, None, scope, thread, None
    issue = verified_issue(config, token, target)
    return str(issue.get("identifier") or issue["id"]), issue, scope, thread, provenance


def _decision(db, event, payload, *, source, reason, plan=None, workspace_id=None, issue=None):
    title = plan.title if plan and plan.title else "AutoLoop 事件需要确认"
    question = plan.question if plan and plan.question else "无法可靠执行此事件，请确认下一步处理方式。"
    need = "event:" + event.id
    evidence = {"factsSummary": reason, "untrustedOriginal": payload,
                "eventId": event.id, "issue": issue} if issue else {
                "factsSummary": reason, "untrustedOriginal": payload, "eventId": event.id}
    row, _ = store.create_or_update_decision(db, owner_id=event.owner_id, workspace_id=workspace_id,
        issue_key=str(issue.get("identifier") or issue["id"]) if issue else None,
        source=source, need_key=need, title=title, question=question,
        options=[item.model_dump() for item in plan.options] if plan else [], evidence=evidence,
        recommended_option_id=plan.recommendedOptionId if plan else None,
        priority=plan.priority if plan else "medium", origin_event_id=event.id)
    return row


def route_event(db, event, prepared):
    owner = db.get(User, event.owner_id, populate_existing=True)
    if owner is None or not owner.is_active:
        raise planner.PlanningError("owner_inactive")
    receipt = db.get(AutoLoopReceipt, event.receipt_id) if event.receipt_id else None
    payload = receipt.payload if receipt else event.data
    snapshot = prepared.rule_snapshot
    config = multica_service.active_config(db, event.owner_id)
    workspace_id = config.workspace_id if config else None
    provider.ensure_autoloop_available()
    attempt = db.begin_nested()
    try:
        if event.hops >= 8:
            raise planner.PlanningError("hop_limit_exceeded")
        token = multica_service.decrypt_token(config) if config else None
        target, issue, scope, thread, provenance = (None, None, None, None, None)
        if config:
            target, issue, scope, thread, provenance = _trusted_target(db, event, receipt, snapshot, config, token)
        if snapshot:
            rule = schemas.RuleCreate.model_validate({key: value for key, value in snapshot.items()
                                                      if key in schemas.RuleCreate.model_fields})
            rendered = service.preview(rule, payload, version=snapshot["version"])
            if len(rendered["renderedTitle"]) > 500 or not rendered["renderedTitle"].strip():
                raise templates.RenderError("template_render_limit", "标题超出任务标题限制")
            if rule.binding and not config:
                raise planner.PlanningError("multica_unavailable")
            plan = planner.Plan(action="append_task" if target else "create_task", reason="first_matching_rule",
                title=rendered["renderedTitle"], description=rendered["renderedDescription"],
                issueRef=target, priority=rule.priority or "medium", assignee=rule.assignee)
        else:
            plan = planner.plan_event(db, event=event, payload=payload,
                workspace_id=workspace_id, trusted_issue=target)
        if plan.action in {"create_task", "append_task"} and not config:
            raise planner.PlanningError("multica_unavailable")
        if plan.issueRef and plan.issueRef != target:
            raise planner.PlanningError("untrusted_issue_target")
        assignee_id = None
        if plan.assignee:
            agents = multica_client.list_agents(config.base_url, token, workspace_id)
            found = next((agent for agent in agents if str(agent.get("id")) == plan.assignee), None)
            assignee_id = str(found["id"]) if found else multica_client.match_agent(agents, plan.assignee)[0]
        decision, action = None, None
        if plan.action == "create_decision":
            decision = _decision(db, event, payload, source="assistant", reason=plan.reason,
                plan=plan, workspace_id=workspace_id, issue=issue)
        elif plan.action in {"create_task", "append_task"}:
            original = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
            description = (plan.description or "") + "\n\n" + templates.UNTRUSTED_LABEL + "\n" + original
            if len(description) > 131072:
                raise templates.RenderError("template_render_limit", "任务描述与原文超出资源限制")
            request = {"workspaceId": workspace_id, "baseUrl": config.base_url,
                "title": plan.title, "description": description, "issueRef": target,
                "priority": plan.priority, "assigneeId": assignee_id,
                "sourceScope": scope, "sourceThread": thread,
                "provenance": provenance, "ruleId": prepared.rule_id, "ruleVersion": prepared.rule_version}
            action, _ = store.persist_action(db, owner_id=event.owner_id, event_id=event.id,
                steps=[("issue.comment" if plan.action == "append_task" else "issue.create", request)])
        result = {"plan": plan.model_dump(exclude_none=True), "untrustedOriginal": payload}
        if issue:
            result["issue"] = issue
        store.append_event(db, owner_id=event.owner_id, receipt_id=event.receipt_id,
            type="routing.completed", source_kind=event.source_kind, source_system=event.source_system,
            source_ref=event.source_ref, parent_event_id=event.id, hops=event.hops,
            rule_id=prepared.rule_id, rule_version=prepared.rule_version, rule_snapshot=snapshot,
            action=plan.action, issue_ref=target, decision_item_id=decision.id if decision else None,
            action_id=action.id if action else None, data=result)
        attempt.commit()
    except provider.DailyLimitExceeded:
        attempt.rollback()
        raise
    except Exception as exc:
        attempt.rollback()
        # Remote/provider exception text may contain credentials or hostile text.
        reason = exc.code if isinstance(exc, templates.RenderError) else str(exc) if isinstance(exc, planner.PlanningError) else "routing_validation_failed"
        decision = _decision(db, event, payload, source="kernel_fallback", reason=reason, workspace_id=workspace_id)
        store.append_event(db, owner_id=event.owner_id, receipt_id=event.receipt_id,
            type="routing.fallback", source_kind=event.source_kind, source_system=event.source_system,
            source_ref=event.source_ref, parent_event_id=event.id, hops=event.hops,
            rule_id=prepared.rule_id, rule_version=prepared.rule_version, rule_snapshot=snapshot,
            action="kernel_fallback", error=reason, decision_item_id=decision.id if decision else None,
            data={"reason": reason})
