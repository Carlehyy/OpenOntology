"""真实 PostgreSQL 边界：materialize → json token → provider → typed SUM.

仅在显式提供一次性（专用、可丢弃）PG 库时运行：

    ONTOLOGY_EXACT_DECIMAL_PG_URL=postgresql+psycopg2://... \
        uv run pytest -q tests/ontologies/test_exact_decimal_storage_pg.py

测试在目标库内创建独立 schema 并在结束时 DROP SCHEMA ... CASCADE；不会读写
public/其他 schema 的业务表。覆盖的生产链路：
  project_to_formal_ontology（真实映射物化）
    → fo_object_instances.properties（真实 SQLAlchemy PG JSON bind）
    → resolve_ontology_relation（现有 ontology query provider）
    → QueryService.execute_typed（typed stats SUM → decimal_string）
断言 '100.5000'/'50.2500'/'7.0000' 的 SUM 严格等于 '157.7500'，且
properties ->> 'amount' 保留源字面 token。
"""
from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

PG_URL = os.environ.get("ONTOLOGY_EXACT_DECIMAL_PG_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="set ONTOLOGY_EXACT_DECIMAL_PG_URL to a disposable PostgreSQL database",
)


def test_pg_materialize_to_typed_sum_preserves_scale():
    from app.model_registry import import_all_models

    import_all_models()
    from app.database import Base
    from app.ontologies.entities.models import Entity
    from app.ontologies.formal_modeling.models import (
        ObjectInstance, ObjectType,
    )
    from app.ontologies.formal_modeling.schemas import ObjectTypeCreate
    from app.ontologies.mappings.formal_projection import project_to_formal_ontology
    from app.ontologies.projects.models import OntologyProject
    from app.ontologies.release_context import create_initial_release
    from app.auth.models import User
    from app.data_channel.query.contracts import Binding, TypedQueryRequest
    from app.data_channel.query.parser import Command, Pipeline, Ref
    from app.data_channel.query.service import QueryService
    from app.ontologies.agent_runtime.models import AgentProfile
    from app.ontologies.sentinels.cdc import register_cdc
    from app.shared.exact_json import dumps_exact

    register_cdc()

    schema = "oo_exact_decimal_" + uuid.uuid4().hex[:12]
    engine = create_engine(
        PG_URL,
        json_serializer=dumps_exact,
        connect_args={"options": f"-csearch_path={schema}"},
    )
    try:
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        db = Session()
        try:
            user = User(id="pg-probe-user", username="pg-probe-user",
                        email="pg-probe@example.invalid", password_hash="x",
                        role="admin")
            project = OntologyProject(
                id="pg-probe-ont", name="PG exact decimals", domain="test",
                status="published", created_by=user.id,
            )
            db.add_all([user, project])
            db.flush()
            properties = [
                {"id": "pk", "name": "pk", "type": "string", "primaryKeyPart": 1},
                {"id": "amount", "name": "amount", "type": "number"},
                {"id": "qty", "name": "qty", "type": "integer"},
                {"id": "fee", "name": "fee", "type": "number"},
            ]
            object_type = ObjectType(
                id="pg-probe-type", ontology_id=project.id, name="Contract",
                display_name="Contract", primary_key="pk", properties=properties,
            )
            db.add(object_type)
            db.flush()
            snapshot = {
                "objectTypes": [{
                    **ObjectTypeCreate(
                        name="Contract", display_name="Contract",
                        primary_key="pk", properties=properties,
                    ).model_dump(by_alias=True),
                    "id": object_type.id,
                }],
                "linkTypes": [], "actions": [], "functions": [],
            }
            release = create_initial_release(
                db, project, snapshot=snapshot, created_by=user.id)
            db.add(AgentProfile(
                id="pg-probe-profile", ontology_id=project.id, enabled=True,
                max_rows_per_query=100,
            ))
            db.execute(Entity.__table__.insert(), [
                {"id": "pe1", "ontology_id": project.id, "type": "Contract",
                 "name_cn": "c1",
                 "properties": {"pk": "c1", "amount": "100.5000",
                                "qty": "10", "fee": "0.10"}},
                {"id": "pe2", "ontology_id": project.id, "type": "Contract",
                 "name_cn": "c2",
                 "properties": {"pk": "c2", "amount": "50.2500",
                                "qty": "20", "fee": "2.5"}},
                {"id": "pe3", "ontology_id": project.id, "type": "Contract",
                 "name_cn": "c3",
                 "properties": {"pk": "c3", "amount": "7.0000",
                                "qty": "30", "fee": ""}},
                {"id": "pe4", "ontology_id": project.id, "type": "Contract",
                 "name_cn": "c4",
                 "properties": {"pk": "c4", "amount": "", "qty": "", "fee": ""}},
            ])
            db.commit()

            meta = {"m": {
                "entity_class": "Contract", "pk_col": "pk",
                "target_object_type_id": object_type.id,
                "property_mappings": [
                    {"column": "pk", "property": "pk"},
                    {"column": "amount", "property": "amount"},
                    {"column": "qty", "property": "qty"},
                    {"column": "fee", "property": "fee"},
                ],
                "binding_context": {"source_dataset_version_id": "pg-sv1"},
            }}
            summary = project_to_formal_ontology(
                db, project.id, meta, ontology_release_id=release.id)
            assert summary["object_instances"] == 4
            db.commit()

            # 存储层：PG json 列逐字保留源 token（不经过 ORM 反序列化）。
            tokens = {
                pk: (amount, fee)
                for pk, amount, fee in db.execute(text(
                    "SELECT properties ->> 'pk', properties ->> 'amount', "
                    "properties ->> 'fee' FROM fo_object_instances "
                    "WHERE ontology_id = :oid"
                ), {"oid": project.id}).all()
            }
            assert tokens == {
                "c1": ("100.5000", "0.10"), "c2": ("50.2500", "2.5"),
                "c3": ("7.0000", None), "c4": (None, None),
            }
            # sentinel 事件流（同为 JSON 列）也保 exact token。created 事件的
            # new_value 是裸标量 JSON（不是 {"v": ...} 包装），用 #>> '{}' 取文本。
            amount_events = db.execute(text(
                "SELECT new_value #>> '{}' FROM sentinel_event_log "
                "WHERE instance_id IN (SELECT id FROM fo_object_instances "
                "WHERE ontology_id = :oid) AND key = 'amount'"
            ), {"oid": project.id}).scalars().all()
            assert set(amount_events) == {"100.5000", "50.2500", "7.0000", None}

            # 真实 provider + typed decimal_string SUM（不同 scale 单列验证）。
            from app.data_channel.query.handles import REGISTRY

            def typed_sum(property_name: str) -> str:
                pipeline = Pipeline("c", "c", (
                    Command("stats", (("sum", Ref("c", property_name), "total"),)),))
                request = TypedQueryRequest(
                    pipeline=pipeline,
                    bindings={"c": Binding(
                        kind="ontology", ontology_id=project.id,
                        release_id=release.id, object_type_id=object_type.id)},
                    params={}, page_size=10, result_storage="memory",
                    origin={"request": {"kind": "typed"}, "expectations": {}},
                    session_kind="ontology", session_id="pg-exact-decimal",
                )
                service = QueryService(db, user, secret="pg-probe-secret")
                response = None
                try:
                    response = service.execute_typed(request)
                finally:
                    # Memory mode registers a process-global handle; release
                    # it explicitly so the test owns no lingering resources.
                    if response is not None:
                        query_id = response["queryId"]
                        assert service.cancel(query_id)["cancelled"] is True
                        assert query_id not in REGISTRY.handles
                return response["rows"][0]["total"]

            assert typed_sum("amount") == "157.7500"
            assert typed_sum("fee") == "2.60"  # 0.10 + 2.5，scale=2

            # native float 与 Decimal 混合消费者：手工实例带 Python float，
            # 仍走同一条 typed 读取/SUM 路径，且 ORM 读回仍是 float。
            db.add(ObjectInstance(
                id="pg-native-float", ontology_id=project.id,
                ontology_release_id=release.id, object_type_id=object_type.id,
                properties={"pk": "c5", "amount": 0.1, "qty": 1, "fee": 0.1},
                computed={}, source="manual",
            ))
            db.commit()
            native_token = db.execute(text(
                "SELECT properties ->> 'amount' FROM fo_object_instances "
                "WHERE id = 'pg-native-float'")).scalar_one()
            assert native_token == "0.1"
            assert typed_sum("amount") == "157.8500"
            # ORM 读回仍是数值消费者语义。
            db.expire_all()
            inst = db.query(ObjectInstance).filter_by(
                ontology_id=project.id, external_id="pe1").one()
            assert inst.properties["amount"] == 100.5
            assert isinstance(inst.properties["qty"], int)
            native = db.get(ObjectInstance, "pg-native-float")
            assert native.properties["amount"] == 0.1
            assert isinstance(native.properties["amount"], float)
            # 本次查询的 memory handles 已全部释放（不遗留进程内结果）。
            assert not [handle for handle in REGISTRY.handles.values()
                        if handle.owner_user_id == str(user.id)]
        finally:
            db.close()
    finally:
        try:
            with engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            engine.dispose()
