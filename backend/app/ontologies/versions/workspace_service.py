"""Version-tree and editable workspace application service."""

from __future__ import annotations

import json
import math
import uuid
from contextlib import contextmanager
from typing import Any

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import desc, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, defer

from app.config import settings
from app.ontologies import cache as ontology_cache
from app.ontologies.inference.models import AuditLog
from app.ontologies.projects.models import OntologyProject
from app.ontologies.versions import release_service
from app.ontologies.versions.snapshot_contract import (
    complete_snapshot,
    next_draft_number,
    snapshot_hash,
)
from app.ontologies.versions.evolution_service import (
    impact_report,
    validate_builtin_sentinel_contract,
    validate_snapshot,
    workspace_snapshot,
)
from app.ontologies.versions.models import (
    OntologyChangeLog,
    OntologyTrialLink,
    OntologyTrialObject,
    OntologyTrialRun,
    OntologyVersion,
)
from app.ontologies.versions.runtime_state_service import (
    _dynamic_sentinel_id_conflict_errors,
    _release_readiness,
)
from app.ontologies.versions.world_model_consumers import (
    affected_services as world_model_affected_services,
)


def _trial_payload(run: OntologyTrialRun) -> dict:
    return {
        "id": run.id, "version_id": run.version_id, "revision": run.revision,
        "snapshot_hash": run.snapshot_hash, "status": run.status,
        "base_release_id": run.base_release_id,
        "dataset_versions": run.dataset_versions or [],
        "result": run.result_json or {}, "impact_hash": run.impact_hash,
        "created_by": run.created_by,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "lease_expires_at": (
            run.lease_expires_at.isoformat()
            if run.status == "running" and run.lease_expires_at else None
        ),
    }


def _version_payload(version: OntologyVersion, latest_trial: OntologyTrialRun | None = None) -> dict:
    semantic = (
        version.snapshot_semantic
        if isinstance(version.snapshot_semantic, dict) else {}
    )
    semantic_revision = semantic.get("semanticRevision")
    return {
        "id": version.id,
        "version_number": version.version_number,
        "version_label": version.version_label,
        "description": version.description,
        "parent_version_id": version.parent_version_id,
        "base_release_id": version.base_release_id,
        "promoted_from_id": version.promoted_from_id,
        "node_kind": version.node_kind or "release",
        "lifecycle_status": version.lifecycle_status or (
            "released" if (version.node_kind or "release") == "release" else "editing"),
        "revision": version.revision or 0,
        "snapshot_hash": version.snapshot_hash,
        # 语义层只透出摘要标记，不透出整份 JSON（画布+文档体积大）。
        "hasSemanticLayer": bool(
            semantic.get("canvas") or semantic.get("documentMd")),
        "semanticRevision": (
            semantic_revision if isinstance(semantic_revision, int) else 0),
        "change_summary": version.change_summary or {},
        "created_by": version.created_by,
        "created_at": version.created_at.isoformat() if version.created_at else None,
        "published_at": version.published_at.isoformat() if version.published_at else None,
        "latest_trial": _trial_payload(latest_trial) if latest_trial else None,
    }


def _json_safe(value: Any) -> Any:
    """快照只保留 JSON 值；不把 ORM/时间对象渗入 JSON 列。"""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


# 版本树/版本列表只消费标量元数据与 change_summary/snapshot_semantic 摘要，
# 六个全量快照 JSON 列在加载时 defer，避免逐行解压大 TOAST 载荷。
_VERSION_LISTING_DEFERS = (
    defer(OntologyVersion.snapshot_entities),
    defer(OntologyVersion.snapshot_relations),
    defer(OntologyVersion.snapshot_logic),
    defer(OntologyVersion.snapshot_actions),
    defer(OntologyVersion.snapshot_formal),
    defer(OntologyVersion.canvas_layout),
)


def _latest_trials_by_version(
    db: Session,
    ontology_id: str,
    version_ids: list[str] | None = None,
) -> dict[str, OntologyTrialRun]:
    """每个版本只保留最近一次试跑。

    两段式加载：先用窄列 (id, version_id) 按时间倒序选出每个版本的最近
    试跑，再只对入选行做整行加载——历史试跑不再解压 result_json 大列。
    """
    query = db.query(
        OntologyTrialRun.id,
        OntologyTrialRun.version_id,
    ).filter(OntologyTrialRun.ontology_id == ontology_id)
    if version_ids is not None:
        query = query.filter(OntologyTrialRun.version_id.in_(version_ids))
    latest_ids: dict[str, str] = {}
    for run_id, version_id in query.order_by(
            desc(OntologyTrialRun.created_at)).all():
        latest_ids.setdefault(version_id, run_id)
    if not latest_ids:
        return {}
    runs = db.query(OntologyTrialRun).filter(
        OntologyTrialRun.id.in_(latest_ids.values())).all()
    return {run.version_id: run for run in runs}


def _with_canvas_layout(snapshot: dict | None, layout: dict | None) -> dict:
    """把独立画布布局投影到工作区 DTO，不改动被哈希的模型快照。"""
    out = complete_snapshot(snapshot)
    positions = layout if isinstance(layout, dict) else {}
    for object_type in out["objectTypes"]:
        position = positions.get(str(object_type.get("id")))
        if not isinstance(position, dict):
            continue
        if "x" in position and "y" in position:
            object_type["positionX"] = position["x"]
            object_type["positionY"] = position["y"]
    return out


