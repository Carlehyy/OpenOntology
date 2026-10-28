"""Ontology Mapping 执行服务 — Entity Mapping + Relation inference."""
from __future__ import annotations
import logging
from sqlalchemy.orm import Session
from app.ontologies.mappings.models import OntologyMapping
from app.ontologies.runtime_fence import _ontology_build_lock
from app.ontologies.mappings.candidate_discovery import CandidateDiscoveryMixin
from app.ontologies.mappings.entity_reconciliation import (
    EntityReconciliationMixin,
)
from app.ontologies.mappings.errors import (
    MappingApplyError,
    MappingReleaseScopeError,
    MappingSentinelDispatchError,
    MappingSourceError,
)
from app.ontologies.mappings.identity_metadata import IdentityMetadataMixin
from app.ontologies.mappings.projection_adapter import ProjectionAdapterMixin
from app.ontologies.mappings.relation_processing import RelationProcessingMixin

logger = logging.getLogger(__name__)

def _release_field_mapping(value: dict | None) -> dict:
    """Remove apply bookkeeping while retaining identity/projection semantics."""
    return {
        key: item for key, item in dict(value or {}).items()
        if key != "__last_apply_error__" and not key.startswith("__applied_")
    }


def _release_binding_view(field_mapping: dict | None) -> dict:
    """Canonical binding view for the release-scope fence.

    Derived from the field_mapping definition (the single definition store);
    snapshot top-level ``bindingMode``/``remote`` keys are mirrors of it.
    """
    from app.ontologies.mappings.remote_binding import (
        BINDING_MODE_REMOTE,
        RemoteBindingError,
        mapping_binding_mode,
    )
    try:
        mode = mapping_binding_mode(field_mapping)
    except RemoteBindingError as exc:
        raise MappingReleaseScopeError(
            f"映射绑定模式非法，已拒绝写入正式实例：{exc.message}") from exc
    view = {"bindingMode": mode, "remote": None}
    if mode == BINDING_MODE_REMOTE:
        from app.ontologies.mappings.remote_binding import remote_descriptor
        try:
            view["remote"] = remote_descriptor(field_mapping)
        except RemoteBindingError as exc:
            raise MappingReleaseScopeError(
                f"远程映射描述符非法，已拒绝写入正式实例：{exc.message}") from exc
    return view






def load_mapping_source_rows(
        db: Session, mapping: OntologyMapping, *,
        require_approved: bool = True,
        allow_empty_version: bool = False,
        batch_size: int | None = None):
    """严格全量读取映射绑定的数据版本，返回 ``(rows, DatasetVersion)``。

    正式映射不允许使用 UI preview 的容错/截断语义。对新 ``v2_datasets`` 使用
    checksum 校验的全量读取；curated 数据要求当前版本审批通过。仅为兼容旧
    ``v2_curated_datasets``，允许读取其 sample_rows/无上限 preview。默认读取
    为空会明确失败；只有已发布映射显式传入 ``allow_empty_version`` 且存在
    一个真实、已治理的 v2 DatasetVersion 时，零行才表示权威的“全部删除”。
    """
    from app.ontologies.mappings.remote_binding import (
        BINDING_MODE_REMOTE,
        mapping_binding_mode,
    )
    if mapping_binding_mode(
            getattr(mapping, "field_mapping", None)) == BINDING_MODE_REMOTE:
        raise MappingSourceError(
            f"Mapping {mapping.id} 是远程绑定（binding_mode=remote），"
            "不存在本地数据版本；本地物化路径已隔离，"
            "查询期由主协调解析远程数据")
    if batch_size is not None:
        from app.ontologies.mappings.source_batches import prepare_mapping_source
        source = prepare_mapping_source(
            db, mapping, require_approved=require_approved,
            allow_empty_version=allow_empty_version, batch_size=batch_size)
        return source, source.version
    dataset_id = mapping.curated_dataset_id
    if not dataset_id:
        raise MappingSourceError(f"Mapping {mapping.id} 未绑定数据集")

    from app.data_channel.datasets.models import Dataset
    from app.data_channel.datasets.service import DatasetService
    from app.data_channel.curated.approved_version_reader import (
        ReviewApprovalError, latest_dataset_version)

    ds = db.query(Dataset).filter(Dataset.id == dataset_id).first()
    version = latest_dataset_version(db, dataset_id)
    if ds is not None:
        if ds.kind == "curated":
            from app.data_channel.curated.approved_version_reader import (
                iter_rows_with_edits)
            try:
                # 湖表版本经 iter_rows_with_edits 分批流式读取 + 批内叠加行
                # 编辑，不再全量物化整份 blob。build_all 下游（实体构建/关系
                # join/Neo4j 对账）结构上仍需全量列表，收集点统一收拢于此：
                # 峰值内存从「整份 blob 解析列表 + 叠加后列表」降为「一份全量
                # + 一批」。
                rows = [
                    row
                    for batch in iter_rows_with_edits(
                        db, dataset_id,
                        require_approved=require_approved, version=version)
                    for row in batch
                ]
            except ReviewApprovalError as e:
                raise MappingSourceError(str(e)) from e
        else:
            rows = DatasetService(db).load_all_rows(
                dataset_id, version.version_no if version is not None else None)
    else:
        # 迁移前的 legacy curated 兼容；没有真实版本时无法提供版本级血缘。
        from app.data_channel.curated.models import CuratedDataset
        legacy = db.query(CuratedDataset).filter(CuratedDataset.id == dataset_id).first()
        if legacy is None:
            raise MappingSourceError(f"映射绑定的数据集 {dataset_id} 不存在")
        if require_approved and legacy.status != "approved":
            raise MappingSourceError(f"数据集「{legacy.name}」尚未审批通过")
        rows = list((legacy.schema_json or {}).get("sample_rows") or [])
        if not rows:
            # 无上限仅用于旧表兼容；真实 v2 Dataset 上方始终走严格 load_all_rows。
            rows = DatasetService(db).preview(dataset_id, None, limit=None)

    if not rows and not (
        allow_empty_version
        and ds is not None
        and version is not None
    ):
        name = getattr(ds, "name", None) or dataset_id
        raise MappingSourceError(f"映射数据集「{name}」当前版本为空，已拒绝空跑投影")
    return rows, version


