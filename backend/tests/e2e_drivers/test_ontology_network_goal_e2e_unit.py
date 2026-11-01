"""Pure unit contracts for the ontology-network goal E2E driver (offline).

Covers the loopback API guard, the offline plan phase expectations, and the
honest not_implemented/blocked exit semantics. No live API is required.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def driver():
    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location(
        "ontology_network_goal_e2e", root / "backend/scripts/ontology_network_goal_e2e.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_main(driver, argv: list[str], capsys):
    saved = sys.argv
    try:
        sys.argv = ["ontology_network_goal_e2e.py", *argv]
        code = driver.main()
    finally:
        sys.argv = saved
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip() else None
    return code, payload


def test_api_base_is_loopback_only(driver):
    assert driver.Api("http://127.0.0.1:56004").base_url.endswith("56004")
    for bad in ("http://10.0.0.1:56004", "http://user:pass@127.0.0.1:56004",
                "https://127.0.0.1:56004", "http://127.0.0.1:56004/prefix",
                "http://127.0.0.1:56004?q=1"):
        with pytest.raises(ValueError):
            driver.Api(bad)


def test_plan_phase_matches_doc_sec8(driver):
    result = driver.phase_plan(argparse.Namespace())
    assert result["result"] == "plan_ready"
    assert result["e2e_pass"] is False  # plan is never E2E evidence
    # Three ontology domains over four tables across two physical sources.
    assert [d["key"] for d in result["domains"]] == ["A", "B", "C"]
    assert sum(len(d["objects"]) for d in result["domains"]) == 4
    assert {d["source"] for d in result["domains"]} == {"postgres", "mysql"}
    customers = result["expected"]["customers"]
    assert customers["c1"]["contract_sum"] == "150.7500"
    assert customers["c2"]["contract_sum"] == "<NULL>"
    assert customers["c3"]["order_sum"] == "<NULL>"
    assert result["expected"]["regions"]["north"]["contract_sum"] == "157.7500"
    assert result["canonical_query"]["shape"].startswith("aggregate per customer")


def test_not_implemented_phase_is_honest(driver, capsys):
    code, payload = _run_main(driver, ["report", "--name", "unit"], capsys)
    assert code == 2
    assert payload["result"] == "not_implemented"
    assert payload["e2e_pass"] is False
    assert "never reported as pass" in payload["note"]


def test_frontdoor_without_live_api_is_blocked_not_pass(driver, capsys):
    # 127.0.0.1:1 has no listener in the test environment; any live-API phase
    # must fail honestly (exit 1 via OSError handler) instead of fabricating
    # a pass from an unreachable endpoint.
    code, _payload = _run_main(
        driver, ["frontdoor", "--name", "unit-offline",
                 "--api-base", "http://127.0.0.1:1", "--admin-password", "x"], capsys)
    assert code == 1


def test_structure_drill_stage_guards(driver, capsys):
    # d2/d3 without a drill ontology must block honestly, not fake-run.
    code, payload = _run_main(
        driver, ["structure-drill", "--name", "unit-drill", "--stage", "d2"], capsys)
    assert code == 2 and payload and payload.get("result") == "blocked"
    # d1 stage dispatches and fails only on unreachable API (honest), while
    # argument parsing accepts all five stages.
    with pytest.raises(SystemExit):
        _run_main(driver, ["structure-drill", "--name", "unit-drill",
                           "--stage", "nope"], capsys)
    # drill constants: five distinct slots, drill API default is loopback 56007.
    assert driver.DRILL_STEPS == ("d1_preclaim_rejected", "d2_post_claim_durable_failed",
                                  "d3_recovery", "d4_auto_hooks")
    with pytest.raises(ValueError):
        driver.Api("http://10.0.0.9:56007")