def _canvas_node_ids(snapshot: dict | None) -> set[str]:
    """Return every stable node id accepted by the read-only structure canvas.

    Object type ids intentionally keep their historical, unprefixed form so the
    full-screen editor and the management detail page share the same positions.
    L2-only nodes use namespaced ids to avoid collisions across object
    properties and actions.  Functions and sentinels are analysis overlays, not
    persistent canvas nodes.

    The data-mapping workspace shares the same per-version layout store under
    namespaced node ids: ``object:<id>`` / ``relation:<id>`` for ontology
    elements and ``dataset:<id>`` for datasets referenced by the version's
    mappings.  Unmapped datasets are rejected (and pruned) because the mapping
    canvas only materializes nodes for mapped elements.
    """
    snap = complete_snapshot(snapshot)
    valid_ids: set[str] = set()
    for object_type in snap["objectTypes"]:
        object_type_id = str(object_type.get("id") or "")
        if not object_type_id:
            continue
        valid_ids.update({object_type_id, f"l1:{object_type_id}", f"l2:{object_type_id}"})
        valid_ids.add(f"object:{object_type_id}")
        for prop in object_type.get("properties") or []:
            if not isinstance(prop, dict):
                continue
            property_id = str(prop.get("id") or prop.get("name") or "")
            if property_id:
                node_id = f"property:{object_type_id}:{property_id}"
                valid_ids.update({node_id, f"l2:{node_id}"})
    for link_type in snap["linkTypes"]:
        link_type_id = str(link_type.get("id") or "")
        if link_type_id:
            valid_ids.add(f"relation:{link_type_id}")
    for action in snap["actions"]:
        action_id = str(action.get("id") or "")
        if action_id:
            node_id = f"action:{action_id}"
            valid_ids.update({node_id, f"l2:{node_id}"})
    for mapping in snap["mappings"]:
        if not isinstance(mapping, dict):
            continue
        dataset_id = str(
            mapping.get("curated_dataset_id") or mapping.get("curatedDatasetId") or "")
        if dataset_id:
            valid_ids.add(f"dataset:{dataset_id}")
    for link_mapping in snap["linkMappings"]:
        if not isinstance(link_mapping, dict):
            continue
        for key in (
            "src_dataset_id", "tgt_dataset_id", "edge_dataset_id",
            "srcDatasetId", "tgtDatasetId", "edgeDatasetId",
        ):
            dataset_id = str(link_mapping.get(key) or "")
            if dataset_id:
                valid_ids.add(f"dataset:{dataset_id}")
    return valid_ids


def _validated_canvas_positions(raw: Any, valid_ids: set[str]) -> dict[str, dict[str, float]]:
    if not isinstance(raw, dict):
        raise HTTPException(422, detail={
            "code": "invalid_canvas_layout",
            "message": "positions 必须是节点 ID 到坐标的对象",
        })
    positions: dict[str, dict[str, float]] = {}
    for raw_id, raw_position in raw.items():
        node_id = str(raw_id)
        if node_id not in valid_ids:
            raise HTTPException(422, detail={
                "code": "invalid_canvas_layout",
                "message": f"节点 {node_id} 不属于该版本",
            })
        if not isinstance(raw_position, dict):
            raise HTTPException(422, detail={
                "code": "invalid_canvas_layout",
                "message": f"节点 {node_id} 的坐标格式无效",
            })
        x, y = raw_position.get("x"), raw_position.get("y")
        if isinstance(x, bool) or isinstance(y, bool):
            x = y = None
        try:
            x_value, y_value = float(x), float(y)
        except (TypeError, ValueError):
            x_value = y_value = math.nan
        if not math.isfinite(x_value) or not math.isfinite(y_value):
            raise HTTPException(422, detail={
                "code": "invalid_canvas_layout",
                "message": f"节点 {node_id} 的坐标必须是有限数字",
            })
        positions[node_id] = {"x": x_value, "y": y_value}
    return positions


def _snapshot_formal(db: Session, ontology_id: str) -> dict:
    """Compatibility wrapper for historical imports and monkeypatch paths."""
    return release_service.collect_publishable_snapshot(db, ontology_id)


_DIFF_NAME_CAP = 10


def _diff_names(items: list[dict]) -> list[str]:
    """从快照元素列表提取可读名称（有界截断，供审计 diff 使用）。"""
    names = [str(i.get("name") or i.get("displayName") or i.get("id") or "?")
             for i in items]
    return names[:_DIFF_NAME_CAP]


def _diff_formal(prev: dict | None, curr: dict) -> dict:
    """按 id 对比两个正规模型快照，输出各集合的 added/modified/deleted 计数与名称。"""
    prev = prev or {}
    out: dict = {}
    total_added = total_modified = total_deleted = 0
    for key in (
        "objectTypes", "linkTypes", "actions", "functions",
        "sentinels", "mappings", "linkMappings",
    ):
        prev_items = {i["id"]: i for i in (prev.get(key) or [])}
        curr_items = {i["id"]: i for i in (curr.get(key) or [])}
        added = len(curr_items.keys() - prev_items.keys())
        deleted = len(prev_items.keys() - curr_items.keys())
        modified_items: list[dict] = []
        for iid in curr_items.keys() & prev_items.keys():
            a = {k: v for k, v in prev_items[iid].items() if k not in ("createdAt", "updatedAt")}
            b = {k: v for k, v in curr_items[iid].items() if k not in ("createdAt", "updatedAt")}
            if a != b:
                modified_items.append(curr_items[iid])
        modified = len(modified_items)
        out[key] = {
            "added": added,
            "modified": modified,
            "deleted": deleted,
            "addedNames": _diff_names([curr_items[i] for i in curr_items.keys() - prev_items.keys()]),
            "modifiedNames": _diff_names(modified_items),
            "deletedNames": _diff_names([prev_items[i] for i in prev_items.keys() - curr_items.keys()]),
        }
        total_added += added; total_modified += modified; total_deleted += deleted
    out["total"] = {"added": total_added, "modified": total_modified, "deleted": total_deleted}
    return out


