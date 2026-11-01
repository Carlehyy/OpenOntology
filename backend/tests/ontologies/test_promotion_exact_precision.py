"""Trial → promotion precision boundary regression.

The trial stores exact JSON tokens (``100.5000``). Promotion used to load
``OntologyTrialObject.properties`` through the ORM JSON decoder, which turns
those tokens into floats before copying them to the live instance. This suite
runs the real HTTP trial/promotion front door and asserts the live JSON tokens
and typed SUM keep the source scale.

The PostgreSQL variant runs only with an explicit disposable DSN:

    ONTOLOGY_PROMOTION_PG_URL=postgresql+psycopg2://... \
        uv run pytest -q tests/ontologies/test_promotion_exact_precision.py
"""
from __future__ import annotations

import hashlib
import io
import os
import uuid
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Text, cast, create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.data_channel.datasets.service import DatasetService
from app.exploration.document import canvas_fingerprint
from app.exploration.reverse_projection import project_snapshot_to_canvas
from app.ontologies.formal_modeling.models import (
    LinkInstance as FoLinkInstance,
    ObjectInstance as FoObjectInstance,
    PropertyFact,
)
from app.ontologies.versions.models import (
    OntologyTrialLink,
    OntologyTrialObject,
    OntologyVersion,
)
from app.shared.exact_json import dumps_exact, loads_exact

PG_URL = os.environ.get("ONTOLOGY_PROMOTION_PG_URL")
MONEY_ROWS = [
    {"id": "O-1", "name": "一号订单", "amount": "100.5000",
     "big": "9007199254740993", "src_id": "O-1", "tgt_id": "O-2",
     "fee": "12.3400"},
    {"id": "O-2", "name": "二号订单", "amount": "50.2500",
     "big": "9007199254740992", "src_id": "O-2", "tgt_id": "O-3",
     "fee": "0.10"},
    {"id": "O-3", "name": "三号订单", "amount": "7.0000",
     "big": "7", "src_id": "O-3", "tgt_id": "O-1",
     "fee": "7.0000"},
]
SEMANTIC_DOCUMENT = "# 金额精度回归\n\npromotion exact precision.\n"


class MemoryStorage:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_bytes(self, bucket: str, key: str, data: bytes,
                  content_type: str = "") -> str:
        uri = f"s3://{bucket}/{key}"
        self.objects[uri] = data
        return uri

    def get_object(self, uri: str) -> bytes:
        return self.objects[uri]

    def delete_object(self, uri: str) -> None:
        self.objects.pop(uri, None)


def _csv(rows: list[dict]) -> bytes:
    import csv
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode()


def _attach_semantic_layer(db: Session, version_id: str) -> None:
    row = db.query(OntologyVersion).filter_by(id=version_id).one()
    canvas = project_snapshot_to_canvas(row.snapshot_formal)
    row.snapshot_semantic = {
        "canvas": canvas,
        "canvasFingerprint": canvas_fingerprint(canvas),
        "documentMd": SEMANTIC_DOCUMENT,
        "documentTitle": "金额精度回归",
        "documentFingerprint": hashlib.sha256(
            SEMANTIC_DOCUMENT.encode("utf-8")).hexdigest(),
        "semanticRevision": 1,
    }
    db.commit()


def _money_dataset(db: Session, monkeypatch) -> Any:
    storage = MemoryStorage()
    monkeypatch.setattr(
        "app.data_channel.datasets.service.get_storage_service",
        lambda: storage)
    service = DatasetService(db, storage=storage)
    dataset = service.create_dataset(
        "金额试跑数据", "structured",
        schema_json={
            "primary_key": "id",
            "columns": [
                {"name": "id", "type": "string"},
                {"name": "name", "type": "string"},
                {"name": "amount", "type": "string"},
                {"name": "big", "type": "string"},
                {"name": "src_id", "type": "string"},
                {"name": "tgt_id", "type": "string"},
                {"name": "fee", "type": "string"},
            ],
        },
    )
    db.commit()
    service.create_version(dataset.id, _csv(MONEY_ROWS),
                           rowcount=len(MONEY_ROWS))
    return dataset


def _root(client: TestClient, headers: dict, ontology_id: str) -> dict:
    response = client.get(
        f"/api/v2/ontologies/{ontology_id}/version-tree", headers=headers)
    assert response.status_code == 200, response.text
    return next(item for item in response.json()["data"]["versions"]
                if item["version_number"] == "v0")


