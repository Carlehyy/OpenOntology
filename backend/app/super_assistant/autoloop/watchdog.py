"""Read complete evidence streams and reconcile durable human decision needs.

Every scan observes the effective (current-revision) workspace for discovery
and additionally rechecks each unresolved historical source (at least the
previous revision after an A→B switch) using the SAME frozen revision, so a
profile switch cannot mix evidence identities across workspaces. Evidence
from a foreign endpoint/workspace is isolated before anything is persisted,
and per-source scans are bounded with least-recently-scanned-first rotation
so open cards in old sources cannot starve.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select

from app.auth.models import User
from app.model_configs.models import ModelConfig
from app.shared.config import settings
from app.shared.database import SessionLocal
from app.super_assistant import multica_client, multica_service, provider
from app.super_assistant.models import (
    SuperAssistantMulticaConfig,
    SuperAssistantMulticaProfile,
)

from . import facts, planner, sourcing, store
from .models import (
    UNFINISHED, AutoLoopAction, AutoLoopActionStep,
    AutoLoopConsumedNeed, AutoLoopDecision, AutoLoopIssueProgress, utcnow,
)

logger = logging.getLogger(__name__)
_FAILURE_CAUSES = {"task_failed", "task_failure", "failure_recovery", "failed_task_recovery"}
# Bounded scans: issues per source and historical sources per owner scan. The
# least-recently-scanned-first ordering rotates the remainder into the next
# scan instead of starving it.
_ISSUES_PER_SOURCE = 50
_HISTORICAL_SOURCES_PER_SCAN = 5


def _instant(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def _as_utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _details(item):
    value = item.get("details") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _task_ids(items):
    found = set()
    for item in items:
        details = _details(item)
        for value in (item.get("task_id"), item.get("source_task_id"), details.get("task_id")):
            if isinstance(value, str) and 0 < len(value) <= 255:
                found.add(value)
    return found


def attention(issue, timeline, *, now=None):
    """Todo requires both a recent failed run and an attributable recovery."""
    status = issue.get("status")
    if status in {"blocked", "in_review"}:
        return True
    if status != "todo":
        return False
    cutoff = (now or utcnow()) - timedelta(hours=settings.autoloop_recent_failure_hours)
    failures, recoveries = set(), set()
    for item in timeline:
        details = _details(item)
        task_id = details.get("task_id") or item.get("task_id")
        happened = _instant(item.get("created_at"))
        if not task_id or not happened or happened < cutoff:
            continue
        if item.get("action") in {"task_failed", "task.failed"} or details.get("task_status") == "failed":
            failures.add(task_id)
        before = details.get("from") or details.get("old_value") or details.get("prev_status")
        after = details.get("to") or details.get("new_value") or details.get("status")
        cause = details.get("cause") or details.get("reason")
        if before == "in_progress" and after == "todo" and cause in _FAILURE_CAUSES:
            recoveries.add(task_id)
            if details.get("task_status") == "failed" or details.get("failure_reason"):
                failures.add(task_id)
    return bool(failures & recoveries)


def _own_comments(db, owner, workspace, reference):
    confirmed, unknown = set(), False
    steps = db.scalars(select(AutoLoopActionStep).join(AutoLoopAction,
        AutoLoopAction.id == AutoLoopActionStep.action_id).where(
            AutoLoopAction.owner_id == owner, AutoLoopActionStep.kind == "issue.comment"))
    for step in steps:
        payload = step.request_payload
        if payload.get("workspaceId") != workspace or payload.get("issueRef") != reference:
            continue
        if step.state == "confirmed" and step.remote_object_id:
            confirmed.add(step.remote_object_id)
        if step.state in {"sending", "unknown"}:
            unknown = True
    return confirmed, unknown


def _watermark(comments, timeline, messages, tasks):
    return {"commentIds": sorted(str(item["id"]) for item in comments),
        "timelineIds": sorted(str(item["id"]) for item in timeline),
        "messageIds": sorted(str(item["id"]) for item in messages), "taskIds": sorted(tasks)}


def _followup(row, comments, timeline, own_ids, unknown, in_attention):
    if unknown:
        return None
    baseline = row.evidence_baseline
    original = set(baseline.get("commentIds", []))
    created = row.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    for item in comments:
        happened = _instant(item.get("created_at"))
        if (item.get("author_type") == "member" and str(item["id"]) not in own_ids
                and str(item["id"]) not in original and happened and happened > created):
            return "检测到外部跟进"
    if not in_attention:
        original = set(baseline.get("timelineIds", []))
        for item in timeline:
            details = _details(item)
            happened = _instant(item.get("created_at"))
            if (item.get("actor_type") == "member" and item.get("actor_id")
                    and str(item["id"]) not in original and happened and happened > created
                    and (details.get("cause") in {"manual", "human", "user"}
                         or details.get("human_initiated") is True)):
                return "已离开关注集合，并检测到可归因人工处理"
    return None


def _assess(db, owner_id, workspace_id, issue, original):
    reference = str(issue.get("identifier") or issue["id"])
    progress = store._lock_issue_scope(db, owner_id, workspace_id, reference)
    configurations = list(db.execute(select(ModelConfig.id, ModelConfig.enabled,
        ModelConfig.is_default, ModelConfig.updated_at).where(
            ModelConfig.config_type == "llm").order_by(ModelConfig.id)))
    context = {"evidence": original, "modelConfigId": settings.autoloop_model_config_id,
        "timeoutSeconds": settings.autoloop_model_timeout_seconds,
        "configurations": [[identity, enabled, default, updated.isoformat() if updated else None]
            for identity, enabled, default, updated in configurations]}
    fingerprint = hashlib.sha256(json.dumps(context, ensure_ascii=False,
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    cached = (progress.evidence or {}).get("assessment")
    if cached and not cached.get("fallback") and cached.get("fingerprint") == fingerprint:
        return cached
    try:
        plan = planner.plan_event(db, event=SimpleNamespace(type="watchdog.observed"),
            payload=original, workspace_id=workspace_id, trusted_issue=reference, decision_only=True)
        result = {"fingerprint": fingerprint, "plan": plan.model_dump(exclude_none=True)}
    except provider.DailyLimitExceeded:
        raise
    except Exception:
        result = {"fingerprint": fingerprint, "fallback": "watchdog_assessment_unavailable"}
    progress.evidence = {**(progress.evidence or {}), "assessment": result}
    progress.version += 1
    progress.updated_at = utcnow()
    store.append_event(db, owner_id=owner_id, type="watchdog.assessed", source_kind="multica",
        source_system="multica", source_ref=reference, issue_ref=reference,
        action=result.get("plan", {}).get("action", "kernel_fallback"),
        data={"workspaceId": workspace_id, **result})
    db.flush()
    return result


def _matches_decision_source(db, row, revision_id):
    try:
        _, expected = sourcing.resolve_decision_source(db, row.owner_id, row)
    except multica_service.MulticaServiceError:
        return False
    return revision_id == expected


def reconcile_issue(db, *, owner_id, workspace_id, issue, comments, timeline,
                    messages, tasks, complete, assess=None, source_ref=None):
    """Reconcile evidence under one scope lock; optional assessment is read-only.

    ``source_ref`` freezes the immutable Multica source (revision, endpoint and
    workspace) the evidence was observed under into the card's evidence.
    """
    reference = str(issue.get("identifier") or issue["id"])
    progress = store._lock_issue_scope(db, owner_id, workspace_id, reference)
    row = db.scalar(select(AutoLoopDecision).where(AutoLoopDecision.owner_id == owner_id,
        AutoLoopDecision.workspace_id == workspace_id, AutoLoopDecision.issue_key == reference,
        AutoLoopDecision.status.in_(UNFINISHED)).with_for_update().execution_options(populate_existing=True))
    if row is not None and source_ref is not None and not _matches_decision_source(
            db, row, source_ref.get("profileRevisionId")):
        # Discovery can see a card from another revision of the same workspace.
        # Only the card's own source may update its evidence or withdraw it.
        return row
    for stream, items in (("comment", comments), ("timeline", timeline), ("message", messages)):
        progress = store.record_evidence(db, owner_id=owner_id, workspace_id=workspace_id,
            issue_key=reference, stream=stream, items=items,
            cursor={"strategy": "full_reread", "complete": complete,
                    "ids": sorted(str(item["id"]) for item in items)})
    if not complete:
        # Skipped or truncated streams never prove absence; no withdrawal or
        # terminal consumption may be claimed from an incomplete read.
        return row
    own_ids, unknown = _own_comments(db, owner_id, workspace_id, reference)
    selected = attention(issue, timeline)
    watermark = _watermark(comments, timeline, messages, tasks)
    evidence = {**watermark, "issue": issue,
        "factsSummary": f"{reference} 当前状态为 {issue.get('status', 'unknown')}，需要确认下一步。",
        "untrustedOriginal": {"issue": issue, "comments": comments, "timeline": timeline, "messages": messages}}
    if source_ref:
        evidence["sourceRef"] = source_ref
    reason = _followup(row, comments, timeline, own_ids, unknown, selected) if row else None
    if row and reason and row.source == "watchdog" and row.status == "open":
        try:
            with db.begin_nested():
                return store.withdraw_decision(db, owner_id=owner_id, decision_id=row.id,
                    expected_content_version=row.content_version, reason=reason, evidence=evidence)
        except store.StoreConflict:
            return row
    if not selected:
        return row
    # A run identity is the need anchor. Late messages and status/wording changes
    # from that run cannot invent a new need after terminal consumption.
    consumed = list(db.scalars(select(AutoLoopConsumedNeed).where(
        AutoLoopConsumedNeed.owner_id == owner_id, AutoLoopConsumedNeed.workspace_id == workspace_id,
        AutoLoopConsumedNeed.issue_key == reference)))
    old_tasks = set().union(*(set(item.evidence_watermark.get("taskIds", [])) for item in consumed))
    latest_close = max((item.consumed_at for item in consumed), default=None)
    if latest_close and latest_close.tzinfo is None:
        latest_close = latest_close.replace(tzinfo=timezone.utc)
    new_tasks = set(tasks) - old_tasks
    if consumed:
        # A newly observed ID alone is insufficient: prove a new run began after
        # the consumed watermark, rather than a late old-run transcript.
        new_tasks = {task for task in new_tasks if any(
            task in _task_ids([item]) and (_instant(item.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)) > latest_close
            and item.get("action") in {"task_started", "task.created", "task.started", "task_failed", "task.failed"}
            for item in timeline)}
        if not new_tasks:
            return row
    anchor = sorted(new_tasks or tasks)[-1] if (new_tasks or tasks) else "initial"
    need_key = "watchdog:" + hashlib.sha256(f"{issue['id']}:{anchor}".encode()).hexdigest()
    # Never replace an assistant-origin question with a polling reminder.
    if row and row.source != "watchdog":
        return row
    if row and row.status != "open":
        return row
    assessment = assess(db, owner_id, workspace_id, issue, evidence["untrustedOriginal"]) if assess else None
    plan = assessment.get("plan", {}) if assessment else {}
    if plan.get("action") == "ignore" and row is None:
        return None
    if assessment:
        evidence["assessment"] = assessment
    chosen = plan if plan.get("action") == "create_decision" else {}
    row, _ = store.create_or_update_decision(db, owner_id=owner_id, workspace_id=workspace_id,
        issue_key=reference, source="watchdog", need_key=need_key,
        title=chosen.get("title") or (row.title if row else f"{reference} 需要确认"[:500]),
        question=chosen.get("question") or (row.question if row else "请确认此 Issue 的下一步处理方式。"),
        options=chosen.get("options", row.options if row else []), evidence=evidence,
        recommended_option_id=chosen.get("recommendedOptionId", row.recommended_option_id if row else None),
        priority=chosen.get("priority") or (issue.get("priority") if issue.get("priority") in {"low", "medium", "high"} else "medium"))
    return row


def _issue_recency(db, owner_id, workspace_id, issue_key, fallback):
    """When the issue was last scanned; never-scanned issues order first."""
    updated = db.scalar(select(AutoLoopIssueProgress.updated_at).where(
        AutoLoopIssueProgress.owner_id == owner_id,
        AutoLoopIssueProgress.workspace_id == workspace_id,
        AutoLoopIssueProgress.issue_key == issue_key))
    return _as_utc(updated) or _as_utc(fallback) or datetime.min.replace(tzinfo=timezone.utc)


def _unresolved_sources(db, owner_id, current_revision):
    """Group cards by their proven revision and recheck the oldest first."""
    rows = db.scalars(select(AutoLoopDecision).where(
        AutoLoopDecision.owner_id == owner_id,
        AutoLoopDecision.status.in_(UNFINISHED),
        AutoLoopDecision.issue_key.is_not(None)))
    groups: dict[str, dict] = {}
    for row in rows:
        try:
            source, revision_id = sourcing.resolve_decision_source(db, owner_id, row)
        except multica_service.MulticaServiceError:
            continue  # ambiguous or revoked sources stay open for manual handling
        if revision_id == current_revision:
            continue  # rechecked by discovery
        group = groups.setdefault(revision_id, {
            "reference": sourcing.source_ref(source, revision_id), "issues": []})
        group["issues"].append((_issue_recency(
            db, owner_id, source.workspace_id, row.issue_key, row.created_at), row.issue_key))
    for group in groups.values():
        group["issues"].sort()
    return sorted(groups.values(), key=lambda group: group["issues"][0])[:_HISTORICAL_SOURCES_PER_SCAN]


def _recheck_issue(sessions, owner_id, base, workspace, secret, issue, reference, existing=None):
    """Read one issue's streams under one source and reconcile the evidence.

    ``existing`` is the set of unfinished issue keys in this workspace for the
    discovery scan; a historical recheck treats every issue as existing. The
    workspace of the returned issue is verified before anything is persisted,
    so evidence from a foreign endpoint is isolated instead of recorded.
    """
    issue = facts.redact(issue, secret)
    if str(issue.get("workspace_id") or issue.get("workspaceId") or "") != workspace:
        return 0
    identifier = str(issue.get("identifier") or issue["id"])
    try:
        timeline = multica_client.list_timeline(base, secret, workspace, identifier)
        if issue.get("status") == "todo" and existing is not None and identifier not in existing:
            # Ordinary todo stops here unless failure recovery is proven. Do not
            # poll all todo comment/transcript histories.
            if not attention(issue, timeline.items):
                return 0
        comments = multica_client.list_comments(base, secret, workspace, identifier)
        active = multica_client.get_active_task(base, secret, workspace, identifier)
        tasks = _task_ids(timeline.items + comments.items)
        # The client contract is strictly the official {tasks: [...]} shape;
        # no singleton response exists to fall back to.
        tasks.update(str(item["id"]) for item in active["tasks"]
                     if isinstance(item, dict) and item.get("id"))
        messages, complete = [], timeline.complete and comments.complete
        for task in sorted(tasks):
            transcript = multica_client.list_task_messages(base, secret, workspace, task)
            messages.extend(transcript.items)
            complete = complete and transcript.complete
        with sessions() as db:
            owner = db.get(User, owner_id, populate_existing=True)
            if not owner or not owner.is_active:
                return 0
            # Revalidate the exact source after I/O, including its revocation state.
            try:
                sourcing.resolve_reference(db, owner_id, reference)
            except multica_service.MulticaServiceError:
                return 0
            reconcile_issue(db, owner_id=owner_id, workspace_id=workspace, issue=issue,
                comments=facts.redact(comments.items, secret), timeline=facts.redact(timeline.items, secret),
                messages=facts.redact(messages, secret), tasks=tasks, complete=complete,
                assess=_assess, source_ref=reference)
            db.commit()
        return 1
    except provider.DailyLimitExceeded:
        raise
    except Exception:
        logger.warning("AutoLoop watchdog issue scan failed owner=%s issue=%s", owner_id, identifier)
        return 0


def _scan_discovery(sessions, owner_id, config, revision_id):
    """Bounded discovery scan of the effective source's workspace."""
    base, workspace = config.base_url, config.workspace_id
    secret = multica_service.decrypt_token(config)
    with sessions() as db:
        rows = list(db.scalars(select(AutoLoopDecision).where(
            AutoLoopDecision.owner_id == owner_id, AutoLoopDecision.workspace_id == workspace,
            AutoLoopDecision.status.in_(UNFINISHED), AutoLoopDecision.issue_key.is_not(None))))
        known = {row.issue_key for row in rows}
        existing = {row.issue_key for row in rows if _matches_decision_source(db, row, revision_id)}
        ordered = sorted(existing, key=lambda key: (
            _issue_recency(db, owner_id, workspace, key, None), key))
    read = multica_client.list_all_issues(base, secret, workspace)
    if not read.complete:
        return 0
    present = {}
    for issue in read.items:
        identity = str(issue.get("identifier") or issue.get("id") or "")
        if identity:
            present.setdefault(identity, issue)
    # Open cards first (least recently scanned first), then fresh candidates.
    candidates = [present[key] for key in ordered if key in present]
    candidates += [issue for identity, issue in present.items() if identity not in known]
    reference = sourcing.source_ref(config, revision_id)
    processed = 0
    for issue in candidates[:_ISSUES_PER_SOURCE]:
        identity = str(issue.get("identifier") or issue.get("id") or "")
        if issue.get("status") not in {"blocked", "in_review", "todo"} and identity not in existing:
            continue
        processed += _recheck_issue(sessions, owner_id, base, workspace, secret,
            issue, reference, existing)
    return processed


