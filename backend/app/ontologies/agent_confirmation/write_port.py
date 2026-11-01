"""受控动作资产写回端口 — 纯协议、冻结计划与规则派生（组合根注入适配器）。

方向约束：本模块属于 ``ontologies`` 域，只声明协议、数据结构与纯派生逻辑，
绝不 import ``data_channel``；manual/curated/integrated 三源适配器由组合根
（main.py）经 :func:`set_action_write_port` 注入，先例见
``app/ontologies/mappings/remote_binding.py`` 的 validator 端口。

资产写语义：

- 单确认 → 单资产 → 单行写；未支持组合（多资产/跨库、create_object 多行、
  链接/通知/webhook 与资产写混合、绑定类型上的 create_link/delete_link）
  在任何写之前拒绝，源侧零变更。未绑定资产的动作完整保留本地语义。
- ``AssetWritePlan`` 在 prepare 阶段由服务端读取 token/version 后冻结并纳入
  payload_hash 与 guard_hash（guard 只取业务恒等投影）；expected token /
  before 行等 CAS 私有材料只存服务端结构（confirmation.parameter_sources
  保留键），绝不进入公开展示载荷。
- 执行按冻结计划重验（release / schema / 源权限 / CAS token）；冲突或漂移
  即失败且不自动重放；commit unknown 保持 unknown，由人工对账。
- curated 提交 = 变更已记录待审核：confirmation.state=succeeded 且
  outcome.status=pending_review（affectedRows=0）；后续状态读取按 review
  真实状态只刷新 outcome，绝不重新执行动作。动作确认与数据审核两层状态
  独立，不复用引擎 HITL 的 pending_approval/approval_rejected。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

from sqlalchemy.orm import Session

# confirmation.parameter_sources 中的保留键：服务端冻结的完整 WritePlan
# （含 CAS 私有材料）。该列不进入任何公开响应，payload_hash/guard_hash 都
# 覆盖本键内容；用户参数不得占用该名字。
WRITE_PLAN_RESERVED_KEY = "__write_plan__"

PLAN_VERSION = 1

ASSET_KIND_MANUAL = "manual"
ASSET_KIND_CURATED = "curated"
ASSET_KIND_INTEGRATED = "integrated"
ASSET_KINDS = (ASSET_KIND_MANUAL, ASSET_KIND_CURATED, ASSET_KIND_INTEGRATED)

OPERATION_INSERT = "insert"
OPERATION_UPDATE = "update"
OPERATION_DELETE = "delete"
OPERATIONS = (OPERATION_INSERT, OPERATION_UPDATE, OPERATION_DELETE)

# delete_row 是资产路由专用规则：本地引擎不支持对象删除（fail-closed），
# 未绑定资产的 delete_row 动作在 prepare 即拒绝。
RULE_TYPE_DELETE_ROW = "delete_row"

OUTCOME_APPLIED = "applied"
OUTCOME_PENDING_REVIEW = "pending_review"
OUTCOME_UNKNOWN = "unknown"
OUTCOME_FAILED = "failed"

# 冻结计划必填键（按资产种类差异化校验见 parse_frozen_plan）
_PLAN_REQUIRED_KEYS = (
    "planVersion", "assetKind", "ontologyId", "releaseId", "mappingId",
    "objectTypeId", "datasetId", "operation", "key", "values",
)
_PLAN_KIND_KEYS = {
    ASSET_KIND_INTEGRATED: ("assetId", "connectionId", "schemaFingerprint"),
    ASSET_KIND_MANUAL: ("expectedToken",),
    ASSET_KIND_CURATED: ("expectedToken",),
}


class WritePortError(Exception):
    """确定性的资产写拒绝（prepare 或执行前）；源侧零变更可证明。

    ``status`` 映射 HTTP 语义；``code`` 进入 outcome.errorCode 与确认
    failure_code，供 UI 与测试断言。
    """

    def __init__(self, code: str, message: str, *, status: int = 422,
                 **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = {k: v for k, v in extra.items() if v is not None}

    def detail(self) -> dict:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        payload.update(self.extra)
        return payload


@dataclass(frozen=True)
class TargetRef:
    """远程/资产行定位：对象类型 + 属性空间主键值（服务端派生资产，非用户 assetId）。"""

    object_type_id: str
    key: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OntologyWriteRequest:
    """从动作规则纯派生的写意图描述（资产无关；由适配器结合映射解析）。

    派生阶段不做组合拒绝：未绑定资产的动作必须完整保留既有本地语义
    （含多 effect 组合）。组合守卫在确认目标类型绑定后才由
    :func:`enforce_asset_write_guards` 执行。
    """

    operation: Optional[str]
    object_type_id: str
    property_values: dict[str, Any] = field(default_factory=dict)
    key_values: dict[str, Any] = field(default_factory=dict)
    target_instance_id: Optional[str] = None
    target_ref: Optional[TargetRef] = None
    link_type_ids: tuple[str, ...] = ()
    has_external_side_rules: bool = False
    extra_create_type_ids: tuple[str, ...] = ()
    mixed_row_writes: bool = False
    missing_write_target: bool = False
    # 依赖目标行值的取值映射（prop → 规则映射）：targetRef 场景在读取有界
    # 目标快照后由服务端求值并入计划（值求值顺序与引擎一致）。
    pending_value_mappings: dict[str, dict] = field(default_factory=dict)
    # 规则求值需读取的目标行属性（source_property 引用 + validation/表达式中
    # object.X 依赖）：服务端授权后进入有界目标快照；未授权字段不得先读后滤。
    read_properties: tuple[str, ...] = ()

    def touched_type_ids(self) -> set[str]:
        """可能涉及资产绑定的类型集合（守卫与路由判定的输入）。"""
        touched: set[str] = set()
        if self.operation is not None:
            touched.add(self.object_type_id)
        touched.update(self.extra_create_type_ids)
        return touched


@dataclass(frozen=True)
class AssetWriteOutcome:
    """资产写结果 — 公开 outcome 契约的唯一来源（camelCase 序列化见 to_public）。"""

    asset_kind: str
    status: str
    operation: str
    affected_rows: int
    dataset_id: str
    version_id: Optional[str] = None
    review_id: Optional[str] = None
    receipt_id: Optional[str] = None
    error_code: Optional[str] = None
    message: Optional[str] = None
    # curated 待审的基线版本（pending 期 version 为空，如实不造版本 ID）
    base_version_id: Optional[str] = None

    def to_public_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "assetKind": self.asset_kind,
            "status": self.status,
            "operation": self.operation,
            "affectedRows": self.affected_rows,
            "datasetId": self.dataset_id,
        }
        if self.version_id is not None:
            payload["versionId"] = self.version_id
        if self.base_version_id is not None:
            payload["baseVersionId"] = self.base_version_id
        if self.review_id is not None:
            payload["reviewId"] = self.review_id
        if self.receipt_id is not None:
            payload["receiptId"] = self.receipt_id
        if self.error_code is not None:
            payload["errorCode"] = self.error_code
        return payload

    def to_evidence_dict(self) -> dict[str, Any]:
        """内部审计（attempt.evidence_ref）：公开字段 + message。不含行值/秘密。"""
        payload = self.to_public_dict()
        if self.message is not None:
            payload["message"] = self.message
        return payload


@runtime_checkable
class ActionWritePort(Protocol):
    """三源资产写适配器协议（data_channel 侧实现，组合根注入）。

    resolve_write：结合 release 快照映射解析写意图 → 冻结计划 dict；返回
    None 表示目标类型无资产绑定（动作保持本地语义）。任何拒绝抛
    WritePortError（prepare 即失败，零源变更）。

    apply_write：按冻结计划执行单行写；幂等键由调用方按 confirmation 派生。
    返回 AssetWriteOutcome；确定性拒绝也用 WritePortError 表达。

    def inspect_write：只读回访（curated 按审核真实状态刷新 outcome）；
    ``evidence`` 是执行时回执（含 reviewId），绑定真实 review 而非按
    dataset+version 猜测。无该语义的资产返回 None。绝不重新执行写。
    """

    def resolve_write(
            self, db: Session, user: Any, *, ontology_id: str,
            release_id: str, request: OntologyWriteRequest) -> Optional[dict]:
        ...

    def apply_write(
            self, db: Session, user: Any, plan: dict, *,
            confirmation_id: str, actor_id: str) -> AssetWriteOutcome:
        ...

    def inspect_write(
            self, db: Session, user: Any, plan: dict, *,
            evidence: dict) -> Optional[AssetWriteOutcome]:
        ...


_port: Optional[ActionWritePort] = None


def set_action_write_port(port: ActionWritePort) -> None:
    """组合根注册（幂等覆盖）；None 拒绝以保持 fail-closed。"""
    global _port
    if port is None:
        raise WritePortError(
            "asset_write_port_invalid", "资产写端口不能为空")
    _port = port


def clear_action_write_port() -> None:
    """测试钩子：恢复未装配的 fail-closed 默认。"""
    global _port
    _port = None


def action_write_port() -> ActionWritePort:
    if _port is None:
        raise WritePortError(
            "asset_write_port_unavailable",
            "资产写端口未装配（组合根未注入 data_channel 适配器），"
            "拒绝资产写路由")
    return _port


def write_port_ready() -> bool:
    return _port is not None


# --------------------------------------------------------------- 冻结计划

def validate_frozen_plan(raw: Any) -> dict:
    """读取冻结计划（confirmation.parameter_sources 保留键）时的防御校验。

    冻结计划在 prepare 后不可变；这里只验证结构与取值域，防御历史/脏数据，
    不重算内容（重算会破坏冻结语义）。
    """
    if not isinstance(raw, dict):
        raise WritePortError("write_plan_invalid", "冻结计划不是对象")
    missing = [key for key in _PLAN_REQUIRED_KEYS if key not in raw]
    if missing:
        raise WritePortError(
            "write_plan_invalid", "冻结计划缺少必需字段", missingFields=missing)
    if raw.get("planVersion") != PLAN_VERSION:
        raise WritePortError(
            "write_plan_invalid",
            f"冻结计划版本不支持：{raw.get('planVersion')}")
    if raw.get("assetKind") not in ASSET_KINDS:
        raise WritePortError(
            "write_plan_invalid", f"资产种类非法：{raw.get('assetKind')}")
    if raw.get("operation") not in OPERATIONS:
        raise WritePortError(
            "write_plan_invalid", f"写操作非法：{raw.get('operation')}")
    for key in _PLAN_KIND_KEYS.get(str(raw.get("assetKind")), ()):
        if raw.get(key) is None:
            raise WritePortError(
                "write_plan_invalid", f"冻结计划缺少 {asset_kind_label(raw)} 专属字段 {key}")
    if not isinstance(raw.get("key"), dict) or not isinstance(
            raw.get("values"), dict):
        raise WritePortError(
            "write_plan_invalid", "冻结计划 key/values 必须是对象")
    return raw


def asset_kind_label(plan: dict) -> str:
    return str(plan.get("assetKind") or "unknown")


def public_plan_summary(plan: dict) -> dict:
    """display_payload.target.asset 的公开摘要 — 不含 CAS 私有材料。

    只暴露：资产身份（datasetId/name/kind）、操作、定位键（业务主键值，
    用户要确认的正是这一行）与写入字段名清单。expected token、before 行、
    schema fingerprint、资产内部 id（assetId/connectionId）一律不出现。
    """
    return {
        "kind": plan.get("assetKind"),
        "datasetId": plan.get("datasetId"),
        "name": plan.get("datasetName"),
        "operation": plan.get("operation"),
        "key": dict(plan.get("key") or {}),
        "fields": sorted(str(name) for name in (plan.get("values") or {})),
    }


def plan_idempotency_key(confirmation_id: str) -> str:
    """集成源写的稳定幂等键（源端回执去重；manual/curated 由操作账本保证）。"""
    return f"proposal-write:{confirmation_id}"


# guard 只取业务恒等键：CAS 材料（expectedToken/before/targetSnapshot）与
# 指纹随源快照变化，进入 guard 会让同一业务意图的不同准备失去互斥（并发双写）。
_GUARD_VOLATILE_PLAN_KEYS = (
    "expectedToken", "before", "targetSnapshot", "schemaFingerprint")


def logical_guard_plan(plan: dict) -> dict:
    """冻结计划的业务恒等投影（guard_hash 输入）— 剔除易变 CAS 材料。"""
    return {key: value for key, value in plan.items()
            if key not in _GUARD_VOLATILE_PLAN_KEYS}


# --------------------------------------------------------------- 规则派生

def build_write_request(
        action: Any,
        final_params: dict[str, Any],
        *,
        target_props: Optional[dict[str, Any]],
        target_instance_id: Optional[str],
        target_ref: Optional[TargetRef],
        resolve_value,
        defer_target_dependent: bool = False) -> OntologyWriteRequest:
    """从动作规则纯派生写意图（描述性，不做组合拒绝）。

    ``resolve_value`` 是值解析器（formal_modeling 的 _resolve_value 同形），
    以注入方式保持本模块与引擎内核的可测试边界。未绑定资产的动作经
    :func:`enforce_asset_write_guards` 之外的完整本地语义执行；派生只负责
    忠实记录：单行写（update_property 集 / 单 create_object）、链接规则、
    通知/webhook 副作用、混合行写与缺目标等事实。
    """
    from app.ontologies.formal_modeling.action_validation import (
        _prepare_action_rules,
    )

    if target_instance_id is not None and target_ref is not None:
        raise WritePortError(
            "target_ref_conflict", "targetInstanceId 与 targetRef 不能同时提供")
    rules, rule_errors = _prepare_action_rules(action)
    if rule_errors:
        raise WritePortError(
            "action_rules_invalid", "动作规则定义无效",
            status=422, errors=rule_errors)

    operation: Optional[str] = None
    object_type_id = str(action.object_type_id or "")
    property_values: dict[str, Any] = {}
    link_type_ids: list[str] = []
    has_external_side_rules = False
    create_types: list[str] = []
    mixed_row_writes = False
    missing_write_target = False
    pending_value_mappings: dict[str, dict] = {}
    read_properties: set[str] = set()

    # 与引擎执行同序（order 稳定排序，后写覆盖先写）。
    for rule in sorted(rules, key=lambda item: item.get("order", 0)):
        rtype = rule.get("type")
        cfg = rule.get("config", {}) or {}
        if rtype == "validation":
            _collect_target_refs(cfg.get("condition"), read_properties)
            continue
        if rtype in ("create_link", "delete_link"):
            link_type_id = str(cfg.get("linkTypeId") or "").strip()
            if not link_type_id:
                raise WritePortError(
                    "unsupported_combination",
                    "链接规则缺少 linkTypeId，无法做资产绑定守卫检查")
            link_type_ids.append(link_type_id)
            continue
        if rtype in ("notification", "webhook"):
            has_external_side_rules = True
            continue
        if rtype == "update_property":
            if operation is not None and operation != OPERATION_UPDATE:
                # update 与 create/delete 混合才是多行；同一目标的多个
                # update_property 合并为单行多属性写。
                mixed_row_writes = True
            if not target_instance_id and target_ref is None:
                missing_write_target = True
            prop = cfg.get("targetProperty")
            if isinstance(prop, str) and prop:
                mapping = {"sourceType": cfg.get("valueSource", "constant"),
                           "sourceValue": cfg.get("value", ""),
                           "functionId": cfg.get("functionId")}
                _collect_mapping_reads(mapping, read_properties)
                if defer_target_dependent and _mapping_needs_target(mapping):
                    # 目标行值未读前不求值：读取有界快照后由服务端按同一
                    # 顺序求值并入计划，避免用空 target_props 算错值。
                    pending_value_mappings[prop] = mapping
                else:
                    value = resolve_value(mapping, final_params,
                                          target_props or {})
                    # 规则按 order 排序执行，后写覆盖先写 — 与本地引擎
                    # update_property 顺序语义一致。
                    property_values[prop] = value
            operation = OPERATION_UPDATE
            object_type_id = _target_object_type_id(
                action, target_instance_id, target_ref)
            continue
        if rtype == RULE_TYPE_DELETE_ROW:
            if operation is not None or create_types:
                mixed_row_writes = True
            if not target_instance_id and target_ref is None:
                missing_write_target = True
            operation = OPERATION_DELETE
            property_values = {}
            object_type_id = _target_object_type_id(
                action, target_instance_id, target_ref)
            continue
        if rtype == "create_object":
            create_type = str(cfg.get("targetObjectTypeId") or "")
            if operation is not None or create_types:
                mixed_row_writes = True
            create_types.append(create_type)
            mappings = cfg.get("propertyMappings", [])
            if isinstance(mappings, list):
                values: dict[str, Any] = {}
                for mapping in mappings:
                    if isinstance(mapping, dict) and mapping.get("targetProperty"):
                        target_property = str(mapping["targetProperty"])
                        _collect_mapping_reads(mapping, read_properties)
                        if defer_target_dependent and _mapping_needs_target(mapping):
                            pending_value_mappings[target_property] = mapping
                        else:
                            values[target_property] = resolve_value(
                                mapping, final_params, target_props or {})
                property_values = values
            operation = OPERATION_INSERT
            object_type_id = create_type
            continue
        raise WritePortError(
            "unsupported_combination", f"未知动作规则类型: {rtype}")

    primary_create = create_types[0] if create_types else None
    extra_creates = tuple(create_types[1:]) if len(create_types) > 1 else ()
    key_values: dict[str, Any] = {}
    if target_ref is not None:
        key_values = {
            str(name): value
            for name, value in (target_ref.key or {}).items()
        }
    return OntologyWriteRequest(
        operation=operation,
        object_type_id=object_type_id,
        property_values=property_values,
        key_values=key_values,
        target_instance_id=target_instance_id,
        target_ref=target_ref,
        link_type_ids=tuple(link_type_ids),
        has_external_side_rules=has_external_side_rules,
        extra_create_type_ids=extra_creates,
        mixed_row_writes=mixed_row_writes,
        missing_write_target=missing_write_target,
        pending_value_mappings=pending_value_mappings,
        read_properties=tuple(sorted(read_properties)),
    )


def enforce_asset_write_guards(request: OntologyWriteRequest) -> None:
    """目标类型绑定资产后的组合守卫 — 任何拒绝都先于源侧零变更。

    - 多行/多类型行写（update+create、多个 create_object）→ 拒绝；
    - create_object 其它类型与主写并存 → 拒绝（多资产未支持）；
    - create_link/delete_link → 拒绝（链接写本地副本会造成源与投影分叉）；
    - notification/webhook 与行写并存 → 拒绝（单外部写点）；
    - update 缺目标定位或无写入字段 → 拒绝；
    - create 主类型与动作/目标类型不一致时由适配器的映射解析兜底拒绝。
    """
    if request.mixed_row_writes:
        raise WritePortError(
            "unsupported_combination",
            "资产写动作不支持多行/多类型行写组合（单确认→单资产→单行）")
    if request.extra_create_type_ids:
        raise WritePortError(
            "unsupported_combination",
            "资产写动作只支持单一 create_object 规则（多资产组合未支持）")
    if request.link_type_ids:
        raise WritePortError(
            "unsupported_combination",
            "资产写动作不支持与 create_link/delete_link 混合"
            "（链接写本地副本会造成源与投影分叉）")
    if request.has_external_side_rules:
        raise WritePortError(
            "unsupported_combination",
            "资产写动作不支持与 notification/webhook 混合（单外部写点）")
    if request.missing_write_target:
        raise WritePortError(
            "unsupported_combination",
            "行写动作需要目标实例或 targetRef 定位")
    if request.operation == OPERATION_UPDATE and not request.property_values:
        raise WritePortError(
            "unsupported_combination", "update 未解析出任何写入字段")
    if request.operation == OPERATION_DELETE and request.property_values:
        raise WritePortError(
            "unsupported_combination", "delete 不接受写入字段")


def structural_write_targets(action: Any) -> tuple[
        Optional[str], set[str], tuple[str, ...]]:
    """纯结构扫描动作规则的写触达（不求值、不解析参数）。

    返回 (operation, touched type ids, link type ids)。供引擎直连入口的
    影子写守卫使用：绑定类型的行写绝不能落本地 fo 副本。
    """
    from app.ontologies.formal_modeling.action_validation import (
        _prepare_action_rules,
    )

    rules, _errors = _prepare_action_rules(action)
    operation: Optional[str] = None
    types: set[str] = set()
    links: list[str] = []
    for rule in sorted(rules, key=lambda item: item.get("order", 0)):
        rtype = rule.get("type")
        cfg = rule.get("config", {}) or {}
        if rtype in ("validation", "notification", "webhook"):
            continue
        if rtype in ("create_link", "delete_link"):
            links.append(str(cfg.get("linkTypeId") or ""))
            continue
        if rtype == "create_object":
            types.add(str(cfg.get("targetObjectTypeId") or ""))
            operation = OPERATION_INSERT
            continue
        if getattr(action, "object_type_id", None):
            types.add(str(action.object_type_id))
        operation = (OPERATION_DELETE if rtype == RULE_TYPE_DELETE_ROW
                     else OPERATION_UPDATE)
    return operation, types, tuple(links)


def bound_object_type_ids(db, ontology_id: str, release_id: str | None
                          ) -> set[str]:
    """release 快照映射覆盖的对象类型 id（fail-closed，快照损坏即抛错）。"""
    from app.ontologies.versions.models import OntologyVersion

    if release_id is None:
        return set()
    release = db.query(OntologyVersion).filter(
        OntologyVersion.id == release_id,
        OntologyVersion.ontology_id == ontology_id,
    ).first()
    if release is None:
        return set()
    snapshot = release.snapshot_formal
    if snapshot is None:
        return set()
    if not isinstance(snapshot, dict):
        raise WritePortError(
            "mapping_contract_invalid", "发布快照损坏，无法判定资产绑定")
    by_name: dict[str, str] = {}
    for ot in snapshot.get("objectTypes") or []:
        if isinstance(ot, dict) and ot.get("id"):
            for key in ("name", "displayName"):
                if ot.get(key):
                    by_name[str(ot[key])] = str(ot["id"])
    bound: set[str] = set()
    for entry in snapshot.get("mappings") or []:
        if not isinstance(entry, dict):
            continue
        target = entry.get("targetObjectTypeId")
        if target:
            bound.add(str(target))
            continue
        entity = str(entry.get("entityClass") or "")
        if entity and entity in by_name:
            bound.add(by_name[entity])
    return bound


def snapshot_link_endpoint_type_ids(db, ontology_id: str, release_id,
                                    link_type_ids) -> set[str]:
    """按 release 快照解析链接两端类型（fail-closed）。"""
    from app.ontologies.versions.models import OntologyVersion

    if release_id is None or not link_type_ids:
        return set()
    release = db.query(OntologyVersion).filter(
        OntologyVersion.id == release_id,
        OntologyVersion.ontology_id == ontology_id,
    ).first()
    if release is None:
        return set()
    snapshot = release.snapshot_formal
    if snapshot is not None and not isinstance(snapshot, dict):
        raise WritePortError(
            "mapping_contract_invalid", "发布快照损坏，无法判定链接绑定")
    wanted = {str(item) for item in link_type_ids}
    endpoints: set[str] = set()
    for link_type in (snapshot or {}).get("linkTypes") or []:
        if not isinstance(link_type, dict):
            continue
        if str(link_type.get("id") or "") in wanted:
            endpoints.add(str(link_type.get("sourceObjectTypeId") or ""))
            endpoints.add(str(link_type.get("targetObjectTypeId") or ""))
    return {item for item in endpoints if item}


_OBJECT_REF_RE = None  # 延迟编译（re 模块在函数内导入）


def _collect_mapping_reads(mapping: dict, read_properties: set) -> None:
    """值映射的目标行读取依赖：source_property 直引 + 表达式/模板 object.X。"""
    st = str(mapping.get("sourceType") or "")
    if st in ("source_property", "property"):
        ref = str(mapping.get("sourceValue") or "").strip()
        if ref:
            read_properties.add(ref)
        return
    if st in ("expression", "constant"):
        _collect_target_refs(mapping.get("sourceValue"), read_properties)


def _collect_target_refs(expression, read_properties: set) -> None:
    import re

    global _OBJECT_REF_RE
    if _OBJECT_REF_RE is None:
        _OBJECT_REF_RE = re.compile(r"object\.([A-Za-z_]\w*)")
    if not isinstance(expression, str):
        return
    for name in _OBJECT_REF_RE.findall(expression):
        read_properties.add(name)


_TARGET_DEPENDENT_SOURCES = {"source_property", "property",
                              "expression", "function"}


def _mapping_needs_target(mapping: dict) -> bool:
    st = str(mapping.get("sourceType") or "")
    return st in _TARGET_DEPENDENT_SOURCES or (
        st == "constant" and ("{{" in str(mapping.get("sourceValue") or "")
                              or "{object." in str(mapping.get("sourceValue")
                                                   or "")))


def _target_object_type_id(action: Any, target_instance_id: Optional[str],
                           target_ref: Optional[TargetRef]) -> str:
    if target_ref is not None and target_ref.object_type_id:
        return str(target_ref.object_type_id)
    if action.object_type_id:
        return str(action.object_type_id)
    raise WritePortError(
        "unsupported_combination",
        "无法确定写目标对象类型（动作未绑定类型且未提供 targetRef）",
        targetInstanceId=target_instance_id)
