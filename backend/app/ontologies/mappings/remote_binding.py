"""Remote (federated) mapping-binding contract, published reads, and the
composition-root validator port.

The remote binding has exactly one definition: the frozen asset descriptor
lives inside the object mapping's ``field_mapping`` (``__binding_mode__`` /
``__remote__``) and every new version snapshot mirrors it as top-level
``bindingMode`` / ``remote`` keys.  Historical snapshots without those keys are
local bindings by definition.  The ontology domain never imports
``data_channel`` here; the integrated adapter is injected by the composition
root through :func:`set_remote_binding_validator`.
"""
from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

from fastapi import HTTPException
from sqlalchemy.orm import Session

BINDING_MODE_LOCAL = "local"
BINDING_MODE_REMOTE = "remote"
BINDING_MODE_KEY = "__binding_mode__"
REMOTE_DESCRIPTOR_KEY = "__remote__"
# Bounded controlled read for trial verification.  Row values are consumed
# in-memory only and are never persisted (counts and fingerprints only).
REMOTE_SAMPLE_ROW_LIMIT = 20

_DESCRIPTOR_IDENTITY_FIELDS = (
    "assetId", "datasetId", "connectionId",
    "schemaName", "tableName", "schemaFingerprint",
)

_SNAPSHOT_COLLECTIONS = ("mappings", "linkMappings", "objectTypes")


class RemoteBindingError(ValueError):
    """远程绑定契约/验证失败；code 与 context 供网关和 UI 显式呈现。"""

    def __init__(self, code: str, message: str, **context: Any):
        self.code = code
        self.message = message
        self.context = {key: value for key, value in context.items()
                        if value is not None}
        super().__init__(message)

    def detail(self) -> dict:
        payload = {"code": self.code, "message": self.message}
        payload.update(self.context)
        return payload


class RemoteBindingAmbiguous(RemoteBindingError):
    """An ObjectType has more than one remote binding in one snapshot."""


class RemoteBindingPortError(RemoteBindingError):
    """The composition root has not wired the integrated validator."""


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def normalize_binding_mode(value: Any) -> str:
    if value is None or value == "":
        return BINDING_MODE_LOCAL
    mode = str(value).strip()
    if mode not in (BINDING_MODE_LOCAL, BINDING_MODE_REMOTE):
        raise RemoteBindingError(
            "binding_mode_invalid",
            f"不支持的绑定模式：{value}（仅 local/remote）",
            bindingMode=value,
        )
    return mode


def mapping_binding_mode(field_mapping: dict | None) -> str:
    """Binding mode of a live mapping definition (reserved-key storage)."""
    return normalize_binding_mode((field_mapping or {}).get(BINDING_MODE_KEY))


def link_mapping_binding_mode(field_mapping: dict | None) -> str:
    """Link mappings reuse the same reserved key (no parallel definition)."""
    return mapping_binding_mode(field_mapping)


def normalize_remote_descriptor(raw: Any) -> dict:
    """Validate the frozen descriptor while preserving the full column spec.

    Columns keep every physical attribute the source reflection produced
    (precision/scale/length/unsigned/timezone/...); only ``name`` and ``type``
    are mandatory so decimal/timezone decisions stay possible downstream.
    """
    if not isinstance(raw, dict):
        raise RemoteBindingError(
            "remote_descriptor_invalid", "远程绑定描述符必须是对象")
    missing = [
        field for field in _DESCRIPTOR_IDENTITY_FIELDS
        if not str(raw.get(field) or "").strip()
    ]
    if missing:
        raise RemoteBindingError(
            "remote_descriptor_invalid",
            "远程绑定描述符缺少必需字段",
            missingFields=missing,
        )
    columns = raw.get("columns")
    if not isinstance(columns, list) or not columns:
        raise RemoteBindingError(
            "remote_descriptor_invalid", "远程绑定描述符缺少列清单")
    names: set[str] = set()
    for column in columns:
        if (not isinstance(column, dict)
                or not str(column.get("name") or "").strip()
                or not str(column.get("type") or "").strip()):
            raise RemoteBindingError(
                "remote_descriptor_invalid",
                "远程绑定列必须包含 name/type（保留完整物理列规格）")
        name = str(column["name"])
        if name in names:
            raise RemoteBindingError(
                "remote_descriptor_invalid",
                f"远程绑定列清单重复：{name}", duplicateColumn=name)
        names.add(name)
    primary_key = raw.get("primaryKey")
    if (not isinstance(primary_key, list) or not primary_key
            or not all(str(column or "").strip() for column in primary_key)):
        raise RemoteBindingError(
            "remote_descriptor_invalid", "远程绑定描述符缺少非空主键清单")
    pk_columns = [str(column) for column in primary_key]
    if len(set(pk_columns)) != len(pk_columns):
        raise RemoteBindingError(
            "remote_descriptor_invalid", "远程绑定主键清单包含重复列")
    unknown = [column for column in pk_columns if column not in names]
    if unknown:
        raise RemoteBindingError(
            "remote_descriptor_invalid",
            "远程绑定主键引用了列清单之外的列",
            unknownPrimaryKeyColumns=unknown,
        )
    return _json_safe(raw)


def remote_descriptor(field_mapping: dict | None) -> dict | None:
    """Frozen descriptor of a remote mapping; ``None`` for local mappings."""
    data = field_mapping or {}
    if mapping_binding_mode(data) != BINDING_MODE_REMOTE:
        return None
    descriptor = data.get(REMOTE_DESCRIPTOR_KEY)
    if descriptor is None:
        raise RemoteBindingError(
            "remote_descriptor_missing",
            "远程绑定缺少冻结资产描述符（__remote__）")
    return normalize_remote_descriptor(descriptor)


def snapshot_entry_binding(entry: Any) -> tuple[str, dict | None]:
    """Normalize a snapshot object-mapping entry's binding.

    New snapshots carry top-level ``bindingMode``/``remote``; entries written
    by older code (or legacy field_mapping storage) resolve through the same
    reserved keys.  Absent keys mean a local binding.
    """
    if not isinstance(entry, dict):
        raise RemoteBindingError(
            "remote_descriptor_invalid", "快照映射条目必须是对象")
    field_mapping = entry.get("fieldMapping")
    field_mapping = field_mapping if isinstance(field_mapping, dict) else {}
    raw_mode = entry.get("bindingMode", field_mapping.get(BINDING_MODE_KEY))
    mode = normalize_binding_mode(raw_mode)
    if mode != BINDING_MODE_REMOTE:
        return BINDING_MODE_LOCAL, None
    raw = entry.get("remote")
    if raw is None:
        raw = field_mapping.get(REMOTE_DESCRIPTOR_KEY)
    if raw is None:
        raise RemoteBindingError(
            "remote_descriptor_missing",
            f"远程映射 {entry.get('id') or ''} 快照缺少冻结描述符")
    return BINDING_MODE_REMOTE, normalize_remote_descriptor(raw)


