"""
正规本体投影层 (Formal Ontology Projection)
==========================================

把数据流水线已落地的旧扁平模型 (Entity / Relation) 投影成
Palantir Foundry 风格的正规本体 (ObjectType / ObjectInstance /
LinkType / LinkInstance)，让流水线与图谱编辑器共用同一套数据。

设计原则
--------
1. **非侵入**：不重写 mapping_service 复杂的映射/推断逻辑，只在
   ``build_all()`` 末尾追加一次投影 (Phase 5)。
2. **幂等**：用稳定 key 做 upsert——
   - ObjectType   按 (ontology_id, name=entity_class) 去重
   - ObjectInstance 按 (ontology_id, external_id=Entity.id) 去重
   - LinkType     按 (ontology_id, name, source/target object_type) 去重
   - LinkInstance 按确定性 id (link_type + src_instance + tgt_instance) 去重
3. **可追溯**：投影出来的实例 ``source="pipeline"``，
   ``external_id`` 指回原 Entity.id，便于回滚 / 重跑。

调用方式
--------
    from app.ontologies.mappings.formal_projection import project_to_formal_ontology
    summary = project_to_formal_ontology(db, ontology_id)
"""
from __future__ import annotations

import logging

from app.ontologies.mappings.formal_projection_contract import (
    _RESERVED_PROP_KEYS,
    _build_object_type_properties,
    _coerce_props_to_type,
    _display_value,
    _infer_property_type,
    _merge_properties,
    _pick_first,
    _property_data_binding,
    _stable_id,
    projection_property_mappings,
    stable_link_instance_id,
    stable_object_instance_id,
    stable_pipeline_entity_id,
    stable_pipeline_relation_id,
)


logger = logging.getLogger(__name__)

# entity_class → 一组默认图标 / 颜色（仅美观，无业务含义）
_DEFAULT_COLORS = ["indigo", "cyan", "violet", "purple", "yellow"]

# 关系投影的内部记账键：仅供映射/去重使用，不应作为业务边属性落进 LinkInstance
_INTERNAL_LINK_PROP_KEYS = frozenset({
    "mapping_type", "src_key", "tgt_key", "cardinality",
    "__edge_key__", "__link_mapping_id__", "fk_column", "alt_column", "source",
})


