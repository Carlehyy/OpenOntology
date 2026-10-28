#!/usr/bin/env python3
"""Four-table federated fixture (PG customers/contracts x MySQL orders/shipment
details), an independent pure-Python oracle, and acceptance evidence capture.

Shares the safety envelope of integrated_stack.py (stdlib + local images only,
no dotenv, no pulls, loopback-only published ports, ownership-checked
containers) through its parameterized helpers. This module never adopts or
even queries the oo-integrated-20261024 project or any other project's
resources; every operation is scoped to FED_PROJECT by container label.

Canonical federated semantics (what the real acceptance must reproduce):

    per_order_shipments AS (
        SELECT order_id, COUNT(*) AS ship_rows, COUNT(DISTINCT waybill_no) AS waybills,
               SUM(ship_qty) AS qty, SUM(ship_cost) AS cost
        FROM fed_shipments GROUP BY order_id)          -- pre-grouped: no fanout
    customer_view = fed_orders o
        JOIN fed_customers c ON c.customer_id = o.customer_ref          -- cross source
        LEFT JOIN per_order_shipments s ON s.order_id = o.order_id
    unshipped(o) := s.ship_rows IS NULL  (equivalently NOT EXISTS; equals status='PENDING')

Duplicate waybill numbers exist because every shipment row of an order shares
the order's waybill; they must not be dodged by a 1:1 filter. Unshipped orders
(status='PENDING', o%17=0) truly have no shipment rows. The oracle goal is the
customer view: contracts of the customer + 90-day order amounts + unshipped
order count; tier/status grouping is an extension, never a replacement.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from datetime import date, timedelta
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
FED_PROJECT = "oo-fedanalysis-20261026"
FED_COMPOSE = ROOT / "docker-compose.federated-e2e.yml"
FED_ART = ROOT / ".artifacts" / "integrated-e2e" / "federated"
FED_STATE = FED_ART / "resources.json"
FED_PORTS = {
    "db": [52432], "source-pg": [52433], "dbx": [52434], "mysql": [51306],
    "redis": [53379], "nats": [51222, 55222],
    "neo4j": [52474, 52687], "minio": [53000, 53001],
}
FED_IMAGES = {
    "db": "postgres:16-alpine", "source-pg": "postgres:16-alpine",
    "mysql": "mysql:8.4", "redis": "redis:7-alpine", "nats": "nats:alpine",
    "neo4j": "neo4j:5.20-community", "minio": "minio/minio:latest",
    "dbx": "docker.cnb.cool/dbxio.com/dbx@sha256:b661f7e35deed06d9611dcda79c44ffcc8e4fd0f44308028ea77f7f18b36638c",
}
FED_SCALES = (1_000_000, 2_300_000, 10_000_000)
MAX_ORDERS = 10_000_000
JS_SAFE = 9_007_199_254_740_993  # 2**53-1: external refs start beyond JS safe ints

# Fixed fixture calendar (DATE columns keep both dialects exactly representable).
CONTRACT_ANCHOR = date(2025, 1, 1)
ORDER_ANCHOR = date(2026, 6, 30)          # +1..90 days -> below
WINDOW_START = date(2026, 7, 1)
WINDOW_END = date(2026, 9, 28)            # fixed 90-day order window, all orders inside
SHIPMENT_ID_STRIDE = 3                    # shipment_id = (order_id-1)*3 + j, j=1..S(o)
UNSHIPPED_MOD = 17                        # o%17==0 -> PENDING, no shipment rows
MULTI_MOD = 9                             # o%9==0 -> second shipment row
TRIPLE_MOD = 81                           # o%81==0 -> third shipment row

SHIPPED_STATUSES = ("PAID", "PACKED", "SHIPPED", "IN_TRANSIT", "DELIVERED", "CLOSED", "RETURNED", "ON_HOLD")
PENDING = "PENDING"

CUSTOMER_COLUMNS = ["customer_id", "external_ref", "is_key", "tier", "region", "credit_limit", "contact_note"]
CONTRACT_COLUMNS = ["contract_id", "customer_id", "contract_no", "valid_from", "valid_to",
                    "signed_amount", "currency", "terms_note"]
ORDER_COLUMNS = ["order_id", "customer_ref", "contract_ref", "waybill_no", "gross_cents",
                 "net_amount", "status", "created_at", "remark"]
SHIPMENT_COLUMNS = ["shipment_id", "order_id", "waybill_no", "ship_qty", "ship_cost", "carrier", "ship_note"]

# Budget classes: dbx is a platform metadata component (platform baseline), the
# source databases are appliances counted separately. API/executor/browser/n8n
# are required baseline members that live outside this compose; until their
# PIDs/jobs are evidenced by the coordinated runner, acceptance cannot pass.
PLATFORM_BUDGET_MIB = {"db": 512, "redis": 96, "nats": 96, "neo4j": 1280, "minio": 256, "dbx": 768}
REQUIRED_BASELINE_OUTSIDE_COMPOSE = ("api", "executor", "browser", "n8n")
SOURCE_BUDGET_MIB = {"source-pg": 384, "mysql": 512}


# --------------------------------------------------------------------------
# Pure Python row formulas: the independent oracle. Nothing in this section
# touches SQL; the engines are only used later to verify the same rows.
# --------------------------------------------------------------------------

def dimensions(orders: int) -> tuple[int, int]:
    return max(10, orders // 10), max(10, orders // 5)


def customer_row(c: int) -> dict[str, Any]:
    return {"customer_id": c, "external_ref": JS_SAFE + c, "is_key": c % 10 == 0,
            "tier": f"T{c % 5}", "region": f"RG{c % 17:02d}",
            "credit_limit": Decimal(c % 100000) + Decimal(c % 97) / Decimal(100),
            "contact_note": None if c % 13 == 0 else f"CN{c:010d}"}


def contract_row(k: int, customers: int) -> dict[str, Any]:
    valid_from = CONTRACT_ANCHOR + timedelta(days=k % 365)
    # (k-1)//5+1 is scale independent, so extend-to-larger-N stays consistent.
    return {"contract_id": k, "customer_id": (k - 1) // 5 + 1,
            "contract_no": f"CT{k:012d}",
            "valid_from": valid_from, "valid_to": valid_from + timedelta(days=365 + k % 180),
            "signed_amount": Decimal(k % 1000000) + Decimal(k % 89) / Decimal(100),
            "currency": ("CNY", "USD", "EUR")[k % 3],
            "terms_note": None if k % 7 == 0 else f"TM{k:010d}"}


def order_row(o: int, customers: int, contracts: int) -> dict[str, Any]:
    return {"order_id": o, "customer_ref": (o - 1) // 10 + 1,
            "contract_ref": (o - 1) // 5 + 1, "waybill_no": f"WB{o:012d}",
            "gross_cents": (o % 1000000) * 100 + o % 31,
            "net_amount": Decimal(o % 1000000) + Decimal(o % 9973) / Decimal(10000),
            "status": order_status(o),
            "created_at": ORDER_ANCHOR + timedelta(days=o % 90 + 1),
            "remark": None if o % 11 == 0 else f"RM{o:010d}"}


def order_status(o: int) -> str:
    if o % UNSHIPPED_MOD == 0:
        return PENDING
    return SHIPPED_STATUSES[o % len(SHIPPED_STATUSES)]


def shipment_count(o: int) -> int:
    if o % UNSHIPPED_MOD == 0:
        return 0
    return 1 + int(o % MULTI_MOD == 0) + int(o % TRIPLE_MOD == 0)


def shipment_row(m: int, o: int) -> dict[str, Any]:
    return {"shipment_id": m, "order_id": o, "waybill_no": f"WB{o:012d}",
            "ship_qty": 1 + m % 7,
            "ship_cost": Decimal(m % 500000) + Decimal(m % 53) / Decimal(10000),
            "carrier": f"CR{m % 7:02d}",
            "ship_note": None if m % 17 == 0 else f"SN{m:010d}"}


def shipment_ids(o: int) -> list[int]:
    return [(o - 1) * SHIPMENT_ID_STRIDE + j for j in range(1, shipment_count(o) + 1)]


def expected_totals(orders: int) -> dict[str, Any]:
    """Single deterministic pass: global totals, multiplicity histogram and the
    tier/status extension for the canonical customer view."""
    customers, contracts = dimensions(orders)
    gross = 0
    net = Decimal(0)
    unshipped = 0
    hist = {1: 0, 2: 0, 3: 0}
    waybill_rows = 0
    statuses: set[str] = set()
    created: set[str] = set()
    min_created = max_created = None
    tier_status: dict[tuple[str, str], list[Any]] = {}
    for o in range(1, orders + 1):
        row = order_row(o, customers, contracts)
        gross += row["gross_cents"]
        net += row["net_amount"]
        count = shipment_count(o)
        waybill_rows += count
        if count == 0:
            unshipped += 1
        else:
            hist[count] += 1
        statuses.add(row["status"])
        iso = row["created_at"].isoformat()
        created.add(iso)
        min_created = iso if min_created is None or iso < min_created else min_created
        max_created = iso if max_created is None or iso > max_created else max_created
        customer = customer_row(row["customer_ref"])
        bucket = tier_status.setdefault((customer["tier"], row["status"]), [0, 0, Decimal(0)])
        bucket[0] += 1
        bucket[1] += row["gross_cents"]
        bucket[2] += row["net_amount"]
    shipped = orders - unshipped
    credit = Decimal(0)
    contact_notes = 0
    key_customers = 0
    for c in range(1, customers + 1):
        row = customer_row(c)
        credit += row["credit_limit"]
        contact_notes += row["contact_note"] is not None
        key_customers += row["is_key"]
    signed = Decimal(0)
    terms_notes = 0
    contract_customers: set[int] = set()
    min_valid_from = max_valid_to = None
    for k in range(1, contracts + 1):
        row = contract_row(k, customers)
        signed += row["signed_amount"]
        terms_notes += row["terms_note"] is not None
        contract_customers.add(row["customer_id"])
        start, end = row["valid_from"].isoformat(), row["valid_to"].isoformat()
        min_valid_from = start if min_valid_from is None or start < min_valid_from else min_valid_from
        max_valid_to = end if max_valid_to is None or end > max_valid_to else max_valid_to
    ship_cost = Decimal(0)
    ship_qty = 0
    ship_notes = 0
    carriers: set[str] = set()
    for o in range(1, orders + 1):
        for m in shipment_ids(o):
            row = shipment_row(m, o)
            ship_cost += row["ship_cost"]
            ship_qty += row["ship_qty"]
            ship_notes += row["ship_note"] is not None
            carriers.add(row["carrier"])
    return {"orders": orders, "customers": customers, "contracts": contracts,
            "shipment_rows": waybill_rows, "shipped_orders": shipped, "unshipped_orders": unshipped,
            "shipment_multiplicity": hist,
            "distinct_waybills_shipments": shipped,  # every row shares the order waybill
            "distinct_status": len(statuses), "distinct_created_days": len(created),
            "distinct_order_customers": customers,
            "distinct_contract_customers": len(contract_customers),
            "min_created_at": min_created, "max_created_at": max_created,
            "min_valid_from": min_valid_from, "max_valid_to": max_valid_to,
            "sum_gross_cents": gross, "sum_net_amount": str(net),
            "sum_credit_limit": str(credit), "contact_note_count": contact_notes,
            "key_customer_count": key_customers,
            "sum_signed_amount": str(signed), "terms_note_count": terms_notes,
            "sum_ship_cost": str(ship_cost), "sum_ship_qty": ship_qty, "ship_note_count": ship_notes,
            "distinct_carriers": len(carriers),
            "created_at_window": {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat(),
                                  "days": 90, "all_orders_inside": True},
            "tier_status_extension": {f"{tier}|{status}": {"orders": value[0], "gross_cents": value[1],
                                                           "net_amount": str(value[2])}
                                      for (tier, status), value in sorted(tier_status.items())}}


def expected_customer_view(orders: int, customer_ids: list[int]) -> list[dict[str, Any]]:
    """Primary oracle goal: per customer, contract count/sum plus 90-day order
    amounts and unshipped order count. Bounded to an explicit id sample."""
    customers, contracts = dimensions(orders)
    wanted = {cid for cid in customer_ids if 1 <= cid <= customers}
    detail = {cid: {"customer_id": cid, "is_key": customer_row(cid)["is_key"],
                    "tier": customer_row(cid)["tier"],
                    "contract_count": 0, "sum_signed_amount": Decimal(0),
                    "orders_90d": 0, "sum_gross_cents": 0, "sum_net_amount": Decimal(0),
                    "unshipped_orders": 0} for cid in wanted}
    for k in range(1, contracts + 1):
        row = contract_row(k, customers)
        entry = detail.get(row["customer_id"])
        if entry is not None:
            entry["contract_count"] += 1
            entry["sum_signed_amount"] += row["signed_amount"]
    for o in range(1, orders + 1):
        row = order_row(o, customers, contracts)
        entry = detail.get(row["customer_ref"])
        if entry is not None:
            entry["orders_90d"] += 1
            entry["sum_gross_cents"] += row["gross_cents"]
            entry["sum_net_amount"] += row["net_amount"]
            if row["status"] == PENDING:
                entry["unshipped_orders"] += 1
    result = []
    for cid in sorted(detail):
        entry = dict(detail[cid])
        entry["sum_signed_amount"] = str(entry["sum_signed_amount"])
        entry["sum_net_amount"] = str(entry["sum_net_amount"])
        result.append(entry)
    return result


def canary_ids(orders: int) -> dict[str, list[int]]:
    customers, contracts = dimensions(orders)
    return {"customers": [1, customers // 2, customers],
            "contracts": [1, contracts // 2, contracts],
            "orders": sorted({1, UNSHIPPED_MOD, MULTI_MOD, TRIPLE_MOD, orders // 2, orders}),
            "shipment_orders": [1, 2, UNSHIPPED_MOD, MULTI_MOD, TRIPLE_MOD]}


# --------------------------------------------------------------------------
# Dialect helpers (same conventions as integrated_stack.expressions).
# --------------------------------------------------------------------------

def _is_pg(source: str) -> bool:
    return source == "postgres"


def _concat(source: str, *parts: str) -> str:
    return " || ".join(parts) if _is_pg(source) else f"CONCAT({','.join(parts)})"


def _text(source: str, value: str) -> str:
    return f"CAST({value} AS text)" if _is_pg(source) else f"CAST({value} AS CHAR)"


def _pad(source: str, value: str, width: int) -> str:
    return f"LPAD({_text(source, value)},{width},'0')"


def _date_literal(source: str, value: date) -> str:
    return f"DATE '{value.isoformat()}'" if _is_pg(source) else f"'{value.isoformat()}'"


def _date_add(source: str, base_expr: str, days_expr: str) -> str:
    if _is_pg(source):
        # date + integer has no bigint overload; MOD over bigint columns needs the cast.
        return f"{base_expr} + CAST(({days_expr}) AS integer)"
    return f"DATE_ADD({base_expr}, INTERVAL ({days_expr}) DAY)"


def _null_note(source: str, row_expr: str, modulus: int, prefix: str) -> str:
    return (f"CASE WHEN MOD({row_expr},{modulus})=0 THEN NULL "
            f"ELSE {_concat(source, chr(39) + prefix + chr(39), _pad(source, row_expr, 10))} END")


def _status_case(order_expr: str = "order_id") -> str:
    inner = " ".join(f"WHEN {index} THEN '{status}'" for index, status in enumerate(SHIPPED_STATUSES))
    return (f"CASE WHEN MOD({order_expr},{UNSHIPPED_MOD})=0 THEN '{PENDING}' "
            f"ELSE CASE MOD({order_expr},8) {inner} ELSE '{SHIPPED_STATUSES[-1]}' END END")


def _int_div(source: str, numerator: str, denominator: int) -> str:
    return f"({numerator})/{denominator}" if _is_pg(source) else f"({numerator}) DIV {denominator}"


def customer_expressions(source: str, n: str = "n") -> list[str]:
    return [n, f"{JS_SAFE}+{n}", f"MOD({n},10)=0",
            _concat(source, "'T'", _pad(source, f"MOD({n},5)", 1)),
            _concat(source, "'RG'", _pad(source, f"MOD({n},17)", 2)),
            f"CAST(MOD({n},100000)+MOD({n},97)/100.00 AS DECIMAL(18,2))",
            _null_note(source, n, 13, "CN")]


def contract_expressions(source: str, n: str = "n") -> list[str]:
    valid_from = _date_add(source, _date_literal(source, CONTRACT_ANCHOR), f"MOD({n},365)")
    return [n, f"{_int_div(source, n + '-1', 5)}+1",
            _concat(source, "'CT'", _pad(source, n, 12)),
            valid_from,
            _date_add(source, valid_from, f"365+MOD({n},180)"),
            f"CAST(MOD({n},1000000)+MOD({n},89)/100.00 AS DECIMAL(18,2))",
            f"CASE MOD({n},3) WHEN 0 THEN 'CNY' WHEN 1 THEN 'USD' ELSE 'EUR' END",
            _null_note(source, n, 7, "TM")]


def order_expressions(source: str, n: str = "n") -> list[str]:
    return [n, f"{_int_div(source, n + '-1', 10)}+1", f"{_int_div(source, n + '-1', 5)}+1",
            _concat(source, "'WB'", _pad(source, n, 12)),
            f"MOD({n},1000000)*100+MOD({n},31)",
            f"CAST(MOD({n},1000000)+MOD({n},9973)/10000.0 AS DECIMAL(18,4))",
            _status_case(n),
            _date_add(source, _date_literal(source, ORDER_ANCHOR), f"MOD({n},90)+1"),
            _null_note(source, n, 11, "RM")]


def shipment_expressions(source: str, m: str = "m", o: str = "n") -> list[str]:
    return [m, o,
            _concat(source, "'WB'", _pad(source, o, 12)),
            f"1+MOD({m},7)",
            f"CAST(MOD({m},500000)+MOD({m},53)/10000.0 AS DECIMAL(12,4))",
            _concat(source, "'CR'", _pad(source, f"MOD({m},7)", 2)),
            _null_note(source, m, 17, "SN")]


# --------------------------------------------------------------------------
# SQL plumbing scoped to the federated project's own containers.
# --------------------------------------------------------------------------

def sql2(source: str, statement: str) -> str:
    return base.sql(source, statement, project=FED_PROJECT)


def require_fed(service: str) -> str:
    return base.require_owned(service, project=FED_PROJECT)


def sql2_stream(source: str, statement: str) -> tuple[str, int]:
    """Bounded streaming of a canonical text projection (sha256, line count)."""
    service = "source-pg" if source == "postgres" else "mysql"
    cid = require_fed(service)
    if source == "postgres":
        command = ["docker", "exec", "-i", cid, "psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
                   "-U", "integrated_e2e", "-d", "integrated_source"]
    else:
        command = ["docker", "exec", "-i", "-e", "MYSQL_PWD=integrated_e2e_fixture", cid,
                   "mysql", "--batch", "--raw", "--skip-column-names", "--quick",
                   "-u", "integrated_e2e", "integrated_source"]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=base.SAFE_ENV)
    assert process.stdin and process.stdout and process.stderr
    process.stdin.write(statement.encode())
    process.stdin.close()
    digest = hashlib.sha256()
    lines = 0
    try:
        for block in iter(lambda: process.stdout.read(65536), b""):
            digest.update(block)
            lines += block.count(b"\n")
        error = process.stderr.read(4096)
        code = process.wait(timeout=60)
        if code:
            raise RuntimeError(f"streaming query failed: exit={code}, error={error.decode(errors='replace')[:500]}")
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
    return digest.hexdigest(), lines


def _seq(source: str, start: int, stop: int) -> str:
    if _is_pg(source):
        return f"generate_series({start},{stop}) AS seq(n)"
    digits = "(" + " UNION ALL ".join(f"SELECT {i} AS d" for i in range(10)) + ")"
    value = "+".join(f"d{i}.d*{10**i}" for i in range(5))
    return (f"(SELECT {start}+{value} AS n FROM "
            + " CROSS JOIN ".join(f"{digits} d{i}" for i in range(5))
            + f") seq WHERE n<={stop}")


def _table_exists(source: str, table: str) -> bool:
    if _is_pg(source):
        return sql2(source, f"SELECT to_regclass('public.{table}') IS NOT NULL;").strip() == "t"
    return sql2(source, f"SELECT COUNT(*) FROM information_schema.tables "
                        f"WHERE table_schema=DATABASE() AND table_name='{table}';").strip() != "0"


# --------------------------------------------------------------------------
# schema / seed
# --------------------------------------------------------------------------

FED_DDL = {
    "postgres": [
        """CREATE TABLE fed_customers (
             customer_id BIGINT PRIMARY KEY, external_ref BIGINT NOT NULL, is_key BOOLEAN NOT NULL,
             tier VARCHAR(8) NOT NULL, region VARCHAR(16) NOT NULL,
             credit_limit DECIMAL(18,2) NOT NULL, contact_note VARCHAR(64))""",
        """CREATE TABLE fed_contracts (
             contract_id BIGINT PRIMARY KEY, customer_id BIGINT NOT NULL,
             contract_no VARCHAR(24) NOT NULL, valid_from DATE NOT NULL, valid_to DATE NOT NULL,
             signed_amount DECIMAL(18,2) NOT NULL, currency VARCHAR(8) NOT NULL,
             terms_note VARCHAR(64))""",
        "CREATE INDEX idx_fed_contracts_customer ON fed_contracts(customer_id)",
    ],
    "mysql": [
        """CREATE TABLE fed_orders (
             order_id BIGINT PRIMARY KEY, customer_ref BIGINT NOT NULL, contract_ref BIGINT NOT NULL,
             waybill_no VARCHAR(24) NOT NULL, gross_cents BIGINT NOT NULL,
             net_amount DECIMAL(18,4) NOT NULL, status VARCHAR(12) NOT NULL,
             created_at DATE NOT NULL, remark VARCHAR(64),
             KEY idx_fed_orders_customer (customer_ref), KEY idx_fed_orders_created (created_at))""",
        """CREATE TABLE fed_shipments (
             shipment_id BIGINT PRIMARY KEY, order_id BIGINT NOT NULL,
             waybill_no VARCHAR(24) NOT NULL, ship_qty INT NOT NULL,
             ship_cost DECIMAL(12,4) NOT NULL, carrier VARCHAR(12) NOT NULL,
             ship_note VARCHAR(64), KEY idx_fed_shipments_order (order_id))""",
    ],
}


def create_schema() -> dict[str, Any]:
    existing = [table for source, tables in (("postgres", ("fed_customers", "fed_contracts")),
                                             ("mysql", ("fed_orders", "fed_shipments")))
                for table in tables if _table_exists(source, table)]
    if existing:
        raise RuntimeError(f"federated tables already exist: {existing}; use seed --extend or drop the volume explicitly")
    for statement in FED_DDL["postgres"]:
        sql2("postgres", statement)
    for statement in FED_DDL["mysql"]:
        sql2("mysql", statement)
    report = {"result": "schema_created", "tables": ["fed_customers", "fed_contracts", "fed_orders", "fed_shipments"]}
    base.write_report("federated-schema.json", report, FED_ART)
    return report


def _insert(source: str, table: str, columns: list[str], expressions: list[str], seq: str,
            extra_where: str = "") -> None:
    # The MySQL digit-table seq already carries "WHERE n<=stop"; extra row
    # filters must extend it with AND instead of opening a second WHERE.
    clause = ""
    if extra_where:
        clause = (" AND " if " WHERE " in seq else " WHERE ") + extra_where
    sql2(source, f"INSERT INTO {table} ({','.join(columns)}) "
                 f"SELECT {','.join(expressions)} FROM {seq}{clause};")


def _generate_range(target: int, start: int, batch_size: int) -> None:
    customers, contracts = dimensions(target)
    current_customers, current_contracts = dimensions(start) if start else (0, 0)
    for c in range(current_customers + 1, customers + 1, batch_size):
        stop = min(c + batch_size - 1, customers)
        _insert("postgres", "fed_customers", CUSTOMER_COLUMNS,
                customer_expressions("postgres"), _seq("postgres", c, stop))
    for k in range(current_contracts + 1, contracts + 1, batch_size):
        stop = min(k + batch_size - 1, contracts)
        _insert("postgres", "fed_contracts", CONTRACT_COLUMNS,
                contract_expressions("postgres"), _seq("postgres", k, stop))
    for o in range(start + 1, target + 1, batch_size):
        stop = min(o + batch_size - 1, target)
        _insert("mysql", "fed_orders", ORDER_COLUMNS,
                order_expressions("mysql"), _seq("mysql", o, stop))
        variants = ((1, f"MOD(n,{UNSHIPPED_MOD})<>0"),
                    (2, f"MOD(n,{MULTI_MOD})=0 AND MOD(n,{UNSHIPPED_MOD})<>0"),
                    (3, f"MOD(n,{TRIPLE_MOD})=0 AND MOD(n,{UNSHIPPED_MOD})<>0"))
        for j, where in variants:
            m = f"(n-1)*{SHIPMENT_ID_STRIDE}+{j}"
            _insert("mysql", "fed_shipments", SHIPMENT_COLUMNS,
                    shipment_expressions("mysql", m=m), _seq("mysql", o, stop), where)


def _current_counts() -> dict[str, int]:
    return {"customers": int(sql2("postgres", "SELECT COUNT(*) FROM fed_customers;").strip()),
            "contracts": int(sql2("postgres", "SELECT COUNT(*) FROM fed_contracts;").strip()),
            "orders": int(sql2("mysql", "SELECT COUNT(*) FROM fed_orders;").strip()),
            "shipments": int(sql2("mysql", "SELECT COUNT(*) FROM fed_shipments;").strip())}


TABLE_SOURCE = {"fed_customers": "postgres", "fed_contracts": "postgres",
                "fed_orders": "mysql", "fed_shipments": "mysql"}


def seed(orders: int, batch_size: int, extend: bool) -> dict[str, Any]:
    # Block mappings ((o-1)//10, (o-1)//5) require orders to be a multiple of 10
    # so every referenced dimension row exists at every scale.
    if orders <= 0 or orders % 10 != 0 or orders > MAX_ORDERS or not 1 <= batch_size <= 100_000:
        raise ValueError(f"orders must be a multiple of 10 in 10..{MAX_ORDERS} and batch-size 1..100000")
    start = 0
    if extend:
        for table, source in TABLE_SOURCE.items():
            if not _table_exists(source, table):
                raise RuntimeError(f"extend requires existing {table}")
        counts = _current_counts()
        start = counts["orders"]
        if start == 0 or start >= orders:
            raise ValueError("extend requires 0 < current orders < target")
        customers, contracts = dimensions(start)
        expected = {"customers": customers, "contracts": contracts, "orders": start,
                    "shipments": sum(shipment_count(o) for o in range(1, start + 1))}
        if counts != expected:
            raise RuntimeError(f"existing federated rows do not match a deterministic prefix: {counts} vs {expected}")
    else:
        for table, source in TABLE_SOURCE.items():
            if _table_exists(source, table):
                raise RuntimeError("federated tables already exist; use --extend to grow a deterministic prefix")
        create_schema()
    started = time.monotonic()
    _generate_range(orders, start, batch_size)
    counts = _current_counts()
    report = {"source_layout": {"postgres": ["fed_customers", "fed_contracts"],
                                "mysql": ["fed_orders", "fed_shipments"]},
              "orders": orders, "extended_from": start, "batch_size": batch_size,
              "final_counts": counts, "wall_seconds": round(time.monotonic() - started, 3),
              "is_final_scale": orders in FED_SCALES, "result": "seeded_not_accepted",
              "full_e2e_pass": False,
              "note": "seed success only proves source data placement; run oracle before any acceptance claim"}
    base.write_report(f"seed-federated-{orders}.json", report, FED_ART)
    return report


# --------------------------------------------------------------------------
# oracle
# --------------------------------------------------------------------------

def _aggregate(source: str, table: str, fields: list[str]) -> list[str]:
    casts = [_text(source, field) for field in fields]
    joined = " || '|' || ".join(casts) if _is_pg(source) else f"CONCAT_WS('|',{','.join(casts)})"
    row = sql2(source, f"SELECT {joined} FROM {table};").strip()
    return [value if value == "" else value for value in row.split("|")]


def _number(value: str) -> Decimal:
    return Decimal(value)


def _compare(observed: list[str], expected: list[Any]) -> None:
    if len(observed) != len(expected):
        raise RuntimeError(f"aggregate arity mismatch: {observed} vs {expected}")
    for index, (got, want) in enumerate(zip(observed, expected)):
        if isinstance(want, Decimal):
            if _number(got) != want:
                raise RuntimeError(f"aggregate[{index}] mismatch: {got} != {want}")
        elif isinstance(want, int):
            if int(got) != want:
                raise RuntimeError(f"aggregate[{index}] mismatch: {got} != {want}")
        elif got != want:
            raise RuntimeError(f"aggregate[{index}] mismatch: {got!r} != {want!r}")


def _formula_mismatch(source: str, table: str, columns: list[str], expressions: list[str]) -> int:
    if _is_pg(source):
        comparisons = [f"{column} IS DISTINCT FROM ({expr})" for column, expr in zip(columns, expressions)]
    else:
        comparisons = [f"NOT ({column} <=> ({expr}))" for column, expr in zip(columns, expressions)]
    return int(sql2(source, f"SELECT COUNT(*) FROM {table} WHERE {' OR '.join(comparisons)};").strip())


def _canonical_projection(source: str, columns: list[str], booleans: tuple[str, ...],
                          dates: tuple[str, ...]) -> str:
    fields = []
    for column in columns:
        if column in booleans:
            expr = f"CASE WHEN {column} THEN '1' ELSE '0' END"
        elif column in dates:
            expr = _text(source, column)
        else:
            expr = _text(source, column)
        fields.append(f"COALESCE({expr},'<NULL>')")
    return " || '|' || ".join(fields) if _is_pg(source) else f"CONCAT_WS('|',{','.join(fields)})"


def _digest(source: str, table: str, order_key: str, columns: list[str], booleans: tuple[str, ...],
            dates: tuple[str, ...]) -> tuple[str, int]:
    projection = _canonical_projection(source, columns, booleans, dates)
    return sql2_stream(source, f"SELECT {projection} FROM {table} ORDER BY {order_key};")


def oracle(orders: int, customer_ids: list[int], sample: int) -> dict[str, Any]:
    started = time.monotonic()
    customers, contracts = dimensions(orders)
    counts = _current_counts()
    totals = expected_totals(orders)
    structural = {"customers": customers, "contracts": contracts, "orders": orders,
                  "shipments": totals["shipment_rows"]}
    if counts != structural:
        raise RuntimeError(f"row counts {counts} do not match target {structural}; seed first or fix --orders")

    # 1) per-table aggregates observed by each engine vs the Python oracle
    pg_customer = _aggregate("postgres", "fed_customers", [
        "COUNT(*)", "COUNT(DISTINCT tier)", "COUNT(DISTINCT region)", "SUM(external_ref)",
        "SUM(credit_limit)", "COUNT(contact_note)", "COUNT(*) FILTER (WHERE is_key)"])
    _compare(pg_customer, [customers, min(5, customers), min(17, customers),
                           customers * JS_SAFE + customers * (customers + 1) // 2,
                           Decimal(totals["sum_credit_limit"]), totals["contact_note_count"],
                           totals["key_customer_count"]])
    pg_contract = _aggregate("postgres", "fed_contracts", [
        "COUNT(*)", "COUNT(DISTINCT customer_id)", "COUNT(DISTINCT currency)", "SUM(signed_amount)",
        "COUNT(terms_note)", "MIN(valid_from)", "MAX(valid_to)"])
    _compare(pg_contract, [contracts, totals["distinct_contract_customers"], min(3, contracts),
                           Decimal(totals["sum_signed_amount"]), totals["terms_note_count"],
                           totals["min_valid_from"], totals["max_valid_to"]])
    my_order = _aggregate("mysql", "fed_orders", [
        "COUNT(*)", "COUNT(DISTINCT status)", "COUNT(DISTINCT waybill_no)", "COUNT(DISTINCT created_at)",
        "MIN(created_at)", "MAX(created_at)", "SUM(gross_cents)", "SUM(net_amount)", "COUNT(remark)",
        "SUM(status='PENDING')"])
    _compare(my_order, [orders, totals["distinct_status"], orders, totals["distinct_created_days"],
                        totals["min_created_at"], totals["max_created_at"],
                        totals["sum_gross_cents"], Decimal(totals["sum_net_amount"]),
                        orders - orders // 11, totals["unshipped_orders"]])
    my_shipment = _aggregate("mysql", "fed_shipments", [
        "COUNT(*)", "COUNT(DISTINCT waybill_no)", "COUNT(DISTINCT order_id)", "SUM(ship_qty)",
        "SUM(ship_cost)", "COUNT(ship_note)", "COUNT(DISTINCT carrier)"])
    _compare(my_shipment, [totals["shipment_rows"], totals["distinct_waybills_shipments"],
                           totals["shipped_orders"], totals["sum_ship_qty"],
                           Decimal(totals["sum_ship_cost"]), totals["ship_note_count"],
                           totals["distinct_carriers"]])

    # 2) full per-row formula verification (every column of every table)
    mismatches = {
        "fed_customers": _formula_mismatch("postgres", "fed_customers", CUSTOMER_COLUMNS,
                                           customer_expressions("postgres", "customer_id")),
        "fed_contracts": _formula_mismatch("postgres", "fed_contracts", CONTRACT_COLUMNS,
                                           contract_expressions("postgres", "contract_id")),
        "fed_orders": _formula_mismatch("mysql", "fed_orders", ORDER_COLUMNS,
                                        order_expressions("mysql", "order_id")),
        "fed_shipments": _formula_mismatch("mysql", "fed_shipments", SHIPMENT_COLUMNS,
                                           shipment_expressions("mysql", "shipment_id", "order_id")),
    }
    if any(mismatches.values()):
        raise RuntimeError(f"formula mismatches: {mismatches}")

    # 3) ordered canonical digests (PK order; indexes avoid engine sorts)
    digests = {
        "fed_customers": _digest("postgres", "fed_customers", "customer_id", CUSTOMER_COLUMNS,
                                 ("is_key",), ()),
        "fed_contracts": _digest("postgres", "fed_contracts", "contract_id", CONTRACT_COLUMNS,
                                 (), ("valid_from", "valid_to")),
        "fed_orders": _digest("mysql", "fed_orders", "order_id", ORDER_COLUMNS, (), ("created_at",)),
        "fed_shipments": _digest("mysql", "fed_shipments", "shipment_id", SHIPMENT_COLUMNS, (), ()),
    }
    for table, (_, lines) in digests.items():
        expected_rows = {"fed_customers": customers, "fed_contracts": contracts,
                         "fed_orders": orders, "fed_shipments": totals["shipment_rows"]}[table]
        if lines != expected_rows:
            raise RuntimeError(f"{table} digest streamed {lines} rows, expected {expected_rows}")

    # 4) customer-view cross-check on the sampled ids (per-source SQL vs Python)
    ids = sorted(set(customer_ids) | set(range(1, min(sample, customers) + 1)))
    if len(ids) > 500:
        raise ValueError("customer sample is capped at 500 ids per oracle run")
    expected_view = expected_customer_view(orders, ids)
    observed_orders: dict[int, list[str]] = {}
    id_list = ",".join(str(cid) for cid in ids)
    for row in sql2("mysql", f"SELECT customer_ref,COUNT(*),SUM(gross_cents),SUM(net_amount),"
                             f"SUM(status='{PENDING}') FROM fed_orders "
                             f"WHERE customer_ref IN ({id_list}) GROUP BY customer_ref;").splitlines():
        parts = row.split("\t")
        observed_orders[int(parts[0])] = parts[1:]
    observed_contracts: dict[int, list[str]] = {}
    for row in sql2("postgres", f"SELECT customer_id,COUNT(*),SUM(signed_amount) FROM fed_contracts "
                                f"WHERE customer_id IN ({id_list}) GROUP BY customer_id;").splitlines():
        parts = row.split("|")
        observed_contracts[int(parts[0])] = parts[1:]
    for entry in expected_view:
        cid = entry["customer_id"]
        got = observed_orders.get(cid)
        want = [str(entry["orders_90d"]), str(entry["sum_gross_cents"]), entry["sum_net_amount"],
                str(entry["unshipped_orders"])]
        if got is None or int(got[0]) != int(want[0]) or int(got[1]) != int(want[1]) or \
                _number(got[2]) != Decimal(want[2]) or int(got[3]) != int(want[3]):
            raise RuntimeError(f"customer {cid} order-side mismatch: {got} vs {want}")
        got_contracts = observed_contracts.get(cid)
        if got_contracts is None or int(got_contracts[0]) != entry["contract_count"] or \
                _number(got_contracts[1]) != Decimal(entry["sum_signed_amount"]):
            raise RuntimeError(f"customer {cid} contract-side mismatch: {got_contracts} vs "
                               f"[{entry['contract_count']}, {entry['sum_signed_amount']}]")

    report = {"orders": orders, "customers": customers, "contracts": contracts,
              "structural_counts": counts, "global_totals": totals,
              "formula_mismatches": mismatches,
              "sha256_canonical_ordered": {table: value[0] for table, value in digests.items()},
              "canonical_columns": {"fed_customers": CUSTOMER_COLUMNS, "fed_contracts": CONTRACT_COLUMNS,
                                    "fed_orders": ORDER_COLUMNS, "fed_shipments": SHIPMENT_COLUMNS},
              "customer_view_sample": expected_view,
              "canonical_semantics": ("per_order = GROUP BY order_id over fed_shipments (no fanout, "
                                      "duplicate waybills preserved); customer_view = fed_orders JOIN "
                                      "fed_customers ON customer_ref=customer_id LEFT JOIN per_order; "
                                      "unshipped = no shipment rows (status=PENDING); all order amounts "
                                      f"inside fixed window {WINDOW_START}..{WINDOW_END}"),
              "tier_status_is_extension_not_replacement": True,
              "wall_seconds": round(time.monotonic() - started, 3),
              "result": "source_oracle_pass_not_full_e2e", "full_e2e_pass": False}
    base.write_report(f"oracle-federated-{orders}.json", report, FED_ART)
    return report


# --------------------------------------------------------------------------
# canary + evidence
# --------------------------------------------------------------------------

def _note_condition(source: str, column: str, value: str | None) -> str:
    """Positive null-safe equality; the caller's outer NOT counts violations."""
    if value is None:
        return f"{column} IS NULL"
    if _is_pg(source):
        return f"{column} IS NOT DISTINCT FROM '{value}'"
    return f"({column} <=> '{value}')"  # MySQL has no IS DISTINCT FROM


