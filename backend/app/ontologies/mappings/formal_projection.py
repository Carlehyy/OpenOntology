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
    from app.ontologies.mappings.formal_projection_staging import projection_stage
    from app.ontologies.mappings.formal_projection_materialization import materialize_formal_projection
    with projection_stage(db) as lineage:
        return materialize_formal_projection(
            db, ontology_id, mapping_meta, ontology_release_id=ontology_release_id,
            lineage=lineage)