def snapshot_link_binding_mode(entry: Any) -> str:
    if not isinstance(entry, dict):
        return BINDING_MODE_LOCAL
    field_mapping = entry.get("fieldMapping")
    field_mapping = field_mapping if isinstance(field_mapping, dict) else {}
    return normalize_binding_mode(
        entry.get("bindingMode", field_mapping.get(BINDING_MODE_KEY)))


def _snapshot_collections(snapshot: dict | None) -> dict[str, list]:
    source = snapshot if isinstance(snapshot, dict) else {}
    return {key: (source.get(key) or []) for key in _SNAPSHOT_COLLECTIONS}


def _business_field_map(field_mapping: dict | None) -> dict[str, str]:
    return {
        str(column): str(target)
        for column, target in (field_mapping or {}).items()
        if not str(column).startswith("__") and target
    }


def snapshot_remote_datasets(snapshot: dict | None) -> set[str]:
    """Datasets remote-bound by object mappings inside one snapshot."""
    datasets: set[str] = set()
    for entry in _snapshot_collections(snapshot)["mappings"]:
        mode, descriptor = snapshot_entry_binding(entry)
        if mode != BINDING_MODE_REMOTE or descriptor is None:
            continue
        datasets.add(str(descriptor.get("datasetId")
                         or entry.get("curatedDatasetId") or ""))
    return {dataset for dataset in datasets if dataset}


def _resolve_object_type(entry: dict, object_types: list[dict]) -> dict | None:
    by_id = {
        str(item.get("id")): item for item in object_types if item.get("id")
    }
    target = entry.get("targetObjectTypeId")
    if target and str(target) in by_id:
        return by_id[str(target)]
    entity_class = str(entry.get("entityClass") or "")
    if not entity_class:
        return None
    for item in object_types:
        if entity_class in (str(item.get("name") or ""),
                            str(item.get("displayName") or "")):
            return item
    return None


def remote_bindings_by_type(snapshot: dict | None) -> dict[str, list[dict]]:
    """All normalized remote object bindings grouped by ObjectType id."""
    collections = _snapshot_collections(snapshot)
    object_types = [
        item for item in collections["objectTypes"] if isinstance(item, dict)
    ]
    grouped: dict[str, list[dict]] = {}
    for entry in collections["mappings"]:
        if not isinstance(entry, dict):
            continue
        mode, descriptor = snapshot_entry_binding(entry)
        if mode != BINDING_MODE_REMOTE or descriptor is None:
            continue
        target = _resolve_object_type(entry, object_types)
        if target is None:
            continue
        field_mapping = entry.get("fieldMapping")
        field_mapping = (field_mapping
                         if isinstance(field_mapping, dict) else {})
        dataset_id = str(entry.get("curatedDatasetId") or "")
        descriptor_dataset = str(descriptor.get("datasetId") or "")
        if descriptor_dataset and dataset_id and descriptor_dataset != dataset_id:
            raise RemoteBindingError(
                "remote_descriptor_dataset_mismatch",
                f"远程映射 {entry.get('id') or ''} 的冻结资产数据集与映射数据集不一致",
                mappingId=str(entry.get("id") or ""),
                mappingDatasetId=dataset_id,
                descriptorDatasetId=descriptor_dataset,
            )
        binding = {
            "mappingId": str(entry.get("id") or ""),
            "objectTypeId": str(target.get("id") or ""),
            "entityClass": str(entry.get("entityClass") or ""),
            "datasetId": dataset_id or descriptor_dataset,
            "assetId": str(descriptor.get("assetId") or ""),
            "connectionId": str(descriptor.get("connectionId") or ""),
            "schemaName": str(descriptor.get("schemaName") or ""),
            "tableName": str(descriptor.get("tableName") or ""),
            "columns": descriptor.get("columns") or [],
            "primaryKey": [str(column) for column in
                           descriptor.get("primaryKey") or []],
            "schemaFingerprint": str(descriptor.get("schemaFingerprint") or ""),
            "fieldMap": _business_field_map(field_mapping),
        }
        grouped.setdefault(binding["objectTypeId"], []).append(binding)
    return grouped


def get_remote_binding(snapshot: dict | None, object_type_id: str) -> dict | None:
    """Unique remote binding for one ObjectType inside a version snapshot.

    Returns ``None`` when the type has no remote binding.  Raises
    :class:`RemoteBindingAmbiguous` when more than one remote mapping targets
    the type.
    """
    matches = remote_bindings_by_type(snapshot).get(str(object_type_id), [])
    if not matches:
        return None
    if len(matches) > 1:
        raise RemoteBindingAmbiguous(
            "remote_binding_ambiguous",
            f"ObjectType {object_type_id} 存在 {len(matches)} 个远程绑定，"
            "拒绝猜测唯一绑定",
            objectTypeId=str(object_type_id),
            mappingIds=sorted(item["mappingId"] for item in matches),
        )
    return matches[0]


def get_remote_link_bindings(
        snapshot: dict | None, link_type_id: str | None = None) -> list[dict]:
    """Normalized remote link definitions (parameterized eq-JOIN contracts)."""
    entries: list[dict] = []
    for entry in _snapshot_collections(snapshot)["linkMappings"]:
        if not isinstance(entry, dict):
            continue
        if snapshot_link_binding_mode(entry) != BINDING_MODE_REMOTE:
            continue
        entry_type = str(entry.get("linkTypeId") or "")
        if link_type_id is not None and entry_type != str(link_type_id):
            continue
        entries.append({
            "mappingId": str(entry.get("id") or ""),
            "linkTypeId": entry_type,
            "relationType": str(entry.get("relationType") or ""),
            "srcDatasetId": str(entry.get("srcDatasetId") or ""),
            "tgtDatasetId": str(entry.get("tgtDatasetId") or ""),
            "srcKey": str(entry.get("srcKey") or ""),
            "tgtKey": str(entry.get("tgtKey") or ""),
        })
    return entries