def canary(orders: int) -> dict[str, Any]:
    """Re-verify bounded canary rows by value; counters alone cannot prove that
    no detail rows were rewritten or spilled somewhere unexpected."""
    customers, contracts = dimensions(orders)
    ids = canary_ids(orders)
    checks: dict[str, Any] = {}
    for c in ids["customers"]:
        row = customer_row(c)
        conditions = [f"external_ref={row['external_ref']}",
                      f"is_key {'IS TRUE' if row['is_key'] else 'IS FALSE'}",
                      f"tier='{row['tier']}'", f"region='{row['region']}'",
                      f"credit_limit={row['credit_limit']}",
                      _note_condition("postgres", "contact_note", row["contact_note"])]
        found = sql2("postgres", f"SELECT COUNT(*) FROM fed_customers "
                                 f"WHERE customer_id={c} AND NOT ({' AND '.join(conditions)});").strip()
        checks[f"customer:{c}"] = found == "0"
    for k in ids["contracts"]:
        row = contract_row(k, customers)
        conditions = [f"customer_id={row['customer_id']}", f"contract_no='{row['contract_no']}'",
                      f"valid_from='{row['valid_from']}'", f"valid_to='{row['valid_to']}'",
                      f"signed_amount={row['signed_amount']}", f"currency='{row['currency']}'",
                      _note_condition("postgres", "terms_note", row["terms_note"])]
        found = sql2("postgres", f"SELECT COUNT(*) FROM fed_contracts "
                                 f"WHERE contract_id={k} AND NOT ({' AND '.join(conditions)});").strip()
        checks[f"contract:{k}"] = found == "0"
    for o in ids["orders"]:
        row = order_row(o, customers, contracts)
        conditions = [f"customer_ref={row['customer_ref']}", f"contract_ref={row['contract_ref']}",
                      f"waybill_no='{row['waybill_no']}'", f"gross_cents={row['gross_cents']}",
                      f"net_amount={row['net_amount']}", f"status='{row['status']}'",
                      f"created_at='{row['created_at']}'",
                      _note_condition("mysql", "remark", row["remark"])]
        found = sql2("mysql", f"SELECT COUNT(*) FROM fed_orders "
                              f"WHERE order_id={o} AND NOT ({' AND '.join(conditions)});").strip()
        checks[f"order:{o}"] = found == "0"
        expected_ids = shipment_ids(o)
        observed = int(sql2("mysql", f"SELECT COUNT(*) FROM fed_shipments WHERE order_id={o};").strip())
        checks[f"order:{o}:shipment_rows"] = observed == len(expected_ids)
        for m in expected_ids:
            row_s = shipment_row(m, o)
            conditions = [f"waybill_no='{row_s['waybill_no']}'", f"ship_qty={row_s['ship_qty']}",
                          f"ship_cost={row_s['ship_cost']}", f"carrier='{row_s['carrier']}'",
                          _note_condition("mysql", "ship_note", row_s["ship_note"])]
            found = sql2("mysql", f"SELECT COUNT(*) FROM fed_shipments "
                                  f"WHERE shipment_id={m} AND NOT ({' AND '.join(conditions)});").strip()
            checks[f"shipment:{m}"] = found == "0"
    ok = all(checks.values())
    report = {"orders": orders, "checks": checks, "all_canaries_match": ok,
              "result": "canary_pass" if ok else "canary_mismatch",
              "note": "value-level re-verification of bounded canary rows; complements, not replaces, counters"}
    base.write_report(f"canary-federated-{orders}.json", report, FED_ART)
    return report