def _scan_source(sessions, owner_id, group):
    """Bounded recheck of one historical source's unresolved issues."""
    reference = group["reference"]
    with sessions() as db:
        try:
            source = sourcing.resolve_reference(db, owner_id, reference)
            secret = multica_service.decrypt_token(source)
        except multica_service.MulticaServiceError:
            return 0  # source unusable; the card stays open for manual handling
        base, workspace = source.base_url, source.workspace_id
    processed = 0
    for _, issue_key in group["issues"][:_ISSUES_PER_SOURCE]:
        try:
            issue = multica_client.get_issue(base, secret, workspace, issue_key)
        except provider.DailyLimitExceeded:
            raise
        except Exception:
            logger.warning("AutoLoop watchdog issue read failed owner=%s issue=%s", owner_id, issue_key)
            continue
        if not isinstance(issue, dict) or not issue.get("id"):
            continue
        if issue_key not in {str(issue.get("id")), str(issue.get("identifier"))}:
            continue
        processed += _recheck_issue(sessions, owner_id, base, workspace, secret, issue, reference)
    return processed


def scan_owner(owner_id, *, session_factory=None):
    provider.ensure_autoloop_available()
    sessions = session_factory or SessionLocal
    with sessions() as db:
        owner = db.get(User, owner_id)
        if not owner or not owner.is_active:
            return 0
        # Scans observe the effective (current-revision) workspace for
        # discovery; per-issue evidence and decisions stay keyed by the
        # workspace they were created in, so a profile switch cannot mix
        # identities across workspaces. Unresolved historical sources are
        # rechecked separately with the SAME revision they were created under.
        # Profile reads are pure: materialize deployment-default drift for
        # this owner in the scan's own short pre-I/O write and commit it
        # before any network read, so the user lock never spans provider
        # calls and the revision identity this scan freezes is resolvable
        # by the separate sessions that recheck each issue after the reads.
        if multica_service.sync_deployment_default(db, owner_id):
            db.commit()
        current = multica_service.active_config(db, owner_id)
        current_revision = str(current.profile_revision_id) if current is not None and current.profile_revision_id else None
        groups = _unresolved_sources(db, owner_id, current_revision)
    processed = 0
    if current is not None:
        try:
            processed += _scan_discovery(sessions, owner_id, current, current_revision)
        except (multica_client.MulticaClientError, multica_service.MulticaServiceError):
            logger.warning("AutoLoop watchdog effective source unavailable owner=%s", owner_id)
    for group in groups:
        processed += _scan_source(sessions, owner_id, group)
    return processed