def published_mapping_contract(
        db: Session, ontology_id: str, *,
        release_id: str | None = None) -> dict:
    """Effective mapping contract of the current (or given) release.

    This is the published read seam for query-time mapping resolution.
    Authorization stays with the caller (agent profile mechanisms); the port
    only resolves immutable published definitions.  The release is resolved
    read-only (models only) so this leaf contract module never depends on the
    version services (no import ring with release snapshot serialization).
    """
    from sqlalchemy import desc

    from app.ontologies.projects.models import OntologyProject
    from app.ontologies.versions.models import OntologyVersion

    project = db.query(OntologyProject).filter(
        OntologyProject.id == ontology_id).first()
    if project is None:
        raise RemoteBindingError(
            "ontology_not_found", f"本体 {ontology_id} 不存在",
            ontologyId=ontology_id)
    if release_id is not None:
        version = db.query(OntologyVersion).filter(
            OntologyVersion.id == release_id,
            OntologyVersion.ontology_id == ontology_id,
            OntologyVersion.node_kind == "release",
            OntologyVersion.lifecycle_status == "released",
        ).first()
        if version is None:
            raise RemoteBindingError(
                "release_not_found",
                f"发布版本 {release_id} 不存在",
                ontologyId=ontology_id, releaseId=release_id)
    else:
        version = None
        if project.current_release_id:
            version = db.query(OntologyVersion).filter(
                OntologyVersion.id == project.current_release_id,
                OntologyVersion.ontology_id == ontology_id,
                OntologyVersion.node_kind == "release",
                OntologyVersion.lifecycle_status == "released",
            ).first()
        if version is None:
            version = db.query(OntologyVersion).filter(
                OntologyVersion.ontology_id == ontology_id,
                OntologyVersion.node_kind == "release",
                OntologyVersion.lifecycle_status == "released",
            ).order_by(
                desc(OntologyVersion.published_at),
                desc(OntologyVersion.created_at),
            ).first()
        if version is None:
            raise RemoteBindingError(
                "release_not_found",
                f"本体 {ontology_id} 没有可读的发布版本",
                ontologyId=ontology_id)
    snapshot = version.snapshot_formal or {}
    collections = _snapshot_collections(snapshot)

    mappings: list[dict] = []
    for entry in collections["mappings"]:
        if not isinstance(entry, dict):
            continue
        mode, descriptor = snapshot_entry_binding(entry)
        field_mapping = entry.get("fieldMapping")
        field_mapping = (field_mapping
                         if isinstance(field_mapping, dict) else {})
        mappings.append({
            "mappingId": str(entry.get("id") or ""),
            "datasetId": str(entry.get("curatedDatasetId") or ""),
            "entityClass": str(entry.get("entityClass") or ""),
            "targetObjectTypeId": (
                str(entry.get("targetObjectTypeId"))
                if entry.get("targetObjectTypeId") is not None else None
            ),
            "bindingMode": mode,
            "remote": descriptor,
            "fieldMap": _business_field_map(field_mapping),
            "primaryKeyColumns": [
                column for column in str(
                    field_mapping.get("__primary_key__") or "").split(",")
                if column
            ],
            "confidence": entry.get("confidence"),
            "status": str(entry.get("status") or ""),
        })

    link_mappings: list[dict] = []
    for entry in collections["linkMappings"]:
        if not isinstance(entry, dict):
            continue
        link_mappings.append({
            "mappingId": str(entry.get("id") or ""),
            "bindingMode": snapshot_link_binding_mode(entry),
            "linkTypeId": (
                str(entry.get("linkTypeId"))
                if entry.get("linkTypeId") is not None else None
            ),
            "relationType": str(entry.get("relationType") or ""),
            "srcDatasetId": str(entry.get("srcDatasetId") or ""),
            "tgtDatasetId": str(entry.get("tgtDatasetId") or ""),
            "srcKey": str(entry.get("srcKey") or ""),
            "tgtKey": str(entry.get("tgtKey") or ""),
            "edgeDatasetId": (
                str(entry.get("edgeDatasetId"))
                if entry.get("edgeDatasetId") is not None else None
            ),
            "fieldMap": _business_field_map(entry.get("fieldMapping")),
        })
    return {
        "ontologyId": ontology_id,
        "releaseId": version.id,
        "versionNumber": version.version_number,
        "mappings": mappings,
        "linkMappings": link_mappings,
    }


# ── Validator port (composition-root injection) ──────────────────────


@runtime_checkable
class RemoteBindingValidator(Protocol):
    """Adapter over the integrated remote-asset domain, wired by main.

    ``accepts`` authorizes the user and returns the live typed metadata for
    the binding (same descriptor shape, full column spec).  ``sample`` is a
    bounded controlled read keyed by column name.
    """

    def accepts(self, db: Session, user: Any, descriptor: dict) -> dict: ...

    def sample(
            self, db: Session, user: Any, descriptor: dict, *,
            limit: int) -> list[dict]: ...


_validator: RemoteBindingValidator | None = None


def set_remote_binding_validator(validator: RemoteBindingValidator) -> None:
    """Composition-root registration (idempotent overwrite)."""
    global _validator
    if validator is None:
        raise RemoteBindingPortError(
            "remote_validator_invalid", "远程绑定验证端口不能为空")
    _validator = validator


def clear_remote_binding_validator() -> None:
    """Test hook restoring the unwired fail-closed default."""
    global _validator
    _validator = None


def remote_binding_validator() -> RemoteBindingValidator:
    if _validator is None:
        raise RemoteBindingPortError(
            "remote_validator_unavailable",
            "远程绑定验证端口未装配（组合根未注入 integrated 适配器），"
            "拒绝放行远程绑定")
    return _validator


def _adapter_error(code: str, exc: Exception) -> RemoteBindingError:
    return RemoteBindingError(
        code, f"{exc}", originalError=type(exc).__name__)


def _call_accepts(db: Session, user: Any, descriptor: dict) -> dict:
    validator = remote_binding_validator()
    try:
        live = validator.accepts(db, user, descriptor)
    except RemoteBindingError:
        raise
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        status = int(exc.status_code or 500)
        code = {
            401: "remote_asset_unauthorized",
            403: "remote_asset_unauthorized",
            404: "remote_asset_not_found",
        }.get(status, "remote_asset_unavailable")
        raise RemoteBindingError(
            code,
            str(detail.get("message") or exc.detail or exc),
            adapterCode=str(detail.get("code") or "") or None,
        ) from exc
    except Exception as exc:  # noqa: BLE001 - adapter boundary
        raise _adapter_error("remote_asset_unavailable", exc) from exc
    if not isinstance(live, dict):
        raise RemoteBindingError(
            "remote_metadata_invalid", "远程验证端口返回了非法元数据")
    return live


