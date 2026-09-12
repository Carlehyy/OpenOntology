"""本体版本树、影响分析与隔离试跑的纯服务层。

这里刻意不写生产 ObjectInstance/LinkInstance，也不执行 Action。试跑的唯一
副作用是写入 ontology_trial_* 隔离表，因而可以安全使用真实数据湖版本。
"""
from __future__ import annotations

import hashlib
import ast
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.data_channel.datasets.models import Dataset, DatasetVersion
from app.ontologies.mappings.models import OntologyMapping
from app.ontologies.formal_modeling import schemas as FS
from app.ontologies.versions.snapshot_contract import (
    SNAPSHOT_KEYS,
    canonical_snapshot,
    complete_snapshot,
    json_safe,
    next_draft_number,
    next_release_number,
    snapshot_hash,
    snapshot_models,
)
from app.ontologies.formal_modeling.function_engine import (
    evaluate_function_contract,
)
from app.ontologies.formal_modeling.safe_eval import SafeEvalError, safe_eval
from app.ontologies.formal_modeling.validation import (
    property_value_type_issue,
    validate_model,
)
from app.ontologies.mappings.mapping_service import (
    MappingService, MappingSourceError, load_mapping_source_rows,
)
from app.ontologies.mappings.formal_projection import (
    _coerce_props_to_type,
    stable_link_instance_id,
    stable_object_instance_id,
    stable_pipeline_entity_id,
    stable_pipeline_relation_id,
)
from app.ontologies.versions.models import (
    OntologyTrialLink, OntologyTrialObject, OntologyTrialRun,
)


MAX_SENTINEL_TUPLES = 1000
BUILTIN_SENTINEL_TRIGGER_MODES = frozenset({
    "on_enter", "on_enter_leave", "run_on_all", "on_pattern",
})
BUILTIN_SENTINEL_SCAN_INTERVAL_MIN = 60
BUILTIN_SENTINEL_SCAN_INTERVAL_MAX = 86_400






def validate_builtin_sentinel_contract(sentinels: Any) -> list[dict]:
    """Validate release-built Sentinel envelope fields without coercion.

    This validator deliberately operates on the raw snapshot dictionaries.  If
    values were first projected through ``bool()``/``int()``, strings such as
    ``"false"`` or an invalid scan interval could survive the isolated trial
    and fail (or change meaning) only while the draft is being promoted.

    Built-in Sentinels retain the editor's explicit "manual only" mode:
    ``onChange == onSchedule == False`` is valid and means that only the manual
    run endpoint evaluates the definition.  Assistant-created Sentinels keep
    their separate, stricter schema and are not routed through this contract.
    """
    if not isinstance(sentinels, list):
        return [{
            "code": "invalid_sentinel_collection",
            "kind": "sentinel",
            "id": "",
            "name": "",
            "field": "sentinels",
            "message": "sentinels 必须是数组",
        }]

    errors: list[dict] = []
    seen_ids: dict[str, int] = {}

    def add_error(
            code: str, message: str, *, index: int,
            item: dict | None = None, field: str = "") -> None:
        raw = item or {}
        raw_id = raw.get("id")
        sentinel_id = raw_id.strip() if isinstance(raw_id, str) else ""
        label = str(
            raw.get("displayName") or raw.get("name")
            or sentinel_id or f"第 {index + 1} 个哨兵"
        )
        error = {
            "code": code,
            "kind": "sentinel",
            "id": sentinel_id,
            "name": label,
            "message": message,
        }
        if field:
            error["field"] = field
        errors.append(error)

    for index, item in enumerate(sentinels):
        if not isinstance(item, dict):
            add_error(
                "invalid_sentinel_definition",
                f"第 {index + 1} 个哨兵定义必须是对象",
                index=index,
            )
            continue

        raw_id = item.get("id")
        sentinel_id = raw_id.strip() if isinstance(raw_id, str) else ""
        if (
            not isinstance(raw_id, str)
            or not sentinel_id
            or raw_id != sentinel_id
        ):
            add_error(
                "invalid_sentinel_id",
                "建模内置哨兵必须提供非空、无首尾空白的字符串 ID",
                index=index, item=item, field="id",
            )
        elif sentinel_id in seen_ids:
            add_error(
                "duplicate_sentinel_id",
                (
                    f"建模内置哨兵 ID「{sentinel_id}」重复"
                    f"（首次出现在第 {seen_ids[sentinel_id] + 1} 项）"
                ),
                index=index, item=item, field="id",
            )
        else:
            seen_ids[sentinel_id] = index

        for field, default in (
            ("onChange", True),
            ("onSchedule", False),
            ("muted", False),
            ("enabled", True),
        ):
            value = item.get(field, default)
            if type(value) is not bool:
                add_error(
                    "invalid_sentinel_boolean",
                    f"哨兵字段 {field} 必须是真正的布尔值，不能使用字符串、数字或 null",
                    index=index, item=item, field=field,
                )

        trigger_mode = item.get("triggerMode", "on_enter")
        if (
            not isinstance(trigger_mode, str)
            or trigger_mode not in BUILTIN_SENTINEL_TRIGGER_MODES
        ):
            add_error(
                "invalid_sentinel_trigger_mode",
                (
                    "triggerMode 必须是 "
                    + "、".join(sorted(BUILTIN_SENTINEL_TRIGGER_MODES))
                ),
                index=index, item=item, field="triggerMode",
            )

        pattern = item.get("pattern")
        if trigger_mode == "on_pattern":
            # 深度结构校验在共享发布门禁（validate_sentinels）执行；
            # 信封层只做形状防御，避免非法结构进入试跑/快照。
            from app.ontologies.sentinels.cep import pattern as cep_pattern
            if not isinstance(pattern, dict) or (
                    cep_pattern.normalize_pattern(pattern) is None):
                add_error(
                    "invalid_sentinel_pattern",
                    "triggerMode=on_pattern 时 pattern 必须是合法的模式定义"
                    "（stages/absence/within/aggregate/condition）",
                    index=index, item=item, field="pattern",
                )
            if item.get("onChange") is not True or (
                    item.get("onSchedule") is not True):
                add_error(
                    "invalid_sentinel_pattern_trigger_flags",
                    "模式哨兵必须同时开启 onChange 与 onSchedule"
                    "（事件驱动推进 + 定时扫描兜底超时判定）",
                    index=index, item=item, field="onChange",
                )
        elif pattern is not None:
            add_error(
                "invalid_sentinel_pattern_mode",
                "pattern 仅在 triggerMode=on_pattern 时允许出现",
                index=index, item=item, field="pattern",
            )

        interval = item.get("scanIntervalSeconds", 300)
        if type(interval) is not int:
            add_error(
                "invalid_sentinel_scan_interval_type",
                "scanIntervalSeconds 必须是整数秒，不能使用字符串、浮点数或布尔值",
                index=index, item=item, field="scanIntervalSeconds",
            )
        elif not (
            BUILTIN_SENTINEL_SCAN_INTERVAL_MIN
            <= interval
            <= BUILTIN_SENTINEL_SCAN_INTERVAL_MAX
        ):
            add_error(
                "invalid_sentinel_scan_interval_range",
                (
                    "scanIntervalSeconds 必须在 "
                    f"{BUILTIN_SENTINEL_SCAN_INTERVAL_MIN} 到 "
                    f"{BUILTIN_SENTINEL_SCAN_INTERVAL_MAX} 秒之间"
                ),
                index=index, item=item, field="scanIntervalSeconds",
            )

        condition_logic = item.get("conditionLogic", "and")
        if type(condition_logic) is not str or condition_logic not in {"and", "or"}:
            add_error(
                "invalid_sentinel_condition_logic",
                "conditionLogic 必须是 and 或 or",
                index=index, item=item, field="conditionLogic",
            )

    return errors












