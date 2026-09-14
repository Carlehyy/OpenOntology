"""
后端动作引擎 (Action Engine)

执行动作 (ActionType)：参数校验 → 校验函数 → 事务性规则执行 → 审计日志。
支持规则类型：validation / create_object / update_property / create_link /
delete_link / notification / webhook。webhook 经受限的 HTTP 投递器真实调用；
外部通知在可靠投递器接入前仍明确失败，绝不把“仅记录”伪装成已投递。

治理语义：
  - requires_approval 的动作真实执行先落 status=pending 日志，等待人工批准/拒绝
    （决策本身写入事实流 kind=decision，批准/拒绝都可回放）。
  - 每条属性/链接变化都追加事实（source=action://<name>，caused_by=执行日志或决策事实）。
  - 单条规则失败 → 整个动作原子回滚，落 status=failed 日志（失败也是可追溯的历史）。
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.ontology_formal import (
    ActionExecutionLog,
    ActionType,
    ObjectInstance,
    ObjectType,
)
from app.ontologies.formal_modeling.action_validation import (
    _definition_id,
    _prepare_action_rules,
    _snapshot_rule_safe,
    action_supports_snapshot_execution,
    prepare_action_parameters,
    validate_action_definition,
)
from app.ontologies.formal_modeling.action_execution_context import (
    ActionDefinitionResolution,
    PreparedActionExecution,
)
from app.ontologies.formal_modeling.action_effects import (
    _execute_action_effects,
    _finalize_action_execution,
)
from app.ontologies.formal_modeling.action_runtime_support import (
    RuleExecutionError,
    _current_release_error,
    _fail_log,
    _idempotency_key,
    _idempotency_owner,
    _idempotent_replay,
    _log_to_dict,
    _match_state_id,
    _normalize_target_snapshot,
    _now,
    _preview_find,
    _preview_instance_values,
    _same_idempotent_request,
    _validate,
    logger,
)
from app.ontologies.formal_modeling.validation import (
    validate_instance_contract,
)


_GRAPH_MUTATING_EFFECTS = {
    "create_object",
    "update_property",
    "create_link",
    "delete_link",
}


def _result_changes_graph(result: dict[str, Any]) -> bool:
    if str(result.get("status") or "").lower() != "success":
        return False
    return any(
        str(effect.get("type") or "") in _GRAPH_MUTATING_EFFECTS
        for effect in (result.get("effects") or [])
        if isinstance(effect, dict)
    )


def execute_action(db: Session, ontology_id: str, body,
                   actor_id: Optional[str] = None,
                   caused_by_fact: Optional[str] = None,
                   skip_approval: bool = False,
                   *,
                   preview_only: bool = False,
                   preview_context: Optional[dict] = None,
                   expected_release_id: str | None = None) -> dict[str, Any]:
    """Authoritative runtime-write fence for every direct action caller.

    Public Sentinel evaluation/resume paths acquire this ontology lock before
    their per-Sentinel lock, preserving the global build→Sentinel order.  The
    router and Mapping-dispatch paths already own it and use the lock's
    same-thread re-entrancy.
    """
    effective_preview_only = bool(
        preview_only or getattr(body, "preview_only", False))
    if db is None and effective_preview_only:
        # Isolated trial action planning deliberately has no database or
        # production projection.  It is already constrained to the frozen
        # preview_context and cannot acquire a runtime lock.
        return _execute_action_locked(
            db,
            ontology_id,
            body,
            actor_id=actor_id,
            caused_by_fact=caused_by_fact,
            skip_approval=skip_approval,
            preview_only=True,
            preview_context=preview_context,
            expected_release_id=expected_release_id,
        )
    from app.config import settings
    from app.ontologies.runtime_fence import _ontology_build_lock
    with _ontology_build_lock(db, ontology_id):
        needs_projection = (
            settings.environment != "test"
            and not effective_preview_only
            and not bool(getattr(body, "dry_run", False))
            and hasattr(db, "info")
            and hasattr(db, "commit")
        )
        projection_owner_key = "ontology_projection_action_owner"
        if needs_projection:
            from app.ontologies.projection_state import (
                mark_projecting,
                snapshot,
            )

            current_projection = snapshot(db, ontology_id)
            if not current_projection.ready:
                # Let the normal action validation path return its established
                # fail-closed response shape; it will observe this external
                # (not self-owned) fence below.
                return _execute_action_locked(
                    db,
                    ontology_id,
                    body,
                    actor_id=actor_id,
                    caused_by_fact=caused_by_fact,
                    skip_approval=skip_approval,
                    preview_only=preview_only,
                    preview_context=preview_context,
                    expected_release_id=expected_release_id,
                )
            mark_projecting(db, ontology_id)
            db.commit()
            db.info[projection_owner_key] = str(ontology_id)
        try:
            result = _execute_action_locked(
                db,
                ontology_id,
                body,
                actor_id=actor_id,
                caused_by_fact=caused_by_fact,
                skip_approval=skip_approval,
                preview_only=preview_only,
                preview_context=preview_context,
                expected_release_id=expected_release_id,
            )
        except Exception as exc:
            if needs_projection:
                from app.ontologies.projection_state import mark_failed

                db.rollback()
                mark_failed(db, ontology_id, exc)
                db.commit()
            raise
        finally:
            if hasattr(db, "info"):
                db.info.pop(projection_owner_key, None)
        if needs_projection:
            if _result_changes_graph(result):
                from app.ontologies.projection_state import rebuild_after_commit

                rebuild_after_commit(db, ontology_id)
            else:
                from app.ontologies.projection_state import mark_ready

                mark_ready(db, ontology_id)
                db.commit()
        return result


def _resolve_action_execution_definition(
        db: Session,
        ontology_id: str,
        body,
        *,
        actor_id: Optional[str],
        preview_only: bool,
        preview_context: Optional[dict],
        expected_release_id: str | None,
) -> ActionDefinitionResolution | dict[str, Any]:
    start = time.time()
    preview_only = bool(
        preview_only or getattr(body, "preview_only", False))
    expected_release_id = (
        expected_release_id
        or getattr(body, "expected_release_id", None)
    )
    lineage_release_conflict = False
    match_state_id = _match_state_id(body)
    if (
        expected_release_id is None
        and preview_context is None
        and db is not None
        and match_state_id is not None
    ):
        # Approval execution is intentionally started in a fresh Session by the
        # router.  Its request body retains the durable Sentinel match-state id
        # but historically lost the evaluator's expected_release_id.  Recover
        # the immutable release from the already-committed proposal/action
        # lineage so an approved R1 action cannot execute a mutable draft
        # ActionType while R1 is still current.
        lineage_release_ids = {
            str(row[0])
            for row in db.query(
                ActionExecutionLog.ontology_release_id,
            ).filter(
                ActionExecutionLog.ontology_id == ontology_id,
                ActionExecutionLog.sentinel_match_state_id == match_state_id,
                ActionExecutionLog.action_id == body.action_id,
                ActionExecutionLog.ontology_release_id.is_not(None),
            ).distinct().all()
            if row[0] is not None
        }
        if len(lineage_release_ids) == 1:
            expected_release_id = next(iter(lineage_release_ids))
        elif len(lineage_release_ids) > 1:
            lineage_release_conflict = True
    if preview_context is not None and not preview_only:
        return {
            "status": "failed",
            "errorMessage": "隔离 preview_context 只能用于 preview_only",
            "actionId": body.action_id,
            "parameters": body.parameters,
            "effects": [],
            "validationErrors": ["preview_context_requires_preview_only"],
            "dryRun": bool(body.dry_run),
            "executedAt": _now().isoformat(),
            "durationMs": 0,
        }
    if preview_only and not bool(body.dry_run):
        return {
            "status": "failed",
            "errorMessage": "preview_only 必须同时启用 dry_run",
            "actionId": body.action_id,
            "parameters": body.parameters,
            "effects": [],
            "validationErrors": ["preview_only_requires_dry_run"],
            "dryRun": bool(body.dry_run),
            "executedAt": _now().isoformat(),
            "durationMs": 0,
            "previewOnly": True,
            "sideEffects": "none",
        }

    definition_context = preview_context
    action = None

    from app.models.ontology import OntologyProject
    if preview_context is not None:
        project = None
        ontology_version = preview_context.get("ontology_version")
        ontology_release_id = preview_context.get("release_id")
    else:
        project_query = db.query(OntologyProject).filter(
            OntologyProject.id == ontology_id)
        # The same project-row lock is used by release promotion.  Holding it
        # across a real action makes the expected release a transaction fence,
        # not merely a best-effort read.
        if not body.dry_run:
            # CDC owns a dedicated FOR KEY SHARE release lease. PostgreSQL's
            # FOR NO KEY UPDATE is compatible with that lease while remaining
            # mutually exclusive with promotion/rollback's FOR UPDATE.
            project_query = project_query.with_for_update(key_share=True)
        project = project_query.first()
        from app.ontologies.release_context import (
            runtime_release_identity,
            runtime_release_version,
        )
        release_identity = (
            runtime_release_identity(db, ontology_id)
            if project is not None else None
        )
        ontology_version = (
            release_identity.version if release_identity is not None
            else runtime_release_version(db, ontology_id)
            if project is not None else None
        )
        ontology_release_id = (
            release_identity.id if release_identity is not None else None
        )

    if project is None and preview_context is None:
        return _fail_log(
            db, ontology_id, None, body, start, "本体不存在",
            validation_errors=["ontology_not_found"], actor_id=actor_id,
            ontology_version=ontology_version,
            ontology_release_id=ontology_release_id,
            suppress_log=preview_only)
    if lineage_release_conflict:
        return _fail_log(
            db, ontology_id, None, body, start,
            "哨兵动作血缘包含多个发布节点，已拒绝执行",
            validation_errors=["action_release_lineage_conflict"],
            actor_id=actor_id,
            ontology_version=ontology_version,
            ontology_release_id=ontology_release_id,
            suppress_log=preview_only,
        )
    if (
        expected_release_id is not None
        and ontology_release_id != expected_release_id
    ):
        return _fail_log(
            db, ontology_id, None, body, start,
            "动作捕获的发布节点已变化，已拒绝跨发布执行",
            validation_errors=["release_context_changed"],
            actor_id=actor_id,
            ontology_version=ontology_version,
            ontology_release_id=ontology_release_id,
            suppress_log=preview_only,
        )

    if (
        preview_context is None
        and expected_release_id is not None
        and ontology_release_id == expected_release_id
    ):
        # Runtime Formal definition tables remain editable compatibility
        # projections while a draft is being prepared.  An execution pinned to
        # release A must therefore resolve Action/Function/ObjectType/LinkType
        # from A's immutable snapshot, never from those mutable tables.
        try:
            from app.models.ontology_version import OntologyVersion
            from app.ontologies.versions.snapshot_contract import (
                snapshot_models,
            )
            release = db.query(OntologyVersion).filter(
                OntologyVersion.id == expected_release_id,
                OntologyVersion.ontology_id == ontology_id,
                OntologyVersion.node_kind == "release",
                OntologyVersion.lifecycle_status == "released",
            ).first()
            if release is None:
                raise ValueError("发布快照不存在或不是有效 release")
            frozen_models = snapshot_models(release.snapshot_formal or {})
            definition_context = {
                "isolated": False,
                "release_id": expected_release_id,
                "ontology_version": release.version_number,
                "object_types": frozen_models["objectTypes"],
                "link_types": frozen_models["linkTypes"],
                "actions": frozen_models["actions"],
                "functions": frozen_models["functions"],
            }
            action = _preview_find(
                definition_context, "actions", body.action_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "加载发布快照动作定义失败: ontology=%s release=%s action=%s",
                ontology_id, expected_release_id, body.action_id)
            return _fail_log(
                db, ontology_id, None, body, start,
                "发布快照动作定义无法加载，请检查服务端日志",
                validation_errors=["release_definition_invalid"],
                actor_id=actor_id,
                ontology_version=ontology_version,
                ontology_release_id=ontology_release_id,
                suppress_log=preview_only,
            )
    elif preview_context is not None:
        action = preview_context.get("action") or _preview_find(
            preview_context, "actions", body.action_id)
        if action is not None and _definition_id(action) != str(body.action_id):
            action = None
    else:
        action = db.query(ActionType).filter(
            ActionType.id == body.action_id,
            ActionType.ontology_id == ontology_id,
        ).first()

    if not action:
        return {"status": "failed", "errorMessage": "动作不存在", "actionId": body.action_id,
                "parameters": body.parameters, "effects": [], "validationErrors": [],
                "dryRun": body.dry_run, "executedAt": _now().isoformat(), "durationMs": 0,
                **({"previewOnly": True, "sideEffects": "none"}
                   if preview_only else {})}
    return ActionDefinitionResolution(
        start=start,
        preview_only=preview_only,
        expected_release_id=expected_release_id,
        match_state_id=match_state_id,
        definition_context=definition_context,
        action=action,
        project=project,
        ontology_version=ontology_version,
        ontology_release_id=ontology_release_id,
    )


def _prepare_action_execution_request(
        db: Session,
        ontology_id: str,
        body,
        *,
        actor_id: Optional[str],
        skip_approval: bool,
        preview_context: Optional[dict],
        resolution: ActionDefinitionResolution,
) -> PreparedActionExecution | dict[str, Any]:
    start = resolution.start
    preview_only = resolution.preview_only
    expected_release_id = resolution.expected_release_id
    match_state_id = resolution.match_state_id
    definition_context = resolution.definition_context
    action = resolution.action
    project = resolution.project
    ontology_version = resolution.ontology_version
    ontology_release_id = resolution.ontology_release_id

    target_snapshot, snapshot_errors = _normalize_target_snapshot(body, action)
    # Draft runtime rows are an editable compatibility projection, not official
    # current-release data.  Published actions must stay inside the immutable
    # release; draft actions continue to operate on the draft projection.
    instance_release_id = (
        ontology_release_id
        if preview_context is not None or expected_release_id is not None
        else (
            ontology_release_id
            if (project.status or "") == "published"
            else None
        )
    )

    params, parameter_errors = prepare_action_parameters(action, body.parameters)
    rules, rule_definition_errors = _prepare_action_rules(action)
    has_effect_rule = any(
        rule.get("type") != "validation" for rule in rules)
    approval_proposal_only = (
        action.requires_approval
        and not body.dry_run
        and not skip_approval
    )
    idem_key = _idempotency_key(body)
    if getattr(body, "idempotency_key", None) is not None and not idem_key:
        parameter_errors.append("idempotency_key 必须是非空字符串")
    if idem_key and len(idem_key) > 255:
        parameter_errors.append("idempotency_key 长度不得超过 255")

    # Replay is resolved before any validation that depends on mutable current
    # projection state.  Otherwise a successful "count 0 -> 1" request would
    # fail its original precondition when the caller retries after losing the
    # response, and HITL crash recovery could be blocked by its own committed
    # effect.
    owner = (
        None if preview_only
        else _idempotency_owner(db, ontology_id, idem_key)
    )
    if owner is not None:
        if not _same_idempotent_request(
                owner, action, body, params, ontology_version,
                ontology_release_id, target_snapshot):
            return _fail_log(
                db, ontology_id, action, body, start,
                "同一 idempotency_key 对应的动作、目标或参数不一致",
                validation_errors=["idempotency_key_payload_mismatch"],
                actor_id=actor_id, parameters=params,
                ontology_version=ontology_version,
                ontology_release_id=ontology_release_id,
                target_snapshot=target_snapshot,
                suppress_log=preview_only,
            )
        replay = _idempotent_replay(
            db,
            ontology_id,
            idem_key,
            # This flag is reached only after _same_idempotent_request above
            # accepted the exact action/target/parameters/release payload.
            same_request_is_sentinel_approval=bool(
                action.requires_approval and match_state_id
            ),
        )
        if replay is not None:
            return replay

    if not body.dry_run:
        from app.config import settings
        if settings.environment != "test":
            from app.ontologies.projection_state import snapshot

            state = snapshot(db, ontology_id)
            self_owned = (
                db.info.get("ontology_projection_action_owner")
                == str(ontology_id)
            )
            if not state.ready and not self_owned:
                return _fail_log(
                    db, ontology_id, action, body, start,
                    "本体数据投影正在更新或处于失败态，真实动作已阻断；请先完成全量映射对账",
                    validation_errors=["ontology_projection_not_ready"],
                    actor_id=actor_id, ontology_version=ontology_version,
                    ontology_release_id=ontology_release_id,
                    target_snapshot=target_snapshot,
                    suppress_log=preview_only)

    target_props: Optional[dict] = None
    target_instance = None
    if body.target_instance_id:
        if preview_context is not None:
            target_instance = next((
                item for item in _preview_instance_values(preview_context)
                if str(item.id) == str(body.target_instance_id)
            ), None)
        else:
            target_query = db.query(ObjectInstance).filter(
                ObjectInstance.id == body.target_instance_id,
                ObjectInstance.ontology_id == ontology_id)
            if instance_release_id is not None:
                target_query = target_query.filter(
                    ObjectInstance.ontology_release_id == instance_release_id)
            target_instance = target_query.first()
        target_props = ({
            **dict(target_instance.properties or {}),
            **dict(target_instance.computed or {}),
        } if target_instance else None)

    target_errors: list[str] = []
    snapshot_target = (
        target_instance is None
        and target_snapshot is not None
        and _snapshot_rule_safe(rules)
    )
    if snapshot_target:
        snapshot_type = (
            _preview_find(
                definition_context, "object_types",
                target_snapshot["objectTypeId"])
            if definition_context is not None else
            db.query(ObjectType).filter(
                ObjectType.id == target_snapshot["objectTypeId"],
                ObjectType.ontology_id == ontology_id,
            ).first()
        )
        if snapshot_type is None:
            target_errors.append(
                f"target_snapshot 引用的对象类型不存在: "
                f"{target_snapshot['objectTypeId']}")
        else:
            candidate = SimpleNamespace(
                id=target_snapshot["id"],
                object_type_id=target_snapshot["objectTypeId"],
                properties=target_snapshot["properties"],
                computed=target_snapshot["computed"],
            )
            contract_errors = validate_instance_contract(
                [snapshot_type], [candidate], validate_ids={candidate.id})
            target_errors.extend(
                f"target_snapshot 契约校验失败: {item.get('message')}"
                for item in contract_errors
            )
        target_props = {
            **target_snapshot["properties"],
            **target_snapshot["computed"],
        }
    elif body.target_instance_id and target_instance is None:
        target_errors.append(f"目标实例不存在: {body.target_instance_id}")
    if action.object_type_id:
        if target_instance is None and not snapshot_target:
            target_errors.append("该动作绑定了对象类型，必须提供有效的目标实例")
        elif (target_instance is not None
              and target_instance.object_type_id != action.object_type_id):
            target_errors.append(
                f"目标实例类型不匹配：动作要求 {action.object_type_id}，"
                f"实际为 {target_instance.object_type_id}")

    # 校验
    errors = [
        *parameter_errors, *snapshot_errors, *target_errors,
        *rule_definition_errors,
    ]
    if not errors:
        errors.extend(_validate(
            action, params, target_props, db, ontology_id, rules,
            ontology_release_id=instance_release_id,
            preview_context=definition_context))
    if not has_effect_rule and not approval_proposal_only:
        errors.append(
            "动作没有启用的可执行副作用规则，已拒绝记录伪成功")
    if errors:
        return _fail_log(db, ontology_id, action, body, start,
                         "校验未通过", validation_errors=errors, actor_id=actor_id,
                         parameters=params, ontology_version=ontology_version,
                         ontology_release_id=ontology_release_id,
                         target_snapshot=target_snapshot,
                         suppress_log=preview_only)

    # —— HITL 审批闸门：真实执行先挂起，等人拍板（决策也是 Fact）——
    if action.requires_approval and not body.dry_run and not skip_approval:
        release_error = _current_release_error(db, resolution, preview_context)
        if release_error:
            return _fail_log(
                db, ontology_id, action, body, start, release_error,
                validation_errors=["release_context_changed"],
                actor_id=actor_id, parameters=params,
                ontology_version=ontology_version,
                ontology_release_id=ontology_release_id,
                target_snapshot=target_snapshot,
                suppress_log=preview_only,
            )
        log = ActionExecutionLog(
            ontology_id=ontology_id, action_id=action.id, action_name=action.display_name,
            object_type_id=action.object_type_id, object_instance_id=body.target_instance_id,
            parameters=params, status="pending", validation_errors=[], effects=[],
            error_message=None, duration_ms=int((time.time() - start) * 1000),
            dry_run=False, actor_id=actor_id,
            idempotency_key=idem_key,
            sentinel_match_state_id=_match_state_id(body),
            ontology_version=ontology_version,
            ontology_release_id=ontology_release_id,
            target_snapshot=target_snapshot,
        )
        db.add(log)
        try:
            # Keep the governance notification in the same transaction as the
            # pending action. Projection failure must leave a retryable inbox
            # outbox row rather than losing the approval request.
            db.flush()
            from app.inbox.service import enqueue_ontology_approval_request
            enqueue_ontology_approval_request(
                db,
                log_id=log.id,
                ontology_id=ontology_id,
                action_name=log.action_name or action.name,
                ontology_version=log.ontology_version,
                ontology_release_id=log.ontology_release_id,
                occurred_at=log.executed_at,
            )
            db.commit(); db.refresh(log)
        except IntegrityError:
            db.rollback()
            replay = _idempotent_replay(db, ontology_id, idem_key)
            if replay is not None:
                return replay
            raise
        out = _log_to_dict(log)
        out["pendingApproval"] = True
        return out
    return PreparedActionExecution(
        target_snapshot=target_snapshot,
        instance_release_id=instance_release_id,
        params=params,
        rules=rules,
        idempotency_key=idem_key,
        target_props=target_props,
        target_instance=target_instance,
    )


def _execute_action_locked(
                   db: Session, ontology_id: str, body,
                   actor_id: Optional[str] = None,
                   caused_by_fact: Optional[str] = None,
                   skip_approval: bool = False,
                   *,
                   preview_only: bool = False,
                   preview_context: Optional[dict] = None,
                   expected_release_id: str | None = None) -> dict[str, Any]:
    """body 是 RunActionRequest。返回 ActionExecutionLog dict (camelCase)。

        actor_id        发起人（哨兵触发为 None）
        caused_by_fact  因果指针覆盖（审批执行时传决策事实 id，对齐 caused_by=f010 语义）
        skip_approval   审批通过后的真正执行走此口，绕过 pending 闸门
        preview_only    只返回动作计划；不落 ActionLog/Fact/Notification 且不发网络
        preview_context 隔离试跑的冻结定义、对象和关系；绝不查询正式运行投影
        expected_release_id 调用方捕获的发布节点；动作开始和提交前均做 CAS 校验
        """
    resolution = _resolve_action_execution_definition(
        db,
        ontology_id,
        body,
        actor_id=actor_id,
        preview_only=preview_only,
        preview_context=preview_context,
        expected_release_id=expected_release_id,
    )
    if isinstance(resolution, dict):
        return resolution

    prepared = _prepare_action_execution_request(
        db,
        ontology_id,
        body,
        actor_id=actor_id,
        skip_approval=skip_approval,
        preview_context=preview_context,
        resolution=resolution,
    )
    if isinstance(prepared, dict):
        return prepared

    executed = _execute_action_effects(
        db,
        ontology_id,
        body,
        actor_id=actor_id,
        caused_by_fact=caused_by_fact,
        preview_context=preview_context,
        resolution=resolution,
        prepared=prepared,
    )
    if isinstance(executed, dict):
        return executed

    return _finalize_action_execution(
        db,
        ontology_id,
        body,
        actor_id=actor_id,
        preview_context=preview_context,
        resolution=resolution,
        prepared=prepared,
        executed=executed,
    )