def _current_release(db: Session, project: OntologyProject) -> OntologyVersion:
    """Compatibility wrapper for historical imports and monkeypatch paths."""
    return release_service.resolve_current_release(
        db,
        project,
        snapshot_loader=_snapshot_formal,
    )


def _workspace_mode(version: OntologyVersion) -> str:
    if version.node_kind == "release":
        return "release"
    if version.lifecycle_status == "trial_ready":
        return "trial"
    if version.lifecycle_status == "superseded":
        return "archived"
    return "draft"


def _workspace_payload(
        project: OntologyProject, version: OntologyVersion, *,
        is_current_release: bool = False,
        trial_run: OntologyTrialRun | None = None,
        trial_objects: list[OntologyTrialObject] | None = None,
        trial_links: list[OntologyTrialLink] | None = None) -> dict:
    """Serialize one immutable/versioned structure workspace.

    The management detail page must never infer a release from mutable Formal
    projection rows.  This payload is built only from the version snapshot
    selected by the project's authoritative ``current_release_id`` pointer.
    """
    snap = _with_canvas_layout(version.snapshot_formal, version.canvas_layout)
    workspace_mode = _workspace_mode(version)
    trial_created_at = (
        trial_run.created_at.isoformat()
        if trial_run is not None and trial_run.created_at else None)
    isolated_objects = [{
        "id": item.object_id,
        "objectTypeId": item.object_type_id,
        "properties": _json_safe(item.properties or {}),
        "computed": _json_safe(item.computed or {}),
        "source": "trial",
        "externalId": item.external_id,
        "createdAt": trial_created_at,
        "updatedAt": trial_created_at,
    } for item in (trial_objects or [])]
    isolated_links = [{
        "id": item.link_id,
        "linkTypeId": item.link_type_id,
        "sourceObjectId": item.source_object_id,
        "targetObjectId": item.target_object_id,
        "properties": _json_safe(item.properties or {}),
        "sourceRelationId": item.source_relation_id,
        "createdAt": trial_created_at,
    } for item in (trial_links or [])]
    return {
        "id": project.id, "name": project.name,
        "description": project.description, "version": version.version_number,
        "revision": f"{version.revision}:{version.snapshot_hash}",
        "objectTypes": snap["objectTypes"], "linkTypes": snap["linkTypes"],
        "actions": snap["actions"], "functions": snap["functions"],
        "mappings": snap["mappings"],
        "linkMappings": snap["linkMappings"],
        "sentinels": snap["sentinels"],
        "canvasLayout": _json_safe(version.canvas_layout or {}),
        # Trial data is read from its isolated tables only. Other version nodes
        # carry definitions without leaking the current production projection.
        "instances": isolated_objects,
        "linkInstances": isolated_links,
        "executionLogs": [],
        "trialRun": _trial_payload(trial_run) if trial_run else None,
        "workspaceMode": workspace_mode,
        "editable": workspace_mode == "draft",
        "versionId": version.id,
        "nodeKind": version.node_kind,
        "lifecycleStatus": version.lifecycle_status,
        "isCurrentRelease": is_current_release,
        "publishedAt": (
            version.published_at.isoformat() if version.published_at else None),
    }


def _mapping_workspace_payload(version: OntologyVersion, *,
                               is_current_release: bool = False) -> dict:
    snap = complete_snapshot(version.snapshot_formal)
    workspace_mode = _workspace_mode(version)
    return {
        "mappings": snap["mappings"],
        "linkMappings": snap["linkMappings"],
        "sentinels": snap["sentinels"],
        "revision": f"{version.revision}:{version.snapshot_hash}",
        "versionId": version.id,
        "versionNumber": version.version_number,
        "workspaceMode": workspace_mode,
        "editable": workspace_mode == "draft",
        "isCurrentRelease": is_current_release,
    }


def _draft_or_404(db: Session, ontology_id: str, version_id: str) -> OntologyVersion:
    draft = db.query(OntologyVersion).filter(
        OntologyVersion.id == version_id,
        OntologyVersion.ontology_id == ontology_id,
    ).first()
    if draft is None:
        raise HTTPException(404, "Version not found")
    if draft.node_kind != "draft":
        raise HTTPException(409, detail={
            "code": "immutable_release", "message": "发布版本不可修改，请先创建草稿分支",
        })
    return draft


def _ensure_editable_draft(draft: OntologyVersion) -> None:
    """Enforce the lifecycle boundary: a successful trial is an immutable snapshot."""
    if draft.lifecycle_status == "trial_ready":
        raise HTTPException(409, detail={
            "code": "trial_snapshot_frozen",
            "message": "试跑态快照已冻结；如需继续修改，请从该版本创建新的草稿分支",
        })
    if draft.lifecycle_status != "editing":
        raise HTTPException(409, detail={
            "code": "archived_version_immutable",
            "message": "该版本已归档且不可修改；如需继续演化，请从当前发布版创建新的草稿分支",
        })


_MAPPING_AUTOMATION_POLICY_KEYS = (
    "__auto_apply_on_review__",
    "__auto_apply_on_version__",
)


def _validate_workspace_mapping_policy_types(body: dict) -> None:
    """Automation flags in an immutable draft snapshot must be JSON booleans."""
    errors: list[dict] = []
    for collection, kind in (
        ("mappings", "mapping"),
        ("linkMappings", "linkMapping"),
    ):
        items = body.get(collection)
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            field_mapping = item.get("fieldMapping")
            if not isinstance(field_mapping, dict):
                continue
            for key in _MAPPING_AUTOMATION_POLICY_KEYS:
                if key not in field_mapping:
                    continue
                value = field_mapping[key]
                # ``bool`` is a subclass of ``int`` in Python; exact type is
                # required so JSON 0/1 cannot silently become policy values.
                if type(value) is bool:
                    continue
                errors.append({
                    "kind": kind,
                    "index": index,
                    "id": str(item.get("id") or ""),
                    "field": f"fieldMapping.{key}",
                    "valueType": (
                        "null" if value is None else type(value).__name__
                    ),
                })
    if errors:
        raise HTTPException(422, detail={
            "code": "invalid_mapping_automation_policy_type",
            "message": (
                "映射自动触发策略必须使用 JSON true/false，"
                "不能使用字符串、数字或 null。"
            ),
            "errors": errors,
        })