_SIZE_UNITS = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, "TiB": 1024**4,
               "kB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4}


def parse_size_bytes(text: str) -> int:
    match = re.fullmatch(r"\s*([\d.]+)\s*([A-Za-z]+)\s*", text)
    if not match or match.group(2) not in _SIZE_UNITS:
        raise ValueError(f"unrecognised docker size: {text!r}")
    return int(float(match.group(1)) * _SIZE_UNITS[match.group(2)])


def parse_net_io(text: str) -> tuple[int, int]:
    received, sent = text.split("/")
    return parse_size_bytes(received), parse_size_bytes(sent)


def parse_mem_usage(text: str) -> tuple[int, int]:
    used, limit = text.split("/")
    return parse_size_bytes(used), parse_size_bytes(limit)


def _docker_stats() -> dict[str, dict[str, Any]]:
    containers = base.owned_containers(FED_PROJECT)
    if not containers:
        return {}
    output = base.run(["docker", "stats", "--no-stream", "--format",
                       "{{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}\t{{.NetIO}}\t{{.BlockIO}}",
                       *[c["id"] for c in containers]])
    stats: dict[str, dict[str, Any]] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        name, mem, cpu, net, block = line.split("\t")
        used, limit = parse_mem_usage(mem)
        received, sent = parse_net_io(net)
        read, written = parse_net_io(block)
        stats[name.lstrip("/")] = {"mem_used_bytes": used, "mem_limit_bytes": limit,
                                   "cpu_percent": cpu.strip(), "net_rx_bytes": received,
                                   "net_tx_bytes": sent, "block_read_bytes": read,
                                   "block_write_bytes": written}
    return stats