def _draft(client: TestClient, headers: dict, ontology_id: str,
           root_id: str) -> dict:
    response = client.post(
        f"/api/v2/ontologies/{ontology_id}/versions/{root_id}/drafts",
        headers=headers, json={"versionLabel": "金额精度草稿"})
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _workspace(draft: dict) -> dict:
    return {
        "version": draft["version_number"],
        "baseRevision": f"{draft['revision']}:{draft['snapshot_hash']}",
        "objectTypes": [{
            "id": "ot-order", "name": "Order", "displayName": "订单",
            "primaryKey": "p-id", "positionX": 10, "positionY": 20,
            "properties": [
                {"id": "p-id", "name": "id", "displayName": "订单号",
                 "type": "string", "required": True},
                {"id": "p-name", "name": "name", "displayName": "名称",
                 "type": "string", "required": True},
                {"id": "p-amount", "name": "amount", "displayName": "金额",
                 "type": "number", "required": False},
                {"id": "p-big", "name": "big", "displayName": "大整数",
                 "type": "number", "required": False},
                {"id": "p-tax", "name": "tax", "displayName": "税额",
                 "type": "number", "required": False,
                 "source": "computed", "computed": True,
                 "functionId": "fn-tax"},
                {"id": "p-big-plus", "name": "big_plus",
                 "displayName": "大整数加一", "type": "number",
                 "required": False, "source": "computed", "computed": True,
                 "functionId": "fn-big-plus"},
            ],
        }],
        "linkTypes": [{
            "id": "lt-order-link", "name": "order_link",
            "displayName": "订单关联",
            "sourceObjectTypeId": "ot-order",
            "targetObjectTypeId": "ot-order",
            "cardinality": "many-to-one",
            "properties": [{
                "id": "p-link-fee", "name": "fee", "displayName": "关联金额",
                "type": "number", "required": False,
            }],
        }], "actions": [],
        "functions": [
            {"id": "fn-tax", "name": "tax", "displayName": "税额",
             "functionType": "object", "language": "expression",
             "targetObjectTypeId": "ot-order", "parameters": [],
             "returnType": "number", "enabled": True,
             "body": "(object.amount or 0) * 1.1"},
            {"id": "fn-big-plus", "name": "big_plus",
             "displayName": "大整数加一", "functionType": "object",
             "language": "expression", "targetObjectTypeId": "ot-order",
             "parameters": [], "returnType": "number", "enabled": True,
             "body": "object.big + 1"},
        ],
        "instances": [], "linkInstances": [],
    }


def _configure_money_draft(client: TestClient, headers: dict,
                           ontology_id: str, draft: dict, db: Session,
                           dataset_id: str) -> dict:
    saved = client.put(
        f"/api/v2/ontologies/{ontology_id}/versions/{draft['id']}/workspace",
        headers=headers, json=_workspace(draft))
    assert saved.status_code == 200, saved.text
    revision = saved.json()["data"]["revision"]
    mapping = client.put(
        f"/api/v2/ontologies/{ontology_id}/versions/{draft['id']}/workspace/mappings",
        headers=headers, json={
            "baseRevision": revision,
            "mappings": [{
                "id": "mapping-order", "curatedDatasetId": dataset_id,
                "entityClass": "Order", "targetObjectTypeId": "ot-order",
                "fieldMapping": {
                    "id": "id", "name": "name", "amount": "amount",
                    "big": "big", "__primary_key__": "id",
                },
                "status": "draft", "confidence": 1,
            }],
            "linkMappings": [{
                "id": "link-order-link", "linkTypeId": "lt-order-link",
                "relationType": "order_link",
                "srcDatasetId": dataset_id, "tgtDatasetId": dataset_id,
                "edgeDatasetId": dataset_id,
                "srcKey": "src_id", "tgtKey": "tgt_id",
                "fieldMapping": {"fee": "fee"},
            }], "sentinels": [],
        })
    assert mapping.status_code == 200, mapping.text
    _attach_semantic_layer(db, draft["id"])
    return draft


