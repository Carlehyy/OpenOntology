"""跨数据集 Link 推断 — 备用键(近唯一非主键列)值重叠 (PRD §2.4 ③ / F5.1)"""
from unittest.mock import patch

from app.models.ontology import OntologyProject
from app.models.relation import Relation
from app.models.v2.dataset import Dataset, DatasetVersion
from app.models.v2.mapping import OntologyMapping
from app.services.v2.mapping.mapping_service import MappingService


def _add_curated(db, name: str) -> Dataset:
    """Mapping 正式入口只接受 canonical Dataset + DatasetVersion。"""
    ds = Dataset(name=name, kind="curated", schema_json={"review_status": "approved"})
    db.add(ds)
    db.flush()
    db.add(DatasetVersion(
        dataset_id=ds.id, version_no=1, rowcount=1,
        storage_uri=f"s3://test/{ds.id}/v1.parquet",
    ))
    db.commit()
    db.refresh(ds)
    return ds


def test_detect_alt_key_columns():
    """近唯一且非纯数字的非主键列应识别为备用键"""
    rows = [
        {"供应商ID": f"SUP00{i}", "供应商名称": f"供应商{i}号公司", "等级": "S" if i % 2 else "A",
         "年采购额": str(1000 + i)}
        for i in range(1, 9)
    ]
    svc = MappingService(db=None)
    alt = svc._detect_alt_key_columns(rows, pk_col="供应商ID")
    assert "供应商名称" in alt
    assert "等级" not in alt        # 低基数枚举
    assert "年采购额" not in alt    # 纯数字列易误连


def test_detect_alt_key_columns_rejects_unique_status_in_small_sample():
    """两行状态值碰巧都不同，也不能被当作跨表身份键。"""
    rows = [
        {"id": "1", "supplier_name": "Alpha Industries", "rating_status": "approved"},
        {"id": "2", "supplier_name": "Beta Technologies", "rating_status": "pending_review"},
    ]
    alt = MappingService(db=None)._detect_alt_key_columns(rows, pk_col="id")
    assert "supplier_name" in alt
    assert "rating_status" not in alt


def test_alt_key_relation_via_document_mentions(db, admin_user):
    """文档记录的 organizations 字段(逗号分隔)按公司名连到 Supplier"""
    onto = OntologyProject(name="alt-key 测试", domain="供应链",
                           build_mode="pipeline_mapping", created_by=admin_user.id)
    db.add(onto)
    db.commit()
    db.refresh(onto)

    ds_sup = _add_curated(db, "suppliers")
    ds_doc = _add_curated(db, "strategy_docs")

    rows_by_ds = {
        ds_sup.id: [
            {"供应商ID": "SUP001", "供应商名称": "天钢原材料有限公司"},
            {"供应商ID": "SUP002", "供应商名称": "芯联电子科技"},
            {"供应商ID": "SUP003", "供应商名称": "聚合包装集团"},
        ],
        ds_doc.id: [
            # mentioned_supplier 与 organizations 双列命中同一目标 → 产生同名
            # Inference Rule, 回归覆盖 autoflush=False 下的重复 INSERT 崩溃
            {"doc_id": "D1", "section": "供应商分级",
             "organizations": "天钢原材料有限公司, 芯联电子科技",
             "mentioned_supplier": "天钢原材料有限公司"},
            {"doc_id": "D2", "section": "风险条款", "organizations": "聚合包装集团",
             "mentioned_supplier": "芯联电子科技"},
            {"doc_id": "D3", "section": "无关章节", "organizations": "", "mentioned_supplier": ""},
        ],
    }
    for ds, cls, pk in [(ds_sup, "Supplier", "供应商ID"), (ds_doc, "StrategyClause", "doc_id")]:
        ds.schema_json = {**(ds.schema_json or {}), "primary_key": pk}
        db.add(OntologyMapping(ontology_id=onto.id, curated_dataset_id=ds.id,
                               entity_class=cls, field_mapping={"__primary_key__": pk},
                               status="draft", confidence=0.9))
    db.commit()

    svc = MappingService(db)
    with patch("app.ontologies.mappings.mapping_service.load_mapping_source_rows",
               side_effect=lambda _db, mapping, **kwargs: (rows_by_ds[mapping.curated_dataset_id],
                   _db.query(DatasetVersion).filter(DatasetVersion.dataset_id == mapping.curated_dataset_id).first())), \
         patch.object(
             MappingService,
             "_rebuild_neo4j_projection",
             return_value=True,
         ):
        svc.build_all(onto.id)
        svc.build_all(onto.id)  # 幂等性: 重跑不应崩溃

    rels = db.query(Relation).filter(Relation.ontology_id == onto.id,
                                     Relation.type == "HAS_SUPPLIER").all()
    # D1 → 天钢+芯联, D2 → 聚合+芯联(mentioned_supplier) = 4 个去重实体对
    assert len(rels) == 4
    via = {(r.properties or {}).get("via") for r in rels}
    assert "alternate_key" in via


def test_exploded_rows_use_composite_lake_identity_for_relation_pairs(db, admin_user):
    """展开行必须以复合湖主键保持身份，不能把重复 order_id 当作单列主键。"""
    onto = OntologyProject(name="去重测试", domain="供应链",
                           build_mode="pipeline_mapping", created_by=admin_user.id)
    db.add(onto)
    db.commit()
    db.refresh(onto)

    ds_sup = _add_curated(db, "suppliers2")
    ds_po = _add_curated(db, "orders2")

    rows_by_ds = {
        ds_sup.id: [
            {"supplier_id": "SUP001", "name": "甲供应商有限公司"},
            {"supplier_id": "SUP002", "name": "乙供应商有限公司"},
        ],
        ds_po.id: [
            {"order_id": "PO1", "supplier_id": "SUP-001", "items.sku": "A1"},
            {"order_id": "PO1", "supplier_id": "SUP-001", "items.sku": "A2"},
            {"order_id": "PO2", "supplier_id": "SUP-002", "items.sku": "B1"},
        ],
    }
    for ds, cls, pk in [
        (ds_sup, "Supplier", "supplier_id"),
        (ds_po, "PurchaseOrder", "order_id,items.sku"),
    ]:
        ds.schema_json = {**(ds.schema_json or {}), "primary_key": pk}
        db.add(OntologyMapping(ontology_id=onto.id, curated_dataset_id=ds.id,
                               entity_class=cls, field_mapping={"__primary_key__": pk},
                               status="draft", confidence=0.9))
    db.commit()

    svc = MappingService(db)
    with patch("app.ontologies.mappings.mapping_service.load_mapping_source_rows",
               side_effect=lambda _db, mapping, **kwargs: (rows_by_ds[mapping.curated_dataset_id],
                   _db.query(DatasetVersion).filter(DatasetVersion.dataset_id == mapping.curated_dataset_id).first())), \
         patch.object(
             MappingService,
             "_rebuild_neo4j_projection",
             return_value=True,
         ):
        svc.build_all(onto.id)  # 此前会因 UNIQUE constraint 崩溃

    rels = db.query(Relation).filter(Relation.ontology_id == onto.id).all()
    pairs = {(r.source_entity, r.target_entity) for r in rels}
    assert len(pairs) == 3  # PO1/A1、PO1/A2、PO2/B1 各自拥有稳定实体身份
