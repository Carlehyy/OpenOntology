"""胖关系（连接表 + 边属性）端到端投影测试。

覆盖 LPG 路线的核心诉求：关系映射指向一张连接表，两端外键各自反查实体，
连接表的属性列被采集进 LinkInstance.properties，并作为 Fact 进入事实流；
同一对实体之间的多条边（连接表多行）不被去重合并。
"""
from unittest.mock import patch

from app.models.ontology import OntologyProject
from app.models.relation import Relation
from app.models.v2.dataset import Dataset, DatasetVersion
from app.models.v2.curated import CuratedReview
from app.models.v2.mapping import OntologyMapping, OntologyLinkMapping
from app.ontologies.formal_modeling.models import LinkInstance, LinkType
from app.services.v2.mapping.mapping_service import MappingService
from .minio_fixture import store_rows


def _add_curated(db, name: str) -> Dataset:
    ds = Dataset(name=name, kind="curated", schema_json={"review_status": "approved"})
    db.add(ds)
    db.flush()
    version = DatasetVersion(
        dataset_id=ds.id, version_no=1, rowcount=1,
        storage_uri=f"s3://test/{ds.id}/v1.parquet",
    )
    db.add(version)
    db.flush()
    db.add(CuratedReview(
        curated_dataset_id=ds.id,
        dataset_version_id=version.id,
        status="approved",
    ))
    db.commit()
    db.refresh(ds)
    return ds


def _build_twice(db, onto_id, rows_by_ds):
    """跑两遍 build_all，顺带验证幂等（重跑不重复建边）。"""
    svc = MappingService(db)
    with patch.object(
        MappingService,
        "_rebuild_neo4j_projection",
        return_value=True,
    ):
        svc.build_all(onto_id)
        svc.build_all(onto_id)  # 幂等：确定性 id 去重，重跑不应翻倍


def test_fat_relationship_via_junction_table(db, admin_user, mapping_storage):
    """订单-[含明细]->商品：连接表 order_items 带 quantity/unit_price 边属性。

    同一订单 O1 两次购买同一商品 P1（连接表两行，数量不同）→ 必须是两条独立的边，
    且各自带自己的边属性，不被去重成一条。
    """
    onto = OntologyProject(name="胖关系测试", domain="零售",
                           build_mode="pipeline_mapping", created_by=admin_user.id)
    db.add(onto)
    db.commit()
    db.refresh(onto)

    ds_order = _add_curated(db, "orders")
    ds_product = _add_curated(db, "products")
    ds_edge = _add_curated(db, "order_items")

    rows_by_ds = {
        ds_order.id: [
            {"order_id": "O1", "buyer": "Alice"},
            {"order_id": "O2", "buyer": "Bob"},
        ],
        ds_product.id: [
            {"sku": "P1", "name": "Widget"},
            {"sku": "P2", "name": "Gadget"},
        ],
        # 连接表：两端外键 order_id / sku + 边属性 qty / price
        ds_edge.id: [
            {"line_id": "L1", "order_id": "O1", "sku": "P1", "qty": "3", "price": "10"},
            {"line_id": "L2", "order_id": "O1", "sku": "P1", "qty": "5", "price": "9"},   # 同一对，第二条边
            {"line_id": "L3", "order_id": "O1", "sku": "P2", "qty": "1", "price": "20"},
            {"line_id": "L4", "order_id": "O2", "sku": "P2", "qty": "2", "price": "20"},
        ],
    }

    # 连接表不需要对象映射，但 canonical version reader 仍要求声明其身份列。
    ds_edge.schema_json = {**(ds_edge.schema_json or {}), "primary_key": "line_id"}
    for ds in (ds_order, ds_product, ds_edge):
        store_rows(db, mapping_storage, ds, rows_by_ds[ds.id])

    # 只给两个端点建对象映射；连接表不是对象，仅作关系数据源
    for ds, cls, pk in [(ds_order, "Order", "order_id"), (ds_product, "Product", "sku")]:
        ds.schema_json = {**(ds.schema_json or {}), "primary_key": pk}
        db.add(OntologyMapping(ontology_id=onto.id, curated_dataset_id=ds.id,
                               entity_class=cls, field_mapping={"__primary_key__": pk},
                               status="draft", confidence=0.9))
    # 胖关系映射：指向连接表，采集边属性
    db.add(OntologyLinkMapping(
        ontology_id=onto.id,
        src_dataset_id=ds_order.id, tgt_dataset_id=ds_product.id,
        relation_type="CONTAINS",
        src_key="order_id", tgt_key="sku",
        edge_dataset_id=ds_edge.id,
        field_mapping={"quantity": "qty", "unit_price": "price"},
        status="active",
    ))
    db.commit()

    _build_twice(db, onto.id, rows_by_ds)

    # ① 扁平层：4 条 CONTAINS 关系（连接表 4 行全部成边），未被幂等重跑翻倍
    rels = db.query(Relation).filter(Relation.ontology_id == onto.id,
                                     Relation.type == "CONTAINS").all()
    assert len(rels) == 4, f"应有 4 条扁平关系，实得 {len(rels)}"

    # ② 正规层：投影出 4 条 LinkInstance —— 同一对实体的两条边没被去重合并成一条
    lis = db.query(LinkInstance).filter(LinkInstance.ontology_id == onto.id).all()
    assert len(lis) == 4, f"应有 4 条 LinkInstance（含同对实体的重复边），实得 {len(lis)}"

    # ③ 每条边都带业务属性，且不含映射记账用的内部键
    internal = {"mapping_type", "src_key", "tgt_key", "cardinality", "__edge_key__"}
    for li in lis:
        props = li.properties or {}
        assert "quantity" in props and "unit_price" in props, f"边属性缺失: {props}"
        assert not (internal & set(props.keys())), f"内部记账键泄漏进边属性: {props}"

    # ④ 同一对 (Order O1, Product P1) 恰有 2 条边，数量分别为 3 与 5
    by_pair: dict[tuple, list] = {}
    for li in lis:
        by_pair.setdefault((li.source_object_id, li.target_object_id), []).append(li)
    dup_pairs = {k: v for k, v in by_pair.items() if len(v) == 2}
    assert len(dup_pairs) == 1, f"应恰有一对实体拥有 2 条边，实得 {len(dup_pairs)}"
    dup_edges = next(iter(dup_pairs.values()))
    quantities = {str((li.properties or {}).get("quantity")) for li in dup_edges}
    assert quantities == {"3", "5"}, f"两条重复边应保留各自数量 3/5，实得 {quantities}"