class MappingService(
    IdentityMetadataMixin,
    RelationProcessingMixin,
    ProjectionAdapterMixin,
    EntityReconciliationMixin,
    CandidateDiscoveryMixin,
):

    def __init__(self, db: Session):
        self._db = db

    # ── CRUD ─────────────────────────────────────────────────────────

    def create_mapping(self, ontology_id: str, curated_dataset_id: str, entity_class: str,
                       field_mapping: dict, primary_key_column: str | None = None,
                       confidence: float = 1.0,
                       target_object_type_id: str | None = None) -> OntologyMapping:
        field_mapping = dict(field_mapping or {})
        if primary_key_column and "__primary_key__" not in field_mapping:
            field_mapping["__primary_key__"] = primary_key_column
            # API callers can only pass the Dataset.schema_json contract.  This
            # argument is not an independent identity definition.
            field_mapping["__pk_source__"] = "lake"
        mapping = OntologyMapping(
            ontology_id=ontology_id, curated_dataset_id=curated_dataset_id,
            entity_class=entity_class, field_mapping=field_mapping,
            target_object_type_id=target_object_type_id,
            status="draft", confidence=confidence,
        )
        self._db.add(mapping); self._db.commit(); self._db.refresh(mapping)
        return mapping

    def get_mappings(self, ontology_id: str) -> list[OntologyMapping]:
        return self._db.query(OntologyMapping).filter(OntologyMapping.ontology_id == ontology_id).all()

    def _assert_current_release_scope(
            self, ontology_id: str, mappings: list[OntologyMapping], *, project=None,
    ) -> str | None:
        """Return the immutable owner after comparing full mapping definitions.

        Legacy projects without a release pointer keep their historical behavior.
        Once a current release exists, however, mutable live mapping tables may
        materialize formal data only when they exactly match that release.
        """
        from app.models.ontology import OntologyProject
        from app.models.ontology_version import OntologyVersion
        from app.ontologies.mappings.models import OntologyLinkMapping
        from app.ontologies.versions.snapshot_contract import (
            complete_snapshot,
        )

        if project is None:
            project = self._db.query(OntologyProject).filter(
                OntologyProject.id == ontology_id,
            ).first()
        if project is None:
            raise MappingApplyError(f"本体 {ontology_id} 不存在")
        # Some legacy/internal callers use lightweight session doubles and have
        # no release model at all.  Only a real project row can establish an
        # immutable ownership boundary.
        if not isinstance(project, OntologyProject):
            return None
        if not project.current_release_id:
            return None

        release = self._db.query(OntologyVersion).filter(
            OntologyVersion.id == project.current_release_id,
            OntologyVersion.ontology_id == ontology_id,
            OntologyVersion.node_kind == "release",
            OntologyVersion.lifecycle_status == "released",
        ).first()
        if release is None:
            raise MappingReleaseScopeError(
                "当前发布指针无效，已拒绝写入正式实例")
        snapshot = complete_snapshot(release.snapshot_formal)

        live_objects = {
            str(item.id): {
                "id": str(item.id),
                "curatedDatasetId": item.curated_dataset_id,
                "entityClass": item.entity_class,
                "fieldMapping": _release_field_mapping(item.field_mapping),
                "targetObjectTypeId": item.target_object_type_id,
                "confidence": item.confidence,
                **_release_binding_view(item.field_mapping),
            }
            for item in mappings
        }
        released_objects = {
            str(item.get("id")): {
                "id": str(item.get("id")),
                "curatedDatasetId": item.get("curatedDatasetId"),
                "entityClass": item.get("entityClass") or "",
                "fieldMapping": _release_field_mapping(item.get("fieldMapping")),
                "targetObjectTypeId": item.get("targetObjectTypeId"),
                "confidence": item.get("confidence"),
                **_release_binding_view(item.get("fieldMapping")),
            }
            for item in snapshot["mappings"] if item.get("id")
        }

        live_link_rows = self._db.query(OntologyLinkMapping).filter(
            OntologyLinkMapping.ontology_id == ontology_id,
        ).all()
        live_links = {
            str(item.id): {
                "id": str(item.id),
                "srcDatasetId": item.src_dataset_id,
                "tgtDatasetId": item.tgt_dataset_id,
                "relationType": item.relation_type,
                "srcKey": item.src_key,
                "tgtKey": item.tgt_key,
                "linkTypeId": item.link_type_id,
                "edgeDatasetId": item.edge_dataset_id,
                "fieldMapping": _release_field_mapping(item.field_mapping),
                **_release_binding_view(item.field_mapping),
            }
            for item in live_link_rows
        }
        released_links = {
            str(item.get("id")): {
                "id": str(item.get("id")),
                "srcDatasetId": item.get("srcDatasetId"),
                "tgtDatasetId": item.get("tgtDatasetId"),
                "relationType": item.get("relationType") or "",
                "srcKey": item.get("srcKey") or "",
                "tgtKey": item.get("tgtKey") or "",
                "linkTypeId": item.get("linkTypeId"),
                "edgeDatasetId": item.get("edgeDatasetId"),
                "fieldMapping": _release_field_mapping(item.get("fieldMapping")),
                **_release_binding_view(item.get("fieldMapping")),
            }
            for item in snapshot["linkMappings"] if item.get("id")
        }

        if live_objects != released_objects or live_links != released_links:
            raise MappingReleaseScopeError(
                "当前运行映射不属于当前发布快照，已拒绝写入正式实例；"
                "请在草稿中完成试跑并晋级发布")
        return str(release.id)

    def remove_mapping_projection(self, mapping: OntologyMapping) -> list[str]:
        """Remove this mapping's materialized current state in the caller transaction.

        Shared identities remain while another mapping still owns them.  Historical
        facts are retained and receive object/link tombstones.
        """
        self._adopt_legacy_projection_ownership(mapping)
        return self._reconcile_mapping_entities(mapping, set())

    def remove_link_mapping_projection(self, link_mapping) -> list[str]:
        """Remove relations/materialized Formal links owned by a LinkMapping."""
        from app.models.relation import Relation
        from app.ontologies.mappings.models import OntologyLinkMapping
        from app.models.ontology_formal import ObjectInstance, LinkType, LinkInstance
        from app.ontologies.formal_modeling.facts import record_link_fact

        signature = (
            OntologyLinkMapping.ontology_id == link_mapping.ontology_id,
            OntologyLinkMapping.src_dataset_id == link_mapping.src_dataset_id,
            OntologyLinkMapping.tgt_dataset_id == link_mapping.tgt_dataset_id,
            OntologyLinkMapping.relation_type == link_mapping.relation_type,
            OntologyLinkMapping.src_key == link_mapping.src_key,
            OntologyLinkMapping.tgt_key == link_mapping.tgt_key,
            OntologyLinkMapping.edge_dataset_id == link_mapping.edge_dataset_id,
        )
        possible_owners = self._db.query(OntologyLinkMapping).filter(*signature).count()
        legacy: list[Relation] = []
        for relation in self._db.query(Relation).filter(
                Relation.ontology_id == link_mapping.ontology_id,
                Relation.type == link_mapping.relation_type).all():
            props = relation.properties or {}
            if props.get("mapping_type") != "link_mapping" or props.get("__link_mapping_id__"):
                continue
            direct_match = (props.get("src_key") == link_mapping.src_key
                            and props.get("tgt_key") == link_mapping.tgt_key)
            edge_match = bool(link_mapping.edge_dataset_id and props.get("__edge_key__"))
            if direct_match or edge_match:
                legacy.append(relation)
        if legacy and possible_owners != 1:
            raise MappingApplyError(
                f"关系 {link_mapping.relation_type} 有 {len(legacy)} 条历史投影缺少 LinkMapping 血缘，"
                f"且存在 {possible_owners} 个同签名映射；拒绝猜测删除归属")
        for relation in legacy:
            props = dict(relation.properties or {})
            props["__link_mapping_id__"] = link_mapping.id
            relation.properties = props

        owned = [
            relation for relation in self._db.query(Relation).filter(
                Relation.ontology_id == link_mapping.ontology_id).all()
            if (relation.properties or {}).get("__link_mapping_id__") == link_mapping.id
        ]
        internal_relation_keys = {
            "mapping_type", "src_key", "tgt_key", "cardinality",
            "__edge_key__", "__link_mapping_id__", "fk_column",
            "alt_column", "source",
        }
        # One-time lineage adoption for links projected before
        # source_relation_id existed.  Only an exact, unambiguous endpoint/type/
        # business-property match is accepted.
        for relation in owned:
            source = self._db.query(ObjectInstance).filter(
                ObjectInstance.ontology_id == link_mapping.ontology_id,
                ObjectInstance.external_id == relation.source_entity).first()
            target = self._db.query(ObjectInstance).filter(
                ObjectInstance.ontology_id == link_mapping.ontology_id,
                ObjectInstance.external_id == relation.target_entity).first()
            if source is None or target is None:
                continue
            link_type_ids = [item.id for item in self._db.query(LinkType).filter(
                LinkType.ontology_id == link_mapping.ontology_id,
                LinkType.name == relation.type,
                LinkType.source_object_type_id == source.object_type_id,
                LinkType.target_object_type_id == target.object_type_id,
            ).all()]
            business_props = {
                key: value for key, value in dict(relation.properties or {}).items()
                if key not in internal_relation_keys
            }
            candidates = [item for item in self._db.query(LinkInstance).filter(
                LinkInstance.ontology_id == link_mapping.ontology_id,
                LinkInstance.source_object_id == source.id,
                LinkInstance.target_object_id == target.id,
                LinkInstance.source_relation_id.is_(None),
            ).all() if item.link_type_id in link_type_ids
                and dict(item.properties or {}) == business_props]
            if len(candidates) > 1:
                raise MappingApplyError(
                    f"关系 {relation.id} 对应 {len(candidates)} 条无血缘 Formal Link，拒绝猜测删除")
            if candidates:
                candidates[0].source_relation_id = relation.id
        relation_ids = [relation.id for relation in owned]
        if relation_ids:
            for link in self._db.query(LinkInstance).filter(
                    LinkInstance.ontology_id == link_mapping.ontology_id).all():
                if link.source_relation_id in relation_ids:
                    record_link_fact(
                        self._db, ontology_id=link_mapping.ontology_id,
                        link_instance_id=link.id, link_type_id=link.link_type_id,
                        exists=False, source=f"link-mapping://{link_mapping.id}")
                    self._db.delete(link)
            for relation in owned:
                self._db.delete(relation)
        self._db.flush()
        return relation_ids

    # ── 单个 Mapping 应用 ─────────────────────────────────────────────

    def apply_mapping(self, mapping_id: str, data: list[dict], *,
                      ontology_id: str | None = None,
                      source_dataset_version_id: str | None = None) -> dict:
        from app.models.ontology import OntologyProject

        q = self._db.query(OntologyMapping).filter(OntologyMapping.id == mapping_id)
        if ontology_id is not None:
            q = q.filter(OntologyMapping.ontology_id == ontology_id)
        mapping = q.first()
        if not mapping:
            suffix = f" in ontology {ontology_id}" if ontology_id else ""
            raise ValueError(f"Mapping {mapping_id} not found{suffix}")

        from app.ontologies.mappings.remote_binding import (
            BINDING_MODE_REMOTE,
            mapping_binding_mode,
        )
        if mapping_binding_mode(mapping.field_mapping) == BINDING_MODE_REMOTE:
            raise MappingApplyError(
                f"Mapping {mapping_id} 是远程绑定（binding_mode=remote），"
                "不走本地物化；查询期由主协调解析远程数据，"
                "发布验证请走试跑/发布正门")

        project = self._db.query(OntologyProject).filter(
            OntologyProject.id == mapping.ontology_id,
        ).with_for_update().first()
        ontology_release_id = self._assert_current_release_scope(
            mapping.ontology_id,
            self.get_mappings(mapping.ontology_id),
            project=project,
        )

        mapping.status = "applying"
        self._db.flush()
        from app.ontologies.sentinels.cdc import (
            CAPTURE_SUPPRESSED_KEY, MAPPING_SCOPE_KEY, SUPPRESS_KEY,
        )
        self._db.info[SUPPRESS_KEY] = True
        self._db.info[CAPTURE_SUPPRESSED_KEY] = True
        self._db.info[MAPPING_SCOPE_KEY] = {mapping.id}
        try:
            # Published mappings are immutable release definitions.  Normalizing
            # them from each incoming dataset would auto-add unselected source
            # columns and make the very next run fail the release-scope fence.
            if data and ontology_release_id is None:
                self._normalize_mapping(mapping, data)
            from app.ontologies.mappings.raw_json_guard import structured_json_columns, reject_raw_json_columns
            reject_raw_json_columns(data, structured_json_columns(self._db, mapping))
            entities = self._rows_to_entities(mapping, data)
            self._adopt_legacy_projection_ownership(mapping)
            v1_count = self._write_v1_entities(mapping, entities)
            stale_entity_ids = self._reconcile_mapping_entities(
                mapping, {entity["id"] for entity in entities},
                source_dataset_version_id=source_dataset_version_id)

            # 单映射路径同样要投影到正规本体（fo_object_instances）。该投影是
            # Mapping applied 的必需条件，不再作为可吞掉的 best-effort 尾步骤。
            from app.ontologies.mappings.formal_projection import (
                project_to_formal_ontology,
                projection_property_mappings,
            )
            pk_col = (mapping.field_mapping or {}).get("__primary_key__") or self._choose_pk_col(data)
            meta = {mapping.id: {
                "mapping_id": mapping.id,
                "curated_dataset_id": mapping.curated_dataset_id,
                "entity_class": mapping.entity_class, "pk_col": pk_col,
                "pk_source": (mapping.field_mapping or {}).get("__pk_source__"),
                "target_object_type_id": mapping.target_object_type_id,
                "source_dataset_version_id": source_dataset_version_id,
                "rows": data,
                "entity_id_map": {
                    self._row_identity_value(row, pk_col):
                        self._stable_row_id(
                            mapping, row,
                            pk_col if self._has_complete_pk(row, pk_col) else None)
                    for row in data
                },
                "columns": list(data[0].keys()) if data else [],
                "property_mappings": projection_property_mappings(
                    mapping.field_mapping),
            }}
            formal_projection = project_to_formal_ontology(
                self._db, mapping.ontology_id, meta,
                ontology_release_id=ontology_release_id)
            self._ensure_source_version_is_current(
                mapping, source_dataset_version_id)

            # Entity + Formal projection + applied lineage are one relational
            # transaction.  A mid-flight commit used to leave partial entities
            # behind when formal validation failed, even though the mapping was
            # marked failed.
            # The relational/Formal source is durable before the external
            # projection runs, but runtime readers must see a fence until
            # Neo4j has been reconciled successfully.
            mapping.status = "projecting"
            from app.ontologies.projection_state import mark_projecting
            mark_projecting(self._db, mapping.ontology_id)
            fm = dict(mapping.field_mapping or {})
            fm.pop("__last_apply_error__", None)
            if source_dataset_version_id:
                fm["__applied_dataset_version_id__"] = source_dataset_version_id
            mapping.field_mapping = fm
            self._db.commit()
        except Exception as e:
            logger.exception("apply_mapping 写入/正规本体投影失败")
            self._db.rollback()
            from app.ontologies.sentinels.cdc import discard_captured_changes
            discard_captured_changes(self._db)
            failed = self._db.query(OntologyMapping).filter(
                OntologyMapping.id == mapping_id).first()
            if failed is not None:
                failed.status = "failed"
                fm = dict(failed.field_mapping or {})
                fm["__last_apply_error__"] = str(e)[:1000]
                failed.field_mapping = fm
                self._db.commit()
            raise MappingApplyError(
                f"Mapping {mapping_id} 正规投影失败，未标记为 applied：{e}") from e

        # Neo4j cannot participate in the SQL transaction. Reconcile it only
        # after truth commits, and keep the mapping fenced until it succeeds.
        # Do not expose a partial per-label write between SQL commit and the
        # validated full rebuild. The durable project fence blocks readers.
        neo4j_deleted = len(stale_entity_ids)
        neo4j_count = 0
        from app.config import settings
        projection_ready = True
        if settings.environment != "test":
            projection_ready = self._rebuild_neo4j_projection(
                mapping.ontology_id)
        if not projection_ready:
            message = "Neo4j projection rebuild failed"
            from app.ontologies.sentinels.cdc import discard_captured_changes
            discard_captured_changes(self._db)
            failed = self._db.query(OntologyMapping).filter(
                OntologyMapping.id == mapping_id).first()
            if failed is not None:
                failed.status = "failed"
                fm = dict(failed.field_mapping or {})
                fm["__last_apply_error__"] = message
                failed.field_mapping = fm
            from app.ontologies.projection_state import mark_failed
            mark_failed(self._db, mapping.ontology_id, message)
            self._db.commit()
            raise MappingApplyError(
                "关系型/Formal 投影已提交，但 Neo4j 查询投影未完成；"
                "已阻断发布和动作执行，重试映射即可修复")

        applied = self._db.query(OntologyMapping).filter(
            OntologyMapping.id == mapping_id).first()
        if applied is not None:
            applied.status = "applied"
        from app.ontologies.projection_state import mark_ready
        mark_ready(self._db, mapping.ontology_id)
        self._db.commit()
        if projection_ready:
            neo4j_count = len(entities)
        sentinel_dispatch = self._dispatch_captured_sentinel_changes(
            mapping.ontology_id)

        return {"mapping_id": mapping_id, "entity_class": mapping.entity_class,
                "nodes_created": neo4j_count, "v1_entities_written": v1_count,
                "formal_projection": formal_projection,
                "stale_entities_removed": len(stale_entity_ids),
                "stale_neo4j_nodes_removed": neo4j_deleted,
                "source_dataset_version_id": source_dataset_version_id,
                "sentinel_dispatch": sentinel_dispatch,
                "warnings": (
                    ["测试环境未写入 Neo4j 查询投影"]
                    if settings.environment == "test"
                    and entities
                    and neo4j_count == 0
                    else []
                ),
                "errors": 0, "total_rows": len(data)}

    def _dispatch_captured_sentinel_changes(
            self, ontology_id: str) -> dict:
        """Run the post-projection barrier without corrupting projection state."""
        from app.ontologies.sentinels.cdc import dispatch_captured_changes
        try:
            dispatch = dispatch_captured_changes(self._db)
        except Exception as exc:
            # An unexpected worker/database exception has the same state
            # boundary as an explicit retry/dead result: projection is already
            # committed, while downstream delivery is not confirmed.
            dispatch = {
                "evaluated": 0,
                "fired": 0,
                "errors": [{
                    "stage": "dispatch_captured_changes",
                    "error": str(exc),
                }],
                "runs": [],
                "barrierCompleted": False,
            }
            raise MappingSentinelDispatchError(
                ontology_id, dispatch) from exc
        if dispatch.get("errors"):
            # The relational/Formal projection is already committed and is
            # safe for runtime reads.  Its downstream automation delivery has
            # an independent durable state in SentinelCdcOutbox.  Re-labelling
            # this mapping as ``failed`` would make ActionEngine reject the
            # outbox retry that is meant to repair the delivery failure.
            raise MappingSentinelDispatchError(ontology_id, dispatch)
        return dispatch

    # ── 全量构建：Entity → Relation → Formal/Neo4j ────────────────────

    def build_all(self, ontology_id: str, *, require_approved: bool = False) -> dict:
        """Rebuild one ontology as one relational transaction.

        The ontology project row is the serialization point shared with release
        publication.  Entity, relation, discovered rule/action and Formal writes
        either commit together or are rolled back together. Neo4j is rebuilt
        only from the committed relational truth afterwards.
        """
        with _ontology_build_lock(self._db, ontology_id):
            return self._build_all_locked(
                ontology_id, require_approved=require_approved)

    def _build_all_locked(
            self, ontology_id: str, *, require_approved: bool = False) -> dict:
        from app.models.ontology import OntologyProject

        mapping_ids: list[str] = []
        try:
            project = self._db.query(OntologyProject).filter(
                OntologyProject.id == ontology_id,
            ).with_for_update().first()
            if project is None:
                raise MappingApplyError(f"本体 {ontology_id} 不存在")
            mappings = self.get_mappings(ontology_id)
            mapping_ids = [mapping.id for mapping in mappings]
            ontology_release_id = self._assert_current_release_scope(
                ontology_id, mappings, project=project)
            return self._build_all_transaction(
                ontology_id, mappings, require_approved=require_approved,
                ontology_release_id=ontology_release_id)
        except Exception as exc:
            self._db.rollback()
            # The projection transaction and its applied fence were committed
            # before the synchronous Sentinel barrier ran.  Keep that fence
            # intact so durable CDC retry can invoke production actions; the
            # retry/dead outbox row remains the authoritative failure state.
            if isinstance(exc, MappingSentinelDispatchError):
                raise
            from app.ontologies.sentinels.cdc import discard_captured_changes
            discard_captured_changes(self._db)
            if isinstance(exc, MappingReleaseScopeError):
                raise
            try:
                if not mapping_ids:
                    mapping_ids = [item[0] for item in self._db.query(
                        OntologyMapping.id).filter(
                            OntologyMapping.ontology_id == ontology_id).all()]
                for mapping_id in mapping_ids:
                    failed = self._db.query(OntologyMapping).filter(
                        OntologyMapping.id == mapping_id).first()
                    if failed is not None:
                        failed.status = "failed"
                        fm = dict(failed.field_mapping or {})
                        fm["__last_apply_error__"] = str(exc)[:1000]
                        failed.field_mapping = fm
                self._db.commit()
            except Exception:
                self._db.rollback()
                logger.exception("记录 build_all 失败状态时数据库不可用")
            if isinstance(exc, (MappingApplyError, MappingSourceError)):
                raise
            raise MappingApplyError(
                f"本体 {ontology_id} 全量映射失败，关系型投影已回滚：{exc}") from exc

    def _build_all_transaction(
            self, ontology_id: str, mappings: list[OntologyMapping], *,
            require_approved: bool = False,
            ontology_release_id: str | None = None) -> dict:
        stages = []
        try:
            return self._execute_build_all(
                ontology_id, mappings, require_approved=require_approved,
                ontology_release_id=ontology_release_id, stages=stages)
        finally:
            if self._db.is_active:
                for stage in reversed(stages):
                    stage.close()

    def _execute_build_all(
            self, ontology_id: str, mappings: list[OntologyMapping], *,
            require_approved: bool, ontology_release_id: str | None,
            stages: list) -> dict:
        if not mappings:
            return {"error": "no mappings configured", "ontology_id": ontology_id}

        # 远程绑定与本地物化隔离：远程映射不读取本地数据版本、不产生实体/关系
        # 投影，只在结果中显式登记为“未物化”，绝不静默跳过。
        from app.ontologies.mappings.remote_binding import (
            BINDING_MODE_REMOTE,
            mapping_binding_mode,
        )
        remote_mappings = [
            item for item in mappings
            if item.curated_dataset_id
            and mapping_binding_mode(item.field_mapping) == BINDING_MODE_REMOTE
        ]
        remote_binding_results = [
            {
                "mapping_id": item.id,
                "entity_class": item.entity_class,
                "materialized": False,
                "reason": "remote_binding_not_materialized",
            }
            for item in remote_mappings
        ]
        remote_datasets = {item.curated_dataset_id for item in remote_mappings}

        # Formal Object/Link writes would otherwise trigger CDC immediately at
        # the first commit while mappings are deliberately fenced as
        # ``projecting``.  Capture the exact delta and release it only after all
        # derived projections succeed and every mapping becomes ``applied``.
        from app.ontologies.sentinels.cdc import (
            CAPTURE_SUPPRESSED_KEY, MAPPING_SCOPE_KEY, SUPPRESS_KEY,
        )
        self._db.info[SUPPRESS_KEY] = True
        self._db.info[CAPTURE_SUPPRESSED_KEY] = True
        self._db.info[MAPPING_SCOPE_KEY] = {
            item.id for item in mappings if item.curated_dataset_id
        }

        from app.data_channel.datasets.models import Dataset
        from app.data_channel.curated.models import CuratedDataset

        # Phase 1: Entity Mapping
        entity_results = []
        mapping_meta: dict[str, dict] = {}
        from app.ontologies.mappings.formal_projection import (
            project_to_formal_ontology,
            projection_property_mappings,
        )

        for m in mappings:
            if not m.curated_dataset_id:
                continue
            if mapping_binding_mode(m.field_mapping) == BINDING_MODE_REMOTE:
                # 远程绑定：物化路径隔离（见 remote_binding_results）。
                continue
            source, source_version = load_mapping_source_rows(
                self._db, m, require_approved=require_approved,
                allow_empty_version=ontology_release_id is not None, batch_size=500)
            from app.ontologies.mappings.batched_materialization import materialize_mapping
            meta, v1_count, stale_count = materialize_mapping(
                self, m, source, source_version,
                released=ontology_release_id is not None, stages=stages)
            dataset_name = None
            ds = self._db.query(Dataset).filter(Dataset.id == m.curated_dataset_id).first()
            if ds:
                dataset_name = ds.name
            else:
                cd = self._db.query(CuratedDataset).filter(CuratedDataset.id == m.curated_dataset_id).first()
                if cd:
                    dataset_name = cd.name
            meta["dataset_name"] = dataset_name
            mapping_meta[m.id] = meta
            m.status = "applying"
            entity_results.append({"mapping_id": m.id, "entity_class": m.entity_class,
                                   "v1_entities_written": v1_count,
                                   "stale_entities_removed": stale_count,
                                   "nodes_created": 0})

        self._db.flush()

        # Phase 2: Relation 推断 is a draft-time discovery aid.  A published
        # release has an explicit, complete LinkMapping contract; inventing FK
        # relations from new source columns would attempt to create undeclared
        # LinkTypes and make production reprojection non-deterministic.
        if ontology_release_id is None:
            relation_results = self._infer_and_write_relations(
                ontology_id, mappings, mapping_meta)
        else:
            from app.ontologies.relations.models import Relation
            from app.ontologies.mappings.source_staging import keyset_pages
            for page in keyset_pages(self._db.query(Relation).filter(
                    Relation.ontology_id == ontology_id), Relation.id):
                for relation in page:
                    if not (relation.properties or {}).get("__link_mapping_id__"):
                        self._db.delete(relation)
            self._db.flush()
            relation_results = []

        # Phase 2b: Link Mapping 处理（手动配置的跨表关系）
        link_results = self._process_link_mappings(
            ontology_id, mapping_meta, remote_datasets=remote_datasets)
        relation_results.extend(link_results)

        # Phase 3 is likewise draft-only.  Runtime projection must never mutate
        # the released executable model behind its immutable version snapshot.
        if ontology_release_id is None:
            logic_result = self._discover_logic_rules(
                ontology_id, mappings, mapping_meta, relation_results)
            action_result = self._discover_action_types(
                ontology_id, mappings, mapping_meta, relation_results,
                logic_result)
        else:
            logic_result = {"total_v2": 0, "skipped": "released_schema"}
            action_result = {"total_v2": 0, "skipped": "released_schema"}

        # Neo4j is a derived projection. It is rebuilt only after the relational
        # + Formal transaction succeeds below, never mid-transaction.

        # Phase 5: 投影到正规本体 (Projection to Formal Ontology)
        # 把已落地的 Entity / Relation 投影成 ObjectType / ObjectInstance /
        # LinkType / LinkInstance，让流水线数据直接在图谱编辑器里可见、可建模。
        formal_projection = project_to_formal_ontology(
            self._db, ontology_id, mapping_meta,
            ontology_release_id=ontology_release_id)
        for mapping_id, meta in mapping_meta.items():
            source_mapping = next(m for m in mappings if m.id == mapping_id)
            self._ensure_source_version_is_current(
                source_mapping, meta.get("source_dataset_version_id"))

        for mapping_id in mapping_meta:
            applied = self._db.query(OntologyMapping).filter(
                OntologyMapping.id == mapping_id).first()
            if applied is not None:
                # Persist a fence before touching non-transactional projections.
                # Runtime actions and publication reject this transient state.
                applied.status = "projecting"
                fm = dict(applied.field_mapping or {})
                fm.pop("__last_apply_error__", None)
                source_version_id = mapping_meta[mapping_id].get(
                    "source_dataset_version_id")
                if source_version_id:
                    fm["__applied_dataset_version_id__"] = source_version_id
                applied.field_mapping = fm
        from app.ontologies.projection_state import mark_projecting
        mark_projecting(self._db, ontology_id)
        for stage in reversed(stages):
            stage.close()
        stages.clear()
        self._db.commit()
        neo4j_rebuilt = self._rebuild_neo4j_projection(ontology_id)
        from app.config import settings
        projection_errors = []
        if not neo4j_rebuilt:
            projection_errors.append("Neo4j projection rebuild failed")
        if projection_errors and settings.environment != "test":
            message = "; ".join(projection_errors)
            for mapping_id in mapping_meta:
                failed = self._db.query(OntologyMapping).filter(
                    OntologyMapping.id == mapping_id).first()
                if failed is not None:
                    failed.status = "failed"
                    fm = dict(failed.field_mapping or {})
                    fm["__last_apply_error__"] = message
                    failed.field_mapping = fm
            from app.ontologies.projection_state import mark_failed
            mark_failed(self._db, ontology_id, message)
            self._db.commit()
            raise MappingApplyError(
                "关系型/Formal 投影已提交，但派生查询投影未完成；"
                f"已阻断发布和动作执行，重试本体全量对账即可修复: {message}")

        for mapping_id in mapping_meta:
            applied = self._db.query(OntologyMapping).filter(
                OntologyMapping.id == mapping_id).first()
            if applied is not None:
                applied.status = "applied"
        from app.ontologies.projection_state import mark_ready
        mark_ready(self._db, ontology_id)
        self._db.commit()
        sentinel_dispatch = self._dispatch_captured_sentinel_changes(
            ontology_id)
        if neo4j_rebuilt:
            for result in entity_results:
                result["nodes_created"] = result["v1_entities_written"]

        return {
            "ontology_id": ontology_id,
            "entity_mappings": entity_results,
            "relations_written": relation_results,
            "logic_discovery": logic_result,
            "action_discovery": action_result,
            "formal_projection": formal_projection,
            "neo4j_projection_rebuilt": neo4j_rebuilt,
            "projection_warnings": projection_errors,
            "remote_bindings": remote_binding_results,
            "sentinel_dispatch": sentinel_dispatch,
            "total_entities": sum(r.get("v1_entities_written", 0) for r in entity_results),
            "total_relations": sum(r.get("count", 0) for r in relation_results),
            "total_logic": logic_result.get("total_v2", 0),
            "total_actions": action_result.get("total_v2", 0),
            "review_required": True,
            "publish_status": "draft",
        }

    def _ensure_source_version_is_current(
            self, mapping: OntologyMapping, source_dataset_version_id: str | None) -> None:
        """Apply 结束前重验版本，封住“读取 v1 时并发发布 v2”的竞态。"""
        if not source_dataset_version_id or not mapping.curated_dataset_id:
            return  # legacy 数据源没有 DatasetVersion，只能维持兼容语义
        from app.data_channel.curated.approved_version_reader import (
            latest_dataset_version,
        )
        current = latest_dataset_version(self._db, mapping.curated_dataset_id)
        if current is None or current.id != source_dataset_version_id:
            current_label = f"v{current.version_no}" if current is not None else "无版本"
            raise MappingApplyError(
                f"映射执行期间数据源已更新为 {current_label}；"
                f"本次读取版本 {source_dataset_version_id} 已过期，拒绝标记 applied")
