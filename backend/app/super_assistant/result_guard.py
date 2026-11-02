"""Authoritative guard for model-claimed ontology action success.

The model can emit a plausible execution report without calling
``ontology_execute_action``. The platform must not persist or publish that
claim as success. This guard is invoked at the end of a generated reply and
consults durable authority only:

* the current assistant message's tool runs (``ontology_prepare_action`` /
  ``ontology_execute_action``);
* the referenced (or most recent) frozen confirmation and its outcome;
* the operation/receipt/affected-row evidence recorded by that confirmation.

A claim is supported only when every referenced confirmation is ``succeeded``
with an effective outcome (``applied`` or a local action with no asset
outcome), and every operation/receipt/row value in the reply matches those
records. Historical success may be restated without a new execute call because
the durable confirmation still carries the authority. Prepared,
pending_approval, pending_review, unknown and failed states are never promoted
to success. Natural-language success words alone are not authority: on a
non-action turn they are treated as ordinary prose, and structured result
status still has to pass the same confirmation checks.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.auth.models import User
from app.super_assistant import ontology_tools
from app.super_assistant.models import SuperAssistantToolRun

_ACTION_TOOLS = (
    ontology_tools.ONTOLOGY_PREPARE_TOOL,
    ontology_tools.ONTOLOGY_EXECUTE_TOOL,
)

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_ACTION_FIELD_RE = re.compile(
    r"(?i)(?:\"?affectedRows\"?\s*[:=]|\"?receiptId\"?\s*[:=]|"
    r"\"?operationId\"?\s*[:=]|\"?confirmationId\"?\s*[:=]|"
    r"\|\s*(?:affectedRows|receiptId|operationId|confirmationId)\s*\|)"
)
_CONFIRMATION_FIELD_RE = re.compile(
    rf"(?i)\"?confirmationId\"?\s*[:=]\s*\"?({_UUID})|"
    rf"\|\s*confirmationId\s*\|\s*({_UUID})\s*\|")
_OPERATION_FIELD_RE = re.compile(
    rf"(?i)\"?operationId\"?\s*[:=]\s*\"?({_UUID})|"
    rf"\|\s*operationId\s*\|\s*({_UUID})\s*\|")
_RECEIPT_FIELD_RE = re.compile(
    rf"(?i)\"?receiptId\"?\s*[:=]\s*\"?({_UUID})|"
    rf"\|\s*receiptId\s*\|\s*({_UUID})\s*\|")
_AFFECTED_ROWS_RE = re.compile(
    r"(?i)\"?affectedRows\"?\s*[:=|]\s*\"?(-?\d+)|"
    r"\|\s*affectedRows\s*\|\s*(-?\d+)\s*\|")
_STATE_SUCCEEDED_RE = re.compile(
    r"(?i)(?:\"?state\"?\s*[:=]\s*\"?succeeded|状态[^\n]{0,24}succeeded|"
    r"\|\s*succeeded\s*\|)")
_STATUS_APPLIED_RE = re.compile(
    r"(?i)(?:\"?status\"?\s*[:=]\s*\"?applied|状态[^\n]{0,24}applied|"
    r"\|\s*applied\s*\|)")
_COMPLETION_RE = re.compile(
    r"(?i)执行完成|已执行成功|执行成功(?=[：:，,。；;！!）)]|$)|恢复完成|"
    r"已恢复|已写回|写回完成|已生效|业务已生效|已改回|操作成功|动作成功|"
    r"已完成|已经完成")
_APPLIED_PHRASE_RE = re.compile(
    r"(?i)已恢复|已由[^，。；\n]{0,24}恢复为|已写回|写回完成|已生效|"
    r"业务已生效|已改回|已改回为|操作成功|动作成功")
# 仅去掉中文引号内的“术语式”提及；不能把 JSON 的双引号删掉，否则
# {"state":"succeeded","outcome":{"status":"applied"}} 会绕过检查。
_QUOTED_RE = re.compile(
    r"「[^」\n]*」|『[^』\n]*』|“[^”\n]*”")
# 流式保护/动作轮提示：只是发布时机与是否需要自然语言成功审查的启发式，
# 成功与否仍由持久确认与真实 ToolRun 决定。
_ACTION_INTENT_RE = re.compile(
    r"(?i)(?:^|[，。；;！!\s])(?:请|帮我|帮忙|麻烦|把|将)?\s*"
    r"(?:确认|批准|授权|执行|恢复|写回|改回|回滚|更新|删除|插入|新建|创建|"
    r"提交|approve|confirm|authorize|execute|apply|restore|write|update|"
    r"delete|insert|create)")
_AFFIRMATION_RE = re.compile(
    r"(?i)^\s*(?:好|好的|可以|行|继续|确认|执行|是|yes|ok|okay)"
    r"\s*[!！。.\s]*$")
_PENDING_STATES = {
    "prepared", "confirmed", "dispatch_queued", "executing", "recovering",
    "pending_approval",
}


@dataclass(frozen=True)
class GuardResult:
    content: str
    blocked: bool
    reason: str | None = None
    authority: dict[str, Any] | None = None


def _json_value(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _confirmation_ids(value: Any, depth: int = 0) -> Iterable[str]:
    if depth > 8:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in {"confirmationid", "confirmation_id"}:
                cid = str(item or "").strip()
                if cid:
                    yield cid
            yield from _confirmation_ids(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value[:100]:
            yield from _confirmation_ids(item, depth + 1)


def _run_arguments(run: SuperAssistantToolRun) -> dict[str, Any]:
    raw = _json_value(run.arguments)
    return raw if isinstance(raw, dict) else {}


def _run_confirmation_ids(run: SuperAssistantToolRun) -> list[str]:
    found: list[str] = []
    arguments = _run_arguments(run)
    for value in (
        arguments.get("confirmationId"),
        arguments.get("confirmation_id"),
    ):
        if value:
            found.append(str(value))
    found.extend(_confirmation_ids(_json_value(run.result)))
    seen: set[str] = set()
    return [cid for cid in found if not (cid in seen or seen.add(cid))]


def _field_values(pattern: re.Pattern[str], content: str) -> set[str]:
    return set(_ordered_field_values(pattern, content))


def _ordered_field_values(pattern: re.Pattern[str], content: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for match in pattern.finditer(content):
        value = next((group for group in match.groups() if group), None)
        if value and value not in seen:
            seen.add(value)
            values.append(str(value))
    return values


def _affected_rows(content: str) -> list[int]:
    rows: list[int] = []
    for match in _AFFECTED_ROWS_RE.finditer(content):
        value = next((group for group in match.groups() if group is not None), None)
        if value is not None:
            try:
                rows.append(int(value))
            except ValueError:
                pass
    return rows


def _claim_shape(content: str) -> tuple[bool, bool, bool, str]:
    """Return (field_marker, applied, completion, claim_text).

    Structured fields alone are not a success claim: an ordinary query result
    may legitimately contain columns such as ``affectedRows``. They only make
    the reply reviewable together with an explicit completion/applied claim.
    """
    field_marker = bool(_ACTION_FIELD_RE.search(content))
    claim_text = _QUOTED_RE.sub("", content)
    applied = bool(
        _STATUS_APPLIED_RE.search(claim_text)
        or _APPLIED_PHRASE_RE.search(claim_text)
    )
    completion = bool(
        _STATE_SUCCEEDED_RE.search(claim_text)
        or _COMPLETION_RE.search(claim_text)
        or re.search(
            r"(?i)\b(?:execution|action|操作|动作)[^\n。；]{0,20}"
            r"\bsucceeded\b", claim_text)
    )
    return field_marker, applied, completion, claim_text


def _load_authority(
    db: Session, user: User, conversation_id: str, cid: str,
) -> dict[str, Any] | None:
    try:
        ontology_id = ontology_tools.confirmation_owner_lookup(db, user, cid)
        if not ontology_id:
            return None
        status = ontology_tools.confirmation_status(
            db, user, ontology_id, cid,
            session_kind=ontology_tools.SESSION_KIND,
            session_id=conversation_id,
        )
    except Exception:  # noqa: BLE001 - unavailable authority must fail closed
        return None
    return status if isinstance(status, dict) else None


def _effective(auth: dict[str, Any] | None) -> bool:
    if not auth:
        return False
    outcome = auth.get("outcome") if isinstance(auth.get("outcome"), dict) else {}
    status = str(outcome.get("status") or "")
    return (
        str(auth.get("state") or "") == "succeeded"
        and not auth.get("failureCode")
        and status in {"", "applied"}
    )


def _pending_review(auth: dict[str, Any] | None) -> bool:
    if not auth:
        return False
    outcome = auth.get("outcome") if isinstance(auth.get("outcome"), dict) else {}
    return (str(auth.get("state") or "") == "succeeded"
            and str(outcome.get("status") or "") == "pending_review")


def _ids_match(content: str, authorities: list[dict[str, Any]]) -> bool:
    authority = authorities[0] if authorities else None
    if not authority:
        return False
    operation_ids = _field_values(_OPERATION_FIELD_RE, content)
    receipt_ids = _field_values(_RECEIPT_FIELD_RE, content)
    confirmation_ids = _field_values(_CONFIRMATION_FIELD_RE, content)
    allowed_operations = {
        str(auth.get("operationId"))
        for auth in authorities if str(auth.get("operationId") or "")
    }
    allowed_receipts = {
        str((auth.get("outcome") or {}).get("receiptId"))
        for auth in authorities
        if isinstance(auth.get("outcome"), dict)
        and str((auth.get("outcome") or {}).get("receiptId") or "")
    }
    allowed_confirmations = {
        str(auth.get("confirmationId") or "")
        for auth in authorities if str(auth.get("confirmationId") or "")
    }
    # 所有声明 ID 都必须落在权威集合内；actual+fabricated 混写会因
    # fabricated 不在集合而失败，不能因为 actual 存在就整体放行。
    if operation_ids and not operation_ids <= allowed_operations:
        return False
    if receipt_ids and not receipt_ids <= allowed_receipts:
        return False
    if confirmation_ids and not confirmation_ids <= allowed_confirmations:
        return False
    rows = _affected_rows(content)
    if rows:
        allowed_rows: set[int] = set()
        for auth in authorities:
            outcome = auth.get("outcome") if isinstance(auth.get("outcome"), dict) else {}
            value = outcome.get("affectedRows")
            if value is None:
                continue
            try:
                allowed_rows.add(int(value))
            except (TypeError, ValueError):
                return False
        if not allowed_rows or not set(rows) <= allowed_rows:
            return False
    return True


def _correction(auth: dict[str, Any] | None, *, reason: str) -> str:
    if auth is None:
        return (
            "本轮没有可核验的本体动作执行记录，不能确认业务已生效；"
            "此前回复中的成功、回执或受影响行数未经平台核验。"
        )
    state = str(auth.get("state") or "unknown")
    outcome = auth.get("outcome") if isinstance(auth.get("outcome"), dict) else {}
    outcome_status = str(outcome.get("status") or "")
    cid = str(auth.get("confirmationId") or "")
    if state == "succeeded" and outcome_status == "pending_review":
        return (
            f"动作确认 {cid} 已提交待审核（pending_review），尚不能视为已应用；"
            "请以审核结果为准。"
        )
    if state == "prepared":
        return (
            f"平台尚未执行该动作：确认 {cid or '（未提供）'} 当前仍为 "
            "prepared（待确认/待执行），本轮没有可核验的执行回执，不能确认"
            "业务已生效。此前回复中的成功、回执或受影响行数未经平台核验。"
        )
    if state in {"unknown", "compensation_required"}:
        return (
            f"平台记录动作执行结果为 {state}（未知/待对账），无法确认业务"
            "是否已生效，不能视为成功，也不会自动重放；请以真实源或人工"
            "对账结果为准。"
        )
    if state in {"failed", "failed_not_executed", "denied", "expired",
                 "approval_rejected", "cancelled"}:
        return (
            f"平台记录动作状态为 {state}，执行未完成或失败，不能视为成功；"
            "源端最终状态以对账结果为准。"
        )
    if state in {"confirmed", "executing", "dispatch_queued", "recovering",
                 "pending_approval"}:
        return (
            f"动作仍处于 {state}，平台尚无完成回执；不能视为业务已完成，"
            "请等待真实执行结果或对账。"
        )
    if state == "succeeded":
        return (
            "动作确认状态为 succeeded，但平台无法用当前回执证明业务已生效；"
            "请以平台操作记录和审核状态为准。"
        )
    return (
        f"本轮没有可核验的成功执行回执（确认状态 {state}）；"
        "此前回复中的成功、回执或受影响行数未经平台核验。"
    )


def _success_text(auth: dict[str, Any]) -> str:
    """Canonical success正文，仅使用持久确认里的真实回执字段。"""
    cid = str(auth.get("confirmationId") or "")
    operation_id = str(auth.get("operationId") or "")
    outcome = auth.get("outcome") if isinstance(auth.get("outcome"), dict) else {}
    status = str(outcome.get("status") or "")
    lines = [f"平台确认本体动作已成功执行（confirmationId: {cid}，"
             "state: succeeded）。"]
    if operation_id:
        lines.append(f"operationId: {operation_id}")
    if status:
        lines.append(f"outcome.status: {status}")
    receipt_id = str(outcome.get("receiptId") or "")
    if receipt_id:
        lines.append(f"receiptId: {receipt_id}")
    affected_rows = outcome.get("affectedRows")
    if affected_rows is not None:
        lines.append(f"affectedRows: {affected_rows}")
    if not status:
        lines.append("该动作没有资产回执（本地动作）。")
    return "；".join(line.rstrip("。") for line in lines) + "。"


def user_requests_action(user_text: str | None) -> bool:
    return bool(_ACTION_INTENT_RE.search(user_text or ""))


def is_action_affirmation(user_text: str | None) -> bool:
    return bool(_AFFIRMATION_RE.match(user_text or ""))


def _latest_pending_authority(
    db: Session, user: User, conversation_id: str,
    runs: list[SuperAssistantToolRun],
) -> dict[str, Any] | None:
    """Live pending confirmation of the newest action turn, if any.

    Only the newest action runs are considered. Historical succeeded/failed
    confirmations never become authority for a new reply, and an old pending
    confirmation outside the current action turn is not used to rewrite an
    ordinary query.
    """
    for run in runs[:3]:
        for cid in _run_confirmation_ids(run):
            authority = _load_authority(db, user, conversation_id, cid)
            if authority and str(authority.get("state") or "") in _PENDING_STATES:
                return authority
    return None


def conversation_has_pending_action(
    db: Session, *, owner_id: str, conversation_id: str,
) -> bool:
    """Whether the conversation has a live non-terminal confirmation.

    Used only for streaming latency and identifying an approval turn. Expired
    or terminal confirmations stop affecting later ordinary queries.
    """
    user = db.get(User, owner_id)
    if user is None:
        return False
    runs = db.query(SuperAssistantToolRun).filter(
        SuperAssistantToolRun.conversation_id == conversation_id,
        SuperAssistantToolRun.tool_name.in_(_ACTION_TOOLS),
    ).order_by(
        SuperAssistantToolRun.created_at.desc(),
        SuperAssistantToolRun.id.desc(),
    ).limit(3).all()
    return _latest_pending_authority(db, user, conversation_id, runs) is not None


def guard_ontology_action_result(
    db: Session,
    *,
    owner_id: str,
    conversation_id: str,
    assistant_message_id: str,
    content: str,
    action_turn: bool = True,
) -> GuardResult:
    """Return authoritative content for one generated assistant reply.

    Associated action replies are driven by durable authority, not by success
    vocabulary: current-turn prepare/execute runs, the live pending
    confirmation of an approval turn, or an explicit confirmationId in the
    reply. If authority is non-terminal or failed, the final text is the
    canonical status. If authority succeeded, model text is kept only when
    every operation/receipt/row it declares matches the authority; otherwise
    the canonical success text is emitted. Ordinary turns are not captured by
    arbitrary old actions.
    """
    _field_marker, applied_claim, completion_claim, _claim_text = _claim_shape(content)

    user = db.get(User, owner_id)
    if user is None:
        if applied_claim or completion_claim:
            return GuardResult(
                content=_correction(None, reason="identity_unavailable"),
                blocked=True, reason="identity_unavailable")
        return GuardResult(content=content, blocked=False)

    runs = db.query(SuperAssistantToolRun).filter(
        SuperAssistantToolRun.conversation_id == conversation_id,
        SuperAssistantToolRun.tool_name.in_(_ACTION_TOOLS),
    ).order_by(
        SuperAssistantToolRun.created_at.desc(),
        SuperAssistantToolRun.id.desc(),
    ).limit(80).all()
    current_runs = [
        run for run in runs
        if run.assistant_message_id == assistant_message_id]
    current_execute = next(
        (run for run in current_runs
         if run.tool_name == ontology_tools.ONTOLOGY_EXECUTE_TOOL),
        None,
    )
    candidates: list[tuple[str, SuperAssistantToolRun]] = []
    seen: set[str] = set()
    for run in runs:
        for cid in _run_confirmation_ids(run):
            if cid not in seen:
                seen.add(cid)
                candidates.append((cid, run))

    explicit_cids = _ordered_field_values(_CONFIRMATION_FIELD_RE, content)
    if not explicit_cids:
        explicit_cids = [
            cid for cid, _run in candidates if cid and cid in content]
    explicit_cids = list(dict.fromkeys(explicit_cids))

    authorities: list[dict[str, Any]] = []
    associated = bool(explicit_cids or current_runs)
    if explicit_cids:
        for cid in explicit_cids:
            authority = _load_authority(db, user, conversation_id, cid)
            if authority is None:
                return GuardResult(
                    content=_correction(None, reason="confirmation_not_verifiable"),
                    blocked=True, reason="confirmation_not_verifiable")
            authorities.append(authority)
    elif current_execute is not None:
        ids = _run_confirmation_ids(current_execute)
        if ids:
            authority = _load_authority(db, user, conversation_id, ids[0])
            if authority is not None:
                authorities.append(authority)
    elif action_turn:
        pending = _latest_pending_authority(db, user, conversation_id, runs)
        if pending is not None:
            associated = True
            authorities.append(pending)

    if not associated:
        # A success claim on an action request with no current action run and
        # no live/explicit confirmation has no execution authority. Ordinary
        # non-action turns are left untouched.
        if action_turn and (applied_claim or completion_claim):
            return GuardResult(
                content=_correction(None, reason="no_execute_authority"),
                blocked=True, reason="no_execute_authority")
        return GuardResult(content=content, blocked=False)

    if not authorities:
        if applied_claim or completion_claim:
            return GuardResult(
                content=_correction(None, reason="no_execute_authority"),
                blocked=True, reason="no_execute_authority")
        return GuardResult(content=content, blocked=False)

    primary: dict[str, Any] | None = next(
        (auth for auth in authorities if not _effective(auth)),
        authorities[0] if authorities else None,
    )
    all_effective = bool(authorities) and all(
        _effective(auth) for auth in authorities)
    pending_review = any(_pending_review(auth) for auth in authorities)
    current_success = bool(
        current_execute is not None and current_execute.status == "success")
    ids_ok = _ids_match(content, authorities)

    if pending_review:
        # "执行完成，待审核" still asserts completion; review is not applied.
        return GuardResult(
            content=_correction(primary, reason="pending_review"),
            blocked=True, reason="pending_review", authority=primary)

    if current_execute is not None and not current_success:
        return GuardResult(
            content=_correction(primary, reason="execution_not_succeeded"),
            blocked=True, reason="execution_not_succeeded", authority=primary)

    if all_effective:
        if ids_ok:
            return GuardResult(content=content, blocked=False, authority=primary)
        # Authority is genuinely successful; fabricated/extra ids in the model
        # text are replaced by the authoritative record instead of being kept.
        return GuardResult(
            content=_success_text(primary),
            blocked=True, reason="authority_canonicalized", authority=primary)

    reason = "no_execute_authority" if current_execute is None else "not_succeeded"
    return GuardResult(
        content=_correction(primary, reason=reason),
        blocked=True, reason=reason, authority=primary)