def list_versions(
    db: Session,
    ontology_id: str,
    limit: int = 20,
    offset: int = 0,
):
    """列出所有版本（分页）"""
    project = db.query(OntologyProject).filter(
        OntologyProject.id == ontology_id).first()
    if not project:
        raise HTTPException(404, "Ontology not found")
    current = _current_release(db, project)
    total = db.query(OntologyVersion).filter(
        OntologyVersion.ontology_id == ontology_id
    ).count()
    versions = db.query(OntologyVersion).options(
        *_VERSION_LISTING_DEFERS,
    ).filter(
        OntologyVersion.ontology_id == ontology_id
    ).order_by(desc(OntologyVersion.created_at)).offset(offset).limit(limit).all()
    trial_by_version = _latest_trials_by_version(
        db, ontology_id, [item.id for item in versions]) if versions else {}
    payload = {
        "data": [_version_payload(v, trial_by_version.get(v.id)) for v in versions],
        "total": total, "limit": limit, "offset": offset,
        "current_release_id": current.id,
        "current_release_version": current.version_number,
    }
    # payload 必须在 commit 前构建：expire_on_commit 会让后续属性访问
    # 触发逐行整列刷新（版本行等于被加载两遍）。
    db.commit()
    return payload


def get_current_release_workspace(
    db: Session,
    ontology_id: str,
):
    """Read the one authoritative published structure snapshot."""
    project = db.query(OntologyProject).filter(
        OntologyProject.id == ontology_id).first()
    if project is None:
        raise HTTPException(404, "Ontology not found")
    release = _current_release(db, project)
    payload = _workspace_payload(project, release, is_current_release=True)
    db.commit()
    return {"data": payload}


def get_current_release_mappings(
    db: Session,
    ontology_id: str,
):
    """Read mappings frozen into the authoritative published snapshot."""
    project = db.query(OntologyProject).filter(
        OntologyProject.id == ontology_id).first()
    if project is None:
        raise HTTPException(404, "Ontology not found")
    release = _current_release(db, project)
    payload = _mapping_workspace_payload(release, is_current_release=True)
    db.commit()
    return {"data": payload}


def get_version_tree(
    db: Session,
    ontology_id: str,
):
    def build():
        project = db.query(OntologyProject).filter(
            OntologyProject.id == ontology_id).first()
        if project is None:
            raise HTTPException(404, "Ontology not found")
        current = _current_release(db, project)
        versions = db.query(OntologyVersion).options(
            *_VERSION_LISTING_DEFERS,
        ).filter(
            OntologyVersion.ontology_id == ontology_id,
        ).order_by(OntologyVersion.created_at.asc()).all()
        latest_trials = _latest_trials_by_version(db, ontology_id)
        payload = {"data": {
            "current_release_id": current.id,
            "current_release_number": current.version_number,
            "current_release_version": current.version_number,
            "versions": [_version_payload(item, latest_trials.get(item.id))
                         for item in versions],
        }}
        # payload 必须在 commit 前构建：expire_on_commit 会让后续属性访问
        # 触发逐行整列刷新（版本行等于被加载两遍）。
        db.commit()
        return payload

    # 详情页版本树高频入口：短 TTL 响应缓存 + 版本行写路径 bump 换键
    # （fail-open）。jsonable_encoder 归一，缓存命中与直查逐字节一致；
    # 试跑状态可能由后台推进，由短 TTL 兜底（与 pending 同语义）。
    return ontology_cache.vtree_cached_call(
        ontology_cache.version_tree_cache_key(ontology_id),
        settings.ontology_version_tree_cache_ttl_seconds,
        lambda: jsonable_encoder(build()),
    )