def validate_expression_function_contract(
        functions: list[Any], object_types: list[Any]) -> list[dict]:
    """Compile every enabled expression function without requiring sample rows."""
    from app.ontologies.formal_modeling.safe_eval import (
        validate_safe_expression,
    )

    object_by_id = {
        str(getattr(item, "id", "") or ""): item for item in object_types
    }
    errors: list[dict] = []
    for function in functions:
        if (
            not bool(getattr(function, "enabled", True))
            or str(getattr(function, "language", "") or "").strip().lower()
            != "expression"
        ):
            continue
        function_id = str(getattr(function, "id", "") or "")
        label = str(
            getattr(function, "display_name", None)
            or getattr(function, "name", "")
            or function_id
        )
        body = str(getattr(function, "body", "") or "").strip()
        if not body:
            errors.append({
                "code": "invalid_expression_function", "kind": "function",
                "id": function_id, "name": label, "field": "body",
                "message": f"启用的表达式函数「{label}」缺少 body",
            })
            continue
        try:
            tree = ast.parse(body.rstrip(";").strip(), mode="eval")
            local_names = {
                node.id for node in ast.walk(tree)
                if isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Store)
            }
            validate_safe_expression(
                body,
                {"object", "objects", "params", *local_names},
            )
        except Exception as exc:
            errors.append({
                "code": "invalid_expression_function", "kind": "function",
                "id": function_id, "name": label, "field": "body",
                "message": f"表达式函数「{label}」无法编译: {exc}",
            })
            continue

        target = object_by_id.get(str(
            getattr(function, "target_object_type_id", "") or ""))
        object_properties = {
            str(prop.get("name"))
            for prop in (
                (getattr(target, "properties", None) or [])
                if target is not None else []
            )
            if isinstance(prop, dict) and prop.get("name")
        }
        parameter_properties = {
            str(parameter.get("name"))
            for parameter in (getattr(function, "parameters", None) or [])
            if isinstance(parameter, dict) and parameter.get("name")
        }
        scopes = {
            "object": object_properties,
            "params": parameter_properties,
        }
        missing: set[str] = set()
        for node in ast.walk(tree):
            alias = prop = None
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
            ):
                alias, prop = node.value.id, node.attr
            elif (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
            ):
                alias, prop = node.value.id, node.slice.value
            if alias in scopes and prop not in scopes[alias]:
                missing.add(f"{alias}.{prop}")
        for reference in sorted(missing):
            errors.append({
                "code": "invalid_expression_function", "kind": "function",
                "id": function_id, "name": label, "field": "body",
                "message": (
                    f"表达式函数「{label}」引用了目标模型中不存在的属性: "
                    f"{reference}"
                ),
            })
    return errors


def validate_snapshot(
    snapshot: dict,
    *,
    require_object_type: bool = True,
    require_executable_action_rules: bool = True,
    warnings_out: list[dict] | None = None,
) -> list[dict]:
    """快照结构校验；默认严格（试跑/发布口径）。

    ``require_executable_action_rules=False`` 仅用于草稿保存期分层：
    「动作无启用的可执行副作用规则」降级为 warning，写入 ``warnings_out``
    （未提供 ``warnings_out`` 时回退到 errors，不允许静默丢失）；
    ``requiresApproval`` 动作本就合法（运行时挂起审批），两种模式都豁免。
    """
    source = snapshot if isinstance(snapshot, dict) else {}
    errors = validate_builtin_sentinel_contract(
        source.get("sentinels", []),
    )
    try:
        models = snapshot_models(snapshot)
    except Exception as exc:  # Pydantic gives precise field details in the text.
        errors.append({
            "code": "invalid_snapshot_shape", "kind": "ontology",
            "id": "", "name": "", "message": str(exc),
        })
        return errors
    errors.extend(validate_model(
        models["objectTypes"], models["linkTypes"], models["actions"],
        models["functions"], [], [],
    ))
    errors.extend(validate_expression_function_contract(
        models["functions"], models["objectTypes"]))
    from app.ontologies.formal_modeling.action_engine import (
        validate_action_definition,
    )
    for action in models["actions"]:
        action_warnings: list[str] = []
        messages = validate_action_definition(
            action, models["objectTypes"], models["linkTypes"],
            models["functions"],
            require_executable_action_rules=require_executable_action_rules,
            warnings_out=action_warnings)

        def action_entry(message: str, _action=action) -> dict:
            return {
                "code": "invalid_action_definition",
                "kind": "action",
                "id": str(getattr(_action, "id", "") or ""),
                "name": str(
                    getattr(_action, "display_name", None)
                    or getattr(_action, "name", "")
                    or getattr(_action, "id", "")
                ),
                "field": "rules",
                "message": message,
            }
        for message in messages:
            errors.append(action_entry(message))
        # 宽松模式未提供 warnings_out 时回退到 errors，降级信息不静默丢失。
        sink = warnings_out if warnings_out is not None else errors
        for message in action_warnings:
            sink.append(action_entry(message))
    if require_object_type and not models["objectTypes"]:
        errors.append({
            "code": "object_type_required", "kind": "ontology",
            "id": "", "name": "", "message": "试跑本体至少需要一个 ObjectType",
        })
    return errors


def validate_manual_mapping_trial_contract(
        db: Session, snapshot: dict,
        dataset_pins: Any) -> list[dict]:
    """Fence production auto-apply mappings to the exact isolated-trial pins.

    Drafts may remain incomplete while being edited.  Once a production trial
    is materialized, however, every mapping consuming a non-curated (manually
    governed) dataset must explicitly subscribe to version automation and must
    resolve to one unambiguous trial pin.  The contextual fields make object vs
    link and source/target/edge failures actionable to API/UI consumers.
    """
    snap = complete_snapshot(snapshot)
    pins_by_dataset: dict[str, list[dict]] = {}
    if isinstance(dataset_pins, list):
        for pin in dataset_pins:
            if not isinstance(pin, dict):
                continue
            dataset_id = str(pin.get("datasetId") or "").strip()
            if dataset_id:
                pins_by_dataset.setdefault(dataset_id, []).append(pin)

    dataset_ids = {
        str(item.get("curatedDatasetId") or "").strip()
        for item in snap["mappings"]
        if isinstance(item, dict) and item.get("curatedDatasetId")
    }
    for item in snap["linkMappings"]:
        if not isinstance(item, dict):
            continue
        dataset_ids.update(
            str(item.get(field) or "").strip()
            for field in ("srcDatasetId", "tgtDatasetId", "edgeDatasetId")
            if item.get(field)
        )
    datasets = {
        str(item.id): item
        for item in (
            db.query(Dataset).filter(Dataset.id.in_(dataset_ids)).all()
            if dataset_ids else []
        )
    }
    errors: list[dict] = []

    def add_error(
            *, code: str, kind: str, item_id: str, name: str,
            dataset_id: str, role: str, field: str, message: str) -> None:
        errors.append({
            "code": code,
            "kind": kind,
            "id": item_id,
            "name": name,
            "datasetId": dataset_id,
            "datasetRole": role,
            "field": field,
            "message": message,
        })

    def validate_pin(
            *, prefix: str, kind: str, item_id: str, name: str,
            dataset: Dataset, role: str) -> None:
        dataset_id = str(dataset.id)
        pins = pins_by_dataset.get(dataset_id, [])
        role_label = {
            "object": "对象",
            "source": "源端",
            "target": "目标端",
            "edge": "边数据",
        }.get(role, role)
        if not pins:
            add_error(
                code=f"{prefix}_trial_dataset_pin_missing",
                kind=kind, item_id=item_id, name=name,
                dataset_id=dataset_id, role=role,
                field="trial.datasetVersions",
                message=(
                    f"{kind}「{name}」的{role_label}人工数据集"
                    f"「{dataset.name}」缺少精确试跑版本 pin"
                ),
            )
            return
        if len(pins) != 1:
            add_error(
                code=f"{prefix}_trial_dataset_pin_ambiguous",
                kind=kind, item_id=item_id, name=name,
                dataset_id=dataset_id, role=role,
                field="trial.datasetVersions",
                message=(
                    f"{kind}「{name}」的{role_label}人工数据集"
                    f"「{dataset.name}」存在 {len(pins)} 个试跑版本 pin"
                ),
            )
            return
        pin = pins[0]
        version_id = str(pin.get("versionId") or "").strip()
        version = (
            db.query(DatasetVersion).filter(
                DatasetVersion.id == version_id,
                DatasetVersion.dataset_id == dataset_id,
            ).first()
            if version_id else None
        )
        if version is None:
            add_error(
                code=f"{prefix}_trial_dataset_pin_invalid",
                kind=kind, item_id=item_id, name=name,
                dataset_id=dataset_id, role=role,
                field="trial.datasetVersions.versionId",
                message=(
                    f"{kind}「{name}」的{role_label}试跑 pin "
                    f"{version_id or '（空）'} 不属于数据集「{dataset.name}」"
                ),
            )
            return
        if dataset.latest_version_id != version.id:
            add_error(
                code=f"{prefix}_trial_dataset_pin_stale",
                kind=kind, item_id=item_id, name=name,
                dataset_id=dataset_id, role=role,
                field="trial.datasetVersions.versionId",
                message=(
                    f"{kind}「{name}」的{role_label}试跑 pin "
                    f"未指向数据集「{dataset.name}」的当前精确版本"
                ),
            )
        if str(pin.get("checksum") or "") != str(version.checksum or ""):
            add_error(
                code=f"{prefix}_trial_dataset_pin_checksum_changed",
                kind=kind, item_id=item_id, name=name,
                dataset_id=dataset_id, role=role,
                field="trial.datasetVersions.checksum",
                message=(
                    f"{kind}「{name}」的{role_label}试跑 pin checksum "
                    f"与数据集「{dataset.name}」版本不一致"
                ),
            )

    for mapping in snap["mappings"]:
        if not isinstance(mapping, dict):
            continue
        dataset_id = str(mapping.get("curatedDatasetId") or "").strip()
        dataset = datasets.get(dataset_id)
        if dataset is None or dataset.kind == "curated":
            continue
        mapping_id = str(mapping.get("id") or "")
        label = str(mapping.get("entityClass") or mapping_id)
        field_mapping = mapping.get("fieldMapping")
        subscribed = (
            isinstance(field_mapping, dict)
            and field_mapping.get("__auto_apply_on_version__") is True
        )
        if not subscribed:
            add_error(
                code="mapping_manual_automation_not_subscribed",
                kind="mapping", item_id=mapping_id, name=label,
                dataset_id=dataset_id, role="object",
                field="fieldMapping.__auto_apply_on_version__",
                message=(
                    f"Mapping「{label}」消费人工数据集「{dataset.name}」，"
                    "精确试跑前必须显式开启版本后自动灌入"
                ),
            )
        validate_pin(
            prefix="mapping", kind="mapping",
            item_id=mapping_id, name=label,
            dataset=dataset, role="object",
        )

    for mapping in snap["linkMappings"]:
        if not isinstance(mapping, dict):
            continue
        mapping_id = str(mapping.get("id") or "")
        label = str(mapping.get("relationType") or mapping_id)
        field_mapping = mapping.get("fieldMapping")
        subscribed = (
            isinstance(field_mapping, dict)
            and field_mapping.get("__auto_apply_on_version__") is True
        )
        for role, field in (
            ("source", "srcDatasetId"),
            ("target", "tgtDatasetId"),
            ("edge", "edgeDatasetId"),
        ):
            dataset_id = str(mapping.get(field) or "").strip()
            dataset = datasets.get(dataset_id)
            if dataset is None or dataset.kind == "curated":
                continue
            if not subscribed:
                add_error(
                    code="link_mapping_manual_automation_not_subscribed",
                    kind="linkMapping", item_id=mapping_id, name=label,
                    dataset_id=dataset_id, role=role,
                    field="fieldMapping.__auto_apply_on_version__",
                    message=(
                        f"LinkMapping「{label}」的 {role} 角色消费人工数据集"
                        f"「{dataset.name}」，精确试跑前必须显式开启版本自动对账"
                    ),
                )
            validate_pin(
                prefix="link_mapping", kind="linkMapping",
                item_id=mapping_id, name=label,
                dataset=dataset, role=role,
            )
    return errors