def _run_money_promotion(client: TestClient, headers: dict, db: Session,
                         monkeypatch, ontology_id: str) -> dict:
    dataset = _money_dataset(db, monkeypatch)
    root = _root(client, headers, ontology_id)
    draft = _configure_money_draft(
        client, headers, ontology_id,
        _draft(client, headers, ontology_id, root["id"]), db, dataset.id)
    trial = client.post(
        f"/api/v2/ontologies/{ontology_id}/versions/{draft['id']}/trial-runs",
        headers=headers, json={})
    assert trial.status_code == 201, trial.text
    run = trial.json()["data"]
    assert run["status"] == "passed", run

    # Trial rows themselves must already carry the exact source token.
    trial_raw = db.execute(select(cast(
        OntologyTrialObject.properties, Text)).where(
            OntologyTrialObject.trial_run_id == run["id"])).scalars().all()
    assert any('"amount": 100.5000' in raw for raw in trial_raw)
    trial_link_raw = db.execute(select(cast(
        OntologyTrialLink.properties, Text)).where(
            OntologyTrialLink.trial_run_id == run["id"])).scalars().all()
    assert any('"fee": 12.3400' in raw for raw in trial_link_raw)
    assert run["result"]["counts"]["links"] == 3

    impact = client.get(
        f"/api/v2/ontologies/{ontology_id}/versions/{draft['id']}/impact",
        headers=headers).json()["data"]
    root_row = db.query(OntologyVersion).filter_by(id=root["id"]).one()
    root_hash = root_row.snapshot_hash
    old_fact = PropertyFact(
        id="legacy-fact-before-promotion",
        ontology_id=ontology_id,
        instance_id="legacy-instance",
        property_name="legacy",
        value={"v": 1},
        kind="property",
        source="manual",
        ontology_release_id=root["id"],
    )
    db.add(old_fact)
    db.commit()

    promoted = client.post(
        f"/api/v2/ontologies/{ontology_id}/versions/{draft['id']}/promote",
        headers=headers,
        json={"trialRunId": run["id"], "impactHash": impact["impactHash"]})
    assert promoted.status_code == 201, promoted.text
    release = promoted.json()["data"]

    # Live JSON tokens must remain exactly as uploaded (no float canonicalization).
    live_raw = db.execute(select(cast(
        FoObjectInstance.properties, Text)).where(
            FoObjectInstance.ontology_id == ontology_id,
            FoObjectInstance.ontology_release_id == release["id"],
        )).scalars().all()
    joined = "\n".join(live_raw)
    assert '"amount": 100.5000' in joined
    assert '"amount": 50.2500' in joined
    assert '"amount": 7.0000' in joined
    assert '"big": 9007199254740993' in joined

    live_link_raw = db.execute(select(cast(
        FoLinkInstance.properties, Text)).where(
            FoLinkInstance.ontology_id == ontology_id,
            FoLinkInstance.ontology_release_id == release["id"],
        )).scalars().all()
    link_joined = "\n".join(live_link_raw)
    assert '"fee": 12.3400' in link_joined
    assert '"fee": 0.10' in link_joined
    assert '"fee": 7.0000' in link_joined

    link_fact_raw = db.execute(select(cast(
        PropertyFact.value, Text)).where(
            PropertyFact.ontology_id == ontology_id,
            PropertyFact.ontology_release_id == release["id"],
            PropertyFact.kind == "link",
            PropertyFact.property_name == "fee",
        )).scalars().all()
    link_fact_joined = "\n".join(link_fact_raw)
    assert '"v": 12.3400' in link_fact_joined
    assert '"v": 0.10' in link_fact_joined
    assert '"v": 7.0000' in link_fact_joined

    db.expire_all()
    instances = db.query(FoObjectInstance).filter_by(
        ontology_id=ontology_id, ontology_release_id=release["id"]).all()
    by_id = {item.properties["id"]: item for item in instances}
    assert by_id["O-1"].properties["amount"] == 100.5
    # ORM read-back stays int/float for existing consumers.
    assert isinstance(by_id["O-1"].properties["amount"], float)
    assert by_id["O-2"].properties["big"] == 9007199254740992
    assert by_id["O-1"].computed["big_plus"] == 9007199254740994
    assert by_id["O-1"].computed["tax"] == pytest.approx(100.5 * 1.1)
    computed_raw = db.execute(select(cast(
        FoObjectInstance.computed, Text)).where(
            FoObjectInstance.id == by_id["O-1"].id)).scalar_one()
    assert '"big_plus": 9007199254740994' in computed_raw

    # Append-only history and the old snapshot are untouched.
    assert db.query(PropertyFact).filter_by(
        id="legacy-fact-before-promotion").one() is not None
    db.refresh(root_row)
    assert root_row.snapshot_hash == root_hash
    return {"release": release, "root": root, "draft": draft}