def create_draft_version(
    db: Session,
    ontology_id: str,
    source_version_id: str,
    body: dict,
    current_user: Any,
):
    project = db.query(OntologyProject).filter(
        OntologyProject.id == ontology_id).with_for_update().first()
    if project is None:
        raise HTTPException(404, "Ontology not found")
    current = _current_release(db, project)
    source = db.query(OntologyVersion).filter(
        OntologyVersion.id == source_version_id,
        OntologyVersion.ontology_id == ontology_id,
    ).first()
    if source is None:
        raise HTTPException(404, "Source version not found")
    if source.snapshot_formal is None:
        raise HTTPException(409, detail={
            "code": "legacy_snapshot_incomplete",
            "message": "该历史版本缺少完整结构快照，不能安全创建分支",
        })
    recovery_mode = body.get("recovery_mode", body.get("recoveryMode"))
    if recovery_mode not in {None, "current_release_trial"}:
        raise HTTPException(422, detail={
            "code": "invalid_recovery_mode",
            "message": "不支持的历史恢复模式",
        })
    is_recovery = recovery_mode == "current_release_trial"
    if is_recovery:
        if source.node_kind != "release" or source.id == current.id:
            raise HTTPException(409, detail={
                "code": "recovery_requires_historical_release",
                "message": "安全恢复只能选择非当前的历史发布版本",
                "currentReleaseId": current.id,
            })
        expected_current = body.get(
            "expected_current_release_id",
            body.get("expectedCurrentReleaseId"),
        )
        if not expected_current:
            raise HTTPException(422, detail={
                "code": "recovery_current_release_required",
                "message": "创建恢复草稿前必须确认当前发布版本",
                "currentReleaseId": current.id,
            })
        if str(expected_current) != current.id:
            raise HTTPException(409, detail={
                "code": "recovery_base_changed",
                "message": "当前发布版本已变化，请刷新版本树后重新确认恢复",
                "expectedCurrentReleaseId": str(expected_current),
                "currentReleaseId": current.id,
            })
    sibling_numbers = [item.version_number for item in db.query(OntologyVersion).filter(
        OntologyVersion.ontology_id == ontology_id,
        OntologyVersion.parent_version_id == source.id,
    ).all()]
    # Version numbers are part of audit/provenance and must not be reused after
    # a branch is deleted.  Keep the deleted row out of the visible tree while
    # reserving its former number from the durable audit log.
    deleted_numbers = []
    for log in db.query(AuditLog).filter(
            AuditLog.ontology_id == ontology_id,
            AuditLog.event_subtype == "version_branch_deleted",
    ).all():
        before = log.before_state if isinstance(log.before_state, dict) else {}
        deleted_number = before.get("versionNumber")
        if isinstance(deleted_number, str):
            deleted_numbers.append(deleted_number)
    number = next_draft_number(
        source.version_number, [*sibling_numbers, *deleted_numbers])
    # 从任何状态分支时继承视觉布局，但仍与新草稿的模型快照分开保存，
    # 避免仅调整过位置就被版本差异误报为对象定义变更。
    snap = complete_snapshot(source.snapshot_formal)
    base_release_id = current.id if is_recovery else (
        source.id if source.node_kind == "release" else (
            source.base_release_id or current.id))
    draft = OntologyVersion(
        id=str(uuid.uuid4()), ontology_id=ontology_id,
        version_number=number,
        version_label=str(body.get("version_label") or body.get("versionLabel") or ""),
        description=str(body.get("description") or ""),
        parent_version_id=source.id, base_release_id=base_release_id,
        node_kind="draft", lifecycle_status="editing", revision=0,
        snapshot_formal=snap, snapshot_hash=snapshot_hash(snap),
        canvas_layout=_json_safe(source.canvas_layout or {}),
        snapshot_semantic=_json_safe(source.snapshot_semantic),
        snapshot_entities=_json_safe(source.snapshot_entities or []),
        snapshot_relations=_json_safe(source.snapshot_relations or []),
        snapshot_logic=_json_safe(source.snapshot_logic or []),
        snapshot_actions=_json_safe(source.snapshot_actions or []),
        change_summary=_diff_formal(
            current.snapshot_formal, snap), created_by=current_user.id,
    )
    db.add(draft)
    db.commit()
    ontology_cache.invalidate_version_tree()
    return {"data": _version_payload(draft)}


def delete_draft_version(
    db: Session,
    ontology_id: str,
    version_id: str,
    current_user: Any,
    *,
    _recover_expired_trial_runs,
):
    """Delete only an unpublished leaf branch.

    A version with descendants is part of the evolution tree's provenance and
    cannot be removed. Published and superseded nodes are immutable audit facts.
    """
    version = db.query(OntologyVersion).filter(
        OntologyVersion.id == version_id,
        OntologyVersion.ontology_id == ontology_id,
    ).with_for_update().first()
    if version is None:
        raise HTTPException(404, "Version not found")
    if (version.node_kind != "draft"
            or version.lifecycle_status not in {"editing", "trial_ready"}):
        raise HTTPException(409, detail={
            "code": "version_delete_forbidden",
            "message": "只有未发布的草稿态或试跑态分支可以删除",
        })
    child = db.query(OntologyVersion).filter(
        OntologyVersion.ontology_id == ontology_id,
        OntologyVersion.parent_version_id == version.id,
    ).first()
    if child is not None:
        raise HTTPException(409, detail={
            "code": "version_not_leaf",
            "message": "该版本下仍有分支，只有叶子节点可以删除",
            "childVersionId": child.id,
            "childVersionNumber": child.version_number,
        })
    _recover_expired_trial_runs(db, ontology_id, version.id)
    running_trial = db.query(OntologyTrialRun).filter(
        OntologyTrialRun.version_id == version.id,
        OntologyTrialRun.status == "running",
    ).first()
    if running_trial is not None:
        raise HTTPException(409, detail={
            "code": "trial_running",
            "message": "该版本仍在试跑中，暂时不能删除",
            "trialRunId": running_trial.id,
            "leaseExpiresAt": (
                running_trial.lease_expires_at.isoformat()
                if running_trial.lease_expires_at else None
            ),
        })

    number = version.version_number
    trial_ids = [item.id for item in db.query(OntologyTrialRun.id).filter(
        OntologyTrialRun.version_id == version.id,
    ).all()]
    if trial_ids:
        # Delete explicitly as well as relying on ON DELETE CASCADE. This keeps
        # SQLite/dev environments (where FK cascades may be disabled) aligned
        # with PostgreSQL production semantics.
        db.query(OntologyTrialLink).filter(
            OntologyTrialLink.trial_run_id.in_(trial_ids),
        ).delete(synchronize_session=False)
        db.query(OntologyTrialObject).filter(
            OntologyTrialObject.trial_run_id.in_(trial_ids),
        ).delete(synchronize_session=False)
        db.query(OntologyTrialRun).filter(
            OntologyTrialRun.id.in_(trial_ids),
        ).delete(synchronize_session=False)
    # Change logs are historical records and remain queryable after branch
    # deletion, but their optional FK must no longer point at the removed node.
    db.query(OntologyChangeLog).filter(
        OntologyChangeLog.version_id == version.id,
    ).update({OntologyChangeLog.version_id: None}, synchronize_session=False)
    db.delete(version)
    db.add(AuditLog(
        id=str(uuid.uuid4()), ontology_id=ontology_id,
        event_type="edit", event_subtype="version_branch_deleted",
        user_id=current_user.id, user_name=current_user.username,
        description=f"删除叶子分支 {number}",
        object_type="ontology_version", object_id=version_id,
        before_state={"versionNumber": number}, after_state=None,
        meta={"lifecycleStatus": version.lifecycle_status},
    ))
    db.commit()
    ontology_cache.invalidate_version_tree()
    return {"data": {"id": version_id, "version_number": number}}


