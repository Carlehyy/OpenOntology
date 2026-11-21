"""Read-only inbound trace projections for the routing workbench.

One trace per receipt: the anchor is the unique ``{source_kind}.received``
event, and prepared/terminal facts join by exact type plus parent. Watchdog
and decision-internal stage facts carry no receipt anchor and never form
traces. List, detail and routing stats share one SQL projection so filters
and counters cannot drift apart; filter and stats queries never read the
payload/data JSON columns.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from . import service, store
from .models import (
    AutoLoopAction, AutoLoopActionStep, AutoLoopDecision,
    AutoLoopEvent, AutoLoopReceipt, utcnow,
)

ROUTES = ("pending", "rule", "assistant")
OUTCOMES = (
    "pending", "queued", "sending", "review", "confirmed", "recorded",
    "failed", "unknown", "ignored", "handled", "cancelled",
)
STATS_DAYS = (7, 30)
# Bound the per-trace audit tail and the assistant reason summary so neither
# endpoint can grow with event churn.
AUDIT_LIMIT = 100
REASON_SUMMARY_CHARS = 200
_MAX_PARENT_HOPS = 12


def _anchor(owner_id):
    """Receipts joined with their one received event, keyed for pagination."""
    return select(
        AutoLoopReceipt.id.label("receipt_id"),
        AutoLoopReceipt.source_kind.label("source_kind"),
        AutoLoopReceipt.source_system.label("source_system"),
        AutoLoopReceipt.accepted_at.label("received_at"),
        AutoLoopReceipt.scheduled_task_id.label("scheduled_task_id"),
        AutoLoopReceipt.scheduled_run_id.label("scheduled_run_id"),
        AutoLoopEvent.id.label("event_id"),
        AutoLoopEvent.source_ref.label("source_ref"),
    ).select_from(AutoLoopReceipt).join(AutoLoopEvent, and_(
        AutoLoopEvent.receipt_id == AutoLoopReceipt.id,
        AutoLoopEvent.type == AutoLoopReceipt.source_kind.concat(".received"),
    )).where(AutoLoopReceipt.owner_id == owner_id, AutoLoopEvent.owner_id == owner_id).subquery()


def _prepared(owner_id):
    return select(
        AutoLoopEvent.parent_event_id.label("parent_id"),
        AutoLoopEvent.rule_id.label("rule_id"),
        AutoLoopEvent.rule_version.label("rule_version"),
        AutoLoopEvent.rule_snapshot.label("rule_snapshot"),
        AutoLoopEvent.seq.label("prepared_seq"),
    ).where(AutoLoopEvent.owner_id == owner_id, AutoLoopEvent.type == "routing.prepared").subquery()


def _terminal(owner_id):
    return select(
        AutoLoopEvent.id.label("terminal_id"),
        AutoLoopEvent.parent_event_id.label("parent_id"),
        AutoLoopEvent.type.label("terminal_type"),
        AutoLoopEvent.action.label("terminal_action"),
        AutoLoopEvent.issue_ref.label("terminal_issue_ref"),
        AutoLoopEvent.decision_item_id.label("decision_item_id"),
        AutoLoopEvent.error.label("terminal_error"),
    ).where(AutoLoopEvent.owner_id == owner_id,
            AutoLoopEvent.type.in_(("routing.completed", "routing.fallback"))).subquery()


def _trace_select(owner_id):
    """Shared route/outcome projection; list filters and stats reuse it."""
    anchor, prepared, terminal = _anchor(owner_id), _prepared(owner_id), _terminal(owner_id)
    action, decision, step = (
        AutoLoopAction.__table__, AutoLoopDecision.__table__, AutoLoopActionStep.__table__)
    decision_action = action.alias("decision_action")
    confirmed_comment = select(1).select_from(decision_action.join(
        step, step.c.action_id == decision_action.c.id)).where(
            decision_action.c.owner_id == owner_id,
            decision_action.c.decision_item_id == decision.c.id,
            step.c.kind == "issue.comment", step.c.state == "confirmed",
        ).exists().correlate(decision)
    # A create response carries an identifier even when there is no thread to bind.
    created_issue_ref = select(step.c.remote_object_id).where(
        step.c.action_id == action.c.id,
        step.c.kind == "issue.create", step.c.state == "confirmed",
        step.c.remote_object_id.is_not(None),
    ).order_by(step.c.ordinal).limit(1).scalar_subquery().correlate(action)
    decision_outcome = case(
        (decision.c.status == "open", "review"),
        (decision.c.status == "pending_dispatch", "queued"),
        (decision.c.status == "sending", "sending"),
        (decision.c.status == "writeback_unknown", "unknown"),
        (decision.c.status == "writeback_failed", "failed"),
        (decision.c.status == "completed", case((confirmed_comment, "confirmed"), else_="recorded")),
        (decision.c.status == "handled", "handled"),
        (decision.c.status == "withdrawn", "cancelled"),
        else_="failed",
    )
    action_outcome = case(
        (action.c.state == "pending", "queued"),
        (action.c.state == "sending", "sending"),
        (action.c.state == "unknown", "unknown"),
        (action.c.state == "failed", "failed"),
        (action.c.state == "partial", "failed"),
        (action.c.state == "confirmed", "confirmed"),
        (action.c.state == "cancelled", "cancelled"),
        else_="failed",
    )
    route = case(
        (prepared.c.rule_id.is_not(None), "rule"),
        (prepared.c.parent_id.is_not(None), "assistant"),
        else_="pending",
    )
    # Facts only: no terminal fact yet means routing has not finished; a
    # terminal without its effect rows must never read as success.
    outcome = case(
        (terminal.c.parent_id.is_(None), "pending"),
        (terminal.c.terminal_type == "routing.fallback", case(
            (terminal.c.decision_item_id.is_not(None), decision_outcome),
            else_="failed",
        )),
        (terminal.c.terminal_action == "ignore", "ignored"),
        (terminal.c.terminal_action.in_(("create_task", "append_task")), case(
            (action.c.id.is_not(None), action_outcome),
            else_="failed",
        )),
        (terminal.c.decision_item_id.is_not(None), decision_outcome),
        else_="failed",
    )
    query = select(
        anchor.c.event_id, anchor.c.receipt_id, anchor.c.received_at,
        anchor.c.source_kind, anchor.c.source_system, anchor.c.source_ref,
        anchor.c.scheduled_task_id, anchor.c.scheduled_run_id,
        prepared.c.rule_id, prepared.c.rule_version, prepared.c.rule_snapshot,
        terminal.c.terminal_id, terminal.c.terminal_type, terminal.c.terminal_action,
        terminal.c.terminal_issue_ref, terminal.c.decision_item_id, terminal.c.terminal_error,
        route.label("route"), outcome.label("outcome"),
        func.coalesce(terminal.c.terminal_issue_ref, created_issue_ref,
                      decision.c.issue_key).label("issue_ref"),
    ).select_from(anchor)
    # The dispatch lease serializes prepared and terminal facts per event.
    # Actions are unique per event and decisions join by primary key.
    query = query.join(prepared, prepared.c.parent_id == anchor.c.event_id, isouter=True)
    query = query.join(terminal, terminal.c.parent_id == anchor.c.event_id, isouter=True)
    query = query.join(action, and_(action.c.event_id == anchor.c.event_id,
                                  action.c.owner_id == owner_id), isouter=True)
    query = query.join(decision, and_(decision.c.id == terminal.c.decision_item_id,
                                    decision.c.owner_id == owner_id), isouter=True)
    return query, anchor, prepared, terminal, route, outcome


def _bounds(since, until):
    parsed = []
    for value in (since, until):
        if value is None:
            parsed.append(None)
            continue
        try:
            instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError) as exc:
            raise store.StoreConflict("invalid_time_range", http_status=422) from exc
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise store.StoreConflict("invalid_time_range", http_status=422)
        parsed.append(instant.astimezone(timezone.utc))
    return tuple(parsed)


def _reason(event):
    if event is None or not isinstance(event.data, dict):
        return None
    if event.type == "routing.fallback":
        value = event.data.get("reason")
    else:
        plan = event.data.get("plan")
        value = plan.get("reason") if isinstance(plan, dict) else None
    return value if isinstance(value, str) and value else None


def _summary(value):
    if not isinstance(value, str) or not value:
        return None
    return value if len(value) <= REASON_SUMMARY_CHARS else value[:REASON_SUMMARY_CHARS - 1] + "…"


def _item(row, payload, *, reason=None):
    ref = row.source_ref or ""
    if row.source_kind == "notification" and isinstance(payload, dict):
        ref = payload.get("notificationId") or ref
    rule = None
    if row.rule_id:
        snapshot = row.rule_snapshot if isinstance(row.rule_snapshot, dict) else {}
        rule = {"id": row.rule_id, "name": snapshot.get("name"), "version": row.rule_version}
    title = payload.get("title") if isinstance(payload, dict) else None
    return {"id": row.event_id, "receivedAt": service.iso(row.received_at),
        "title": title if isinstance(title, str) and title.strip() else None,
        "source": {"kind": row.source_kind, "system": row.source_system, "ref": str(ref)},
        "route": row.route, "outcome": row.outcome, "rule": rule,
        "action": row.terminal_action, "decisionItemId": row.decision_item_id,
        "issueRef": row.issue_ref, "scheduledTaskId": row.scheduled_task_id,
        "scheduledRunId": row.scheduled_run_id, "reason": reason}


def _assistant_reasons(db, rows):
    """Bounded page-side fetch: terminal facts of assistant-routed rows only."""
    identities = [row.terminal_id for row in rows
                  if row.route == "assistant" and row.terminal_id]
    if not identities:
        return {}
    facts = db.scalars(select(AutoLoopEvent).where(AutoLoopEvent.id.in_(identities)))
    return {fact.id: _summary(_reason(fact)) for fact in facts}


def list_traces(db: Session, owner_id, *, cursor=None, limit=20, source=None,
                sourceSystem=None, route=None, action=None, outcome=None, ruleId=None,
                since=None, until=None, scheduledTaskId=None, scheduledRunId=None):
    if route is not None and route not in ROUTES:
        raise store.StoreConflict("invalid_route", http_status=422)
    if outcome is not None and outcome not in OUTCOMES:
        raise store.StoreConflict("invalid_outcome", http_status=422)
    since_at, until_at = _bounds(since, until)
    filters = {key: value for key, value in {
        "source": source, "sourceSystem": sourceSystem, "route": route, "action": action,
        "outcome": outcome, "ruleId": ruleId, "since": since, "until": until,
        "scheduledTaskId": scheduledTaskId, "scheduledRunId": scheduledRunId,
    }.items() if value is not None}
    query, anchor, prepared, terminal, route_expr, outcome_expr = _trace_select(owner_id)
    if source is not None:
        query = query.where(anchor.c.source_kind == source)
    if sourceSystem is not None:
        query = query.where(anchor.c.source_system == sourceSystem)
    if ruleId is not None:
        query = query.where(prepared.c.rule_id == ruleId)
    if action is not None:
        query = query.where(terminal.c.terminal_action == action)
    if scheduledTaskId is not None:
        query = query.where(anchor.c.scheduled_task_id == scheduledTaskId)
    if scheduledRunId is not None:
        query = query.where(anchor.c.scheduled_run_id == scheduledRunId)
    if route is not None:
        query = query.where(route_expr == route)
    if outcome is not None:
        query = query.where(outcome_expr == outcome)
    if since_at is not None:
        query = query.where(anchor.c.received_at >= since_at)
    if until_at is not None:
        query = query.where(anchor.c.received_at < until_at)
    if cursor:
        instant, identity = service._decode(cursor, "traces", owner_id, filters)
        query = query.where(or_(anchor.c.received_at < instant,
            and_(anchor.c.received_at == instant, anchor.c.event_id < identity)))
    rows = db.execute(query.order_by(
        anchor.c.received_at.desc(), anchor.c.event_id.desc()).limit(limit + 1)).all()
    more, rows = len(rows) > limit, rows[:limit]
    payloads, reasons = {}, {}
    if rows:
        payloads = dict(db.execute(select(AutoLoopReceipt.id, AutoLoopReceipt.payload).where(
            AutoLoopReceipt.id.in_([row.receipt_id for row in rows]))).all())
        reasons = _assistant_reasons(db, rows)
    items = []
    for row in rows:
        reason = reasons.get(row.terminal_id) if row.route == "assistant" else None
        items.append(_item(row, payloads.get(row.receipt_id), reason=reason))
    next_cursor = None
    if more and rows:
        last = rows[-1]
        next_cursor = service._cursor("traces", owner_id, filters,
            [service._timestamp(last.received_at), last.event_id])
    return {"items": items, "nextCursor": next_cursor, "hasMore": more}


def _resolve_anchor(db: Session, owner_id, event):
    """Any stage fact of a trace resolves to its inbound anchor, or nothing."""
    current = event
    for _ in range(_MAX_PARENT_HOPS):
        if current is None or current.owner_id != owner_id:
            return None, None
        if current.receipt_id:
            receipt = db.get(AutoLoopReceipt, current.receipt_id)
            if receipt is not None:
                anchor_event = db.scalar(select(AutoLoopEvent).where(
                    AutoLoopEvent.receipt_id == receipt.id,
                    AutoLoopEvent.type == f"{receipt.source_kind}.received",
                ).order_by(AutoLoopEvent.seq))
                return (receipt, anchor_event) if anchor_event is not None else (None, None)
        # AutoLoopEvent is keyed by seq; stage links use the event id.
        current = db.scalar(select(AutoLoopEvent).where(
            AutoLoopEvent.id == current.parent_event_id)) if current.parent_event_id else None
    return None, None


def _trace_action(db: Session, owner_id, row):
    query = select(AutoLoopAction).where(AutoLoopAction.owner_id == owner_id)
    if row.terminal_action in ("create_task", "append_task"):
        return db.scalar(query.where(AutoLoopAction.event_id == row.event_id))
    if row.decision_item_id:
        return db.scalar(query.where(AutoLoopAction.decision_item_id == row.decision_item_id))
    return None


def _issue_evidence(db: Session, owner_id, row, steps):
    for step in steps:
        if step.state != "confirmed" or step.kind not in ("issue.create", "issue.comment"):
            continue
        result = step.remote_result if isinstance(step.remote_result, dict) else {}
        if isinstance(result.get("issue"), dict):
            return result["issue"]
    if row.decision_item_id:
        decision = db.get(AutoLoopDecision, row.decision_item_id)
        evidence = decision.current_evidence if decision and decision.owner_id == owner_id else None
        candidate = evidence.get("issue") if isinstance(evidence, dict) else None
        if isinstance(candidate, dict):
            return candidate
    return None


def get_trace(db: Session, owner_id, event_id):
    event = db.scalar(select(AutoLoopEvent).where(AutoLoopEvent.id == event_id))
    if event is None or event.owner_id != owner_id:
        raise store.StoreConflict("not_found", http_status=404)
    receipt, anchor_event = _resolve_anchor(db, owner_id, event)
    if receipt is None or anchor_event is None:
        # Legal stage facts without an inbound receipt (watchdog, evidence
        # scans) have no routing trace; the raw event stays on GET /events/:id.
        return None
    if receipt.owner_id != owner_id or anchor_event.owner_id != owner_id:
        raise store.StoreConflict("not_found", http_status=404)
    query, anchor = _trace_select(owner_id)[:2]
    row = db.execute(query.where(anchor.c.event_id == anchor_event.id)).first()
    if row is None:
        return None
    steps = service._steps(db, _trace_action(db, owner_id, row))
    terminal = db.scalar(select(AutoLoopEvent).where(
        AutoLoopEvent.id == row.terminal_id)) if row.terminal_id else None
    issue = _issue_evidence(db, owner_id, row, steps)
    audit = list(db.scalars(select(AutoLoopEvent).where(
        AutoLoopEvent.parent_event_id == anchor_event.id, AutoLoopEvent.owner_id == owner_id)
        .order_by(AutoLoopEvent.seq.desc()).limit(AUDIT_LIMIT)))
    result = _item(row, receipt.payload)
    result.update(untrustedOriginal=receipt.payload, reason=_reason(terminal),
        error=row.terminal_error,
        issueUrl=service._issue_url(db, owner_id, {"issue": issue}) if issue else None,
        actionSteps=[{**service.step_out(step), "remoteObjectId": step.remote_object_id
            if step.state == "confirmed" and step.kind in ("issue.create", "issue.comment") else None}
            for step in steps],
        auditEvents=[{"id": item.id, "type": item.type,
            "happenedAt": service.iso(item.happened_at), "action": item.action,
            "error": item.error} for item in reversed(audit)])
    return result


def _rule_names(db: Session, anchor, prepared, since, until):
    """Latest frozen snapshot name per rule inside the window."""
    ranked = select(
        prepared.c.rule_id, prepared.c.rule_snapshot,
        func.row_number().over(partition_by=prepared.c.rule_id,
            order_by=prepared.c.prepared_seq.desc()).label("rank"),
    ).select_from(anchor).join(prepared, prepared.c.parent_id == anchor.c.event_id).where(
        anchor.c.received_at >= since, anchor.c.received_at < until,
        prepared.c.rule_id.is_not(None)).subquery()
    rows = db.execute(select(ranked.c.rule_id, ranked.c.rule_snapshot)
        .where(ranked.c.rank == 1)).all()
    return {identity: snapshot.get("name") if isinstance(snapshot, dict) else None
            for identity, snapshot in rows}


def routing_stats(db: Session, owner_id, *, days):
    if days not in STATS_DAYS:
        raise store.StoreConflict("invalid_days", http_status=422)
    until = utcnow()
    since = until - timedelta(days=days)
    query, anchor, prepared = _trace_select(owner_id)[:3]
    window = query.where(anchor.c.received_at >= since,
                         anchor.c.received_at < until).subquery()

    counts = db.execute(select(window.c.route, window.c.outcome, window.c.rule_id,
        func.count(func.distinct(window.c.receipt_id))).group_by(
        window.c.route, window.c.outcome, window.c.rule_id))
    routes = dict.fromkeys(ROUTES, 0)
    outcomes = dict.fromkeys(OUTCOMES, 0)
    hits = {}
    for route, outcome, rule_id, total in counts:
        routes[route] += total
        outcomes[outcome] += total
        if rule_id is not None:
            hits[rule_id] = hits.get(rule_id, 0) + total
    received = sum(routes.values())
    names = _rule_names(db, anchor, prepared, since, until) if hits else {}
    rules = sorted(({"ruleId": identity, "ruleName": names.get(identity), "hits": total}
                    for identity, total in hits.items()),
                   key=lambda item: (-item["hits"], item["ruleId"]))
    return {"days": days, "since": service.iso(since), "until": service.iso(until),
        "received": received, "matched": routes.get("rule", 0),
        "unmatched": routes.get("assistant", 0), "pendingRoute": routes.get("pending", 0),
        "outcomes": {outcome: outcomes.get(outcome, 0) for outcome in OUTCOMES},
        "rules": rules}
