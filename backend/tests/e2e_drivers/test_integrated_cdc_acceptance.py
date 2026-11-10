"""Pure unit contracts for the integrated CDC acceptance runner.

These tests cover deterministic fixture construction, ownership/safety gates
and result classification only. They do not start Docker, touch any database
or source, read dotenv, or simulate a CDC/E2E success: the real receiver ->
ingest -> Sentinel -> notification evidence is produced exclusively by
running ``scripts/e2e/integrated_cdc_acceptance.py`` against the isolated
canary stack.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
REQUIRED_SCENARIOS = {
    # every assertion the acceptance contract names must be executable
    "no_history",
    "separate_commits_same_timestamp",
    "one_transaction_intermediate_honest",
    "rollback_no_event",
    "multi_field_one_row_one_notification",
    "restart_resume_offsets",
    "source_gap_explicit_unhealthy",
    "ddl_drift_explicit_unhealthy",
    "zero_object_instance_no_lake_mirror",
    "notification_provenance",
    "latency_p95_commit_to_notification",
    "pg_commit_order_no_loss",
    "last_mapping_removal_stops_and_cleans",
    "mysql_same_contract",
}


@pytest.fixture(scope="module")
def acc():
    added = [name for name in ("integrated_stack", "integrated_cdc")
             if name not in sys.modules]
    spec = importlib.util.spec_from_file_location(
        "integrated_cdc_acceptance_unit",
        ROOT / "scripts/e2e/integrated_cdc_acceptance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    for name in added:  # leave no script module behind for other tests
        sys.modules.pop(name, None)


def _descriptor() -> dict:
    return {
        "assetId": "asset-unit", "datasetId": "dataset-unit",
        "connectionId": "connection-unit", "schemaName": "public",
        "tableName": "inventory",
        "columns": [
            {"name": "source_id", "type": "string", "nullable": False},
            {"name": "balance", "type": "integer", "nullable": True},
            {"name": "source_status", "type": "string", "nullable": True},
            {"name": "observed_at", "type": "timestamp", "nullable": False},
        ],
        "primaryKey": ["source_id"], "schemaFingerprint": "f" * 64,
    }


def test_scenario_registry_is_complete_and_ordered(acc):
    assert set(acc.SCENARIO_ORDER) == set(acc.SCENARIOS)
    assert len(acc.SCENARIO_ORDER) == len(set(acc.SCENARIO_ORDER))
    assert REQUIRED_SCENARIOS <= set(acc.SCENARIOS)
    assert all(callable(function) for function in acc.SCENARIOS.values())
    order = list(acc.SCENARIO_ORDER)
    # History must be asserted before any change; destructive lifecycle
    # scenarios run last so they cannot poison the foundational evidence.
    assert order[0] == "no_history"
    for foundational in ("separate_commits_same_timestamp",
                         "multi_field_one_row_one_notification",
                         "rollback_no_event"):
        for destructive in ("source_gap_explicit_unhealthy",
                            "ddl_drift_explicit_unhealthy",
                            "last_mapping_removal_stops_and_cleans"):
            assert order.index(foundational) < order.index(destructive)
    # Provenance inspects every notice persisted so far, so every scenario
    # that produces a notification must run before it: item-01/02/04 on PG
    # and item-07 on MySQL (which also anchors the second source's stream).
    for notifying in ("separate_commits_same_timestamp",
                      "multi_field_one_row_one_notification",
                      "one_transaction_intermediate_honest",
                      "mysql_same_contract"):
        assert order.index(notifying) < order.index("notification_provenance")
    assert (order.index("repoint_and_decommission_structured_409")
            < order.index("last_mapping_removal_stops_and_cleans")
            < order.index("republish_after_dormant_new_generation"))


def test_runner_targets_only_the_isolated_canary_stack(acc):
    assert acc.PROJECT == "cdc-canary-20261110"
    assert acc.API == "http://127.0.0.1:48180"
    assert "@127.0.0.1:45532/cdc_platform" in acc.PLATFORM_DSN
    assert acc.ART == ROOT / ".artifacts" / "cdc-validation" / "acceptance"
    compose = acc.driver.COMPOSE.read_text(encoding="utf-8")
    for source, url in acc.SOURCE_URLS.items():
        # Sources are reached by compose-internal service name with the
        # public deterministic fixture credentials declared in the compose.
        assert f"@source-{source}:" in url
        assert "cdc_e2e:cdc_e2e_fixture@" in url
    assert "POSTGRES_USER: cdc_e2e" in compose
    assert "POSTGRES_PASSWORD: cdc_e2e_fixture" in compose
    assert "MYSQL_PASSWORD: cdc_e2e_fixture" in compose
    # The split host/port form silently defaults SQLConnector to MySQL.
    assert acc.SOURCE_URLS["pg"].startswith("postgresql+psycopg2://")
    assert acc.SOURCE_URLS["mysql"].startswith("mysql+pymysql://")
    assert acc.SOURCE_KINDS == {"pg": "postgres", "mysql": "mysql"}
    # Credential-only rotation must keep the physical identity (same host/
    # port/database), while a repoint to another host must change it.
    from app.data_channel.integrated.cdc_control import (
        source_identity_from_config,
    )

    rotated_url = acc.SOURCE_URLS["pg"].replace(
        f":{acc.ORIGINAL_PG_PASSWORD}@", f":{acc.ROTATED_PG_PASSWORD}@")
    assert acc.ROTATED_PG_PASSWORD != acc.ORIGINAL_PG_PASSWORD
    assert acc.ROTATED_PG_PASSWORD in rotated_url
    assert (source_identity_from_config("postgres", {"connection_string": acc.SOURCE_URLS["pg"]})
            == source_identity_from_config("postgres", {"connection_string": rotated_url}))
    repoint_url = acc.SOURCE_URLS["pg"].replace(
        "@source-pg:5432/", "@source-mysql:3306/")
    assert (source_identity_from_config("postgres", {"connection_string": acc.SOURCE_URLS["pg"]})
            != source_identity_from_config("postgres", {"connection_string": repoint_url}))


def test_admin_fixture_comes_from_compose_and_fails_closed(acc, monkeypatch, tmp_path):
    username, password = acc.fixture_admin()
    compose = acc.driver.COMPOSE.read_text(encoding="utf-8")
    assert f"FIRST_ADMIN_USER: {username}" in compose
    assert f"FIRST_ADMIN_PASSWORD: {password}" in compose
    assert username and password and username != "admin"

    stripped = tmp_path / "compose.yml"
    stripped.write_text(
        "name: x\nservices:\n  backend:\n    image: y\n    environment:\n"
        "      ENVIRONMENT: test\n", encoding="utf-8")
    monkeypatch.setattr(acc.driver, "COMPOSE", stripped)
    with pytest.raises(RuntimeError, match="refusing to guess"):
        acc.fixture_admin()


def test_fixture_ids_are_namespaced_per_ontology(acc):
    pg, mysql = (acc.fixture_ids(acc.PG_ONTOLOGY),
                 acc.fixture_ids(acc.MYSQL_ONTOLOGY))
    assert set(pg) == set(mysql)
    assert not set(pg.values()) & set(mysql.values())
    for ids, ontology in ((pg, acc.PG_ONTOLOGY), (mysql, acc.MYSQL_ONTOLOGY)):
        assert all(value.startswith(ontology + "-") for value in ids.values())
    assert acc.compute_instance_id(
        acc.PG_ONTOLOGY, pg["object_type"], "asset", "connection", 0,
        {"source_id": "item-01"}) != acc.compute_instance_id(
        acc.MYSQL_ONTOLOGY, mysql["object_type"], "asset", "connection", 0,
        {"source_id": "item-01"})
    # A generation boundary opens a new row-identity namespace.
    assert acc.compute_instance_id(
        acc.PG_ONTOLOGY, pg["object_type"], "asset", "connection", 0,
        {"source_id": "item-01"}) != acc.compute_instance_id(
        acc.PG_ONTOLOGY, pg["object_type"], "asset", "connection", 1,
        {"source_id": "item-01"})


def test_seeded_snapshot_yields_one_production_subscription(acc):
    """The ORM seed must be exactly what the production port subscribes to."""
    from app.ontologies.sentinels.external_events import (
        release_remote_subscriptions,
    )

    for ontology in (acc.PG_ONTOLOGY, acc.MYSQL_ONTOLOGY):
        ids = acc.fixture_ids(ontology)
        snapshot, digest = acc._release_snapshot(
            ontology, _descriptor(), "admin-unit")
        assert len(digest) == 64
        subscriptions = release_remote_subscriptions(
            ontology, ids["release_v1"], snapshot)
        assert len(subscriptions) == 1
        subscription = subscriptions[0]
        assert subscription["mapping_id"] == ids["mapping"]
        assert subscription["object_type_id"] == ids["object_type"]
        assert subscription["asset_id"] == "asset-unit"
        assert subscription["connection_id"] == "connection-unit"
        assert subscription["primary_key_columns"] == ["source_id"]
        # PHYSICAL column -> ontology property; the unmapped timestamp column
        # is never part of the authorized stream.
        assert subscription["field_map"] == {
            "source_id": "item_id", "balance": "quantity",
            "source_status": "state"}
        sentinel = snapshot["sentinels"][0]
        assert sentinel["id"] == ids["sentinel"]
        assert sentinel["triggerMode"] == "on_pattern"
        assert len(sentinel["pattern"]["stages"]) == 1
        assert "quantity == 0" in sentinel["pattern"]["stages"][0]["filter"]
        rule = snapshot["actions"][0]["rules"][0]
        assert rule["type"] == "notification"
        assert rule["config"]["channel"] == "internal"
        assert rule["config"]["recipient"] == "admin-unit"


def test_snapshot_is_deterministic(acc):
    first = acc._release_snapshot(acc.PG_ONTOLOGY, _descriptor(), "admin-unit")
    second = acc._release_snapshot(acc.PG_ONTOLOGY, _descriptor(), "admin-unit")
    assert first[1] == second[1]
    other = acc._release_snapshot(acc.MYSQL_ONTOLOGY, _descriptor(), "admin-unit")
    assert other[1] != first[1]


def test_table_names_resolve_from_the_models(acc):
    from app.ontologies.formal_modeling.models import ObjectInstance
    from app.ontologies.sentinels.models import Notification, SentinelEventLog

    assert acc.table("notifications") == Notification.__tablename__
    assert acc.table("event_log") == SentinelEventLog.__tablename__
    assert acc.table("object_instances") == ObjectInstance.__tablename__
    with pytest.raises(KeyError):
        acc.table("not-a-fixture-table")


def test_container_ops_are_refused_without_explicit_opt_in(acc, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("docker must not be reached without opt-in")

    monkeypatch.setattr(acc, "_ALLOW_CONTAINER_OPS", False)
    monkeypatch.setattr(acc, "docker", forbidden)
    monkeypatch.setattr(acc, "container_id", forbidden)
    monkeypatch.setattr(acc, "owned_container_any_state", forbidden)
    for operation in (acc.stop_own_container, acc.start_own_container):
        with pytest.raises(acc.ContainerOpsNotAllowed):
            operation("cdc-executor")


def test_start_resolves_a_stopped_owned_container(acc, monkeypatch):
    calls = []
    monkeypatch.setattr(acc, "_ALLOW_CONTAINER_OPS", True)
    monkeypatch.setattr(acc.base, "owned_containers", lambda project: [
        {"id": "stopped-id", "service": "cdc-executor", "running": False},
        {"id": "other-id", "service": "backend", "running": True},
    ])

    class _Done:
        returncode = 0
        stderr = ""

    def fake_docker(*args, **_kwargs):
        calls.append(args)
        return _Done()

    monkeypatch.setattr(acc, "docker", fake_docker)
    assert acc.start_own_container("cdc-executor") == {
        "service": "cdc-executor", "started": True}
    assert calls == [("start", "stopped-id")]
    monkeypatch.setattr(acc.base, "owned_containers", lambda project: [])
    with pytest.raises(RuntimeError, match="exactly one owned"):
        acc.start_own_container("cdc-executor")


def test_wait_until_reports_timeout_and_success(acc):
    ok, elapsed, last = acc.wait_until(lambda: None, 0.05, 0.01)
    assert ok is False and last is None and elapsed >= 0.05
    values = iter([None, None, "ready"])
    ok, _elapsed, last = acc.wait_until(lambda: next(values), 1.0, 0.0)
    assert ok is True and last == "ready"


def test_blocked_scenarios_never_count_as_passed(acc, monkeypatch, tmp_path):
    class _Context(acc.Context):
        def __init__(self):
            self.db = self.api = None
            self.state, self.results = {}, {}

    def blocked(_ctx):
        raise acc.ContainerOpsNotAllowed("needs --allow-container-ops")

    monkeypatch.setattr(acc, "ART", tmp_path)
    monkeypatch.setattr(acc, "sanitize_environment", lambda: None)
    monkeypatch.setattr(acc, "Context", _Context)
    monkeypatch.setattr(acc, "_bind_context", lambda ctx: None)
    monkeypatch.setattr(acc, "SCENARIOS", {
        "ok": lambda _ctx: {"status": "passed", "detail": "real"},
        "gated": blocked,
    })
    summary = acc.cmd_run(["ok", "gated"])
    assert summary["results"]["gated"]["status"] == "blocked"
    assert summary["overall"] == "incomplete"
    assert summary["counts"] == {"blocked": 1, "passed": 1}
    persisted = json.loads((tmp_path / "summary.json").read_text("utf-8"))
    assert persisted["overall"] == "incomplete"

    def boom(_ctx):
        raise RuntimeError("live assertion failed")

    monkeypatch.setattr(acc, "SCENARIOS", {
        "ok": lambda _ctx: {"status": "passed"}, "broken": boom})
    summary = acc.cmd_run(["ok", "broken"])
    assert summary["overall"] == "failed"
    assert "live assertion failed" in summary["results"]["broken"]["detail"]
    with pytest.raises(SystemExit):
        acc.cmd_run(["does-not-exist"])


def test_setup_failure_always_leaves_a_partial_artifact(acc, monkeypatch, tmp_path):
    def failing(*, skip_seed=False):
        acc._SETUP_REPORT.update({"started_at": "t0", "steps": [
            {"step": "platform_schema", "result": {"at_head": True}}]})
        raise RuntimeError("connection test failed")

    monkeypatch.setattr(acc, "ART", tmp_path)
    monkeypatch.setattr(acc, "_setup", failing)
    with pytest.raises(RuntimeError, match="connection test failed"):
        acc.cmd_setup()
    report = json.loads((tmp_path / "setup.json").read_text("utf-8"))
    assert report["status"] == "failed"
    assert "connection test failed" in report["error"]
    assert [step["step"] for step in report["steps"]] == ["platform_schema"]


def test_dotenv_presence_refuses_before_touching_the_environment(
        acc, monkeypatch, tmp_path):
    import os

    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / ".env").write_text("X=1\n", encoding="utf-8")
    monkeypatch.setattr(acc, "ROOT", tmp_path)
    before = dict(os.environ)
    with pytest.raises(RuntimeError, match="must not exist"):
        acc.sanitize_environment()
    assert dict(os.environ) == before


def test_platform_head_verification_is_read_only(acc):
    class _Db:
        def __init__(self, value):
            self.value, self.statements = value, []

        def scalar(self, statement, **_params):
            self.statements.append(statement)
            return self.value

    probe = acc.verify_platform_head(_Db("not-a-revision"))
    assert len(probe["heads"]) == 1 and probe["at_head"] is False
    current = _Db(probe["heads"][0])
    assert acc.verify_platform_head(current)["at_head"] is True
    assert all(statement.lstrip().upper().startswith("SELECT")
               for statement in current.statements)


def test_publication_and_slot_reads_target_real_catalogs(acc, monkeypatch):
    """``pg_publication`` (singular) is the real catalog; a plural typo once
    aborted the gap scenario's recovery on the live stack."""
    import re

    statements: list[str] = []

    def fake_source_sql(service, statement):
        assert service == "source-pg"
        statements.append(statement)
        return "cdc_0123456789abcdef\n\n"

    monkeypatch.setattr(acc, "source_sql", fake_source_sql)
    assert acc.pg_slot_names() == ["cdc_0123456789abcdef"]
    assert acc.pg_publication_names() == ["cdc_0123456789abcdef"]
    slot_sql, publication_sql = statements
    assert re.findall(r"\bpg_\w+", slot_sql) == ["pg_replication_slots"]
    assert re.findall(r"\bpg_\w+", publication_sql) == ["pg_publication"]
    for statement in statements:
        assert statement.lstrip().upper().startswith("SELECT")