def get_version_workspace(
    db: Session,
    ontology_id: str,
    version_id: str,
):
    version = db.query(OntologyVersion).filter(
        OntologyVersion.id == version_id,
        OntologyVersion.ontology_id == ontology_id,
    ).first()
    if version is None:
        raise HTTPException(404, "Version not found")
    project = db.query(OntologyProject).filter(
        OntologyProject.id == ontology_id).first()
    if project is None:
        raise HTTPException(404, "Ontology not found")
    trial_run = None
    trial_objects: list[OntologyTrialObject] = []
    trial_links: list[OntologyTrialLink] = []
    if _workspace_mode(version) == "trial":
        trial_run = db.query(OntologyTrialRun).filter(
            OntologyTrialRun.ontology_id == ontology_id,
            OntologyTrialRun.version_id == version.id,
            OntologyTrialRun.status == "passed",
        ).order_by(desc(OntologyTrialRun.created_at)).first()
        if trial_run is not None:
            trial_objects = db.query(OntologyTrialObject).filter(
                OntologyTrialObject.trial_run_id == trial_run.id,
            ).order_by(OntologyTrialObject.object_type_id,
                       OntologyTrialObject.object_id).all()
            trial_links = db.query(OntologyTrialLink).filter(
                OntologyTrialLink.trial_run_id == trial_run.id,
            ).order_by(OntologyTrialLink.link_type_id,
                       OntologyTrialLink.link_id).all()
    return {"data": _workspace_payload(
        project, version,
        is_current_release=project.current_release_id == version.id,
        trial_run=trial_run, trial_objects=trial_objects,
        trial_links=trial_links)}


# 行锁等待上限（D-015）：生产为单 uvicorn 同步线程池，无界的 FOR UPDATE
# 等待会在上游事务楔死时把保存请求一起挂死（nginx 侧表现为 502/挂起）。
# 有界等待把该场景转化为即刻 409，由前端按可重试错误呈现。
_ROW_LOCK_WAIT_TIMEOUT = "5s"

# PostgreSQL 锁等待超时 / 语句取消 / 死锁检测的 SQLSTATE
_LOCK_WAIT_SQLSTATES = {"55P03", "57014", "40P01"}


def _bound_row_lock_wait(db: Session) -> None:
    """给当前事务的行锁等待设上限；SQLite（单测）与其他方言不支持则跳过。"""
    try:
        bind = db.get_bind()
    except Exception:  # noqa: BLE001 — 未绑定引擎的假会话按无界旧语义处理
        return
    if bind is None or bind.dialect.name != "postgresql":
        return
    db.execute(text(f"SET LOCAL lock_timeout = '{_ROW_LOCK_WAIT_TIMEOUT}'"))


@contextmanager
def _bounded_row_locks(db: Session):
    """写阶段锁纪律：进入前设 lock_timeout，锁等待超时转 409 而非无限挂起。"""
    _bound_row_lock_wait(db)
    try:
        yield
    except DBAPIError as exc:
        if getattr(exc.orig, "pgcode", None) not in _LOCK_WAIT_SQLSTATES:
            raise
        db.rollback()
        raise HTTPException(409, detail={
            "code": "write_lock_timeout",
            "message": (
                "写入与并发操作冲突（数据库锁等待超时），"
                "请稍后重试；如持续出现请检查是否有未完成的试跑或探索会话"
            ),
        }) from exc


def save_canvas_layout(
    db: Session,
    ontology_id: str,
    body: dict,
):
    """保存共享画布布局；不推进模型 revision，也不改变 snapshot_hash。"""
    project = db.query(OntologyProject).filter(
        OntologyProject.id == ontology_id,
    ).first()
    if project is None:
        raise HTTPException(404, "Ontology not found")

    version_id = body.get("versionId", body.get("version_id"))
    if version_id:
        version = db.query(OntologyVersion).filter(
            OntologyVersion.id == str(version_id),
            OntologyVersion.ontology_id == ontology_id,
        ).first()
    else:
        version = _current_release(db, project)
    if version is None:
        raise HTTPException(404, "Version not found")

    # 写阶段（短锁）：project → version 的加锁顺序与发布/删除路径一致；
    # 快照节点集与现有布局必须在锁内取新鲜值，防止并发结构保存丢更新。
    with _bounded_row_locks(db):
        locked_project = db.query(OntologyProject).filter(
            OntologyProject.id == ontology_id,
        ).with_for_update().first()
        if locked_project is None:
            raise HTTPException(404, "Ontology not found")
        if version_id:
            locked = db.query(OntologyVersion).filter(
                OntologyVersion.id == version.id,
                OntologyVersion.ontology_id == ontology_id,
            ).populate_existing().with_for_update().first()
        else:
            # project 行锁已挡住发布指针切换，锁内解析 current release
            # 保持与旧实现相同的指针一致性语义。
            locked = _current_release(db, locked_project)
        if locked is None:
            raise HTTPException(404, "Version not found")

        snapshot = complete_snapshot(locked.snapshot_formal)
        valid_ids = _canvas_node_ids(snapshot)
        updates = _validated_canvas_positions(body.get("positions"), valid_ids)
        current = locked.canvas_layout if isinstance(locked.canvas_layout, dict) else {}
        merged = {
            str(node_id): value for node_id, value in current.items()
            if str(node_id) in valid_ids and isinstance(value, dict)
        }
        merged.update(updates)
        locked.canvas_layout = merged
        db.commit()
    ontology_cache.invalidate_version_tree()
    return {"data": {
        "versionId": locked.id,
        "positions": merged,
    }}


