"""Pure unit contracts for the four-table federated fixture and its oracle.

These tests cover deterministic formulas, extend consistency, no-fanout
shipment semantics, evidence parsing and budget classification only. They do
not start Docker, touch source databases, read dotenv, or simulate any E2E
success; real-service runs happen in coordinated slots via
scripts/e2e/federated_fixture.py.
"""
from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def fed():
    root = Path(__file__).resolve().parents[4]
    spec = importlib.util.spec_from_file_location("federated_fixture_unit", root / "scripts/e2e/federated_fixture.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scales_and_dimensions(fed):
    assert fed.FED_SCALES == (1_000_000, 2_300_000, 10_000_000)
    assert fed.dimensions(2_300_000) == (230_000, 460_000)
    assert fed.dimensions(10_000_000) == (1_000_000, 2_000_000)
    assert fed.dimensions(500) == (50, 100)
    # Above the dims() floor of 10, block mappings land exactly on the last
    # dimension row; below it they stay inside bounds (sparse coverage).
    for orders in (100, 500, 1000, 1_000_000, 2_300_000, 10_000_000):
        customers, contracts = fed.dimensions(orders)
        assert fed.order_row(orders, customers, contracts)["customer_ref"] == customers
        assert fed.order_row(orders, customers, contracts)["contract_ref"] == contracts
        assert fed.contract_row(contracts, customers)["customer_id"] <= customers
    tiny_customers, tiny_contracts = fed.dimensions(10)
    assert tiny_customers == tiny_contracts == 10
    assert fed.order_row(10, tiny_customers, tiny_contracts)["customer_ref"] == 1
    assert fed.order_row(10, tiny_customers, tiny_contracts)["contract_ref"] == 2


def test_row_formula_invariants(fed):
    for c in (1, 10, 13, 100, 99991):
        row = fed.customer_row(c)
        assert row["external_ref"] > 2**53  # beyond JavaScript safe integers
        assert row["is_key"] == (c % 10 == 0)
        assert (row["contact_note"] is None) == (c % 13 == 0)
        assert -row["credit_limit"].as_tuple().exponent <= 2
    for k in (1, 7, 365, 123457):
        row = fed.contract_row(k, 10_000)
        assert row["customer_id"] == (k - 1) // 5 + 1
        assert row["valid_to"] > row["valid_from"]
        assert (row["terms_note"] is None) == (k % 7 == 0)
        assert -row["signed_amount"].as_tuple().exponent <= 2
    for o in (1, 11, 17, 81, 90, 9973):
        row = fed.order_row(o, 50, 100)
        assert row["customer_ref"] == (o - 1) // 10 + 1
        assert row["contract_ref"] == (o - 1) // 5 + 1
        assert (row["remark"] is None) == (o % 11 == 0)
        assert -row["net_amount"].as_tuple().exponent <= 4
        assert fed.WINDOW_START <= row["created_at"] <= fed.WINDOW_END
        pending = fed.order_status(o) == "PENDING"
        assert pending == (o % 17 == 0) == (fed.shipment_count(o) == 0)
    for m, o in ((1, 1), (241, 81), (48, 17 + 1)):
        row = fed.shipment_row(m, o)
        assert row["waybill_no"] == fed.order_row(o, 50, 100)["waybill_no"]
        assert -row["ship_cost"].as_tuple().exponent <= 4


def test_golden_small_scale_totals(fed):
    # Hand-computed golden values for N=10 keep the oracle independent of the
    # aggregate loop it is compared against.
    totals = fed.expected_totals(10)
    assert totals["customers"] == 10 and totals["contracts"] == 10
    assert totals["unshipped_orders"] == 0  # first multiple of 17 is 17
    assert totals["shipment_multiplicity"] == {1: 9, 2: 1, 3: 0}  # o=9 doubles
    assert totals["shipment_rows"] == 11
    assert totals["sum_gross_cents"] == sum((o % 1000000) * 100 + o % 31 for o in range(1, 11)) == 5555
    assert Decimal(totals["sum_net_amount"]) == Decimal("55.0055")
    assert totals["created_at_window"] == {"start": "2026-07-01", "end": "2026-09-28",
                                           "days": 90, "all_orders_inside": True}


def test_shipment_semantics_multi_active_and_unshipped(fed):
    orders = 1000
    customers, contracts = fed.dimensions(orders)
    per_order: dict[int, list[int]] = {}
    for o in range(1, orders + 1):
        ids = fed.shipment_ids(o)
        if ids:
            per_order[o] = ids
        assert (fed.order_status(o) == "PENDING") == (not ids)
    totals = fed.expected_totals(orders)
    assert totals["unshipped_orders"] == orders // 17
    assert totals["shipped_orders"] == orders - orders // 17
    assert totals["shipment_multiplicity"][2] > 0 and totals["shipment_multiplicity"][3] > 0
    # Duplicate waybills exist structurally: every row of an order shares the
    # order waybill, so naive waybill joins fan out (rows > distinct waybills).
    assert totals["shipment_rows"] > totals["distinct_waybills_shipments"]
    # Canonical semantics pre-group by order: exactly one rollup row per
    # shipped order, so amounts are counted once (no double counting).
    assert len(per_order) == totals["shipped_orders"] == totals["distinct_waybills_shipments"]
    assert sum(len(ids) for ids in per_order.values()) == totals["shipment_rows"]
    # Unshipped orders have no shipment rows at all (status is not a dodge).
    for o in (17, 34, 170):
        assert o not in per_order


def test_customer_view_primary_goal(fed):
    orders = 200
    view = {entry["customer_id"]: entry for entry in fed.expected_customer_view(orders, [1, 2, 3])}
    # Customer 1 owns exactly orders 1..10 and contracts 1..5 (block mappings).
    assert view[1]["orders_90d"] == 10
    assert view[1]["contract_count"] == 5
    assert view[1]["unshipped_orders"] == 0
    assert view[1]["sum_gross_cents"] == 5555
    # Customer 2 owns orders 11..20 which include o=17 (unshipped/PENDING).
    assert view[2]["orders_90d"] == 10
    assert view[2]["unshipped_orders"] == 1
    assert view[2]["contract_count"] == 5
    # Every amount lies in the fixed 90-day window by construction.
    totals = fed.expected_totals(orders)
    assert totals["min_created_at"] >= fed.WINDOW_START.isoformat()
    assert totals["max_created_at"] <= fed.WINDOW_END.isoformat()
    # Tier/status grouping is an extension, never a replacement.
    assert set(totals["tier_status_extension"])
    assert sum(cell["orders"] for cell in totals["tier_status_extension"].values()) == orders


def test_extend_determinism_row_local(fed):
    # All row formulas are row-local, so generating 1..N directly must equal
    # prefix(20) plus the rows the extend path would append (21..200).
    customers_full, contracts_full = fed.dimensions(200)
    full_orders = {o: fed.order_row(o, customers_full, contracts_full) for o in range(1, 201)}
    prefix_customers, prefix_contracts = fed.dimensions(20)
    assert {o: fed.order_row(o, prefix_customers, prefix_contracts) for o in range(1, 21)} == \
        {o: full_orders[o] for o in range(1, 21)}
    full_customers = {c: fed.customer_row(c) for c in range(1, customers_full + 1)}
    prefix_customers_rows = {c: fed.customer_row(c) for c in range(1, prefix_customers + 1)}
    for c, row in prefix_customers_rows.items():
        assert full_customers[c] == row
    full_shipments = {m: (m, fed.shipment_row(m, o)) for o in range(1, 201) for m in fed.shipment_ids(o)}
    prefix_ids = {m for o in range(1, 21) for m in fed.shipment_ids(o)}
    assert all(m in full_shipments for m in prefix_ids)
    # The extend prefix-count contract used by seed --extend.
    counts = {"customers": prefix_customers, "contracts": prefix_contracts, "orders": 20,
              "shipments": sum(fed.shipment_count(o) for o in range(1, 21))}
    assert counts == {"customers": fed.dimensions(20)[0], "contracts": fed.dimensions(20)[1],
                      "orders": 20, "shipments": 21}  # 19 shipped + o=9,18 doubled


def test_expression_sql_shape(fed):
    for source in ("postgres", "mysql"):
        customers = fed.customer_expressions(source)
        contracts = fed.contract_expressions(source)
        orders = fed.order_expressions(source)
        shipments = fed.shipment_expressions(source)
        assert len(customers) == len(fed.CUSTOMER_COLUMNS)
        assert len(contracts) == len(fed.CONTRACT_COLUMNS)
        assert len(orders) == len(fed.ORDER_COLUMNS)
        assert len(shipments) == len(fed.SHIPMENT_COLUMNS)
        assert f"{fed.JS_SAFE}+n" in customers[1]
        assert "NULL" in customers[6] and "NULL" in orders[8]
        assert "DECIMAL(18,2)" in customers[5] and "DECIMAL(18,2)" in contracts[5]
        assert "DECIMAL(18,4)" in orders[5] and "DECIMAL(12,4)" in shipments[4]
        assert "'PENDING'" in orders[6]
        if source == "postgres":
            assert " || " in customers[3] and "CAST(" in customers[3]
            assert "DATE '2026-06-30' + CAST((MOD(n,90)+1) AS integer)" in orders[7]
            assert contracts[1] == "(n-1)/5+1" and orders[1] == "(n-1)/10+1"
            assert "IS DISTINCT FROM" not in " ".join(orders)
        else:
            assert "CONCAT(" in customers[3]
            assert "DATE_ADD('2026-06-30', INTERVAL (MOD(n,90)+1) DAY)" in orders[7]
            assert contracts[1] == "(n-1) DIV 5+1" and orders[1] == "(n-1) DIV 10+1"


def test_dialect_null_safe_note_condition(fed):
    assert fed._note_condition("postgres", "remark", None) == "remark IS NULL"
    assert fed._note_condition("postgres", "remark", "RM1") == "remark IS NOT DISTINCT FROM 'RM1'"
    assert fed._note_condition("mysql", "remark", None) == "remark IS NULL"
    assert fed._note_condition("mysql", "remark", "RM1") == "(remark <=> 'RM1')"


def test_insert_where_concatenation(fed, monkeypatch):
    captured = []
    monkeypatch.setattr(fed, "sql2", lambda source, statement: captured.append((source, statement)))
    pg_seq = "generate_series(1,10) AS seq(n)"
    mysql_seq = "(SELECT 1+n AS n FROM t) seq WHERE n<=10"
    fed._insert("postgres", "fed_customers", ["a"], ["1"], pg_seq, "MOD(n,2)=0")
    fed._insert("mysql", "fed_shipments", ["a"], ["1"], mysql_seq, "MOD(n,2)=0")
    assert " WHERE MOD(n,2)=0" in captured[0][1]
    assert " AND MOD(n,2)=0" in captured[1][1]
    assert captured[1][1].count("WHERE") == 1


def test_parse_helpers(fed):
    assert fed.parse_size_bytes("738B") == 738
    assert fed.parse_size_bytes("1.2kB") == 1200
    assert fed.parse_size_bytes("512MiB") == 512 * 1024 * 1024
    assert fed.parse_size_bytes("1.5GiB") == 1610612736
    assert fed.parse_net_io("6.3kB / 927MB") == (6300, 927000000)
    used, limit = fed.parse_mem_usage("512.3MiB / 512MiB")
    assert limit == 512 * 1024 * 1024 and used == int(512.3 * 1024 * 1024)
    with pytest.raises(ValueError):
        fed.parse_size_bytes("512 Mi BB")


def test_budget_report_classification(fed, monkeypatch):
    def stats_for(services):
        containers, stats = {}, {}
        for service in services:
            name = f"oo-fedanalysis-20261026-{service}-1"
            containers[name] = {"service": service, "running": True, "oom_killed": False,
                                "health": "healthy", "memory_limit_bytes": 0}
            stats[name] = {"mem_used_bytes": 1024, "mem_limit_bytes": 2048, "cpu_percent": "0.1%",
                           "net_rx_bytes": 100, "net_tx_bytes": 200, "block_read_bytes": 0,
                           "block_write_bytes": 0}
        return containers, stats

    platform = ["db", "redis", "nats", "neo4j", "minio", "dbx"]
    containers, stats = stats_for(platform + ["source-pg", "mysql"])
    snapshot = {"containers": containers, "stats": stats}
    monkeypatch.setattr(fed.base, "run", lambda args, **kwargs: "\n".join(sorted(containers)) + "\n")
    report = fed.budget_report(snapshot, None)
    assert report["platform_budget_bytes"] == 3008 * 1024 * 1024
    assert report["source_budget_bytes"] == 896 * 1024 * 1024
    assert set(report["source_appliances"]) == {"source-pg", "mysql"}
    assert report["platform_budget_verifiable"] is True  # only own project resident
    reasons = " ".join(report["verdict"]["cannot_claim_pass_reasons"])
    assert "required baseline missing" in reasons
    assert "shared VM" not in reasons

    # A resident of another project makes the shared-VM budget unverifiable.
    residents = sorted(containers) + ["someoneelse-db-1"]
    monkeypatch.setattr(fed.base, "run", lambda args, **kwargs: "\n".join(residents) + "\n")
    report = fed.budget_report(snapshot, None)
    assert report["platform_budget_verifiable"] is False
    assert "shared VM" in " ".join(report["verdict"]["cannot_claim_pass_reasons"])

    containers_partial, stats_partial = stats_for(["db", "redis"])
    monkeypatch.setattr(fed.base, "run", lambda args, **kwargs: "\n".join(sorted(containers_partial)) + "\n")
    report = fed.budget_report({"containers": containers_partial, "stats": stats_partial}, None)
    assert "platform baseline incomplete" in " ".join(report["verdict"]["cannot_claim_pass_reasons"])

    # IO deltas are computed per container from the earlier snapshot.
    earlier = {"stats": {name: dict(stat, net_rx_bytes=40, net_tx_bytes=80)
                         for name, stat in stats.items()}}
    report = fed.budget_report(snapshot, earlier)
    assert all(delta["net_rx_bytes"] == 60 and delta["net_tx_bytes"] == 120
               for delta in report["container_io_deltas"].values())


def test_seed_and_phase_validation_offline(fed):
    with pytest.raises(ValueError):
        fed.seed(1003, 1000, False)  # not a multiple of 10
    with pytest.raises(ValueError):
        fed.seed(0, 1000, False)
    with pytest.raises(ValueError):
        fed.seed(100, 100001, False)
    with pytest.raises(ValueError):
        fed.evidence_begin("bad phase!")


def test_canary_ids_are_bounded_and_targeted(fed):
    ids = fed.canary_ids(1000)
    assert ids["orders"] == sorted({1, 17, 9, 81, 500, 1000})
    assert ids["shipment_orders"] == [1, 2, 17, 9, 81] or sorted(ids["shipment_orders"]) == [1, 2, 9, 17, 81]
    assert len(ids["customers"]) == 3 and len(ids["contracts"]) == 3


def test_project_and_port_isolation(fed):
    assert fed.FED_PROJECT == "oo-fedanalysis-20261026"
    assert fed.FED_PROJECT != "oo-integrated-20261024"
    flat = [port for ports in fed.FED_PORTS.values() for port in ports]
    legacy = [45432, 45433, 45434, 43306, 46379, 44222, 48222, 47474, 47687, 49000, 49001,
              48004, 45173, 45678, 49222]
    assert not set(flat) & set(legacy)
    assert len(flat) == len(set(flat))
    # dbx is platform budget, sources are appliances, both explicitly split.
    assert "dbx" in fed.PLATFORM_BUDGET_MIB
    assert set(fed.SOURCE_BUDGET_MIB) == {"source-pg", "mysql"}
    assert set(fed.REQUIRED_BASELINE_OUTSIDE_COMPOSE) == {"api", "executor", "browser", "n8n"}
