#!/usr/bin/env python3
"""A-28 acceptance driver: real LLM through the super assistant.

Goal doc (docs/development/ontology-network-goal.md sec 11 A-28): a real LLM
must complete the cross-3-domain natural-language query and one governed
integrated mutation through the super assistant front door; self-made typed
HTTP requests may never stand in for the assistant. Typed HTTP is used here
ONLY to prepare this driver's own goala28-* fixture chain through the version
front door (draft -> workspace -> mappings -> join assertions -> trial ->
impact -> promote); the query and the mutation themselves are performed by the
model via the assistant chat SSE + ontology tools + the user approval gate.

Honesty rules (goal doc sec 8):
- oracle values come from scripts/e2e/ontology_network_goal.py::expected_view
  (offline authority); money compares as exact decimal strings, NULL stays
  NULL and never becomes 0; a doubled contract total (join-then-aggregate
  fan-out) is an explicit failure, never an accepted alternative;
- the model must obtain every aggregate from ontology_query tool output; a
  turn without a successful ontology_query call cannot pass;
- oracle mismatch stays "fail" with the observed values recorded; the driver
  never coaches the model around a known runtime defect;
- secrets (DeepSeek key, external MySQL accounts) are read in-process via
  json.load from the private 0700 runtime dir; they never appear in argv,
  environment, dotenv, logs, or artifacts (transcripts are scrubbed).

Phases: plan (offline) / setup (fixture chain via HTTP front door) / smoke
(connectivity only, explicitly not A-28 evidence) / query (LLM north + detail)
/ mutate (LLM governed update s003 qty 5->6, verification, restore 6->5) /
report (acceptance mapping + ledger update).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO = BACKEND_DIR.parent
# driver runs as scripts/<file>.py: put backend/ on sys.path so the SPL plan
# proof can import the repository's own parser (app.* package) for AST grading
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
RUNTIME = REPO / ".artifacts" / "ontology-network-goal" / "a28"
RUNS = RUNTIME / "runs"
DEFAULT_API_BASE = "http://127.0.0.1:56004"
# Secrets dir is always passed explicitly (--secrets-dir) or via
# OO_GOAL_A28_SECRETS_DIR; a machine-specific path is never committed.

MODEL_NAME = "DeepSeek V4 Pro (goala28)"
DOMAINS = ("goala28-crm", "goala28-erp")
ONTOLOGIES = [
    {"key": "A", "name": "goala28-crm", "domain": "goala28-crm"},
    {"key": "B", "name": "goala28-orders", "domain": "goala28-erp"},
    {"key": "C", "name": "goala28-shipments", "domain": "goala28-erp"},
]
EXTERNAL_CONNECTION_NAME = "goala28-external-mysql"
# Historical auxiliary PG fixture created by the typed driver. It is NOT
# admissible as the A-28 manual+MySQL source mode: actual acceptance reuses
# A′ and must never re-run this setup or create/adopt assets.
LEGACY_PG_CONNECTION = "goal-e2e-source_pg"

# Stable object type ids ride inside the workspace snapshot (workspace_snapshot
# keeps submitted ids), so the action can reference its bound type in one PUT.
TYPE_IDS = {"Customer": "a28-ot-customer", "Contract": "a28-ot-contract",
            "Order": "a28-ot-order", "Shipment": "a28-ot-shipment"}
ACTION_ID = "a28-act-update-qty"

# Domain A rides the existing local-PG fixture assets (goal-e2e-* created by
# the typed driver; adopted by listing, never mutated). Domains B/C map to the
# dedicated external MySQL schema (task fixture, 16 rows, rw/ro accounts).
# fieldMapping direction is dataset column -> object property (remote_binding).
OBJECT_MODEL: dict[str, dict[str, Any]] = {
    "A": {"connection": LEGACY_PG_CONNECTION, "schema": "public",
          "legacy_diagnostic": True,
          "asset_prefix": "goal-e2e-",
          "types": {
              "Customer": {"table": "goal_customers", "pk": "customer_id",
                           "properties": {"customer_id": "string",
                                          "region": "string",
                                          "big_ref": "number",
                                          "note": "string"}},
              "Contract": {"table": "goal_contracts", "pk": "contract_id",
                           "properties": {"contract_id": "number",
                                          "customer_id": "string",
                                          "amount": "number",
                                          "contract_no": "string"}}}},
    "B": {"connection": EXTERNAL_CONNECTION_NAME, "schema": None,  # set at setup
          "asset_prefix": "goala28-ext-",
          "types": {
              "Order": {"table": "goal_orders", "pk": "order_id",
                        "properties": {"order_id": "string",
                                       "customer_id": "string",
                                       "amount": "number"}}}},
    "C": {"connection": EXTERNAL_CONNECTION_NAME, "schema": None,
          "asset_prefix": "goala28-ext-", "writable": True,
          "types": {
              "Shipment": {"table": "goal_shipments", "pk": "shipment_id",
                           "properties": {"shipment_id": "string",
                                          "order_id": "string",
                                          "qty": "number"},
                           # external column is `quantity`; object property qty
                           "column_map": {"shipment_id": "shipment_id",
                                          "order_id": "order_id",
                                          "quantity": "qty"}}}},
}

JOIN_ASSERTIONS = [
    ("A", "Customer", "customer_id", "A", "Contract", "customer_id", "string",
     "goala28 客户持有合同（本体A内部）"),
    ("A", "Customer", "customer_id", "B", "Order", "customer_id", "string",
     "goala28 客户下订单（跨本体 A-B）"),
    ("B", "Order", "order_id", "C", "Shipment", "order_id", "string",
     "goala28 订单发运明细（跨本体 B-C）"),
]

# Wire value normal forms. NULL tokens map to None; money keeps its string.
NULL_TOKENS = frozenset({None, "NULL", "null", "<NULL>", ""})

NORTH_KEYS = ("north_contract_sum", "north_order_sum", "north_ship_qty",
              "north_contract_count", "north_order_count")
DETAIL_KEYS = ("customer_id", "region", "contract_sum", "contract_count",
               "order_sum", "order_count", "ship_qty")

_SECRET_KEY_RE = re.compile(r"password|api[_-]?key|secret|authorization|token",
                            re.IGNORECASE)


# --------------------------------------------------------------------------
# offline oracle


def load_fixture_oracle() -> dict[str, Any]:
    path = REPO / "scripts" / "e2e" / "ontology_network_goal.py"
    spec = importlib.util.spec_from_file_location("ontology_network_goal_fixture",
                                                  path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.expected_view()


def north_expected(oracle: dict[str, Any]) -> dict[str, str]:
    north = oracle["regions"]["north"]
    return {"north_contract_sum": north["contract_sum"],
            "north_order_sum": north["order_sum"],
            "north_ship_qty": north["ship_qty"],
            "north_contract_count": str(north["contract_count"]),
            "north_order_count": str(north["order_count"])}


def detail_expected(oracle: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {cid: {"region": row["region"],
                  "contract_sum": row["contract_sum"],
                  "contract_count": str(row["contract_count"]),
                  "order_sum": row["order_sum"],
                  "order_count": str(row["order_count"]),
                  "ship_qty": row["ship_qty"]}
            for cid, row in oracle["customers"].items()}


def normalize(value: Any) -> Any:
    if isinstance(value, str):
        token = value.strip()
        return None if token in NULL_TOKENS else token
    if value is None:
        return None
    return str(value)


def _money_problem(field: str, expected: Any, observed: Any) -> str | None:
    """Strict money equality: NULL tokens kept; otherwise byte-exact strings.

    Goal doc sec 8 requires money to compare as exact strings. Numeric
    equivalence is NOT equality: "157.75", "157.750", " 157.7500",
    "157.7500 ", "1.5775E+2" and JSON numbers must all fail against the
    oracle's "157.7500". ``normalize()``'s strip is intentionally bypassed
    for non-NULL money values.
    """
    want_null = expected is None or (isinstance(expected, str)
                                     and expected in NULL_TOKENS)
    got_null = observed is None or (isinstance(observed, str)
                                    and observed in NULL_TOKENS)
    if want_null or got_null:
        if want_null and got_null:
            return None
        return f"{field}: expected {expected!r}, got {observed!r} (NULL)"
    if not isinstance(expected, str):
        return (f"{field}: oracle money must be a string, got "
                f"{type(expected).__name__} {expected!r}")
    if not isinstance(observed, str):
        return (f"{field}: money must stay an exact decimal string on the "
                f"wire, got {type(observed).__name__} {observed!r}")
    if observed != expected:
        return f"{field}: {observed!r} != {expected!r} (exact string)"
    return None


def compare_cell(field: str, expected: str, observed: Any) -> str | None:
    """Exact compare with wire-honesty typing.

    - money (`*_sum`): NULL tokens unchanged; non-NULL values must equal the
      oracle's exact decimal STRING (no strip, no numeric equivalence, no
      scientific notation; goal doc sec 8: 金额按字符串精确比较, 禁浮点);
    - counters: must be integral; 1.9 never truncates into 1;
    - NULL stays distinct from 0.
    """
    if field.endswith("_sum"):
        return _money_problem(field, expected, observed)
    want = normalize(expected)
    got = normalize(observed)
    if want is None or got is None:
        if want is None and got is None:
            return None
        return f"{field}: expected {want!r}, got {got!r}"
    # counters: integral values only, never truncated
    if isinstance(observed, bool):
        return f"{field}: boolean is not a counter ({observed!r})"
    try:
        decimal_value = Decimal(got)
    except Exception as error:  # noqa: BLE001
        return f"{field}: non-numeric value {got!r} ({error})"
    if decimal_value != decimal_value.to_integral_value():
        return (f"{field}: counter must be integral, got {got!r} "
                "(truncation never passes)")
    try:
        return None if int(Decimal(want)) == int(decimal_value) else \
            f"{field}: {got!r} != {want!r} (int)"
    except Exception as error:  # noqa: BLE001
        return f"{field}: non-numeric expected {want!r} ({error})"


def check_fanout(observed: dict[str, Any]) -> list[str]:
    """The doubled (join-then-aggregate) contract total must never pass."""
    doubled = Decimal("301.5000")
    value = normalize(observed.get("north_contract_sum"))
    if value is None:
        return []
    try:
        if Decimal(value) == doubled:
            return ["north_contract_sum == 301.5000: contract amounts doubled "
                    "(join-then-aggregate fan-out; goal doc sec 8 forbids)"]
    except Exception:  # noqa: BLE001
        pass
    return []


# --------------------------------------------------------------------------
# plan proof: the assistant's OWN ontology_query arguments must carry the
# >=3 domains / >=4 tables / >=2 aggregation stages shape (A-02 hard floor);
# a successful-but-unrelated query plus model arithmetic never passes A-28.


def _walk_sources(sources: Any, *, depth: int,
                  leaf_types: set, ontologies: set, stages: list) -> int:
    """Returns aggregate-stage count contributed by this level."""
    if depth > 4 or not isinstance(sources, list):
        return 0
    stage_here = 0
    for source in sources:
        if not isinstance(source, dict):
            continue
        subquery = source.get("subquery")
        if isinstance(subquery, dict):
            inner = _walk_sources(subquery.get("sources"), depth=depth + 1,
                                  leaf_types=leaf_types, ontologies=ontologies,
                                  stages=stages)
            has_aggregation = bool(subquery.get("groupBy") or subquery.get("aggregates"))
            stages.append({"depth": depth + 1, "aggregates": has_aggregation})
            stage_here += (1 if has_aggregation else 0) + inner
        elif source.get("ontologyId") and source.get("objectTypeId"):
            ontologies.add(str(source["ontologyId"]))
            leaf_types.add((str(source["ontologyId"]), str(source["objectTypeId"])))
    return stage_here


def plan_proof(arguments: Any) -> dict[str, Any]:
    """Grade one ontology_query argument set against the A-02 floor.

    A-28 only accepts a real, non-empty SPL text plan (``mode="spl_text"``).
    The typed sources tree is retained for diagnostics but can never qualify:
    ``ok`` is always False there, with ``diagnostic_ok`` carrying the old
    shape verdict so the report can explain the gap without passing it.
    """
    if not isinstance(arguments, dict):
        return {"ok": False, "diagnostic_ok": False, "mode": "invalid",
                "reason": "arguments missing/unparsable",
                "domains": 0, "leaf_types": 0, "stages": 0}
    spl = arguments.get("spl")
    if isinstance(spl, str) and spl.strip():
        bindings_raw = arguments.get("semanticBindings")
        if not isinstance(bindings_raw, dict):
            bindings_raw = arguments.get("bindings")  # 过渡期兼容
        return _spl_plan_proof(spl, bindings_raw)
    ontologies: set = set()
    leaf_types: set = set()
    stage_marks: list = []
    derived_stages = _walk_sources(arguments.get("sources"), depth=0,
                                   leaf_types=leaf_types, ontologies=ontologies,
                                   stages=stage_marks)
    top_aggregates = bool(arguments.get("groupBy") or arguments.get("aggregates"))
    stages = (1 if top_aggregates else 0) + derived_stages
    joins = arguments.get("joins")
    join_count = len(joins) if isinstance(joins, list) else 0
    diagnostic_ok = (len(ontologies) >= 3 and len(leaf_types) >= 4
                     and stages >= 2 and join_count >= 1)
    return {"ok": False, "diagnostic_ok": diagnostic_ok, "mode": "typed_shape",
            "domains": len(ontologies), "leaf_types": len(leaf_types),
            "stages": stages, "join_count": join_count,
            "reason": "typed shape is diagnostic only; A-28 requires a real "
                      "non-empty SPL text plan (mode=spl_text)"}


def _leaves_connected(names: list[str],
                      edges: frozenset[frozenset[str]]) -> bool:
    """Minimal connectivity over AST-derived leaf edges (no new parser)."""
    if len(names) <= 1:
        return True
    adjacency: dict[str, set[str]] = {name: set() for name in names}
    for edge in edges:
        pair = list(edge)
        if len(pair) != 2:
            continue
        left, right = pair
        if left in adjacency and right in adjacency:
            adjacency[left].add(right)
            adjacency[right].add(left)
    seen: set[str] = set()
    stack = [names[0]]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(adjacency.get(node, ()))
    return len(seen) == len(names)


def _spl_plan_proof(spl: str, bindings_raw: Any) -> dict[str, Any]:
    """Grade an SPL text plan against the A-02 floor using the real AST.

    Small-fixture strict, not a general engine: counts only REACHABLE leaf
    bindings (unused bindings never inflate domains/tables), requires each
    reachable leaf to carry a valid (ontologyId, releaseId, objectTypeId)
    triple, counts DISTINCT published triples, the longest cascaded
    aggregation chain (parallel stages cannot fake a cascade), joins that
    actually occur in the reachable plan, and minimal join-graph connectivity
    over the AST aliases.
    """
    try:
        from app.data_channel.query.parser import parse_spl_plan
    except ImportError as error:  # pragma: no cover - venv mismatch guard
        return {"ok": False, "reason": f"parser unavailable: {error}",
                "domains": 0, "leaf_types": 0, "stages": 0,
                "mode": "spl_text"}
    if not isinstance(bindings_raw, dict) or not bindings_raw:
        return {"ok": False, "reason": "semanticBindings missing/empty",
                "domains": 0, "leaf_types": 0, "stages": 0,
                "mode": "spl_text"}
    try:
        plan = parse_spl_plan(spl)
    except Exception as error:  # noqa: BLE001 - QueryError surfaces as reason
        return {"ok": False, "reason": f"spl parse failed: {error}",
                "domains": 0, "leaf_types": 0, "stages": 0,
                "mode": "spl_text"}
    bindings = bindings_raw
    stage_by_name = {stage.name: stage for stage in plan.stages}
    memo: dict[str, tuple[frozenset, int, int, frozenset]] = {}

    def binding_triple(name: str) -> tuple[str, str, str] | None:
        binding = bindings.get(name)
        if not isinstance(binding, dict):
            return None
        parts = tuple(binding.get(key) for key in
                      ("ontologyId", "releaseId", "objectTypeId"))
        if not all(isinstance(part, str) and part.strip() for part in parts):
            return None
        return parts  # type: ignore[return-value]

    def resolve(name: str) -> frozenset[str]:
        if name in bindings:
            return frozenset({name})
        if name in stage_by_name:
            return walk(stage_by_name[name].pipeline)[0]
        return frozenset()

    def walk(pipeline) -> tuple[frozenset[str], int, int, frozenset]:
        """(reachable leaves, joins, longest chain, leaf-edge pairs)."""
        aggregates = bool(pipeline.commands) and pipeline.commands[-1].op == "stats"
        leaves: set[str] = set()
        joins = sum(1 for command in pipeline.commands if command.op == "join")
        chain = 1 if aggregates else 0
        edges: set[frozenset[str]] = set()
        alias_leaves: dict[str, frozenset[str]] = {
            pipeline.alias: resolve(pipeline.source)}
        for command in pipeline.commands:
            if command.op == "join":
                alias_leaves[command.args[1]] = resolve(command.args[0])
        for command in pipeline.commands:
            if command.op != "join":
                continue
            left_alias = getattr(command.args[2], "alias", "")
            right_alias = getattr(command.args[3], "alias", "")
            left = alias_leaves.get(left_alias, frozenset())
            right = alias_leaves.get(right_alias, frozenset())
            for left_leaf in left:
                for right_leaf in right:
                    if left_leaf != right_leaf:
                        edges.add(frozenset((left_leaf, right_leaf)))
        for name, _alias in pipeline.sources:
            if name in bindings:
                leaves.add(name)
            elif name in stage_by_name:
                if name in memo:
                    sub_leaves, sub_joins, sub_chain, sub_edges = memo[name]
                else:
                    sub_leaves, sub_joins, sub_chain, sub_edges = walk(
                        stage_by_name[name].pipeline)
                    memo[name] = (frozenset(sub_leaves), sub_joins, sub_chain,
                                  frozenset(sub_edges))
                leaves |= sub_leaves
                joins += sub_joins
                chain = max(chain, (1 if aggregates else 0) + sub_chain)
                edges |= set(sub_edges)
        return frozenset(leaves), joins, chain, frozenset(edges)

    leaves, joins, chain, edges = walk(plan.pipeline)
    invalid = sorted(name for name in leaves if binding_triple(name) is None)
    triples = {binding_triple(name) for name in leaves
               if binding_triple(name) is not None}
    domains = {triple[0] for triple in triples}
    tables = len(triples)
    connected = (not invalid
                 and _leaves_connected(sorted(leaves), edges))
    ok = (not invalid and len(domains) >= 3 and tables >= 4 and chain >= 2
          and joins >= 1 and connected)
    if invalid:
        reason = ("invalid/missing reachable bindings: "
                  f"{invalid[:5]} (need ontologyId/releaseId/objectTypeId)")
    else:
        reason = (None if ok else
                  "SPL plan needs >=3 reachable domains/4 distinct published "
                  "triples/2 cascaded aggregation levels/1 join/connected "
                  f"leaves (got {len(domains)}/{tables}/{chain}/{joins}/"
                  f"connected={connected})")
    return {"ok": ok, "domains": len(domains),
            "leaf_types": tables, "stages": chain, "join_count": joins,
            "binding_triples": tables, "invalid_bindings": invalid,
            "connected": connected, "mode": "spl_text", "reason": reason}


def qualifying_query_calls(turn: "AssistantTurn") -> list[dict[str, Any]]:
    """Successful ontology_query calls with a REAL non-empty SPL text plan.

    Typed sources trees are diagnostic only: ``plan_proof`` marks them
    ``ok=False``/``mode="typed_shape"`` and they can never qualify A-28.
    """
    qualifying = []
    for entry in turn.tool_calls.values():
        if entry.get("toolName") != "ontology_query" \
                or entry.get("status") != "success":
            continue
        arguments = entry.get("arguments")
        proof = plan_proof(arguments)
        entry["plan_proof"] = proof
        if (proof.get("ok") and proof.get("mode") == "spl_text"
                and isinstance(arguments, dict)
                and isinstance(arguments.get("spl"), str)
                and arguments["spl"].strip()):
            qualifying.append(entry)
    return qualifying


def _decimal_literal_pattern(value: str) -> str | None:
    """Regex for any decimal spelling numerically equal to `value`.

    157.75 matches "157.7500" (trailing zeros) but never "157.7501";
    integral values never match inside longer numbers (7 vs 157.7500).
    """
    try:
        decimal_value = Decimal(value)
    except Exception:  # noqa: BLE001
        return None
    plain = format(decimal_value, "f")  # never scientific notation
    if "." in plain:
        base = plain.rstrip("0").rstrip(".") or "0"
    else:
        base = plain
    integral = decimal_value == decimal_value.to_integral_value()
    if "." in base:
        core = re.escape(base) + r"0*"          # 157.75 also matches 157.7500
    else:
        core = re.escape(base) + r"(?:\.0*)?"   # 40 also matches 40.0000
    tail = r"(?![\d.])" if integral else r"(?![\d])"
    return rf"(?<![\d.]){core}{tail}"


def _value_token_in_text(value: str, text: str) -> bool:
    """Token-boundary presence of a numeric literal in tool preview text."""
    pattern = _decimal_literal_pattern(value)
    if pattern is None:
        return str(value) in text
    return re.search(pattern, text) is not None


# ---------------------------------------------------------------------------
# row-level provenance: oracle cells must match a real tool-result ROW cell
# (exact alias, exact decimal/int/NULL). Token co-occurrence is not proof —
# 157.75/40/7 appearing anywhere in one preview says nothing about which
# region/customer/field they belong to.


_ROW_OBJECT_RE = re.compile(r"\{[^{}]*\}")


def extract_result_rows(text: str,
                        expected_aliases: set[str] | None = None
                        ) -> list[dict[str, Any]]:
    """Complete JSON row objects from a tool result (full text or preview).

    Full envelope parses authoritatively (rows / data.rows). Truncated
    previews may only SALVAGE balanced inner objects that carry at least one
    expected output alias — envelope metadata (releases/columns entries)
    must never be mistaken for result rows. Without expected_aliases the
    salvage path returns nothing (strict by default).
    """
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        rows: list[dict[str, Any]] = []
        if not expected_aliases:
            return rows
        for match in _ROW_OBJECT_RE.finditer(text):
            try:
                candidate = json.loads(match.group(0))
            except json.JSONDecodeError:
                continue
            if (isinstance(candidate, dict) and candidate
                    and set(candidate) & expected_aliases):
                rows.append(candidate)
        return rows
    if isinstance(payload, dict):
        inner = payload.get("rows")
        if isinstance(inner, list):
            return [row for row in inner if isinstance(row, dict)]
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("rows"), list):
            return [row for row in data["rows"] if isinstance(row, dict)]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def parse_persisted_result(text: str) -> tuple[list[dict[str, Any]], str | None]:
    """Strict acceptance parser for a persisted ToolRun result.

    Unlike preview salvage, this NEVER regex-scans broken/truncated text. The
    real envelope contract must be a JSON object with a rows array plus
    terminal completeness evidence: partial=False, truncated=False,
    hasNext=False, cursor present and null, rowCounting="complete", and
    pageRowCount/totalRowCount both equal to len(rows). Missing indicators are
    not evidence of completeness. Optional completeness/exact fields, when
    present, must also say complete/true.
    """
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as error:
        return [], f"persisted result is not valid JSON ({type(error).__name__})"
    if not isinstance(payload, dict):
        return [], "persisted result envelope is not a JSON object"
    rows = payload.get("rows")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return [], "persisted result has no complete rows array"
    if payload.get("partial") is not False:
        return [], "persisted result partial is not false"
    if payload.get("truncated") is not False:
        return [], "persisted result truncated is not false"
    if payload.get("hasNext") is not False:
        return [], "persisted result hasNext is not false"
    if "cursor" not in payload or payload.get("cursor") is not None:
        return [], "persisted result cursor is not terminal null"
    if payload.get("rowCounting") != "complete":
        return [], "persisted result rowCounting is not complete"
    if "completeness" in payload and payload.get("completeness") != "complete":
        return [], "persisted result completeness is not complete"
    if "exact" in payload and payload.get("exact") is not True:
        return [], "persisted result exact is not true"
    page_count = payload.get("pageRowCount")
    total_count = payload.get("totalRowCount")
    if type(page_count) is not int or page_count != len(rows):
        return [], "persisted result pageRowCount does not match rows"
    if type(total_count) is not int or total_count != len(rows):
        return [], "persisted result totalRowCount does not match complete rows"
    return [dict(row) for row in rows], None


def _cell_equal(alias: str, oracle_value: str, row_cell: Any) -> bool:
    """Exact per-cell equality: string-exact money/identity, integral counters."""
    if alias.endswith("_sum"):
        return _money_problem(alias, oracle_value, row_cell) is None
    if alias in {"customer_id", "region"}:
        return (isinstance(oracle_value, str) and isinstance(row_cell, str)
                and row_cell == oracle_value)
    want = normalize(oracle_value)
    got = normalize(row_cell)
    if want is None and got is None:
        return True
    if want is None or got is None:
        return False
    try:
        decimal_got = Decimal(got)
    except Exception:  # noqa: BLE001
        return False
    if decimal_got != decimal_got.to_integral_value():
        return False
    try:
        return int(Decimal(want)) == int(decimal_got)
    except Exception:  # noqa: BLE001
        return False


def match_oracle_row(expected_cells: dict[str, str],
                     rows: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    """A single tool row must carry EVERY oracle cell under the exact alias.

    Identity binding is by full-row signature (all aliases at once), never by
    trusting the model's labels or loose token co-occurrence.
    """
    if not rows:
        return False, ["no tool rows available"]
    problems: list[str] = []
    for index, row in enumerate(rows):
        cell_problems = [f"{alias}={row.get(alias)!r} != {value!r}"
                         for alias, value in expected_cells.items()
                         if not _cell_equal(alias, value, row.get(alias))]
        if not cell_problems:
            return True, []
        problems.append(f"row[{index}]: {'; '.join(cell_problems)}")
    return False, problems


def verify_oracle_against_tool_rows(
        expected_by_identity: dict[str, dict[str, str]],
        tool_rows_by_call: list[tuple[str, list[dict[str, Any]]]],
        ) -> dict[str, Any]:
    """Per-identity matching with NO cross-call pooling.

    Every expected identity must be covered by the same set of real rows from
    ONE qualifying call; all identities of the same main question must share
    that callId. The report records identity -> callId so stitched evidence is
    visible instead of silently averaged into a shared pool.
    """
    report: dict[str, Any] = {"matched": {}, "call_ids": {}, "problems": []}
    for identity, cells in expected_by_identity.items():
        matched_call = None
        attempts: list[str] = []
        for call_id, rows in tool_rows_by_call:
            ok, problems = match_oracle_row(cells, rows)
            if ok:
                matched_call = call_id
                break
            attempts.append(f"{call_id}: {problems[:2]}")
        report["matched"][identity] = matched_call
        if matched_call is not None:
            report["call_ids"][identity] = matched_call
        else:
            report["problems"].append(
                f"{identity}: no single call covers all oracle cells "
                f"({attempts[:3]})")
    call_ids = {call_id for call_id in report["matched"].values()
                if call_id is not None}
    if len(call_ids) > 1:
        report["problems"].append(
            f"oracle identities span multiple calls {sorted(call_ids)}; "
            "one qualifying call must cover all identities")
    return report


def fetch_persisted_tool_result(meta_db_url: str, conversation_id: str,
                                tool_run_id: str) -> str | None:
    """The platform's own stored tool result for this run (staging metadata).

    Read-only SELECT scoped to this driver's conversation; the value was
    written by the runtime at execution time, before any model synthesis.
    Grouped queries persist as shells by design (detail red line) — callers
    treat a shell (no rows) as unavailable, never as a pass.
    """
    import psycopg2

    with psycopg2.connect(meta_db_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT result FROM super_assistant_tool_runs "
                "WHERE id = %s AND conversation_id = %s", (tool_run_id,
                                                           conversation_id))
            row = cursor.fetchone()
    return row[0] if row and row[0] else None


# --------------------------------------------------------------------------
# helpers


def _payload(body: Any) -> Any:
    if isinstance(body, dict) and isinstance(body.get("data"), (dict, list)):
        return body["data"]
    return body


def scrub(value: Any) -> Any:
    """Recursively drop secret-looking values before writing artifacts."""
    if isinstance(value, dict):
        return {k: ("<redacted>" if _SECRET_KEY_RE.search(str(k)) else scrub(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def parse_final_json(text: str, required_keys: tuple[str, ...]) -> list[dict]:
    """All fenced JSON blocks containing every required key, in order."""
    blocks: list[dict] = []
    for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S):
        try:
            candidate = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and all(k in candidate for k in required_keys):
            blocks.append(candidate)
    return blocks


# --------------------------------------------------------------------------
# API client (loopback only; mirrors the typed driver's conventions)


class Api:
    def __init__(self, base_url: str, token: str | None = None,
                 timeout: float = 30.0):
        parsed = urlsplit(base_url)
        if (parsed.scheme != "http"
                or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError("api base must be an explicit loopback http URL")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.wire_bytes = 0

    def request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        payload = None if body is None else json.dumps(body).encode()
        headers = {"Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(self.base_url + path, data=payload,
                                         headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                self.wire_bytes += len(raw or b"") + len(payload or b"")
                return response.status, _decode(raw)
        except urllib.error.HTTPError as error:
            raw = error.read()
            self.wire_bytes += len(raw or b"") + len(payload or b"")
            return error.code, _decode(raw)


def _decode(raw: bytes) -> Any:
    text = raw.decode(errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text[:400]


# --------------------------------------------------------------------------
# secrets (in-process only)


def load_secrets(secrets_dir: Path) -> dict[str, Any]:
    resources = json.loads((secrets_dir / "resources.json").read_text())
    fixture = json.loads((secrets_dir / "external-fixture.json").read_text())
    mode = (secrets_dir / "resources.json").stat().st_mode & 0o777
    if mode != 0o600:
        raise RuntimeError(f"secrets dir not 0600 (mode {oct(mode)})")
    llm = resources["llm"]
    return {"llm": {"base_url": llm["base_url"], "api_key": llm["api_key"],
                    "model": llm["models"][0]},
            "external": {"host": fixture["host"], "port": fixture["port"],
                         "database": fixture["database"],
                         "rw": fixture["users"]["rw"],
                         "ro": fixture["users"]["ro"]}}


def mysql_read(secrets: dict[str, Any], sql: str,
               params: tuple = ()) -> tuple[Any, ...] | None:
    import pymysql  # backend venv dependency; never installed ad hoc

    connection = pymysql.connect(
        host=secrets["external"]["host"], port=secrets["external"]["port"],
        user=secrets["external"]["ro"]["name"],
        password=secrets["external"]["ro"]["password"],
        database=secrets["external"]["database"], connect_timeout=10,
        cursorclass=pymysql.cursors.Cursor)
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchone()
    finally:
        connection.close()


def shipment_qty(secrets: dict[str, Any], shipment_id: str) -> Any:
    row = mysql_read(secrets, "SELECT quantity FROM goal_shipments "
                              "WHERE shipment_id = %s", (shipment_id,))
    return None if row is None else row[0]


# --------------------------------------------------------------------------
# run state


class Run:
    def __init__(self, name: str):
        if not name or not all(ch.isalnum() or ch in "-_" for ch in name):
            raise ValueError("run name must be letters/digits/dash/underscore")
        RUNS.mkdir(parents=True, exist_ok=True)
        self.path = RUNS / f"{name}.json"
        self.state: dict[str, Any] = (
            json.loads(self.path.read_text(encoding="utf-8"))
            if self.path.exists() else
            {"name": name, "steps": {}, "a28_result": "not_run"})

    def save(self) -> None:
        self.path.write_text(json.dumps(scrub(self.state), ensure_ascii=False,
                                        indent=2, default=str) + "\n",
                             encoding="utf-8")

    def step(self, name: str) -> dict[str, Any]:
        return self.state["steps"].setdefault(name, {})

    def done(self, name: str) -> bool:
        return bool(self.step(name).get("ok"))

    def ontology_id(self, key: str) -> str:
        return self.state["ontologies"][key]


# --------------------------------------------------------------------------
# setup phase (fixture chain through the HTTP front door; no LLM)


def ensure_model_config(api: Api, secrets: dict[str, Any]) -> dict[str, Any]:
    _, listing = api.request("GET", "/api/v1/models")
    items = _payload(listing)
    items = items if isinstance(items, list) else items.get("items", [])
    existing = next((m for m in items if m.get("name") == MODEL_NAME), None)
    if existing and existing.get("enabled"):
        return {"model_config_id": existing["id"], "adoption": "already_enabled"}
    if existing:
        model_id = existing["id"]
        adoption = "reused_disabled_then_enabled"
    else:
        status, body = api.request(
            "POST", "/api/v1/models",
            {"name": MODEL_NAME, "config_type": "llm", "provider": "openai",
             "api_key": secrets["llm"]["api_key"],
             "api_base": secrets["llm"]["base_url"],
             "models": [secrets["llm"]["model"]],
             "enabled": False, "is_default": False})
        if status != 201:
            raise RuntimeError(f"model create: HTTP {status} {str(body)[:200]}")
        model_id = _payload(body)["id"]
        adoption = "created"
    status, body = api.request("POST", f"/api/v1/models/{model_id}/test")
    payload = _payload(body)
    ok = status == 200 and payload.get("success", payload.get("ok", True)) is not False
    if not ok:
        raise RuntimeError(f"model test failed: HTTP {status} "
                           f"{str(payload)[:200]}")
    status, body = api.request("POST", f"/api/v1/models/{model_id}/enabled",
                               {"enabled": True})
    if status not in (200, 201):
        raise RuntimeError(f"model enable: HTTP {status} {str(body)[:200]}")
    return {"model_config_id": model_id, "adoption": adoption}


def ensure_domains(api: Api) -> dict[str, str]:
    _, body = api.request("GET", "/api/v1/domains")
    existing = {item["name"] for item in (_payload(body) or [])}
    ensured: dict[str, str] = {}
    for name in DOMAINS:
        if name in existing:
            ensured[name] = "already_present"
            continue
        status, _body = api.request("POST", "/api/v1/domains",
                                    {"name": name,
                                     "description": f"a28 fixture {name}"})
        ensured[name] = ("created" if status == 201 else
                         "created_by_earlier_run" if status == 409 else
                         _raise(f"domain {name}: HTTP {status}"))
    return ensured


def _raise(message: str) -> str:
    raise RuntimeError(message)


def ensure_ontologies(api: Api, run: Run) -> dict[str, Any]:
    created: dict[str, Any] = {}
    for ontology in ONTOLOGIES:
        status, body = api.request("POST", "/api/v1/ontologies",
                                   {"name": ontology["name"],
                                    "domain": ontology["domain"]})
        detail = (body or {}).get("detail") or {}
        existing_id = detail.get("existing_id") if isinstance(detail, dict) else None
        if status in (200, 201):
            ontology_id = _payload(body)["id"]
            adoption = "created"
        elif status == 409 and existing_id and ontology["name"].startswith("goala28-"):
            ontology_id = existing_id
            adoption = "reused_from_earlier_a28_run"
        else:
            raise RuntimeError(f"ontology {ontology['key']}: HTTP {status} "
                               f"{str(body)[:200]}")
        _, versions = api.request(
            "GET", f"/api/v2/ontologies/{ontology_id}/versions")
        release = versions.get("current_release_id") if isinstance(versions, dict) else None
        created[ontology["key"]] = {"ontology_id": ontology_id, "adoption": adoption,
                                    "current_release_id": release,
                                    "v0_frozen": bool(release)}
    run.state["ontologies"] = {k: v["ontology_id"] for k, v in created.items()}
    return created


def ensure_connection(api: Api, secrets: dict[str, Any],
                      external_schema: str) -> dict[str, Any]:
    status, body = api.request(
        "POST", "/api/v2/connections",
        {"name": EXTERNAL_CONNECTION_NAME, "kind": "mysql",
         "config": {"host": secrets["external"]["host"],
                    "port": secrets["external"]["port"],
                    "database": secrets["external"]["database"],
                    "user": secrets["external"]["rw"]["name"],
                    "password": secrets["external"]["rw"]["password"],
                    "dbx_host": secrets["external"]["host"],
                    "dbx_port": secrets["external"]["port"]}})
    if status == 409:
        _, listing = api.request("GET", "/api/v2/connections")
        entry = next(item for item in _payload(listing)
                     if isinstance(item, dict)
                     and item.get("name") == EXTERNAL_CONNECTION_NAME)
        return {"id": entry["id"], "adoption": "reused"}
    if status not in (200, 201):
        raise RuntimeError(f"external connection: HTTP {status} {str(body)[:200]}")
    return {"id": _payload(body)["id"], "adoption": "created",
            "schema": external_schema}


def ensure_assets(api: Api, connection_id: str,
                  external_schema: str) -> dict[str, Any]:
    legacy = sorted(key for key, model in OBJECT_MODEL.items()
                    if model.get("connection") == LEGACY_PG_CONNECTION)
    if legacy:
        raise RuntimeError(
            "A-28 setup refused: legacy PG fixture "
            f"({LEGACY_PG_CONNECTION}) is a historical auxiliary source, not "
            f"admissible as manual+MySQL A-28 (domains {legacy}); reuse A′ and "
            "do not create or adopt assets")
    _, listing = api.request("GET", "/api/v2/integrated-datasets")
    existing = {item.get("name"): item
                for item in _payload(listing)["items"]}
    entries: dict[str, Any] = {}
    for domain_key, model in OBJECT_MODEL.items():
        for type_name, definition in model["types"].items():
            asset_name = f"{model['asset_prefix']}{definition['table']}"
            if asset_name in existing:
                item = existing[asset_name]
            else:
                status, body = api.request(
                    "POST", "/api/v2/integrated-datasets",
                    {"name": asset_name,
                     "connection_id": (connection_id
                                       if model["connection"] == EXTERNAL_CONNECTION_NAME
                                       else _find_connection(api, model["connection"])),
                     "schema_name": (external_schema
                                     if model["connection"] == EXTERNAL_CONNECTION_NAME
                                     else model["schema"]),
                     "table_name": definition["table"],
                     "writable": bool(model.get("writable", False))})
                if status == 409:
                    _, listing = api.request("GET", "/api/v2/integrated-datasets")
                    item = next(x for x in _payload(listing)["items"]
                                if x.get("name") == asset_name)
                elif status not in (200, 201):
                    raise RuntimeError(f"asset {asset_name}: HTTP {status} "
                                       f"{str(body)[:200]}")
                else:
                    item = _payload(body)
            entries[type_name] = {"asset_id": item["id"],
                                  "dataset_id": item["dataset_id"],
                                  "writable": item.get("writable")}
    return entries


def _find_connection(api: Api, name: str) -> str:
    _, listing = api.request("GET", "/api/v2/connections")
    entry = next(item for item in _payload(listing)
                 if isinstance(item, dict) and item.get("name") == name)
    return entry["id"]


def create_drafts(api: Api, run: Run) -> dict[str, Any]:
    drafts: dict[str, Any] = {}
    for ontology in ONTOLOGIES:
        key = ontology["key"]
        ontology_id = run.ontology_id(key)
        _, versions = api.request(
            "GET", f"/api/v2/ontologies/{ontology_id}/versions?limit=5")
        current_release = (versions.get("current_release_id")
                           if isinstance(versions, dict) else None)
        if not current_release:
            raise RuntimeError(f"no current release for {key}")
        _, body = api.request(
            "POST",
            f"/api/v2/ontologies/{ontology_id}/versions/{current_release}/drafts",
            {})
        payload = _payload(body)
        drafts[key] = {"version_id": payload.get("id") or payload.get("version_id"),
                       "source_version_id": current_release}
    return drafts


def _workspace_type(type_name: str, definition: dict[str, Any]) -> dict[str, Any]:
    return {"id": TYPE_IDS[type_name], "name": type_name,
            "displayName": type_name, "primaryKey": definition["pk"],
            "properties": [{"name": prop, "type": kind}
                           for prop, kind in definition["properties"].items()],
            "interfaces": [], "positionX": 100.0, "positionY": 100.0}


def save_workspaces(api: Api, run: Run, drafts: dict[str, Any]) -> dict[str, Any]:
    saved: dict[str, Any] = {}
    for domain_key, model in OBJECT_MODEL.items():
        ontology_id = run.ontology_id(domain_key)
        version = drafts[domain_key]["version_id"]
        actions: list[dict[str, Any]] = []
        if domain_key == "C":
            actions.append(_shipment_qty_action())
        body = {"objectTypes": [_workspace_type(name, definition)
                                for name, definition in model["types"].items()],
                "linkTypes": [], "actions": actions, "functions": [],
                "baseRevision": None}
        status, response = api.request(
            "PUT", f"/api/v2/ontologies/{ontology_id}/versions/{version}/workspace",
            body)
        if status not in (200, 201):
            raise RuntimeError(f"workspace save {domain_key}: HTTP {status} "
                               f"{str(response)[:300]}")
        _, current = api.request(
            "GET", f"/api/v2/ontologies/{ontology_id}/versions/{version}/workspace")
        types = {item["name"]: item["id"] for item in _payload(current)["objectTypes"]}
        if set(types) != set(model["types"]) or not all(types.values()):
            raise RuntimeError(f"workspace identity mismatch for {domain_key}")
        action_ids = {item["id"] for item in _payload(current).get("actions", [])}
        if domain_key == "C" and ACTION_ID not in action_ids:
            raise RuntimeError("update-qty action missing after save")
        saved[domain_key] = {"object_types": types,
                             "action_ids": sorted(action_ids)}
    return saved


def _shipment_qty_action() -> dict[str, Any]:
    return {"id": ACTION_ID, "name": "update-qty",
            "displayName": "更新发运数量",
            "description": "按发运单主键更新发运数量（integrated 受控修改）",
            "objectTypeId": TYPE_IDS["Shipment"],
            "parameters": [{"name": "qty", "displayName": "发运数量",
                            "type": "number"}],
            "rules": [{"id": "a28-rule-qty", "name": "set-qty",
                       "type": "update_property", "order": 1, "enabled": True,
                       "config": {"targetProperty": "qty",
                                  "valueSource": "parameter", "value": "qty"}}],
            "requiresApproval": False}


def save_mappings(api: Api, run: Run, drafts: dict[str, Any],
                  assets: dict[str, Any]) -> dict[str, Any]:
    mappings: dict[str, Any] = {}
    for domain_key, model in OBJECT_MODEL.items():
        ontology_id = run.ontology_id(domain_key)
        version = drafts[domain_key]["version_id"]
        entries = []
        for type_name, definition in model["types"].items():
            column_map = definition.get("column_map") or \
                {prop: prop for prop in definition["properties"]}
            entries.append({"id": f"a28-map-{TYPE_IDS[type_name]}",
                            "entityClass": type_name, "bindingMode": "remote",
                            "curatedDatasetId": assets[type_name]["dataset_id"],
                            "fieldMapping": column_map})
        status, _ = api.request(
            "PUT",
            f"/api/v2/ontologies/{ontology_id}/versions/{version}/workspace/mappings",
            {"mappings": entries, "linkMappings": [], "sentinels": [],
             "baseRevision": None})
        if status not in (200, 201):
            raise RuntimeError(f"mapping save {domain_key}: HTTP {status}")
        mappings[domain_key] = {"remote_mappings": [e["entityClass"] for e in entries]}
    return mappings


def semantic_canvas(domain_key: str, model: dict[str, Any]) -> dict[str, Any]:
    """Business canvas whose converter replay matches the workspace structure.

    Field names are snake_case: the gate's schema accepts both forms, but the
    exploration converter reads raw dicts with snake keys (key_attribute etc.)
    — a camelCase keyAttribute is silently ignored and the converter injects
    a synthetic `id` property, drifting the property-name signature.
    """
    objects = [{"name": type_name, "display_name": type_name,
                "description": f"goala28 fixture {type_name}",
                "key_attribute": definition["pk"],
                "attributes": [{"name": prop}
                               for prop in definition["properties"]]}
               for type_name, definition in model["types"].items()]
    behaviors: list[dict[str, Any]] = []
    if domain_key == "C":
        behaviors = [{"name": "update-qty", "display_name": "更新发运数量",
                      "object": "Shipment",
                      "inputs": [{"name": "qty", "type_hint": "number"}],
                      "outcome": "按主键更新发运数量"}]
    return {"objects": objects, "actors": [], "behaviors": behaviors,
            "events": [], "rules": [], "scenarios": [], "processes": []}


def save_semantic_layers(api: Api, run: Run,
                         drafts: dict[str, Any]) -> dict[str, Any]:
    saved: dict[str, Any] = {}
    for domain_key, model in OBJECT_MODEL.items():
        ontology_id = run.ontology_id(domain_key)
        version = drafts[domain_key]["version_id"]
        _, current = api.request(
            "GET", f"/api/v2/ontologies/{ontology_id}/versions/{version}/workspace")
        revision = _payload(current).get("revision")
        if not revision:
            raise RuntimeError(f"no revision token for {domain_key}")
        status, body = api.request(
            "PUT", f"/api/v2/ontologies/{ontology_id}/versions/{version}/semantic",
            {"baseRevision": revision,
             "canvas": semantic_canvas(domain_key, model),
             "documentMd": (f"# goala28 {domain_key} 业务语义\n\n"
                            f"{'、'.join(model['types'])} 的最小业务画布，"
                            "与结构工作区逐属性一致（A-28 fixture）。"),
             "documentTitle": f"goala28-{domain_key} 业务语义"})
        if status not in (200, 201):
            raise RuntimeError(f"semantic save {domain_key}: HTTP {status} "
                               f"{str(body)[:400]}")
        saved[domain_key] = {"ok": True}
    return saved


def register_join_assertions(api: Api, run: Run,
                             workspace: dict[str, Any]) -> dict[str, Any]:
    refs = []
    for (left_key, left_type, left_prop, right_key, right_type, right_prop,
         key_type, meaning) in JOIN_ASSERTIONS:
        left, right = run.ontology_id(left_key), run.ontology_id(right_key)
        body = {"leftOntologyId": left,
                "leftObjectTypeId": workspace[left_key]["object_types"][left_type],
                "leftProperty": left_prop, "rightOntologyId": right,
                "rightObjectTypeId": workspace[right_key]["object_types"][right_type],
                "rightProperty": right_prop, "keyType": key_type,
                "businessMeaning": meaning}
        status, response = api.request(
            "POST", f"/api/v2/formal/ontologies/{left}/agent/join-assertions",
            body)
        if status not in (200, 201):
            raise RuntimeError(f"join assertion {left_type}->{right_type}: "
                               f"HTTP {status} {str(response)[:200]}")
        refs.append(_payload(response)["evidenceRef"])
    return {"evidence_refs": refs, "count": len(refs)}


def promote_chain(api: Api, run: Run, drafts: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in ("A", "B", "C"):
        ontology_id = run.ontology_id(key)
        version = drafts[key]["version_id"]
        _, body = api.request(
            "POST",
            f"/api/v2/ontologies/{ontology_id}/versions/{version}/trial-runs", {})
        payload = _payload(body)
        if payload.get("status") != "passed" or not payload.get("id"):
            raise RuntimeError(f"trial did not pass for {key}: "
                               f"{payload.get('status')} {str(payload)[:200]}")
        trial_run_id, impact_hash = payload["id"], payload["impact_hash"]
        _, body = api.request(
            "GET",
            f"/api/v2/ontologies/{ontology_id}/versions/{version}/impact")
        impact_payload = _payload(body)
        if impact_payload.get("impactHash") != impact_hash:
            raise RuntimeError(f"impact hash drift for {key}")
        _, body = api.request(
            "POST",
            f"/api/v2/ontologies/{ontology_id}/versions/{version}/promote",
            {"trialRunId": trial_run_id, "impactHash": impact_hash})
        payload = _payload(body)
        release_id = payload.get("release_id") or payload.get("id")
        if not release_id:
            raise RuntimeError(f"promote returned no release id for {key}: "
                               f"{str(payload)[:200]}")
        result[key] = {"release_id": release_id, "trial_run_id": trial_run_id}
    return result


# join_assertions run AFTER promote: the assertion boundary resolves object
# types from the immutable current-release snapshot (agent_runtime/boundary),
# so draft-only types are invisible until the release exists. The semantic
# layer (business canvas) must be saved on the draft BEFORE trial: the
# semantic gate replays the canvas through the exploration converter and
# flags every structure element without a canvas counterpart
# (semantic_business_missing) at trial/publish time.
SETUP_STEPS = ("model_config", "domains", "ontologies", "connection_external",
               "assets", "drafts", "workspace", "mappings", "semantic",
               "promote", "join_assertions")


def phase_setup(args: argparse.Namespace) -> dict[str, Any]:
    legacy = sorted(key for key, model in OBJECT_MODEL.items()
                    if model.get("connection") == LEGACY_PG_CONNECTION)
    if legacy:
        return {"phase": "setup", "result": "blocked",
                "reason": ("A-28 setup refused: legacy PG fixture "
                           f"({LEGACY_PG_CONNECTION}) is a historical "
                           f"auxiliary source (domains {legacy}), not "
                           "admissible as manual+MySQL A-28; reuse A′ and do "
                           "not create/adopt assets or start setup"),
                "e2e_pass": False}
    secrets = load_secrets(Path(args.secrets_dir))
    api = Api(args.api_base)
    status, body = api.request("POST", "/api/v1/auth/login",
                               {"username": args.admin_user,
                                "password": args.admin_password})
    if status != 200:
        return {"phase": "setup", "result": "blocked",
                "reason": f"login HTTP {status}", "e2e_pass": False}
    api.token = _payload(body)["access_token"]
    run = Run(args.name)
    external_schema = secrets["external"]["database"]

    def step_model_config() -> None:
        run.step("model_config").update(ensure_model_config(api, secrets))

    def step_domains() -> None:
        run.step("domains").update({"domains": ensure_domains(api)})

    def step_ontologies() -> None:
        created = ensure_ontologies(api, run)
        run.step("ontologies").update({
            "created": created,
            "ok": all(v["v0_frozen"] for v in created.values())})

    def step_connection() -> None:
        run.step("connection_external").update(
            ensure_connection(api, secrets, external_schema))

    def step_assets() -> None:
        entries = ensure_assets(api, run.step("connection_external")["id"],
                                external_schema)
        run.step("assets").update({"assets": entries,
                                   "ok": len(entries) == 4
                                   and entries["Shipment"]["writable"]})

    def step_drafts() -> None:
        drafts = create_drafts(api, run)
        run.state["drafts"] = drafts
        run.step("drafts").update({"ok": all(v["version_id"] for v in drafts.values())})

    def step_workspace() -> None:
        workspace = save_workspaces(api, run, run.state["drafts"])
        run.state["workspace"] = workspace
        run.step("workspace").update({"ok": True})

    def step_mappings() -> None:
        mappings = save_mappings(api, run, run.state["drafts"],
                                 run.step("assets")["assets"])
        run.state["mappings"] = mappings
        run.step("mappings").update({"ok": True})

    def step_semantic() -> None:
        run.step("semantic").update(save_semantic_layers(api, run,
                                                         run.state["drafts"]))

    def step_join_assertions() -> None:
        assertions = register_join_assertions(api, run, run.state["workspace"])
        run.state["join_assertions"] = assertions
        run.step("join_assertions").update(
            {"ok": assertions["count"] == len(JOIN_ASSERTIONS)})

    def step_promote() -> None:
        releases = promote_chain(api, run, run.state["drafts"])
        run.state["releases"] = releases
        run.step("promote").update({"ok": True})

    executors = {"model_config": step_model_config, "domains": step_domains,
                 "ontologies": step_ontologies, "connection_external": step_connection,
                 "assets": step_assets, "drafts": step_drafts,
                 "workspace": step_workspace, "mappings": step_mappings,
                 "semantic": step_semantic,
                 "join_assertions": step_join_assertions, "promote": step_promote}
    failures: list[str] = []
    for name in SETUP_STEPS:
        if run.done(name):
            continue
        try:
            executors[name]()
        except Exception as error:  # noqa: BLE001 - record, never crash
            failures.append(f"{name}: {error}")
            break
        run.step(name)["ok"] = True
        run.save()
    setup_ok = not failures and all(run.done(n) for n in SETUP_STEPS)
    run.state["setup_result"] = "pass" if setup_ok else "fail"
    run.save()
    return {"phase": "setup", "result": run.state["setup_result"],
            "steps": {n: run.done(n) for n in SETUP_STEPS},
            "failures": failures, "e2e_pass": False,
            "note": "fixture chain only; A-28 evidence requires the LLM phases"}


# --------------------------------------------------------------------------
# smoke phase (connectivity only; explicitly NOT A-28 evidence)


def phase_smoke(args: argparse.Namespace) -> dict[str, Any]:
    secrets = load_secrets(Path(args.secrets_dir))
    api = Api(args.api_base)
    status, body = api.request("POST", "/api/v1/auth/login",
                               {"username": args.admin_user,
                                "password": args.admin_password})
    if status != 200:
        return {"phase": "smoke", "result": "blocked", "e2e_pass": False}
    api.token = _payload(body)["access_token"]
    run = Run(args.name)
    ontology_id = run.state.get("ontologies", {}).get("B")
    if not ontology_id:
        return {"phase": "smoke", "result": "blocked",
                "reason": "setup not complete (ontology B missing)", "e2e_pass": False}
    _, versions = api.request("GET", f"/api/v2/ontologies/{ontology_id}/versions")
    release = versions.get("current_release_id") if isinstance(versions, dict) else None
    if not release:
        return {"phase": "smoke", "result": "blocked",
                "reason": "no current release for B", "e2e_pass": False}
    # One typed remote read through the SAME unified kernel the assistant uses.
    # This is fixture connectivity verification only and is never counted as
    # A-28 evidence (the acceptance query must be authored by the LLM).
    status, body = api.request(
        "POST", "/api/v2/formal/agent/query",
        {"sources": [{"alias": "o", "ontologyId": ontology_id,
                      "releaseId": release,
                      "objectTypeId": TYPE_IDS["Order"]}],
         "select": [{"source": "o", "property": "order_id", "as": "order_id"},
                    {"source": "o", "property": "customer_id", "as": "customer_id"}],
         "orderBy": [{"output": "order_id", "direction": "asc"}], "pageSize": 10})
    payload = _payload(body)
    rows = payload.get("rows") if isinstance(payload, dict) else None
    direct = mysql_read(secrets, "SELECT COUNT(*) FROM goal_orders")
    ok = status == 200 and isinstance(rows, list) and len(rows) == 3 \
        and rows[0].get("order_id") == "o100"
    run.step("smoke").update({"ok": bool(ok), "status": status,
                              "rows": rows,
                              "source_count": direct[0] if direct else None,
                              "error": body if status != 200 else None,
                              "note": "connectivity only, not A-28 evidence"})
    run.save()
    return {"phase": "smoke", "result": "pass" if ok else "fail",
            "rows": rows, "source_count": direct[0] if direct else None,
            "e2e_pass": False}


# --------------------------------------------------------------------------
# assistant chat client (SSE + approval gate)


_HANDLE_ID_RE = re.compile(r'queryId\\?"\s*:\s*\\?"([0-9a-f]{32})')


def extract_query_ids(*texts: str) -> set[str]:
    """All query handle ids visible in captured texts (previews/steps/answers).

    Enumeration is limited to what the wire shows us; anything not visible is
    disclosed as un-enumerable, never claimed clean. TTL expiry is NOT
    cleanup evidence — callers must explicitly cancel via the front door.
    """
    ids: set[str] = set()
    for text in texts:
        if text:
            ids.update(_HANDLE_ID_RE.findall(text))
    return ids


def collect_row_evidence(turn: "AssistantTurn", expected_aliases: set[str],
                         *, conversation_id: str,
                         meta_db_url: str) -> list[tuple[str, list[dict]]]:
    """Acceptance row evidence: ONLY complete persisted ToolRun rows count.

    Preview text is salvaged for diagnostics (``preview_diagnostic`` and
    ``preview_row_count``) but is never returned to the verifier: an 800-char
    preview is not authoritative for A-28. A persisted shell with no rows is
    likewise unavailable, never a pass.
    """
    collected: list[tuple[str, list[dict]]] = []
    for tool_run_id, entry in turn.tool_calls.items():
        proof = entry.get("plan_proof") or {}
        if not proof.get("ok") or proof.get("mode") != "spl_text":
            continue
        persisted_rows: list[dict] = []
        if meta_db_url:
            persisted = fetch_persisted_tool_result(
                meta_db_url, conversation_id, tool_run_id)
            if persisted:
                persisted_rows, persisted_problem = parse_persisted_result(
                    persisted)
                if persisted_problem:
                    entry["persisted_reject_reason"] = persisted_problem
        if persisted_rows:
            entry["rows_source"] = "persisted_result"
            entry["row_count"] = len(persisted_rows)
            entry["preview_row_count"] = None
            collected.append((tool_run_id, persisted_rows))
            continue
        preview_rows = extract_result_rows(entry.get("preview") or "",
                                           expected_aliases)
        entry["rows_source"] = ("preview_diagnostic" if preview_rows else None)
        entry["row_count"] = 0
        entry["preview_row_count"] = len(preview_rows)
    return collected


def cancel_query_handles(api: "Api", ids: set[str]) -> dict[str, Any]:
    """Explicit front-door cancel per handle; response recorded verbatim.

    ``cancelled`` is the server's own boolean (true = this call actually
    freed a live handle; false = already absent/expired). HTTP 200 alone is
    NEVER reported as "cancelled" — counts use the server flag.
    """
    results: dict[str, Any] = {}
    truly_cancelled = 0
    for handle in sorted(ids):
        status, body = api.request("POST", "/api/v2/query/cancel",
                                   {"queryId": handle})
        payload = _payload(body) if isinstance(body, dict) else {}
        cancelled = payload.get("cancelled")
        if cancelled is True:
            truly_cancelled += 1
        results[handle] = {"http": status,
                           "cancelled": cancelled,
                           "response": payload}
    return {"cancelled": truly_cancelled,
            "absent_or_expired": sum(1 for r in results.values()
                                     if r["cancelled"] is False),
            "total": len(results), "results": results}


class AssistantTurn:
    """One chat turn: SSE reader thread + main-thread approval policy."""

    def __init__(self, api: Api, conversation_id: str, model_config_id: str):
        self.api = api
        self.conversation_id = conversation_id
        self.model_config_id = model_config_id
        self.events: list[dict[str, Any]] = []
        self.text = ""
        self.tool_calls: dict[str, dict[str, Any]] = {}
        self.confirmations: list[dict[str, Any]] = []
        self.finished = threading.Event()
        self.stream_error: str | None = None
        self.close_state: str | None = None
        self.query_ids: set[str] = set()
        self._decision_attempts: dict[str, int] = {}

    def turn(self, message: str, *, decide=None, total_timeout: float = 900.0,
             idle_timeout: float = 240.0) -> dict[str, Any]:
        """decide(card) -> "approve"|"deny" runs while the stream waits."""
        done_evt = threading.Event()
        holder: dict[str, Any] = {"response": None}

        def reader() -> None:
            payload = json.dumps({"message": message,
                                  "model_config_id": self.model_config_id,
                                  "agent_mode": False}).encode()
            request = urllib.request.Request(
                self.api.base_url +
                f"/api/v2/super-assistant/conversations/{self.conversation_id}/chat",
                data=payload,
                headers={"Content-Type": "application/json", "Accept": "text/event-stream",
                         "Authorization": f"Bearer {self.api.token}"},
                method="POST")
            try:
                response = urllib.request.urlopen(request, timeout=idle_timeout)
                holder["response"] = response
                event_name, data_lines = None, []
                for raw_line in response:
                    line = raw_line.decode(errors="replace").rstrip("\n\r")
                    if line == "":
                        if event_name is not None:
                            self._handle(event_name, "\n".join(data_lines))
                        event_name, data_lines = None, []
                        continue
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].strip())
                holder["close"] = "clean"
            except Exception as error:  # noqa: BLE001 - surfaced in result
                holder["close"] = f"error: {type(error).__name__}: {error}"
            finally:
                holder["response"] = None
            done_evt.set()
            self.finished.set()

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        deadline = time.monotonic() + total_timeout
        while not done_evt.wait(timeout=1.0):
            if time.monotonic() > deadline:
                self.stream_error = "total_timeout"
                break
            self._pump_decisions(decide)
        self.close_state = holder.get("close")
        response = holder.get("response")
        if self.stream_error and response is not None:
            try:  # deadline hit: close the actual response so the reader exits
                response.close()
            except Exception:  # noqa: BLE001
                pass
        thread.join(timeout=15.0)
        if thread.is_alive():
            self.stream_error = self.stream_error or "reader_thread_residual"
        self.close_state = holder.get("close")
        if not self.stream_error:
            # instant/short streams may deliver the confirmation card and close
            # before the polling loop ever pumped; decide once more post-join
            self._pump_decisions(decide)
        self._finalize()
        if self.stream_error and self.query_ids:
            # abnormal teardown: still cancel every visible handle explicitly
            # (TTL expiry is not cleanup evidence), best effort + recorded
            self.handle_cancel = cancel_query_handles(self.api, self.query_ids)
        return self.summary()

    def _handle(self, event_name: str, data_text: str) -> None:
        try:
            data = json.loads(data_text) if data_text else {}
        except json.JSONDecodeError:
            data = {"_raw": data_text[:400]}
        self.events.append({"event": event_name, "data": data})
        if event_name == "text_delta" and isinstance(data.get("delta"), str):
            self.text += data["delta"]
        elif event_name == "tool_start":
            self.tool_calls[data["toolRunId"]] = {
                "toolName": data.get("toolName"),
                "arguments": scrub(data.get("arguments") or {}),
                "status": "started"}
        elif event_name == "tool_result":
            entry = self.tool_calls.setdefault(data["toolRunId"], {"toolName": "?"})
            entry["status"] = data.get("status")
            entry["preview"] = (data.get("preview") or "")[:800]
        elif event_name == "tool_confirmation_required":
            self.confirmations.append(data)

    def _pump_decisions(self, decide) -> None:
        if decide is None:
            return
        for card in list(self.confirmations):
            tool_run_id = card.get("toolRunId")
            entry = self.tool_calls.setdefault(
                tool_run_id, {"toolName": card.get("toolName")})
            if entry.get("decision") is not None:
                continue  # already approved/denied through us
            if self._decision_attempts.get(tool_run_id, 0) >= 3:
                continue  # bounded retry; server approval timer decides the rest
            decision = decide(card) or "deny"
            if decision not in ("approve", "deny"):
                decision = "deny"
            self._decision_attempts[tool_run_id] = \
                self._decision_attempts.get(tool_run_id, 0) + 1
            entry["decision_attempts"] = self._decision_attempts[tool_run_id]
            status, body = self.api.request(
                "POST", f"/api/v2/super-assistant/tool-runs/{tool_run_id}/decision",
                {"decision": decision})
            entry["decision_last_http"] = status
            if status in (200, 201):
                entry["decision"] = decision
                self.confirmations.remove(card)
                entry["status"] = f"decision_{decision}"

    def _finalize(self) -> None:
        status, body = self.api.request(
            "GET",
            f"/api/v2/super-assistant/conversations/{self.conversation_id}/messages"
            "?limit=50")
        messages = _payload(body)
        messages = messages.get("messages", messages) if isinstance(messages, dict) \
            else messages
        assistants = [m for m in (messages or [])
                      if isinstance(m, dict) and m.get("role") == "assistant"]
        if assistants:
            self.persisted_final = assistants[-1].get("content") or ""
            self.persisted_steps = scrub(assistants[-1].get("steps") or [])
        # collect every handle id visible anywhere in this turn's evidence
        self.query_ids |= extract_query_ids(
            self.text,
            getattr(self, "persisted_final", "") or "",
            json.dumps(getattr(self, "persisted_steps", []), default=str),
            *[entry.get("preview") or "" for entry in self.tool_calls.values()])

    def summary(self) -> dict[str, Any]:
        confirmation_events = sum(1 for e in self.events
                                  if e["event"] == "tool_confirmation_required")
        return {"text": self.persisted_final if hasattr(self, "persisted_final")
                else self.text,
                "stream_text": self.text,
                "tool_calls": self.tool_calls,
                "confirmation_events": confirmation_events,
                "events": [{"event": e["event"]} for e in self.events],
                "event_counts": _event_counts(self.events),
                "persisted_steps": getattr(self, "persisted_steps", []),
                "stream_error": self.stream_error,
                "close_state": self.close_state,
                "query_ids": sorted(self.query_ids),
                "handle_cancel": getattr(self, "handle_cancel", None)}


def _event_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        counts[event["event"]] = counts.get(event["event"], 0) + 1
    return counts


def has_successful_query_call(turn: AssistantTurn) -> bool:
    """Deprecated gate kept for legibility: use qualifying_query_calls()."""
    return bool(qualifying_query_calls(turn))


def _assistant_api(args: argparse.Namespace) -> tuple[Api | None, dict[str, Any]]:
    api = Api(args.api_base)
    status, body = api.request("POST", "/api/v1/auth/login",
                               {"username": args.admin_user,
                                "password": args.admin_password})
    if status != 200:
        return None, {"reason": f"login HTTP {status}"}
    api.token = _payload(body)["access_token"]
    return api, {}


def _ensure_conversation(api: Api, run: Run, model_config_id: str,
                         title: str) -> str:
    conversation = run.state.get("conversation_id")
    if conversation:
        return conversation
    status, body = api.request("POST", "/api/v2/super-assistant/conversations",
                               {"title": title, "model_config_id": model_config_id})
    if status != 201:
        raise RuntimeError(f"conversation create: HTTP {status} {str(body)[:200]}")
    conversation = _payload(body)["id"]
    run.state["conversation_id"] = conversation
    run.save()
    return conversation


NORTH_PROMPT = (
    "请用本体工具完成一个跨本体统一查询并给出结果：统计 north 区域全部"
    "客户的合同总金额、订单总金额、发运总数量、合同行数与订单行数。"
    "硬性要求：1) 先用 ontology_catalog/ontology_structure 发现 goala28-* "
    "本体与结构；2) 必须用一次 ontology_query 的 SPL 文本形态完成（参数必须传 "
    "spl + semanticBindings 三元组绑定 + params + pageSize；语法：with 命名"
    "阶段 = from 源 as 别名 | 命令; ... 最终 from 阶段 as 别名 | 命令；每条"
    "管道以 fields 或 stats 收尾；阶段内先按订单/客户聚合再跨本体 join；"
    "join 键必须可追溯到发布属性且方向与已登记声明一致（customer_id 两端"
    "同为 string；order_id 两端同为 string）；字符串字面量用双引号；聚合"
    "写 sum(x.f) as 名；count() 计行；不要用 limit，分页用 pageSize）；"
    "不得退回结构树/typed 形态；若工具 schema 未暴露 spl，直接回答无法完成，"
    "不要用 typed 冒充；3) 最终阶段对 region=north "
    "过滤后直接输出五个聚合值、只返回一行（不要在最终阶段按 region 或 "
    "customer 分组），聚合输出别名必须恰好是 contract_sum、order_sum、"
    "ship_qty、contract_count、order_count，金额必须原样输出工具返回的"
    "字符串；4) 禁止自己口算、估算或拼接多次查询的结果；5) 回答必须以"
    "如下 JSON 代码块结尾（```json 围栏，键固定，值逐字来自该次查询工具"
    "返回的行，空值写 null，不要输出任何其他结尾文字）：\n"
    "```json\n"
    "{\"north_contract_sum\": \"...\", \"north_order_sum\": \"...\", "
    "\"north_ship_qty\": ..., \"north_contract_count\": ..., "
    "\"north_order_count\": ...}\n"
    "```")

DETAIL_PROMPT = (
    "继续用一次 ontology_query 的 SPL 文本形态核对三个客户的明细聚合："
    "同一调用按 customer_id 分组，一次返回 c1、c2、c3 三行；不得分三次调用，"
    "不得把多次查询结果拼接。输出别名必须恰好是 customer_id、region、"
    "contract_sum、contract_count、order_sum、order_count、ship_qty；注意某"
    "客户没有某类数据时该聚合值必须是 null 而不是 0，行数 count 仍按 0 计；"
    "金额原样字符串。禁止口算。回答必须以如下 JSON 数组代码块结尾"
    "（```json 围栏，按 c1、c2、c3 排序，值逐字来自该次查询工具返回的行，"
    "不要输出任何其他结尾文字）：\n"
    "```json\n"
    "[{\"customer_id\": \"c1\", \"region\": \"...\", \"contract_sum\": \"...\", "
    "\"contract_count\": ..., \"order_sum\": \"...\", \"order_count\": ..., "
    "\"ship_qty\": ...}, ...]\n"
    "```")


def phase_query(args: argparse.Namespace) -> dict[str, Any]:
    api, blocked = _assistant_api(args)
    if api is None:
        return {"phase": "query", "result": "blocked", **blocked, "e2e_pass": False}
    run = Run(args.name)
    model_config_id = run.step("model_config").get("model_config_id")
    if not model_config_id:
        return {"phase": "query", "result": "blocked",
                "reason": "setup not complete (model config missing)",
                "e2e_pass": False}
    conversation_id = _ensure_conversation(api, run, model_config_id,
                                           "goala28-a28-assistant")
    oracle = load_fixture_oracle()
    transcript_dir = RUNTIME / args.name
    transcript_dir.mkdir(parents=True, exist_ok=True)

    problems: list[str] = []
    meta_db_url = getattr(args, "meta_db_url", None) or ""
    if not meta_db_url:
        problems.append(
            "query: --meta-db-url not configured; preview-only ToolRun "
            "evidence is diagnostic and cannot pass A-28")

    def tool_rows_of(turn: "AssistantTurn",
                     expected_aliases: set[str]) -> list[tuple[str, list[dict]]]:
        return collect_row_evidence(turn, expected_aliases,
                                    conversation_id=conversation_id,
                                    meta_db_url=meta_db_url)

    # -- north: one comprehensive plan, values only from the tool ------------
    north_turn = AssistantTurn(api, conversation_id, model_config_id)
    north_turn.turn(NORTH_PROMPT, decide=_approval_policy(run))
    (transcript_dir / "north-turn.json").write_text(
        json.dumps(scrub(north_turn.summary()), ensure_ascii=False, indent=2,
                   default=str), encoding="utf-8")
    qualifying_north = qualifying_query_calls(north_turn)
    if not qualifying_north:
        proofs = [entry.get("plan_proof") for entry in north_turn.tool_calls.values()
                  if entry.get("toolName") == "ontology_query"]
        problems.append("north: no successful ontology_query call whose own "
                        f"arguments prove >=3 domains/4 tables/2 stages "
                        f"(query proofs: {proofs})")
    north_aliases = {"contract_sum", "order_sum", "ship_qty",
                     "contract_count", "order_count"}
    north_rows = tool_rows_of(north_turn, north_aliases)
    oracle_north = load_fixture_oracle()["regions"]["north"]
    row_report = verify_oracle_against_tool_rows(
        {"north": {"contract_sum": oracle_north["contract_sum"],
                   "order_sum": oracle_north["order_sum"],
                   "ship_qty": oracle_north["ship_qty"],
                   "contract_count": str(oracle_north["contract_count"]),
                   "order_count": str(oracle_north["order_count"])}},
        north_rows)
    for problem in row_report["problems"]:
        problems.append(f"north tool-row evidence: {problem}")
    blocks = parse_final_json(getattr(north_turn, "persisted_final",
                                      north_turn.text), NORTH_KEYS)
    observed: dict[str, Any] = {}
    if not blocks:
        problems.append("north: no parsable JSON block with all keys in the "
                        "final answer")
    else:
        observed = blocks[-1]
        expected = north_expected(load_fixture_oracle())
        for field, want in expected.items():
            problem = compare_cell(field, want, observed.get(field))
            if problem:
                problems.append(f"north: {problem}")
        problems.extend(f"north: {line}" for line in check_fanout(observed))
    north_summary = {"observed": observed,
                     "row_evidence": row_report,
                     "plan_proofs": [entry.get("plan_proof")
                                     for entry in north_turn.tool_calls.values()
                                     if entry.get("toolName") == "ontology_query"],
                     "tool_calls": north_turn.tool_calls,
                     "stream_error": north_turn.stream_error}
    run.step("query_north").update({"ok": not problems, "observed": observed,
                                    "problems": list(problems)})
    run.save()

    # -- detail: per-customer NULL/count honesty through per-customer rows ---
    detail_problems: list[str] = []
    detail_turn = AssistantTurn(api, conversation_id, model_config_id)
    detail_turn.turn(DETAIL_PROMPT, decide=_approval_policy(run))
    (transcript_dir / "detail-turn.json").write_text(
        json.dumps(scrub(detail_turn.summary()), ensure_ascii=False, indent=2,
                   default=str), encoding="utf-8")
    qualifying_detail = qualifying_query_calls(detail_turn)
    if not qualifying_detail:
        detail_problems.append("detail: no successful ontology_query call "
                               "proving the cross-ontology plan")
    detail_aliases = north_aliases | {"customer_id", "region"}
    detail_rows = tool_rows_of(detail_turn, detail_aliases)
    oracle_customers = load_fixture_oracle()["customers"]
    detail_identities = {
        cid: {"customer_id": cid, "region": row["region"],
              "contract_sum": row["contract_sum"],
              "contract_count": str(row["contract_count"]),
              "order_sum": row["order_sum"],
              "order_count": str(row["order_count"]),
              "ship_qty": row["ship_qty"]}
        for cid, row in oracle_customers.items()}
    detail_row_report = verify_oracle_against_tool_rows(
        detail_identities, detail_rows)
    for problem in detail_row_report["problems"]:
        detail_problems.append(f"detail tool-row evidence: {problem}")
    final_text = getattr(detail_turn, "persisted_final", detail_turn.text)
    rows = _detail_rows(final_text)
    if not rows:
        detail_problems.append("detail: no parsable per-customer JSON rows")
    else:
        expected_detail = detail_expected(load_fixture_oracle())
        observed_ids = {str(row.get("customer_id")) for row in rows}
        if observed_ids != set(expected_detail):
            detail_problems.append(f"detail: customer set {sorted(observed_ids)} "
                                   f"!= {sorted(expected_detail)}")
        for row in rows:
            cid = str(row.get("customer_id"))
            want = expected_detail.get(cid)
            if want is None:
                continue
            for field, expected_value in want.items():
                if field == "region":
                    if str(row.get(field)) != expected_value:
                        detail_problems.append(f"detail {cid}.region: "
                                               f"{row.get(field)} != {expected_value}")
                    continue
                problem = compare_cell(f"{cid}.{field}", expected_value,
                                       row.get(field))
                if problem:
                    detail_problems.append(f"detail: {problem}")
    run.step("query_detail").update({"ok": not detail_problems,
                                     "row_evidence": detail_row_report,
                                     "problems": list(detail_problems)})
    run.save()

    # finally: explicit front-door cancel of every handle this phase opened
    # (TTL expiry is not cleanup evidence; per-id results recorded)
    handles = north_turn.query_ids | detail_turn.query_ids
    cleanup = cancel_query_handles(api, handles)
    run.step("query_handle_cleanup").update(cleanup)
    run.save()

    result = "pass" if not problems and not detail_problems else "fail"
    run.state["query_result"] = result
    run.save()
    return {"phase": "query", "result": result,
            "north": north_summary, "detail_problems": detail_problems,
            "problems": problems, "handle_cleanup": cleanup, "e2e_pass": False,
            "note": "A-28 query half; mutation half required separately"}


def _detail_rows(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for match in re.finditer(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S):
        try:
            candidate = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, list):
            good = [r for r in candidate if isinstance(r, dict)
                    and all(k in r for k in DETAIL_KEYS)]
            if good:
                rows.extend(good)
    return rows


def _approval_policy(run: Run):
    """Query turns approve NOTHING: governance evidence (join assertions) is
    never auto-generated mid-conversation — it was registered by the driver
    owner at setup through the assertion front door. Execute cards are only
    ever approved by the strict mutation decider inside mutate legs."""

    def decide(card: dict[str, Any]) -> str:
        return "deny"

    return decide


def strict_mutation_decider(run: Run, expected_qty: int, expect_before: int,
                            secrets: dict[str, Any], turn: "AssistantTurn",
                            problems: list[str]):
    """The only approver for ontology_execute_action in mutate legs.

    Approve requires ALL of: prepared state; ontology == our C; action id ==
    our update-qty action; final parameter qty == expected; a captured
    ontology_prepare_action call in the same turn targeting shipment s003 on
    the Shipment type with the same qty; and the zero-source-change
    checkpoint (live external row still == expect_before) immediately before
    the decision is posted. Anything else denies with a recorded reason.
    """
    mine_c = run.state.get("ontologies", {}).get("C")

    def decide(card: dict[str, Any]) -> str:
        confirmation = card.get("ontologyConfirmation") or {}
        display = confirmation.get("display") or {}
        reasons: list[str] = []
        if confirmation.get("state") != "prepared":
            reasons.append(f"state={confirmation.get('state')!r}")
        if (display.get("ontology") or {}).get("id") != mine_c:
            reasons.append("ontology not goala28-C")
        if (display.get("action") or {}).get("id") != ACTION_ID:
            reasons.append(f"action id {(display.get('action') or {}).get('id')!r}"
                           f" != {ACTION_ID}")
        parameters = {str(item.get("name")): item.get("value")
                      for item in display.get("parameters") or []
                      if isinstance(item, dict)}
        if _as_int(parameters.get("qty")) != expected_qty:
            reasons.append(f"card qty {parameters.get('qty')!r} != {expected_qty}")
        prepare_ok = False
        for entry in turn.tool_calls.values():
            if entry.get("toolName") != "ontology_prepare_action":
                continue
            arguments = entry.get("arguments") or {}
            target_ref = arguments.get("targetRef") or {}
            key = target_ref.get("key") or {}
            if (target_ref.get("objectTypeId") == TYPE_IDS["Shipment"]
                    and key.get("shipment_id") == "s003"
                    and _as_int((arguments.get("parameters") or {}).get("qty"))
                    == expected_qty):
                prepare_ok = True
        if not prepare_ok:
            reasons.append("no matching ontology_prepare_action(s003, "
                           f"qty={expected_qty}) captured in this turn")
        gate_value: Any = None
        if not reasons:
            # approval window == zero-source-change checkpoint (live read)
            gate_value = shipment_qty(secrets, "s003")
            if gate_value != expect_before:
                reasons.append(f"source qty={gate_value!r} before approval, "
                               f"expected {expect_before} (prepare must be "
                               "zero-change)")
        if reasons:
            problems.append("gate denied: " + "; ".join(reasons))
            return "deny"
        turn.gate_source_value = gate_value  # evidence for the leg report
        return "approve"

    return decide


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        decimal_value = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None
    if decimal_value != decimal_value.to_integral_value():
        return None
    return int(decimal_value)


# --------------------------------------------------------------------------
# mutate phase (governed integrated update s003 qty 5 -> 6 -> 5)


def _mutation_prompt(target_qty: int, release_c: str = "") -> str:
    return (
        f"请把发运单 s003 的发运数量改为 {target_qty}。目标本体与动作（已发布，"
        f"经结构核实）：本体 goala28-erp-shipments（ID 8530ae0e-1fa8-4d17-b56c-"
        f"04d2f9af8507，当前 release {release_c or 'a411f2fe-84bf-414c-a905-20f3a2f24a8a'}），"
        "对象类型 Shipment（ID a28-ot-shipment，主键 shipment_id），动作 "
        f"a28-act-update-qty（参数 qty，数值 {target_qty}），目标行主键 "
        f"shipment_id = \"s003\"（完整主键定位）。流程：先用 ontology_prepare_action "
        "准备确认（targetRef 用该类型与主键），拿到 confirmationId 后在同一轮"
        "调用 ontology_execute_action 请求执行；执行需要用户确认，等待即可，"
        "我会批准。禁止直接改库、编造成功或在未获批时声称已执行。")


def _execute_nudge_prompt() -> str:
    return ("请继续：用 ontology_execute_action 提交你刚才准备好的确认"
            "（只传 confirmationId），触发用户确认即可，不要重新准备。")


def _region_qty_prompt() -> str:
    return (
        "再用一次 ontology_query 的 SPL 文本形态统计 north 与 south 两个区域"
        "的发运总数量：同一调用按 region 分组返回两行，输出别名必须恰好是 "
        "region、ship_qty；不得分两次调用或拼接结果。禁止口算。最终回答末尾"
        "给出 JSON 代码块，键为 north_ship_qty、south_ship_qty，值逐字来自该"
        "次查询工具返回的行，空值写 null。")


def _run_mutation_leg(api: Api, run: Run, conversation_id: str,
                      model_config_id: str, transcript_dir: Path, label: str,
                      target_qty: int, expect_before: int,
                      secrets: dict[str, Any]) -> dict[str, Any]:
    """One governed mutation leg: prompt -> prepare -> strict gate -> approve."""
    problems: list[str] = []
    turn = AssistantTurn(api, conversation_id, model_config_id)
    # fallback policy for anything that is not the execute gate (e.g. a join
    # assertion card) stays the conservative _approval_policy
    fallback = _approval_policy(run)
    strict = strict_mutation_decider(run, target_qty, expect_before,
                                     secrets, turn, problems)

    def decide(card: dict[str, Any]) -> str:
        if card.get("toolName") == "ontology_execute_action":
            return strict(card)
        return fallback(card)

    turn.turn(_mutation_prompt(target_qty, run.state.get("releases", {})
                               .get("C", {}).get("release_id", "")), decide=decide)
    if not turn.confirmations and not getattr(turn, "gate_source_value", None):
        prepared = [entry for entry in turn.tool_calls.values()
                    if entry.get("toolName") == "ontology_prepare_action"
                    and entry.get("status") == "success"]
        if prepared:
            # model prepared then stopped without requesting execution:
            # same conversation, explicit minimal execute request
            turn.turn(_execute_nudge_prompt(), decide=decide)
    (transcript_dir / f"{label}-turn.json").write_text(
        json.dumps(scrub(turn.summary()), ensure_ascii=False, indent=2,
                   default=str), encoding="utf-8")
    gate_source_value = getattr(turn, "gate_source_value", None)
    if gate_source_value is None:
        problems.append(f"{label}: approval gate never reached (no approvable "
                        "ontology_execute_action confirmation surfaced)")
    prepare_calls = [entry for entry in turn.tool_calls.values()
                     if entry.get("toolName") == "ontology_prepare_action"]
    if not prepare_calls:
        problems.append(f"{label}: model never called ontology_prepare_action")
    execute_calls = [entry for entry in turn.tool_calls.values()
                     if entry.get("toolName") == "ontology_execute_action"
                     and entry.get("status") == "success"]
    if not execute_calls:
        problems.append(f"{label}: ontology_execute_action did not succeed")
    after = shipment_qty(secrets, "s003")
    if after != target_qty:
        problems.append(f"{label}: source qty after execution = {after!r}, "
                        f"expected {target_qty}")
    return {"ok": not problems, "problems": problems,
            "gate_source_value": gate_source_value,
            "source_after": after,
            "prepare_call_count": len(prepare_calls),
            "execute_succeeded": bool(execute_calls),
            "query_ids": sorted(turn.query_ids)}


def phase_mutate(args: argparse.Namespace) -> dict[str, Any]:
    api, blocked = _assistant_api(args)
    if api is None:
        return {"phase": "mutate", "result": "blocked", **blocked, "e2e_pass": False}
    secrets = load_secrets(Path(args.secrets_dir))
    run = Run(args.name)
    model_config_id = run.step("model_config").get("model_config_id")
    conversation_id = run.state.get("conversation_id")
    if not model_config_id or not conversation_id:
        return {"phase": "mutate", "result": "blocked",
                "reason": "run query phase first (conversation/model missing)",
                "e2e_pass": False}
    # exclusive mutation conversation: the query conversation carries failed
    # multi-round context that derails the model; mutation gets a clean one
    mutation_conversation = run.state.get("mutation_conversation_id")
    if not mutation_conversation:
        status, body = api.request(
            "POST", "/api/v2/super-assistant/conversations",
            {"title": f"{run.state.get('name', 'a28')}-mutation",
             "model_config_id": model_config_id})
        if status != 201:
            return {"phase": "mutate", "result": "blocked",
                    "reason": f"mutation conversation create HTTP {status}",
                    "e2e_pass": False}
        mutation_conversation = _payload(body)["id"]
        run.state["mutation_conversation_id"] = mutation_conversation
        run.save()
    transcript_dir = RUNTIME / args.name
    transcript_dir.mkdir(parents=True, exist_ok=True)
    oracle = load_fixture_oracle()

    problems: list[str] = []
    before = shipment_qty(secrets, "s003")
    if before != 5:
        return {"phase": "mutate", "result": "blocked",
                "reason": f"fixture drift: s003 quantity is {before!r}, "
                          "expected 5 (refuse to mutate a moved fixture)",
                "e2e_pass": False}

    forward = _run_mutation_leg(api, run, mutation_conversation, model_config_id,
                                transcript_dir, "mutate-forward", 6, 5, secrets)
    problems.extend(forward["problems"])
    run.step("mutate_forward").update(forward)
    run.save()

    source_now = shipment_qty(secrets, "s003")
    if source_now != 6:
        # forward never executed (or failed): requery/restore against a 5
        # would be a hollow run — record, clean handles, stop honestly.
        problems.append(f"forward did not reach source=6 (source={source_now!r}); "
                        "requery/restore skipped")
        cleanup = cancel_query_handles(
            api, set(forward.get("query_ids") or []))
        run.step("mutate_handle_cleanup").update(cleanup)
        run.state["mutate_result"] = "fail"
        run.save()
        return {"phase": "mutate", "result": "fail",
                "forward": forward, "problems": problems,
                "handle_cleanup": cleanup, "e2e_pass": False}

    # Post-mutation unified re-query through the assistant: south 5->6,
    # north must stay 7 (c2 only contributes to south).
    requery_problems: list[str] = []
    requery_turn = AssistantTurn(api, mutation_conversation, model_config_id)
    requery_turn.turn(_region_qty_prompt(), decide=_approval_policy(run))
    (transcript_dir / "requery-turn.json").write_text(
        json.dumps(scrub(requery_turn.summary()), ensure_ascii=False, indent=2,
                   default=str), encoding="utf-8")
    qualifying_requery = qualifying_query_calls(requery_turn)
    if not qualifying_requery:
        requery_problems.append("requery: no successful ontology_query call "
                                "proving the cross-ontology plan")
    meta_db_url = getattr(args, "meta_db_url", None) or ""
    if not meta_db_url:
        requery_problems.append(
            "requery: --meta-db-url not configured; preview-only ToolRun "
            "evidence is diagnostic and cannot pass A-28")
    requery_rows = collect_row_evidence(requery_turn, {"region", "ship_qty"},
                                        conversation_id=mutation_conversation,
                                        meta_db_url=meta_db_url)
    # after s003 5->6: south ship_qty becomes 6, north stays 7; one call must
    # carry both region rows (no cross-call stitching)
    row_report = verify_oracle_against_tool_rows(
        {"north": {"region": "north", "ship_qty": "7"},
         "south": {"region": "south", "ship_qty": "6"}},
        requery_rows)
    for problem in row_report["problems"]:
        requery_problems.append(f"requery tool-row evidence: {problem}")
    blocks = parse_final_json(getattr(requery_turn, "persisted_final",
                                      requery_turn.text),
                              ("north_ship_qty", "south_ship_qty"))
    if not blocks:
        requery_problems.append("requery: no parsable JSON block")
    else:
        observed = blocks[-1]
        for field, want in (("north_ship_qty", "7"), ("south_ship_qty", "6")):
            problem = compare_cell(field, want, observed.get(field))
            if problem:
                requery_problems.append(f"requery: {problem}")
    run.step("mutate_requery").update({"ok": not requery_problems,
                                       "row_evidence": row_report,
                                       "problems": requery_problems})
    run.save()
    problems.extend(requery_problems)

    # Restore the shared fixture through the same governed path (6 -> 5).
    restore = _run_mutation_leg(api, run, mutation_conversation, model_config_id,
                                transcript_dir, "mutate-restore", 5, 6, secrets)
    problems.extend(restore["problems"])
    run.step("mutate_restore").update(restore)
    run.save()

    # finally: explicit front-door cancel of every handle opened by the
    # mutation/requery/restore turns (TTL expiry is not cleanup evidence)
    handles = set(forward.get("query_ids") or []) \
        | set(requery_turn.query_ids) | set(restore.get("query_ids") or [])
    cleanup = cancel_query_handles(api, handles)
    run.step("mutate_handle_cleanup").update(cleanup)
    run.save()

    result = "pass" if not problems else "fail"
    run.state["mutate_result"] = result
    run.save()
    return {"phase": "mutate", "result": result,
            "forward": forward, "requery_problems": requery_problems,
            "restore": restore, "problems": problems,
            "handle_cleanup": cleanup, "e2e_pass": False}


# --------------------------------------------------------------------------
# plan + report


def phase_plan(args: argparse.Namespace) -> dict[str, Any]:
    oracle = load_fixture_oracle()
    return {"phase": "plan", "result": "plan_ready", "e2e_pass": False,
            "acceptance_id": "A-28",
            "oracle": {"north": north_expected(oracle),
                       "detail": detail_expected(oracle)},
            "chain": {"ontologies": [o["name"] for o in ONTOLOGIES],
                      "mutation": "external goal_shipments s003 quantity "
                                  "5->6 (governed) then restore 6->5"},
            "source_mode_boundary": (
                "legacy PG A fixture (goal-e2e-source_pg) is historical and "
                "diagnostic-only; A-28 acceptance must reuse A′; setup and "
                "ensure_assets refuse the legacy PG mode and never create or "
                "adopt assets"),
            "note": "offline plan is not evidence; the LLM phases are"}


def phase_report(args: argparse.Namespace) -> dict[str, Any]:
    run = Run(args.name)
    steps = run.state.get("steps", {})
    subchecks = {
        "setup": run.state.get("setup_result", "not_run"),
        "north_query": ("pass" if steps.get("query_north", {}).get("ok")
                        else "not_run" if "query_north" not in steps else "fail"),
        "detail_query": ("pass" if steps.get("query_detail", {}).get("ok")
                         else "not_run" if "query_detail" not in steps else "fail"),
        "mutation_forward": ("pass" if steps.get("mutate_forward", {}).get("ok")
                             else "not_run" if "mutate_forward" not in steps
                             else "fail"),
        "mutation_requery": ("pass" if steps.get("mutate_requery", {}).get("ok")
                             else "not_run" if "mutate_requery" not in steps
                             else "fail"),
        "mutation_restore": ("pass" if steps.get("mutate_restore", {}).get("ok")
                             else "not_run" if "mutate_restore" not in steps
                             else "fail"),
    }
    a28 = ("pass" if all(v == "pass" for v in subchecks.values())
           else "blocked" if subchecks["setup"] == "not_run" else "fail")
    if subchecks["setup"] == "blocked":
        a28 = "blocked"
    run.state["a28_result"] = a28
    run.state["subchecks"] = subchecks
    run.save()
    return {"phase": "report", "a28": a28, "subchecks": subchecks,
            "run_file": str(run.path), "e2e_pass": a28 == "pass"}


# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan")
    for name in ("setup", "smoke", "query", "mutate", "report"):
        entry = sub.add_parser(name)
        entry.add_argument("--name", required=True)
        entry.add_argument("--api-base",
                           default=_env_default("ONTOLOGY_GOAL_A28_API_BASE",
                                                DEFAULT_API_BASE))
        entry.add_argument("--admin-user", default="goaladmin")
        entry.add_argument("--admin-password", default="goalnet-admin-fixture")
        entry.add_argument("--secrets-dir",
                           default=_env_default("OO_GOAL_A28_SECRETS_DIR", ""),
                           help="private 0700 dir holding resources.json and "
                                "external-fixture.json; never committed")
        entry.add_argument("--meta-db-url",
                           default=_env_default("OO_GOAL_A28_META_DB_URL", ""),
                           help="optional staging metadata DSN to read the "
                                "platform's persisted tool results (full rows "
                                "the SSE preview may truncate); explicit only")
    args = parser.parse_args()
    if args.command in {"setup", "smoke", "mutate"} and not args.secrets_dir:
        print("ontology-goal-a28: --secrets-dir (or OO_GOAL_A28_SECRETS_DIR) "
              "is required for this phase; no machine-specific default exists",
              file=sys.stderr)
        return 2
    try:
        if args.command == "plan":
            result = phase_plan(args)
        elif args.command == "setup":
            result = phase_setup(args)
        elif args.command == "smoke":
            result = phase_smoke(args)
        elif args.command == "query":
            result = phase_query(args)
        elif args.command == "mutate":
            result = phase_mutate(args)
        else:
            result = phase_report(args)
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        print(f"ontology-goal-a28: {error}", file=sys.stderr)
        return 1
    print(json.dumps(scrub(result), ensure_ascii=False, indent=2, default=str))
    if str(result.get("result", result.get("a28", ""))) in {
            "blocked", "fail", "not_implemented"}:
        return 2
    return 0


def _env_default(name: str, default: str) -> str:
    import os
    return os.environ.get(name) or default


if __name__ == "__main__":
    raise SystemExit(main())