def _typed_amount_sum(db: Session, ontology_id: str, release_id: str,
                      object_type_id: str, user) -> str:
    from app.data_channel.query.contracts import Binding, TypedQueryRequest
    from app.data_channel.query.parser import Command, Pipeline, Ref
    from app.data_channel.query.service import QueryService
    from app.ontologies.agent_runtime.models import AgentProfile

    profile = db.query(AgentProfile).filter_by(ontology_id=ontology_id).one_or_none()
    if profile is None:
        db.add(AgentProfile(
            id=str(uuid.uuid4()), ontology_id=ontology_id, enabled=True,
            max_rows_per_query=100))
        db.commit()
    pipeline = Pipeline("c", "c", (
        Command("stats", (("sum", Ref("c", "amount"), "total"),)),))
    request = TypedQueryRequest(
        pipeline=pipeline,
        bindings={"c": Binding(
            kind="ontology", ontology_id=ontology_id,
            release_id=release_id, object_type_id=object_type_id)},
        params={}, page_size=10, result_storage="memory",
        origin={"request": {"kind": "typed"}, "expectations": {}},
        session_kind="ontology", session_id="promotion-exact-precision",
    )
    response = QueryService(
        db, user, secret="promotion-exact-precision").execute_typed(request)
    return response["rows"][0]["total"]


def test_sqlite_promotion_preserves_exact_money(client, auth_headers,
                                                ontology, db, monkeypatch):
    result = _run_money_promotion(
        client, auth_headers, db, monkeypatch, ontology["id"])
    assert result["release"]["version_number"] == "v1"
    # SQLite NUMERIC is float-backed; the exact token is asserted above, and
    # the PostgreSQL test asserts the decimal_string SUM scale.
    from app.ontologies.agent_runtime.read_queries import typed_property
    from sqlalchemy import func
    stmt = select(func.sum(typed_property(
        {"name": "amount", "type": "number"}, "amount", postgres=False))).where(
        FoObjectInstance.ontology_id == ontology["id"],
        FoObjectInstance.ontology_release_id == result["release"]["id"])
    assert float(db.execute(stmt).scalar()) == pytest.approx(157.75)


def test_loads_exact_preserves_scale_bigint_and_computed_mix():
    raw = dumps_exact({
        "amount": Decimal("100.5000"),
        "big": 9007199254740993,
        "tax": Decimal("110.5500"),
        "big_plus": 9007199254740994,
        "text": "100.5000",
        "nullable": None,
        "list": [Decimal("7.0000"), 1],
    })
    decoded = loads_exact(raw)
    assert str(decoded["amount"]) == "100.5000"
    assert str(decoded["tax"]) == "110.5500"
    assert decoded["big"] == 9007199254740993
    assert decoded["big_plus"] == 9007199254740994
    assert str(decoded["list"][0]) == "7.0000"
    assert decoded["text"] == "100.5000"
    assert decoded["nullable"] is None
    assert loads_exact(None) is None


@pytest.mark.skipif(
    not PG_URL,
    reason="set ONTOLOGY_PROMOTION_PG_URL to a disposable PostgreSQL database",
)
def test_pg_promotion_preserves_exact_money(monkeypatch):
    from app.auth.models import User
    from app.database import Base
    from app.deps import get_db
    from app.main import app
    from app.services.auth_service import hash_password

    schema = "oo_promotion_exact_" + uuid.uuid4().hex[:12]
    engine = create_engine(
        PG_URL, json_serializer=dumps_exact,
        connect_args={"options": f"-csearch_path={schema}"})
    try:
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        Base.metadata.create_all(bind=engine)
        SessionLocal = sessionmaker(bind=engine)
        db = SessionLocal()
        try:
            user = User(
                id="promotion-exact-user", username="promotion-exact-user",
                email="promotion-exact@example.invalid",
                password_hash=hash_password("promotion-exact"),
                role="admin")
            db.add(user)
            db.commit()
            app.dependency_overrides[get_db] = lambda: db
            with TestClient(app) as client:
                login = client.post(
                    "/api/v1/auth/login",
                    json={"username": "promotion-exact-user",
                          "password": "promotion-exact"})
                assert login.status_code == 200, login.text
                headers = {
                    "Authorization": f"Bearer {login.json()['data']['access_token']}"}
                created = client.post(
                    "/api/v1/ontologies",
                    json={"name": "金额精度本体", "domain": "供应链"},
                    headers=headers)
                assert created.status_code == 201, created.text
                ontology_id = created.json()["data"]["id"]
                result = _run_money_promotion(
                    client, headers, db, monkeypatch, ontology_id)
                assert result["release"]["version_number"] == "v1"
                live = db.get(User, user.id)
                total = _typed_amount_sum(
                    db, ontology_id, result["release"]["id"],
                    "ot-order", live)
                assert total == "157.7500"
        finally:
            app.dependency_overrides.pop(get_db, None)
            db.close()
    finally:
        try:
            with engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            engine.dispose()
