"""Internal notification effect for formal Actions."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.ontologies.formal_modeling.action_execution_errors import (
    RuleExecutionError,
)
from app.ontologies.formal_modeling.action_runtime_values import (
    _preview_instance_values,
    _preview_link_values,
    _preview_reader,
    _render_template,
    _resolve_recipient,
)
from app.ontologies.formal_modeling.safe_eval import SafeEvalError


def _execute_internal_notification(
    db: Session,
    ontology_id: str,
    body,
    *,
    action,
    config: dict,
    rule_name: str,
    parameters: dict,
    target_properties: dict,
    target_instance,
    preview_context: dict | None,
    dry_run_links: list,
    dry_run_created_objects: list,
    dry_run_deleted_link_ids: set[str],
    ontology_release_id: str | None,
    sentinel_id: str | None,
    execution_log_id: str,
) -> dict:
    """Resolve and persist one reliable in-app notification effect."""
    channel = config.get("channel", "internal")
    if channel not in ("internal", "in_app", "in-app"):
        raise RuleExecutionError(
            rule_name,
            f"外部通知通道「{channel}」尚未配置可靠投递器，已拒绝伪造 delivered",
        )
    reader = _preview_reader(preview_context)
    try:
        recipient = _resolve_recipient(
            config,
            parameters,
            target_properties,
            target_instance,
            db,
            ontology_id,
            virtual_links=(dry_run_links if body.dry_run else None),
            virtual_objects=(
                {
                    item.id: item
                    for item in [
                        *(
                            _preview_instance_values(preview_context)
                            if preview_context is not None
                            and preview_context.get("isolated", False)
                            and reader is None
                            else []
                        ),
                        *dry_run_created_objects,
                    ]
                }
                if body.dry_run
                else None
            ),
            excluded_link_ids=(
                dry_run_deleted_link_ids if body.dry_run else None
            ),
            isolated_links=(
                _preview_link_values(preview_context)
                if preview_context is not None
                and preview_context.get("isolated", False)
                and reader is None
                else None
            ),
            isolated_reader=reader,
        )
    except SafeEvalError as exc:
        raise RuleExecutionError(
            rule_name,
            f"内部通知无法解析收件人: {exc}",
        ) from exc
    if not recipient:
        raise RuleExecutionError(rule_name, "内部通知无法解析收件人")
    try:
        message = _render_template(
            config.get("messageTemplate", ""),
            parameters,
            target_properties,
        )
    except SafeEvalError as exc:
        raise RuleExecutionError(
            rule_name,
            f"内部通知模板无效: {exc}",
        ) from exc
    if not message.strip():
        raise RuleExecutionError(rule_name, "内部通知消息不能为空")

    channel = "internal"
    if not body.dry_run:
        from app.models.sentinel import Notification

        db.add(
            Notification(
                ontology_id=ontology_id,
                channel=channel,
                recipient=recipient,
                subject=config.get("subject") or action.display_name,
                body=message,
                related_object_id=(
                    target_instance.id
                    if target_instance
                    else body.target_instance_id
                ),
                action_id=action.id,
                ontology_release_id=ontology_release_id,
                sentinel_id=sentinel_id,
                action_log_id=execution_log_id,
                status="delivered",
            )
        )
    return {
        "type": "notification",
        "channel": channel,
        "recipient": recipient,
        "message": message,
        "description": (
            f"站内通知预览（未写入）→ {recipient}"
            if body.dry_run
            else f"站内通知已写入 → {recipient}"
        ),
        "status": "preview" if body.dry_run else "delivered",
        "sink": "internal_inbox",
    }
