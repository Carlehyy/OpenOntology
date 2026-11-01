"""受控动作三源资产写适配器 — 实现 ontologies 侧 ActionWritePort 协议。

组合根（main.py）装配：

    from app.data_channel.action_write_port import AssetWritePortAdapter
    from app.ontologies.agent_confirmation import write_port as action_write_port
    action_write_port.set_action_write_port(AssetWritePortAdapter())

语义（与确认协议服务配套）：

- resolve_write（prepare 阶段，零业务写）：按 release 快照映射唯一命中数据
  集/远程资产（经 remote_binding.snapshot_entry_binding 规范解析，兼容历史
  field_mapping 描述符），验证字段映射/主键/派生属性/源写权限，服务端读取
  当前 token/version 后冻结完整计划。任何拒绝都是 WritePortError。
- apply_write（execute 阶段）：重验资产湖写角色与源端身份后单行写 —
  manual → edit_rows 产生新版本（base_version CAS；回执缺失按 unknown）；
  curated → 复用/新建 pending review + batch_edit_rows 单事务批量（不自动
  approve，批准前查询不可见）；integrated → mutate_row 单行
  insert/update/delete（row token CAS、幂等键去重、commit unknown 保持
  unknown 不自动重放、schema fingerprint 漂移拒绝）。
- inspect_write（只读回访）：curated 按执行回执 reviewId 绑定真实 review
  刷新 outcome（approved→applied / rejected→failed / 新版本后旧批准
  superseded→failed）；manual/integrated 返回 None（以执行时回执为准，
  不做源端主动探测）。

显式不支持（拒绝且源侧零变更）：curated 行删除（审核模型仅字段编辑）、
跨库多资产组合、聚合/派生属性写、无稳定主键写、未映射属性写。
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.data_channel.datasets.models import Dataset, DatasetVersion
from app.ontologies.agent_confirmation import write_port as WP
from app.ontologies.formal_modeling.models import ObjectInstance

_PLAN_VERSION = WP.PLAN_VERSION


def _write_error(exc: Exception, *, default_code: str) -> WP.WritePortError:
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        code = str(detail.get("code") or default_code)
        message = str(detail.get("message") or detail.get("detail") or exc)
        http_status = getattr(exc, "status_code", 422) or 422
        # extra 只保留与构造器形参不冲突的键：源 detail 里的 "status" 是
        # 领域状态（如 review status），与 HTTP status 形参冲突必须让位 —
        # HTTP 权威状态取自 status_code，领域状态改写为 detailStatus 保留，
        # 其余键（dataset_version_id/review_id/…）原样透传，不得丢失。
        extra = {}
        for key, value in detail.items():
            if key in ("code", "message", "mutation", "status"):
                continue
            extra[key] = value
        if "status" in detail:
            extra["detailStatus"] = detail["status"]
        mutation = detail.get("mutation")
        if isinstance(mutation, dict):
            extra["receiptId"] = mutation.get("id")
            extra["receiptStatus"] = mutation.get("status")
        return WP.WritePortError(code, message, status=http_status, **extra)
    return WP.WritePortError(
        default_code, str(getattr(exc, "detail", None) or exc))




def _source_config_fingerprint(db, connection_id: str) -> Optional[str]:
    """连接配置的单向指纹（不回显任何配置值）；执行时重验同连接配置漂移。"""
    import hashlib
    import json as _json

    from app.data_channel.connections.models import Connection

    try:
        conn = db.query(Connection).filter(
            Connection.id == str(connection_id)).first()
    except Exception:  # noqa: BLE001 - 连接表不可用时无法冻结身份，拒绝
        return None
    if conn is None:
        return None
    material = {
        "id": str(conn.id),
        "kind": str(getattr(conn, "kind", "") or ""),
        "status": str(getattr(conn, "status", "") or ""),
        "config": conn.config or {},
    }
    return hashlib.sha256(
        _json.dumps(material, ensure_ascii=False, sort_keys=True,
                    default=str).encode("utf-8")).hexdigest()


def _require_lake_write_role(user) -> None:
    """复刻资产湖写口径（access.asset_lake_access_guard）：viewer 只读。"""
    if str(getattr(user, "role", "viewer") or "viewer") not in {
            "admin", "editor"}:
        raise WP.WritePortError(
            "source_write_denied",
            "资产湖对 viewer 只读；该确认发起人无数据写角色",
            status=403)


def _business_field_map(field_mapping: dict | None) -> dict[str, str]:
    """列 → 本体属性（剔除保留键；值端必须是属性名）。"""
    return {
        str(column): str(target)
        for column, target in (field_mapping or {}).items()
        if not str(column).startswith("__") and target
    }


def _unique_reverse(field_map: dict[str, str]) -> dict[str, str]:
    """属性 → 列；同一属性被多列映射（来源多义）直接拒绝。"""
    reverse: dict[str, str] = {}
    for column, prop in field_map.items():
        if prop in reverse:
            raise WP.WritePortError(
                "mapping_ambiguous",
                f"属性「{prop}」被多列映射（{reverse[prop]}/{column}），"
                "属性来源多义拒绝资产写",
                status=409, property=prop)
        reverse[prop] = column
    return reverse


class AssetWritePortAdapter:
    """manual / curated / integrated 三源单行写适配器。"""

    @staticmethod
    def _entry_binding(entry: dict) -> tuple:
        """snapshot_entry_binding 的转译入口（RemoteBindingError → 端口错误）。"""
        from app.ontologies.mappings.remote_binding import (
            RemoteBindingError, snapshot_entry_binding,
        )

        try:
            return snapshot_entry_binding(entry)
        except RemoteBindingError as exc:
            raise WP.WritePortError(
                "remote_descriptor_invalid", exc.message,
                status=422, **exc.context) from exc

    # ------------------------------------------------------------- resolve

    def resolve_write(self, db: Session, user, *, ontology_id: str,
                      release_id: str,
                      request: WP.OntologyWriteRequest) -> Optional[dict]:
        from app.ontologies.versions.models import OntologyVersion
        from app.ontologies.mappings.remote_binding import (
            snapshot_entry_binding,
        )

        release = db.query(OntologyVersion).filter(
            OntologyVersion.id == release_id,
            OntologyVersion.ontology_id == ontology_id,
            OntologyVersion.node_kind == "release",
            OntologyVersion.lifecycle_status == "released",
        ).first()
        if release is None:
            raise WP.WritePortError(
                "release_not_found", "资产写解析需有效发布版本",
                status=409, releaseId=release_id)
        snapshot = release.snapshot_formal or {}
        object_types = {
            str(ot.get("id") or ""): ot
            for ot in (snapshot.get("objectTypes") or [])
            if isinstance(ot, dict)
        }
        type_def = object_types.get(str(request.object_type_id))
        if type_def is None:
            raise WP.WritePortError(
                "object_type_not_in_release",
                "写目标对象类型不在发布快照中",
                status=422, objectTypeId=request.object_type_id)

        matches = self._matching_mappings(
            snapshot, request.object_type_id, object_types)
        if not matches:
            return None
        if len(matches) > 1:
            raise WP.WritePortError(
                "mapping_ambiguous",
                "同一对象类型存在多个数据映射绑定（属性来源多义），拒绝资产写；"
                "读路径不受影响",
                status=409,
                mappingIds=sorted(str(m.get("id") or "") for m in matches))
        entry = matches[0]

        self._reject_computed_properties(type_def, request.property_values)
        field_map = _business_field_map(entry.get("fieldMapping"))
        reverse = _unique_reverse(field_map)
        binding_mode, _descriptor = self._entry_binding(entry)

        if binding_mode == "remote":
            return self._resolve_integrated(
                db, user, ontology_id, release_id, request, entry,
                field_map, reverse)
        return self._resolve_local_dataset(
            db, user, ontology_id, release_id, request, entry,
            field_map, reverse)

    @staticmethod
    def _matching_mappings(snapshot: dict, object_type_id: str,
                           object_types: dict) -> list[dict]:
        type_def = object_types.get(str(object_type_id)) or {}
        names = {str(type_def.get(key) or "") for key in ("name", "displayName")}
        matches = []
        for entry in snapshot.get("mappings") or []:
            if not isinstance(entry, dict):
                continue
            target = entry.get("targetObjectTypeId")
            if target and str(target) == str(object_type_id):
                matches.append(entry)
                continue
            entity = str(entry.get("entityClass") or "")
            if entity and entity in names:
                matches.append(entry)
        return matches

    @staticmethod
    def _reject_computed_properties(type_def: dict,
                                    property_values: dict) -> None:
        computed = {
            str(prop.get("name"))
            for prop in (type_def.get("properties") or [])
            if isinstance(prop, dict)
            and (prop.get("source") == "computed"
                 or bool(prop.get("computed")))
            and prop.get("name")
        }
        blocked = sorted(set(property_values) & computed)
        if blocked:
            raise WP.WritePortError(
                "computed_property_write_denied",
                f"派生/计算属性不能写入资产源：{blocked}",
                status=422, properties=blocked)

    # -------------------------------------------------------- integrated

    def _resolve_integrated(self, db, user, ontology_id, release_id,
                            request, entry, field_map,
                            reverse) -> dict:
        from app.data_channel.integrated import native
        from app.ontologies.mappings.remote_binding import (
            snapshot_entry_binding,
        )

        # 描述符契约错误转译为确定性 WritePortError，不裸穿确认链。
        _mode, descriptor = self._entry_binding(entry)
        asset_id = str(descriptor.get("assetId") or "")
        try:
            native.authorize(db, user, write=True)
            asset = native.get_asset(db, user, asset_id, write=True)
        except HTTPException as exc:
            raise _write_error(exc, default_code="source_write_denied") from exc
        dataset = db.get(Dataset, asset.dataset_id)
        if dataset is None or dataset.kind != "integrated":
            raise WP.WritePortError(
                "remote_binding_asset_invalid",
                "远程映射引用的资产不是已登记的集成资产",
                status=409, datasetId=str(asset.dataset_id))

        from app.data_channel.integrated.service import asset_projection

        projection = asset_projection(asset)
        pk_columns = [str(c) for c in descriptor.get("primaryKey") or []]
        operation = str(request.operation or WP.OPERATION_UPDATE)
        self._require_mapped_reads(request, reverse)
        if operation == WP.OPERATION_INSERT:
            key, values = self._split_insert_key(
                request, field_map, pk_columns)
        else:
            key = self._resolve_key_columns(
                db, request, reverse, pk_columns, require_target_ref=True)
            values = self._resolve_value_columns(request, reverse, pk_columns)
        self._verify_write_namespace(projection, pk_columns, key, values)
        pk_properties = [field_map[col] for col in pk_columns
                         if col in field_map]
        value_map = self._value_map(request, reverse)
        write_properties = sorted(
            set(request.property_values or {})
            | set(request.pending_value_mappings or {}))

        expected_token = None
        target_snapshot: Optional[dict] = None
        before: Optional[dict] = None
        if operation in (WP.OPERATION_UPDATE, WP.OPERATION_DELETE):
            row, fingerprint, table = self._read_integrated_row(
                db, user, asset, key)
            if row is None:
                raise WP.WritePortError(
                    "row_not_found",
                    "目标行在源端不存在（可能已被删除），拒绝写",
                    status=409, key=key)
            from app.data_channel.integrated.contracts import row_token
            from app.data_channel.integrated.native import normalize_row

            expected_token = row_token(normalize_row(row, table), fingerprint)
            # 有界目标快照：仅定位键、写入属性与待求值属性（不整行）。
            wanted_props = set(request.key_values) | set(request.property_values) \
                | set(request.pending_value_mappings) \
                | set(request.read_properties)
            wanted_columns = {reverse[prop] for prop in wanted_props
                              if prop in reverse}
            target_snapshot = {
                prop: row.get(col)
                for col, prop in field_map.items() if col in wanted_columns}
            before = {
                col: row.get(col)
                for col in wanted_columns if col not in key}
        elif operation == WP.OPERATION_INSERT:
            row, _, _ = self._read_integrated_row(db, user, asset, key)
            if row is not None:
                raise WP.WritePortError(
                    "row_already_exists",
                    "目标主键在源端已存在，拒绝插入",
                    status=409, key=key)

        return {
            "planVersion": _PLAN_VERSION,
            "assetKind": WP.ASSET_KIND_INTEGRATED,
            "ontologyId": ontology_id,
            "releaseId": release_id,
            "mappingId": str(entry.get("id") or ""),
            "objectTypeId": str(request.object_type_id),
            "datasetId": str(asset.dataset_id),
            "datasetName": getattr(dataset, "name", None),
            "assetId": asset.id,
            "connectionId": str(asset.connection_id),
            "schemaName": str(asset.schema_name or ""),
            "tableName": str(asset.table_name or ""),
            "schemaFingerprint": str(asset.schema_fingerprint or ""),
            "projectionFingerprint": (
                str(projection["fingerprint"]) if projection else None),
            "sourceConfigFingerprint": _source_config_fingerprint(
                db, asset.connection_id),
            "operation": operation,
            "key": key,
            "values": values,
            "valueMap": value_map,
            "expectedToken": expected_token,
            "before": before,
            "targetSnapshot": target_snapshot,
            "keyProperties": pk_properties,
            "properties": write_properties,
        }

    @staticmethod
    def _read_integrated_row(db, user, asset, key: dict):
        """按完整主键有界读取单行；返回 (row, fingerprint, table)。"""
        import sqlalchemy as sa

        from app.data_channel.integrated import native

        with native.open_read_scope(
                db, user, [asset.id], statement_timeout_ms=10000) as scope:
            meta = scope.meta[asset.id]
            table = scope.tables[asset.id]
            fingerprint = meta["schema_fingerprint"]
            projection = meta.get("projection")
            to_physical = (
                projection["logical_to_physical"] if projection else {})
            condition = sa.and_(*(
                table.c[to_physical.get(name, name)] == value
                for name, value in key.items()))
            statement = sa.select(table).where(condition).limit(2)
            row = None
            for batch in scope.iter_batches(statement, batch_size=2):
                row = batch[0] if batch else None
                break
            return row, fingerprint, table

    def _integrated_row_token(self, db, user, asset, key: dict, *,
                              missing_code: str,
                              missing_message: str) -> str:
        row, fingerprint, table = self._read_integrated_row(
            db, user, asset, key)
        if row is None:
            raise WP.WritePortError(
                missing_code, missing_message, status=409, key=key)
        from app.data_channel.integrated.contracts import row_token
        from app.data_channel.integrated.native import normalize_row

        return row_token(normalize_row(row, table), fingerprint)

    def _integrated_row_exists(self, db, user, asset, key: dict) -> bool:
        row, _, _ = self._read_integrated_row(db, user, asset, key)
        return row is not None


    @staticmethod
    def _split_insert_key(request: WP.OntologyWriteRequest,
                          field_map: dict[str, str],
                          pk_columns: list[str]) -> tuple[dict, dict]:
        """insert 主键派生：create 映射中的主键属性值进入 key，其余进 values。

        - targetRef.key 与 create 映射同列不同值 → primary_key_conflict；
        - 主键不完整（且无已展开默认值）→ complete_primary_key_required；
        - update/delete 仍禁改主键（_resolve_value_columns 路径）。
        """
        key: dict[str, Any] = {}
        if request.target_ref is not None:
            for prop, value in (request.key_values or {}).items():
                column = next((col for col, p in field_map.items()
                               if p == prop), None)
                if column is not None:
                    key[column] = value
        consumed_props: set[str] = set()
        for column in pk_columns:
            prop = field_map.get(column)
            if prop is None or prop not in (request.property_values or {}):
                continue
            value = request.property_values[prop]
            if column in key and str(key[column]) != str(value):
                raise WP.WritePortError(
                    "primary_key_conflict",
                    f"插入主键列「{column}」在 targetRef 与 create 映射中"
                    "取值不一致",
                    status=422, column=column)
            key[column] = value
            consumed_props.add(prop)
        missing = [column for column in pk_columns if column not in key]
        if missing:
            raise WP.WritePortError(
                "complete_primary_key_required",
                f"插入缺少完整主键 {missing}：主键值须来自 create 映射或"
                " targetRef.key（数据资产不自动生成行身份）",
                status=422, missing=missing)
        values = {
            column: request.property_values[prop]
            for column, prop in field_map.items()
            if prop in (request.property_values or {})
            and prop not in consumed_props
        }
        pending_props = set(request.pending_value_mappings or {})
        pending_pk = [field_map[c] for c in pk_columns
                      if field_map.get(c) in pending_props]
        if pending_pk:
            raise WP.WritePortError(
                "complete_primary_key_required",
                f"插入主键属性 {pending_pk} 的取值依赖目标行，但插入没有"
                "已有行可读",
                status=422, properties=pending_pk)
        return key, values

    @staticmethod
    def _verify_write_namespace(projection, pk_columns, key: dict,
                                values: dict) -> None:
        """写请求必须落在资产可寻址命名空间（projection 逻辑名优先）。"""
        if projection is None:
            return
        addressable = set(projection.get("logical_to_physical") or {})
        unknown = (set(key) | set(values)) - addressable
        if unknown:
            raise WP.WritePortError(
                "unknown_column",
                f"写入列 {sorted(unknown)} 不在资产逻辑视图的可寻址列内",
                status=422, unknownColumns=sorted(unknown))
        logical_pk = [str(c) for c in projection.get("primary_key") or []]
        if logical_pk and set(key) != set(logical_pk):
            raise WP.WritePortError(
                "complete_primary_key_required",
                f"配置资产的主键逻辑列为 {logical_pk}，与冻结定位键不一致",
                status=422, expected=logical_pk)

    @staticmethod
    def _require_mapped_reads(request: WP.OntologyWriteRequest,
                              reverse: dict[str, str]) -> None:
        """读取依赖属性必须已映射：未映射依赖在源读之前 422，绝不静默丢弃。"""
        unmapped = sorted(
            prop for prop in set(request.read_properties)
            | set(request.pending_value_mappings)
            if prop not in reverse)
        if unmapped:
            raise WP.WritePortError(
                "unknown_read_property",
                f"规则读取依赖的目标属性 {unmapped} 未映射到资产列，"
                "无法构造有界目标快照",
                status=422, properties=unmapped)

    @staticmethod
    def _value_map(request: WP.OntologyWriteRequest,
                   reverse: dict[str, str]) -> dict[str, str]:
        """属性 → 列（含待求值属性），供服务端在读取目标快照后合并延迟值。"""
        props = set(request.property_values or {}) \
            | set(request.pending_value_mappings or {})
        mapping = {}
        for prop in props:
            column = reverse.get(prop)
            if column is None:
                raise WP.WritePortError(
                    "unknown_write_column",
                    f"属性「{prop}」未映射到资产列，拒绝写",
                    status=422, property=prop)
            mapping[prop] = column
        return mapping

    # ------------------------------------------------------ local dataset

    def _resolve_local_dataset(self, db, user, ontology_id, release_id,
                               request, entry, field_map,
                               reverse) -> dict:
        dataset_id = str(entry.get("curatedDatasetId") or "")
        dataset = db.get(Dataset, dataset_id)
        if dataset is None:
            raise WP.WritePortError(
                "mapping_dataset_missing",
                "映射引用的数据集不存在",
                status=409, datasetId=dataset_id)
        is_curated = dataset.kind == "curated"
        if is_curated:
            pk_columns = self._curated_pk_columns(db, dataset_id)
            if str(request.operation or "") == WP.OPERATION_DELETE:
                raise WP.WritePortError(
                    "curated_delete_unsupported",
                    "成品数据集审核模型仅支持字段级编辑，不支持行删除；"
                    "如需删除请调整流水线源数据后重新入湖",
                    status=422, datasetId=dataset_id)
        else:
            from app.data_channel.datasets.lake_gate import split_pk

            pk_columns = split_pk(
                (dataset.schema_json or {}).get("primary_key"))
            if not pk_columns:
                raise WP.WritePortError(
                    "no_stable_primary_key",
                    "人工数据集未声明主键契约，无法安全定位行；读不受影响、"
                    "拒绝写",
                    status=409, datasetId=dataset_id)
        self._require_mapped_reads(request, reverse)
        operation = str(request.operation or WP.OPERATION_UPDATE)
        if operation == WP.OPERATION_INSERT:
            key, values = self._split_insert_key(request, field_map, pk_columns)
        else:
            key = self._resolve_key_columns(
                db, request, reverse, pk_columns, require_target_ref=False)
            values = self._resolve_value_columns(request, reverse, pk_columns)
        pk_properties = [field_map[col] for col in pk_columns
                         if col in field_map]
        value_map = self._value_map(request, reverse)
        write_properties = sorted(
            set(request.property_values or {})
            | set(request.pending_value_mappings or {}))

        from app.data_channel.curated.review_row_identity import (
            latest_dataset_version,
        )

        from app.data_channel.datasets.version_pointer import (
            resolve_current_version,
        )

        latest = resolve_current_version(db, dataset_id)
        if latest is None:
            raise WP.WritePortError(
                "dataset_has_no_version",
                "数据集没有可写版本", status=409, datasetId=dataset_id)
        if is_curated:
            from app.data_channel.curated.review_row_identity import (
                version_review as _version_review,
            )

            base_review = _version_review(db, dataset_id, latest)
            if base_review is None or base_review.status != "approved":
                raise WP.WritePortError(
                    "curated_base_not_approved",
                    "成品数据集当前版本尚无已批准审核背书；动作待审变更"
                    "必须基于已批准基线（请先完成当前版本审核）",
                    status=409, datasetId=dataset_id,
                    baseVersionId=str(latest.id))

        # manual 行存在性与主键唯一性由 edit_rows 在执行时按最新快照定位
        # （其定位失败即拒绝、零版本变更），prepare 不做整表扫描。
        # curated 审核编辑需要 old_value 证据：读取当前审核版本行（与
        # 审核 UI 同一版本快照读路径）。
        before: Optional[dict] = None
        target_snapshot: Optional[dict] = None
        if is_curated and operation == WP.OPERATION_UPDATE:
            rows = self._load_dataset_rows(db, dataset_id)
            row = self._locate_row(rows, pk_columns, key)
            if row is None:
                raise WP.WritePortError(
                    "row_not_found",
                    "目标行在当前审核版本中不存在，拒绝提交编辑",
                    status=409, key=key)
            # 有界目标快照 + old_value 证据：定位键、写入属性（含延迟
            # 求值列——before/snapshot 必须覆盖其当前值）与已授权读取依赖。
            wanted_columns = set(key) | set(values) | {
                reverse[prop] for prop in set(request.pending_value_mappings)
                | set(request.read_properties) if prop in reverse}
            before = {col: row.get(col) for col in wanted_columns
                      if col not in key}
            target_snapshot = {
                prop: row.get(col)
                for col, prop in field_map.items() if col in wanted_columns}

        if is_curated:
            expected: Any = str(latest.id)
        else:
            expected = int(latest.version_no)
        return {
            "planVersion": _PLAN_VERSION,
            "assetKind": WP.ASSET_KIND_CURATED if is_curated
            else WP.ASSET_KIND_MANUAL,
            "ontologyId": ontology_id,
            "releaseId": release_id,
            "mappingId": str(entry.get("id") or ""),
            "objectTypeId": str(request.object_type_id),
            "datasetId": dataset_id,
            "datasetName": getattr(dataset, "name", None),
            "operation": operation,
            "key": key,
            "values": values,
            "valueMap": value_map,
            "expectedToken": expected,
            "before": before,
            "targetSnapshot": target_snapshot,
            "keyProperties": pk_properties,
            "properties": write_properties,
        }

    @staticmethod
    def _curated_pk_columns(db: Session, dataset_id: str) -> list[str]:
        from app.data_channel.curated.review_row_identity import (
            dataset_pk_columns,
        )

        try:
            columns = dataset_pk_columns(db, dataset_id)
        except HTTPException as exc:
            raise _write_error(
                exc, default_code="no_stable_primary_key") from exc
        if not columns:
            raise WP.WritePortError(
                "no_stable_primary_key",
                "成品数据集未声明主键，无法安全定位行；读不受影响、拒绝写",
                status=409, datasetId=dataset_id)
        return columns

    @staticmethod
    def _load_dataset_rows(db: Session, dataset_id: str) -> list[dict]:
        from app.data_channel.datasets.service import (
            DatasetReadError, DatasetService,
        )

        try:
            return DatasetService(db).load_all_rows(dataset_id)
        except DatasetReadError as exc:
            raise WP.WritePortError(
                "dataset_read_failed", f"数据集读取失败：{exc}",
                status=503, datasetId=dataset_id) from exc

    @staticmethod
    def _locate_row(rows: list[dict], pk_columns: list[str],
                    key: dict) -> Optional[dict]:
        wanted = tuple(str(key.get(col)) for col in pk_columns)
        for row in rows:
            if tuple(str(row.get(col)) for col in pk_columns) == wanted:
                return row
        return None

    def _resolve_key_columns(self, db: Session,
                             request: WP.OntologyWriteRequest,
                             reverse: dict[str, str],
                             pk_columns: list[str],
                             *, require_target_ref: bool) -> dict[str, Any]:
        """主键定位：属性空间 ↔ 列空间经 field_map（列→属性）正向派生。

        ``reverse`` 是属性→列（已保证属性无多义）；这里再用其反函数得到
        列→属性，避免把物理列名当属性名解析。
        """
        column_to_prop = {col: prop for prop, col in reverse.items()}
        pk_properties = [column_to_prop.get(col) for col in pk_columns]
        if any(prop is None for prop in pk_properties):
            missing = [col for col, prop in zip(pk_columns, pk_properties)
                       if prop is None]
            raise WP.WritePortError(
                "no_stable_addressing",
                "主键列未映射到本体属性，无法经本体定位行；读不受影响、拒绝写",
                status=409, unmappedPrimaryKeyColumns=missing)

        if request.target_ref is not None:
            key_props = dict(request.key_values or {})
            if set(key_props) != set(pk_properties):
                raise WP.WritePortError(
                    "complete_primary_key_required",
                    f"targetRef.key 必须恰好提供主键属性 {pk_properties}",
                    status=422, expected=pk_properties,
                    provided=sorted(key_props))
        elif request.target_instance_id:
            if require_target_ref:
                raise WP.WritePortError(
                    "target_ref_required",
                    "远程资产对象没有本地实例，必须用 targetRef 按主键定位",
                    status=422)
            instance = db.query(ObjectInstance).filter(
                ObjectInstance.id == request.target_instance_id).first()
            if instance is None:
                raise WP.WritePortError(
                    "row_not_found",
                    "本地目标实例不存在，无法解析行主键", status=409)
            props = {
                **dict(instance.properties or {}),
                **dict(instance.computed or {}),
            }
            key_props = {prop: props.get(prop) for prop in pk_properties}
        else:
            raise WP.WritePortError(
                "unsupported_combination",
                "写目标缺少定位（需 targetRef 或本地目标实例）", status=422)

        if any(value is None or str(value).strip() == ""
               for value in key_props.values()):
            raise WP.WritePortError(
                "complete_primary_key_required",
                "主键属性值存在空值，无法定位行", status=422)
        return {
            col: key_props[prop]
            for col, prop in zip(pk_columns, pk_properties)
        }

    @staticmethod
    def _resolve_value_columns(request: WP.OntologyWriteRequest,
                               reverse: dict[str, str],
                               pk_columns: list[str]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for prop, value in (request.property_values or {}).items():
            column = reverse.get(prop)
            if column is None:
                raise WP.WritePortError(
                    "unknown_write_column",
                    f"属性「{prop}」未映射到资产列，拒绝写",
                    status=422, property=prop)
            if column in pk_columns:
                raise WP.WritePortError(
                    "primary_key_change_denied",
                    f"主键列「{column}」属于定位键，不能作为写值",
                    status=422, column=column)
            values[column] = value
        return values

    # --------------------------------------------------------------- apply

    def apply_write(self, db: Session, user, plan: dict, *,
                    confirmation_id: str,
                    actor_id: str) -> WP.AssetWriteOutcome:
        # 执行时重验：资产湖写角色对三源一致（integrated 另有源端
        # get_asset(write=True) 与 writable 复验）。
        _require_lake_write_role(user)
        kind = str(plan.get("assetKind"))
        if kind == WP.ASSET_KIND_INTEGRATED:
            return self._apply_integrated(db, user, plan, confirmation_id)
        if kind == WP.ASSET_KIND_CURATED:
            return self._apply_curated(db, user, plan, actor_id,
                                       confirmation_id)
        if kind == WP.ASSET_KIND_MANUAL:
            return self._apply_manual(db, user, plan)
        raise WP.WritePortError(
            "write_plan_invalid", f"未知资产种类：{kind}", status=422)

    def _apply_manual(self, db: Session, user, plan: dict) -> WP.AssetWriteOutcome:
        from app.data_channel.datasets.manual_contract import (
            RowEditOp, RowEditsRequest, require_manual_dataset,
        )
        from app.data_channel.datasets import mutation_service

        dataset_id = str(plan["datasetId"])
        operation = str(plan["operation"])
        try:
            base_version_no = int(plan.get("expectedToken"))
        except (TypeError, ValueError):
            raise WP.WritePortError(
                "write_plan_invalid", "manual 计划缺少 base version",
                status=422) from None
        body = RowEditsRequest(base_version_no=base_version_no)
        if operation == WP.OPERATION_UPDATE:
            body.updates = [RowEditOp(key=dict(plan["key"]),
                                      values=dict(plan["values"]))]
        elif operation == WP.OPERATION_INSERT:
            body.inserts = [RowEditOp(values={
                **dict(plan["key"]), **dict(plan["values"])})]
        elif operation == WP.OPERATION_DELETE:
            body.deletes = [RowEditOp(key=dict(plan["key"]))]
        else:
            raise WP.WritePortError(
                "write_plan_invalid", f"manual 不支持操作：{operation}",
                status=422)
        try:
            result = mutation_service.edit_rows(
                dataset_id, body, db,
                require_manual_dataset_fn=require_manual_dataset)
        except HTTPException as exc:
            if (getattr(exc, "status_code", None) == 409
                    and isinstance(exc.detail, dict)
                    and "current_version_no" in exc.detail):
                raise WP.WritePortError(
                    "version_conflict",
                    str((exc.detail or {}).get("message") or exc),
                    status=409) from exc
            raise _write_error(exc, default_code="manual_write_failed") from exc
        changed = int(result.get("updated") or 0) \
            + int(result.get("inserted") or 0) \
            + int(result.get("deleted") or 0)
        version = db.query(DatasetVersion).filter(
            DatasetVersion.dataset_id == dataset_id,
            DatasetVersion.version_no == result.get("version_no"),
        ).first()
        if version is None or changed != 1:
            # 版本已提交但回执不可证 — 不能假 applied 也不能假未执行。
            return WP.AssetWriteOutcome(
                asset_kind=WP.ASSET_KIND_MANUAL,
                status=WP.OUTCOME_UNKNOWN,
                operation=operation,
                affected_rows=0,
                dataset_id=dataset_id,
                error_code="execution_result_unknown",
                message="数据集已响应但版本回执不可证，保持 unknown 人工对账",
            )
        return WP.AssetWriteOutcome(
            asset_kind=WP.ASSET_KIND_MANUAL,
            status=WP.OUTCOME_APPLIED,
            operation=operation,
            affected_rows=changed,
            dataset_id=dataset_id,
            version_id=str(version.id),
            message=f"已产生新版本 v{version.version_no}",
        )

    def _apply_curated(self, db: Session, user, plan: dict, actor_id: str,
                       confirmation_id: str = "") -> WP.AssetWriteOutcome:
        from app.data_channel.curated.review_row_identity import encode_row_pk
        from app.data_channel.curated.review_service import ReviewService

        dataset_id = str(plan["datasetId"])
        operation = str(plan["operation"])
        if operation != WP.OPERATION_UPDATE:
            raise WP.WritePortError(
                "write_plan_invalid",
                f"curated 仅支持 update（收到 {operation}）；行删除显式不支持",
                status=422)
        service = ReviewService(db)
        row_pk = encode_row_pk(
            dict(plan["key"]), list(plan["key"].keys()),
            dataset_name=str(plan.get("datasetName") or dataset_id))
        before = dict(plan.get("before") or {})
        from app.data_channel.curated.approved_version_reader import (
            ReviewApprovalError,
        )
        from app.data_channel.curated.proposal_lifecycle import (
            PostCommitVerificationError, create_candidate_review,
        )
        from app.data_channel.datasets.service import DatasetReadError

        try:
            # 基于已批准 base 创建候选版本 + 绑定审核（不动当前指针）；
            # commit 前可验证拒绝 rollback 零痕迹；同 confirmation 幂等复用。
            review = create_candidate_review(
                service, db, dataset_id,
                base_version_id=str(plan.get("expectedToken") or ""),
                proposal_key=WP.plan_idempotency_key(confirmation_id),
                reviewer_id=actor_id,
                edits=[{
                    "row_pk": row_pk,
                    "field_name": field,
                    "old_value": before.get(field),
                    "new_value": new_value,
                } for field, new_value in dict(plan["values"]).items()])
        except PostCommitVerificationError as exc:
            # 候选事务已提交但回执读回失败：unknown（不吞成零写）。
            return WP.AssetWriteOutcome(
                asset_kind=WP.ASSET_KIND_CURATED,
                status=WP.OUTCOME_UNKNOWN,
                operation=operation,
                affected_rows=0,
                dataset_id=dataset_id,
                error_code="execution_result_unknown",
                message=str(exc),
            )
        except HTTPException as exc:
            error = _write_error(exc, default_code="curated_write_failed")
            raise error from exc
        except (ReviewApprovalError, DatasetReadError, ValueError) as exc:
            # commit 前领域确定性拒绝（零候选/零审核壳/零编辑）。
            raise WP.WritePortError(
                "curated_write_failed", str(exc),
                status=409, datasetId=dataset_id) from exc
        if review.status == "approved":
            return WP.AssetWriteOutcome(
                asset_kind=WP.ASSET_KIND_CURATED,
                status=WP.OUTCOME_APPLIED,
                operation=operation,
                affected_rows=1,
                dataset_id=dataset_id,
                version_id=str(review.dataset_version_id),
                review_id=str(review.id),
                message="幂等复用：该确认的候选已获批准并生效",
            )
        if review.status == "rejected":
            return WP.AssetWriteOutcome(
                asset_kind=WP.ASSET_KIND_CURATED,
                status=WP.OUTCOME_FAILED,
                operation=operation,
                affected_rows=0,
                dataset_id=dataset_id,
                review_id=str(review.id),
                base_version_id=str(review.base_version_id or ""),
                error_code="curated_review_rejected",
                message="幂等复用：该确认的候选已被拒绝，未生效",
            )
        return WP.AssetWriteOutcome(
            asset_kind=WP.ASSET_KIND_CURATED,
            status=WP.OUTCOME_PENDING_REVIEW,
            operation=operation,
            affected_rows=0,
            dataset_id=dataset_id,
            review_id=str(review.id),
            base_version_id=str(review.base_version_id or ""),
            message="变更已记录待审核，批准前查询不可见（未生效）",
        )

    def _apply_integrated(self, db: Session, user, plan: dict,
                          confirmation_id: str) -> WP.AssetWriteOutcome:
        from app.data_channel.integrated import service as integrated_service
        from app.data_channel.integrated.contracts import RowMutation
        from app.data_channel.integrated.models import IntegratedAsset

        # 执行时 fresh 授权（独立于确认链上层检查）：菜单/角色/writable
        # 撤回后在此拒绝，绝不进入 mutate。
        from app.data_channel.integrated import native

        try:
            native.authorize(db, user, write=True)
            asset = native.get_asset(db, user, str(plan["assetId"]),
                                     write=True)
        except HTTPException as exc:
            raise _write_error(
                exc, default_code="source_write_denied") from exc
        # 执行时重验资产身份：prepare 冻结的连接/库表/指纹任一变化即拒绝
        # （连接更换即使同 schema 也可能写错库）；源端活 schema 漂移由
        # mutate_row 内 reflect 二次校验兜底。
        if asset.dataset_id != str(plan["datasetId"]):
            raise WP.WritePortError(
                "remote_asset_missing",
                "计划引用的集成资产已不存在或归属变化，拒绝执行",
                status=409)
        if (str(asset.connection_id or "") != str(plan.get("connectionId") or "")
                or str(asset.schema_name or "") != str(plan.get("schemaName") or "")
                or str(asset.table_name or "") != str(plan.get("tableName") or "")
                or str(asset.schema_fingerprint or "") != str(
                    plan.get("schemaFingerprint") or "")):
            raise WP.WritePortError(
                "schema_drift",
                "集成资产身份已变化（连接/库表/指纹与冻结计划不一致），"
                "需重新准备确认",
                status=409)
        # 逻辑视图投影与连接配置（同连接改配置）漂移同样拒绝：冻结的是
        # one-way 指纹，不含任何配置明文。
        from app.data_channel.integrated.service import asset_projection

        projection = asset_projection(asset)
        frozen_projection = plan.get("projectionFingerprint")
        live_projection = (
            str(projection["fingerprint"]) if projection else None)
        if (frozen_projection or None) != (live_projection or None):
            raise WP.WritePortError(
                "schema_drift",
                "集成资产逻辑视图（projection）已变化，需重新准备确认",
                status=409)
        if (_source_config_fingerprint(db, asset.connection_id)
                != plan.get("sourceConfigFingerprint")):
            raise WP.WritePortError(
                "source_config_drift",
                "源连接配置已变化（同连接改配置），需重新准备确认",
                status=409)
        self._verify_write_namespace(
            projection,
            [str(c) for c in (projection.get("primary_key")
                              if projection else None)
             or plan.get("key", {}).keys()],
            dict(plan["key"]), dict(plan["values"]))
        body = RowMutation(
            key=dict(plan["key"]),
            values=dict(plan["values"]),
            expected_row_token=plan.get("expectedToken"),
            idempotency_key=WP.plan_idempotency_key(confirmation_id),
        )
        try:
            receipt = integrated_service.mutate_row(
                db, user, str(plan["assetId"]), str(plan["operation"]), body)
        except HTTPException as exc:
            detail = getattr(exc, "detail", None)
            record = detail.get("mutation") if isinstance(detail, dict) else None
            receipt_id = record.get("id") if isinstance(record, dict) else None
            receipt_status = (
                record.get("status") if isinstance(record, dict) else None)
            if receipt_status == "unknown":
                return WP.AssetWriteOutcome(
                    asset_kind=WP.ASSET_KIND_INTEGRATED,
                    status=WP.OUTCOME_UNKNOWN,
                    operation=str(plan["operation"]),
                    affected_rows=0,
                    dataset_id=str(plan["datasetId"]),
                    receipt_id=str(receipt_id) if receipt_id else None,
                    error_code="execution_result_unknown",
                    message="源端提交结果未知，保持 unknown 不自动重放；"
                            "按回执对源端人工对账",
                )
            error = _write_error(exc, default_code="source_write_rejected")
            if receipt_id is not None and "receiptId" not in error.extra:
                error.extra["receiptId"] = str(receipt_id)
            raise error from exc
        replayed = bool(receipt.get("replayed"))
        affected = int(receipt.get("affected_rows") or 0)
        if receipt.get("status") != "applied" or affected != 1:
            return WP.AssetWriteOutcome(
                asset_kind=WP.ASSET_KIND_INTEGRATED,
                status=WP.OUTCOME_UNKNOWN,
                operation=str(plan["operation"]),
                affected_rows=affected,
                dataset_id=str(plan["datasetId"]),
                receipt_id=str(receipt.get("id") or "") or None,
                error_code="execution_result_unknown",
                message="源端回执状态异常，不能宣称已应用",
            )
        return WP.AssetWriteOutcome(
            asset_kind=WP.ASSET_KIND_INTEGRATED,
            status=WP.OUTCOME_APPLIED,
            operation=str(plan["operation"]),
            affected_rows=affected,
            dataset_id=str(plan["datasetId"]),
            receipt_id=str(receipt.get("id") or "") or None,
            message="幂等重放命中既有回执" if replayed else None,
        )

    # ------------------------------------------------------------- inspect

    def inspect_write(self, db: Session, user, plan: dict, *,
                      evidence: dict) -> Optional[WP.AssetWriteOutcome]:
        """只读回访：按执行回执 reviewId 绑定真实 review 刷新 outcome。

        - review 必须仍属于计划数据集（防串他人审核）；
        - 数据集已出新版本 → 旧批准失效（superseded → failed）；
        - affectedRows 语义为“受影响行数”，单行编辑批准后为 1。
        """
        if str(plan.get("assetKind")) != WP.ASSET_KIND_CURATED:
            return None
        review_id = str((evidence or {}).get("reviewId") or "")
        if not review_id:
            return None
        from app.data_channel.curated.models import CuratedReview

        review = db.get(CuratedReview, review_id)
        if review is None or review.curated_dataset_id != str(plan["datasetId"]):
            return None
        if review.status == "pending":
            return None
        if review.dataset_version_id is None and review.base_version_id:
            # B'：未绑定候选的终态——rejected 无需版本即可如实判定；
            # approved 必须已绑定正式版本（未绑定的 approved 属异常形态，
            # 不判 applied，交人工对账）。
            if review.status == "rejected":
                return WP.AssetWriteOutcome(
                    asset_kind=WP.ASSET_KIND_CURATED,
                    status=WP.OUTCOME_FAILED,
                    operation=str(plan.get("operation")
                                  or WP.OPERATION_UPDATE),
                    affected_rows=0,
                    dataset_id=str(plan.get("datasetId") or ""),
                    review_id=str(review.id),
                    base_version_id=str(review.base_version_id or ""),
                    error_code="curated_review_rejected",
                    message="审核已拒绝，变更未生效",
                )
            return None
        asset_kind = WP.ASSET_KIND_CURATED
        operation = str(plan.get("operation") or WP.OPERATION_UPDATE)
        dataset_id = str(plan["datasetId"])
        if review.status == "approved":
            from app.data_channel.datasets.version_pointer import (
                resolve_current_version,
            )

            latest = resolve_current_version(db, dataset_id)
            if (latest is None or not review.dataset_version_id
                    or str(latest.id) != str(review.dataset_version_id)):
                return WP.AssetWriteOutcome(
                    asset_kind=asset_kind, status=WP.OUTCOME_FAILED,
                    operation=operation, affected_rows=0,
                    dataset_id=dataset_id, review_id=str(review.id),
                    error_code="curated_review_superseded",
                    message="数据集已产生新版本，旧批准失效（编辑未随新版本"
                            "生效）；如需变更请基于新版本重新提交")
            return WP.AssetWriteOutcome(
                asset_kind=asset_kind, status=WP.OUTCOME_APPLIED,
                operation=operation, affected_rows=1,
                dataset_id=dataset_id,
                version_id=str(review.dataset_version_id),
                review_id=str(review.id),
                message="审核已批准，变更已生效")
        return WP.AssetWriteOutcome(
            asset_kind=asset_kind, status=WP.OUTCOME_FAILED,
            operation=operation, affected_rows=0,
            dataset_id=dataset_id, review_id=str(review.id),
            error_code="curated_review_rejected",
            message="审核已拒绝，变更未生效")