def _spill_counters() -> dict[str, Any]:
    spill: dict[str, Any] = {}
    try:
        cid = require_fed("db")
        row = base.run(["docker", "exec", cid, "psql", "-X", "-q", "-A", "-t", "-U", "integrated_e2e",
                        "-d", "integrated_metadata", "-c",
                        "SELECT temp_files||'|'||temp_bytes FROM pg_stat_database "
                        "WHERE datname='integrated_metadata';"]).strip()
        files, bytes_ = row.split("|")
        spill["db"] = {"temp_files": int(files), "temp_bytes": int(bytes_)}
    except RuntimeError:
        spill["db"] = None
    try:
        cid = require_fed("source-pg")
        row = base.run(["docker", "exec", cid, "psql", "-X", "-q", "-A", "-t", "-U", "integrated_e2e",
                        "-d", "integrated_source", "-c",
                        "SELECT temp_files||'|'||temp_bytes FROM pg_stat_database "
                        "WHERE datname='integrated_source';"]).strip()
        files, bytes_ = row.split("|")
        spill["source-pg"] = {"temp_files": int(files), "temp_bytes": int(bytes_)}
    except RuntimeError:
        spill["source-pg"] = None
    try:
        cid = require_fed("mysql")
        output = base.run(["docker", "exec", "-e", "MYSQL_PWD=integrated_e2e_fixture", cid,
                           "mysql", "--batch", "--skip-column-names", "-u", "integrated_e2e",
                           "integrated_source", "-e",
                           "SHOW GLOBAL STATUS LIKE 'Created_tmp%';"])
        counters = {}
        for line in output.splitlines():
            name, value = line.split("\t")
            counters[name] = int(value)
        spill["mysql"] = counters
    except RuntimeError:
        spill["mysql"] = None
    return spill


