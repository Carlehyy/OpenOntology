#!/usr/bin/env python3
"""Unified agent-query acceptance against real PostgreSQL (YIYA-36 query core).

Exercises the ontology assistant unified query service end-to-end on a real
PostgreSQL: projection/filter/aggregate, computed derived fields, cross-ontology
typed joins with user-registered assertion evidence (structure-only
field-existence is rejected), stable cursor pagination, transaction pinning
(REPEATABLE READ + SET LOCAL budgets), the non-superuser role budget path
(unset fail-closed → ALTER ROLE cap too wide fail-closed → proper cap on a
fresh connection succeeds), and quantified run facts (SQL statement counts
that must not grow with instance volume, page bytes, per-scenario durations,
on-demand structure isolation from unrelated ontologies).

Safety: requires the explicit ``OPENONTOLOGY_ONTOLOGY_ASSISTANT_E2E=1``
sentinel and ``ENVIRONMENT=test``.  All fixtures live in one UUID-suffixed
private schema (``search_path``) that this script creates and always drops in
``finally``; other schemas/databases are never modified.  The throwaway
non-superuser login role is likewise created and dropped by the script.  The
JSON report contains only fixed fields, counts, booleans, and durations — no
connection strings, credentials, role names, or schema names.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import sys
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

SENTINEL = "OPENONTOLOGY_ONTOLOGY_ASSISTANT_E2E"
DEFAULT_REPORT = BACKEND_DIR.parent / ".artifacts" / "ontology-assistant" / \
    "query-postgres.json"

REPORT_STAGES = {
    "starting",
    "database_guard",
    "schema_setup",
    "fixtures",
    "query_core",
    "computed_fields",
    "typed_join",
    "pagination",
    "repeatable_read",
    "budgets",
    "metrics",
    "complete",
}
REPORT_FIELDS = {
    "schema_version",
    "result",
    "stage",
    "server_version_major",
    "primary_role_superuser",
    "low_privilege_role_verified",
    "low_privilege_role_dropped",
    "set_local_statements",
    "isolation_statements",
    "cases",
    "sql_counts",
    "sql_shape_stable",
    "page_bytes",
    "durations_ms",
    "structure_bytes",
}
REPORT_CASES = {
    "projection_filter",
    "aggregate_group",
    "declared_relation",
    "pure_aggregation",
    "computed_fields",
    "computed_join_key",
    "typed_join",
    "invalid_typed_key_rejected",
    "fixture_evidence_rejected",
    "structure_only_rejected",
    "pagination_stable",
    "repeatable_read_snapshot",
    "concurrent_visible_after",
    "low_privilege_unset_rejected",
    "low_privilege_wide_rejected",
    "low_privilege_query",
}

ORDERS_OT = "ot-order"
SUPPLIER_OT = "ot-supplier"
PARTY_OT = "ot-party"

_SNAPSHOT_ORDERS = {
    "objectTypes": [
        {
            "id": ORDERS_OT, "name": "Order", "displayName": "订单",
            "primaryKey": "order_no",
            "properties": [
                {"id": "p1", "name": "order_no", "displayName": "订单号",
                 "type": "string", "required": True},
                {"id": "p2", "name": "status", "displayName": "状态",
                 "type": "string"},
                {"id": "p3", "name": "amount", "displayName": "金额",
                 "type": "number"},
                {"id": "p4", "name": "tenant_key", "displayName": "租户键",
                 "type": "string"},
                {"id": "p5", "name": "risk_score", "displayName": "风险分",
                 "type": "number", "source": "computed"},
                {"id": "p6", "name": "tenant_ref", "displayName": "租户引用",
                 "type": "string", "source": "computed"},
            ],
            "positionX": 0, "positionY": 0,
        },
        {
            "id": SUPPLIER_OT, "name": "Supplier", "displayName": "供应商",
            "primaryKey": "sname",
            "properties": [
                {"id": "p7", "name": "sname", "displayName": "名称",
                 "type": "string", "required": True},
            ],
            "positionX": 0, "positionY": 0,
        },
    ],
    "linkTypes": [
        {
            "id": "lt-1", "name": "order_supplier",
            "displayName": "订单-供应商",
            "sourceObjectTypeId": ORDERS_OT,
            "targetObjectTypeId": SUPPLIER_OT,
            "cardinality": "many-to-one",
        },
    ],
}
_SNAPSHOT_PARTIES = {
    "objectTypes": [
        {
            "id": PARTY_OT, "name": "Party", "displayName": "客户",
            "primaryKey": "party_key",
            "properties": [
                {"id": "q1", "name": "party_key", "displayName": "客户键",
                 "type": "string", "required": True},
                {"id": "q2", "name": "label", "displayName": "名称",
                 "type": "string"},
            ],
            "positionX": 0, "positionY": 0,
        },
    ],
    "linkTypes": [],
}


def _report_template() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "result": "fail",
        "stage": "starting",
        "server_version_major": 0,
        "primary_role_superuser": None,
        "low_privilege_role_verified": False,
        "low_privilege_role_dropped": None,
        "set_local_statements": {"primary": 0, "low_privilege": None},
        "isolation_statements": 0,
        "cases": {name: False for name in sorted(REPORT_CASES)},
        "sql_counts": {"instances_40": 0, "instances_80": 0},
        "sql_shape_stable": False,
        "page_bytes": 0,
        "durations_ms": {},
        "structure_bytes": {"before": 0, "after": 0, "isolated": False},
    }


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, (bool, int)) or value is None:
        return value
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return {str(key): _sanitize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item) for item in value]
    raise ValueError(f"unsupported report value type: {type(value)!r}")


def _assert_sanitized_report(report: dict[str, Any],
                             sensitive_values: set[str]) -> bytes:
    if set(report) != REPORT_FIELDS:
        raise ValueError("report schema mismatch")
    if report["schema_version"] != 1:
        raise ValueError("report schema version mismatch")
    if report["result"] not in {"pass", "fail"}:
        raise ValueError("invalid report result")
    if report["stage"] not in REPORT_STAGES:
        raise ValueError("invalid report stage")
    if set(report["cases"]) != REPORT_CASES:
        raise ValueError("report case set mismatch")
    payload = json.dumps(_sanitize_value(report), sort_keys=True,
                         indent=2) + "\n"
    for value in sensitive_values:
        if value and value in payload:
            raise ValueError("sensitive value reached report payload")
    return payload.encode("utf-8")


def _write_report(path: Path, report: dict[str, Any],
                  sensitive_values: set[str]) -> None:
    payload = _assert_sanitized_report(report, sensitive_values)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        path.chmod(0o600)
    finally:
        if tmp.exists():
            tmp.unlink()
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ValueError("report mode must be 0600")


def _guard_database_url() -> str:
    if os.environ.get(SENTINEL) != "1":
        raise ValueError(f"{SENTINEL} must be 1")
    if os.environ.get("ENVIRONMENT") != "test":
        raise ValueError("ENVIRONMENT must be test")
    database_url = os.environ.get("DATABASE_URL", "")
    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("DATABASE_URL must use PostgreSQL")
    if parsed.query or parsed.fragment:
        raise ValueError(
            "DATABASE_URL must not contain query or fragment overrides")
    if not parsed.username or not parsed.password:
        raise ValueError("DATABASE_URL must contain credentials")
    return database_url


def _isolation_url(raw_url: str, schema: str, *, username: str | None = None,
                   password: str | None = None) -> str:
    """Rebuild the URL with a private search_path (and optional role).

    ``str(url)`` 会把密码掩码成 ``***``，必须用 render_as_string 输出真实
    连接串（仅内存内传递，不落日志/报告）。
    """
    from sqlalchemy.engine import make_url

    url = make_url(raw_url)
    return url.set(
        username=username or url.username,
        password=password or url.password,
        query={"options": f"-csearch_path={schema}"},
    ).render_as_string(hide_password=False)


class _StatementCounter:
    """Count engine statements during a block.

    Mirror of ``tests.conftest.QueryCounter``: importing the test conftest
    from this standalone script is unsafe because it force-sets the SQLite
    ``DATABASE_URL`` and imports the full FastAPI app at module scope.
    """

    def __init__(self, engine):
        self._engine = engine
        self.count = 0
        self.set_local = 0
        self.isolation = 0

    def _before_cursor_execute(self, conn, cursor, statement, parameters,
                               context, executemany):
        self.count += 1
        head = statement.lstrip()[:40].upper()
        if head.startswith("SET LOCAL"):
            self.set_local += 1
        elif head.startswith("SET TRANSACTION ISOLATION LEVEL"):
            self.isolation += 1

    def __enter__(self):
        from sqlalchemy import event

        event.listen(self._engine, "before_cursor_execute",
                     self._before_cursor_execute)
        return self

    def __exit__(self, *exc):
        from sqlalchemy import event

        event.remove(self._engine, "before_cursor_execute",
                     self._before_cursor_execute)


def _seed_world(session) -> SimpleNamespace:
    from app.auth.models import User
    from app.models.ontology_formal import LinkInstance, ObjectInstance
    from app.ontologies.projects.models import OntologyProject
    from app.ontologies.release_context import create_initial_release

    run_id = uuid.uuid4().hex[:8]
    user = User(
        id=f"oq-e2e-user-{run_id}",
        username=f"oq_e2e_admin_{run_id}",
        email=f"oq-e2e-{run_id}@example.invalid",
        password_hash="not-used-in-service-layer",
        role="admin",
    )
    session.add(user)
    session.flush()

    orders = OntologyProject(
        id=f"oq-e2e-orders-{run_id}", name="订单本体E2E", domain="供应链",
        created_by=user.id)
    parties = OntologyProject(
        id=f"oq-e2e-parties-{run_id}", name="客户本体E2E", domain="供应链",
        created_by=user.id)
    session.add_all([orders, parties])
    session.flush()
    release_a = create_initial_release(
        session, orders, snapshot=_SNAPSHOT_ORDERS, created_by=user.id)
    release_b = create_initial_release(
        session, parties, snapshot=_SNAPSHOT_PARTIES, created_by=user.id)

    order_specs = [
        ("o1", "SO-001", "pending", 100, "t-a", 10),
        ("o2", "SO-002", "paid", 250, "t-a", 30),
        ("o3", "SO-003", "paid", None, "t-b", 20),
    ]
    rows = [
        ObjectInstance(
            id=f"inst-{tag}-{run_id}", ontology_id=orders.id,
            ontology_release_id=release_a.id, object_type_id=ORDERS_OT,
            properties={"order_no": order_no, "status": status,
                        "amount": amount, "tenant_key": tenant},
            computed={"risk_score": risk, "tenant_ref": tenant},
        )
        for tag, order_no, status, amount, tenant, risk in order_specs
    ]
    rows.append(ObjectInstance(
        id=f"inst-s1-{run_id}", ontology_id=orders.id,
        ontology_release_id=release_a.id, object_type_id=SUPPLIER_OT,
        properties={"sname": "华南电子"}, computed={}))
    rows.append(LinkInstance(
        id=f"li-1-{run_id}", ontology_id=orders.id,
        ontology_release_id=release_a.id, link_type_id="lt-1",
        source_object_id=f"inst-o1-{run_id}",
        target_object_id=f"inst-s1-{run_id}"))
    rows.extend(
        ObjectInstance(
            id=f"inst-{tag}-{run_id}", ontology_id=parties.id,
            ontology_release_id=release_b.id, object_type_id=PARTY_OT,
            properties={"party_key": key, "label": label}, computed={})
        for tag, key, label in (("pa", "t-a", "租户A"), ("pb", "t-b", "租户B")))
    session.add_all(rows)
    session.commit()

    return SimpleNamespace(
        run_id=run_id, user_id=user.id,
        orders=SimpleNamespace(ontology_id=orders.id, release_id=release_a.id,
                               object_type_id=ORDERS_OT),
        parties=SimpleNamespace(ontology_id=parties.id,
                                release_id=release_b.id,
                                object_type_id=PARTY_OT),
    )


def _register_assertion(session, world: SimpleNamespace, *,
                        left_property: str) -> str:
    """登记用户关联含义声明（evidenceRef 有用户依据的通路）。"""
    from app.ontologies.agent_query.models import AgentJoinAssertion

    assertion = AgentJoinAssertion(
        id=f"oq-e2e-as-{uuid.uuid4().hex[:8]}",
        created_by=world.user_id,
        left_ontology_id=world.orders.ontology_id,
        left_object_type_id=ORDERS_OT,
        left_property=left_property,
        right_ontology_id=world.parties.ontology_id,
        right_object_type_id=PARTY_OT,
        right_property="party_key",
        key_type="string",
        business_meaning="E2E：用户登记的租户业务键声明",
    )
    session.add(assertion)
    session.commit()
    return f"assertion:{assertion.id}"


def _scale_orders(session, world: SimpleNamespace, start: int,
                  stop: int) -> None:
    from app.models.ontology_formal import ObjectInstance

    session.add_all(
        ObjectInstance(
            id=f"inst-oq-n{i}-{world.run_id}",
            ontology_id=world.orders.ontology_id,
            ontology_release_id=world.orders.release_id,
            object_type_id=ORDERS_OT,
            properties={"order_no": f"SO-{i:03d}", "status": "paid",
                        "amount": i, "tenant_key": "t-a"},
            computed={"risk_score": i, "tenant_ref": "t-a"})
        for i in range(start, stop))
    session.commit()


def _concurrent_insert(engine_url: str, world: SimpleNamespace, *,
                       order_no: str) -> None:
    """Commit a new order row from a separate connection (outside app session)."""
    from sqlalchemy import create_engine, text

    engine = create_engine(engine_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO fo_object_instances "
                    "(id, ontology_id, ontology_release_id, object_type_id, "
                    " properties, computed, created_at, updated_at) "
                    "VALUES (:id, :oid, :rid, :ot, CAST(:props AS json), "
                    " CAST(:comp AS json), now(), now())"
                ),
                {
                    "id": f"inst-cc-{order_no}-{world.run_id}",
                    "oid": world.orders.ontology_id,
                    "rid": world.orders.release_id,
                    "ot": ORDERS_OT,
                    "props": json.dumps(
                        {"order_no": order_no, "status": "paid",
                         "amount": 999, "tenant_key": "t-a"}),
                    "comp": json.dumps(
                        {"risk_score": 999, "tenant_ref": "t-a"}),
                },
            )
    finally:
        engine.dispose()


def _query_payload(orders: SimpleNamespace, **overrides) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "sources": [{
            "alias": "a",
            "ontologyId": orders.ontology_id,
            "releaseId": orders.release_id,
            "objectTypeId": ORDERS_OT,
        }],
        "joins": [],
        "select": [
            {"source": "a", "property": "order_no", "as": "orderNo"},
            {"source": "a", "property": "amount", "as": "amount"},
        ],
        "orderBy": [{"output": "orderNo", "direction": "asc"}],
        "pageSize": 10,
    }
    payload.update(overrides)
    return payload


def _join_payload(left: SimpleNamespace, right: SimpleNamespace, *,
                  left_property: str, evidence_refs: list[str],
                  select: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "sources": [
            {"alias": "a", "ontologyId": left.ontology_id,
             "releaseId": left.release_id, "objectTypeId": left.object_type_id},
            {"alias": "b", "ontologyId": right.ontology_id,
             "releaseId": right.release_id,
             "objectTypeId": right.object_type_id},
        ],
        "joins": [{
            "left": "a", "right": "b", "type": "inner",
            "on": {
                "kind": "explicit_key",
                "leftProperty": left_property,
                "rightProperty": "party_key",
                "keyType": "string",
                "cardinality": "N:1",
                "businessMeaning": "模型填写文案（不构成证据）",
                "evidenceRefs": evidence_refs,
            },
        }],
        "select": select,
        "orderBy": [{"output": select[0]["as"]}],
        "pageSize": 10,
    }


def _structure_refs(left: SimpleNamespace, right: SimpleNamespace,
                    left_property: str) -> list[str]:
    return [
        f"structure:{left.ontology_id}:{left.release_id}:"
        f"{left.object_type_id}:{left_property}",
        f"structure:{right.ontology_id}:{right.release_id}:"
        f"{right.object_type_id}:party_key",
    ]


def _run_query(session, principal, payload: dict[str, Any]) -> dict:
    from app.ontologies.agent_query import schemas as QS
    from app.ontologies.agent_query.service import execute_unified_query

    request = QS.QueryRequest.model_validate(payload)
    return execute_unified_query(session, principal, request,
                                 session_kind="direct",
                                 session_id=str(principal.id))


def _purge_snapshots(session_factory, world: SimpleNamespace) -> None:
    """场景间清理本人查询快照 — 配额是每用户 10 个，全程会超限。"""
    from app.ontologies.agent_query.models import AgentQuerySnapshot

    session = session_factory()
    try:
        session.query(AgentQuerySnapshot).filter(
            AgentQuerySnapshot.owner_user_id == world.user_id).delete(
            synchronize_session=False)
        session.commit()
    finally:
        session.close()


def _seed_noise_ontology(session, world: SimpleNamespace) -> None:
    """50 个无关对象类型进入另一本体：同问题 structure 返回不得扩大。"""
    from app.ontologies.projects.models import OntologyProject
    from app.ontologies.release_context import create_initial_release

    noise = OntologyProject(
        id=f"oq-e2e-noise-{world.run_id}", name="噪声本体E2E", domain="噪声",
        created_by=world.user_id)
    session.add(noise)
    session.flush()
    snapshot = {
        "objectTypes": [
            {
                "id": f"ot-noise-{index:02d}",
                "name": f"Noise{index:02d}", "displayName": f"噪声{index:02d}",
                "properties": [
                    {"id": "n1", "name": "value", "displayName": "值",
                     "type": "string"},
                ],
                "positionX": 0, "positionY": 0,
            }
            for index in range(50)
        ],
        "linkTypes": [],
    }
    create_initial_release(session, noise, snapshot=snapshot,
                           created_by=world.user_id)
    session.commit()


def _run(report_path: Path) -> None:
    report = _report_template()
    sensitive_values: set[str] = set()
    stage = "starting"
    engine = None
    schema = f"oq_query_e2e_{uuid.uuid4().hex[:12]}"
    low_priv_role: str | None = None
    try:
        _write_report(report_path, report, sensitive_values)
        stage = "database_guard"
        report["stage"] = stage
        database_url = _guard_database_url()
        sensitive_values.add(database_url)
        sensitive_values.add(schema)

        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import sessionmaker

        engine = create_engine(_isolation_url(database_url, schema))
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))

        stage = "schema_setup"
        report["stage"] = stage
        import app.models  # noqa: F401 - register base metadata
        from app.auth import models as _auth_models  # noqa: F401
        from app.database import Base
        from app.ontologies.agent_query import models as _aq_models  # noqa: F401
        from app.ontologies.agent_runtime import models as _ar_models  # noqa: F401
        from app.ontologies.formal_modeling import models as _fm_models  # noqa: F401
        from app.ontologies.projects import models as _pj_models  # noqa: F401
        from app.ontologies.versions import models as _vs_models  # noqa: F401
        Base.metadata.create_all(bind=engine)
        with engine.connect() as conn:
            report["server_version_major"] = int(conn.execute(text(
                "SELECT current_setting('server_version_num')")).scalar()) \
                // 10000
            report["primary_role_superuser"] = str(conn.execute(text(
                "SELECT current_setting('is_superuser')")).scalar()
            ).strip().lower() == "on"

        from app.auth.models import User

        session_factory = sessionmaker(bind=engine)

        def principal_of(session):
            return session.query(User).filter_by(id=world.user_id).one()

        stage = "fixtures"
        report["stage"] = stage
        setup_session = session_factory()
        try:
            world = _seed_world(setup_session)
            tenant_assertion = _register_assertion(
                setup_session, world, left_property="tenant_key")
            tenant_ref_assertion = _register_assertion(
                setup_session, world, left_property="tenant_ref")
        finally:
            setup_session.close()
        secondary_url = _isolation_url(database_url, schema)

        # -------------------------------------------------- query_core
        stage = "query_core"
        report["stage"] = stage
        started = time.perf_counter()
        _purge_snapshots(session_factory, world)
        session = session_factory()
        try:
            principal = principal_of(session)
            data = _run_query(session, principal, _query_payload(
                world.orders,
                where=[{"source": "a", "property": "status", "op": "eq",
                        "value": "paid"}]))
            assert data["totalRowCount"] == 2, data["totalRowCount"]
            assert [row["orderNo"] for row in data["rows"]] == \
                ["SO-002", "SO-003"]
            assert data["inputCounts"]["a"] == 3
            assert data["distinctEntityCounts"]["a"] == 2
            report["cases"]["projection_filter"] = True

            grouped = _run_query(session, principal, _query_payload(
                world.orders,
                select=[{"source": "a", "property": "status", "as": "status"}],
                groupBy=[{"source": "a", "property": "status"}],
                aggregates=[
                    {"fn": "count", "as": "rowCount"},
                    {"fn": "sum", "source": "a", "property": "amount",
                     "as": "total"}],
                orderBy=[{"output": "status"}]))
            rows = {row["status"]: row for row in grouped["rows"]}
            assert rows["paid"]["rowCount"] == 2
            assert float(rows["paid"]["total"]) == 250.0
            assert float(rows["pending"]["total"]) == 100.0
            report["cases"]["aggregate_group"] = True

            relation_payload = {
                "sources": [
                    {"alias": "a", "ontologyId": world.orders.ontology_id,
                     "releaseId": world.orders.release_id,
                     "objectTypeId": ORDERS_OT},
                    {"alias": "s", "ontologyId": world.orders.ontology_id,
                     "releaseId": world.orders.release_id,
                     "objectTypeId": SUPPLIER_OT},
                ],
                "joins": [{
                    "left": "a", "right": "s", "type": "left",
                    "on": {"kind": "declared_relation", "linkTypeId": "lt-1",
                           "direction": "source_to_target"}}],
                "select": [
                    {"source": "a", "property": "order_no", "as": "orderNo"},
                    {"source": "s", "property": "sname", "as": "supplier"}],
                "orderBy": [{"output": "orderNo"}],
                "pageSize": 10,
            }
            relation = _run_query(session, principal, relation_payload)
            assert relation["totalRowCount"] == 3
            suppliers = {row["orderNo"]: row["supplier"]
                         for row in relation["rows"]}
            assert suppliers["SO-001"] == "华南电子"
            assert suppliers["SO-002"] is None
            report["cases"]["declared_relation"] = True

            pure = _run_query(session, principal, _query_payload(
                world.orders,
                select=[],
                aggregates=[
                    {"fn": "count", "as": "rowCount"},
                    {"fn": "avg", "source": "a", "property": "amount",
                     "as": "avgAmt"}],
                orderBy=[{"output": "rowCount"}]))
            assert pure["rows"][0]["rowCount"] == 3
            assert float(pure["rows"][0]["avgAmt"]) == 175.0
            report["cases"]["pure_aggregation"] = True
        finally:
            session.close()
        report["durations_ms"]["query_core"] = round(
            (time.perf_counter() - started) * 1000, 1)

        # ---------------------------------------------- computed_fields
        stage = "computed_fields"
        report["stage"] = stage
        started = time.perf_counter()
        _purge_snapshots(session_factory, world)
        session = session_factory()
        try:
            principal = principal_of(session)
            computed = _run_query(session, principal, _query_payload(
                world.orders,
                select=[
                    {"source": "a", "property": "order_no", "as": "orderNo"},
                    {"source": "a", "property": "risk_score", "as": "risk"}],
                where=[{"source": "a", "property": "risk_score", "op": "gte",
                        "value": 20}]))
            risks = {row["orderNo"]: float(row["risk"])
                     for row in computed["rows"]}
            assert risks == {"SO-002": 30.0, "SO-003": 20.0}, risks
            report["cases"]["computed_fields"] = True
        finally:
            session.close()
        report["durations_ms"]["computed_fields"] = round(
            (time.perf_counter() - started) * 1000, 1)

        # ---------------------------------------------------- typed_join
        stage = "typed_join"
        report["stage"] = stage
        started = time.perf_counter()
        _purge_snapshots(session_factory, world)
        from app.ontologies.agent_query.service import QueryError

        session = session_factory()
        try:
            principal = principal_of(session)
            select = [
                {"source": "a", "property": "order_no", "as": "orderNo"},
                {"source": "b", "property": "label", "as": "partyLabel"}]
            joined = _run_query(session, principal, _join_payload(
                world.orders, world.parties, left_property="tenant_key",
                evidence_refs=[tenant_assertion], select=select))
            labels = {row["orderNo"]: row["partyLabel"]
                      for row in joined["rows"]}
            assert labels == {"SO-001": "租户A", "SO-002": "租户A",
                              "SO-003": "租户B"}, labels
            assert joined["joinEvidence"][0]["categories"] == \
                ["user_asserted"]
            report["cases"]["typed_join"] = True

            computed_key = _run_query(session, principal, _join_payload(
                world.orders, world.parties, left_property="tenant_ref",
                evidence_refs=[tenant_ref_assertion], select=select))
            labels2 = {row["orderNo"]: row["partyLabel"]
                       for row in computed_key["rows"]}
            assert labels2 == labels, labels2
            report["cases"]["computed_join_key"] = True

            # 仅字段存在（双侧 structure 引用，无用户依据/发布关联）→ 拒绝
            try:
                _run_query(session, principal, _join_payload(
                    world.orders, world.parties, left_property="tenant_key",
                    evidence_refs=_structure_refs(
                        world.orders, world.parties, "tenant_key"),
                    select=select))
                raise AssertionError("structure-only evidence was accepted")
            except QueryError as exc:
                assert exc.status == 422 and \
                    exc.code == "join_semantics_unverified", \
                    (exc.status, exc.code)
                report["cases"]["structure_only_rejected"] = True

            # fixture 类证据 → 拒绝（类别未知）
            fixture_payload = _join_payload(
                world.orders, world.parties, left_property="tenant_key",
                evidence_refs=["fixture:tenant-key"], select=select)
            try:
                _run_query(session, principal, fixture_payload)
                raise AssertionError("fixture evidence was accepted")
            except QueryError as exc:
                assert exc.status == 422 and \
                    exc.code == "join_semantics_unverified", \
                    (exc.status, exc.code)
                report["cases"]["fixture_evidence_rejected"] = True

            # number 进 string 键 → 422 invalid_typed_key（PG json_typeof 路径）
            from app.models.ontology_formal import ObjectInstance

            party = session.query(ObjectInstance).filter_by(
                id=f"inst-pa-{world.run_id}").one()
            original_props = dict(party.properties or {})
            party.properties = {"party_key": 42, "label": "租户A"}
            session.commit()
            try:
                _run_query(session, principal, _join_payload(
                    world.orders, world.parties, left_property="tenant_key",
                    evidence_refs=[tenant_assertion], select=select))
                raise AssertionError("invalid typed key was accepted")
            except QueryError as exc:
                assert exc.status == 422 and exc.code == "invalid_typed_key", \
                    (exc.status, exc.code)
                assert exc.extra.get("errorCount") == 1
                report["cases"]["invalid_typed_key_rejected"] = True
            finally:
                restored = session.query(ObjectInstance).filter_by(
                    id=f"inst-pa-{world.run_id}").one()
                restored.properties = original_props
                session.commit()
        finally:
            session.close()
        report["durations_ms"]["typed_join"] = round(
            (time.perf_counter() - started) * 1000, 1)

        # ---------------------------------------------------- pagination
        stage = "pagination"
        report["stage"] = stage
        started = time.perf_counter()
        _purge_snapshots(session_factory, world)
        from app.ontologies.agent_query.service import fetch_unified_query_page

        session = session_factory()
        try:
            principal = principal_of(session)
            page1 = _run_query(session, principal, _query_payload(
                world.orders, pageSize=2))
            assert page1["pageRowCount"] == 2 and page1["hasNext"]
            cursor = page1["cursor"]
            report["page_bytes"] = len(json.dumps(
                page1["rows"], ensure_ascii=False, default=str)
                .encode("utf-8"))

            _concurrent_insert(secondary_url, world, order_no="SO-009")
            page2 = fetch_unified_query_page(
                session, principal, cursor, session_kind="direct",
                session_id=str(principal.id))
            assert page2["pageRowCount"] == 1
            assert page2["totalRowCount"] == 3
            all_nos = [row["orderNo"] for row in page1["rows"]] + \
                [row["orderNo"] for row in page2["rows"]]
            assert sorted(all_nos) == ["SO-001", "SO-002", "SO-003"], all_nos
            report["cases"]["pagination_stable"] = True
        finally:
            session.close()
        report["durations_ms"]["pagination"] = round(
            (time.perf_counter() - started) * 1000, 1)

        # ------------------------------------------------ repeatable_read
        stage = "repeatable_read"
        report["stage"] = stage
        started = time.perf_counter()
        _purge_snapshots(session_factory, world)
        from app.ontologies.agent_query import service as agent_query_service

        original_materialize = \
            agent_query_service.UnifiedQueryService._materialize
        rr_probe_fired: list[bool] = []

        def _rr_probe(self, final_sql):
            if not rr_probe_fired:
                rr_probe_fired.append(True)
                # 快照已由前面的读语句建立：此刻其他连接提交的新行必须不可见
                _concurrent_insert(secondary_url, world, order_no="SO-RR")
            return original_materialize(self, final_sql)

        agent_query_service.UnifiedQueryService._materialize = _rr_probe
        session = session_factory()
        try:
            principal = principal_of(session)
            snapshot_view = _run_query(session, principal,
                                       _query_payload(world.orders))
            nos = [row["orderNo"] for row in snapshot_view["rows"]]
            assert "SO-RR" not in nos, nos
            assert snapshot_view["totalRowCount"] == 4, \
                snapshot_view["totalRowCount"]  # SO-001..003 + SO-009
            report["cases"]["repeatable_read_snapshot"] = True
        finally:
            session.close()
            agent_query_service.UnifiedQueryService._materialize = \
                original_materialize
        session = session_factory()
        try:
            principal = principal_of(session)
            fresh_view = _run_query(session, principal,
                                    _query_payload(world.orders))
            assert fresh_view["totalRowCount"] == 5, \
                fresh_view["totalRowCount"]  # + SO-RR（并发提交确实落库）
            report["cases"]["concurrent_visible_after"] = True
        finally:
            session.close()
        report["durations_ms"]["repeatable_read"] = round(
            (time.perf_counter() - started) * 1000, 1)

        # -------------------------------------------------------- budgets
        stage = "budgets"
        report["stage"] = stage
        started = time.perf_counter()
        _purge_snapshots(session_factory, world)
        session = session_factory()
        try:
            principal = principal_of(session)
            with _StatementCounter(engine) as counter:
                _run_query(session, principal,
                           _query_payload(world.orders, pageSize=2))
            report["set_local_statements"]["primary"] = counter.set_local
            report["isolation_statements"] = counter.isolation
            expected_primary = 5 if report["primary_role_superuser"] else 4
            assert counter.set_local == expected_primary, \
                (counter.set_local, expected_primary)
            assert counter.isolation >= 1
        finally:
            session.close()

        if report["primary_role_superuser"]:
            # 真实验证普通角色三段式：未设 → fail closed；过宽 → fail closed；
            # ALTER ROLE 设帽 + 新连接 → 可用（SUSET 帽由角色级设置生效）。
            from app.config import settings as app_settings

            cap_mib = app_settings.agent_query_temp_file_limit_mib
            low_priv_role = f"oq_e2e_r{uuid.uuid4().hex[:8]}"
            low_priv_password = secrets.token_urlsafe(24)
            sensitive_values.add(low_priv_password)
            sensitive_values.add(low_priv_role)
            with engine.begin() as conn:
                conn.execute(text(
                    f'CREATE ROLE "{low_priv_role}" LOGIN '
                    f"PASSWORD '{low_priv_password}'"))
                conn.execute(text(
                    f'GRANT USAGE ON SCHEMA "{schema}" '
                    f'TO "{low_priv_role}"'))
                conn.execute(text(
                    f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES '
                    f'IN SCHEMA "{schema}" TO "{low_priv_role}"'))
                conn.execute(text(
                    f'GRANT USAGE, SELECT ON ALL SEQUENCES '
                    f'IN SCHEMA "{schema}" TO "{low_priv_role}"'))

            def _role_engine():
                return create_engine(_isolation_url(
                    database_url, schema, username=low_priv_role,
                    password=low_priv_password))

            def _expect_budget_unavailable(role_engine, case: str) -> None:
                from sqlalchemy.orm import sessionmaker as role_sm

                role_session = role_sm(bind=role_engine)()
                try:
                    low_principal = role_session.query(User).filter_by(
                        id=world.user_id).one()
                    try:
                        _run_query(role_session, low_principal,
                                   _query_payload(world.orders, pageSize=2))
                        raise AssertionError(
                            f"low-privilege query passed {case}")
                    except QueryError as exc:
                        assert exc.status == 503 and \
                            exc.code == "query_budget_unavailable", \
                            (exc.status, exc.code)
                        report["cases"][case] = True
                finally:
                    role_session.close()
                    role_engine.dispose()

            # 1) 未设置 temp_file_limit（默认 -1）→ 503 fail closed
            unset_engine = _role_engine()
            _expect_budget_unavailable(unset_engine,
                                       "low_privilege_unset_rejected")

            # 2) 角色级设置宽于配置帽 → 503 fail closed
            with engine.begin() as conn:
                conn.execute(text(
                    f'ALTER ROLE "{low_priv_role}" SET temp_file_limit = '
                    f"'{cap_mib * 32}MB'"))
            wide_engine = _role_engine()
            _expect_budget_unavailable(wide_engine,
                                       "low_privilege_wide_rejected")

            # 3) 角色级设置等于配置帽 + 新连接 → 查询可用，SET LOCAL=4
            with engine.begin() as conn:
                conn.execute(text(
                    f'ALTER ROLE "{low_priv_role}" SET temp_file_limit = '
                    f"'{cap_mib}MB'"))
            from sqlalchemy.orm import sessionmaker as role_sm

            ok_engine = _role_engine()
            role_session = role_sm(bind=ok_engine)()
            try:
                low_principal = role_session.query(User).filter_by(
                    id=world.user_id).one()
                with _StatementCounter(ok_engine) as low_counter:
                    low_result = _run_query(
                        role_session, low_principal,
                        _query_payload(world.orders, pageSize=2))
                assert low_result["totalRowCount"] == 5, \
                    low_result["totalRowCount"]
                assert low_counter.set_local == 4, low_counter.set_local
                assert low_counter.isolation >= 1
                report["set_local_statements"]["low_privilege"] = \
                    low_counter.set_local
                report["low_privilege_role_verified"] = True
                report["cases"]["low_privilege_query"] = True
            finally:
                role_session.close()
                ok_engine.dispose()
        else:
            # 主角色本身即普通角色：主跑（已断言 SET LOCAL=4）即普通角色验证
            report["set_local_statements"]["low_privilege"] = \
                report["set_local_statements"]["primary"]
            report["low_privilege_role_verified"] = True
            report["cases"]["low_privilege_query"] = True
        report["durations_ms"]["budgets"] = round(
            (time.perf_counter() - started) * 1000, 1)

        # -------------------------------------------------------- metrics
        stage = "metrics"
        report["stage"] = stage
        started = time.perf_counter()
        _purge_snapshots(session_factory, world)
        # 5 行基线 + 35 = 40 实例，再 +40 = 80 实例：固定形状 SQL 数不随行数增长
        scale_session = session_factory()
        try:
            _scale_orders(scale_session, world, 40, 75)
        finally:
            scale_session.close()
        session = session_factory()
        try:
            principal = principal_of(session)
            with _StatementCounter(engine) as counter_40:
                _run_query(session, principal,
                           _query_payload(world.orders, pageSize=5))
            report["sql_counts"]["instances_40"] = counter_40.count
        finally:
            session.close()
        scale_session = session_factory()
        try:
            _scale_orders(scale_session, world, 75, 115)
        finally:
            scale_session.close()
        session = session_factory()
        try:
            principal = principal_of(session)
            with _StatementCounter(engine) as counter_80:
                result_80 = _run_query(session, principal,
                                       _query_payload(world.orders,
                                                      pageSize=5))
            report["sql_counts"]["instances_80"] = counter_80.count
            assert result_80["totalRowCount"] == 80, result_80[
                "totalRowCount"]
            assert counter_40.count == counter_80.count, \
                (counter_40.count, counter_80.count)
            report["sql_shape_stable"] = True
        finally:
            session.close()

        from app.ontologies.agent_runtime.structure_service import build_structure

        session = session_factory()
        try:
            principal = principal_of(session)
            before = build_structure(
                session, principal, world.orders.ontology_id,
                expected_release_id=world.orders.release_id,
                object_type_ids=[ORDERS_OT])
            before_bytes = len(json.dumps(
                before, ensure_ascii=False, default=str).encode("utf-8"))
            _seed_noise_ontology(session, world)
            after = build_structure(
                session, principal, world.orders.ontology_id,
                expected_release_id=world.orders.release_id,
                object_type_ids=[ORDERS_OT])
            after_bytes = len(json.dumps(
                after, ensure_ascii=False, default=str).encode("utf-8"))
            assert before_bytes == after_bytes, (before_bytes, after_bytes)
            report["structure_bytes"] = {
                "before": before_bytes,
                "after": after_bytes,
                "isolated": True,
            }
        finally:
            session.close()
        report["durations_ms"]["metrics"] = round(
            (time.perf_counter() - started) * 1000, 1)

        stage = "complete"
        report["stage"] = stage
        report["result"] = "pass"
        _write_report(report_path, report, sensitive_values)
        print(f"ontology assistant PostgreSQL E2E: pass "
              f"(cases={len(REPORT_CASES)})")
    except Exception:
        import traceback

        traceback.print_exc()
        report["result"] = "fail"
        report["stage"] = stage
        _write_report(report_path, report, sensitive_values)
        raise SystemExit(
            f"ontology assistant PostgreSQL E2E failed at stage: {stage}"
        ) from None
    finally:
        if engine is not None:
            try:
                with engine.begin() as conn:
                    conn.execute(
                        text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
                if low_priv_role is not None:
                    with engine.begin() as conn:
                        conn.execute(text(
                            f'DROP ROLE IF EXISTS "{low_priv_role}"'))
                    report["low_privilege_role_dropped"] = True
            except Exception:  # noqa: BLE001 - cleanup best effort
                pass
            engine.dispose()
            # 清理事实（角色是否删除）落进最终报告；失败路径此前已写过一次。
            try:
                _write_report(report_path, report, sensitive_values)
            except Exception:  # noqa: BLE001 - 不掩盖主流程结果
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report", type=Path, default=DEFAULT_REPORT,
        help=f"write a strictly sanitized 0600 JSON report "
             f"(default: {DEFAULT_REPORT})")
    args = parser.parse_args()
    _run(args.report.resolve())


if __name__ == "__main__":
    main()