def freeze_remote_descriptor(
        db: Session, user: Any, dataset_id: str) -> dict:
    """Freeze the authoritative live asset metadata as a binding descriptor.

    The client never supplies source-schema metadata; the descriptor is
    resolved server-side through the injected validator.
    """
    dataset_id = str(dataset_id or "").strip()
    if not dataset_id:
        raise RemoteBindingError(
            "remote_dataset_missing", "远程绑定必须指定数据集")
    live = _call_accepts(db, user, {"datasetId": dataset_id})
    descriptor = normalize_remote_descriptor(live)
    if str(descriptor.get("datasetId") or "") != dataset_id:
        raise RemoteBindingError(
            "remote_descriptor_dataset_mismatch",
            "远程验证端口返回的资产不属于该数据集",
            requestedDatasetId=dataset_id,
            descriptorDatasetId=str(descriptor.get("datasetId") or ""),
        )
    return descriptor


def verify_remote_binding(
        db: Session, user: Any, descriptor: dict, *,
        sample: bool = False,
        sample_limit: int = REMOTE_SAMPLE_ROW_LIMIT) -> dict:
    """Fail-closed remote verification: authorization, metadata, drift.

    Drift is decided here (ontology contract), not in the adapter: the live
    fingerprint and identity fields must match the frozen descriptor exactly.
    The controlled read is bounded; row values are returned for in-memory
    checks only and must never be persisted by callers.
    """
    frozen = normalize_remote_descriptor(descriptor)
    live = normalize_remote_descriptor(
        _call_accepts(db, user, frozen))
    for field in ("assetId", "datasetId", "connectionId",
                  "schemaName", "tableName"):
        if str(live.get(field) or "") != str(frozen.get(field) or ""):
            raise RemoteBindingError(
                "remote_binding_identity_changed",
                f"远程绑定资产身份已变化（{field}），请重新冻结远程绑定",
                field=field,
                expectedValue=str(frozen.get(field) or ""),
                actualValue=str(live.get(field) or ""),
            )
    if str(live.get("schemaFingerprint") or "") != str(
            frozen.get("schemaFingerprint") or ""):
        raise RemoteBindingError(
            "remote_binding_schema_drift",
            "远程资产 schema 指纹与冻结描述符不一致（schema drift），"
            "已拒绝放行；请刷新资产 schema 后重新绑定并重新试跑",
            expectedFingerprint=str(frozen.get("schemaFingerprint") or ""),
            actualFingerprint=str(live.get("schemaFingerprint") or ""),
        )
    result: dict[str, Any] = {
        "descriptor": frozen,
        "live": live,
        "sampleRows": [],
        "sampleRowCount": 0,
    }
    if sample:
        validator = remote_binding_validator()
        try:
            rows = validator.sample(
                db, user, frozen, limit=max(1, int(sample_limit)))
        except RemoteBindingError:
            raise
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            raise RemoteBindingError(
                "remote_sample_unavailable",
                str(detail.get("message") or exc.detail or exc),
                adapterCode=str(detail.get("code") or "") or None,
            ) from exc
        except Exception as exc:  # noqa: BLE001 - adapter boundary
            raise _adapter_error("remote_sample_unavailable", exc) from exc
        if not isinstance(rows, list):
            raise RemoteBindingError(
                "remote_sample_invalid", "远程受控读返回了非法行集")
        result["sampleRows"] = rows
        result["sampleRowCount"] = len(rows)
    return result


def remote_field_mapping_errors(
        field_mapping: dict | None, descriptor: dict, *,
        kind: str = "mapping", item_id: str = "", name: str = "") -> list[dict]:
    """Structural contract errors between a remote mapping and its descriptor.

    Used by the definition workflows, the isolated trial, and the release
    mapping contract so all three share one exact rule set.
    """

    def error(code: str, message: str, **context: Any) -> dict:
        payload = {"code": code, "kind": kind, "id": item_id,
                   "name": name, "message": message}
        payload.update({key: value for key, value in context.items()
                        if value is not None})
        return payload

    errors: list[dict] = []
    frozen = normalize_remote_descriptor(descriptor)
    columns = {
        str(column["name"]) for column in frozen.get("columns") or []
        if isinstance(column, dict) and column.get("name")
    }
    pk_value = str((field_mapping or {}).get("__primary_key__") or "")
    pk_columns = [column for column in pk_value.split(",") if column]
    asset_pk = [str(column) for column in frozen.get("primaryKey") or []]
    if not pk_columns:
        errors.append(error(
            "remote_primary_key_missing",
            "远程绑定未声明主键列，无法证明对象身份"))
    elif pk_columns != asset_pk:
        errors.append(error(
            "remote_pk_contract_mismatch",
            f"远程绑定主键 {pk_columns} 与冻结资产主键 {asset_pk} 不一致",
            declaredPrimaryKey=pk_columns, assetPrimaryKey=asset_pk))
    used_columns = [
        str(column) for column in (field_mapping or {})
        if not str(column).startswith("__")
    ]
    missing = [column for column in used_columns if column not in columns]
    if missing:
        errors.append(error(
            "remote_mapping_column_missing",
            f"字段映射引用了冻结列清单之外的列：{missing}",
            columns=missing, availableColumns=sorted(columns)))
    unmapped_pk = [column for column in pk_columns
                   if column not in used_columns]
    if unmapped_pk:
        errors.append(error(
            "remote_pk_not_mapped",
            f"远程主键列 {unmapped_pk} 未映射到本体属性",
            columns=unmapped_pk))
    return errors


def _remote_auto_apply_flags(field_mapping: dict) -> list[str]:
    return [
        key for key in ("__auto_apply_on_review__", "__auto_apply_on_version__")
        if (field_mapping or {}).get(key)
    ]


def _raise_workspace_remote_errors(errors: list[dict]) -> None:
    if errors:
        raise HTTPException(422, detail={
            "code": "remote_binding_contract_failed",
            "message": "远程绑定契约校验未通过",
            "errors": errors,
        })


def remote_column_types(descriptor: dict) -> dict[str, str]:
    """Normalized mapping-type vocabulary for a frozen descriptor's columns.

    Uses the same normalization as the lake contract so remote and local
    mappings share one compatibility rule set.
    """
    from app.ontologies.mappings.request_validation import _normal_mapping_type

    frozen = normalize_remote_descriptor(descriptor)
    return {
        str(column["name"]): _normal_mapping_type(column.get("type"))
        for column in frozen.get("columns") or []
        if isinstance(column, dict) and column.get("name")
    }


_REMOTE_ERROR_STATUS = {
    "remote_validator_unavailable": 503,
    "remote_validator_invalid": 503,
    "remote_asset_unauthorized": 403,
    "remote_asset_not_found": 404,
    "remote_asset_unavailable": 503,
    "remote_sample_unavailable": 503,
    "remote_metadata_invalid": 503,
}