def evidence_snapshot() -> dict[str, Any]:
    containers = base.owned_containers(FED_PROJECT)
    return {"observed_at_unix": time.time(), "project": FED_PROJECT,
            "containers": {c["name"]: {"service": c["service"], "running": c["running"],
                                       "oom_killed": c["oom_killed"], "health": c["health"],
                                       "memory_limit_bytes": c["memory_limit_bytes"]}
                           for c in containers},
            "stats": _docker_stats(), "spill": _spill_counters(),
            "note": "no host process is started by this module"}


def evidence_begin(phase: str) -> dict[str, Any]:
    if not phase or not all(ch.isalnum() or ch in "-_" for ch in phase):
        raise ValueError("phase must contain only letters, digits, dash and underscore")
    snapshot = evidence_snapshot()
    base.write_report(f"federated-evidence-{phase}-begin.json", snapshot, FED_ART)
    return snapshot


_LOG_PATTERN = re.compile(r"(temp(orary)? file|created tmp|tmp table|swap|out of memory|oom)", re.I)


def _log_scan_counts(since_unix: float) -> dict[str, dict[str, int]]:
    result = {}
    for container in base.owned_containers(FED_PROJECT):
        try:
            output = base.run(["docker", "logs", "--since", str(int(since_unix)), "--tail", "5000",
                               container["id"]])
        except RuntimeError:
            continue
        result[container["service"]] = {"lines_scanned": len(output.splitlines()),
                                        "pattern_matches": len(_LOG_PATTERN.findall(output))}
    return result


