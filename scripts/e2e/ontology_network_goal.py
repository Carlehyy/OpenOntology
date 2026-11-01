#!/usr/bin/env python3
"""Ontology-network goal micro fixture: three ontologies over four tables.

Goal doc anchor: docs/development/ontology-network-goal.md sec 8. The fixture
is a fixed, hand-checkable dataset (<= 500 rows total, currently 12 rows):

    ontology A (PG source):      goal_customers + goal_contracts
    ontology B (MySQL source):   goal_orders
    ontology C (MySQL source):   goal_shipments

Canonical semantics: aggregate per customer inside each source first, then
cross-source join in the consumer; contract amounts must never be multiplied
by an order-side fan-out. NULLs are asserted honestly (count(*) vs
count(field), SUM over an empty group is NULL). Amounts compare as exact
decimal strings; bigint refs beyond 2**53 must round-trip digit-exact.

This module shares the safety envelope of integrated_stack.py through its
parameterized helpers (stdlib + local images only, no dotenv, no pulls,
loopback-only published ports, ownership-checked containers). It never adopts
or queries oo-integrated-20261024, oo-fedanalysis-20261026 or any other
project's resources; every operation is scoped to GOAL_PROJECT by container
label. API/Web (56004/55173) and any later n8n/CDP readiness live outside this
compose; base-service health is never readiness for them.
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

try:  # normal script execution: scripts/e2e is on sys.path
    import integrated_stack as base
except ImportError:  # importlib loading from tests without package context
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "integrated_stack", Path(__file__).resolve().with_name("integrated_stack.py"))
    base = importlib.util.module_from_spec(_spec)
    assert _spec.loader is not None
    _spec.loader.exec_module(base)

ROOT = base.ROOT
GOAL_PROJECT = "oo-ontology-goal-20261101"
GOAL_COMPOSE = ROOT / "docker-compose.ontology-goal-e2e.yml"
GOAL_ART = ROOT / ".artifacts" / "ontology-goal-e2e"
GOAL_STATE = GOAL_ART / "resources.json"
GOAL_PORTS = {
    "db": [56432], "source-pg": [56433], "dbx": [56434], "mysql": [56306],
    "redis": [56379], "nats": [56222, 56822],
    "neo4j": [56474, 56687], "minio": [56000, 56001],
    "n8n": [56678], "browser": [56223], "kernel-gateway": [56440],
}
# Host-process slots reserved for later phases; not owned by this compose.
RESERVED_HOST_PORTS = {"api": [56004], "web": [55173]}
GOAL_IMAGES = {
    "db": "postgres:16-alpine", "source-pg": "postgres:16-alpine",
    "mysql": "mysql:8.4", "redis": "redis:7-alpine", "nats": "nats:alpine",
    "neo4j": "neo4j:5.20-community", "minio": "minio/minio:latest",
    "dbx": "docker.cnb.cool/dbxio.com/dbx@sha256:b661f7e35deed06d9611dcda79c44ffcc8e4fd0f44308028ea77f7f18b36638c",
    "n8n": "docker.n8n.io/n8nio/n8n:2.30.5", "browser": "openontology-browser:local",
    "kernel-gateway": "openontology-python_kernel_gateway:latest",
}
ROW_CAP = 500
BIGINT_BASE = 9_007_199_254_740_993  # = 2**53 + 1: every big_ref is already
#                                    beyond the JavaScript safe-integer range.

# Fixed rows: goal doc sec 8 (region map, contract lines hung off customers,
# o100/o101/o102 amounts, o100 shipments 3+4, o101 shipment 5, o102 none).
CUSTOMERS = [  # (customer_id, region, big_ref, note)
    ("c1", "north", BIGINT_BASE + 3, "goal-c1"),
    ("c2", "south", BIGINT_BASE + 4, None),
    ("c3", "north", BIGINT_BASE + 5, "goal-c3"),
]
CONTRACTS = [  # (contract_id, customer_id, amount, contract_no)
    (1, "c1", Decimal("100.5000"), "CT-100"),
    (2, "c1", Decimal("50.2500"), "CT-101"),
    (3, "c3", Decimal("7.0000"), "CT-102"),
]
ORDERS = [  # (order_id, customer_id, amount, big_ref, remark)
    (100, "c1", Decimal("10.0000"), BIGINT_BASE + 13, None),
    (101, "c2", Decimal("20.0000"), BIGINT_BASE + 14, "r-101"),
    (102, "c1", Decimal("30.0000"), BIGINT_BASE + 15, None),
]
SHIPMENTS = [  # (shipment_id, order_id, qty, waybill_no)
    (1, 100, 3, "WB-100"),
    (2, 100, 4, "WB-100"),
    (3, 101, 5, "WB-101"),
]
ROW_TOTAL = len(CUSTOMERS) + len(CONTRACTS) + len(ORDERS) + len(SHIPMENTS)


# --------------------------------------------------------------------------
# Pure-Python expected view: the independent oracle (goal doc sec 8 values).
# Nothing here touches SQL; the engines only verify the same rows later.
# --------------------------------------------------------------------------

def _money(value: Decimal | None) -> str:
    return "<NULL>" if value is None else str(value.quantize(Decimal("0.0001")))


def _rollup_regions(view: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """NULL-honest region rollup: a region sums only what exists, and an
    all-NULL (or empty) set stays NULL instead of turning into zero."""
    regions: dict[str, dict[str, Any]] = {}
    for customer_id, row in view.items():
        bucket = regions.setdefault(row["region"], {
            "customers": [], "contract_count": 0, "order_count": 0})
        bucket["customers"].append(customer_id)
        bucket["contract_count"] += int(row["contract_count"])
        bucket["order_count"] += int(row["order_count"])
        if row["contract_sum"] != "<NULL>":
            bucket["contract_sum"] = bucket.get("contract_sum", Decimal("0.0000")) + Decimal(row["contract_sum"])
        if row["order_sum"] != "<NULL>":
            bucket["order_sum"] = bucket.get("order_sum", Decimal("0.0000")) + Decimal(row["order_sum"])
        if row["ship_qty"] != "<NULL>":
            bucket["ship_qty"] = bucket.get("ship_qty", 0) + int(row["ship_qty"])
    for bucket in regions.values():
        bucket["contract_sum"] = _money(bucket.get("contract_sum"))
        bucket["order_sum"] = _money(bucket.get("order_sum"))
        bucket["ship_qty"] = "<NULL>" if "ship_qty" not in bucket else str(bucket["ship_qty"])
    return regions


def expected_view() -> dict[str, Any]:
    """Aggregate-then-join expectations per customer and per region."""
    customers = {row[0]: row[1] for row in CUSTOMERS}
    contracts: dict[str, list[Decimal]] = {}
    for _, customer_id, amount, _ in CONTRACTS:
        contracts.setdefault(customer_id, []).append(amount)
    orders: dict[str, list[tuple[Decimal, bool]]] = {}  # amount, remark-not-null
    for _, customer_id, amount, _, remark in ORDERS:
        orders.setdefault(customer_id, []).append((amount, remark is not None))
    qty_per_order: dict[int, int] = {}
    for _, order_id, qty, _ in SHIPMENTS:
        qty_per_order[order_id] = qty_per_order.get(order_id, 0) + qty
    qty: dict[str, int] = {}
    for order_id, customer_id, *_ in ORDERS:
        if order_id in qty_per_order:
            qty[customer_id] = qty.get(customer_id, 0) + qty_per_order[order_id]

    view: dict[str, dict[str, Any]] = {}
    for customer_id, region in customers.items():
        lines = contracts.get(customer_id, [])
        amounts = orders.get(customer_id, [])
        view[customer_id] = {
            "region": region,
            "contract_sum": _money(sum(lines) if lines else None),
            "contract_count": len(lines),                 # count(*): 0 for c2
            "order_sum": _money(sum(a for a, _ in amounts) if amounts else None),
            "order_count": len(amounts),
            "order_remark_count": sum(1 for _, present in amounts if present),
            "ship_qty": "<NULL>" if customer_id not in qty else str(qty[customer_id]),
            "ship_order_count": sum(1 for o, c, *_ in ORDERS
                                    if c == customer_id and o in qty_per_order),
        }
    return {"customers": view, "regions": _rollup_regions(view),
            "fanout_note": "expected values assume aggregate-then-join; any "
                           "join-then-aggregate contract amount for c1 must NOT "
                           "match contract_sum and must fail acceptance"}


# --------------------------------------------------------------------------
# SQL plumbing scoped to the goal project's own containers.
# --------------------------------------------------------------------------

def sql(source: str, statement: str) -> str:
    return base.sql(source, statement, project=GOAL_PROJECT)


def _pg_text(parts: list[str]) -> str:
    return " || '|' || ".join(parts)


def _my_text(parts: list[str]) -> str:
    return f"CONCAT_WS('|',{','.join(parts)})"


def create_schema() -> dict[str, Any]:
    pg = [
        "CREATE TABLE IF NOT EXISTS goal_customers (customer_id VARCHAR(8) PRIMARY KEY,"
        " region VARCHAR(16) NOT NULL, big_ref BIGINT NOT NULL, note VARCHAR(64));",
        "CREATE TABLE IF NOT EXISTS goal_contracts (contract_id INT PRIMARY KEY,"
        " customer_id VARCHAR(8) NOT NULL, amount DECIMAL(18,4) NOT NULL,"
        " contract_no VARCHAR(16) NOT NULL);",
    ]
    my = [
        "CREATE TABLE IF NOT EXISTS goal_orders (order_id INT PRIMARY KEY,"
        " customer_id VARCHAR(8) NOT NULL, amount DECIMAL(18,4) NOT NULL,"
        " big_ref BIGINT NOT NULL, remark VARCHAR(64));",
        "CREATE TABLE IF NOT EXISTS goal_shipments (shipment_id INT PRIMARY KEY,"
        " order_id INT NOT NULL, qty INT NOT NULL, waybill_no VARCHAR(16) NOT NULL);",
    ]
    for statement in pg + my:
        sql("postgres" if statement in pg else "mysql", statement)
    return {"schema": "ready", "tables": ["goal_customers", "goal_contracts",
                                          "goal_orders", "goal_shipments"]}


def seed() -> dict[str, Any]:
    if ROW_TOTAL > ROW_CAP:
        raise ValueError(f"fixture rows {ROW_TOTAL} exceed cap {ROW_CAP}")
    create_schema()
    for source, tables in (("postgres", ("goal_customers", "goal_contracts")),
                           ("mysql", ("goal_orders", "goal_shipments"))):
        for table in tables:
            count = int(sql(source, f"SELECT COUNT(*) FROM {table};").strip())
            if count:
                raise RuntimeError(f"{table} already seeded ({count} rows); refuse re-seed;"
                                   " recovery is down --destroy-data on this project's own"
                                   " databases only, never truncating unfamiliar tables")
    started = time.monotonic()
    # One transaction per source: a mid-batch failure leaves no partial rows
    # behind, so re-seeding after a failure cannot fork the fixture state.
    pg_rows = [
        "INSERT INTO goal_customers VALUES "
        + ",".join(f"('{cid}','{region}',{ref},{'NULL' if note is None else chr(39)+note+chr(39)})"
                   for cid, region, ref, note in CUSTOMERS) + ";",
        "INSERT INTO goal_contracts VALUES "
        + ",".join(f"({cid},'{customer}',{amount},'{no}')"
                   for cid, customer, amount, no in CONTRACTS) + ";",
    ]
    my_rows = [
        "INSERT INTO goal_orders VALUES "
        + ",".join(f"({oid},'{customer}',{amount},{ref},{'NULL' if remark is None else chr(39)+remark+chr(39)})"
                   for oid, customer, amount, ref, remark in ORDERS) + ";",
        "INSERT INTO goal_shipments VALUES "
        + ",".join(f"({sid},{oid},{qty},'{wb}')" for sid, oid, qty, wb in SHIPMENTS) + ";",
    ]
    sql("postgres", "BEGIN;\n" + "\n".join(pg_rows) + "\nCOMMIT;")
    sql("mysql", "START TRANSACTION;\n" + "\n".join(my_rows) + "\nCOMMIT;")
    expected_counts = {"goal_customers": len(CUSTOMERS), "goal_contracts": len(CONTRACTS),
                       "goal_orders": len(ORDERS), "goal_shipments": len(SHIPMENTS)}
    for table, expected_count in expected_counts.items():
        source = "postgres" if table in ("goal_customers", "goal_contracts") else "mysql"
        count = int(sql(source, f"SELECT COUNT(*) FROM {table};").strip())
        if count != expected_count:
            raise RuntimeError(f"{table}: seeded {count} rows, expected {expected_count}")
    report = {"rows_total": ROW_TOTAL, "row_cap": ROW_CAP, "ontologies": {
              "A_customers_contracts": "postgres", "B_orders": "mysql", "C_shipments": "mysql"},
              "platform_assets_note": "manual/curated action acceptance additionally needs "
                                      "platform-side assets created through the version front "
                                      "door in later phases; two physical sources alone only "
                                      "cover the query chain",
              "databases": {"source-pg": "integrated_source", "mysql": "integrated_source",
                            "db": "integrated_metadata"},
              "wall_seconds": round(time.monotonic() - started, 3),
              "result": "seeded_not_accepted", "full_e2e_pass": False}
    base.write_report("seed-goal.json", report, GOAL_ART)
    return report


def _observed_rows(source: str, statement: str) -> list[list[str]]:
    output = sql(source, statement).strip()
    return [line.split("|") for line in output.splitlines()] if output else []


def oracle() -> dict[str, Any]:
    expected = expected_view()
    started = time.monotonic()
    # One pipe-joined text column per row: psql -A and mysql --batch use
    # different client separators, so rows are joined server-side instead.
    contracts_pg = _observed_rows("postgres",
        "SELECT customer_id || '|' || COUNT(*) || '|' || COUNT(contract_no) || '|' ||"
        " COALESCE(CAST(SUM(amount) AS text),'<NULL>') FROM goal_contracts"
        " GROUP BY customer_id ORDER BY customer_id;")
    customers_pg = _observed_rows("postgres",
        "SELECT customer_id || '|' || region || '|' || CAST(big_ref AS text) || '|' ||"
        " COALESCE(note,'<NULL>') FROM goal_customers ORDER BY customer_id;")
    # MySQL side: per-customer order aggregation and per-order pre-grouped
    # shipment quantities rolled up per customer.
    orders_my = _observed_rows("mysql",
        "SELECT CONCAT_WS('|', customer_id, COUNT(*), COUNT(remark),"
        " COALESCE(CAST(SUM(amount) AS CHAR),'<NULL>')) FROM goal_orders"
        " GROUP BY customer_id ORDER BY customer_id;")
    qty_my = _observed_rows("mysql",
        "SELECT CONCAT_WS('|', o.customer_id, COUNT(t.order_id),"
        " COALESCE(CAST(SUM(t.qty) AS CHAR),'<NULL>')) FROM goal_orders o LEFT JOIN"
        " (SELECT order_id, SUM(qty) qty FROM goal_shipments GROUP BY order_id) t"
        " ON t.order_id = o.order_id GROUP BY o.customer_id ORDER BY o.customer_id;")

    observed: dict[str, dict[str, Any]] = {}
    for customer_id, region, big_ref, note in customers_pg:
        observed[customer_id] = {"region": region, "big_ref": big_ref, "note": note}
    for customer_id, count_star, count_field, total in contracts_pg:
        observed[customer_id]["contract_count"] = int(count_star)
        observed[customer_id]["contract_count_field"] = int(count_field)
        observed[customer_id]["contract_sum"] = total
    for customer_id, count_star, count_field, total in orders_my:
        observed[customer_id]["order_count"] = int(count_star)
        observed[customer_id]["order_remark_count"] = int(count_field)
        observed[customer_id]["order_sum"] = total
    for customer_id, ship_order_count, ship_qty in qty_my:
        observed[customer_id]["ship_order_count"] = int(ship_order_count)
        observed[customer_id]["ship_qty"] = ship_qty

    problems: list[str] = []
    for customer_id, row in expected["customers"].items():
        got = observed.get(customer_id)
        if got is None:
            problems.append(f"{customer_id}: missing from source rows")
            continue
        source_customer = next(c for c in CUSTOMERS if c[0] == customer_id)
        if got["region"] != row["region"]:
            problems.append(f"{customer_id}.region {got['region']} != {row['region']}")
        if got["big_ref"] != str(source_customer[2]):
            problems.append(f"{customer_id}.big_ref {got['big_ref']} != {source_customer[2]}")
        for field in ("contract_sum", "contract_count", "order_sum", "order_count",
                      "order_remark_count", "ship_qty", "ship_order_count"):
            absent = "<NULL>" if field in ("contract_sum", "order_sum", "ship_qty") else 0
            if str(got.get(field, absent)) != str(row[field]):
                problems.append(f"{customer_id}.{field} {got.get(field, absent)} != {row[field]}")
        # A customer absent from a source's groups (c2 contracts, c3 orders and
        # shipments) surfaces as NULL/0 aggregates, never as a fabricated value.
        if got.get("contract_count_field", 0) != row["contract_count"]:
            problems.append(f"{customer_id}: count(*)/count(field) contract mismatch")
        if customer_id not in dict((r[0], r[3]) for r in contracts_pg) and row["contract_sum"] != "<NULL>":
            problems.append(f"{customer_id}: absent group must expect NULL contract_sum")

    # Region rollup verified independently: expected buckets come from the pure
    # Python view, observed buckets roll up the SQL-observed per-customer values.
    observed_view = {}
    for customer_id, row in expected["customers"].items():
        got = observed.get(customer_id, {})
        absent = "<NULL>"
        observed_view[customer_id] = {
            "region": row["region"],
            "contract_sum": got.get("contract_sum", absent),
            "contract_count": got.get("contract_count", 0),
            "order_sum": got.get("order_sum", absent),
            "order_count": got.get("order_count", 0),
            "ship_qty": got.get("ship_qty", absent),
        }
    if _rollup_regions(observed_view) != expected["regions"]:
        problems.append(f"region rollup mismatch: {_rollup_regions(observed_view)} != {expected['regions']}")

    report = {"expected": expected, "observed": observed,
              "row_total": ROW_TOTAL, "problems": problems,
              "wall_seconds": round(time.monotonic() - started, 3),
              "result": "source_oracle_pass_not_full_e2e" if not problems else "oracle_mismatch",
              "full_e2e_pass": False,
              "note": "seed/oracle prove source data only; page, mapping, assistant "
                      "and deployment acceptance are separate phases"}
    base.write_report("oracle-goal.json", report, GOAL_ART)
    if problems:
        raise RuntimeError(f"goal oracle mismatch: {problems}")
    return report


# --------------------------------------------------------------------------
# stack lifecycle wrappers (ownership-checked; execution needs a slot).
# --------------------------------------------------------------------------

def goal_preflight(services: list[str]) -> dict[str, Any]:
    result = base.preflight(services, project=GOAL_PROJECT, ports=GOAL_PORTS,
                            images=GOAL_IMAGES, directory=GOAL_ART)
    reserved = {}
    for name, ports in RESERVED_HOST_PORTS.items():
        for port in ports:
            sock = socket.socket()
            try:
                sock.bind(("127.0.0.1", port)); reserved[f"{name}:{port}"] = "free"
            except OSError:
                reserved[f"{name}:{port}"] = "occupied"
            finally:
                sock.close()
    result["reserved_host_ports"] = reserved
    result["n8n_cdp"] = "not_in_compose_no_fake_readiness"
    return result


def goal_up(services: list[str]) -> dict[str, Any]:
    profiles = ["mysql"]
    for service, profile in (("dbx", "dbx"), ("n8n", "n8n"), ("browser", "cdp"),
                             ("kernel-gateway", "kernel")):
        if service in services:
            profiles.append(profile)
    return base.stack_up(services, project=GOAL_PROJECT, ports=GOAL_PORTS,
                         images=GOAL_IMAGES, compose_file=GOAL_COMPOSE,
                         profiles=tuple(profiles), state_path=GOAL_STATE,
                         directory=GOAL_ART)


def goal_down(destroy_data: bool) -> dict[str, Any]:
    return base.stack_down(destroy_data, project=GOAL_PROJECT, state_path=GOAL_STATE,
                           directory=GOAL_ART, profiles=("mysql", "dbx", "n8n", "cdp", "kernel"),
                           compose_file=GOAL_COMPOSE)


def goal_status() -> dict[str, Any]:
    state = base.update_state(GOAL_PROJECT, GOAL_ART)
    state["databases"] = {"db": "integrated_metadata", "source-pg": "integrated_source",
                          "mysql": "integrated_source"}
    state["reserved_host_ports"] = RESERVED_HOST_PORTS
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "up"):
        entry = sub.add_parser(name)
        entry.add_argument("services", nargs="+", choices=sorted(GOAL_PORTS))
    down = sub.add_parser("down")
    down.add_argument("--destroy-data", action="store_true")
    sub.add_parser("status")
    sub.add_parser("schema")
    sub.add_parser("seed")
    sub.add_parser("oracle")
    args = parser.parse_args()
    try:
        if args.command == "preflight":
            result = goal_preflight(args.services)
        elif args.command == "up":
            result = goal_up(args.services)
        elif args.command == "down":
            result = goal_down(args.destroy_data)
        elif args.command == "status":
            result = goal_status()
        elif args.command == "schema":
            result = create_schema()
        elif args.command == "seed":
            result = seed()
        else:
            result = oracle()
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 2 if result.get("result") == "blocked" else 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"ontology-goal-fixture: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