def project_to_formal_ontology(
    db,
    ontology_id: str,
    mapping_meta: dict | None = None,
    *,
    ontology_release_id: str | None = None,
) -> dict:
    """
    把已落地的 Entity / Relation 投影为正规本体。

    参数
    ----
    db            : SQLAlchemy session
    ontology_id   : 本体 id
    mapping_meta  : build_all() 产出的 per-mapping 元数据（可选，用于
                    取 pk_col / property_mappings 提升属性质量）；
                    不传时仅依据 Entity.properties 推断。

    返回投影统计 dict。
    """
    from app.models.entity import Entity
    from app.models.relation import Relation
    from app.models.ontology_formal import (
        ObjectType, ObjectInstance, LinkType, LinkInstance,
    )
    from app.models.ontology import OntologyProject

    project = db.query(OntologyProject).filter(OntologyProject.id == ontology_id).first()
    if project is None:
        raise ValueError(f"Ontology {ontology_id} not found")
    schema_locked = (project.status or "") == "published"

    summary = {
        "object_types": 0,
        "object_instances": 0,
        "removed_object_instances": 0,
        "link_types": 0,
        "link_instances": 0,
        "skipped_relations": 0,
        "removed_link_instances": 0,
    }

    entities = db.query(Entity).filter(Entity.ontology_id == ontology_id).all()

    # ── 元数据辅助：entity_class → {pk_col, property_mappings} ──
    class_meta: dict[str, dict] = {}
    reconciliation_classes: set[str] = set()
    for meta in (mapping_meta or {}).values():
        ec = meta.get("entity_class")
        if ec and ec not in class_meta:
            class_meta[ec] = {
                "pk_col": meta.get("pk_col"),
                "property_mappings": meta.get("property_mappings") or [],
                "target_object_type_id": meta.get("target_object_type_id"),
                "binding_context": {
                    "mapping_id": meta.get("mapping_id"),
                    "curated_dataset_id": meta.get("curated_dataset_id"),
                    "source_dataset_version_id": meta.get("source_dataset_version_id"),
                    "dataset_name": meta.get("dataset_name"),
                },
            }
        if ec:
            reconciliation_classes.add(str(ec))

    # 绑定兜底：投影会重投影本体下全部 entity_class（不只本次映射），
    # 其余类的绑定信息不在本次 meta 里——从映射表按 entity_class 补齐，
    # 否则单映射增量灌入时其他类会丢绑定、退化成自建平行类型
    try:
        from app.ontologies.mappings.models import OntologyMapping as _OM
        for _m in db.query(_OM).filter(_OM.ontology_id == ontology_id).all():
            cm = class_meta.setdefault(_m.entity_class, {})
            if _m.target_object_type_id:
                if not cm.get("target_object_type_id"):
                    cm["target_object_type_id"] = _m.target_object_type_id
            binding_context = cm.get("binding_context") or {}
            metadata_owner = binding_context.get("mapping_id")
            if metadata_owner in (None, _m.id):
                if not cm.get("pk_col"):
                    cm["pk_col"] = (
                        (_m.field_mapping or {}).get("__primary_key__"))
                persisted_properties = projection_property_mappings(
                    _m.field_mapping)
                if persisted_properties:
                    current_by_column = {
                        str(item.get("column")): dict(item)
                        for item in (cm.get("property_mappings") or [])
                        if isinstance(item, dict) and item.get("column")
                    }
                    # Persisted explicit source→target bindings are the
                    # authority; per-run metadata contributes richer types.
                    for item in persisted_properties:
                        column = str(item["column"])
                        enriched = dict(item)
                        enriched.update(current_by_column.get(column, {}))
                        enriched["column"] = column
                        enriched["property"] = item["property"]
                        current_by_column[column] = enriched
                    cm["property_mappings"] = list(
                        current_by_column.values())
                if not binding_context:
                    cm["binding_context"] = {
                        "mapping_id": _m.id,
                        "curated_dataset_id": _m.curated_dataset_id,
                    }
            # A caller which does not provide per-run metadata is asking for a
            # full projection.  In that case the persisted Mapping definitions
            # are the authoritative cleanup scope, including mappings whose
            # latest source snapshot contains zero rows.  Conversely,
            # apply_mapping() supplies one explicit meta entry and must not wipe
            # trial-promoted objects owned by unrelated mappings.
            if not mapping_meta:
                reconciliation_classes.add(str(_m.entity_class))
    except Exception:  # noqa: BLE001 — 兜底失败不阻断投影（仍有按名匹配保护）
        logger.warning("读取映射绑定失败，仅按名匹配", exc_info=True)

    if not entities and ontology_release_id is None:
        logger.info("投影跳过：本体 %s 无 Entity 数据", ontology_id)
        return summary

    # ── 1. 按 entity_class 分组 ──
    entities_by_class: dict[str, list[Entity]] = {}
    for ent in entities:
        ec = ent.type or "Object"
        entities_by_class.setdefault(ec, []).append(ent)

    # ── 2. 投影 ObjectType（upsert by name）+ ObjectInstance（upsert by external_id）──
    # entity_class → ObjectType.id
    class_to_ot_id: dict[str, str] = {}
    # entity_class → 主键属性名 (用于 LinkInstance 的属性记录，非必须)
    class_to_pk_field: dict[str, str] = {}
    # Entity.id → ObjectInstance.id
    entity_to_instance: dict[str, str] = {}

    for idx, (ec, ent_list) in enumerate(entities_by_class.items()):
        ent_props_list = []
        for entity in ent_list:
            flattened = dict(entity.properties or {})
            business = flattened.pop("__business_properties__", {})
            if isinstance(business, dict):
                flattened.update(business)
            ent_props_list.append(flattened)
        meta = class_meta.get(ec, {})
        # 来源湖版本（事实血缘）：本类实体由哪一版数据快照投影而来。
        # binding_context 可能缺失（旧映射行），此时事实血缘保持 NULL。
        source_version_id = (
            (meta.get("binding_context") or {}).get("source_dataset_version_id")
            or None)
        # property_mappings 里的 property 名是落地后的属性键
        pk_source_fields = [part.strip() for part in str(
            meta.get("pk_col") or "").split(",") if part.strip()]
        source_to_property = {
            str(item.get("column")): str(item.get("property") or item.get("column"))
            for item in (meta.get("property_mappings") or [])
            if isinstance(item, dict) and item.get("column")
        }
        # pk_col 是源列名，Entity 中使用的是 field mapping 后的属性名。
        pk_fields = [source_to_property.get(field, field) for field in pk_source_fields]
        data_props, pk_prop_id = _build_object_type_properties(
            ent_props_list, pk_fields, meta.get("property_mappings"),
            meta.get("binding_context"),
        )
        pk_name_from_data = next((p["name"] for p in data_props if p["id"] == pk_prop_id), None)

        # —— 类型解析三级顺序（model-first 的关键）——
        # ① 映射上人工绑定的对象实体（target_object_type_id）
        # ② 图谱里同名的手绘类型（避免平行重复类型 + 编辑器 duplicate_name 422）
        # ③ 投影自建（data-first：uuid5 稳定 id，幂等 upsert）
        existing_ot = None
        bound_id = meta.get("target_object_type_id")
        if bound_id:
            existing_ot = db.query(ObjectType).filter(
                ObjectType.id == bound_id,
                ObjectType.ontology_id == ontology_id).first()
            if existing_ot is None:
                logger.warning("映射绑定的对象实体 %s 已不存在，回退按名匹配 (entity_class=%s)",
                               bound_id, ec)
        if existing_ot is None:
            from sqlalchemy import or_
            existing_ot = db.query(ObjectType).filter(
                ObjectType.ontology_id == ontology_id,
                or_(ObjectType.name == ec, ObjectType.display_name == ec)).first()
        if existing_ot is None:
            proj_id = _stable_id("ot", ontology_id, ec)
            existing_ot = db.query(ObjectType).filter(ObjectType.id == proj_id).first()

        color = _DEFAULT_COLORS[idx % len(_DEFAULT_COLORS)]
        if existing_ot:
            # 属性合并而非替换：手绘/已有定义（computed 属性、类型修正、显示名）
            # 永远优先，只追加数据里新出现的列——重跑投影不再冲掉人的加工
            if schema_locked:
                declared = {p.get("name") for p in (existing_ot.properties or [])
                            if isinstance(p, dict) and p.get("name")}
                incoming = {p.get("name") for p in data_props
                            if isinstance(p, dict) and p.get("name")}
                unknown = sorted(incoming - declared)
                if unknown:
                    # Source datasets routinely contain columns deliberately
                    # omitted from the release mapping.  MappingService's
                    # normalization keeps metadata for those columns in its
                    # intermediate Entity envelope; they are not permission to
                    # extend a published Formal schema.  Ignore them here.  A
                    # genuinely changed release mapping is already rejected by
                    # the immutable release-scope fence before projection.
                    logger.info(
                        "已发布对象类型 %s 忽略未声明且未发布的源字段 %s",
                        existing_ot.display_name, unknown)
                merged = list(existing_ot.properties or [])
            else:
                merged = _merge_properties(existing_ot.properties or [], data_props)
            merged = [dict(prop) for prop in merged]
            # Existing hand-authored property metadata remains authoritative,
            # except that every canonical composite-key component must be
            # non-null and visibly marked in the final type contract.
            pk_rank = {name: index + 1 for index, name in enumerate(pk_fields)}
            for prop in merged:
                if prop.get("name") in pk_rank:
                    prop["required"] = True
                    prop["primaryKeyPart"] = pk_rank[prop["name"]]
            if merged != (existing_ot.properties or []):
                existing_ot.properties = merged
            existing_ot.display_name = existing_ot.display_name or ec
            if not existing_ot.primary_key and pk_name_from_data:
                pk_prop = next((p for p in merged if p.get("name") == pk_name_from_data), None)
                if pk_prop:
                    existing_ot.primary_key = pk_prop.get("id")
            ot_id = existing_ot.id
            final_props = merged
            final_pk_id = existing_ot.primary_key
        else:
            if schema_locked:
                raise ValueError(
                    f"已发布本体不允许由数据投影自动创建对象类型「{ec}」；"
                    "请先在草稿中建立并绑定类型"
                )
            ot_id = _stable_id("ot", ontology_id, ec)
            db.add(ObjectType(
                id=ot_id,
                ontology_id=ontology_id,
                name=ec,
                display_name=ec,
                description=f"由数据流水线投影生成（来源 entity_class={ec}）",
                icon="📦",
                color=color,
                primary_key=pk_prop_id,
                properties=data_props,
                position_x=float((idx % 4) * 320),
                position_y=float((idx // 4) * 240),
            ))
            summary["object_types"] += 1
            final_props = data_props
            final_pk_id = pk_prop_id
        class_to_ot_id[ec] = ot_id
        # 找出主键属性的 name（以最终类型定义为准）
        pk_name = next((p["name"] for p in final_props if p.get("id") == final_pk_id), None)
        class_to_pk_field[ec] = pk_name or pk_name_from_data or ""

        # ObjectInstance：每个 Entity → 一条实例，external_id=Entity.id 去重。
        # 变化才写：无变化实例不进 session.dirty（否则重跑投影会让 CDC 对全量
        # 实例误报变化）；变化的写入事实流（source=pipeline）并触发派生重算。
        from app.ontologies.formal_modeling.facts import (
            record_object_presence,
            record_property_facts,
        )
        from app.ontologies.formal_modeling.derived import recompute_instance_derived
        for ent in ent_list:
            canonical_inst_id = stable_object_instance_id(ontology_id, ent.id)
            inst_id = canonical_inst_id
            props = {k: v for k, v in (ent.properties or {}).items()
                     if k not in ("ontology_id", "__mapping_ids__", "__business_properties__")}
            business = (ent.properties or {}).get("__business_properties__")
            if isinstance(business, dict):
                props.update(business)
            # 按类型属性定义转换值类型（CSV 字符串 → number/boolean）
            props = _coerce_props_to_type(props, final_props)
            if schema_locked:
                declared_property_names = {
                    str(prop.get("name")) for prop in final_props
                    if isinstance(prop, dict) and prop.get("name")
                }
                props = {
                    key: value for key, value in props.items()
                    if key in declared_property_names
                }
            # 补充展示名仅限声明了 name 的类型。已发布闭合 schema 不能因为
            # 投影层的展示便利被悄悄写入一个未知业务属性。
            declared_property_names = {
                str(prop.get("name")) for prop in final_props
                if isinstance(prop, dict) and prop.get("name")
            }
            if not declared_property_names or "name" in declared_property_names:
                props.setdefault("name", ent.name_cn or ent.name_en or ent.id)
            existing_inst = db.query(ObjectInstance).filter(
                ObjectInstance.id == canonical_inst_id,
            ).first()
            if existing_inst is None:
                # Promotion versions before the stable-identity contract used
                # the row identity as external_id and a different object UUID.
                # First adopt any already-canonical lineage row, then fall back
                # to an unambiguous primary-key match.  We deliberately keep
                # the old materialized ID so facts, match states and links do
                # not experience a synthetic leave/enter transition.
                existing_inst = db.query(ObjectInstance).filter(
                    ObjectInstance.ontology_id == ontology_id,
                    ObjectInstance.object_type_id == ot_id,
                    ObjectInstance.external_id == ent.id,
                ).first()
            if existing_inst is None and class_to_pk_field.get(ec):
                pk_field = class_to_pk_field[ec]
                pk_value = props.get(pk_field)
                legacy_candidates = [
                    item for item in db.query(ObjectInstance).filter(
                        ObjectInstance.ontology_id == ontology_id,
                        ObjectInstance.object_type_id == ot_id,
                        ObjectInstance.source == "pipeline",
                    ).all()
                    if (item.properties or {}).get(pk_field) == pk_value
                ]
                if len(legacy_candidates) > 1:
                    raise ValueError(
                        f"对象类型「{ec}」主键 {pk_field}={pk_value!r} "
                        f"对应 {len(legacy_candidates)} 条历史实例，拒绝猜测投影血缘")
                if legacy_candidates:
                    existing_inst = legacy_candidates[0]
            if existing_inst:
                inst_id = existing_inst.id
                old_props = dict(existing_inst.properties or {})
                changed = (old_props != props
                           or existing_inst.object_type_id != ot_id
                           or existing_inst.external_id != ent.id
                           or existing_inst.ontology_release_id != ontology_release_id)
                if changed:
                    existing_inst.object_type_id = ot_id
                    existing_inst.ontology_release_id = ontology_release_id
                    existing_inst.properties = props
                    existing_inst.source = "pipeline"
                    existing_inst.external_id = ent.id
                    new_facts = record_property_facts(
                        db, ontology_id=ontology_id, instance_id=inst_id,
                        object_type_id=ot_id, old_props=old_props, new_props=props,
                        source="pipeline",
                        ontology_release_id=ontology_release_id,
                        source_dataset_version_id=source_version_id)
                    if new_facts:
                        recompute_instance_derived(
                            db, ontology_id=ontology_id, instance=existing_inst,
                            trigger_facts=new_facts)
                    summary["updated_instances"] = summary.get("updated_instances", 0) + 1
                inst_obj = existing_inst
            else:
                inst_obj = ObjectInstance(
                    id=inst_id,
                    ontology_id=ontology_id,
                    ontology_release_id=ontology_release_id,
                    object_type_id=ot_id,
                    properties=props,
                    computed={},
                    source="pipeline",
                    external_id=ent.id,
                )
                db.add(inst_obj)
                db.flush()
                record_object_presence(
                    db,
                    ontology_id=ontology_id,
                    instance_id=inst_id,
                    object_type_id=ot_id,
                    source="pipeline",
                    ontology_release_id=ontology_release_id,
                    source_dataset_version_id=source_version_id,
                )
                new_facts = record_property_facts(
                    db, ontology_id=ontology_id, instance_id=inst_id,
                    object_type_id=ot_id, old_props=None, new_props=props,
                    source="pipeline",
                    ontology_release_id=ontology_release_id,
                    source_dataset_version_id=source_version_id)
                if new_facts:
                    recompute_instance_derived(
                        db, ontology_id=ontology_id, instance=inst_obj,
                        trigger_facts=new_facts)
                summary["object_instances"] += 1
            entity_to_instance[ent.id] = inst_id

    # A promoted trial initially has no legacy Entity rows.  The legacy
    # MappingService reconciliation therefore cannot see pipeline Formal
    # objects which disappear from a later lake snapshot.  Reconcile the
    # authoritative released projection here by canonical Entity lineage.
    #
    # Scope is intentionally narrow: only pipeline objects owned by a mapped
    # type and the current release are eligible.  Action/manual-created objects
    # are business state and must survive normal source refreshes.
    if ontology_release_id is not None:
        from sqlalchemy import or_
        from app.ontologies.formal_modeling.facts import (
            record_link_fact, record_object_tombstone,
        )

        mapped_type_ids: set[str] = set()
        # 血缘：object_type_id → 本次应用该类型的来源湖版本（墓碑/悬挂边删除同样带版本）
        version_by_type_id: dict[str, str | None] = {}
        for entity_class in reconciliation_classes:
            class_meta_item = class_meta.get(entity_class, {}) or {}
            version = (
                (class_meta_item.get("binding_context") or {})
                .get("source_dataset_version_id"))
            object_type_id = (
                class_meta_item.get("target_object_type_id")
                or class_to_ot_id.get(entity_class)
            )
            if not object_type_id:
                mapped_type = db.query(ObjectType).filter(
                    ObjectType.ontology_id == ontology_id,
                    or_(
                        ObjectType.name == entity_class,
                        ObjectType.display_name == entity_class,
                    ),
                ).first()
                object_type_id = mapped_type.id if mapped_type is not None else None
            if object_type_id:
                mapped_type_ids.add(str(object_type_id))
                version_by_type_id.setdefault(str(object_type_id), version)
            elif schema_locked:
                raise ValueError(
                    f"已发布映射实体类型「{entity_class}」无法解析到 ObjectType，"
                    "拒绝在未知清理范围下继续投影"
                )

        # Initialize every released mapping type even when its current source
        # contributes no Entity rows.  An empty set is meaningful authoritative
        # lineage ("all source rows were deleted"), not "skip reconciliation".
        current_external_ids_by_type: dict[str, set[str]] = {
            object_type_id: set() for object_type_id in mapped_type_ids
        }
        for entity in entities:
            object_type_id = class_to_ot_id.get(entity.type or "Object")
            if object_type_id in mapped_type_ids:
                current_external_ids_by_type.setdefault(
                    object_type_id, set()).add(entity.id)

        stale_instances = []
        if mapped_type_ids:
            for instance in db.query(ObjectInstance).filter(
                    ObjectInstance.ontology_id == ontology_id,
                    ObjectInstance.ontology_release_id == ontology_release_id,
                    ObjectInstance.source == "pipeline",
                    ObjectInstance.object_type_id.in_(sorted(mapped_type_ids)),
            ).all():
                current_lineage = current_external_ids_by_type.get(
                    instance.object_type_id, set())
                if (
                    not instance.external_id
                    or instance.external_id not in current_lineage
                ):
                    stale_instances.append(instance)

        for instance in stale_instances:
            instance_version = version_by_type_id.get(instance.object_type_id)
            dangling_links = db.query(LinkInstance).filter(
                LinkInstance.ontology_id == ontology_id,
                LinkInstance.ontology_release_id == ontology_release_id,
                or_(
                    LinkInstance.source_object_id == instance.id,
                    LinkInstance.target_object_id == instance.id,
                ),
            ).all()
            for link in dangling_links:
                record_link_fact(
                    db,
                    ontology_id=ontology_id,
                    link_instance_id=link.id,
                    link_type_id=link.link_type_id,
                    exists=False,
                    source="pipeline-reconcile",
                    ontology_release_id=ontology_release_id,
                    source_dataset_version_id=instance_version,
                )
                db.delete(link)
                summary["removed_link_instances"] += 1
            record_object_tombstone(
                db,
                ontology_id=ontology_id,
                instance_id=instance.id,
                object_type_id=instance.object_type_id,
                source="pipeline-reconcile",
                ontology_release_id=ontology_release_id,
                source_dataset_version_id=instance_version,
            )
            db.delete(instance)
            summary["removed_object_instances"] += 1

    # Keep object and link projection in one caller-controlled transaction.
    db.flush()

    # ── 3. 投影 LinkType + LinkInstance ──
    relations = db.query(Relation).filter(Relation.ontology_id == ontology_id).all()
    if not relations:
        logger.info("投影：本体 %s 无 Relation，跳过链接投影", ontology_id)
        from app.ontologies.formal_modeling.validation import validate_instance_contract
        from app.ontologies.formal_modeling.facts import record_link_fact
        link_query = db.query(LinkInstance).filter(
            LinkInstance.ontology_id == ontology_id)
        if ontology_release_id is not None:
            link_query = link_query.filter(
                LinkInstance.ontology_release_id == ontology_release_id)
        for link in link_query.all():
            if link.source_relation_id:
                record_link_fact(
                    db, ontology_id=ontology_id, link_instance_id=link.id,
                    link_type_id=link.link_type_id, exists=False,
                    source="pipeline-reconcile",
                    ontology_release_id=ontology_release_id)
                db.delete(link)
                summary["removed_link_instances"] += 1
        all_object_types = db.query(ObjectType).filter(ObjectType.ontology_id == ontology_id).all()
        instance_query = db.query(ObjectInstance).filter(
            ObjectInstance.ontology_id == ontology_id)
        if ontology_release_id is not None:
            instance_query = instance_query.filter(
                ObjectInstance.ontology_release_id == ontology_release_id)
        all_instances = instance_query.all()
        contract_errors = validate_instance_contract(all_object_types, all_instances)
        if contract_errors:
            preview = "; ".join(error.get("message", "") for error in contract_errors[:5])
            raise ValueError(
                f"正规本体投影违反运行契约（{len(contract_errors)} 项）: {preview}")
        # 即使无关系，实例投影也已发生 → 同样要推进修订戳
        from datetime import datetime as _dt, timezone as _tz
        from app.models.ontology import OntologyProject as _OP
        proj = db.query(_OP).filter(_OP.id == ontology_id).first()
        if proj is not None:
            proj.updated_at = _dt.now(_tz.utc)
        db.flush()
        return summary

    # entity_class 查询缓存：Entity.id → entity_class
    entity_class_of: dict[str, str] = {e.id: (e.type or "Object") for e in entities}

    # (src_class, tgt_class, rel_type) → LinkType.id
    linktype_cache: dict[tuple[str, str, str], str] = {}
    linktype_property_cache: dict[str, list[dict]] = {}

    # ── 基数兜底推断 ──
    # 上游 FK 推断理应把 cardinality 写进 Relation.properties，但若缺失
    # （历史数据 / 回写失败），这里按实际边的重数自行推断，避免一律退化为
    # many-to-many。统计每个 link 分组下：每个 src 连了几个不同 tgt，反之亦然。
    def _infer_cardinality_for_group(rel_group: list) -> str:
        # source_object_type --link--> target_object_type
        # src_to_tgts: 一个 source 实例连了几个不同 target
        # tgt_to_srcs: 一个 target 实例被几个不同 source 连
        src_to_tgts: dict[str, set] = {}
        tgt_to_srcs: dict[str, set] = {}
        for r in rel_group:
            src_to_tgts.setdefault(r.source_entity, set()).add(r.target_entity)
            tgt_to_srcs.setdefault(r.target_entity, set()).add(r.source_entity)
        # source 侧是否"多"：某个 target 被多个 source 指
        source_is_many = max((len(v) for v in tgt_to_srcs.values()), default=1) > 1
        # target 侧是否"多"：某个 source 指向多个 target
        target_is_many = max((len(v) for v in src_to_tgts.values()), default=1) > 1
        if source_is_many and target_is_many:
            return "many-to-many"
        if source_is_many and not target_is_many:
            return "many-to-one"
        if target_is_many and not source_is_many:
            return "one-to-many"
        return "one-to-one"

    # 预分组用于兜底推断
    _rels_by_key: dict[tuple[str, str, str], list] = {}
    for _r in relations:
        _sc = entity_class_of.get(_r.source_entity, "Object")
        _tc = entity_class_of.get(_r.target_entity, "Object")
        _rt = _r.type or "RELATED_TO"
        _rels_by_key.setdefault((_sc, _tc, _rt), []).append(_r)

    for rel in relations:
        from app.ontologies.formal_modeling.facts import (
            record_link_fact, record_link_property_facts,
        )
        src_ent = rel.source_entity
        tgt_ent = rel.target_entity
        src_inst = entity_to_instance.get(src_ent)
        tgt_inst = entity_to_instance.get(tgt_ent)
        if not src_inst or not tgt_inst:
            summary["skipped_relations"] += 1
            continue

        src_class = entity_class_of.get(src_ent, "Object")
        tgt_class = entity_class_of.get(tgt_ent, "Object")
        rel_type = rel.type or "RELATED_TO"
        # 边的事实血缘：以源实体类的来源湖版本归因（边的生成数据来自该快照）
        link_source_version = (
            ((class_meta.get(src_class, {}) or {}).get("binding_context") or {})
            .get("source_dataset_version_id"))
        # 优先取 Relation.properties 里上游 FK 推断写入的基数；缺失时按实际边重数兜底推断，
        # 避免一律退化成 many-to-many。
        raw_card = (rel.properties or {}).get("cardinality")
        if raw_card:
            cardinality = str(raw_card).replace("_", "-")
        else:
            cardinality = _infer_cardinality_for_group(
                _rels_by_key.get((src_class, tgt_class, rel_type), [rel])
            )
        if cardinality not in ("one-to-one", "one-to-many", "many-to-one", "many-to-many"):
            cardinality = "many-to-many"

        key = (src_class, tgt_class, rel_type)
        lt_id = linktype_cache.get(key)
        if lt_id is None:
            src_ot = class_to_ot_id.get(src_class)
            tgt_ot = class_to_ot_id.get(tgt_class)
            if not src_ot or not tgt_ot:
                summary["skipped_relations"] += 1
                continue
            # 关系类型同样先复用：图谱里已有"两端类型一致、同名"的手绘关系时直接用它
            # （手绘关系的基数/角色是人定的，不被数据推断覆盖）
            hand_lt = db.query(LinkType).filter(
                LinkType.ontology_id == ontology_id,
                LinkType.name == rel_type,
                LinkType.source_object_type_id == src_ot,
                LinkType.target_object_type_id == tgt_ot).first()
            if hand_lt is not None:
                lt_id = hand_lt.id
            else:
                lt_id = _stable_id("lt", ontology_id, src_class, tgt_class, rel_type)
                existing_lt = db.query(LinkType).filter(LinkType.id == lt_id).first()
                if existing_lt:
                    # 投影自建的关系：随数据更新基数/端点
                    if schema_locked:
                        if (existing_lt.source_object_type_id != src_ot
                                or existing_lt.target_object_type_id != tgt_ot):
                            raise ValueError(f"已发布关系类型「{rel_type}」端点与投影数据不一致")
                    else:
                        existing_lt.cardinality = cardinality
                        existing_lt.source_object_type_id = src_ot
                        existing_lt.target_object_type_id = tgt_ot
                else:
                    if schema_locked:
                        raise ValueError(
                            f"已发布本体不允许由数据投影自动创建关系类型「{rel_type}」；"
                            "请先在草稿中建立关系定义"
                        )
                    db.add(LinkType(
                        id=lt_id,
                        ontology_id=ontology_id,
                        name=rel_type,
                        display_name=rel_type.replace("_", " ").title(),
                        description=f"由数据流水线投影生成（{src_class} → {tgt_class}）",
                        source_object_type_id=src_ot,
                        target_object_type_id=tgt_ot,
                        cardinality=cardinality,
                        source_role=src_class,
                        target_role=tgt_class,
                        properties=[],
                    ))
                    summary["link_types"] += 1
            linktype_cache[key] = lt_id

        # LinkInstance：确定性 id 去重；新建链接进入事实流（Link 也是 Fact）
        # 连接表胖关系：同一对实体可有多条属性不同的边 → id 纳入 __edge_key__，防被去重合并成一条。
        raw_props = rel.properties or {}
        edge_key = raw_props.get("__edge_key__") or ""
        canonical_li_id = stable_link_instance_id(
            ontology_id, lt_id, src_inst, tgt_inst, edge_key)
        li_id = canonical_li_id
        existing_li = db.query(LinkInstance).filter(
            LinkInstance.id == canonical_li_id).first()
        # 只保留真正的业务边属性，剔除映射记账用的内部键
        li_props = {k: v for k, v in raw_props.items() if k not in _INTERNAL_LINK_PROP_KEYS}
        link_type_properties = linktype_property_cache.get(lt_id)
        if link_type_properties is None:
            resolved_link_type = db.query(LinkType).filter(
                LinkType.id == lt_id,
                LinkType.ontology_id == ontology_id,
            ).first()
            if resolved_link_type is None:
                raise ValueError(f"关系类型 {lt_id} 不存在，无法投影关系属性")
            link_type_properties = list(resolved_link_type.properties or [])
            linktype_property_cache[lt_id] = link_type_properties
        li_props = _coerce_props_to_type(li_props, link_type_properties)
        if existing_li is None:
            lineage_candidates = db.query(LinkInstance).filter(
                LinkInstance.ontology_id == ontology_id,
                LinkInstance.link_type_id == lt_id,
                LinkInstance.source_object_id == src_inst,
                LinkInstance.target_object_id == tgt_inst,
                LinkInstance.source_relation_id == rel.id,
            ).all()
            if len(lineage_candidates) > 1:
                raise ValueError(
                    f"关系 {rel.id} 对应 {len(lineage_candidates)} 条 Formal Link，"
                    "拒绝猜测投影血缘")
            if lineage_candidates:
                existing_li = lineage_candidates[0]
        if existing_li is None:
            # Adopt links promoted by older versions which did not persist
            # Relation lineage.  Endpoint/type/business properties must match
            # exactly and uniquely; ambiguity is a hard error.
            legacy_candidates = [
                item for item in db.query(LinkInstance).filter(
                    LinkInstance.ontology_id == ontology_id,
                    LinkInstance.link_type_id == lt_id,
                    LinkInstance.source_object_id == src_inst,
                    LinkInstance.target_object_id == tgt_inst,
                    LinkInstance.source_relation_id.is_(None),
                ).all()
                if dict(item.properties or {}) == li_props
            ]
            if len(legacy_candidates) > 1:
                raise ValueError(
                    f"关系 {rel.id} 对应 {len(legacy_candidates)} 条无血缘 Formal Link，"
                    "拒绝猜测投影血缘")
            if legacy_candidates:
                existing_li = legacy_candidates[0]
        if existing_li:
            li_id = existing_li.id
            old_li_props = dict(existing_li.properties or {})
            if (existing_li.link_type_id != lt_id
                    or existing_li.source_object_id != src_inst
                    or existing_li.target_object_id != tgt_inst
                    or old_li_props != li_props
                    or existing_li.source_relation_id != rel.id
                    or existing_li.ontology_release_id != ontology_release_id):
                existing_li.link_type_id = lt_id
                existing_li.ontology_release_id = ontology_release_id
                existing_li.source_object_id = src_inst
                existing_li.target_object_id = tgt_inst
                existing_li.properties = li_props
                existing_li.source_relation_id = rel.id
                if old_li_props != li_props:
                    # 关系属性变化同样是事实（kind='link' 属性级），可时态回放
                    record_link_property_facts(
                        db, ontology_id=ontology_id, instance_id=li_id,
                        link_type_id=lt_id, old_props=old_li_props,
                        new_props=li_props, source="pipeline",
                        ontology_release_id=ontology_release_id,
                        source_dataset_version_id=link_source_version)
        else:
            from app.ontologies.formal_modeling.facts import (
                record_link_fact, record_link_property_facts,
            )
            li_obj = LinkInstance(
                id=li_id,
                ontology_id=ontology_id,
                ontology_release_id=ontology_release_id,
                link_type_id=lt_id,
                source_object_id=src_inst,
                target_object_id=tgt_inst,
                properties=li_props,
                source_relation_id=rel.id,
            )
            db.add(li_obj)
            db.flush()
            record_link_fact(
                db, ontology_id=ontology_id, link_instance_id=li_id,
                link_type_id=lt_id, exists=True, source="pipeline",
                ontology_release_id=ontology_release_id,
                source_dataset_version_id=link_source_version)
            record_link_property_facts(
                db, ontology_id=ontology_id, instance_id=li_id,
                link_type_id=lt_id, old_props=None, new_props=li_props,
                source="pipeline", ontology_release_id=ontology_release_id,
                source_dataset_version_id=link_source_version)
            summary["link_instances"] += 1

    # Materialized links must mirror the current Relation projection.  Keep the
    # immutable link facts, but tombstone and remove links whose source relation
    # disappeared after an approved lake snapshot or link-mapping change.
    current_relation_ids = {rel.id for rel in relations}
    from app.ontologies.formal_modeling.facts import record_link_fact
    link_query = db.query(LinkInstance).filter(
        LinkInstance.ontology_id == ontology_id)
    if ontology_release_id is not None:
        link_query = link_query.filter(
            LinkInstance.ontology_release_id == ontology_release_id)
    for link in link_query.all():
        relation_id = link.source_relation_id
        if relation_id and relation_id not in current_relation_ids:
            record_link_fact(
                db, ontology_id=ontology_id, link_instance_id=link.id,
                link_type_id=link.link_type_id, exists=False,
                source="pipeline-reconcile",
                ontology_release_id=ontology_release_id)
            db.delete(link)
            summary["removed_link_instances"] += 1

    # 投影改变了本体数据 → 推进修订戳，否则编辑器的乐观并发检测（以
    # OntologyProject.updated_at 为 revision）看不见投影发生过，旧画布
    # 保存会把刚灌入的数据整体覆盖掉
    from datetime import datetime as _dt, timezone as _tz
    from app.models.ontology import OntologyProject as _OP
    proj = db.query(_OP).filter(_OP.id == ontology_id).first()
    if proj is not None:
        proj.updated_at = _dt.now(_tz.utc)

    from app.ontologies.formal_modeling.validation import (
        validate_instance_contract, validate_link_instance_contract,
    )
    all_object_types = db.query(ObjectType).filter(ObjectType.ontology_id == ontology_id).all()
    all_link_types = db.query(LinkType).filter(LinkType.ontology_id == ontology_id).all()
    instance_query = db.query(ObjectInstance).filter(
        ObjectInstance.ontology_id == ontology_id)
    link_query = db.query(LinkInstance).filter(
        LinkInstance.ontology_id == ontology_id)
    if ontology_release_id is not None:
        instance_query = instance_query.filter(
            ObjectInstance.ontology_release_id == ontology_release_id)
        link_query = link_query.filter(
            LinkInstance.ontology_release_id == ontology_release_id)
    all_instances = instance_query.all()
    all_links = link_query.all()
    contract_errors = [
        *validate_instance_contract(all_object_types, all_instances),
        *validate_link_instance_contract(all_link_types, all_instances, all_links),
    ]
    if contract_errors:
        preview = "; ".join(error.get("message", "") for error in contract_errors[:5])
        raise ValueError(f"正规本体投影违反运行契约（{len(contract_errors)} 项）: {preview}")

    db.flush()
    logger.info("正规本体投影完成 ontology=%s: %s", ontology_id, summary)
    return summary