def save_draft_workspace(
    db: Session,
    ontology_id: str,
    version_id: str,
    body: dict,
    current_user: Any,
    *,
    _raise_publish_errors,
    _stale_previous_trials,
):
    draft = db.query(OntologyVersion).filter(
        OntologyVersion.id == version_id,
        OntologyVersion.ontology_id == ontology_id,
    ).first()
    if draft is None:
        raise HTTPException(404, "Version not found")
    if draft.node_kind != "draft":
        raise HTTPException(409, detail={"code": "immutable_release", "message": "发布版本不可修改"})
    _ensure_editable_draft(draft)
    expected = f"{draft.revision}:{draft.snapshot_hash}"
    base_revision = body.get("baseRevision", body.get("base_revision"))
    if base_revision is not None and str(base_revision) != expected:
        raise HTTPException(409, detail={
            "code": "conflict", "message": "该草稿已被其他会话修改，请重新加载",
            "currentRevision": expected,
        })
    try:
        candidate = workspace_snapshot(body, draft.snapshot_formal)
    except Exception as exc:
        raise HTTPException(422, detail={
            "code": "invalid_workspace", "message": str(exc),
        }) from exc
    # 草稿保存期分层：「动作无启用的可执行副作用规则」（探索产物落地即休眠）
    # 不阻断保存，降级为 warnings 随响应透出；试跑/发布仍走严格口径。
    save_warnings: list[dict] = []
    errors = validate_snapshot(
        candidate,
        require_object_type=False,
        require_executable_action_rules=False,
        warnings_out=save_warnings,
    )
    errors.extend(_dynamic_sentinel_id_conflict_errors(
        db, ontology_id, candidate.get("sentinels"),
    ))
    _raise_publish_errors(errors, "草稿结构校验未通过")
    # 审计 diff 必须在替换快照前计算，否则 prev/curr 相同、差异永远为空。
    diff = _diff_formal(draft.snapshot_formal, candidate)
    valid_layout_ids = _canvas_node_ids(candidate)

    # 写阶段（短锁）：校验/审计等重活不持锁；锁内按 revision:snapshot_hash
    # CAS 复核（D-015），等待有界，并发漂移即刻 409。布局保存不推进
    # revision，CAS 挡不住它，因此 canvas_layout 必须在锁内取新鲜值合并。
    with _bounded_row_locks(db):
        locked = db.query(OntologyVersion).filter(
            OntologyVersion.id == version_id,
            OntologyVersion.ontology_id == ontology_id,
        ).populate_existing().with_for_update().first()
        if locked is None:
            raise HTTPException(404, "Version not found")
        if locked.node_kind != "draft":
            raise HTTPException(409, detail={"code": "immutable_release", "message": "发布版本不可修改"})
        _ensure_editable_draft(locked)
        current_expected = f"{locked.revision}:{locked.snapshot_hash}"
        if current_expected != expected:
            raise HTTPException(409, detail={
                "code": "conflict", "message": "该草稿已被其他会话修改，请重新加载",
                "currentRevision": current_expected,
            })
        previous_layout = locked.canvas_layout if isinstance(locked.canvas_layout, dict) else {}
        next_layout = {
            str(node_id): value for node_id, value in previous_layout.items()
            if str(node_id) in valid_layout_ids and isinstance(value, dict)
        }
        # Object coordinates are still edited by the full-screen graph workspace.
        # Preserve the independent L2 property/action coordinates while refreshing
        # those shared object positions from the submitted model workspace.
        next_layout.update({
            str(item["id"]): {
                "x": float(item.get("positionX") or 0),
                "y": float(item.get("positionY") or 0),
            }
            for item in candidate["objectTypes"] if item.get("id")
        })
        locked.snapshot_formal = candidate
        locked.canvas_layout = next_layout
        locked.revision = (locked.revision or 0) + 1
        locked.snapshot_hash = snapshot_hash(candidate)
        locked.lifecycle_status = "editing"
        _stale_previous_trials(db, locked)
        db.add(AuditLog(
            id=str(uuid.uuid4()), ontology_id=ontology_id,
            event_type="edit", event_subtype="workspace_saved",
            user_id=current_user.id, user_name=current_user.username,
            description=(
                f"保存草稿 {locked.version_number} 结构工作区，"
                f"revision 推进至 {locked.revision}"
            ),
            object_type="ontology_version", object_id=version_id,
            before_state=None, after_state=None,
            meta={
                "revision": locked.revision,
                "snapshotHash": locked.snapshot_hash,
                "diff": diff,
            },
        ))
        db.commit()
    ontology_cache.invalidate_version_tree()
    return {"data": {
        "revision": f"{locked.revision}:{locked.snapshot_hash}",
        "snapshotHash": locked.snapshot_hash,
        "warnings": save_warnings,
    }}


def get_draft_mappings(
    db: Session,
    ontology_id: str,
    version_id: str,
):
    version = db.query(OntologyVersion).filter(
        OntologyVersion.id == version_id,
        OntologyVersion.ontology_id == ontology_id,
    ).first()
    if version is None:
        raise HTTPException(404, "Version not found")
    project = db.query(OntologyProject).filter(
        OntologyProject.id == ontology_id).first()
    return {"data": _mapping_workspace_payload(
        version,
        is_current_release=bool(
            project and project.current_release_id == version.id),
    )}