def workspace_snapshot(body: dict, previous: dict | None) -> dict:
    """把图谱编辑器 DTO 写回完整快照，同时保留独立维护的映射和哨兵。"""
    parsed = FS.SaveFullOntologyRequest.model_validate(body)
    now = datetime.now(timezone.utc).isoformat()

    def dump(items: list) -> list[dict]:
        out = []
        for item in items:
            data = item.model_dump(mode="json", by_alias=True, exclude_none=False)
            data["id"] = data.get("id") or str(uuid.uuid4())
            data.setdefault("createdAt", now)
            data["updatedAt"] = now
            out.append(data)
        return out

    old = complete_snapshot(previous)
    return {
        "objectTypes": dump(parsed.object_types),
        "linkTypes": dump(parsed.link_types),
        "actions": dump(parsed.actions),
        "functions": dump(parsed.functions),
        "sentinels": old["sentinels"],
        "mappings": old["mappings"],
        "linkMappings": old["linkMappings"],
    }


def impact_report(base: dict | None, candidate: dict | None) -> dict:
    """输出可审核、可哈希的结构影响；breaking 项不会藏在总计里。"""
    before = complete_snapshot(base)
    after = complete_snapshot(candidate)
    resources: dict[str, dict] = {}
    breaking: list[dict] = []

    for key in SNAPSHOT_KEYS:
        left = {str(item.get("id")): item for item in before[key]}
        right = {str(item.get("id")): item for item in after[key]}
        added_ids = sorted(right.keys() - left.keys())
        deleted_ids = sorted(left.keys() - right.keys())
        modified_ids = sorted(
            item_id for item_id in left.keys() & right.keys()
            if left[item_id] != right[item_id]
        )
        resources[key] = {
            "added": added_ids, "modified": modified_ids,
            "deleted": deleted_ids,
        }
        for item_id in deleted_ids:
            breaking.append({
                "code": "resource_deleted", "resource": key, "id": item_id,
                "name": left[item_id].get("displayName") or left[item_id].get("name"),
                "message": "已发布结构中的元素将被删除",
            })

    left_types = {str(item.get("id")): item for item in before["objectTypes"]}
    right_types = {str(item.get("id")): item for item in after["objectTypes"]}
    for type_id in sorted(left_types.keys() & right_types.keys()):
        old = left_types[type_id]
        new = right_types[type_id]
        old_props = {str(p.get("id") or p.get("name")): p for p in old.get("properties") or []}
        new_props = {str(p.get("id") or p.get("name")): p for p in new.get("properties") or []}
        for prop_id in sorted(old_props.keys() - new_props.keys()):
            breaking.append({
                "code": "property_deleted", "resource": "objectTypes",
                "id": type_id, "propertyId": prop_id,
                "name": old_props[prop_id].get("displayName") or old_props[prop_id].get("name"),
                "message": "旧事实中的属性将失去新版定义",
            })
        for prop_id in sorted(old_props.keys() & new_props.keys()):
            old_prop, new_prop = old_props[prop_id], new_props[prop_id]
            if old_prop.get("type") != new_prop.get("type"):
                breaking.append({
                    "code": "property_type_changed", "resource": "objectTypes",
                    "id": type_id, "propertyId": prop_id,
                    "from": old_prop.get("type"), "to": new_prop.get("type"),
                    "message": "属性类型变化可能使旧数据无法注入",
                })
            if not old_prop.get("required") and new_prop.get("required"):
                breaking.append({
                    "code": "property_became_required", "resource": "objectTypes",
                    "id": type_id, "propertyId": prop_id,
                    "message": "新增必填约束可能拒绝旧数据",
                })
        if old.get("primaryKey") != new.get("primaryKey"):
            breaking.append({
                "code": "primary_key_changed", "resource": "objectTypes",
                "id": type_id, "from": old.get("primaryKey"),
                "to": new.get("primaryKey"),
                "message": "主键变化会改变对象身份和关系端点",
            })

    totals = {
        name: sum(len(resources[key][name]) for key in SNAPSHOT_KEYS)
        for name in ("added", "modified", "deleted")
    }
    report = {
        "resources": resources, "total": totals,
        "breaking": breaking, "breakingCount": len(breaking),
    }
    report["impactHash"] = hashlib.sha256(json.dumps(
        report, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")).hexdigest()
    return report


def _pk_columns(value: Any) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _row_identity(row: dict, pk: str) -> str | None:
    columns = _pk_columns(pk)
    if not columns or any(row.get(column) in (None, "") for column in columns):
        return None
    if len(columns) == 1:
        return f"{columns[0]}:{row.get(columns[0])}"
    return "composite_pk:" + json.dumps({
        "columns": columns, "values": [row.get(column) for column in columns],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _latest_version(db: Session, dataset_id: str) -> tuple[Dataset, DatasetVersion]:
    dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
    if dataset is None:
        raise MappingSourceError(f"映射绑定的数据集 {dataset_id} 不存在")
    version = None
    if dataset.latest_version_id:
        version = db.query(DatasetVersion).filter(
            DatasetVersion.id == dataset.latest_version_id,
            DatasetVersion.dataset_id == dataset_id,
        ).first()
    if version is None:
        raise MappingSourceError(f"数据集「{dataset.name}」没有可固定的 latest 版本")
    return dataset, version


def _object_type_for_mapping(mapping: dict, object_types: dict[str, dict]) -> dict | None:
    target = mapping.get("targetObjectTypeId")
    if target and str(target) in object_types:
        return object_types[str(target)]
    entity_class = str(mapping.get("entityClass") or "")
    return next((item for item in object_types.values()
                 if item.get("name") == entity_class
                 or item.get("displayName") == entity_class), None)


def _mapping_entity_class_target_errors(
        mappings: list[dict], object_types: dict[str, dict]) -> list[dict]:
    """Reject a legacy Entity namespace that would route to multiple types.

    MappingService stores ``Entity.type == entityClass`` and Formal projection
    groups by that value.  Therefore one entityClass cannot safely mean two
    ObjectTypes, even if today's primary-key values happen not to overlap.
    """
    targets_by_class: dict[str, set[str]] = {}
    mapping_ids_by_class: dict[str, list[str]] = {}
    for mapping in mappings:
        entity_class = str(mapping.get("entityClass") or "").strip()
        target = _object_type_for_mapping(mapping, object_types)
        target_id = str((target or {}).get("id") or "")
        if not entity_class or not target_id:
            continue
        targets_by_class.setdefault(entity_class, set()).add(target_id)
        mapping_ids_by_class.setdefault(entity_class, []).append(
            str(mapping.get("id") or ""))

    return [{
        "code": "mapping_entity_class_target_ambiguous",
        "kind": "mapping",
        "id": ",".join(filter(None, mapping_ids_by_class[entity_class])),
        "name": entity_class,
        "message": (
            f"entityClass「{entity_class}」同时绑定多个 ObjectType "
            f"({', '.join(sorted(target_ids))})；后续重投影无法保持对象路由，"
            "请为不同对象类型使用不同 entityClass"
        ),
        "field": "entityClass",
        "targetIds": sorted(target_ids),
    } for entity_class, target_ids in sorted(targets_by_class.items())
      if len(target_ids) > 1]


def validate_trial_mapping_contract(snapshot: dict | None) -> list[dict]:
    """Allow a draft to enter trial once one object has an effective mapping.

    Trial is an isolated validation space, so it deliberately permits unmapped
    object/link types. Materialization will warn about skipped object types and
    still fail closed if the selected mapping cannot read data or produce valid
    instances. Publication continues to use the stricter release contract.
    """
    snap = complete_snapshot(snapshot)
    object_types = {
        str(item.get("id") or ""): item
        for item in snap["objectTypes"]
        if item.get("id")
    }
    ambiguous = _mapping_entity_class_target_errors(
        snap["mappings"], object_types)
    if ambiguous:
        return ambiguous
    for mapping in snap["mappings"]:
        target = _object_type_for_mapping(mapping, object_types)
        field_mapping = mapping.get("fieldMapping")
        if target is None or not mapping.get("curatedDatasetId") or not isinstance(field_mapping, dict):
            continue
        mapped_properties = {
            str(target_name)
            for source_name, target_name in field_mapping.items()
            if not str(source_name).startswith("__")
            and isinstance(target_name, str) and target_name
        }
        required_properties = {
            str(prop["name"])
            for prop in (target.get("properties") or [])
            if isinstance(prop, dict)
            and prop.get("name")
            and prop.get("source") != "computed"
            and not prop.get("computed")
        }
        primary_key = target.get("primaryKey")
        primary_property = next((
            prop for prop in (target.get("properties") or [])
            if isinstance(prop, dict)
            and primary_key
            and (prop.get("id") == primary_key or prop.get("name") == primary_key)
        ), None)
        if primary_property and primary_property.get("name"):
            required_properties.add(str(primary_property["name"]))
        if required_properties and required_properties.issubset(mapped_properties):
            return []
    return [{
        "code": "trial_object_mapping_required", "kind": "mapping",
        "id": "", "name": "",
        "message": "草稿至少需要一个已绑定数据集并完成全部存储属性映射的对象实体，才能进入试跑态",
        "field": "mapping",
    }]


def validate_release_mapping_contract(snapshot: dict | None) -> list[dict]:
    """Require a complete lake mapping before a trial may be published.

    Every object/link type needs a mapping and every persisted property must be
    covered. Computed properties are deliberately excluded because their value
    comes from ontology logic rather than a lake column.
    """
    snap = complete_snapshot(snapshot)
    errors: list[dict] = []
    object_types = {
        str(item.get("id") or ""): item
        for item in snap["objectTypes"]
        if item.get("id")
    }
    mappings_by_type: dict[str, list[dict]] = {}

    def label(item: dict) -> str:
        return str(item.get("displayName") or item.get("name") or item.get("id") or "")

    def stored_properties(item: dict) -> list[dict]:
        return [
            prop for prop in (item.get("properties") or [])
            if isinstance(prop, dict)
            and prop.get("name")
            and prop.get("source") != "computed"
            and not prop.get("computed")
        ]

    errors.extend(_mapping_entity_class_target_errors(
        snap["mappings"], object_types))
    for mapping in snap["mappings"]:
        mapping_id = str(mapping.get("id") or "")
        mapping_name = str(mapping.get("entityClass") or mapping_id)
        target = _object_type_for_mapping(mapping, object_types)
        if target is None:
            errors.append({
                "code": "mapping_object_type_not_found", "kind": "mapping",
                "id": mapping_id, "name": mapping_name,
                "message": f"Mapping「{mapping_name}」未绑定有效的 ObjectType",
                "field": "targetObjectTypeId",
            })
            continue

        target_id = str(target.get("id") or "")
        mappings_by_type.setdefault(target_id, []).append(mapping)
        if not mapping.get("curatedDatasetId"):
            errors.append({
                "code": "mapping_dataset_missing", "kind": "mapping",
                "id": mapping_id, "name": mapping_name,
                "message": f"Mapping「{mapping_name}」未绑定数据集",
                "field": "curatedDatasetId",
            })

        field_mapping = mapping.get("fieldMapping")
        if not isinstance(field_mapping, dict):
            errors.append({
                "code": "mapping_field_mapping_invalid", "kind": "mapping",
                "id": mapping_id, "name": mapping_name,
                "message": f"Mapping「{mapping_name}」的字段映射必须是对象",
                "field": "fieldMapping",
            })
            field_mapping = {}
        mapped_properties = {
            str(target_name)
            for source_name, target_name in field_mapping.items()
            if not str(source_name).startswith("__")
            and isinstance(target_name, str) and target_name
        }
        required = {str(prop["name"]) for prop in stored_properties(target)}
        primary_key = target.get("primaryKey")
        primary_property = next((
            prop for prop in (target.get("properties") or [])
            if isinstance(prop, dict)
            and primary_key
            and (prop.get("id") == primary_key or prop.get("name") == primary_key)
        ), None)
        if primary_property and primary_property.get("name"):
            required.add(str(primary_property["name"]))
        for property_name in sorted(required - mapped_properties):
            errors.append({
                "code": "mapping_property_missing", "kind": "mapping",
                "id": mapping_id, "name": mapping_name,
                "targetId": target_id, "targetName": label(target),
                "message": (
                    f"Mapping「{mapping_name}」未覆盖 ObjectType「{label(target)}」"
                    f"的存储属性「{property_name}」"
                ),
                "field": property_name,
            })

    for object_type_id, object_type in object_types.items():
        if not mappings_by_type.get(object_type_id):
            errors.append({
                "code": "object_type_mapping_required", "kind": "objectType",
                "id": object_type_id, "name": label(object_type),
                "message": (
                    f"ObjectType「{label(object_type)}」尚未与数据资产湖建立映射，"
                    "不能转为发布态"
                ),
                "field": "mapping",
            })
        if not object_type.get("primaryKey"):
            errors.append({
                "code": "object_type_primary_key_required", "kind": "objectType",
                "id": object_type_id, "name": label(object_type),
                "message": f"ObjectType「{label(object_type)}」未设置主键，不能转为发布态",
                "field": "primaryKey",
            })

    link_types = {
        str(item.get("id") or ""): item
        for item in snap["linkTypes"]
        if item.get("id")
    }
    mapped_link_type_ids: set[str] = set()
    for mapping in snap["linkMappings"]:
        mapping_id = str(mapping.get("id") or "")
        mapping_name = str(mapping.get("relationType") or mapping_id)
        link_type_id = str(mapping.get("linkTypeId") or "")
        link_type = link_types.get(link_type_id)
        if link_type is None:
            errors.append({
                "code": "link_mapping_type_not_found", "kind": "linkMapping",
                "id": mapping_id, "name": mapping_name,
                "message": f"LinkMapping「{mapping_name}」未绑定有效的 LinkType",
                "field": "linkTypeId",
            })
            continue
        mapped_link_type_ids.add(link_type_id)
        for field, text in (("srcDatasetId", "源端"), ("tgtDatasetId", "目标端"),
                            ("srcKey", "源端外键"), ("tgtKey", "目标端外键")):
            if not mapping.get(field):
                errors.append({
                    "code": "link_mapping_endpoint_missing", "kind": "linkMapping",
                    "id": mapping_id, "name": mapping_name,
                    "message": f"LinkMapping「{mapping_name}」缺少{text}配置",
                    "field": field,
                })

        source_type_id = str(link_type.get("sourceObjectTypeId") or "")
        target_type_id = str(link_type.get("targetObjectTypeId") or "")
        source_dataset_id = str(mapping.get("srcDatasetId") or "")
        target_dataset_id = str(mapping.get("tgtDatasetId") or "")
        if source_dataset_id and not any(
            str(item.get("curatedDatasetId") or "") == source_dataset_id
            for item in mappings_by_type.get(source_type_id, [])
        ):
            errors.append({
                "code": "link_mapping_source_object_mapping_missing",
                "kind": "linkMapping", "id": mapping_id, "name": mapping_name,
                "message": f"LinkMapping「{mapping_name}」的源端数据集没有对应的对象映射",
                "field": "srcDatasetId",
            })
        if target_dataset_id and not any(
            str(item.get("curatedDatasetId") or "") == target_dataset_id
            for item in mappings_by_type.get(target_type_id, [])
        ):
            errors.append({
                "code": "link_mapping_target_object_mapping_missing",
                "kind": "linkMapping", "id": mapping_id, "name": mapping_name,
                "message": f"LinkMapping「{mapping_name}」的目标端数据集没有对应的对象映射",
                "field": "tgtDatasetId",
            })

        field_mapping = mapping.get("fieldMapping")
        if not isinstance(field_mapping, dict):
            errors.append({
                "code": "link_mapping_field_mapping_invalid", "kind": "linkMapping",
                "id": mapping_id, "name": mapping_name,
                "message": f"LinkMapping「{mapping_name}」的字段映射必须是对象",
                "field": "fieldMapping",
            })
            field_mapping = {}
        mapped_properties = {
            str(property_name)
            for property_name, source_name in field_mapping.items()
            if not str(property_name).startswith("__")
            and isinstance(source_name, str) and source_name
        }
        required = {str(prop["name"]) for prop in stored_properties(link_type)}
        for property_name in sorted(required - mapped_properties):
            errors.append({
                "code": "link_mapping_property_missing",
                "kind": "linkMapping", "id": mapping_id, "name": mapping_name,
                "targetId": link_type_id, "targetName": label(link_type),
                "message": (
                    f"LinkMapping「{mapping_name}」未覆盖 LinkType「{label(link_type)}」"
                    f"的存储属性「{property_name}」"
                ),
                "field": property_name,
            })

    for link_type_id, link_type in link_types.items():
        if link_type_id not in mapped_link_type_ids:
            errors.append({
                "code": "link_type_mapping_required", "kind": "linkType",
                "id": link_type_id, "name": label(link_type),
                "message": (
                    f"LinkType「{label(link_type)}」尚未与数据资产湖建立映射，"
                    "不能转为发布态"
                ),
                "field": "mapping",
            })

    return errors


def _simulate_sentinels(snapshot: dict, objects: list[dict], links: list[dict]) -> list[dict]:
    """Evaluate isolated trial data with the production sentinel contracts.

    Every planned action runs through Action Engine's explicit preview-only
    branch.  That branch shares validation, mapping, recipient, webhook and
    object/link planning semantics with production while suppressing every
    ActionLog/Fact/Notification/network side effect.
    """
    by_type: dict[str, list[dict]] = {}
    for item in objects:
        by_type.setdefault(item["objectTypeId"], []).append(item)
    link_keys = {
        (item["linkTypeId"], item["sourceObjectId"], item["targetObjectId"])
        for item in links
    }
    try:
        models = snapshot_models(snapshot)
        action_models = {
            action.id: action for action in models["actions"]
        }
    except Exception:
        models = {
            "objectTypes": [], "linkTypes": [], "actions": [],
            "functions": [],
        }
        action_models = {}

    # Keep the deliberately non-executable parameter binding language and
    # action contract identical to the production evaluator.
    from app.ontologies.sentinels.evaluator import (
        _binding_instance,
        _configured_action_parameters,
        _holds,
        _match_key,
        _passes,
    )
    from app.ontologies.formal_modeling.action_engine import (
        execute_action,
        prepare_action_parameters,
    )
    from app.ontologies.formal_modeling.derived import (
        DerivedComputationError,
    )

    def add_error(bucket: list[str], message: str) -> None:
        if message not in bucket and len(bucket) < 20:
            bucket.append(message)

    def object_model(item: dict) -> SimpleNamespace:
        return SimpleNamespace(
            id=item["objectId"],
            object_type_id=item["objectTypeId"],
            properties=dict(item.get("properties") or {}),
            computed=dict(item.get("computed") or {}),
            ontology_release_id=None,
        )

    isolated_objects = [object_model(item) for item in objects]
    isolated_links = [
        SimpleNamespace(
            id=item["linkId"],
            link_type_id=item["linkTypeId"],
            source_object_id=item["sourceObjectId"],
            target_object_id=item["targetObjectId"],
            properties=dict(item.get("properties") or {}),
        )
        for item in links
    ]

    def derive_candidate(candidate, _object_type, extras: list) -> dict:
        candidates = [
            item for item in [*isolated_objects, *extras]
            if str(item.id) != str(candidate.id)
        ]
        candidates.append(candidate)
        materialized = [{
            "objectId": str(item.id),
            "objectTypeId": str(item.object_type_id),
            "properties": dict(item.properties or {}),
            "computed": dict(getattr(item, "computed", None) or {}),
        } for item in candidates]
        derived_errors = _compute_trial_derived(snapshot, materialized)
        if derived_errors:
            raise DerivedComputationError(
                "；".join(
                    str(item.get("message") or item)
                    for item in derived_errors[:8]))
        computed = next((
            item.get("computed") or {}
            for item in materialized
            if str(item["objectId"]) == str(candidate.id)
        ), {})
        return dict(computed)

    preview_context_base = {
        "isolated": True,
        "release_id": None,
        "ontology_version": "trial",
        "object_types": models["objectTypes"],
        "link_types": models["linkTypes"],
        "actions": models["actions"],
        "functions": models["functions"],
        "objects": isolated_objects,
        "links": isolated_links,
        "derive": derive_candidate,
    }

    results = []
    for sentinel in snapshot.get("sentinels") or []:
        bindings = sentinel.get("bindings") or []
        errors: list[str] = []
        activation = (
            "disabled" if not sentinel.get("enabled", True)
            else "muted" if sentinel.get("muted", False)
            else "active"
        )
        if not bindings:
            results.append({
                "id": sentinel.get("id"),
                "name": sentinel.get("displayName") or sentinel.get("name"),
                "matched": 0,
                "candidateCount": 0,
                "candidateCapReached": False,
                "parameterErrorCount": 0,
                "errors": ["哨兵至少需要一个对象绑定"],
                "plannedActions": 0,
                "plannedActionSamples": [],
                "plannedActionsTruncated": False,
                "sideEffects": "none",
            })
            continue

        filtered_by_alias: dict[str, list[SimpleNamespace]] = {}
        for binding in bindings:
            alias = str(binding.get("alias") or "")
            filtered: list[SimpleNamespace] = []
            for obj in by_type.get(str(binding.get("objectTypeId")), []):
                model = object_model(obj)
                values = {
                    **dict(model.properties or {}),
                    **dict(model.computed or {}),
                }
                evaluation_errors: list[str] = []
                if _passes(
                        binding.get("filter"), alias, values,
                        evaluation_errors):
                    filtered.append(model)
                for error in evaluation_errors:
                    add_error(errors, error)
            filtered_by_alias[alias] = filtered

        sentinel_links = sentinel.get("links") or []

        def joined_links_hold(tup: dict[str, SimpleNamespace]) -> bool:
            for link in sentinel_links:
                from_alias = str(link.get("from") or "")
                to_alias = str(link.get("to") or "")
                if from_alias not in tup or to_alias not in tup:
                    continue
                if (
                    str(link.get("linkTypeId") or ""),
                    tup[from_alias].id,
                    tup[to_alias].id,
                ) not in link_keys:
                    return False
            return True

        # Production expands one binding at a time, uses available links to
        # constrain candidates and validates every link whose endpoints have
        # become bound.  The trial mirrors that semantic rather than validating
        # a differently ordered full Cartesian product.
        first_alias = str(bindings[0].get("alias") or "")
        tuples: list[dict[str, SimpleNamespace]] = [
            {first_alias: item}
            for item in filtered_by_alias.get(first_alias, [])
        ]
        cap_reached = len(tuples) > MAX_SENTINEL_TUPLES
        if cap_reached:
            tuples = tuples[:MAX_SENTINEL_TUPLES]
        for binding in bindings[1:]:
            if cap_reached:
                break
            alias = str(binding.get("alias") or "")
            expanded: list[dict[str, SimpleNamespace]] = []
            for tup in tuples:
                for candidate in filtered_by_alias.get(alias, []):
                    joined = {**tup, alias: candidate}
                    if not joined_links_hold(joined):
                        continue
                    expanded.append(joined)
                    if len(expanded) > MAX_SENTINEL_TUPLES:
                        cap_reached = True
                        break
                if cap_reached:
                    break
            tuples = expanded[:MAX_SENTINEL_TUPLES]
        if cap_reached:
            add_error(
                errors,
                f"跨对象候选组合超过安全上限 {MAX_SENTINEL_TUPLES}，"
                "请收窄绑定过滤条件后重试",
            )

        matched_tuples: list[dict[str, SimpleNamespace]] = []
        for tup in tuples:
            evaluation_errors = []
            if _holds(sentinel.get("condition"), tup, evaluation_errors):
                matched_tuples.append(tup)
            for error in evaluation_errors:
                add_error(errors, error)

        primary = str(sentinel.get("primaryAlias") or "") or first_alias
        sentinel_model = SimpleNamespace(
            action_parameters=sentinel.get("actionParameters", {}),
        )
        action_ids = [str(item) for item in (sentinel.get("actionIds") or [])]
        parameter_error_count = 0
        planned_samples: list[dict] = []
        total_actions = 0
        for tup in matched_tuples:
            target = _binding_instance(tup, primary, primary)
            match_ids = {alias: instance.id for alias, instance in tup.items()}
            event = {
                # Trial parameter resolution uses the same real edge vocabulary
                # as runtime.  A synthetic "preview" value made otherwise valid
                # enum-constrained action parameters fail only in trial.
                "edge": "enter",
                "matchKey": _match_key(tup, primary),
                "occurredAt": datetime.now(timezone.utc).isoformat(),
                "sentinelId": sentinel.get("id"),
                "sentinelName": (
                    sentinel.get("displayName") or sentinel.get("name")),
            }
            for action_id in action_ids:
                action = action_models.get(action_id)
                edges = ["enter"]
                if sentinel.get("triggerMode") == "on_enter_leave":
                    edges.append("leave")
                for edge in edges:
                    edge_event = {**event, "edge": edge}
                    parameters, binding_errors = (
                        _configured_action_parameters(
                            sentinel_model, action_id, tup, primary,
                            action=action, event=edge_event)
                    )
                    if action is None:
                        binding_errors.append(
                            f"动作不存在: {action_id}")
                    elif action.object_type_id and (
                        target is None
                        or target.object_type_id != action.object_type_id
                    ):
                        binding_errors.append(
                            f"动作 {action.display_name or action.name} "
                            "的目标类型与命中对象不一致")
                    if action is not None:
                        parameters, parameter_errors = (
                            prepare_action_parameters(action, parameters)
                        )
                        binding_errors.extend(parameter_errors)
                    parameter_error_count += len(binding_errors)
                    for error in binding_errors:
                        add_error(
                            errors,
                            f"{edge} 参数: {error}"
                            if edge == "leave" else error)

                    preview = {
                        "status": "failed",
                        "effects": [],
                        "validationErrors": list(binding_errors),
                        "errorMessage": (
                            "; ".join(binding_errors)
                            if binding_errors else "动作不存在"),
                        "sideEffects": "none",
                    }
                    if action is not None:
                        body = SimpleNamespace(
                            action_id=action_id,
                            parameters=parameters,
                            target_instance_id=(
                                target.id if target else None),
                            dry_run=True,
                            target_snapshot=None,
                            idempotency_key=None,
                            sentinel_match_state_id=None,
                            sentinel_id=sentinel.get("id"),
                            preview_only=True,
                        )
                        preview = execute_action(
                            None,
                            "isolated-trial",
                            body,
                            preview_only=True,
                            preview_context={
                                **preview_context_base,
                                "action": action,
                            },
                        )
                        if preview.get("status") != "success":
                            preview_errors = [
                                *list(
                                    preview.get("validationErrors") or []),
                            ]
                            if (
                                preview.get("errorMessage")
                                and preview.get("errorMessage")
                                not in preview_errors
                            ):
                                preview_errors.append(
                                    str(preview["errorMessage"]))
                            for error in preview_errors:
                                add_error(
                                    errors,
                                    f"{edge} 动作: {error}"
                                    if edge == "leave" else str(error))
                    total_actions += 1
                    if len(planned_samples) < 200:
                        planned_samples.append({
                            "actionId": action_id,
                            "actionName": (
                                action.display_name or action.name
                                if action is not None else action_id
                            ),
                            "edge": edge,
                            "targetInstanceId": (
                                target.id if target else None),
                            "match": match_ids,
                            "parameters": parameters,
                            "status": preview.get("status"),
                            "effects": preview.get("effects") or [],
                            "validationErrors": [
                                *binding_errors,
                                *[
                                    item for item in (
                                        preview.get(
                                            "validationErrors") or [])
                                    if item not in binding_errors
                                ],
                            ],
                            "errorMessage": preview.get(
                                "errorMessage"),
                            "sideEffects": "none",
                        })
        matched = len(matched_tuples)
        results.append({
            "id": sentinel.get("id"),
            "name": sentinel.get("displayName") or sentinel.get("name"),
            "activation": activation,
            "matched": matched,
            "candidateCount": len(tuples),
            "candidateCapReached": cap_reached,
            "parameterErrorCount": parameter_error_count,
            "errors": errors,
            # 动作只展示计划，绝不在试跑执行外部副作用。
            "plannedActions": total_actions,
            "plannedActionSamples": planned_samples,
            "plannedActionsTruncated": total_actions > len(planned_samples),
            "sideEffects": "none",
        })
    return results


def _compute_trial_derived(snapshot: dict, objects: list[dict]) -> list[dict]:
    """Compute expression-derived values in the isolated trial projection.

    Production Formal projection computes these values before sentinels inspect
    an instance.  The version trial must expose the same merged property view.
    TypeScript/client-side functions remain unavailable on the backend exactly
    as they do in production.
    """
    object_types = {
        str(item.get("id") or ""): item
        for item in (snapshot.get("objectTypes") or [])
    }
    functions = {}
    for item in snapshot.get("functions") or []:
        function_id = str(item.get("id") or "")
        functions[function_id] = SimpleNamespace(
            id=function_id,
            name=item.get("name"),
            display_name=(
                item.get("displayName") or item.get("display_name")
            ),
            function_type=(
                item.get("functionType")
                or item.get("function_type")
                or "object"
            ),
            language=item.get("language") or "expression",
            target_object_type_id=(
                item.get("targetObjectTypeId")
                or item.get("target_object_type_id")
            ),
            body=item.get("body") or "",
            enabled=item.get("enabled", True),
        )
    errors: list[dict] = []
    all_values = [
        dict(item.get("properties") or {}) for item in objects
    ]
    values_by_type: dict[str, list[dict]] = {}
    for item, values in zip(objects, all_values):
        values_by_type.setdefault(str(item.get("objectTypeId") or ""), []).append(
            values)

    for item in objects:
        object_type = object_types.get(str(item.get("objectTypeId") or ""))
        if object_type is None:
            continue
        computed: dict = {}
        for prop in object_type.get("properties") or []:
            if not isinstance(prop, dict) or not (
                prop.get("source") == "computed" or prop.get("computed")
            ):
                continue
            property_name = str(prop.get("name") or "")
            property_label = str(
                prop.get("displayName")
                or prop.get("display_name")
                or property_name
                or "(未命名派生属性)"
            )
            if not property_name:
                errors.append({
                    "code": "derived_property_evaluation_failed",
                    "kind": "objectInstance",
                    "id": item.get("objectId") or "",
                    "name": property_label,
                    "field": "",
                    "message": "试跑对象类型存在未命名的派生属性，无法安全计算",
                })
                continue
            function_id = str(prop.get("functionId") or "")
            if not function_id:
                # Matches production: an unbound/client-maintained computed
                # property has no authoritative server value and is omitted.
                continue
            fn = functions.get(function_id)
            if fn is None:
                errors.append({
                    "code": "derived_property_evaluation_failed",
                    "kind": "objectInstance",
                    "id": item.get("objectId") or "",
                    "name": property_label,
                    "field": property_name,
                    "message": (
                        f"派生属性「{property_label}」引用的函数不存在: "
                        f"{function_id}"
                    ),
                })
                continue
            language = str(
                getattr(fn, "language", None) or "expression"
            ).strip().lower()
            # Matches production: an enabled non-expression function is not
            # authoritative on the server, so no trial projection is emitted.
            # Disabled bindings fail before this branch in both environments.
            if bool(getattr(fn, "enabled", True)) and language != "expression":
                continue
            target_type = str(
                getattr(fn, "target_object_type_id", None) or "")
            scope_objects = (
                values_by_type.get(target_type, [])
                if target_type else all_values
            )
            result = evaluate_function_contract(
                fn,
                obj_props=dict(item.get("properties") or {}),
                params={},
                object_loader=lambda values=scope_objects: values,
            )
            if not result.get("success"):
                errors.append({
                    "code": "derived_property_evaluation_failed",
                    "kind": "objectInstance",
                    "id": item.get("objectId") or "",
                    "name": property_label,
                    "field": property_name,
                    "message": (
                        f"派生属性「{property_label}」试算失败: "
                        f"{result.get('error') or '未知错误'}"
                    ),
                })
                continue
            value = result.get("result")
            type_issue = property_value_type_issue(prop, value)
            if type_issue is not None:
                message = (
                    f"派生属性「{property_label}」的类型定义"
                    f"「{prop.get('type')}」非法"
                    if type_issue["code"]
                    == "invalid_property_type_definition"
                    else (
                        f"派生属性「{property_label}」试算结果类型不匹配："
                        f"期望 {type_issue['expected']}，"
                        f"实际为 {type_issue['actual']}"
                    )
                )
                errors.append({
                    "code": type_issue["code"],
                    "kind": "objectInstance",
                    "id": item.get("objectId") or "",
                    "name": property_label,
                    "field": property_name,
                    "message": message,
                })
                continue
            computed[property_name] = value
        item["computed"] = computed
    return errors


def materialize_trial(db: Session, run: OntologyTrialRun, snapshot: dict) -> dict:
    """固定 latest 湖版本并构建隔离对象/关系；任何错误都 fail-closed。"""
    snap = complete_snapshot(snapshot)
    errors = validate_snapshot(snap)
    warnings: list[dict] = []
    object_types = {str(item.get("id")): item for item in snap["objectTypes"]}
    mapped_types: set[str] = set()
    objects: list[dict] = []
    links: list[dict] = []
    pinned: dict[str, dict] = {}
    # 一个资产可以同时映射到多个对象类型。端点行必须按
    # (dataset, object type) 精确索引，不能让后出现的映射覆盖前者。
    mapping_rows: dict[tuple[str, str], dict[str, Any]] = {}
    object_ids: set[str] = set()
    object_entity_ids: dict[str, str] = {}

    for raw_mapping in snap["mappings"]:
        mapping_id = str(raw_mapping.get("id") or "")
        dataset_id = str(raw_mapping.get("curatedDatasetId") or "")
        target = _object_type_for_mapping(raw_mapping, object_types)
        if target is None:
            errors.append({
                "code": "mapping_object_type_not_found", "kind": "mapping",
                "id": mapping_id, "name": raw_mapping.get("entityClass") or "",
                "message": "Mapping 绑定的 ObjectType 不存在",
            })
            continue
        if not dataset_id:
            errors.append({
                "code": "mapping_dataset_missing", "kind": "mapping",
                "id": mapping_id, "name": raw_mapping.get("entityClass") or "",
                "message": "Mapping 未绑定数据集",
            })
            continue
        try:
            dataset, version = _latest_version(db, dataset_id)
            mapping = OntologyMapping(
                id=mapping_id or str(uuid.uuid4()), ontology_id=run.ontology_id,
                curated_dataset_id=dataset_id,
                entity_class=str(raw_mapping.get("entityClass") or target.get("name") or ""),
                target_object_type_id=str(target.get("id")),
                field_mapping=json_safe(raw_mapping.get("fieldMapping") or {}),
                status=str(raw_mapping.get("status") or "draft"),
                confidence=raw_mapping.get("confidence"),
            )
            rows, loaded_version = load_mapping_source_rows(db, mapping, require_approved=True)
            if loaded_version is None or loaded_version.id != version.id:
                raise MappingSourceError("读取的数据版本与 latest 指针不一致")
        except Exception as exc:
            errors.append({
                "code": "mapping_source_unavailable", "kind": "mapping",
                "id": mapping_id, "name": raw_mapping.get("entityClass") or "",
                "message": str(exc),
            })
            continue

        pinned[dataset_id] = {
            "datasetId": dataset_id, "datasetName": dataset.name,
            "versionId": version.id, "versionNo": version.version_no,
            "checksum": version.checksum, "rowCount": len(rows),
        }
        field_map = raw_mapping.get("fieldMapping") or {}
        pk = field_map.get("__primary_key__") or (dataset.schema_json or {}).get("primary_key")
        if not _pk_columns(pk):
            errors.append({
                "code": "mapping_primary_key_missing", "kind": "mapping",
                "id": mapping_id, "name": raw_mapping.get("entityClass") or "",
                "message": "数据集没有稳定主键，无法证明对象身份",
            })
            continue
        mapped_types.add(str(target.get("id")))
        rows_with_ids: list[tuple[dict, str]] = []
        for index, row in enumerate(rows):
            identity = _row_identity(row, str(pk))
            if identity is None:
                errors.append({
                    "code": "mapping_primary_key_value_missing", "kind": "objectInstance",
                    "id": f"{mapping_id}:{index}", "name": raw_mapping.get("entityClass") or "",
                    "message": "数据行主键为空，无法生成稳定对象身份",
                })
                continue
            entity_class = str(
                raw_mapping.get("entityClass") or target.get("name") or "")
            entity_id = stable_pipeline_entity_id(
                run.ontology_id, entity_class, identity)
            object_id = stable_object_instance_id(run.ontology_id, entity_id)
            if object_id in object_ids:
                errors.append({
                    "code": "duplicate_primary_key", "kind": "objectInstance",
                    "id": object_id, "name": raw_mapping.get("entityClass") or "",
                    "message": "数据集主键重复，多个数据行会覆盖同一对象",
                })
                continue
            object_ids.add(object_id)
            properties = {
                str(target_name): row.get(source_name)
                for source_name, target_name in field_map.items()
                if not str(source_name).startswith("__") and source_name in row
            }
            # Formal projection performs the same coercion after MappingService
            # writes its intermediate Entity rows.  Trial data must therefore
            # validate and feed sentinels with those production values rather
            # than raw CSV strings.
            try:
                properties = _coerce_props_to_type(
                    properties, list(target.get("properties") or []))
            except ValueError as exc:
                errors.append({
                    "code": "mapping_property_coercion_failed",
                    "kind": "objectInstance", "id": object_id,
                    "name": raw_mapping.get("entityClass") or "",
                    "message": str(exc),
                })
            item = {
                "objectId": object_id, "objectTypeId": str(target.get("id")),
                "properties": properties, "sourceDatasetId": dataset_id,
                "sourceDatasetVersionId": version.id, "externalId": entity_id,
            }
            objects.append(item)
            object_entity_ids[object_id] = entity_id
            rows_with_ids.append((row, object_id))
        mapping_rows[(dataset_id, str(target.get("id")))] = {
            "rows": rows_with_ids,
            "primaryKey": pk,
        }

    for object_type in snap["objectTypes"]:
        if str(object_type.get("id")) not in mapped_types:
            warnings.append({
                "code": "object_type_unmapped", "kind": "objectType",
                "id": object_type.get("id"),
                "message": "该对象类型没有数据映射，试跑中不会产生实例",
            })

    link_types = {str(item.get("id")): item for item in snap["linkTypes"]}
    for link_mapping in snap["linkMappings"]:
        link_type_id = str(link_mapping.get("linkTypeId") or "")
        if link_type_id not in link_types:
            errors.append({
                "code": "link_mapping_type_not_found", "kind": "linkMapping",
                "id": link_mapping.get("id") or "", "name": link_mapping.get("relationType") or "",
                "message": "LinkMapping 绑定的 LinkType 不存在",
            })
            continue
        link_type = link_types[link_type_id]
        src_dataset = str(link_mapping.get("srcDatasetId") or "")
        tgt_dataset = str(link_mapping.get("tgtDatasetId") or "")
        src_type_id = str(link_type.get("sourceObjectTypeId") or "")
        tgt_type_id = str(link_type.get("targetObjectTypeId") or "")
        src_materialization = mapping_rows.get((src_dataset, src_type_id))
        tgt_materialization = mapping_rows.get((tgt_dataset, tgt_type_id))
        if src_materialization is None or tgt_materialization is None:
            errors.append({
                "code": "link_mapping_endpoint_mapping_missing",
                "kind": "linkMapping", "id": link_mapping.get("id") or "",
                "name": link_mapping.get("relationType") or "",
                "message": "关系两端必须分别存在与对象类型匹配的数据映射",
            })
            continue
        src_rows = src_materialization["rows"]
        tgt_rows = tgt_materialization["rows"]
        src_key = str(link_mapping.get("srcKey") or "")
        tgt_key = str(link_mapping.get("tgtKey") or "")
        pairs: list[tuple[str, str, dict, str]] = []
        edge_dataset = link_mapping.get("edgeDatasetId")
        if edge_dataset:
            try:
                dataset, version = _latest_version(db, str(edge_dataset))
                transient = OntologyMapping(
                    id=f"edge:{link_mapping.get('id')}", ontology_id=run.ontology_id,
                    curated_dataset_id=str(edge_dataset), entity_class="__edge__",
                    field_mapping={}, status="draft",
                )
                edge_rows, loaded = load_mapping_source_rows(
                    db, transient, require_approved=True)
                if loaded is None or loaded.id != version.id:
                    raise MappingSourceError("关系数据版本与 latest 指针不一致")
                pinned[str(edge_dataset)] = {
                    "datasetId": str(edge_dataset), "datasetName": dataset.name,
                    "versionId": version.id, "versionNo": version.version_no,
                    "checksum": version.checksum, "rowCount": len(edge_rows),
                }
                # srcKey/tgtKey 是连接表的外键列；端点侧必须按各自对象映射
                # 的主键建立索引，不能错误地把连接表列名套到端点数据集。
                src_index = {
                    str(row.get(_pk_columns(src_materialization["primaryKey"])[0])): oid
                    for row, oid in src_rows
                    if len(_pk_columns(src_materialization["primaryKey"])) == 1
                }
                tgt_index = {
                    str(row.get(_pk_columns(tgt_materialization["primaryKey"])[0])): oid
                    for row, oid in tgt_rows
                    if len(_pk_columns(tgt_materialization["primaryKey"])) == 1
                }
                if not src_index and src_rows:
                    raise MappingSourceError("连接表暂不支持复合主键源端点")
                if not tgt_index and tgt_rows:
                    raise MappingSourceError("连接表暂不支持复合主键目标端点")
                fmap = link_mapping.get("fieldMapping") or {}
                mapping_identity = MappingService(db)
                edge_pk = mapping_identity._choose_pk_col(edge_rows)
                for row in edge_rows:
                    source_id = src_index.get(str(row.get(src_key)))
                    target_id = tgt_index.get(str(row.get(tgt_key)))
                    if source_id and target_id:
                        props = {str(prop): row.get(column) for prop, column in fmap.items()
                                 if not str(prop).startswith("__") and column in row}
                        edge_key = mapping_identity._row_identity_value(
                            row, edge_pk)
                        pairs.append((source_id, target_id, props, edge_key))
            except Exception as exc:
                errors.append({
                    "code": "link_mapping_source_unavailable", "kind": "linkMapping",
                    "id": link_mapping.get("id") or "", "name": link_mapping.get("relationType") or "",
                    "message": str(exc),
                })
        else:
            target_index: dict[str, list[str]] = {}
            for row, oid in tgt_rows:
                target_index.setdefault(str(row.get(tgt_key)), []).append(oid)
            for row, source_id in src_rows:
                for target_id in target_index.get(str(row.get(src_key)), []):
                    pairs.append((source_id, target_id, {}, ""))
        relation_type = str(
            link_mapping.get("relationType") or link_type.get("name") or "")
        link_mapping_id = str(link_mapping.get("id") or "")
        for source_id, target_id, properties, edge_key in pairs:
            source_entity_id = object_entity_ids[source_id]
            target_entity_id = object_entity_ids[target_id]
            relation_source = (
                f"link_mapping:{link_mapping_id}:{edge_key}"
                if edge_key else f"link_mapping:{link_mapping_id}"
            )
            source_relation_id = stable_pipeline_relation_id(
                run.ontology_id,
                source_entity_id,
                target_entity_id,
                relation_type,
                relation_source,
            )
            link_id = stable_link_instance_id(
                run.ontology_id, link_type_id, source_id, target_id, edge_key)
            try:
                properties = _coerce_props_to_type(
                    properties, list(link_type.get("properties") or []))
            except ValueError as exc:
                errors.append({
                    "code": "link_mapping_property_coercion_failed",
                    "kind": "linkInstance", "id": link_id,
                    "name": relation_type,
                    "message": str(exc),
                })
                continue
            links.append({
                "linkId": link_id, "linkTypeId": link_type_id,
                "sourceObjectId": source_id, "targetObjectId": target_id,
                "properties": properties,
                "sourceRelationId": source_relation_id,
            })

    errors.extend(_compute_trial_derived(snap, objects))

    try:
        models = snapshot_models(snap)
        instance_models = [SimpleNamespace(
            id=item["objectId"], object_type_id=item["objectTypeId"],
            properties=item["properties"], computed=item.get("computed") or {},
        ) for item in objects]
        link_models = [SimpleNamespace(
            id=item["linkId"], link_type_id=item["linkTypeId"],
            source_object_id=item["sourceObjectId"],
            target_object_id=item["targetObjectId"], properties=item["properties"],
        ) for item in links]
        errors.extend(validate_model(
            models["objectTypes"], models["linkTypes"], models["actions"],
            models["functions"], instance_models, link_models,
        ))
    except Exception as exc:
        errors.append({
            "code": "trial_contract_validation_failed", "kind": "ontology",
            "id": "", "name": "", "message": str(exc),
        })

    sentinel_results = _simulate_sentinels(snap, objects, links)
    for item in sentinel_results:
        for error in item.get("errors") or []:
            errors.append({
                "code": "sentinel_trial_error", "kind": "sentinel",
                "id": item.get("id") or "", "name": item.get("name") or "",
                "message": error,
            })

    run.dataset_versions = list(pinned.values())
    if settings.environment == "production":
        errors.extend(validate_manual_mapping_trial_contract(
            db, snap, run.dataset_versions,
        ))

    # 仅通过的试跑保留可晋级的完整投影；失败试跑保留摘要和样例，避免把大量
    # 无效数据误认为可发布候选。
    if not errors:
        for item in objects:
            db.add(OntologyTrialObject(
                trial_run_id=run.id, object_id=item["objectId"],
                object_type_id=item["objectTypeId"],
                properties=item["properties"],
                computed=item.get("computed") or {},
                source_dataset_id=item["sourceDatasetId"],
                source_dataset_version_id=item["sourceDatasetVersionId"],
                external_id=item["externalId"],
            ))
        for item in links:
            db.add(OntologyTrialLink(
                trial_run_id=run.id, link_id=item["linkId"],
                link_type_id=item["linkTypeId"],
                source_object_id=item["sourceObjectId"],
                target_object_id=item["targetObjectId"],
                properties=item["properties"],
                source_relation_id=item["sourceRelationId"],
            ))

    result = {
        "counts": {
            "objects": len(objects), "links": len(links),
            "facts": sum(len(item["properties"]) for item in objects),
            "datasets": len(pinned),
        },
        "errors": errors, "warnings": warnings,
        "sentinels": sentinel_results,
        "samples": {"objects": objects[:10], "links": links[:10]},
        "actionsExecuted": 0,
        "sideEffects": "blocked",
    }
    run.result_json = json_safe(result)
    run.status = "passed" if not errors else "failed"
    run.completed_at = datetime.now(timezone.utc)
    return result