def budget_report(snapshot: dict[str, Any], earlier: dict[str, Any] | None) -> dict[str, Any]:
    stats_by_service = {}
    for name, stat in snapshot["stats"].items():
        service = snapshot["containers"].get(name, {}).get("service", name)
        stats_by_service[service] = stat

    def klass(budget: dict[str, int]) -> dict[str, Any]:
        entries = {}
        for service, limit_mib in budget.items():
            stat = stats_by_service.get(service)
            entries[service] = {"budget_bytes": limit_mib * 1024 * 1024,
                                "running": stat is not None,
                                "observed_mem_bytes": stat["mem_used_bytes"] if stat else None}
        return entries

    platform = klass(PLATFORM_BUDGET_MIB)
    source = klass(SOURCE_BUDGET_MIB)
    outside = {name: {"required": True, "running": None, "budget_bytes": None,
                      "evidence": "host process; PID/job must be recorded by the coordinated runner"}
               for name in REQUIRED_BASELINE_OUTSIDE_COMPOSE}
    platform_complete = all(entry["running"] for entry in platform.values())
    missing = [name for name in REQUIRED_BASELINE_OUTSIDE_COMPOSE if outside[name]["running"] is None]
    resident = base.run(["docker", "ps", "--format", "{{.Names}}"]).splitlines()
    other_projects = len([name for name in resident if not name.startswith("oo-fedanalysis")])
    deltas = {}
    if earlier:
        for name, stat in snapshot["stats"].items():
            before = earlier.get("stats", {}).get(name)
            if before:
                deltas[name] = {"net_rx_bytes": stat["net_rx_bytes"] - before["net_rx_bytes"],
                                "net_tx_bytes": stat["net_tx_bytes"] - before["net_tx_bytes"],
                                "block_read_bytes": stat["block_read_bytes"] - before["block_read_bytes"],
                                "block_write_bytes": stat["block_write_bytes"] - before["block_write_bytes"]}
    reasons = []
    if not platform_complete:
        reasons.append("platform baseline incomplete")
    if missing:
        reasons.append(f"required baseline missing: {','.join(missing)}")
    if other_projects:
        reasons.append("shared VM prevents platform budget verification")
    return {"platform_baseline": platform, "platform_budget_bytes": sum(PLATFORM_BUDGET_MIB.values()) * 1024 * 1024,
            "platform_baseline_complete": platform_complete and not missing,
            "required_baseline_outside_compose": outside,
            "required_baseline_missing": missing,
            "platform_budget_verifiable": other_projects == 0,
            "platform_budget_verifiability_note": (f"{other_projects} containers of other projects are resident in "
                                                   "the shared Docker VM; full-platform budget cannot be verified "
                                                   "while the VM is shared"),
            "source_appliances": source, "source_budget_bytes": sum(SOURCE_BUDGET_MIB.values()) * 1024 * 1024,
            "container_io_deltas": deltas,
            "verdict": {"cannot_claim_pass_reasons": reasons}}


