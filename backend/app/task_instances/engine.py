"""任务实例 — 事件溯源执行引擎（设计方案 §5）。

单反应器规则 R：节点完成 → 事务内评估出边 → 就绪判定 → 幂等创建
下游尝试。移植 qwenpaw 已验证的正确性范式（qwenpaw-cluster
backend/workflow/engine.py 的 appendEventAndDispatch），落到 PostgreSQL
行锁（替代 SQLite BEGIN IMMEDIATE）。

就绪判定统一化：condition / join / terminal 是纯图构件——
  - condition 在其 on 上游完成时即时求值路由（不落 node_run）；
  - join 在 mode 条件满足时落一行 completed 的 node_run（any：首个
    入边送达；all：全部入边源均有「最新有效产出」），下游用同一套
    「源节点最新尝试为 completed」规则判就绪；
  - terminal 收到送达即收束实例。

inputs 上下文（§5.1）：taskContext / output / correction / rejection
四类条目；inputs_hash 对排序后规范 JSON 求 sha256（顺序不敏感），
(instance, node, inputs_hash) 唯一 —— 上游重做产出不变则下游幂等跳过。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.task_instances import events as ev
from app.task_instances import contracts as contract_engine
from app.task_instances.models import (
    APPROVAL_APPROVED,
    APPROVAL_CANCELLED,
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
    INSTANCE_ACTIVE,
    INSTANCE_CANCELLED,
    INSTANCE_COMPLETED,
    INSTANCE_FAILED,
    NODE_CANCELLED,
    NODE_COMPLETED,
    NODE_DISPATCHED,
    NODE_FAILED,
    NODE_PENDING,
    NODE_RUNNING,
    NODE_SKIPPED,
    NODE_VOIDED,
    NODE_WAITING_APPROVAL,
    NODE_WAITING_HUMAN,
    TaskApproval,
    TaskArtifact,
    TaskInstance,
    TaskNodeRun,
)
from app.task_instances.spec import (
    DEFAULT_OUTPUT_PORT,
    NODE_AGENT,
    NODE_APPROVAL,
    NODE_CONDITION,
    NODE_HUMAN,
    NODE_JOIN,
    NODE_TERMINAL,
    PoliciesSpec,
    WorkflowSpec,
)

_ENGINE_ACTOR = "engine"


class EngineGuardError(Exception):
    """状态前置不满足（实例非 active / 节点非等待态等）。"""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class ReworkBudgetExhausted(Exception):
    """打回次数达到 rework 预算上限。"""


class ContractViolation(Exception):
    """产出违反端口契约（纠正预算耗尽后由引擎转节点/实例失败）。"""


@dataclass
class EngineResult:
    """引擎操作结果：dispatches 为提交后需要派发的 agent 节点尝试。"""

    dispatches: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# spec 快照访问（spec_snapshot 为 canonical dict，见 spec.canonical_json）
# ---------------------------------------------------------------------------


def _spec_of(instance: TaskInstance) -> dict:
    if not instance.spec_snapshot:
        raise EngineGuardError("SPEC_MISSING", "实例缺少 spec 快照")
    return instance.spec_snapshot


def _nodes(spec: dict) -> dict:
    return spec.get("nodes") or {}


def _edges(spec: dict) -> list[dict]:
    return spec.get("edges") or []


def _policies(spec: dict) -> PoliciesSpec:
    return PoliciesSpec.model_validate(spec.get("policies") or {})


def _incoming_edges(spec: dict, node_id: str) -> list[dict]:
    return [e for e in _edges(spec)
            if e.get("to") == node_id and not e.get("rework")]


def _outgoing_edges(spec: dict, node_id: str, port: str | None = None) -> list[dict]:
    result = []
    for edge in _edges(spec):
        src = edge.get("from") or ""
        src_node, src_port = (src.split(".", 1) + [None])[:2] if "." in src else (src, None)
        if src_node != node_id:
            continue
        if port is not None:
            # 端口过滤是硬匹配：未写端口的边只属于默认 done 口径，
            # 不得混入 approved/rejected 等显式端口的投递
            if src_port != port:
                continue
        result.append(edge)
    return result


def compute_inputs_hash(inputs: list[dict]) -> str:
    ordered = sorted(inputs, key=lambda item: (
        str(item.get("kind", "")), str(item.get("ref", "")),
        json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)))
    payload = json.dumps(ordered, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _latest_attempt(db: Session, instance_id: str, node_id: str) -> TaskNodeRun | None:
    return (
        db.query(TaskNodeRun)
        .filter(TaskNodeRun.instance_id == instance_id,
                TaskNodeRun.node_id == node_id)
        .order_by(TaskNodeRun.attempt_no.desc())
        .first()
    )


def _latest_valid_output(db: Session, instance_id: str,
                         node_id: str) -> dict | None:
    latest = _latest_attempt(db, instance_id, node_id)
    if latest is not None and latest.status == NODE_COMPLETED:
        return latest.output or {}
    return None


def _actor_of(user) -> str:
    if user is None:
        return _ENGINE_ACTOR
    if isinstance(user, str):
        # 系统角色字符串（system:expiry 等）原样保留
        return user if user.startswith("system:") else f"user:{user}"
    user_id = getattr(user, "id", None)
    if isinstance(user_id, str) and user_id.startswith("system:"):
        return user_id
    return f"user:{user_id or 'unknown'}"


def reconcile_stale_attempt(db: Session, run: TaskNodeRun) -> EngineResult:
    """对账收口：租约过期的 running/长期未认领的 dispatched 尝试。

    按纠正预算重派（携 lease 上下文）；预算耗尽则节点失败 → 实例失败。
    """
    instance = _lock_instance(db, run.instance_id)
    spec = _spec_of(instance)
    policies = _policies(spec)
    if instance.status != INSTANCE_ACTIVE:
        return EngineResult()
    if run.status not in (NODE_DISPATCHED, NODE_RUNNING):
        return EngineResult()
    result = EngineResult()
    run.status = NODE_VOIDED
    run.error = "lease expired (reconciler)"
    run.finished_at = _now()
    ev.append_event(
        db, instance.id, ev.NODE_VOIDED,
        {"node_id": run.node_id, "attempt_no": run.attempt_no,
         "cause": "lease_expired"},
        node_run_id=run.id, actor="system:reconciler",
        event_budget=policies.event_budget)
    if run.correction_count < policies.corrections_per_node:
        inputs_list = list(run.inputs or [])
        inputs_list.append({
            "kind": "correction",
            "violations": ["执行租约过期，由对账器收回"],
            "previous_output": None})
        _create_attempt(db, instance, spec, run.node_id,
                        inputs_list=inputs_list, actor="system:reconciler",
                        result=result, routed_via=f"reconcile:{run.node_id}")
        new_run = _latest_attempt(db, instance.id, run.node_id)
        if new_run is not None and new_run.id != run.id:
            new_run.correction_count = run.correction_count + 1
        return result
    _fail_node(db, instance, policies, run,
               error="执行租约过期且纠正预算耗尽")
    return result


# ---------------------------------------------------------------------------
# 激活
# ---------------------------------------------------------------------------


def activate(
    db: Session,
    *,
    revision,
    name: str,
    goal: str,
    inputs: dict | None,
    created_by,
    idempotency_key: str | None = None,
) -> tuple[TaskInstance, EngineResult]:
    if idempotency_key:
        existing = db.query(TaskInstance).filter(
            TaskInstance.idempotency_key == idempotency_key).first()
        if existing is not None:
            return existing, EngineResult()
    spec = _spec_of_revision(revision)
    instance = TaskInstance(
        template_revision_id=revision.id,
        name=name,
        goal=goal,
        inputs=inputs or {},
        spec_snapshot=revision.spec_compiled or spec,
        idempotency_key=idempotency_key,
        created_by=getattr(created_by, "id", None),
    )
    db.add(instance)
    db.flush()
    ev.append_event(
        db, instance.id, ev.INSTANCE_CREATED,
        {"template_revision_id": revision.id, "name": name, "goal": goal},
        actor=_actor_of(created_by),
        event_budget=_policies(spec).event_budget,
    )
    result = EngineResult()
    # condition 的分支/默认目标（无入边但不得作为起点——它们等路由送达）
    branch_targets: set[str] = set()
    for node in _nodes(spec).values():
        if node.get("kind") != NODE_CONDITION:
            continue
        for branch in node.get("branches") or []:
            branch_targets.update(branch.get("to") or [])
        branch_targets.update(node.get("default") or [])
    # 起点节点：无入边（非打回）且非 condition 分支目标的可执行节点
    for node_id, node in _nodes(spec).items():
        kind = node.get("kind")
        if kind not in (NODE_AGENT, NODE_HUMAN, NODE_APPROVAL):
            continue
        if _incoming_edges(spec, node_id) or node_id in branch_targets:
            continue
        _create_attempt(db, instance, spec, node_id, inputs_list=[
            {"kind": "taskContext", "goal": goal,
             "inputs": inputs or {}},
        ], actor=_actor_of(created_by), result=result)
    return instance, result


def _spec_of_revision(revision) -> dict:
    if revision.spec_compiled:
        return revision.spec_compiled
    from app.task_instances.spec import canonical_json, parse_workflow_yaml
    return canonical_json(parse_workflow_yaml(revision.spec_yaml))


# ---------------------------------------------------------------------------
# 节点尝试创建（就绪判定 + 幂等）
# ---------------------------------------------------------------------------


def _create_attempt(
    db: Session,
    instance: TaskInstance,
    spec: dict,
    node_id: str,
    *,
    inputs_list: list[dict],
    actor: str,
    result: EngineResult,
    routed_via: str | None = None,
    rework_count: int = 0,
) -> TaskNodeRun | None:
    policies = _policies(spec)
    node = _nodes(spec)[node_id]
    kind = node.get("kind")
    inputs_hash = compute_inputs_hash(inputs_list)
    existing = (
        db.query(TaskNodeRun)
        .filter(TaskNodeRun.instance_id == instance.id,
                TaskNodeRun.node_id == node_id,
                TaskNodeRun.inputs_hash == inputs_hash)
        .first()
    )
    # 并行预算：agent 执行位满时先落 pending，由 promote/对账补派
    if existing is not None and existing.status != NODE_VOIDED:
        return existing  # 幂等：同输入且旧尝试有效时不重复派发
    if existing is not None:
        # 同输入且旧尝试已作废（打回/纠正环回到同一状态）：原地重开，
        # 保留 attempt_no 与计数——满足 (instance, node, inputs_hash)
        # 唯一约束，重开痕迹走 node.reopened 事件
        run = existing
        run.status = _initial_status(db, instance.id, spec, kind)
        run.output = None
        run.error = None
        run.finished_at = None
        reopened = True
    else:
        prior = _latest_attempt(db, instance.id, node_id)
        attempt_no = (prior.attempt_no + 1) if prior else 1
        status = _initial_status(db, instance.id, spec, kind)
        run = TaskNodeRun(
            instance_id=instance.id,
            node_id=node_id,
            attempt_no=attempt_no,
            status=status,
            inputs=inputs_list,
            inputs_hash=inputs_hash,
            rework_count=rework_count,
        )
        db.add(run)
        reopened = False
    if kind == NODE_AGENT and node.get("model"):
        run.model_config_id = node["model"]
    db.flush()
    ev.append_event(
        db, instance.id,
        ev.NODE_REOPENED if reopened else ev.NODE_CREATED,
        {"node_id": run.node_id, "attempt_no": run.attempt_no,
         "node_kind": kind,
         **({"routed_via": routed_via} if routed_via else {})},
        node_run_id=run.id, actor=actor, event_budget=policies.event_budget,
    )
    if run.status == NODE_DISPATCHED:
        result.dispatches.append(run.id)
        ev.append_event(
            db, instance.id, ev.NODE_DISPATCHED,
            {"node_id": node_id, "attempt_no": run.attempt_no},
            node_run_id=run.id, actor=_ENGINE_ACTOR,
            event_budget=policies.event_budget,
        )
    elif run.status == NODE_WAITING_HUMAN:
        ev.append_event(
            db, instance.id, ev.HUMAN_ASSIGNED,
            {"node_id": node_id, "attempt_no": run.attempt_no,
             "role": node.get("role", "")},
            node_run_id=run.id, actor=actor,
            event_budget=policies.event_budget,
        )
    elif run.status == NODE_WAITING_APPROVAL:
        _create_approval(db, instance, spec, run, node, inputs_list,
                         policies=policies)
    return run


def _initial_status(db: Session, instance_id: str, spec: dict, kind: str) -> str:
    if kind == NODE_AGENT:
        running = (
            db.query(TaskNodeRun)
            .filter(TaskNodeRun.instance_id == instance_id,
                    TaskNodeRun.status.in_((NODE_DISPATCHED, NODE_RUNNING)))
            .count()
        )
        if running >= _policies(spec).max_parallelism:
            return NODE_PENDING
        return NODE_DISPATCHED
    if kind == NODE_HUMAN:
        return NODE_WAITING_HUMAN
    if kind == NODE_APPROVAL:
        return NODE_WAITING_APPROVAL
    return NODE_PENDING


def _create_approval(db: Session, instance: TaskInstance, spec: dict,
                     run: TaskNodeRun, node: dict, inputs_list: list[dict],
                     *, policies: PoliciesSpec) -> None:
    proposal = _merge_inputs(inputs_list)
    approval = TaskApproval(
        instance_id=instance.id,
        node_run_id=run.id,
        proposal=proposal,
        proposal_hash=hashlib.sha256(
            json.dumps(proposal, ensure_ascii=False, sort_keys=True,
                       default=str).encode("utf-8")).hexdigest(),
        status=APPROVAL_PENDING,
    )
    expires_hours = node.get("expires_hours") or 72
    from datetime import datetime, timedelta, timezone
    approval.expires_at = datetime.now(timezone.utc) + timedelta(
        hours=int(expires_hours))
    db.add(approval)
    db.flush()
    ev.append_event(
        db, instance.id, ev.APPROVAL_REQUESTED,
        {"node_id": run.node_id, "approval_id": approval.id,
         "approvers": node.get("approvers", []),
         "expires_at": approval.expires_at.isoformat()
         if approval.expires_at else None},
        node_run_id=run.id, actor=_ENGINE_ACTOR,
        event_budget=policies.event_budget,
    )


def _merge_inputs(inputs_list: list[dict]) -> dict:
    merged: dict = {}
    for item in inputs_list:
        if item.get("kind") == "taskContext":
            merged.setdefault("taskContext", {
                "goal": item.get("goal"), "inputs": item.get("inputs")})
        elif item.get("kind") == "output":
            merged.setdefault("inputs", {})[item.get("ref", "?")] = \
                item.get("output")
    return merged


# ---------------------------------------------------------------------------
# 规则 R：节点完成后的推进
# ---------------------------------------------------------------------------


def _guard_active(instance: TaskInstance) -> None:
    if instance.status != INSTANCE_ACTIVE:
        raise EngineGuardError(
            "INSTANCE_NOT_ACTIVE", f"实例状态为 {instance.status}，"
            f"仅 active 实例允许推进")


def _advance(db: Session, instance: TaskInstance, spec: dict,
             source_node_id: str, output: dict, result: EngineResult,
             *, port: str | None = None) -> None:
    """规则 R：source 完成后评估其出边与 condition 消费者。

    port 指定时只走该端口（approval 的 approved/rejected 分端口路由）。
    """
    _guard_active(instance)
    delivered: set[str] = set()
    # 1) 普通数据边（含 join 的出边由 join 落行后走同一逻辑）
    for edge in _outgoing_edges(spec, source_node_id, port=port):
        target = edge.get("to")
        if edge.get("rework"):
            continue  # 打回边只在审批拒绝/人工驳回路径处理
        if target in delivered:
            continue
        delivered.add(target)
        if not _deliver(db, instance, spec, target, result,
                        routed_via=f"edge:{source_node_id}"):
            return
    # 2) 以本节点为 on 上游的 condition 节点
    for node_id, node in _nodes(spec).items():
        if node.get("kind") != NODE_CONDITION:
            continue
        on_ref = node.get("on") or ""
        on_node = on_ref.split(".", 1)[0]
        if on_node != source_node_id:
            continue
        # 谓词字段相对 on 上游的产出解析（join 产出天然嵌套在 inputs 下）
        targets = _evaluate_condition(node, output)
        for target in targets:
            if target in delivered:
                continue
            delivered.add(target)
            if not _deliver(db, instance, spec, target, result,
                            routed_via=f"condition:{node_id}"):
                return


def _evaluate_condition(node: dict, upstream_output: dict) -> list[str]:
    """确定性谓词路由：按 branches 顺序首个命中；否则 default。"""
    for branch in node.get("branches") or []:
        when = branch.get("when") or {}
        value = _resolve_field(upstream_output or {}, when.get("field", ""))
        if _predicate_matches(when, value):
            return list(branch.get("to") or [])
    return list(node.get("default") or [])


def _resolve_field(outputs: dict, field: str):
    value: object = outputs
    for part in field.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return None
    return value


def _predicate_matches(when: dict, value) -> bool:
    if "equals" in when:
        return value == when["equals"]
    if "in" in when:
        candidates = when["in"] or []
        return any(value == c for c in candidates)
    if "matches" in when:
        import re
        subject = value if isinstance(value, str) else json.dumps(
            value, ensure_ascii=False, default=str)
        return re.search(when["matches"], subject) is not None
    return False


def _deliver(db: Session, instance: TaskInstance, spec: dict, target: str,
             result: EngineResult, *, routed_via: str) -> bool:
    """向目标节点/构件送达上游产出；返回实例是否仍在推进（非终态）。"""
    _guard_active(instance)
    node = _nodes(spec).get(target)
    if node is None:
        return True
    kind = node.get("kind")
    if kind == NODE_TERMINAL:
        outcome = node.get("outcome", "success")
        _complete_instance(db, instance, spec,
                           INSTANCE_COMPLETED if outcome == "success"
                           else INSTANCE_FAILED,
                           reason=f"terminal:{target}")
        return False
    if kind == NODE_JOIN:
        return _deliver_join(db, instance, spec, target, result,
                             routed_via=routed_via)
    # agent / human / approval：全部入边源就绪才建尝试
    incoming = _incoming_edges(spec, target)
    inputs_list: list[dict] = [{
        "kind": "taskContext", "goal": instance.goal,
        "inputs": instance.inputs or {}}]
    for edge in incoming:
        src = (edge.get("from") or "").split(".", 1)[0]
        upstream_output = _latest_valid_output(db, instance.id, src)
        if upstream_output is None:
            return True  # 尚未就绪，等其它入边
        inputs_list.append({
            "kind": "output", "ref": src,
            "port": DEFAULT_OUTPUT_PORT, "output": upstream_output})
    _create_attempt(db, instance, spec, target, inputs_list=inputs_list,
                    actor=_ENGINE_ACTOR, result=result,
                    routed_via=routed_via)
    return True


def _deliver_join(db: Session, instance: TaskInstance, spec: dict,
                  join_id: str, result: EngineResult, *,
                  routed_via: str) -> bool:
    node = _nodes(spec)[join_id]
    mode = node.get("mode", "all")
    incoming = _incoming_edges(spec, join_id)
    satisfied: dict[str, dict] = {}
    for edge in incoming:
        src = (edge.get("from") or "").split(".", 1)[0]
        upstream_output = _latest_valid_output(db, instance.id, src)
        if upstream_output is not None:
            satisfied[src] = upstream_output
    if mode == "any":
        if not satisfied:
            return True
    else:  # all
        for edge in incoming:
            src = (edge.get("from") or "").split(".", 1)[0]
            if src not in satisfied:
                return True  # 等其余入边
    # join 满足：落一行 completed 尝试（统一就绪模型），随即推进其出边
    policies = _policies(spec)
    inputs_list = [{
        "kind": "taskContext", "goal": instance.goal,
        "inputs": instance.inputs or {}}] + [
        {"kind": "output", "ref": src, "port": DEFAULT_OUTPUT_PORT,
         "output": output} for src, output in sorted(satisfied.items())]
    inputs_hash = compute_inputs_hash(inputs_list)
    existing = (
        db.query(TaskNodeRun)
        .filter(TaskNodeRun.instance_id == instance.id,
                TaskNodeRun.node_id == join_id,
                TaskNodeRun.inputs_hash == inputs_hash)
        .first())
    if existing is not None:
        return True
    prior = _latest_attempt(db, instance.id, join_id)
    run = TaskNodeRun(
        instance_id=instance.id, node_id=join_id,
        attempt_no=(prior.attempt_no + 1) if prior else 1,
        status=NODE_COMPLETED, inputs=inputs_list,
        inputs_hash=inputs_hash, output={"inputs": satisfied},
        started_at=None, finished_at=None,
    )
    db.add(run)
    db.flush()
    run.finished_at = run.created_at
    ev.append_event(
        db, instance.id, ev.NODE_CREATED,
        {"node_id": join_id, "attempt_no": run.attempt_no,
         "node_kind": NODE_JOIN, "routed_via": routed_via},
        node_run_id=run.id, actor=_ENGINE_ACTOR,
        event_budget=policies.event_budget)
    ev.append_event(
        db, instance.id, ev.NODE_COMPLETED,
        {"node_id": join_id, "attempt_no": run.attempt_no,
         "mode": mode},
        node_run_id=run.id, actor=f"{_ENGINE_ACTOR}:join",
        event_budget=policies.event_budget)
    _advance(db, instance, spec, join_id, {"inputs": satisfied}, result)
    return instance.status == INSTANCE_ACTIVE


# ---------------------------------------------------------------------------
# 交活（agent 执行器与人工提交共用）
# ---------------------------------------------------------------------------


def complete_node(
    db: Session,
    node_run_id: str,
    output: dict,
    *,
    artifacts: list[dict] | None = None,
    actor: str,
) -> EngineResult:
    run = _load_run(db, node_run_id)
    instance = _lock_instance(db, run.instance_id)
    db.refresh(run)  # 等锁期间状态可能已被并发事务改写
    spec = _spec_of(instance)
    policies = _policies(spec)
    _guard_active(instance)
    if run.status not in (NODE_DISPATCHED, NODE_RUNNING, NODE_WAITING_HUMAN):
        raise EngineGuardError(
            "NODE_NOT_COMPLETABLE",
            f"节点 {run.node_id} 尝试#{run.attempt_no} 状态为 {run.status}，"
            f"仅 dispatched/running/waiting_human 可交活")
    result = EngineResult()
    node = _nodes(spec).get(run.node_id, {})
    port = (node.get("outputs") or {}).get(DEFAULT_OUTPUT_PORT) or {}
    contract_name = port.get("contract")
    if contract_name:
        contract = (spec.get("contracts") or {}).get(contract_name)
        violations = contract_engine.validate(contract, output) if contract else []
        if violations:
            return _handle_contract_violation(
                db, instance, spec, run, output, violations, result)
    required_artifacts = port.get("required_artifacts") or []
    provided = {a.get("name") for a in (artifacts or [])}
    missing = [name for name in required_artifacts if name not in provided]
    if missing:
        return _handle_contract_violation(
            db, instance, spec, run, output,
            [f"缺少必交产物: {missing}"], result)
    _persist_completion(db, instance, policies, run, output, artifacts, actor)
    _advance(db, instance, spec, run.node_id, output, result)
    return result


def _handle_contract_violation(
    db: Session, instance: TaskInstance, spec: dict, run: TaskNodeRun,
    output: dict, violations: list[str], result: EngineResult,
) -> EngineResult:
    policies = _policies(spec)
    ev.append_event(
        db, instance.id, ev.CONTRACT_VIOLATED,
        {"node_id": run.node_id, "attempt_no": run.attempt_no,
         "port": DEFAULT_OUTPUT_PORT, "violations": violations},
        node_run_id=run.id, actor=_ENGINE_ACTOR,
        event_budget=policies.event_budget)
    run.status = NODE_VOIDED
    run.output = None
    run.error = "contract violation: " + "; ".join(violations)[:2000]
    run.finished_at = _now()
    if run.correction_count < policies.corrections_per_node:
        inputs_list = list(run.inputs or [])
        inputs_list.append({
            "kind": "correction", "violations": violations,
            "previous_output": output})
        _create_attempt(
            db, instance, spec, run.node_id, inputs_list=inputs_list,
            actor=_ENGINE_ACTOR, result=result,
            routed_via=f"correction:{run.node_id}")
        new_run = _latest_attempt(db, instance.id, run.node_id)
        if new_run is not None and new_run.id != run.id:
            new_run.correction_count = run.correction_count + 1
        ev.append_event(
            db, instance.id, ev.NODE_CORRECTION_REQUESTED,
            {"node_id": run.node_id,
             "next_attempt_no": (new_run.attempt_no if new_run
                                 else run.attempt_no + 1),
             "remaining": policies.corrections_per_node
             - ((new_run.correction_count if new_run
                 else run.correction_count + 1))},
            node_run_id=new_run.id if new_run else run.id,
            actor=_ENGINE_ACTOR, event_budget=policies.event_budget)
        return result
    _fail_node(db, instance, policies, run,
               error=f"契约纠正预算耗尽: {'; '.join(violations)[:1500]}")
    return result


def _persist_completion(db: Session, instance: TaskInstance,
                        policies: PoliciesSpec, run: TaskNodeRun,
                        output: dict, artifacts: list[dict] | None,
                        actor: str) -> None:
    run.status = NODE_COMPLETED
    run.output = output
    run.finished_at = _now()
    ev.append_event(
        db, instance.id, ev.NODE_COMPLETED,
        {"node_id": run.node_id, "attempt_no": run.attempt_no},
        node_run_id=run.id, actor=actor, event_budget=policies.event_budget)
    for artifact in artifacts or []:
        row = TaskArtifact(
            instance_id=instance.id, node_run_id=run.id,
            name=artifact["name"],
            mime_type=artifact.get("mime_type", "application/octet-stream"),
            size_bytes=artifact.get("size_bytes", 0),
            sha256=artifact["sha256"], storage_uri=artifact["storage_uri"])
        db.add(row)
        db.flush()
        ev.append_event(
            db, instance.id, ev.ARTIFACT_DECLARED,
            {"node_id": run.node_id, "artifact_id": row.id,
             "name": row.name},
            node_run_id=run.id, actor=actor,
            event_budget=policies.event_budget)
        ev.append_event(
            db, instance.id, ev.ARTIFACT_COMPLETED,
            {"node_id": run.node_id, "artifact_id": row.id,
             "sha256": row.sha256},
            node_run_id=run.id, actor=actor,
            event_budget=policies.event_budget)


def _fail_node(db: Session, instance: TaskInstance, policies: PoliciesSpec,
               run: TaskNodeRun, *, error: str) -> None:
    run.status = NODE_FAILED
    run.error = error[:2000]
    run.finished_at = _now()
    ev.append_event(
        db, instance.id, ev.NODE_FAILED,
        {"node_id": run.node_id, "attempt_no": run.attempt_no,
         "error": error[:2000]},
        node_run_id=run.id, actor=_ENGINE_ACTOR,
        event_budget=policies.event_budget, allow_over_budget=True)
    _complete_instance(db, instance, _spec_of(instance), INSTANCE_FAILED,
                       reason=f"node:{run.node_id}:{error[:300]}")


# ---------------------------------------------------------------------------
# 审批决定（approved / rejected + 打回边）
# ---------------------------------------------------------------------------


def decide_approval(
    db: Session,
    approval_id: str,
    decision: str,
    *,
    reason: str | None,
    decided_by,
) -> EngineResult:
    approval = db.query(TaskApproval).filter(
        TaskApproval.id == approval_id).first()
    if approval is None:
        raise EngineGuardError("APPROVAL_NOT_FOUND", "审批不存在")
    if approval.status != APPROVAL_PENDING:
        raise EngineGuardError(
            "APPROVAL_DECIDED", f"审批已处于 {approval.status} 状态")
    if decision not in (APPROVAL_APPROVED, APPROVAL_REJECTED):
        raise EngineGuardError("APPROVAL_DECISION", "decision 必须是 approved/rejected")
    if decision == APPROVAL_REJECTED and not (reason or "").strip():
        raise EngineGuardError(
            "REJECT_REASON_REQUIRED", "驳回必须携带理由")
    run = db.query(TaskNodeRun).filter(
        TaskNodeRun.id == approval.node_run_id).first()
    instance = _lock_instance(db, run.instance_id)
    db.refresh(run)  # 等锁期间状态可能已被并发事务改写
    db.refresh(approval)
    spec = _spec_of(instance)
    policies = _policies(spec)
    _guard_active(instance)
    result = EngineResult()
    actor = _actor_of(decided_by)
    approval.status = decision
    approval.reason = reason
    approval.decided_by = getattr(decided_by, "id", None) or _actor_of(
        decided_by)
    approval.decided_at = _now()
    ev.append_event(
        db, instance.id, ev.APPROVAL_DECIDED,
        {"node_id": run.node_id, "approval_id": approval.id,
         "decision": decision, "reason": reason},
        node_run_id=run.id, actor=actor, event_budget=policies.event_budget)
    run.status = NODE_COMPLETED
    run.output = {"decision": decision, "reason": reason}
    run.finished_at = _now()
    ev.append_event(
        db, instance.id, ev.NODE_COMPLETED,
        {"node_id": run.node_id, "attempt_no": run.attempt_no},
        node_run_id=run.id, actor=actor, event_budget=policies.event_budget)
    if decision == APPROVAL_APPROVED:
        _advance(db, instance, spec, run.node_id,
                 run.output, result, port="approved")
        return result
    # rejected：approved 口径的出边不走；rejected 端口的边逐条处理
    _guard_active(instance)
    _precheck_rework_budgets(db, instance, spec, run.node_id, port="rejected")
    for edge in _outgoing_edges(spec, run.node_id, port="rejected"):
        target = edge.get("to")
        if edge.get("rework"):
            _rework_target(db, instance, spec, target,
                           reason=reason or "", rejector=run.node_id,
                           result=result)
        else:
            _deliver(db, instance, spec, target, result,
                     routed_via=f"edge:{run.node_id}.rejected")
        if instance.status != INSTANCE_ACTIVE:
            break
    if instance.status == INSTANCE_ACTIVE and not _outgoing_edges(
            spec, run.node_id, port="rejected"):
        # 没有任何 rejected 出边的拒绝：实例失败（关口否决且无路可走）
        _complete_instance(db, instance, spec, INSTANCE_FAILED,
                           reason=f"approval:{run.node_id}:rejected-no-route")
        return result
    if instance.status == INSTANCE_ACTIVE and _outgoing_edges(
            spec, run.node_id, port="rejected"):
        # 打回触发后，审批节点本次尝试作废（决策留痕在 approval 行与
        # approval.decided 事件）——上游重做完成后的再投递才能重建
        # 关口（qwenpaw 语义：拒绝方工作单元 voided）
        run.status = NODE_VOIDED
        run.finished_at = _now()
        ev.append_event(
            db, instance.id, ev.NODE_VOIDED,
            {"node_id": run.node_id, "attempt_no": run.attempt_no,
             "cause": "rework_rejected"},
            node_run_id=run.id, actor=_ENGINE_ACTOR,
            event_budget=policies.event_budget)
    return result


# ---------------------------------------------------------------------------
# 人工节点：交活 / 打回直接上游
# ---------------------------------------------------------------------------


def human_reject(
    db: Session,
    node_run_id: str,
    *,
    reason: str,
    rejected_by,
    target_node_id: str | None = None,
) -> EngineResult:
    run = _load_run(db, node_run_id)
    instance = _lock_instance(db, run.instance_id)
    db.refresh(run)  # 等锁期间状态可能已被并发事务改写
    spec = _spec_of(instance)
    _guard_active(instance)
    if run.status != NODE_WAITING_HUMAN:
        raise EngineGuardError(
            "NODE_NOT_WAITING", f"节点 {run.node_id} 状态为 {run.status}，"
            f"仅 waiting_human 可打回")
    if not reason or not reason.strip():
        raise EngineGuardError("REJECT_REASON_REQUIRED", "驳回必须携带理由")
    if target_node_id is None:
        incoming = _incoming_edges(spec, run.node_id)
        sources = {(e.get("from") or "").split(".", 1)[0] for e in incoming}
        if not sources:
            # condition 分支目标没有入边：回溯路由到本节点的 condition 的 on 源
            for node in _nodes(spec).values():
                if node.get("kind") != NODE_CONDITION:
                    continue
                routed = set(node.get("default") or [])
                for branch in node.get("branches") or []:
                    routed.update(branch.get("to") or [])
                if run.node_id in routed:
                    sources.add((node.get("on") or "").split(".", 1)[0])
        if len(sources) != 1:
            raise EngineGuardError(
                "REJECT_TARGET_AMBIGUOUS",
                f"上游不唯一（{sorted(sources)}），必须显式指定 target_node_id")
        target_node_id = sources.pop()
    # 预算先检后改：打回预算耗尽时不得先作废打回方节点（否则实例卡死）
    _check_rework_budget(db, instance, spec, target_node_id)
    result = EngineResult()
    # 打回方自己的当前尝试作废（上游重做完成后按新输入重新派生）
    run.status = NODE_VOIDED
    run.error = f"rejected upstream: {reason[:1500]}"
    run.finished_at = _now()
    ev.append_event(
        db, instance.id, ev.NODE_VOIDED,
        {"node_id": run.node_id, "attempt_no": run.attempt_no,
         "cause": "rejected_upstream"},
        node_run_id=run.id, actor=_actor_of(rejected_by),
        event_budget=_policies(spec).event_budget)
    _rework_target(db, instance, spec, target_node_id, reason=reason,
                   rejector=run.node_id, result=result)
    return result


def _check_rework_budget(db: Session, instance: TaskInstance, spec: dict,
                         target_node_id: str) -> None:
    policies = _policies(spec)
    target_node = _nodes(spec).get(target_node_id)
    if target_node is None or target_node.get("kind") not in (
            NODE_AGENT, NODE_HUMAN):
        raise EngineGuardError(
            "REWORK_TARGET", f"打回目标 {target_node_id} 不是可重做节点")
    # 各尝试行存的是重做序号（第 N 次重做 = N），预算以最大序号计
    used = (
        db.query(TaskNodeRun)
        .filter(TaskNodeRun.instance_id == instance.id,
                TaskNodeRun.node_id == target_node_id)
        .with_entities(TaskNodeRun.rework_count)
        .all())
    total_rework = max((count for (count,) in used), default=0)
    if total_rework >= policies.rework_per_edge:
        raise ReworkBudgetExhausted(
            f"节点 {target_node_id} 打回次数已达上限 "
            f"{policies.rework_per_edge}")


def _precheck_rework_budgets(db: Session, instance: TaskInstance, spec: dict,
                             rejector_node_id: str, *, port: str) -> None:
    for edge in _outgoing_edges(spec, rejector_node_id, port=port):
        if edge.get("rework"):
            _check_rework_budget(db, instance, spec, edge.get("to"))


def _rework_target(
    db: Session, instance: TaskInstance, spec: dict, target_node_id: str,
    *, reason: str, rejector: str, result: EngineResult,
) -> None:
    policies = _policies(spec)
    target_node = _nodes(spec).get(target_node_id)
    if target_node is None or target_node.get("kind") not in (
            NODE_AGENT, NODE_HUMAN):
        raise EngineGuardError(
            "REWORK_TARGET", f"打回目标 {target_node_id} 不是可重做节点")
    # 各尝试行存的是重做序号（第 N 次重做 = N），预算以最大序号计
    used = (
        db.query(TaskNodeRun)
        .filter(TaskNodeRun.instance_id == instance.id,
                TaskNodeRun.node_id == target_node_id)
        .with_entities(TaskNodeRun.rework_count)
        .all())
    total_rework = max((count for (count,) in used), default=0)
    if total_rework >= policies.rework_per_edge:
        raise ReworkBudgetExhausted(
            f"节点 {target_node_id} 打回次数已达上限 "
            f"{policies.rework_per_edge}")
    ev.append_event(
        db, instance.id, ev.NODE_REJECTED,
        {"node_id": rejector, "target_node_id": target_node_id,
         "reason": reason, "rejector": rejector},
        actor=_ENGINE_ACTOR, event_budget=policies.event_budget)
    target_latest = _latest_attempt(db, instance.id, target_node_id)
    rejected_output = (target_latest.output or {}) if (
        target_latest and target_latest.status == NODE_COMPLETED) else None
    if target_latest is not None and target_latest.status == NODE_COMPLETED:
        target_latest.status = NODE_VOIDED
        target_latest.finished_at = _now()
        ev.append_event(
            db, instance.id, ev.NODE_VOIDED,
            {"node_id": target_node_id,
             "attempt_no": target_latest.attempt_no,
             "cause": "rework"},
            node_run_id=target_latest.id, actor=_ENGINE_ACTOR,
            event_budget=policies.event_budget)
    # 以「原始入参 + 驳回上下文」重做（保留 taskContext/output 引用）
    base_inputs = list(target_latest.inputs or []) if target_latest else [{
        "kind": "taskContext", "goal": instance.goal,
        "inputs": instance.inputs or {}}]
    base_inputs.append({
        "kind": "rejection", "reason": reason, "rejector": rejector,
        "rejected_output": rejected_output})
    _create_attempt(db, instance, spec, target_node_id,
                    inputs_list=base_inputs, actor=_ENGINE_ACTOR,
                    result=result, routed_via=f"rework:{rejector}",
                    rework_count=total_rework + 1)


# ---------------------------------------------------------------------------
# 取消（两阶段语义在单事务内完成：先冻结后级联）
# ---------------------------------------------------------------------------


def cancel_instance(db: Session, instance_id: str, *, reason: str,
                    actor) -> TaskInstance:
    instance = _lock_instance(db, instance_id)
    if instance.status not in (INSTANCE_ACTIVE,):
        return instance
    spec = _spec_of(instance)
    policies = _policies(spec)
    instance.status = "cancelling"  # 冻结：并发推进在行锁后看到 cancelling
    runs = db.query(TaskNodeRun).filter(
        TaskNodeRun.instance_id == instance.id,
        TaskNodeRun.status.in_((NODE_PENDING, NODE_DISPATCHED, NODE_RUNNING,
                                NODE_WAITING_HUMAN, NODE_WAITING_APPROVAL)),
    ).all()
    cancelled_ids: list[str] = []
    for run in runs:
        run.status = NODE_CANCELLED if run.status in (
            NODE_DISPATCHED, NODE_RUNNING, NODE_WAITING_HUMAN,
            NODE_WAITING_APPROVAL) else NODE_SKIPPED
        run.finished_at = _now()
        cancelled_ids.append(run.node_id)
    db.query(TaskApproval).filter(
        TaskApproval.instance_id == instance.id,
        TaskApproval.status == APPROVAL_PENDING,
    ).update({"status": APPROVAL_CANCELLED}, synchronize_session=False)
    instance.status = INSTANCE_CANCELLED
    instance.cancel_reason = reason
    instance.finished_at = _now()
    ev.append_event(
        db, instance.id, ev.INSTANCE_TERMINAL,
        {"outcome": INSTANCE_CANCELLED, "reason": reason,
         "cancelled_nodes": cancelled_ids},
        actor=_actor_of(actor), event_budget=policies.event_budget,
        allow_over_budget=True)
    return instance


# ---------------------------------------------------------------------------
# 实例收束
# ---------------------------------------------------------------------------


def _complete_instance(db: Session, instance: TaskInstance, spec: dict,
                        outcome: str, *, reason: str) -> None:
    if instance.status != INSTANCE_ACTIVE:
        return
    policies = _policies(spec)
    runs = db.query(TaskNodeRun).filter(
        TaskNodeRun.instance_id == instance.id,
        TaskNodeRun.status.in_((NODE_PENDING, NODE_DISPATCHED, NODE_RUNNING,
                                NODE_WAITING_HUMAN, NODE_WAITING_APPROVAL)),
    ).all()
    for run in runs:
        run.status = NODE_CANCELLED if run.status in (
            NODE_DISPATCHED, NODE_RUNNING, NODE_WAITING_HUMAN,
            NODE_WAITING_APPROVAL) else NODE_SKIPPED
        run.finished_at = _now()
    if outcome == INSTANCE_FAILED:
        instance.fail_reason = reason
    instance.status = outcome
    instance.finished_at = _now()
    ev.append_event(
        db, instance.id, ev.INSTANCE_TERMINAL,
        {"outcome": outcome, "reason": reason},
        actor=_ENGINE_ACTOR, event_budget=policies.event_budget,
        allow_over_budget=True)


# ---------------------------------------------------------------------------
# 执行器侧原语：认领 / 进度 / 派发后补
# ---------------------------------------------------------------------------


def claim_node(db: Session, node_run_id: str, *, owner: str,
               lease_minutes: int = 30) -> TaskNodeRun:
    from datetime import datetime, timedelta, timezone
    run = _load_run(db, node_run_id)
    instance = _lock_instance(db, run.instance_id)
    db.refresh(run)  # 等锁期间状态可能已被并发事务改写
    if run.status not in (NODE_DISPATCHED, NODE_RUNNING):
        raise EngineGuardError(
            "NODE_NOT_DISPATCHED", f"节点 {run.node_id} 状态为 {run.status}")
    if instance is None or instance.status != INSTANCE_ACTIVE:
        raise EngineGuardError("INSTANCE_NOT_ACTIVE", "实例非 active")
    run.status = NODE_RUNNING
    run.lease_owner = owner
    run.lease_expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=lease_minutes)
    if run.started_at is None:
        run.started_at = _now()
    ev.append_event(
        db, run.instance_id, ev.NODE_CLAIMED,
        {"node_id": run.node_id, "attempt_no": run.attempt_no,
         "owner": owner},
        node_run_id=run.id, actor=f"executor:{owner}",
        event_budget=_policies(_spec_of(instance)).event_budget)
    return run


def record_progress(db: Session, node_run_id: str, note: str) -> None:
    run = _load_run(db, node_run_id)
    instance = db.query(TaskInstance).filter(
        TaskInstance.id == run.instance_id).first()
    if instance is None or instance.status != INSTANCE_ACTIVE:
        return
    ev.append_event(
        db, run.instance_id, ev.NODE_PROGRESS,
        {"node_id": run.node_id, "attempt_no": run.attempt_no,
         "note": note[:1000]},
        node_run_id=run.id, actor=_ENGINE_ACTOR,
        event_budget=_policies(_spec_of(instance)).event_budget)


def promote_pending(db: Session, instance_id: str) -> EngineResult:
    """并行预算释放后补派 pending 的就绪 agent 尝试。"""
    instance = db.query(TaskInstance).filter(
        TaskInstance.id == instance_id).first()
    if instance is None or instance.status != INSTANCE_ACTIVE:
        return EngineResult()
    spec = _spec_of(instance)
    result = EngineResult()
    runs = db.query(TaskNodeRun).filter(
        TaskNodeRun.instance_id == instance_id,
        TaskNodeRun.status == NODE_PENDING,
    ).order_by(TaskNodeRun.attempt_no).all()
    for run in runs:
        node = _nodes(spec).get(run.node_id, {})
        if node.get("kind") != NODE_AGENT:
            continue
        running = (
            db.query(TaskNodeRun)
            .filter(TaskNodeRun.instance_id == instance_id,
                    TaskNodeRun.status.in_((NODE_DISPATCHED, NODE_RUNNING)))
            .count())
        if running >= _policies(spec).max_parallelism:
            break
        run.status = NODE_DISPATCHED
        run.dispatched_at = _now()
        result.dispatches.append(run.id)
        ev.append_event(
            db, instance_id, ev.NODE_DISPATCHED,
            {"node_id": run.node_id, "attempt_no": run.attempt_no},
            node_run_id=run.id, actor=_ENGINE_ACTOR,
            event_budget=_policies(spec).event_budget)
    return result


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _load_run(db: Session, node_run_id: str) -> TaskNodeRun:
    run = db.query(TaskNodeRun).filter(TaskNodeRun.id == node_run_id).first()
    if run is None:
        raise EngineGuardError("NODE_RUN_NOT_FOUND", "节点尝试不存在")
    return run


def _lock_instance(db: Session, instance_id: str) -> TaskInstance:
    instance = (
        db.query(TaskInstance)
        .filter(TaskInstance.id == instance_id)
        .with_for_update()
        .first()
    )
    if instance is None:
        raise EngineGuardError("INSTANCE_NOT_FOUND", "实例不存在")
    return instance