def remote_binding_http_error(exc: RemoteBindingError) -> HTTPException:
    """Map a remote-binding failure to an explicit HTTP error."""
    status = _REMOTE_ERROR_STATUS.get(exc.code, 422)
    return HTTPException(status, detail=exc.detail())


def remote_dataset_ids(db: Session, ontology_id: str) -> set[str]:
    """该本体下被远程对象映射绑定的数据集（本地关系物化不可消费）。"""
    from app.ontologies.mappings.models import OntologyMapping

    return {
        mapping.curated_dataset_id
        for mapping in db.query(OntologyMapping).filter(
            OntologyMapping.ontology_id == ontology_id,
            OntologyMapping.curated_dataset_id.isnot(None),
        ).all()
        if mapping_binding_mode(mapping.field_mapping) == BINDING_MODE_REMOTE
    }


def reject_local_link_on_remote_dataset(
        db: Session, ontology_id: str,
        dataset_ids: set[str | None]) -> None:
    """本地关系物化不能引用远程绑定数据集（过期同步副本会造假边）。"""
    offenders = {
        str(dataset_id) for dataset_id in dataset_ids
        if dataset_id and str(dataset_id) in remote_dataset_ids(
            db, ontology_id)
    }
    if offenders:
        raise HTTPException(
            422,
            detail={
                "code": "link_dataset_remote_bound",
                "message": (
                    "关系端点/连接表引用了远程绑定数据集；本地关系物化不可用，"
                    "请改用远程关系定义（binding_mode=remote）或解绑远程映射"
                ),
                "dataset_ids": sorted(offenders),
            },
        )


def create_remote_link_definition(db: Session, ontology_id: str, body) -> dict:
    """远程关系定义：两端均远程绑定，仅验证冻结契约，不做本地物化/预览。

    远程关系沿版本正门（同本地 LinkMapping）：定义冻结 src/tgt 数据集与
    srcKey/tgtKey，查询期由主协调做参数化 eq JOIN；图投影不产生假边实例。
    """
    from app.models.ontology_formal import LinkType
    from app.ontologies.mappings.models import (
        OntologyLinkMapping,
        OntologyMapping,
    )

    if body.auto_apply_on_version:
        raise HTTPException(
            422,
            detail={
                "code": "remote_auto_apply_forbidden",
                "message": "远程绑定不参与本地自动灌入，不能开启自动触发订阅",
            },
        )
    if body.edge_dataset_id:
        raise HTTPException(
            422,
            detail={
                "code": "remote_link_edge_forbidden",
                "message": "远程关系定义暂不支持连接表（胖边）",
            },
        )
    if any(
            not str(key).startswith("__")
            for key in (body.field_mapping or {})
    ):
        raise HTTPException(
            422,
            detail={
                "code": "remote_link_properties_forbidden",
                "message": "远程关系定义不带边属性（无连接表）",
            },
        )
    if not body.link_type_id:
        raise HTTPException(
            422,
            detail={
                "code": "remote_link_type_required",
                "message": "远程关系定义必须显式绑定 LinkType",
            },
        )
    link_type = (
        db.query(LinkType)
        .filter(
            LinkType.id == body.link_type_id,
            LinkType.ontology_id == ontology_id,
        )
        .first()
    )
    if link_type is None:
        raise HTTPException(422, "绑定的 LinkType 不存在")

    def remote_endpoint(dataset_id: str, object_type_id: str | None):
        for mapping in db.query(OntologyMapping).filter(
                OntologyMapping.ontology_id == ontology_id,
                OntologyMapping.curated_dataset_id == dataset_id).all():
            if mapping_binding_mode(
                    mapping.field_mapping) != BINDING_MODE_REMOTE:
                continue
            if object_type_id and mapping.target_object_type_id != object_type_id:
                continue
            try:
                return remote_descriptor(mapping.field_mapping)
            except RemoteBindingError:
                continue
        return None

    failures: list[dict] = []
    for role, dataset_id, key, object_type_id in (
            ("source", body.src_dataset_id, body.src_key,
             link_type.source_object_type_id),
            ("target", body.tgt_dataset_id, body.tgt_key,
             link_type.target_object_type_id),
    ):
        descriptor = remote_endpoint(dataset_id, object_type_id)
        if descriptor is None:
            failures.append({
                "role": role,
                "reason": "remote_endpoint_mapping_missing",
                "dataset_id": dataset_id,
            })
            continue
        columns = {
            str(column.get("name"))
            for column in descriptor.get("columns") or []
            if isinstance(column, dict)
        }
        if not key or str(key) not in columns:
            failures.append({
                "role": role,
                "reason": "remote_link_key_missing",
                "field": "srcKey" if role == "source" else "tgtKey",
                "key": str(key or ""),
            })
    if failures:
        raise HTTPException(
            422,
            detail={
                "code": "remote_link_endpoint_invalid",
                "message": "远程关系端点校验未通过；src/tgt 数据集必须已被"
                           "匹配 LinkType 类型的远程对象映射绑定，且端点键"
                           "存在于冻结列清单",
                "errors": failures,
            },
        )

    existing = (
        db.query(OntologyLinkMapping)
        .filter(
            OntologyLinkMapping.ontology_id == ontology_id,
            OntologyLinkMapping.link_type_id == body.link_type_id,
        )
        .first()
    )
    if existing is not None:
        raise HTTPException(
            409,
            detail={
                "code": "link_mapping_already_exists",
                "message": (
                    "该 LinkType 已存在关系映射；请维护现有映射，不要重复创建。"
                ),
                "link_mapping_id": existing.id,
            },
        )
    link_mapping = OntologyLinkMapping(
        ontology_id=ontology_id,
        src_dataset_id=body.src_dataset_id,
        tgt_dataset_id=body.tgt_dataset_id,
        relation_type=body.relation_type,
        src_key=body.src_key,
        tgt_key=body.tgt_key,
        link_type_id=body.link_type_id,
        edge_dataset_id=None,
        field_mapping={BINDING_MODE_KEY: BINDING_MODE_REMOTE},
        status="active",
    )
    db.add(link_mapping)
    db.commit()
    db.refresh(link_mapping)
    return {
        "link_mapping_id": link_mapping.id,
        "relation_type": link_mapping.relation_type,
        "match_count": None,
        "edge_dataset_id": None,
        "edge_properties": [],
        "binding_mode": BINDING_MODE_REMOTE,
    }