def evidence_end(phase: str, orders: int | None) -> dict[str, Any]:
    begin_path = FED_ART / f"federated-evidence-{phase}-begin.json"
    if not begin_path.exists():
        raise RuntimeError(f"no begin snapshot for phase {phase!r}")
    begin = json.loads(begin_path.read_text(encoding="utf-8"))
    snapshot = evidence_snapshot()
    spill_deltas = {}
    for key in ("db", "source-pg"):
        before = (begin.get("spill") or {}).get(key)
        after = snapshot["spill"].get(key)
        if before and after:
            spill_deltas[key] = {name: after[name] - before[name] for name in before}
    mysql_before = (begin.get("spill") or {}).get("mysql")
    mysql_after = snapshot["spill"].get("mysql")
    if mysql_before and mysql_after:
        spill_deltas["mysql"] = {name: mysql_after.get(name, 0) - mysql_before.get(name, 0)
                                 for name in mysql_before}
    report = {"phase": phase, "begin_unix": begin["observed_at_unix"], "end_unix": snapshot["observed_at_unix"],
              "wall_seconds": round(snapshot["observed_at_unix"] - begin["observed_at_unix"], 3),
              "end_snapshot": snapshot, "spill": {
                  "platform": {"db": spill_deltas.get("db")},
                  "source": {"source-pg": spill_deltas.get("source-pg"), "mysql": spill_deltas.get("mysql"),
                             "allowed": True, "listed_separately": True},
                  "counter_limitation": ("pg temp_files/temp_bytes and MySQL Created_tmp counters do "
                                         "not see TEMP-table writes, WAL/redo or row logging; they are "
                                         "signals, not proof of zero detail spill")},
              "log_scan": _log_scan_counts(begin["observed_at_unix"]),
              "budget": budget_report(snapshot, begin)}
    if orders is not None:
        report["canary"] = {"all_canaries_match": canary(orders)["all_canaries_match"]}
    base.write_report(f"federated-evidence-{phase}-end.json", report, FED_ART)
    return report


