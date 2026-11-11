"""Pure unit contracts for the integrated CDC canary stack driver.

Offline only: constants, isolation from other fixture stacks, compose text
safety, wrapper parameter threading, fixture determinism, the scenario
registry and the migration-probe comparator helpers. Nothing here starts
Docker, touches a database, reads dotenv, or simulates any E2E success; real
service runs happen in coordinated slots via scripts/e2e/integrated_cdc.py.
"""
from __future__ import annotations

import importlib.util
import inspect
import re
from pathlib import Path

import pytest
import sqlalchemy as sa


def _load(name: str, relative: str):
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location(name, root / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cdc():
    return _load("integrated_cdc_stack_driver", "scripts/e2e/integrated_cdc.py")


@pytest.fixture(scope="module")
def compose_text(cdc):
    return cdc.COMPOSE.read_text(encoding="utf-8")


def _service_blocks(text: str) -> dict[str, str]:
    """Split the compose ``services:`` section into per-service text blocks."""
    body = text.split("\nservices:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    blocks: dict[str, str] = {}
    current = None
    for line in body.splitlines():
        match = re.fullmatch(r"  ([a-z0-9-]+):", line)
        if match:
            current = match.group(1)
            blocks[current] = ""
        elif current is not None:
            blocks[current] += line + "\n"
    return blocks


def test_project_and_port_isolation(cdc):
    base = _load("integrated_stack_for_cdc", "scripts/e2e/integrated_stack.py")
    fed = _load("federated_ports_for_cdc", "scripts/e2e/federated_fixture.py")
    goal = _load("goal_ports_for_cdc", "scripts/e2e/ontology_network_goal.py")
    assert cdc.PROJECT == "cdc-canary-20261110"
    assert cdc.PROJECT not in (base.PROJECT, fed.FED_PROJECT, goal.GOAL_PROJECT)
    own = {port for ports in cdc.PORTS.values() for port in ports}
    assert own == {45532, 48180}
    others = set()
    for mapping in (base.PORTS, fed.FED_PORTS, goal.GOAL_PORTS,
                    goal.RESERVED_HOST_PORTS):
        others |= {port for ports in mapping.values() for port in ports}
    assert not own & others
    # Evidence and ledger live in this driver's own artifact directory.
    assert cdc.ART.name == "cdc-validation" and cdc.ART.parent.name == ".artifacts"
    assert cdc.STATE.parent == cdc.ART


def test_compose_is_loopback_only_never_pulls_and_bounded(cdc, compose_text):
    assert "name: cdc-canary-20261110" in compose_text
    assert "env_file:" not in compose_text      # directive absent
    # No host environment interpolation; "$${VAR}" is compose-escaped and
    # expands inside the container only (healthcheck fixture credentials).
    assert not re.search(r"(?<!\$)\$\{", compose_text)
    blocks = _service_blocks(compose_text)
    assert set(blocks) == set(cdc.IMAGES) == set(cdc.PORTS)
    for service, block in blocks.items():
        assert "pull_policy: never" in block, service
        assert "mem_limit:" in block and "cpus:" in block, service
        assert f"image: {cdc.IMAGES[service]}" in block, service
        published = re.findall(r'"([0-9.:]+):\d+"', block)
        assert all(entry.startswith("127.0.0.1:") for entry in published), service
        assert sorted(int(entry.rsplit(":", 1)[1]) for entry in published) == \
            sorted(cdc.PORTS[service]), service
    assert "0.0.0.0:" not in compose_text.replace("DBX_BIND_ADDR: 0.0.0.0", "")


def test_only_metadata_db_and_backend_publish_ports(cdc, compose_text):
    blocks = _service_blocks(compose_text)
    publishing = {service for service, block in blocks.items() if "ports:" in block}
    assert publishing == {"db", "backend"}
    # The Debezium HTTP receiver is loopback inside the executor container.
    assert "ports:" not in blocks["cdc-executor"]
    assert "profiles: [cdc]" in blocks["cdc-executor"]


def test_sources_are_real_cdc_capable_databases(compose_text):
    blocks = _service_blocks(compose_text)
    pg = blocks["source-pg"]
    assert "wal_level=logical" in pg and "max_replication_slots=10" in pg
    mysql = blocks["source-mysql"]
    for flag in ("--binlog-format=ROW", "--binlog-row-image=FULL",
                 "--gtid-mode=ON", "--enforce-gtid-consistency=ON", "--log-bin="):
        assert flag in mysql
    # No stub/mock/fake service may stand in for Debezium, sources or sinks.
    for absent in ("stub", "mock", "fake", "debezium"):
        assert f"  {absent}:" not in compose_text


def test_executor_service_matches_runtime_contract(cdc, compose_text):
    block = _service_blocks(compose_text)["cdc-executor"]
    assert "image: openontology-cdc-executor:canary" in block
    assert 'command: ["python", "-m", "app.data_channel.integrated.cdc_executor"]' in block
    for setting in ("CDC_STATE_ROOT: /var/lib/openontology-cdc",
                    "CDC_RUN_ROOT: /tmp/cdc",
                    "JAVA_HOME: /opt/java/openjdk",
                    "CDC_DEBEZIUM_HOME: /opt/debezium",
                    "CDC_JAVA_BINARY: /opt/java/openjdk/bin/java",
                    'CDC_MAX_SOURCES: "2"',
                    'CDC_QUARKUS_PORT_BASE: "21024"'):
        assert setting in block
    # Offsets persist on a named volume; the run root is ephemeral tmpfs that
    # the non-root executor user can write (bounded size).
    assert "canary_cdc_offsets:/var/lib/openontology-cdc" in block
    tmpfs = re.search(r"tmpfs: \[(.+)\]", block)
    assert tmpfs and "/tmp/cdc:" in tmpfs.group(1) and "size=64m" in tmpfs.group(1)
    # The backend and the executor run the bind-mounted worktree sources.
    assert "./backend/app:/app/app:ro" in block
    assert "./backend/app:/app/app:ro" in _service_blocks(compose_text)["backend"]
    assert cdc.EXECUTOR_BUILD_COMMAND == [
        "docker", "build", "-f", "backend/Dockerfile.cdc",
        "--build-arg", "PYTHON_BASE_IMAGE=oo-ontology-mcp-backend:acceptance",
        "--build-arg", "CDC_PREBUILT_BACKEND=1",
        "-t", "openontology-cdc-executor:canary", "backend"]
    assert cdc.DEBEZIUM_SERVER_IMAGE == "quay.io/debezium/server:3.7.1.Final"


def test_phase_service_sets_are_bounded(cdc, compose_text):
    blocks = _service_blocks(compose_text)
    # The migration probe must never reserve the whole stack.
    assert cdc.PHASES["migration"] == ["db"]
    assert set(cdc.PHASES["acceptance"]) == set(blocks)
    assert cdc.phase_profiles("migration") == ()
    assert cdc.phase_profiles("acceptance") == ("cdc",)
    plan = cdc.resources()
    assert plan["project"] == cdc.PROJECT and plan["pull_policy"] == "never"
    assert plan["published_loopback_ports"] == {"db": [45532], "backend": [48180]}
    assert plan["total_memory_limit_bytes"] < plan["docker"]["memory_bytes"]
    with pytest.raises(SystemExit):
        cdc.phase_services("production")


def test_wrappers_thread_own_project_compose_and_ledger(cdc):
    source = inspect.getsource(cdc.cmd_up)
    assert "project=PROJECT" in source and "compose_file=COMPOSE" in source
    assert "state_path=STATE" in source and "directory=ART" in source
    source = inspect.getsource(cdc.cmd_down)
    assert "project=PROJECT" in source and "compose_file=COMPOSE" in source
    assert "state_path=STATE" in source
    # Source SQL and probes resolve containers only through the owned label.
    assert "base.require_owned(service, PROJECT)" in inspect.getsource(cdc._source_sql)
    assert 'base.require_owned("db", PROJECT)' in inspect.getsource(cdc.cmd_migrate_probe)


def test_pending_services_skips_own_running_containers(cdc, monkeypatch):
    monkeypatch.setattr(cdc.base, "owned_containers", lambda project: [
        {"service": "db", "running": True},
        {"service": "backend", "running": False},
    ])
    pending = cdc.pending_services("acceptance")
    assert "db" not in pending and "backend" in pending
    assert cdc.pending_services("migration") == []


def test_preflight_blocks_without_executor_dockerfile(cdc, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cdc.base, "owned_containers", lambda project: [])
    monkeypatch.setattr(cdc.base, "preflight", lambda services, **kwargs: {
        "result": "pass", "services": services})
    monkeypatch.setattr(cdc, "EXECUTOR_DOCKERFILE", tmp_path / "backend" / "Dockerfile.cdc")
    monkeypatch.setattr(cdc, "ROOT", tmp_path)
    report = cdc.cmd_preflight("acceptance")
    capsys.readouterr()
    assert report["result"] == "blocked"
    assert "Dockerfile.cdc" in report["blocked_reason"]
    # The migration phase does not depend on the executor image at all.
    assert cdc.cmd_preflight("migration")["result"] == "pass"
    capsys.readouterr()


def test_inventory_fixture_is_deterministic_and_synthetic(cdc):
    rows = cdc.INVENTORY_ROWS
    assert len(rows) == 8 and len({row[0] for row in rows}) == 8
    assert [row[0] for row in rows] == [f"item-{n:02d}" for n in range(1, 9)]
    assert sum(row[1] for row in rows) == 800
    assert {row[2] for row in rows} == {"ready", "pending"}
    # One fixed business timestamp: identical timestamps can never be an identity.
    assert {row[3] for row in rows} == {"2026-01-01 00:00:00"}
    assert cdc.INVENTORY_COLUMNS == ("source_id", "balance", "source_status", "observed_at")


def test_seed_sources_asserts_real_source_prerequisites(cdc):
    source = inspect.getsource(cdc.cmd_seed_sources)
    assert "REPLICA IDENTITY FULL" in source
    assert 'ident != "f"' in source
    assert 'wal.lower() != "logical"' in source
    assert "@@binlog_format" in source and "@@binlog_row_image" in source
    assert "expected_aggregate" in source


def test_mysql_init_fixture_grants_are_replication_scoped():
    root = Path(__file__).resolve().parents[3]
    text = (root / "scripts/e2e/cdc_mysql_init.sql").read_text(encoding="utf-8")
    statements = [line for line in text.splitlines()
                  if line.strip() and not line.startswith("--")]
    assert statements[0].startswith(
        "GRANT REPLICATION SLAVE, REPLICATION CLIENT, SHOW DATABASES, RELOAD ON *.*")
    assert "GRANT SELECT ON cdc_source.*" in statements[1]
    joined = " ".join(statements).upper()
    for forbidden in ("ALL PRIVILEGES", "SUPER", "INSERT", "UPDATE", "DELETE",
                      "DROP", "GRANT OPTION"):
        assert forbidden not in joined


def test_scenario_registry_covers_required_matrix(cdc):
    ids = [entry[0] for entry in cdc.SCENARIOS]
    assert len(ids) == len(set(ids))
    for required in (
            "migrate_fresh_head", "migrate_prior_0143_to_head",
            "migrate_0146_down_up", "migrate_no_model_drift",
            "separate_commits_same_timestamp",
            "multi_field_one_row_one_notification", "rollback_no_event",
            "one_transaction_intermediate_honest",
            "duplicate_delivery_exactly_one_notification",
            "restart_resume_offsets", "backend_restart_resume",
            "source_gap_explicit_unhealthy", "ddl_drift_explicit_unhealthy",
            "no_object_instance_materialized",
            "latency_p95_commit_to_notification", "pg_commit_order_no_loss",
            "cross_source_late_commit_no_skip",
            "last_mapping_removal_stops_and_cleans",
            "decommission_active_structured_409",
            "republish_after_dormant_new_generation",
            "credential_rotation_resumes_offset",
            "repoint_rejected_until_cleanup", "mysql_same_contract"):
        assert required in ids, required
    # The registry never pre-claims a result.
    report = inspect.getsource(cdc.cmd_scenarios)
    assert '"status": "not_run"' in report


def test_frozen_runtime_contract_vocabulary(cdc):
    assert cdc.INGEST_STATUSES == ("captured", "duplicate", "stale", "heartbeat", "control")
    assert cdc.SOURCE_HEALTHY_STATUSES == ("active", "starting")
    assert "running" not in cdc.SOURCE_HEALTHY_STATUSES + cdc.SOURCE_UNHEALTHY_EXPLICIT
    assert cdc.CHANGE_KINDS == ("created", "updated", "deleted")
    assert cdc.SNAPSHOT_MODE == "no_data"
    assert "never timestamp or content" in cdc.REPLAY_IDENTITY_RULE
    assert "txn/gtid never" in cdc.EVENT_GROUP_RULE
    assert cdc.MIGRATION_PRIOR_REVISION == "0143_operations_menu_keys"
    assert cdc.CDC_REVISION == "0146_integrated_cdc"


def test_migration_probe_stays_isolated_and_honest(cdc):
    for function in (cdc.cmd_migrate_probe, cdc.cmd_platform_migrate):
        source = inspect.getsource(function)
        # The probes import alembic + targeted models only, never the API app.
        assert not re.search(r"^\s*(import app\.main|from app\.main)", source, re.M)
        assert "uvicorn" not in source
        assert '"ENVIRONMENT": "test"' in source
    probe = inspect.getsource(cdc.cmd_migrate_probe)
    # Probe databases are uniquely named, marker-verified and always dropped.
    assert 'r"cdc_probe_[0-9a-f]{12}"' in probe
    assert "cdc-canary:" in probe and "WITH (FORCE)" in probe
    assert "refuse cleanup" in probe
    assert '"full_e2e_pass": False' in probe
    assert "passed_migration_probe_not_full_e2e" in probe
    # Drift is scoped to CDC-owned schema, never the whole metadata.
    assert cdc.CDC_OWNED_EVENT_LOG_COLUMNS == (
        "source_event_id", "event_group_id", "external_asset_id",
        "external_mapping_id", "event_snapshot")
    assert "Base.metadata" not in inspect.getsource(cdc.migration_drift)


def test_reflected_types_compile_under_the_pg_dialect(cdc):
    # Regression: generic str() of a reflected type drops timezone/length and
    # produced false drift against models compiled for PostgreSQL.
    model = sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True)
    reflected = {"name": "heartbeat_at", "type": sa.TIMESTAMP(timezone=True),
                 "nullable": True, "default": None}
    assert cdc._column_signature(model) == cdc._db_column_signature(reflected)
    assert cdc._db_column_signature(reflected)[1] == "TIMESTAMP WITH TIME ZONE"
    naive = dict(reflected, type=sa.TIMESTAMP(timezone=False))
    assert cdc._column_signature(model) != cdc._db_column_signature(naive)
    sized = sa.Column("status", sa.String(20), nullable=False, server_default="stopped")
    assert cdc._column_signature(sized) == cdc._db_column_signature({
        "name": "status", "type": sa.VARCHAR(20), "nullable": False,
        "default": "'stopped'::character varying"})


def test_default_normalization_is_exact(cdc):
    assert cdc._normalize_default(None) == ""
    assert cdc._normalize_default("0") == "0"
    assert cdc._normalize_default("'0'") == "0"
    assert cdc._normalize_default("'stopped'::character varying") == "stopped"
    assert cdc._normalize_default("'starting'::character varying") == "starting"
    # Different defaults must stay different after normalization.
    assert cdc._normalize_default("'stopped'::character varying") != \
        cdc._normalize_default("'starting'::character varying")
