"""Export and import the current formal ontology structure as portable JSON.

Instances, execution history, mappings, and runtime state are intentionally not
part of this contract.  Imported packages receive fresh database identifiers and
become a sealed v0 baseline release.
"""
from __future__ import annotations

import copy
import json
import math
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.inference import AuditLog
from app.models.ontology import OntologyProject
from app.models.ontology_formal import (
    ActionType,
    LinkType,
    ObjectType,
    OntologyFunction,
)
from app.models.ontology_version import OntologyVersion
from app.ontologies.export.schemas import (
    OntologyStructurePackage,
    PortableActionType,
    PortableFunction,
    PortableLinkType,
    PortableObjectType,
    PortableOntologyMetadata,
    PortableOntologyStructure,
)
from app.ontologies.formal_modeling import schemas as formal_schemas
from app.ontologies.formal_modeling.validation import validate_model
from app.ontologies.versions.release_service import resolve_current_release
from app.ontologies.versions.snapshot_contract import (
    snapshot_hash,
)
from app.ontologies.versions.workspace_service import _canvas_node_ids
from app.settings.domains.service import (
    LOCAL_IMPORT_DESCRIPTION,
    ensure_domain,
)


def _new_id() -> str:
    return str(uuid.uuid4())


def _without_external_lineage(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_external_lineage(child)
            for key, child in value.items()
            if key != "dataBinding"
        }
    if isinstance(value, list):
        return [_without_external_lineage(child) for child in value]
    return value


def _portable_object_type(item: ObjectType) -> PortableObjectType:
    data = _without_external_lineage(
        formal_schemas.ObjectTypeOut.model_validate(item).model_dump())
    return PortableObjectType.model_validate({
        key: value for key, value in data.items()
        if key not in {"created_at", "updated_at", "source"}
    })


def _portable_link_type(item: LinkType) -> PortableLinkType:
    data = _without_external_lineage(
        formal_schemas.LinkTypeOut.model_validate(item).model_dump())
    return PortableLinkType.model_validate({
        key: value for key, value in data.items()
        if key not in {"created_at", "updated_at", "source"}
    })


def _portable_action(item: ActionType) -> PortableActionType:
    data = _without_external_lineage(
        formal_schemas.ActionTypeOut.model_validate(item).model_dump())
    return PortableActionType.model_validate({
        key: value for key, value in data.items()
        if key not in {"created_at", "updated_at", "source"}
    })


def _portable_function(item: OntologyFunction) -> PortableFunction:
    data = _without_external_lineage(
        formal_schemas.FunctionOut.model_validate(item).model_dump())
    return PortableFunction.model_validate({
        key: value for key, value in data.items()
        if key not in {"created_at", "updated_at", "source"}
    })


# 画布坐标系以像素计；超出该幅度的坐标必然来自损坏或恶意数据，
# 落库后会让前端 fitView 视口计算失效（整图被缩到不可见）。
_CANVAS_POSITION_MAX = 10_000_000.0


def _finite_position(value: Any) -> dict[str, float] | None:
    """坐标必须是有限数字对；呈现层状态不合法时丢弃而非拒绝导入。"""
    if not isinstance(value, dict):
        return None
    x, y = value.get("x"), value.get("y")
    # bool 是 int 子类，会被 float() 静默转成 0/1——与 save_canvas_layout
    # 的既有语义一致，明确拒绝。
    if isinstance(x, bool) or isinstance(y, bool):
        return None
    try:
        x_value, y_value = float(x), float(y)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(x_value) and math.isfinite(y_value)):
        return None
    if abs(x_value) > _CANVAS_POSITION_MAX or abs(y_value) > _CANVAS_POSITION_MAX:
        return None
    return {"x": x_value, "y": y_value}


def _exportable_canvas_layout(db: Session, project: OntologyProject) -> dict[str, dict[str, float]] | None:
    """导出当前发布版本的画布布局。

    数据映射画布的 key（object:/relation:/dataset: 前缀）依赖 mappings，
    而结构导出包有意不含 mappings，这些位置在新本体中没有落点，不导出。
    """
    release = resolve_current_release(db, project)
    layout = getattr(release, "canvas_layout", None)
    if not isinstance(layout, dict):
        return None
    portable: dict[str, dict[str, float]] = {}
    for raw_id, raw_position in layout.items():
        node_id = str(raw_id)
        if node_id.startswith(("object:", "relation:", "dataset:")):
            continue
        position = _finite_position(raw_position)
        if position is not None:
            # 亚像素精度对画布无意义；取整同时稳定反复导出导入的坐标，
            # 并显著缩小大本体的导出文件体积（布局条目可达数万条）。
            portable[node_id] = {"x": float(round(position["x"])), "y": float(round(position["y"]))}
    return portable or None