def _scan_owners(db):
    """Owners to scan: enabled configs, default-profile users, unresolved
    historical owners even when their active config is disabled, and — when
    the deployment default is complete — every active user, so first-use
    owners with no materialized profile rows are also reached."""
    enabled = select(SuperAssistantMulticaConfig.owner_id).join(
        User, User.id == SuperAssistantMulticaConfig.owner_id).where(
            SuperAssistantMulticaConfig.enabled.is_(True), User.is_active.is_(True))
    defaults = select(SuperAssistantMulticaProfile.owner_id).join(
        User, User.id == SuperAssistantMulticaProfile.owner_id).where(
            SuperAssistantMulticaProfile.kind == multica_service.PROFILE_KIND_PLATFORM_DEFAULT,
            SuperAssistantMulticaProfile.deleted_at.is_(None), User.is_active.is_(True))
    unresolved = select(AutoLoopDecision.owner_id).join(
        User, User.id == AutoLoopDecision.owner_id).where(
            AutoLoopDecision.status.in_(UNFINISHED), User.is_active.is_(True))
    owners = (set(db.scalars(enabled)) | set(db.scalars(defaults))
              | set(db.scalars(unresolved)))
    if all(multica_service._platform_default_settings().values()):
        # A complete deployment default serves every active user even before
        # any request materialized their platform profile; enumerate them so
        # the automatic scan reaches first-use owners instead of waiting for
        # an earlier settings read. Same completeness predicate as the
        # config service; settings-only, no network or write here.
        owners |= set(db.scalars(select(User.id).where(User.is_active.is_(True))))
    return owners


def scan_watchdog(*, session_factory=None):
    provider.ensure_autoloop_available()
    sessions = session_factory or SessionLocal
    with sessions() as db:
        owners = sorted(_scan_owners(db))
    processed = 0
    for owner in owners:
        try:
            processed += scan_owner(owner, session_factory=sessions)
        except provider.DailyLimitExceeded:
            raise
        except Exception:
            logger.warning("AutoLoop watchdog owner scan failed owner=%s", owner)
    return processed