# --------------------------------------------------------------------------
# stack lifecycle wrappers (still ownership-checked; execution needs a slot)
# --------------------------------------------------------------------------

def fed_preflight(services: list[str]) -> dict[str, Any]:
    return base.preflight(services, project=FED_PROJECT, ports=FED_PORTS, images=FED_IMAGES, directory=FED_ART)


def fed_up(services: list[str]) -> dict[str, Any]:
    profiles = ("mysql", "dbx") if "dbx" in services else ("mysql",)
    return base.stack_up(services, project=FED_PROJECT, ports=FED_PORTS, images=FED_IMAGES,
                         compose_file=FED_COMPOSE, profiles=profiles, state_path=FED_STATE, directory=FED_ART)


def fed_down(destroy_data: bool) -> dict[str, Any]:
    return base.stack_down(destroy_data, project=FED_PROJECT, state_path=FED_STATE,
                           directory=FED_ART, profiles=("mysql", "dbx"))


def fed_status() -> dict[str, Any]:
    return base.update_state(FED_PROJECT, FED_ART)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "up"):
        entry = sub.add_parser(name)
        entry.add_argument("services", nargs="+", choices=sorted(FED_PORTS))
    down = sub.add_parser("down")
    down.add_argument("--destroy-data", action="store_true")
    sub.add_parser("status")
    sub.add_parser("schema")
    seeding = sub.add_parser("seed")
    seeding.add_argument("--orders", type=int, required=True)
    seeding.add_argument("--batch-size", type=int, default=1000)
    seeding.add_argument("--extend", action="store_true",
                         help="verify the existing deterministic prefix, then grow it")
    oracling = sub.add_parser("oracle")
    oracling.add_argument("--orders", type=int, required=True)
    oracling.add_argument("--customers", default="", help="comma-separated customer ids for the view sample")
    oracling.add_argument("--sample", type=int, default=50, help="add the first N customer ids to the sample")
    canary_parser = sub.add_parser("canary")
    canary_parser.add_argument("--orders", type=int, required=True)
    evidence = sub.add_parser("evidence")
    evidence.add_argument("action", choices=["begin", "end"])
    evidence.add_argument("--phase", required=True)
    evidence.add_argument("--orders", type=int, default=None, help="run value canaries at evidence end")
    args = parser.parse_args()
    try:
        if args.command == "preflight":
            result = fed_preflight(args.services)
        elif args.command == "up":
            result = fed_up(args.services)
        elif args.command == "down":
            result = fed_down(args.destroy_data)
        elif args.command == "status":
            result = fed_status()
        elif args.command == "schema":
            result = create_schema()
        elif args.command == "seed":
            result = seed(args.orders, args.batch_size, args.extend)
        elif args.command == "oracle":
            ids = [int(value) for value in args.customers.split(",") if value.strip()]
            result = oracle(args.orders, ids, args.sample)
        elif args.command == "canary":
            result = canary(args.orders)
        elif args.action == "begin":
            result = evidence_begin(args.phase)
        else:
            result = evidence_end(args.phase, args.orders)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 2 if result.get("result") == "blocked" else 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"federated-fixture: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
