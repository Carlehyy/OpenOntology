"""Local (materialized) aggregate_objects ordering/Top-N contract.

Covers the two local executors behind one shared order_by normalization:
SQLite compatibility execution and the PostgreSQL SQL path (the PG branch is
driven on the test SQLite engine; its statements are portable and the budget
guard is isolated). Remote bounded stats are covered elsewhere.
"""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from app.models.ontology_formal import ObjectInstance, ObjectType
from app.ontologies.agent_runtime.boundary import ToolError, build_scope
from app.ontologies.agent_runtime.toolkit import ToolRunner
from tests.ontologies.test_agent_runtime import modeled_ontology  # noqa: F401

MATCHED_SCOPE = "filtered_rows_before_group_limit"


@pytest.fixture
def local_probe(db, modeled_ontology):
    """Probe type with groups a(2), b(1), NULL(1): 4 filtered rows, 3 groups."""
    oid = modeled_ontology["id"]
    db.add(ObjectType(
        id="ot-probe", ontology_id=oid, name="Probe", display_name="探针",
        primary_key="code",
        properties=[
            {"id": "pp1", "name": "code", "displayName": "编码",
             "type": "string", "required": True},
            {"id": "pp2", "name": "label", "displayName": "标签",
             "type": "string"},
        ]))
    for index, (code, label) in enumerate(
            [("p1", "a"), ("p2", "a"), ("p3", "b"), ("p4", None)], start=1):
        properties = {"code": code}
        if label is not None:
            properties["label"] = label
        db.add(ObjectInstance(
            id=f"inst-p{index}", ontology_id=oid, object_type_id="ot-probe",
            properties=properties, computed={}, source="manual"))
    db.commit()
    _, _, scope = build_scope(db, oid)
    return ToolRunner(db, scope)


def _groups(runner, **args):
    out = runner.run("aggregate_objects", {
        "object_type": "Probe", "metric": "count", "group_by": "label",
        **args,
    })
    return out


def test_local_sqlite_group_order_asc_desc_nulls_last_and_limit(local_probe):
    default = _groups(local_probe)
    # Unchanged Top-N default: value desc, then group asc, NULL last.
    assert [g["group"] for g in default["groups"]] == ["a", "b", ""]

    asc = _groups(local_probe, order_by={"output": "group", "direction": "asc"})
    assert [g["group"] for g in asc["groups"]] == ["a", "b", ""]
    assert asc["truncated"] is False and asc["totalGroups"] == 3
    assert asc["matched"] == 4 and asc["matchedScope"] == MATCHED_SCOPE

    desc = _groups(local_probe, order_by={"output": "group", "direction": "desc"})
    # NULL group sorts last in both directions, matching PG NULLS LAST and the
    # remote group display (NULL -> empty string).
    assert [g["group"] for g in desc["groups"]] == ["b", "a", ""]

    asc_limited = _groups(local_probe, limit=2,
                          order_by={"output": "group", "direction": "asc"})
    assert [g["group"] for g in asc_limited["groups"]] == ["a", "b"]
    assert asc_limited["returned"] == 2
    assert asc_limited["truncated"] is True and asc_limited["totalGroups"] == 3
    # matched is the independently known full filtered-row total, even though
    # the group list is truncated; matchedScope states that explicitly.
    assert asc_limited["matched"] == 4
    assert asc_limited["matchedScope"] == MATCHED_SCOPE

    desc_limited = _groups(local_probe, limit=2,
                           order_by={"output": "group", "direction": "desc"})
    assert [g["group"] for g in desc_limited["groups"]] == ["b", "a"]
    assert desc_limited["matched"] == 4


def test_local_postgres_branch_orders_before_limit_and_keeps_matched_scope(
        local_probe, monkeypatch):
    """Drive the PG SQL branch on SQLite; assert SQL ordering/limit contract."""
    real_db = local_probe.db

    class _PgDialectSession:
        def __init__(self, session):
            self._session = session

        def __getattr__(self, name):
            return getattr(self._session, name)

        def get_bind(self, *args, **kwargs):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    monkeypatch.setattr(local_probe, "db", _PgDialectSession(real_db))
    monkeypatch.setattr(local_probe, "_sql_read_budget", lambda: nullcontext())

    asc = _groups(local_probe, order_by={"output": "group", "direction": "asc"})
    assert [g["group"] for g in asc["groups"]] == ["a", "b", ""]
    assert asc["matched"] == 4 and asc["matchedScope"] == MATCHED_SCOPE
    assert asc["truncated"] is False and asc["totalGroups"] == 3

    asc_limited = _groups(local_probe, limit=2,
                          order_by={"output": "group", "direction": "asc"})
    assert [g["group"] for g in asc_limited["groups"]] == ["a", "b"]
    assert asc_limited["truncated"] is True
    assert asc_limited["totalGroups"] == 3
    assert asc_limited["matched"] == 4
    assert asc_limited["matchedScope"] == MATCHED_SCOPE

    desc_limited = _groups(local_probe, limit=2,
                           order_by={"output": "group", "direction": "desc"})
    assert [g["group"] for g in desc_limited["groups"]] == ["b", "a"]
    assert desc_limited["truncated"] is True


def test_local_aggregate_rejects_invalid_order_before_execution(
        local_probe, monkeypatch):
    def fail_scan(*_args, **_kwargs):
        raise AssertionError("aggregate source scan must not run")

    monkeypatch.setattr(local_probe, "_stream_instances", fail_scan)

    with pytest.raises(ToolError, match="aggregate_objects_invalid_order_by"):
        _groups(local_probe, order_by={"output": "count"})
    # Remote has no "matched" aggregate column for metric=count.
    with pytest.raises(ToolError, match="aggregate_objects_invalid_order_by"):
        _groups(local_probe, order_by={"output": "matched", "direction": "desc"})
    with pytest.raises(ToolError, match="aggregate_objects_invalid_order_by"):
        _groups(local_probe,
                order_by={"output": "group", "direction": "sideways"})