def _remap_canvas_layout(
    raw: Any,
    *,
    object_ids: dict[str, str],
    action_ids: dict[str, str],
) -> dict[str, dict[str, float]]:
    """把包内布局 key 引用的旧结构 ID 改写为导入后的新 ID。

    布局 key 形态：``<objId>``（全屏编辑器）、``l1:<objId>``、``l2:<objId>``、
    ``[l2:]property:<objId>:<propKey>``、``[l2:]action:<actionId>``。属性
    内部 ID 不参与导入重映射，原样保留。引用未知 ID 或坐标非法的条目
    静默剔除，不阻塞结构导入。
    """
    if not isinstance(raw, dict):
        return {}
    remapped: dict[str, dict[str, float]] = {}

    def emit(node_id: str, position: Any) -> None:
        value = _finite_position(position)
        if value is not None:
            remapped[node_id] = value

    for raw_id, raw_position in raw.items():
        parts = str(raw_id).split(":")
        if len(parts) == 1:
            new_id = object_ids.get(parts[0])
            if new_id:
                emit(new_id, raw_position)
            continue
        prefix = parts[0]
        if prefix == "property" and len(parts) == 3:
            new_object = object_ids.get(parts[1])
            if new_object:
                emit(f"property:{new_object}:{parts[2]}", raw_position)
        elif prefix == "action" and len(parts) == 2:
            new_action = action_ids.get(parts[1])
            if new_action:
                emit(f"action:{new_action}", raw_position)
        elif prefix in ("l1", "l2") and len(parts) == 2:
            new_object = object_ids.get(parts[1])
            if new_object:
                emit(f"{prefix}:{new_object}", raw_position)
        elif prefix == "l2" and len(parts) == 4 and parts[1] == "property":
            new_object = object_ids.get(parts[2])
            if new_object:
                emit(f"l2:property:{new_object}:{parts[3]}", raw_position)
        elif prefix == "l2" and len(parts) == 3 and parts[1] == "action":
            new_action = action_ids.get(parts[2])
            if new_action:
                emit(f"l2:action:{new_action}", raw_position)
        # 其余形态（object:/relation:/dataset: 等映射画布 key）丢弃
    return remapped


def build_export_package(db: Session, project: OntologyProject) -> OntologyStructurePackage:
    """Build a stable structure-only package from the current formal model."""

    def items(model):
        return db.query(model).filter(model.ontology_id == project.id).all()

    return OntologyStructurePackage(
        exported_at=datetime.now(timezone.utc),
        ontology=PortableOntologyMetadata(
            id=project.id,
            name=project.name,
            domain=project.domain,
            description=project.description,
            icon=project.icon or "network",
            source_version=project.version,
            source_status=project.status,
        ),
        structure=PortableOntologyStructure(
            object_types=[_portable_object_type(item) for item in items(ObjectType)],
            link_types=[_portable_link_type(item) for item in items(LinkType)],
            actions=[_portable_action(item) for item in items(ActionType)],
            functions=[_portable_function(item) for item in items(OntologyFunction)],
        ),
        canvas_layout=_exportable_canvas_layout(db, project),
    )


def export_json(db: Session, project: OntologyProject) -> str:
    package = build_export_package(db, project)
    return json.dumps(package.model_dump(mode="json", by_alias=True), ensure_ascii=False, indent=2)


def _validate_unique_ids(kind: str, items: list[Any], errors: list[dict]) -> None:
    seen: set[str] = set()
    for item in items:
        if item.id in seen:
            errors.append({
                "code": "duplicate_id",
                "kind": kind,
                "id": item.id,
                "message": f"{kind} 中存在重复 ID: {item.id}",
            })
        seen.add(item.id)