def reject_remote_auto_apply_flags(*flags: bool) -> None:
    """远程绑定不参与本地自动灌入；任何订阅请求显式拒绝。"""
    if any(flags):
        raise HTTPException(
            422,
            detail={
                "code": "remote_auto_apply_forbidden",
                "message": "远程绑定不参与本地自动灌入，不能开启自动触发订阅",
            },
        )


def apply_remote_definition_keys(
        field_mapping: dict, descriptor: dict, declared_pk: str) -> dict:
    """把冻结远程定义写入映射定义（服务端唯一写入口）。"""
    field_mapping[BINDING_MODE_KEY] = BINDING_MODE_REMOTE
    field_mapping[REMOTE_DESCRIPTOR_KEY] = descriptor
    field_mapping["__primary_key__"] = declared_pk
    field_mapping["__pk_source__"] = "remote"
    return field_mapping


def strip_remote_definition_keys(field_mapping: dict) -> dict:
    """remote → local 切换时剥离冻结键（湖契约由调用方恢复）。"""
    field_mapping.pop(BINDING_MODE_KEY, None)
    field_mapping.pop(REMOTE_DESCRIPTOR_KEY, None)
    return field_mapping


def reject_remote_link_auto_apply(
        field_mapping: dict | None, requested: bool) -> None:
    """远程关系定义上开启版本自动灌入订阅 → 显式拒绝。"""
    if requested and link_mapping_binding_mode(field_mapping) == (
            BINDING_MODE_REMOTE):
        reject_remote_auto_apply_flags(True)


def reject_mixed_binding_for_type(
        db: Session, ontology_id: str, target_type, *, remote: bool) -> None:
    """同一 ObjectType 不允许本地与远程绑定混用（查询数据面会分裂）。"""
    from app.ontologies.mappings.models import OntologyMapping

    if target_type is None:
        return
    conflicts = []
    for mapping in db.query(OntologyMapping).filter(
            OntologyMapping.ontology_id == ontology_id,
            OntologyMapping.target_object_type_id == target_type.id).all():
        mode = mapping_binding_mode(mapping.field_mapping)
        if (mode == BINDING_MODE_REMOTE) != remote:
            conflicts.append({"mapping_id": mapping.id, "binding_mode": mode})
    if conflicts:
        raise HTTPException(
            422,
            detail={
                "code": "object_type_binding_mode_conflict",
                "message": (
                    "同一对象实体不能同时绑定本地与远程数据面；"
                    "请统一绑定模式后重试"
                ),
                "conflicts": conflicts,
            },
        )


def reject_remote_dataset_link_consumers(
        db: Session, ontology_id: str, dataset_id: str) -> None:
    """冻结远程绑定时，引用该数据集的本地关系物化必须先移除。"""
    from sqlalchemy import or_

    from app.ontologies.mappings.models import OntologyLinkMapping

    consumers = [
        link for link in db.query(OntologyLinkMapping).filter(
            OntologyLinkMapping.ontology_id == ontology_id,
            or_(
                OntologyLinkMapping.src_dataset_id == dataset_id,
                OntologyLinkMapping.tgt_dataset_id == dataset_id,
                OntologyLinkMapping.edge_dataset_id == dataset_id,
            ),
        ).all()
        if link_mapping_binding_mode(link.field_mapping) != BINDING_MODE_REMOTE
    ]
    if consumers:
        raise HTTPException(
            422,
            detail={
                "code": "link_dataset_remote_bound",
                "message": (
                    "该数据集存在本地关系映射引用；远程绑定后本地关系物化"
                    "不可用，请先删除或改远程关系定义"
                ),
                "link_mapping_ids": [link.id for link in consumers],
            },
        )


def assert_remote_mapping_types_compatible(
        descriptor: dict, target_type, field_mapping: dict) -> None:
    """对冻结远程列执行与本地同源的类型兼容校验。"""
    from app.ontologies.mappings.request_validation import (
        _mapping_types_compatible,
        _normal_mapping_type,
    )

    if target_type is None:
        return
    source_types = remote_column_types(descriptor)
    properties = {
        str(item.get("name")): item
        for item in (getattr(target_type, "properties", None) or [])
        if isinstance(item, dict) and item.get("name")
    }
    failures: list[dict] = []
    for source, target in (field_mapping or {}).items():
        source_name, target_name = str(source), str(target)
        source_type = source_types.get(source_name)
        target_property = properties.get(target_name)
        if source_type is None:
            failures.append({
                "source": source_name, "target": target_name,
                "reason": "source_column_not_found",
            })
            continue
        if target_property is None:
            failures.append({
                "source": source_name, "target": target_name,
                "reason": "target_property_not_found",
            })
            continue
        target_type_name = _normal_mapping_type(target_property.get("type"))
        if not _mapping_types_compatible(source_type, target_type_name):
            failures.append({
                "source": source_name, "source_type": source_type,
                "target": target_name, "target_type": target_type_name,
                "reason": "type_mismatch",
            })
    if failures:
        raise HTTPException(
            422,
            detail={
                "code": "mapping_type_mismatch",
                "message": "字段映射包含不存在的字段或不兼容的数据类型，请修正后再保存。",
                "errors": failures,
            },
        )


def prepare_remote_mapping_definition(
        db: Session, user: Any, *,
        dataset_id: str, target_type, field_mapping: dict | None,
        ignored_fields: list[str] | None,
        primary_key_column: str | None,
        rebind: bool, switching_to_remote: bool,
        check_mixed_binding: bool = True,
        item_id: str = "", name: str = "",
        ontology_id: str = "") -> tuple[dict, str]:
    """Shared remote create/update preparation: freeze + identity + fences.

    Returns ``(descriptor, declared_pk)``.  Raises explicit HTTP errors:
    the descriptor is always frozen server-side, the primary key comes from
    the frozen asset contract, and type compatibility plus the structural
    contract are enforced against the frozen columns.  ``ontology_id`` is
    required when the mixed-binding/link-consumer fences run.
    """
    from app.ontologies.mappings.request_validation import (
        _assert_client_primary_key_matches,
        _assert_ignored_fields_do_not_hide_identity,
    )

    field_mapping = field_mapping or {}
    business_mapping = {
        str(key): value for key, value in field_mapping.items()
        if not str(key).startswith("__")
    }
    if rebind:
        if user is None:
            raise HTTPException(
                401,
                detail={
                    "code": "remote_user_required",
                    "message": "远程绑定冻结需要认证用户执行授权验证",
                },
            )
        try:
            descriptor = freeze_remote_descriptor(db, user, dataset_id)
        except RemoteBindingError as exc:
            raise remote_binding_http_error(exc) from exc
    else:
        try:
            descriptor = remote_descriptor(field_mapping)
        except RemoteBindingError as exc:
            raise remote_binding_http_error(exc) from exc
    declared_pk = ",".join(
        str(column) for column in descriptor.get("primaryKey") or [])
    _assert_ignored_fields_do_not_hide_identity(
        ignored_fields or [], declared_pk)
    _assert_client_primary_key_matches(
        primary_key_column, declared_pk, dataset_id)
    assert_remote_mapping_types_compatible(
        descriptor, target_type, business_mapping)
    structural_errors = remote_field_mapping_errors(
        {"__primary_key__": declared_pk, **business_mapping},
        descriptor, kind="mapping", item_id=item_id, name=name)
    if structural_errors:
        raise HTTPException(422, detail={
            "code": "remote_binding_contract_failed",
            "message": "远程绑定契约校验未通过",
            "errors": structural_errors,
        })
    if check_mixed_binding:
        reject_mixed_binding_for_type(
            db, ontology_id, target_type, remote=True)
    if switching_to_remote:
        reject_remote_dataset_link_consumers(db, ontology_id, dataset_id)
    return descriptor, declared_pk