def test_cmd_run_fails_fast_and_reports_what_did_not_run(
        acc, monkeypatch, tmp_path):
    """A failed scenario stops the run: later (destructive) scenarios must not
    operate on a fixture state the failure may have left unrecovered."""
    class _Context(acc.Context):
        def __init__(self):
            self.db = self.api = None
            self.state, self.results = {}, {}

    executed: list[str] = []

    def passing(_ctx):
        executed.append("passing")
        return {"status": "passed"}

    def failing(_ctx):
        executed.append("failing")
        raise RuntimeError("live assertion failed")

    def never(_ctx):
        executed.append("never")
        return {"status": "passed"}

    monkeypatch.setattr(acc, "ART", tmp_path)
    monkeypatch.setattr(acc, "sanitize_environment", lambda: None)
    monkeypatch.setattr(acc, "Context", _Context)
    monkeypatch.setattr(acc, "_bind_context", lambda ctx: None)
    monkeypatch.setattr(acc, "SCENARIOS", {
        "passing": passing, "failing": failing,
        "never-a": never, "never-b": never})
    summary = acc.cmd_run(["passing", "failing", "never-a", "never-b"])
    assert executed == ["passing", "failing"]
    assert summary["not_run"] == ["never-a", "never-b"]
    assert set(summary["results"]) == {"passing", "failing"}
    assert summary["overall"] == "failed"
    assert summary["counts"] == {"failed": 1, "passed": 1}
    persisted = json.loads((tmp_path / "summary.json").read_text("utf-8"))
    assert persisted["not_run"] == ["never-a", "never-b"]
    assert persisted["overall"] == "failed"
    # A blocked scenario is not a failure: the run continues past it.
    executed.clear()

    def blocked(_ctx):
        executed.append("blocked")
        raise acc.ContainerOpsNotAllowed("needs --allow-container-ops")

    monkeypatch.setattr(acc, "SCENARIOS", {"blocked": blocked, "after": never})
    summary = acc.cmd_run(["blocked", "after"])
    assert executed == ["blocked", "never"]
    assert "not_run" not in summary
    assert summary["overall"] == "incomplete"