def save_draft_mappings(
    db: Session,
    ontology_id: str,
    version_id: str,
    body: dict,
    current_user: Any,
    *,
    _raise_publish_errors,
    _stale_previous_trials,
):
    draft = db.query(OntologyVersion).filter(
        OntologyVersion.id == version_id,
        OntologyVersion.ontology_id == ontology_id,
    ).first()
    if draft is None:
        raise HTTPException(404, "Version not found")
    if draft.node_kind != "draft":
        raise HTTPException(409, detail={"code": "immutable_release", "message": "发布版本不可修改"})
    _ensure_editable_draft(draft)
    expected = f"{draft.revision}:{draft.snapshot_hash}"
    base_revision = body.get("baseRevision", body.get("base_revision"))
    if base_revision is not None and str(base_revision) != expected:
        raise HTTPException(409, detail={
            "code": "conflict", "message": "该草稿映射已被修改，请重新加载",
            "currentRevision": expected,
        })
    _validate_workspace_mapping_policy_types(body)
    snap = complete_snapshot(draft.snapshot_formal)
    for key in ("mappings", "linkMappings", "sentinels"):
        if key in body:
            if not isinstance(body[key], list):
                raise HTTPException(422, f"{key} must be an array")
            snap[key] = _json_safe(body[key])
    sentinel_errors = validate_builtin_sentinel_contract(snap["sentinels"])
    sentinel_errors.extend(_dynamic_sentinel_id_conflict_errors(
        db, ontology_id, snap["sentinels"],
    ))
    _raise_publish_errors(
        sentinel_errors,
        "建模内置哨兵字段校验未通过",
    )
    # 审计 diff 必须在替换快照前计算，否则 prev/curr 相同、差异永远为空。
    diff = _diff_formal(draft.snapshot_formal, snap)

    # 写阶段（短锁）：同 save_draft_workspace 的 CAS 复核与有界等待。
    with _bounded_row_locks(db):
        locked = db.query(OntologyVersion).filter(
            OntologyVersion.id == version_id,
            OntologyVersion.ontology_id == ontology_id,
        ).populate_existing().with_for_update().first()
        if locked is None:
            raise HTTPException(404, "Version not found")
        if locked.node_kind != "draft":
            raise HTTPException(409, detail={"code": "immutable_release", "message": "发布版本不可修改"})
        _ensure_editable_draft(locked)
        current_expected = f"{locked.revision}:{locked.snapshot_hash}"
        if current_expected != expected:
            raise HTTPException(409, detail={
                "code": "conflict", "message": "该草稿映射已被修改，请重新加载",
                "currentRevision": current_expected,
            })
        locked.snapshot_formal = snap
        locked.revision = (locked.revision or 0) + 1
        locked.snapshot_hash = snapshot_hash(snap)
        locked.lifecycle_status = "editing"
        _stale_previous_trials(db, locked)
        db.add(AuditLog(
            id=str(uuid.uuid4()), ontology_id=ontology_id,
            event_type="edit", event_subtype="workspace_mappings_saved",
            user_id=current_user.id, user_name=current_user.username,
            description=(
                f"保存草稿 {locked.version_number} 数据映射与哨兵，"
                f"revision 推进至 {locked.revision}"
            ),
            object_type="ontology_version", object_id=version_id,
            before_state=None, after_state=None,
            meta={
                "revision": locked.revision,
                "snapshotHash": locked.snapshot_hash,
                "diff": diff,
            },
        ))
        db.commit()
    ontology_cache.invalidate_version_tree()
    return {"data": {
        "revision": f"{locked.revision}:{locked.snapshot_hash}",
        "snapshotHash": locked.snapshot_hash,
    }}


def get_draft_impact(
    db: Session,
    ontology_id: str,
    version_id: str,
    *,
    validate_release_mapping_contract,
    semantic_overview_fn=None,
):
    draft = _draft_or_404(db, ontology_id, version_id)
    project = db.query(OntologyProject).filter(
        OntologyProject.id == ontology_id).first()
    current = _current_release(db, project)
    report = impact_report(current.snapshot_formal, draft.snapshot_formal)
    data = {
        **report,
        "baseReleaseId": draft.base_release_id,
        "currentReleaseId": current.id,
        "baseOutdated": draft.base_release_id != current.id,
        "releaseReadiness": _release_readiness(
            db,
            draft=draft,
            current=current,
            report=report,
            release_mapping_validator=validate_release_mapping_contract,
        ),
    }
    if semantic_overview_fn is not None:
        data["semanticOverview"] = semantic_overview_fn(
            draft.snapshot_semantic, complete_snapshot(draft.snapshot_formal))
    # 世界模型消费方影响：实时查询、不参与 impact 哈希（哈希只覆盖纯结构 diff）
    data["worldModelImpact"] = world_model_affected_services(
        db, ontology_id, report)
    return {"data": data}


def get_version_semantic(
    db: Session,
    ontology_id: str,
    version_id: str,
    *,
    semantic_overview_fn,
    semantic_consistency_fn=None,
):
    """读取任一版本的业务语义层快照与一致性总览（发布/草稿均可，只读）。"""
    version = db.query(OntologyVersion).filter(
        OntologyVersion.id == version_id,
        OntologyVersion.ontology_id == ontology_id,
    ).first()
    if version is None:
        raise HTTPException(404, "Version not found")
    semantic = (
        version.snapshot_semantic
        if isinstance(version.snapshot_semantic, dict) else None
    )
    # 语义层尚未沉淀时不做重放比对：否则结构元素会被全部误报为
    # semantic_business_missing（语义缺失与语义漂移是两件事）。
    issues: list[dict] = []
    if semantic is not None and semantic_consistency_fn is not None:
        issues = semantic_consistency_fn(semantic, version.snapshot_formal)
    return {"data": {
        "semantic": semantic,
        "overview": semantic_overview_fn(semantic, version.snapshot_formal),
        "issues": issues,
    }}