def test_thin_fk_link_still_works(db, admin_user, mapping_storage):
    """回归：不带 edge_dataset_id 的直连外键瘦关系行为不变（走原路径）。"""
    onto = OntologyProject(name="瘦关系回归", domain="供应链",
                           build_mode="pipeline_mapping", created_by=admin_user.id)
    db.add(onto)
    db.commit()
    db.refresh(onto)

    ds_order = _add_curated(db, "po")
    ds_sup = _add_curated(db, "sup")

    rows_by_ds = {
        ds_order.id: [
            {"order_id": "PO1", "supplier_id": "S1"},
            {"order_id": "PO2", "supplier_id": "S2"},
        ],
        ds_sup.id: [
            {"supplier_id": "S1", "name": "甲"},
            {"supplier_id": "S2", "name": "乙"},
        ],
    }
    for ds, cls, pk in [(ds_order, "PurchaseOrder", "order_id"), (ds_sup, "Supplier", "supplier_id")]:
        ds.schema_json = {**(ds.schema_json or {}), "primary_key": pk}
        db.add(OntologyMapping(ontology_id=onto.id, curated_dataset_id=ds.id,
                               entity_class=cls, field_mapping={"__primary_key__": pk},
                               status="draft", confidence=0.9))
        store_rows(db, mapping_storage, ds, rows_by_ds[ds.id])
    # 瘦关系：源表 supplier_id == 目标表 supplier_id，无连接表、无边属性
    db.add(OntologyLinkMapping(
        ontology_id=onto.id,
        src_dataset_id=ds_order.id, tgt_dataset_id=ds_sup.id,
        relation_type="SUPPLIED_BY",
        src_key="supplier_id", tgt_key="supplier_id",
        status="active",
    ))
    db.commit()

    _build_twice(db, onto.id, rows_by_ds)

    lis = db.query(LinkInstance).join(
        LinkType, LinkInstance.link_type_id == LinkType.id
    ).filter(LinkInstance.ontology_id == onto.id, LinkType.name == "SUPPLIED_BY").all()
    assert len(lis) == 2, f"瘦关系应产生 2 条边，实得 {len(lis)}"
    # 瘦关系无业务边属性
    for li in lis:
        assert not (li.properties or {}), f"瘦关系不应有边属性，实得 {li.properties}"