def normalize_workspace_remote_entries(db: Session, user: Any, snap: dict) -> None:
    """Server-freeze remote definitions in a workspace mapping save.

    ``snap`` is the merged draft snapshot (mutated in place).  Remote object
    mappings are always re-frozen from the live asset through the injected
    validator — the client-supplied ``remote`` / ``__remote__`` payload is
    never trusted.  Pure-local entries without remote keys keep their
    established reserved-key semantics completely untouched.
    """
    from app.ontologies.mappings.request_validation import (
        _canonical_primary_key,
    )

    errors: list[dict] = []
    mappings = snap.get("mappings") or []
    link_mappings = snap.get("linkMappings") or []

    def client_binding_mode(entry: dict, field_mapping: dict) -> str | None:
        raw = entry.get("bindingMode")
        if raw is None:
            raw = field_mapping.get(BINDING_MODE_KEY)
        if raw is None:
            return None
        return normalize_binding_mode(raw)

    remote_by_dataset: dict[str, dict] = {}

    for entry in mappings:
        if not isinstance(entry, dict):
            continue
        field_mapping = entry.get("fieldMapping")
        field_mapping = (dict(field_mapping)
                         if isinstance(field_mapping, dict) else {})
        had_remote_keys = (
            field_mapping.get(BINDING_MODE_KEY) == BINDING_MODE_REMOTE
            or REMOTE_DESCRIPTOR_KEY in field_mapping
            or "remote" in entry
        )
        mode = client_binding_mode(entry, field_mapping)
        item_id = str(entry.get("id") or "")
        label = str(entry.get("entityClass") or item_id)
        try:
            if mode is None and not had_remote_keys:
                # Pure-local entry: existing semantics, untouched.
                continue
            if mode is None:
                mode = BINDING_MODE_REMOTE
            if mode == BINDING_MODE_REMOTE:
                dataset_id = str(entry.get("curatedDatasetId") or "").strip()
                if not dataset_id:
                    errors.append({
                        "code": "mapping_dataset_missing", "kind": "mapping",
                        "id": item_id, "name": label,
                        "message": "远程映射必须绑定数据集（curatedDatasetId）",
                    })
                    continue
                flags = _remote_auto_apply_flags(field_mapping)
                if flags:
                    errors.append({
                        "code": "remote_auto_apply_forbidden",
                        "kind": "mapping", "id": item_id, "name": label,
                        "field": f"fieldMapping.{flags[0]}",
                        "message": "远程绑定不参与本地自动灌入，"
                                   "不能开启自动触发订阅",
                    })
                    continue
                try:
                    frozen = freeze_remote_descriptor(db, user, dataset_id)
                except RemoteBindingError as exc:
                    errors.append({
                        "code": exc.code, "kind": "mapping",
                        "id": item_id, "name": label,
                        "message": f"Mapping「{label}」远程绑定冻结失败："
                                   f"{exc.message}",
                        **exc.context,
                    })
                    continue
                field_mapping.pop(REMOTE_DESCRIPTOR_KEY, None)
                field_mapping[BINDING_MODE_KEY] = BINDING_MODE_REMOTE
                field_mapping[REMOTE_DESCRIPTOR_KEY] = frozen
                field_mapping["__primary_key__"] = ",".join(
                    str(column) for column in frozen.get("primaryKey") or [])
                field_mapping["__pk_source__"] = "remote"
                entry["fieldMapping"] = field_mapping
                entry.pop("remote", None)
                entry["bindingMode"] = BINDING_MODE_REMOTE
                entry["remote"] = frozen
                errors.extend(remote_field_mapping_errors(
                    field_mapping, frozen,
                    kind="mapping", item_id=item_id, name=label))
                remote_by_dataset.setdefault(dataset_id, frozen)
            else:
                if not had_remote_keys:
                    # Explicit local on a local entry: unchanged semantics.
                    continue
                # remote → local switch: strip frozen keys, restore the lake
                # identity contract from the authoritative dataset schema.
                dataset_id = str(entry.get("curatedDatasetId") or "").strip()
                try:
                    declared_pk = _canonical_primary_key(db, dataset_id)
                except HTTPException as exc:
                    detail = exc.detail if isinstance(exc.detail, dict) else {}
                    errors.append({
                        "code": str(detail.get("code") or exc.status_code),
                        "kind": "mapping", "id": item_id, "name": label,
                        "message": f"Mapping「{label}」切回本地绑定时失败："
                                   f"{detail.get('message') or exc.detail}",
                    })
                    continue
                for key in (BINDING_MODE_KEY, REMOTE_DESCRIPTOR_KEY):
                    field_mapping.pop(key, None)
                field_mapping["__primary_key__"] = declared_pk
                field_mapping["__pk_source__"] = "lake"
                entry["fieldMapping"] = field_mapping
                entry.pop(BINDING_MODE_KEY, None)
                entry.pop("remote", None)
                entry["bindingMode"] = BINDING_MODE_LOCAL
        except RemoteBindingError as exc:
            errors.append({
                "code": exc.code, "kind": "mapping", "id": item_id,
                "name": label, "message": exc.message, **exc.context,
            })

    _raise_workspace_remote_errors(errors)

    # One ObjectType cannot mix local and remote bindings: the query data
    # plane would split between materialized instances and live rows.
    object_types = [
        item for item in (snap.get("objectTypes") or [])
        if isinstance(item, dict)
    ]
    modes_by_type: dict[str, set[str]] = {}
    ids_by_type: dict[str, list[str]] = {}
    for entry in mappings:
        if not isinstance(entry, dict):
            continue
        target = _resolve_object_type(entry, object_types)
        if target is None:
            continue
        field_mapping = entry.get("fieldMapping")
        field_mapping = (field_mapping
                         if isinstance(field_mapping, dict) else {})
        mode = client_binding_mode(entry, field_mapping) or BINDING_MODE_LOCAL
        type_id = str(target.get("id") or "")
        modes_by_type.setdefault(type_id, set()).add(mode)
        ids_by_type.setdefault(type_id, []).append(str(entry.get("id") or ""))
    for type_id, modes in sorted(modes_by_type.items()):
        if len(modes) > 1:
            errors.append({
                "code": "object_type_binding_mode_conflict",
                "kind": "objectType", "id": type_id, "name": "",
                "message": "同一 ObjectType 不能同时绑定本地与远程数据面；"
                           "请统一绑定模式",
                "mappingIds": ids_by_type.get(type_id, []),
            })
    _raise_workspace_remote_errors(errors)

    # Remote bindings by (dataset, object type) for link endpoint resolution.
    remote_endpoint: dict[tuple[str, str], dict] = {}
    for entry in mappings:
        if not isinstance(entry, dict):
            continue
        field_mapping = entry.get("fieldMapping")
        field_mapping = (field_mapping
                         if isinstance(field_mapping, dict) else {})
        if client_binding_mode(entry, field_mapping) != BINDING_MODE_REMOTE:
            continue
        target = _resolve_object_type(entry, object_types)
        dataset_id = str(entry.get("curatedDatasetId") or "")
        if target is not None and dataset_id:
            remote_endpoint[(dataset_id, str(target.get("id") or ""))] = (
                field_mapping.get(REMOTE_DESCRIPTOR_KEY) or {})
    remote_datasets = {
        dataset for dataset, _ in remote_endpoint
    }

    link_types = {
        str(item.get("id")): item
        for item in (snap.get("linkTypes") or [])
        if isinstance(item, dict) and item.get("id")
    }

    for entry in link_mappings:
        if not isinstance(entry, dict):
            continue
        field_mapping = entry.get("fieldMapping")
        field_mapping = (dict(field_mapping)
                         if isinstance(field_mapping, dict) else {})
        had_remote_keys = (
            field_mapping.get(BINDING_MODE_KEY) == BINDING_MODE_REMOTE
        )
        mode = client_binding_mode(entry, field_mapping)
        item_id = str(entry.get("id") or "")
        label = str(entry.get("relationType") or item_id)
        if mode is None and not had_remote_keys:
            # Pure-local link: only the remote-dataset fence is new.
            for role, field in (("source", "srcDatasetId"),
                                ("target", "tgtDatasetId"),
                                ("edge", "edgeDatasetId")):
                if str(entry.get(field) or "") in remote_datasets:
                    errors.append({
                        "code": "link_dataset_remote_bound",
                        "kind": "linkMapping", "id": item_id, "name": label,
                        "field": field,
                        "message": f"LinkMapping「{label}」的 {role} 数据集"
                                   "已被远程绑定，本地关系物化不可用；"
                                   "请改为远程关系定义或解绑远程映射",
                    })
            continue
        if mode is None:
            mode = BINDING_MODE_REMOTE
        if mode != BINDING_MODE_REMOTE:
            if had_remote_keys:
                field_mapping.pop(BINDING_MODE_KEY, None)
                entry["fieldMapping"] = field_mapping
                entry.pop("bindingMode", None)
            continue
        link_type_id = str(entry.get("linkTypeId") or "")
        link_type = link_types.get(link_type_id)
        if link_type is None:
            errors.append({
                "code": "remote_link_type_required", "kind": "linkMapping",
                "id": item_id, "name": label, "field": "linkTypeId",
                "message": "远程关系定义必须显式绑定 LinkType",
            })
            continue
        if entry.get("edgeDatasetId"):
            errors.append({
                "code": "remote_link_edge_forbidden", "kind": "linkMapping",
                "id": item_id, "name": label, "field": "edgeDatasetId",
                "message": "远程关系定义暂不支持连接表（胖边）",
            })
            continue
        business_keys = [
            key for key in field_mapping if not str(key).startswith("__")
        ]
        if business_keys:
            errors.append({
                "code": "remote_link_properties_forbidden",
                "kind": "linkMapping", "id": item_id, "name": label,
                "field": "fieldMapping",
                "message": "远程关系定义不带边属性（无连接表）",
            })
            continue
        if _remote_auto_apply_flags(field_mapping):
            errors.append({
                "code": "remote_auto_apply_forbidden", "kind": "linkMapping",
                "id": item_id, "name": label,
                "field": "fieldMapping.__auto_apply_on_version__",
                "message": "远程绑定不参与本地自动灌入，不能开启自动触发订阅",
            })
            continue
        endpoint_errors = False
        endpoint_descriptors: dict[str, dict] = {}
        for role, dataset_field, key_field, type_field in (
                ("source", "srcDatasetId", "srcKey", "sourceObjectTypeId"),
                ("target", "tgtDatasetId", "tgtKey", "targetObjectTypeId")):
            dataset_id = str(entry.get(dataset_field) or "")
            endpoint = remote_endpoint.get(
                (dataset_id, str(link_type.get(type_field) or "")))
            if endpoint is None:
                errors.append({
                    "code": "remote_link_endpoint_mapping_required",
                    "kind": "linkMapping", "id": item_id, "name": label,
                    "field": dataset_field,
                    "message": f"远程关系 {role} 数据集未被匹配 LinkType "
                               f"{role} 类型的远程对象映射绑定",
                })
                endpoint_errors = True
                continue
            key = str(entry.get(key_field) or "")
            columns = {
                str(column.get("name"))
                for column in endpoint.get("columns") or []
                if isinstance(column, dict)
            }
            if not key or key not in columns:
                errors.append({
                    "code": "remote_link_key_missing", "kind": "linkMapping",
                    "id": item_id, "name": label, "field": key_field,
                    "message": f"远程关系 {role} 端键「{key or '（空）'}」"
                               "不在冻结列清单中",
                })
                endpoint_errors = True
            endpoint_descriptors[role] = endpoint
        if endpoint_errors:
            continue
        field_mapping[BINDING_MODE_KEY] = BINDING_MODE_REMOTE
        entry["fieldMapping"] = field_mapping
        entry["bindingMode"] = BINDING_MODE_REMOTE

    _raise_workspace_remote_errors(errors)
    # Local links referencing remote-bound datasets fail closed above.
