"""Pure unit contracts for the ontology-network goal micro fixture.

Offline only: constants, isolation from other fixture stacks, compose text
safety, wrapper parameter threading, and the doc sec 8 oracle expectations.
Nothing here starts Docker, touches source databases, reads dotenv, or
simulates any E2E success; real-service runs happen in coordinated slots via
scripts/e2e/ontology_network_goal.py.
"""
from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

import pytest


def _load(name: str, relative: str):
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location(name, root / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def goal():
    return _load("ontology_network_goal_fixture", "scripts/e2e/ontology_network_goal.py")


@pytest.fixture(scope="module")
def base():
    return _load("integrated_stack_for_goal", "scripts/e2e/integrated_stack.py")


def test_project_and_port_isolation(goal, base):
    fed = _load("federated_ports_for_goal", "scripts/e2e/federated_fixture.py")
    assert goal.GOAL_PROJECT == "oo-ontology-goal-20261101"
    assert goal.GOAL_PROJECT not in (base.PROJECT, fed.FED_PROJECT)
    goal_ports = {port for ports in goal.GOAL_PORTS.values() for port in ports}
    integrated_ports = {port for ports in base.PORTS.values() for port in ports}
    federated_ports = {port for ports in fed.FED_PORTS.values() for port in ports}
    assert not goal_ports & integrated_ports
    assert not goal_ports & federated_ports
    reserved = {port for ports in goal.RESERVED_HOST_PORTS.values() for port in ports}
    assert not reserved & goal_ports & integrated_ports & federated_ports
    assert reserved == {56004, 55173}


def test_compose_is_loopback_only_and_never_pulls(goal):
    text = goal.GOAL_COMPOSE.read_text(encoding="utf-8")
    assert "name: oo-ontology-goal-20261101" in text
    assert "env_file:" not in text  # directive absent (a comment may name it)
    assert text.count("pull_policy: never") == text.count("image:")
    for ports in goal.GOAL_PORTS.values():
        for port in ports:
            assert f"127.0.0.1:{port}:" in text
    # Readiness is only claimed through real services: n8n/browser are genuine
    # pinned-image instances (never stubs); chromium/cdp stay undefined as
    # separate service keys.
    assert "  n8n:" in text and "  browser:" in text
    for absent in ("chromium", "cdp", "stub", "mock"):
        assert f"  {absent}:" not in text


def test_stack_down_keeps_default_compose_and_wrapper_threads_own(goal, base):
    signature = inspect.signature(base.stack_down)
    assert signature.parameters["compose_file"].default is None
    # project default stays None (owner falls back to base.PROJECT): old
    # callers keep their behavior, only the goal wrapper passes overrides.
    assert signature.parameters["project"].default is None
    # The goal wrapper must pass its own compose file and ledger to every
    # lifecycle call so cleanup can never touch other projects' files.
    source = inspect.getsource(goal.goal_down)
    assert "compose_file=GOAL_COMPOSE" in source and "state_path=GOAL_STATE" in source
    source = inspect.getsource(goal.goal_up)
    assert "compose_file=GOAL_COMPOSE" in source and "project=GOAL_PROJECT" in source


def test_fixture_shape_and_row_cap(goal):
    assert goal.ROW_TOTAL == 12
    assert goal.ROW_TOTAL <= goal.ROW_CAP <= 500
    assert len(goal.CUSTOMERS) == 3 and len(goal.CONTRACTS) == 3
    assert len(goal.ORDERS) == 3 and len(goal.SHIPMENTS) == 3
    # bigint precision target: refs beyond JavaScript safe integers only.
    assert all(ref > 2**53 for _, _, ref, _ in goal.CUSTOMERS)
    assert all(ref > 2**53 for row in goal.ORDERS for ref in (row[3],))


def test_doc_sec8_oracle_expectations(goal):
    view = goal.expected_view()["customers"]
    # Exact decimal strings from the goal doc; float-approximate values fail.
    assert view["c1"]["contract_sum"] == "150.7500"
    assert view["c1"]["order_sum"] == "40.0000" and view["c1"]["ship_qty"] == "7"
    assert view["c2"]["contract_sum"] == "<NULL>"
    assert view["c2"]["order_sum"] == "20.0000" and view["c2"]["ship_qty"] == "5"
    assert view["c3"]["contract_sum"] == "7.0000"
    assert view["c3"]["order_sum"] == "<NULL>" and view["c3"]["ship_qty"] == "<NULL>"
    # count(*) vs count(field): c1 has two orders but no non-NULL remark.
    assert view["c1"]["order_count"] == 2 and view["c1"]["order_remark_count"] == 0
    assert view["c2"]["order_remark_count"] == 1
    assert view["c3"]["order_count"] == 0


def test_region_rollup_null_honesty_and_no_fanout(goal):
    regions = goal.expected_view()["regions"]
    north = regions["north"]
    assert north["customers"] == ["c1", "c3"]
    assert north["contract_sum"] == "157.7500"   # 150.7500 + 7.0000, never doubled
    assert north["order_sum"] == "40.0000" and north["ship_qty"] == "7"
    # An all-NULL set stays NULL instead of silently becoming zero.
    assert regions["south"]["contract_sum"] == "<NULL>"
    # The fan-out trap is documented as an assertion, not as an expected value.
    assert "must NOT" in goal.expected_view()["fanout_note"]