def _validate_reference(
    value: Any,
    valid_ids: set[str],
    *,
    kind: str,
    item_id: str,
    field: str,
    errors: list[dict],
) -> None:
    if value in (None, ""):
        return
    if not isinstance(value, str) or value not in valid_ids:
        errors.append({
            "code": "dangling_reference",
            "kind": kind,
            "id": item_id,
            "field": field,
            "message": f"{kind} {item_id} 的 {field} 引用了不存在的结构 ID: {value}",
        })


def _validate_nested_references(
    value: Any,
    *,
    kind: str,
    item_id: str,
    path: str,
    object_ids: set[str],
    link_ids: set[str],
    action_ids: set[str],
    function_ids: set[str],
    errors: list[dict],
) -> None:
    reference_sets = {
        "objectTypeId": object_ids,
        "targetObjectTypeId": object_ids,
        "sourceObjectTypeId": object_ids,
        "linkTypeId": link_ids,
        "actionId": action_ids,
        "targetActionId": action_ids,
        "functionId": function_ids,
        "validationFunctionId": function_ids,
    }
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            if key in reference_sets:
                _validate_reference(
                    child,
                    reference_sets[key],
                    kind=kind,
                    item_id=item_id,
                    field=child_path,
                    errors=errors,
                )
            elif key == "referenceType":
                _validate_reference(
                    child,
                    object_ids,
                    kind=kind,
                    item_id=item_id,
                    field=child_path,
                    errors=errors,
                )
            _validate_nested_references(
                child,
                kind=kind,
                item_id=item_id,
                path=child_path,
                object_ids=object_ids,
                link_ids=link_ids,
                action_ids=action_ids,
                function_ids=function_ids,
                errors=errors,
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_nested_references(
                child,
                kind=kind,
                item_id=item_id,
                path=f"{path}[{index}]",
                object_ids=object_ids,
                link_ids=link_ids,
                action_ids=action_ids,
                function_ids=function_ids,
                errors=errors,
            )


def _validate_import_package(package: OntologyStructurePackage) -> None:
    structure = package.structure
    errors = validate_model(
        structure.object_types,
        structure.link_types,
        structure.actions,
        structure.functions,
        [],
        [],
    )
    if not structure.object_types:
        errors.append({
            "code": "object_type_required",
            "kind": "ontology",
            "id": "",
            "message": "导入并发布的本体至少需要一个对象类型",
        })

    _validate_unique_ids("objectType", structure.object_types, errors)
    _validate_unique_ids("linkType", structure.link_types, errors)
    _validate_unique_ids("action", structure.actions, errors)
    _validate_unique_ids("function", structure.functions, errors)

    object_ids = {item.id for item in structure.object_types}
    link_ids = {item.id for item in structure.link_types}
    action_ids = {item.id for item in structure.actions}
    function_ids = {item.id for item in structure.functions}

    for function in structure.functions:
        if function.enabled and function.language.strip().lower() == "typescript":
            errors.append({
                "code": "enabled_typescript_function_forbidden",
                "kind": "function",
                "id": function.id,
                "field": "language",
                "message": f"启用的 TypeScript 函数「{function.display_name}」不能进入发布版本",
            })

    for kind, items in (
        ("objectType", structure.object_types),
        ("linkType", structure.link_types),
        ("action", structure.actions),
        ("function", structure.functions),
    ):
        for item in items:
            _validate_nested_references(
                item.model_dump(mode="json", by_alias=True),
                kind=kind,
                item_id=item.id,
                path="",
                object_ids=object_ids,
                link_ids=link_ids,
                action_ids=action_ids,
                function_ids=function_ids,
                errors=errors,
            )

    if errors:
        raise HTTPException(status_code=422, detail={
            "code": "ontology_import_validation_failed",
            "message": f"导入文件中的本体结构未通过校验（{len(errors)} 个错误）",
            "errors": errors,
        })


def _unique_project_name(db: Session, requested: str) -> str:
    base = requested.strip()
    candidate = base
    index = 0
    while db.query(OntologyProject.id).filter(OntologyProject.name.ilike(candidate)).first():
        index += 1
        suffix = "（导入）" if index == 1 else f"（导入 {index}）"
        candidate = f"{base[:max(1, 200 - len(suffix))]}{suffix}"
    return candidate


def _remap_nested(
    value: Any,
    *,
    object_ids: dict[str, str],
    link_ids: dict[str, str],
    action_ids: dict[str, str],
    function_ids: dict[str, str],
) -> Any:
    mappings = {
        "objectTypeId": object_ids,
        "object_type_id": object_ids,
        "targetObjectTypeId": object_ids,
        "target_object_type_id": object_ids,
        "sourceObjectTypeId": object_ids,
        "source_object_type_id": object_ids,
        "linkTypeId": link_ids,
        "link_type_id": link_ids,
        "actionId": action_ids,
        "action_id": action_ids,
        "targetActionId": action_ids,
        "target_action_id": action_ids,
        "functionId": function_ids,
        "function_id": function_ids,
        "validationFunctionId": function_ids,
        "validation_function_id": function_ids,
    }
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            mapping = mappings.get(key)
            if mapping and isinstance(child, str) and child:
                result[key] = mapping[child]
            elif key in {"referenceType", "reference_type"} and isinstance(child, str) and child in object_ids:
                result[key] = object_ids[child]
            elif key == "dataBinding":
                # Dataset/mapping lineage belongs to the source environment and
                # is intentionally outside the structure-only package.
                continue
            else:
                result[key] = _remap_nested(
                    child,
                    object_ids=object_ids,
                    link_ids=link_ids,
                    action_ids=action_ids,
                    function_ids=function_ids,
                )
        return result
    if isinstance(value, list):
        return [
            _remap_nested(
                child,
                object_ids=object_ids,
                link_ids=link_ids,
                action_ids=action_ids,
                function_ids=function_ids,
            )
            for child in value
        ]
    return value


def _snapshot_formal(db: Session, ontology_id: str) -> dict:
    def items(model):
        return db.query(model).filter(model.ontology_id == ontology_id).all()

    return {
        "objectTypes": [
            formal_schemas.ObjectTypeOut.model_validate(item).model_dump(mode="json", by_alias=True)
            for item in items(ObjectType)
        ],
        "linkTypes": [
            formal_schemas.LinkTypeOut.model_validate(item).model_dump(mode="json", by_alias=True)
            for item in items(LinkType)
        ],
        "actions": [
            formal_schemas.ActionTypeOut.model_validate(item).model_dump(mode="json", by_alias=True)
            for item in items(ActionType)
        ],
        "functions": [
            formal_schemas.FunctionOut.model_validate(item).model_dump(mode="json", by_alias=True)
            for item in items(OntologyFunction)
        ],
        "sentinels": [],
        "mappings": [],
        "linkMappings": [],
    }


def import_structure_package(
    db: Session,
    package: OntologyStructurePackage,
    *,
    current_user: Any,
) -> dict:
    """Import a validated package and atomically create its published v0 baseline."""

    _validate_import_package(package)
    ensure_domain(
        db,
        name=package.ontology.domain,
        description=LOCAL_IMPORT_DESCRIPTION,
        created_by=current_user.id,
    )
    structure = package.structure
    ontology_id = _new_id()
    project_name = _unique_project_name(db, package.ontology.name)
    lineage = {
        "kind": "local_import",
        "sourceOntologyId": package.ontology.id,
        "sourceVersion": package.ontology.source_version,
        "exportedAt": package.exported_at.isoformat(),
    }

    object_ids = {item.id: _new_id() for item in structure.object_types}
    link_ids = {item.id: _new_id() for item in structure.link_types}
    action_ids = {item.id: _new_id() for item in structure.actions}
    function_ids = {item.id: _new_id() for item in structure.functions}

    def remapped_data(item: Any) -> dict:
        raw = item.model_dump(exclude={"id"}, exclude_none=False)
        return _remap_nested(
            copy.deepcopy(raw),
            object_ids=object_ids,
            link_ids=link_ids,
            action_ids=action_ids,
            function_ids=function_ids,
        )

    try:
        project = OntologyProject(
            id=ontology_id,
            name=project_name,
            domain=package.ontology.domain,
            description=package.ontology.description,
            icon=package.ontology.icon or "network",
            version="v0",
            status="published",
            build_mode="manual",
            created_by=current_user.id,
        )
        db.add(project)
        db.flush()

        for item in structure.object_types:
            db.add(ObjectType(
                id=object_ids[item.id],
                ontology_id=ontology_id,
                source=lineage,
                **remapped_data(item),
            ))
        for item in structure.functions:
            db.add(OntologyFunction(
                id=function_ids[item.id],
                ontology_id=ontology_id,
                source=lineage,
                **remapped_data(item),
            ))
        for item in structure.actions:
            db.add(ActionType(
                id=action_ids[item.id],
                ontology_id=ontology_id,
                source=lineage,
                **remapped_data(item),
            ))
        for item in structure.link_types:
            db.add(LinkType(
                id=link_ids[item.id],
                ontology_id=ontology_id,
                source=lineage,
                **remapped_data(item),
            ))
        db.flush()

        formal_snapshot = _snapshot_formal(db, ontology_id)
        # 布局 key 引用的旧 ID 改写为新 ID 后，再按新快照的合法节点集合
        # 过滤一遍，保证写入的 canvas_layout 与导入结构自洽（悬挂条目剔除）。
        canvas_layout = {
            node_id: position
            for node_id, position in _remap_canvas_layout(
                package.canvas_layout,
                object_ids=object_ids,
                action_ids=action_ids,
            ).items()
            if node_id in _canvas_node_ids(formal_snapshot)
        }
        counts = {
            "objectTypes": len(structure.object_types),
            "linkTypes": len(structure.link_types),
            "actions": len(structure.actions),
            "functions": len(structure.functions),
        }
        total = sum(counts.values())
        formal_diff = {
            key: {"added": value, "modified": 0, "deleted": 0}
            for key, value in counts.items()
        }
        formal_diff.update({
            "sentinels": {"added": 0, "modified": 0, "deleted": 0},
            "mappings": {"added": 0, "modified": 0, "deleted": 0},
            "linkMappings": {"added": 0, "modified": 0, "deleted": 0},
            "total": {"added": total, "modified": 0, "deleted": 0},
        })

        version_id = _new_id()
        published_at = datetime.now(timezone.utc)
        version = OntologyVersion(
            id=version_id,
            ontology_id=ontology_id,
            version_number="v0",
            version_label="本地导入基线",
            description=(
                f"从本地 JSON 导入"
                f"（源版本 {package.ontology.source_version or '未知'}）"
            ),
            base_release_id=version_id,
            node_kind="release",
            lifecycle_status="released",
            revision=0,
            snapshot_entities=[],
            snapshot_relations=[],
            snapshot_logic=[],
            snapshot_actions=[],
            snapshot_formal=formal_snapshot,
            snapshot_hash=snapshot_hash(formal_snapshot),
            canvas_layout=canvas_layout or None,
            published_at=published_at,
            change_summary={
                "added": 0,
                "modified": 0,
                "deleted": 0,
                "formal": formal_diff,
            },
            created_by=current_user.id,
        )
        db.add(version)
        db.flush()
        project.current_release_id = version.id
        db.add(AuditLog(
            id=_new_id(),
            ontology_id=ontology_id,
            event_type="import",
            event_subtype="local_json_v0",
            user_id=current_user.id,
            user_name=getattr(current_user, "username", None),
            description=f"从本地 JSON 导入本体并创建发布版本 v0",
            object_type="ontology_version",
            object_id=version.id,
            meta={
                "version_number": "v0",
                "source_ontology_id": package.ontology.id,
                "source_version": package.ontology.source_version,
                "counts": counts,
            },
        ))
        db.commit()
        db.refresh(project)
    except Exception:
        db.rollback()
        raise

    return {
        "ontology": {
            "id": project.id,
            "name": project.name,
            "domain": project.domain,
            "description": project.description,
            "icon": project.icon,
            "version": project.version,
            "current_release_id": project.current_release_id,
            "current_release_version": version.version_number,
            "status": project.status,
            "created_by": project.created_by,
            "created_at": project.created_at.isoformat(),
            "updated_at": project.updated_at.isoformat(),
        },
        "version": {
            "id": version.id,
            "version_number": version.version_number,
            "version_label": version.version_label,
            "node_kind": version.node_kind,
            "lifecycle_status": version.lifecycle_status,
            "snapshot_hash": version.snapshot_hash,
            "published_at": version.published_at.isoformat(),
        },
        "counts": counts,
    }
