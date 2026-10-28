#!/usr/bin/env python3
"""Formal API vertical acceptance driver for the four-table federated fixture.

Contract payloads cite this worktree's code; execution evidence must come from
an isolated real API run. Front door only, no source-detail copying:

    login -> POST /api/v2/connections (two sources)
          -> POST /api/v2/integrated-datasets (one asset per table; native
             snapshot/mutation lanes, dbx only stamps discovery metadata)
          -> POST /api/v1/ontologies {name,domain} x2 (creates frozen empty v0)
          -> resolve v0 via GET /api/v2/ontologies/{id}/versions (current_release_id)
          -> POST .../versions/{v0}/drafts x2
          -> PUT .../workspace (object types per ontology)
          -> PUT .../workspace/mappings (client sends bindingMode=remote +
             curatedDatasetId only; the server freezes __remote__ descriptors)
          -> POST .../trial-runs -> GET .../impact -> POST .../promote
          -> POST /api/v2/formal/ontologies/{id}/agent/join-assertions
             (published type ids, evidenceRefs for explicit_key joins)
          -> POST /api/v2/formal/agent/query (canonical composition, cursor
             pagination) compared full-result against the independent oracle
          -> guards: permission / cursor invalid+expired / owner-session
             change / stale release / no-detail-persistence canary
          -> report: metric-only roll-up of the three artifacts;
             functional_acceptance_pass never promotes full_e2e_pass

NO asset sync, NO DatasetVersion sealing, NO snapshot fabrication: remote
bindings read live sources; run state records only ids the API returned.
The script starts nothing (no containers, no LLM, no scale runs) and only
talks to an explicit loopback API base reserved by the coordinator
(--api-base / FEDERATED_ACCEPTANCE_API_BASE, e.g. http://127.0.0.1:52004 —
a reserved port, not a running service until a slot is granted).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:  # normal script execution
    import federated_fixture as fixture
except ImportError:  # importlib loading from tests without package context
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "federated_fixture", Path(__file__).resolve().with_name("federated_fixture.py"))
    fixture = importlib.util.module_from_spec(_spec)
    assert _spec.loader is not None
    _spec.loader.exec_module(fixture)

ROOT = fixture.ROOT
ART = fixture.FED_ART
RUNS = ART / "acceptance"
METADATA_DB = os.environ.get("FEDERATED_ACCEPTANCE_METADATA_DB", "integrated_metadata")

# ---------------------------------------------------------------------------
# Verified contract registry. Every entry cites the code that defines it.
# ---------------------------------------------------------------------------
CONTRACT: dict[str, dict[str, Any]] = {
    "auth.login": {"method": "POST", "path": "/api/v1/auth/login",
                   "request": {"username": str, "password": str},
                   "response": "data.access_token bearer",
                   "evidence": "app/auth/router.py login; main.py /api/v1/auth"},
    "connections.register": {"method": "POST", "path": "/api/v2/connections",
                             "request": {"name": str, "kind": "postgres|mysql", "config": dict},
                             "response": "201 {id,name,kind,status}; duplicate name -> 409",
                             "note": "config carries the host-visible native endpoint plus the "
                                     "documented dbx_host/dbx_port pair (compose service names) "
                                     "so the pinned dbx container can reach the same physical "
                                     "source from inside the compose network",
                             "evidence": "data_channel/connections/router.py create_connection; "
                                         "integrated/dbx_adapter.py _dbx_endpoint"},
    "integrated.asset.register": {
        "method": "POST", "path": "/api/v2/integrated-datasets",
        # Plain BaseModel (no camel alias): snake_case body keys.
        "request": {"name": str, "connection_id": str, "schema_name": str,
                    "table_name": str, "writable": False},
        "response": "201 asset_dto: id, dataset_id, connection_id, schema_name, "
                    "table_name, schema_fingerprint, primary_key, columns; "
                    "duplicate resource -> 409 RESOURCE_ALREADY_REGISTERED",
        "note": "snapshot/mutation are native lanes; dbx only provides discovery "
                "metadata evidence (asset register fails closed without pinned dbx)",
        "evidence": "data_channel/integrated/contracts.py RegisterAsset; "
                    "integrated/service.py register_asset/asset_dto"},
    "integrated.asset.list": {"method": "GET", "path": "/api/v2/integrated-datasets",
                              "response": "{items:[asset_dto], total}",
                              "evidence": "integrated/service.py list_assets"},
    "ontology.create": {"method": "POST", "path": "/api/v1/ontologies",
                        "request": {"name": str, "domain": str, "description": "str | None"},
                        "response": "201 {data:{id,...}} with frozen empty v0 initial release; "
                                    "duplicate name -> 409; admin/editor only",
                        "evidence": "ontologies/projects/router.py create_ontology "
                                    "(main.py mounts at /api/v1/ontologies)"},
    "ontology.versions": {"method": "GET",
                          "path": "/api/v2/ontologies/{ontology_id}/versions",
                          "response": "{data:[version payloads], current_release_id, total,...}; "
                                      "current_release_id is the frozen v0 source version",
                          "evidence": "versions/router.py list_versions; workspace_service.list_versions"},
    "ontology.draft": {"method": "POST",
                       "path": "/api/v2/ontologies/{ontology_id}/versions/{source_version_id}/drafts",
                       "response": "201 draft version payload (id)",
                       "evidence": "versions/router.py create_draft_version"},
    "ontology.workspace.put": {
        "method": "PUT", "path": "/api/v2/ontologies/{ontology_id}/versions/{version_id}/workspace",
        "request": {"objectTypes": "list of {name, displayName, primaryKey, properties: "
                                   "[{name, type}], interfaces: [], positionX, positionY}",
                    "linkTypes": "list", "actions": "list", "functions": "list",
                    "baseRevision": None},
        "note": "camelCase (CamelModel); replaces modelling data only, keeps mappings",
        "evidence": "versions/router.py save_draft_workspace; "
                    "evolution_service.workspace_snapshot -> formal_modeling/schemas.py "
                    "SaveFullOntologyRequest/ObjectTypeBase"},
    "ontology.workspace_mappings.put": {
        "method": "PUT",
        "path": "/api/v2/ontologies/{ontology_id}/versions/{version_id}/workspace/mappings",
        "request": {"mappings": "list of {id, entityClass, bindingMode: 'remote', "
                                "curatedDatasetId: '<asset dataset_id>', "
                                "fieldMapping: {property: column}}",
                    "linkMappings": "list", "sentinels": "list", "baseRevision": None},
        "note": "client NEVER sends remote/__remote__: the server re-freezes the "
                "descriptor from the live dataset (client metadata is discarded); "
                "mapping ids are stable version-diff identities",
        "evidence": "versions/router.py save_draft_mappings; workspace_service lines "
                "~1047-1085; mappings/remote_binding.py normalize_workspace_remote_entries"},
    "ontology.semantic.put": {
        "method": "PUT",
        "path": "/api/v2/ontologies/{ontology_id}/versions/{version_id}/semantic",
        "request": {"baseRevision": str, "canvas": "business canvas", "documentMd": str,
                    "documentTitle": str},
        "response": "200 data.revision + server-derived semantic fingerprints",
        "evidence": "versions/router.py save_version_semantic; "
                    "versions/semantic_service.py SemanticLayerWrite"},
    "join_assertions.register": {

        "method": "POST", "path": "/api/v2/formal/ontologies/{ontology_id}/agent/join-assertions",
        "request": "JoinAssertionCreate (camelCase): leftOntologyId, leftObjectTypeId, "
                   "leftProperty, rightOntologyId, rightObjectTypeId, rightProperty, "
                   "keyType (string|number|uuid), businessMeaning",
        "response": "evidenceRef 'assertion:{id}' for explicit_key joins",
        "evidence": "agent_runtime/proposals_router.py create_join_assertion; "
                    "agent_query/schemas.py JoinAssertionCreate"},
    "ontology.trial": {"method": "POST",
                       "path": "/api/v2/ontologies/{ontology_id}/versions/{version_id}/trial-runs",
                       "request": {}, "response": "201 trial run payload",
                       "evidence": "versions/router.py create_trial_run"},
    "ontology.impact": {"method": "GET",
                        "path": "/api/v2/ontologies/{ontology_id}/versions/{version_id}/impact",
                        "evidence": "versions/router.py get impact"},
    "ontology.promote": {"method": "POST",
                         "path": "/api/v2/ontologies/{ontology_id}/versions/{version_id}/promote",
                         "request": {"trialRunId": str, "impactHash": str},
                         "response": "201 data.id release (admin only)",
                         "evidence": "versions/router.py promote_draft (require_admin)"},
    "query.execute": {
        "method": "POST", "path": "/api/v2/formal/agent/query",
        "request": "QueryRequest (camelCase, extra=forbid): sources<=4 "
                   "[{alias, ontologyId, releaseId, objectTypeId} | {alias, subquery} "
                   "(derived depth<=2, <=16 leaves)], joins<=3 [{left,right,type "
                   "inner|left, on explicit_key{leftProperty,rightProperty,keyType,"
                   "cardinality,businessMeaning,evidenceRefs[1..8]}}], "
                   "source_filters/where (FilterNode), select, group_by, aggregates "
                   "(count/count_distinct/sum/avg/min/max), order_by, page_size<=200",
        "response": "{data: remote schemaVersion=2 camelCase: rows, pageRowCount, "
                    "totalRowCount, hasNext, cursor, expiresAt, truncated, partial, persistence}",
        "evidence": "agent_query/router.py run_query; data_channel/query/ontology_adapter.py _response"},
    "query.cursor": {"method": "POST", "path": "/api/v2/formal/agent/query",
                     "request": {"cursor": str}, "evidence": "schemas.py CursorRequest; "
                                                            "router fetch branch"},
}
PHASE_CONTRACTS: dict[str, list[str]] = {}  # everything above is code-verified


class ContractBlocked(RuntimeError):
    """Reserved for genuinely unverifiable future gaps; none currently."""


def pending_contracts(phase: str) -> list[str]:
    return [name for name in PHASE_CONTRACTS.get(phase, [])
            if CONTRACT.get(name, {}).get("status") == "pending"]


def require_contract(phase: str) -> None:
    for name in pending_contracts(phase):
        raise ContractBlocked(f"phase {phase!r} blocked by pending contract {name}")


# ---------------------------------------------------------------------------
# Semantic types describe values, not their lossless JSON transport encoding.
# Remote DECIMAL numbers stay native Decimal; DATE columns stay native dates.
# ---------------------------------------------------------------------------
OBJECT_MODEL: dict[str, dict[str, Any]] = {
    "crm": {
        "name": "fed-accept-crm", "domain": "customer",
        "types": {
            "Customer": {"table": "fed_customers", "primaryKey": "customer_id",
                         "properties": {"customer_id": "number", "external_ref": "number",
                                        "is_key": "boolean", "tier": "string",
                                        "region": "string", "credit_limit": "number",
                                        "contact_note": "string"}},
            "Contract": {"table": "fed_contracts", "primaryKey": "contract_id",
                         "properties": {"contract_id": "number", "customer_id": "number",
                                        "contract_no": "string", "valid_from": "date",
                                        "valid_to": "date", "signed_amount": "number",
                                        "currency": "string", "terms_note": "string"}}},
    },
    "erp": {
        "name": "fed-accept-erp", "domain": "order",
        "types": {
            "Order": {"table": "fed_orders", "primaryKey": "order_id",
                      "properties": {"order_id": "number", "customer_ref": "number",
                                     "contract_ref": "number", "waybill_no": "string",
                                     "gross_cents": "number", "net_amount": "number",
                                     "status": "string", "created_at": "date",
                                     "remark": "string"}},
            "Shipment": {"table": "fed_shipments", "primaryKey": "shipment_id",
                         "properties": {"shipment_id": "number", "order_id": "number",
                                        "waybill_no": "string", "ship_qty": "number",
                                        "ship_cost": "number", "carrier": "string",
                                        "ship_note": "string"}}},
    },
}

# Every listed table must remain unchanged during the remote QUERY phase,
# including legacy persisted-query tables. Counts are a signal, not a proof
# about logs, object storage, PostgreSQL TEMP/WAL, or changed row contents.
CANARY_ZERO_DELTA_TABLES = ("v2_unified_query_snapshots", "v2_unified_query_rows",
                            "fo_agent_query_snapshots", "fo_agent_query_rows",
                            "v2_dataset_versions", "v2_dataset_changesets",
                            "v2_dataset_changeset_rows", "fo_property_facts",
                            "fo_object_instances", "fo_link_instances",
                            "ontology_trial_runs", "ontology_trial_objects",
                            "ontology_trial_links", "super_assistant_messages",
                            "super_assistant_tool_runs", "super_assistant_reflection_runs",
                            "ontology_change_logs")

FRONT_DOOR_STEPS = ("login", "register_connections", "register_assets", "create_ontologies",
                    "create_drafts", "define_workspace", "workspace_mappings",
                    "save_semantics", "trial", "impact", "promote", "join_assertions")

# Deterministic public fixture password shared by the isolated compose dbx
# container (DBX_PASSWORD) and the platform API (INTEGRATED_DBX_PASSWORD).
# Both ends must use the same value; it is a fixture, not a secret.
DBX_FIXTURE_PASSWORD = "fedanalysis-dbx-fixture-20261026"


# ---------------------------------------------------------------------------
# Offline plan: canonical composition and its independent oracle expectations.
# QS supports projection/group/stats only (no CASE/COALESCE/arithmetic), so the
# canonical shape is: outer <=4 sources, CRM key customers INNER valid-contract
# pre-aggregation anchors rows, two ERP subquery branches (90-day order totals;
# unshipped counts via orders LEFT shipments-grouped-by-order_id then
# shipment_order_id IS NULL) attach with LEFT. Amounts are pre-aggregated
# before the outer join (never a flat 4-table join). LEFT-no-match stays NULL.
# ---------------------------------------------------------------------------
QUERY_SPECS = {
    "canonical_composition": {
        "outer_sources": ["crm_key_customers_with_contracts (inner anchor, derived)",
                          "erp_orders_90d_totals (LEFT, derived)",
                          "erp_pending_counts (LEFT, derived)"],
        "preaggregation": "contracts and orders pre-aggregate per customer; shipments "
                          "group by order_id inside the ERP branch; flat 4-table joins "
                          "are forbidden",
        "null_semantics": "LEFT no-match => NULL presented honestly; canonical comparison, "
                          "no zero-fill, no model arithmetic",
        "lineage_evidence": "explicit_key joins carry evidenceRefs from user-registered "
                            "join-assertions (assertion:{id})"},
    "crm_key_customers_with_contracts": {
        "role": "inner anchor (derived: Customer INNER Contract on customer_id)",
        "filters": {"is_key": True, "contract_active_on": fixture.WINDOW_END.isoformat()},
        "group_by": ["customer_id"],
        "measures": ["contract_count", "sum_signed_amount"]},
    "erp_orders_90d_totals": {
        "role": "LEFT subquery (Order, created_at within fixed 90d window)",
        "group_by": ["customer_ref"],
        "measures": ["order_count", "sum_gross_cents", "sum_net_amount"]},
    "erp_pending_counts": {
        "role": "LEFT subquery",
        "pipeline": "90-day Order LEFT (Shipment GROUP BY order_id) -> shipment.order_id "
                    "IS NULL -> group by customer_ref",
        "group_by": ["customer_ref"], "measures": ["pending_order_count"],
        "null_when": "customer has no unshipped order -> outer LEFT yields NULL, not 0"},
}


def plan_queries(orders: int, customer_ids: list[int]) -> dict[str, Any]:
    """Pure-Python expected results for the canonical acceptance composition."""
    customers, contract_count = fixture.dimensions(orders)
    all_ids = range(10, customers + 1, 10) if customer_ids == ["*"] else sorted(set(customer_ids))
    canonical: dict[str, Any] = {}
    for cid in all_ids:
        if not 1 <= cid <= customers or not fixture.customer_row(cid)["is_key"]:
            continue
        contracts = [fixture.contract_row(k, customers)
                     for k in range((cid - 1) * 5 + 1, min(cid * 5, contract_count) + 1)]
        valid = [row for row in contracts
                 if row["valid_from"] <= fixture.WINDOW_END <= row["valid_to"]]
        if not valid:
            continue
        customer_orders = [fixture.order_row(o, customers, contract_count)
                           for o in range((cid - 1) * 10 + 1, min(cid * 10, orders) + 1)]
        recent = [row for row in customer_orders
                  if fixture.WINDOW_START <= row["created_at"] <= fixture.WINDOW_END]
        pending = sum(fixture.shipment_count(row["order_id"]) == 0 for row in recent)
        canonical[str(cid)] = {
            "customer_id": cid,
            "contract_count": len(valid),
            "sum_signed_amount": str(sum((row["signed_amount"] for row in valid), Decimal(0))),
            "order_count": len(recent) if recent else None,
            "sum_gross_cents": sum(row["gross_cents"] for row in recent) if recent else None,
            "sum_net_amount": str(sum((row["net_amount"] for row in recent), Decimal(0))) if recent else None,
            "pending_order_count": pending or None,
        }
    totals = fixture.expected_totals(orders)
    return {"orders": orders, "customers": customers, "specs": QUERY_SPECS,
            "expected_canonical": canonical,
            "acceptance_checklist": {
                "key_customers_90d_amount": "sum_gross_cents/sum_net_amount per canonical row",
                "unshipped_orders": "pending_order_count (NULL when none)",
                "contract_amount": "sum_signed_amount per canonical row"},
            "expected_totals_subset": {"unshipped_orders_total": totals["unshipped_orders"],
                                       "created_at_window": totals["created_at_window"]},
            "comparison_rules": ["Decimal-exact on every measure",
                                 "full-result equality across all pages, never page samples",
                                 "NULL means LEFT no-match: expected None must stay null; a zero "
                                 "there is a mismatch unless a COALESCE contract is agreed",
                                 "no model arithmetic or zero-filling anywhere"],
            "full_e2e_pass": False}


# ---------------------------------------------------------------------------
# HTTP layer (stdlib only; nothing is started by this script).
# ---------------------------------------------------------------------------
class Api:
    def __init__(self, base_url: str, token: str | None = None, timeout: float = 60.0):
        parsed = urlsplit(base_url)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError("acceptance API base must be an explicit loopback http URL")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        # Per-call JSON transport counters (wire bytes). Subclasses that
        # override request() (test fakes) may leave them untouched; readers
        # must tolerate that via getattr defaults.
        self.last_request_bytes = 0
        self.last_response_bytes = 0
        self.total_request_bytes = 0
        self.total_response_bytes = 0

    def request(self, method: str, path: str, body: dict | None = None,
                expect: tuple[int, ...] = (200,)) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        self.last_request_bytes = len(data) if data is not None else 0
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read().decode()
                status = response.status
        except urllib.error.HTTPError as error:
            payload = error.read().decode()
            status = error.code
        self.last_response_bytes = len(payload.encode("utf-8"))
        self.total_request_bytes += self.last_request_bytes
        self.total_response_bytes += self.last_response_bytes
        if payload.strip():
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError as exc:
                if status not in expect:
                    raise RuntimeError(
                        f"{method} {path} -> {status} non-JSON response: "
                        f"{payload[:500]!r}") from exc
                raise RuntimeError(
                    f"{method} {path} -> {status} returned invalid JSON: "
                    f"{payload[:500]!r}") from exc
        else:
            parsed = {}
        if status not in expect:
            raise RuntimeError(f"{method} {path} -> {status}: {str(parsed)[:500]}")
        return status, parsed

    def login(self, username: str, password: str) -> None:
        _, body = self.request("POST", "/api/v1/auth/login",
                               {"username": username, "password": password})
        self.token = _payload(body)["access_token"]


def _payload(body: Any) -> Any:
    """Unwrap the {'data': ...} envelope when present (endpoints differ)."""
    if isinstance(body, dict) and isinstance(body.get("data"), (dict, list)):
        return body["data"]
    return body


def collect_all_pages(api: Api, request_body: dict, *, max_pages: int = 500,
                      page_cap: int = 200) -> tuple[list[dict], dict[str, Any]]:
    """Walk the remote camelCase envelope; never infer completeness from rows."""
    rows: list[dict] = []
    seen_cursors: set[str] = set()
    meta: dict[str, Any] = {}
    identity = None
    body = request_body
    pages = 0
    first_plan = None
    first_source_states = None
    request_bytes = 0
    response_bytes = 0
    for _ in range(max_pages):
        _, response = api.request("POST", "/api/v2/formal/agent/query", body)
        pages += 1
        # Api.request maintains per-call wire counters on self; overridden
        # request() implementations without them degrade to zero, never fail.
        request_bytes += getattr(api, "last_request_bytes", 0)
        response_bytes += getattr(api, "last_response_bytes", 0)
        data = _payload(response)
        page = data.get("rows")
        if (data.get("schemaVersion") != 2 or not isinstance(page, list)
                or type(data.get("hasNext")) is not bool
                or type(data.get("pageRowCount")) is not int
                or type(data.get("totalRowCount")) is not int):
            raise RuntimeError("invalid remote query response contract")
        if data["pageRowCount"] > page_cap or len(page) > page_cap:
            raise RuntimeError(f"pageRowCount exceeds the contract cap {page_cap}")
        if data["pageRowCount"] != len(page):
            raise RuntimeError("pageRowCount does not match the page contents")
        current_identity = (data.get("queryId"), data["totalRowCount"], data.get("expiresAt"))
        if not current_identity[0] or not current_identity[2]:
            raise RuntimeError("remote query identity or expiry missing")
        if identity is not None and identity != current_identity:
            raise RuntimeError("query identity changed between pages")
        identity = current_identity
        if data.get("persistence", {}).get("mode") != "ephemeral":
            raise RuntimeError("remote result did not use ephemeral storage")
        if data.get("truncated") is not False or data.get("partial") is not False:
            raise RuntimeError("remote result is partial or truncated")
        if pages == 1:
            # First-response execution evidence. plan/sourceStates are
            # structural metrics by contract (strategy, operators, source
            # row counts, alias/asOf/consistency/completion); they never
            # carry result rows, so retaining them keeps artifacts
            # row-free.
            first_plan = data.get("plan")
            first_source_states = data.get("sourceStates")
        rows.extend(page)
        if len(rows) > data["totalRowCount"]:
            raise RuntimeError("pagination exceeded the declared totalRowCount")
        meta = {"queryId": data["queryId"], "totalRowCount": data["totalRowCount"],
                "truncated": False, "partial": False, "expiresAt": data["expiresAt"],
                "consistency": data.get("consistency"), "countsScope": data.get("countsScope"),
                "pageCount": pages, "plan": first_plan, "sourceStates": first_source_states,
                "transportBytes": {"request": request_bytes, "response": response_bytes}}
        if not data["hasNext"]:
            if data.get("cursor") is not None or len(rows) != data["totalRowCount"]:
                raise RuntimeError("last page does not match the complete result")
            return rows, meta
        cursor = data.get("cursor")
        if not isinstance(cursor, str) or not cursor.startswith("mem."):
            raise RuntimeError("hasNext without a memory cursor")
        if cursor in seen_cursors:
            raise RuntimeError("pagination cursor repeated; aborting to avoid an infinite loop")
        seen_cursors.add(cursor)
        body = {"cursor": cursor}
    raise RuntimeError(f"pagination exceeded {max_pages} pages without exhaustion")


def compare_decimal_exact(expected: dict[str, Any], observed: dict[str, Any]) -> list[str]:
    """Full-result comparison with NULL-honest LEFT semantics."""
    mismatches = []
    for key in sorted(set(expected) | set(observed)):
        want, got = expected.get(key), observed.get(key)
        if want is None or got is None:
            if not (want is None and got is None):
                mismatches.append(f"{key}: missing {'observed' if got is None else 'expected'}")
            continue
        for field in want:
            a, b = want[field], got.get(field)
            if a is None or b is None:
                if not (a is None and b is None):
                    mismatches.append(f"{key}.{field}: NULL honesty violated "
                                      f"(expected {a!r}, observed {b!r})")
                continue
            try:
                equal = Decimal(str(a)) == Decimal(str(b))
            except Exception:
                equal = str(a) == str(b)
            if not equal:
                mismatches.append(f"{key}.{field}: expected {a}, observed {b}")
    return mismatches


# ---------------------------------------------------------------------------
# Front-door run state machine (persisted after every step; resumable).
# ---------------------------------------------------------------------------
def _snake_asset_body(name: str, connection_id: str, schema_name: str, table_name: str) -> dict:
    return {"name": name, "connection_id": connection_id, "schema_name": schema_name,
            "table_name": table_name, "writable": False}


def _workspace_object_type(type_name: str, definition: dict[str, Any]) -> dict:
    return {"name": type_name, "displayName": type_name,
            "primaryKey": definition["primaryKey"],
            "properties": [{"name": prop, "type": kind}
                           for prop, kind in definition["properties"].items()],
            "interfaces": [], "positionX": 100.0, "positionY": 100.0}


def _remote_mapping_entry(ontology_id: str, entity_class: str, dataset_id: str,
                          definition: dict[str, Any]) -> dict:
    """Client contract: stable id plus bindingMode + curatedDatasetId only.

    The server freezes __remote__/__primary_key__ from the live asset; client
    metadata is dropped. Stable IDs are required for version diff/audit and
    make reruns address the same draft entry. The id is a UUID5 over the
    ontology identity and entity class (repo NAMESPACE_URL convention), so
    independently repeated fixture ontologies in one metadata database can
    never collide on the global ``v2_ontology_mappings`` primary key the way
    the former global literal ids did.
    """
    return {"id": str(uuid.uuid5(uuid.NAMESPACE_URL,
                                 f"{ontology_id}:remote-mapping:{entity_class}")),
            "entityClass": entity_class, "bindingMode": "remote",
            "curatedDatasetId": dataset_id,
            "fieldMapping": {prop: prop for prop in definition["properties"]}}


def _semantic_canvas(model: dict[str, Any]) -> dict[str, Any]:
    """Build a minimal self-consistent business canvas from the fixture model."""
    return {
        "objects": [
            {
                "name": type_name,
                "display_name": type_name,
                "description": "federated acceptance business object",
                "key_attribute": definition["primaryKey"],
                "attributes": [
                    {"name": prop, "display_name": prop,
                     "type_hint": kind,
                     "required": prop == definition["primaryKey"]}
                    for prop, kind in definition["properties"].items()
                ],
                "relations": [],
            }
            for type_name, definition in model["types"].items()
        ],
        "actors": [], "behaviors": [], "events": [], "rules": [],
        "scenarios": [], "processes": [],
    }


class AcceptanceRun:
    def __init__(self, name: str, api: Api, orders: int):
        if not name or not all(ch.isalnum() or ch in "-_" for ch in name):
            raise ValueError("run name must contain only letters, digits, dash and underscore")
        RUNS.mkdir(parents=True, exist_ok=True)
        self.path = RUNS / f"{name}.json"
        if not self.path.exists():
            self.state: dict[str, Any] = {"name": name, "orders": orders, "steps": {},
                                          "created_unix": time.time(), "full_e2e_pass": False}
        else:
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
            if self.state.get("orders") != orders:
                raise RuntimeError("run exists with a different --orders; use a new run name")
        self.api = api
        self.orders = orders

    def save(self) -> None:
        self.path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2, default=str) + "\n",
                             encoding="utf-8")

    def step(self, name: str) -> dict[str, Any]:
        return self.state["steps"].setdefault(name, {})

    def done(self, name: str) -> bool:
        return bool(self.step(name).get("ok"))

    def run(self) -> dict[str, Any]:
        for name in FRONT_DOOR_STEPS:
            if self.done(name):
                continue
            getattr(self, f"step_{name}")()
            self.save()
        self.state["front_door_complete"] = all(self.done(n) for n in FRONT_DOOR_STEPS)
        self.save()
        return self.state

    # -- steps -----------------------------------------------------------
    def step_login(self) -> None:
        if not self.api.token:
            raise RuntimeError("authenticate the API before running the front door")
        self.step("login").update({"ok": True})

    def step_register_connections(self) -> None:
        connections = self.step("register_connections")
        # host/port stay the host-visible published endpoint for the native
        # lane; the dbx pair points the pinned dbx container at the same
        # physical source through the compose network service name.
        for key, kind, port, dbx_host, dbx_port in (
                ("source_pg", "postgres", 52433, "source-pg", 5432),
                ("source_mysql", "mysql", 51306, "mysql", 3306)):
            name = f"fed-acceptance-{key}"
            config = {"host": "127.0.0.1", "port": port, "database": "integrated_source",
                      "user": "integrated_e2e", "password": "integrated_e2e_fixture",
                      "dbx_host": dbx_host, "dbx_port": dbx_port}
            status, body = self.api.request("POST", "/api/v2/connections",
                                            {"name": name, "kind": kind, "config": config},
                                            expect=(201, 409))
            if status == 409:
                _, listing = self.api.request("GET", "/api/v2/connections")
                body = next(item for item in _payload(listing) if item.get("name") == name)
            connections[key] = {"id": body["id"], "name": name}
        connections["ok"] = True

    def step_register_assets(self) -> None:
        connections = self.state["steps"]["register_connections"]
        assets = self.step("register_assets")
        tables = {key: definition["table"]
                  for model in OBJECT_MODEL.values()
                  for key, definition in model["types"].items()}
        asset_entries = {}
        for type_name, table in tables.items():
            connection_key = "source_pg" if type_name in OBJECT_MODEL["crm"]["types"] else "source_mysql"
            schema = "public" if connection_key == "source_pg" else "integrated_source"
            body = _snake_asset_body(f"fed-acceptance-{table}", connections[connection_key]["id"],
                                     schema, table)
            status, response = self.api.request("POST", "/api/v2/integrated-datasets", body,
                                                expect=(201, 409))
            if status == 409:  # RESOURCE_ALREADY_REGISTERED: adopt by listing
                _, listing = self.api.request("GET", "/api/v2/integrated-datasets")
                response = next(item for item in _payload(listing)["items"]
                                if item.get("schema_name") == schema and item.get("table_name") == table)
            asset = _payload(response) if status == 201 else response
            asset_entries[type_name] = {"asset_id": asset["id"], "dataset_id": asset["dataset_id"],
                                        "schema_fingerprint": asset.get("schema_fingerprint"),
                                        "primary_key": asset.get("primary_key")}
        assets["assets"] = asset_entries
        assets["ok"] = len(asset_entries) == 4

    def step_create_ontologies(self) -> None:
        ontologies = self.step("create_ontologies")
        entries = {}
        # Register fixture domains through the same admin settings API used by
        # production, rather than relying on legacy ontology-domain fallbacks.
        for model in OBJECT_MODEL.values():
            self.api.request(
                "POST", "/api/v1/domains",
                {"name": model["domain"],
                 "description": "federated acceptance fixture"},
                expect=(201, 409),
            )
        for key, model in OBJECT_MODEL.items():
            status, body = self.api.request("POST", "/api/v1/ontologies",
                                            {"name": model["name"], "domain": model["domain"],
                                             "description": "federated acceptance fixture"},
                                            expect=(201, 409))
            if status == 409:  # DUPLICATE_NAME: adopt via list search
                _, listing = self.api.request("GET", "/api/v1/ontologies")
                payload = next(item for item in _payload(listing)["items"]
                               if isinstance(item, dict) and item.get("name") == model["name"])
            else:
                payload = _payload(body)
            entries[key] = {"id": payload["id"], "name": model["name"]}
        ontologies["ontologies"] = entries
        ontologies["ok"] = len(entries) == 2

    def step_create_drafts(self) -> None:
        ontologies = self.state["steps"]["create_ontologies"]["ontologies"]
        drafts = self.step("create_drafts")
        for key, ontology in ontologies.items():
            _, versions = self.api.request(
                "GET", f"/api/v2/ontologies/{ontology['id']}/versions?limit=5")
            current_release = versions.get("current_release_id") \
                if isinstance(versions, dict) else None
            if not current_release:
                raise RuntimeError(f"versions response lacked current_release_id for {key}")
            _, body = self.api.request(
                "POST", f"/api/v2/ontologies/{ontology['id']}/versions/{current_release}/drafts",
                {}, expect=(201,))
            payload = _payload(body)
            drafts[key] = {"version_id": payload.get("id") or payload.get("version_id"),
                           "source_version_id": current_release}
        drafts["ok"] = bool(drafts.get("crm") and drafts.get("erp"))

    def step_define_workspace(self) -> None:
        drafts = self.state["steps"]["create_drafts"]
        workspace = self.step("define_workspace")
        for key, model in OBJECT_MODEL.items():
            ontology = self.state["steps"]["create_ontologies"]["ontologies"][key]
            version = drafts[key]["version_id"]
            body = {"objectTypes": [_workspace_object_type(name, definition)
                                    for name, definition in model["types"].items()],
                    "linkTypes": [], "actions": [], "functions": [], "baseRevision": None}
            self.api.request("PUT",
                             f"/api/v2/ontologies/{ontology['id']}/versions/{version}/workspace", body)
            _, response = self.api.request(
                "GET", f"/api/v2/ontologies/{ontology['id']}/versions/{version}/workspace")
            types = {item["name"]: item["id"] for item in _payload(response)["objectTypes"]}
            if set(types) != set(model["types"]) or not all(types.values()):
                raise RuntimeError("saved workspace did not return every object type identity")
            workspace[key] = {"object_types": types}
        workspace["ok"] = True

    def step_workspace_mappings(self) -> None:
        assets = self.state["steps"]["register_assets"]["assets"]
        mappings_step = self.step("workspace_mappings")
        for key, model in OBJECT_MODEL.items():
            ontology = self.state["steps"]["create_ontologies"]["ontologies"][key]
            version = self.state["steps"]["create_drafts"][key]["version_id"]
            entries = [_remote_mapping_entry(ontology["id"], type_name,
                                             assets[type_name]["dataset_id"], definition)
                       for type_name, definition in model["types"].items()]
            body = {"mappings": entries, "linkMappings": [], "sentinels": [], "baseRevision": None}
            self.api.request("PUT",
                             f"/api/v2/ontologies/{ontology['id']}/versions/{version}/workspace/mappings",
                             body)
            mappings_step[key] = {"remote_mappings": [e["entityClass"] for e in entries]}
        mappings_step["ok"] = True

    def step_save_semantics(self) -> None:
        semantics = self.step("save_semantics")
        for key, model in OBJECT_MODEL.items():
            ontology = self.state["steps"]["create_ontologies"]["ontologies"][key]
            version = self.state["steps"]["create_drafts"][key]["version_id"]
            _, workspace = self.api.request(
                "GET", f"/api/v2/ontologies/{ontology['id']}/versions/{version}/workspace")
            current = _payload(workspace)
            base_revision = current.get("revision") if isinstance(current, dict) else None
            if not isinstance(base_revision, str) or not base_revision:
                raise RuntimeError(f"workspace response lacked revision for semantic step: {key}")
            body = {
                "baseRevision": base_revision,
                "canvas": _semantic_canvas(model),
                "documentMd": (
                    f"# {model['name']} 业务语义\n\n"
                    "本体结构与远程资产字段按验收 fixture 冻结；该文档只记录语义定义，"
                    "不包含远程源记录。\n"
                ),
                "documentTitle": f"{model['name']} 业务语义",
            }
            _, response = self.api.request(
                "PUT", f"/api/v2/ontologies/{ontology['id']}/versions/{version}/semantic",
                body, expect=(200,))
            payload = _payload(response)
            if not isinstance(payload, dict) or not payload.get("revision"):
                raise RuntimeError(f"semantic save response lacked revision for {key}")
            semantics[key] = {
                "revision": payload["revision"],
                "semantic_revision": payload.get("semanticRevision"),
                "canvas_fingerprint": payload.get("canvasFingerprint"),
            }
        semantics["ok"] = True

    def step_join_assertions(self) -> None:
        crm = self.state["steps"]["create_ontologies"]["ontologies"]["crm"]
        erp = self.state["steps"]["create_ontologies"]["ontologies"]["erp"]
        assertions = self.step("join_assertions")
        registered = []
        types = self.state["steps"]["define_workspace"]
        for left_domain, left_ot, left_prop, right_domain, right_ot, right_prop in (
                ("crm", "Customer", "customer_id", "crm", "Contract", "customer_id"),
                ("crm", "Customer", "customer_id", "erp", "Order", "customer_ref"),
                ("erp", "Order", "order_id", "erp", "Shipment", "order_id")):
            left = crm if left_domain == "crm" else erp
            right = crm if right_domain == "crm" else erp
            body = {"leftOntologyId": left["id"],
                    "leftObjectTypeId": types[left_domain]["object_types"][left_ot],
                    "leftProperty": left_prop, "rightOntologyId": right["id"],
                    "rightObjectTypeId": types[right_domain]["object_types"][right_ot],
                    "rightProperty": right_prop, "keyType": "number",
                    "businessMeaning": "已确认的客户/合同/订单/发运业务键（合成验收）"}
            _, response = self.api.request("POST", f"/api/v2/formal/ontologies/{left['id']}/agent/join-assertions",
                                           body, expect=(200, 201))
            payload = _payload(response)
            registered.append(payload["evidenceRef"])
        assertions["evidence_refs"] = registered
        assertions["ok"] = bool(registered)

    def step_trial(self) -> None:
        drafts = self.state["steps"]["create_drafts"]
        trial = self.step("trial")
        for key, draft in drafts.items():
            if key == "ok":
                continue
            ontology = self.state["steps"]["create_ontologies"]["ontologies"][key]
            _, body = self.api.request(
                "POST", f"/api/v2/ontologies/{ontology['id']}/versions/{draft['version_id']}/trial-runs",
                {}, expect=(200, 201))
            payload = _payload(body)
            if payload.get("status") != "passed" or not payload.get("id"):
                raise RuntimeError(f"remote trial did not pass for {key}: {payload.get('status')}")
            trial[key] = {"run_id": payload["id"], "impact_hash": payload["impact_hash"]}
        trial["ok"] = True

    def step_impact(self) -> None:
        drafts = {k: v for k, v in self.state["steps"]["create_drafts"].items() if k != "ok"}
        impact = self.step("impact")
        for key, draft in drafts.items():
            ontology = self.state["steps"]["create_ontologies"]["ontologies"][key]
            _, body = self.api.request(
                "GET", f"/api/v2/ontologies/{ontology['id']}/versions/{draft['version_id']}/impact")
            impact[key] = {"summary": _payload(body)}
        impact["ok"] = True

    def step_promote(self) -> None:
        drafts = {k: v for k, v in self.state["steps"]["create_drafts"].items() if k != "ok"}
        promote = self.step("promote")
        for key, draft in drafts.items():
            ontology = self.state["steps"]["create_ontologies"]["ontologies"][key]
            trial = self.state["steps"]["trial"][key]
            impact = self.state["steps"]["impact"][key]["summary"]
            if impact["impactHash"] != trial["impact_hash"]:
                raise RuntimeError("reviewed impact no longer matches the passed trial")
            _, body = self.api.request(
                "POST", f"/api/v2/ontologies/{ontology['id']}/versions/{draft['version_id']}/promote",
                {"trialRunId": trial["run_id"], "impactHash": impact["impactHash"]},
                expect=(201,))
            payload = _payload(body)
            promote[key] = {"release_id": payload.get("release_id") or payload.get("id"),
                            "response_keys": sorted(payload) if isinstance(payload, dict) else []}
        promote["ok"] = all(entry.get("release_id") for entry in promote.values()
                            if isinstance(entry, dict) and "release_id" in entry)


# ---------------------------------------------------------------------------
# Canonical QueryRequest construction (camelCase per QS schemas).
# ---------------------------------------------------------------------------
def build_canonical_query(crm_release: str, erp_release: str, evidence_refs: list[str],
                          crm_id: str, erp_id: str, type_ids: dict[str, str],
                          page_size: int = 50) -> dict[str, Any]:
    """Outer <=4 sources; derived depth <=2; explicit_key joins with user
    join-assertion evidence; pre-aggregated measures; no arithmetic."""
    if len(evidence_refs) != 3 or set(type_ids) != {"Customer", "Contract", "Order", "Shipment"}:
        raise ValueError("canonical query requires published type ids and all three join assertions")

    def object_source(alias: str, ontology: str, release: str, object_type: str) -> dict:
        return {"alias": alias, "ontologyId": ontology, "releaseId": release,
                "objectTypeId": type_ids[object_type]}

    crm_anchor = {
        "alias": "crm",
        "subquery": {
            "sources": [object_source("c", crm_id, crm_release, "Customer"),
                        object_source("k", crm_id, crm_release, "Contract")],
            "joins": [{"left": "c", "right": "k", "type": "inner",
                       "on": {"kind": "explicit_key", "leftProperty": "customer_id",
                              "rightProperty": "customer_id", "keyType": "number",
                              "cardinality": "1:N",
                              "businessMeaning": "客户持有合同",
                              "evidenceRefs": evidence_refs[:1]}}],
            "where": [{"source": "c", "property": "is_key", "op": "eq", "value": True},
                      {"source": "k", "property": "valid_from", "op": "lte",
                       "value": fixture.WINDOW_END.isoformat()},
                      {"source": "k", "property": "valid_to", "op": "gte",
                       "value": fixture.WINDOW_END.isoformat()}],
            "groupBy": [{"source": "c", "property": "customer_id"}],
            "aggregates": [{"fn": "count",
                            "as": "contract_count"},
                           {"fn": "sum", "source": "k", "property": "signed_amount",
                            "as": "sum_signed_amount"}],
            "select": [{"source": "c", "property": "customer_id", "as": "customer_id"}],
        },
    }
    erp_totals = {
        "alias": "totals",
        "subquery": {
            "sources": [object_source("o", erp_id, erp_release, "Order")],
            "where": [{"source": "o", "property": "created_at", "op": "gte",
                       "value": fixture.WINDOW_START.isoformat()},
                      {"source": "o", "property": "created_at", "op": "lte",
                       "value": fixture.WINDOW_END.isoformat()}],
            "groupBy": [{"source": "o", "property": "customer_ref"}],
            "aggregates": [{"fn": "count",
                            "as": "order_count"},
                           {"fn": "sum", "source": "o", "property": "gross_cents",
                            "as": "sum_gross_cents"},
                           {"fn": "sum", "source": "o", "property": "net_amount",
                            "as": "sum_net_amount"}],
            "select": [{"source": "o", "property": "customer_ref", "as": "customer_id"}],
        },
    }
    erp_pending = {
        "alias": "pending",
        "subquery": {
            "sources": [object_source("po", erp_id, erp_release, "Order"),
                        {"alias": "ps",
                         "subquery": {"sources": [object_source("s", erp_id, erp_release, "Shipment")],
                                      "groupBy": [{"source": "s", "property": "order_id"}],
                                      "select": [{"source": "s", "property": "order_id",
                                                  "as": "order_id"}]}}],
            "joins": [{"left": "po", "right": "ps", "type": "left",
                       "on": {"kind": "explicit_key", "leftProperty": "order_id",
                              "rightProperty": "order_id", "keyType": "number",
                              "cardinality": "1:1", "businessMeaning": "订单发运明细预聚合",
                              "evidenceRefs": evidence_refs[2:3]}}],
            "where": [{"source": "ps", "property": "order_id", "op": "is_null"},
                      {"source": "po", "property": "created_at", "op": "gte",
                       "value": fixture.WINDOW_START.isoformat()},
                      {"source": "po", "property": "created_at", "op": "lte",
                       "value": fixture.WINDOW_END.isoformat()}],
            "groupBy": [{"source": "po", "property": "customer_ref"}],
            "aggregates": [{"fn": "count",
                            "as": "pending_order_count"}],
            "select": [{"source": "po", "property": "customer_ref", "as": "customer_id"}],
        },
    }
    return {
        "sources": [crm_anchor, erp_totals, erp_pending],
        "joins": [
            {"left": "crm", "right": "totals", "type": "left",
             "on": {"kind": "explicit_key", "leftProperty": "customer_id",
                    "rightProperty": "customer_id", "keyType": "number",
                    "cardinality": "1:1", "businessMeaning": "客户90天订单聚合",
                    "evidenceRefs": evidence_refs[1:2]}},
            {"left": "crm", "right": "pending", "type": "left",
             "on": {"kind": "explicit_key", "leftProperty": "customer_id",
                    "rightProperty": "customer_id", "keyType": "number",
                    "cardinality": "1:1", "businessMeaning": "客户未发运订单计数",
                    "evidenceRefs": evidence_refs[1:2]}},
        ],
        "select": [{"source": "crm", "property": "customer_id", "as": "customer_id"},
                   {"source": "crm", "property": "contract_count", "as": "contract_count"},
                   {"source": "crm", "property": "sum_signed_amount", "as": "sum_signed_amount"},
                   {"source": "totals", "property": "order_count", "as": "order_count"},
                   {"source": "totals", "property": "sum_gross_cents", "as": "sum_gross_cents"},
                   {"source": "totals", "property": "sum_net_amount", "as": "sum_net_amount"},
                   {"source": "pending", "property": "pending_order_count",
                    "as": "pending_order_count"}],
        "orderBy": [{"output": "customer_id", "direction": "asc", "nulls": "last"}],
        "pageSize": page_size,
    }


# ---------------------------------------------------------------------------
# Guards and the metadata canary (read-only counts on the OWN db container).
# ---------------------------------------------------------------------------
GUARD_CASES = {
    "permission_denied": {"expect_status": (403,), "expect_code": "MENU_ACCESS_DENIED",
                          "setup": "viewer role without the agent menu binding executes the query"},
    "cursor_invalid": {"expect_status": (400, 404, 410, 422), "expect_code": None,
                       "setup": "fetch with a malformed cursor"},
    "cursor_expired": {"expect_status": (410,), "expect_code": None,
                       "setup": "fetch after the snapshot expires_at window"},
    "owner_session_change": {"expect_status": (403, 404), "expect_code": None,
                             "setup": "reuse the admin cursor from another user's session"},
    "stale_release": {"expect_status": (400, 404, 409), "expect_code": None,
                      "setup": "execute against a superseded release_id explicitly"},
}


def evaluate_guard(case: str, status: int, body: Any) -> bool:
    expectation = GUARD_CASES[case]
    allowed = expectation["expect_status"]
    if status not in allowed:
        return False
    code = expectation["expect_code"]
    if code is None:
        return True
    detail = body.get("detail", body) if isinstance(body, dict) else None
    return isinstance(detail, dict) and detail.get("code") == code


def metadata_fingerprint() -> dict[str, int]:
    """Read-only row counts of the canary tables on the OWN ledger db container."""
    cid = fixture.require_fed("db")
    names = list(CANARY_ZERO_DELTA_TABLES)
    union = " UNION ALL ".join(f"SELECT '{t}' AS t, COUNT(*) AS n FROM {t}" for t in names)
    output = fixture.base.run(["docker", "exec", cid, "psql", "-X", "-q", "-A", "-t",
                               "-U", "integrated_e2e", "-d", METADATA_DB,
                               "-c", union + ";"])
    fingerprint = {}
    for line in output.splitlines():
        if "|" in line:
            table, count = line.split("|")
            fingerprint[table] = int(count)
    return fingerprint


def persistence_canary(before: dict[str, int], after: dict[str, int]) -> dict[str, Any]:
    """Require zero query-table growth; counts alone never prove no persistence."""
    missing = sorted(set(CANARY_ZERO_DELTA_TABLES) - (set(before) & set(after)))
    zero = {t: after[t] - before[t] for t in CANARY_ZERO_DELTA_TABLES
            if t in before and t in after}
    passed = not missing and all(d == 0 for d in zero.values())
    return {"zero_delta_tables": zero, "missing_tables": missing,
            "zero_delta_violations": {t: d for t, d in zero.items() if d != 0},
            "row_counts_unchanged": passed,
            "no_detail_persistence_proven": False,
            "verdict": "pass" if passed else "fail",
            "note": "row-count signal only; unchanged contents, logs, object storage and TEMP/WAL "
                    "require separate evidence"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _resolve_api_base(args: Any) -> str:
    """--api-base is valid before OR after the subcommand.

    The shared parent registers argparse.SUPPRESS: a plain default would be
    re-applied by the subparser and clobber a pre-subcommand value (the
    classic argparse subparser-default bug), so the option is only written
    into the namespace when it actually appears on the command line.
    """
    return getattr(args, "api_base", "") or os.environ.get("FEDERATED_ACCEPTANCE_API_BASE", "")


def _phase_timeout(command: str) -> float:
    """Per-call HTTP timeout for query phases (client-side only; no server
    budget change). Defaults stay consistent with the source execution
    deadline, guards also waits out snapshot expiry; explicit env wins."""
    default = 360.0 if command == "guards" else 300.0
    override = os.environ.get("FEDERATED_ACCEPTANCE_API_TIMEOUT", "").strip()
    return float(override) if override else default


def build_report(name: str, state: dict[str, Any]) -> dict[str, Any]:
    """Metric-only roll-up of the front-door, query and guards artifacts.

    Artifacts must exist and carry passing results for
    functional_acceptance_pass; full_e2e_pass stays False because broader
    evidence (baseline processes outside compose, shared-VM budget checks,
    log/object-storage/WAL probes, the remaining coordinated scale matrix)
    is only owned by the coordinated matrix. No result rows are copied.
    """
    paths = {"front_door": RUNS / f"{name}.json",
             "query": RUNS / f"{name}-query.json",
             "guards": RUNS / f"{name}-guards.json"}
    missing = [key for key, path in paths.items() if not path.exists()]
    query = json.loads(paths["query"].read_text(encoding="utf-8")) if paths["query"].exists() else None
    guards = json.loads(paths["guards"].read_text(encoding="utf-8")) if paths["guards"].exists() else None
    front_door = state.get("front_door_complete") is True
    query_pass = bool(query and query.get("query_result") == "pass")
    guards_pass = bool(guards and guards.get("guards_result") == "pass")
    return {
        "name": name,
        "artifacts": {key: path.name for key, path in paths.items()},
        "missing_artifacts": missing,
        "front_door_complete": front_door,
        "query": None if query is None else {
            "query_result": query.get("query_result"),
            "rows_observed": query.get("rows_observed"),
            "mismatch_count": query.get("mismatch_count"),
            "failure": query.get("failure"),
            "mismatches_sample": (query.get("mismatches") or [])[:10],
            "pagination_meta": query.get("pagination_meta"),
            "metadata_canary": query.get("metadata_canary"),
            "source_canary": query.get("source_canary")},
        "guards": None if guards is None else {
            "guards_result": guards.get("guards_result"),
            "passed": guards.get("passed"), "skipped": guards.get("skipped"),
            "guards": guards.get("guards")},
        "functional_acceptance_pass": front_door and query_pass and guards_pass,
        "full_e2e_pass": False,
        "unresolved_broader_evidence": [
            "required baseline processes outside compose have no PID/job evidence: "
            + ", ".join(fixture.REQUIRED_BASELINE_OUTSIDE_COMPOSE),
            "platform memory budget is unverifiable while the Docker VM is shared",
            "no-detail-persistence beyond row-count canaries: logs, object storage, TEMP/WAL",
            "remaining coordinated scale matrix and staging sign-off"],
        "note": "final acceptance sign-off belongs to the coordinated matrix"}


def main() -> int:
    common = argparse.ArgumentParser(add_help=False)
    # default=SUPPRESS: a normal default here would be re-applied by each
    # subparser (parents=[common]) and clobber a pre-subcommand --api-base.
    common.add_argument("--api-base", default=argparse.SUPPRESS,
                        help="coordinated loopback API base (reserved port, e.g. "
                             "http://127.0.0.1:52004; env FEDERATED_ACCEPTANCE_API_BASE)")
    parser = argparse.ArgumentParser(description=__doc__, parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("contract-status", parents=[common])
    planning = sub.add_parser("plan")
    planning.add_argument("--orders", type=int, required=True)
    planning.add_argument("--customers", default="", help="comma-separated ids, or * for all")
    for name in ("front-door", "query", "guards"):
        entry = sub.add_parser(name, parents=[common])
        entry.add_argument("--name", required=True)
        entry.add_argument("--orders", type=int, required=True)
        entry.add_argument("--admin-user", default="admin")
        entry.add_argument("--admin-password", required=True)
        if name == "guards":
            entry.add_argument("--viewer-user", default="")
            entry.add_argument("--viewer-password", default="")
            entry.add_argument("--other-user", default="")
            entry.add_argument("--other-password", default="")
            entry.add_argument("--max-expiry-wait", type=float, default=360.0)
    report = sub.add_parser("report")
    report.add_argument("--name", required=True)
    args = parser.parse_args()

    def write(name: str, payload: Any) -> None:
        RUNS.mkdir(parents=True, exist_ok=True)
        (RUNS / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str)
                                 + "\n", encoding="utf-8")

    try:
        if args.command == "contract-status":
            result = {name: {"method": entry["method"], "path": entry["path"]}
                      for name, entry in sorted(CONTRACT.items())}
        elif args.command == "plan":
            ids = ["*"] if args.customers.strip() == "*" else \
                [int(value) for value in args.customers.split(",") if value.strip()]
            if ids and ids != ["*"] and len(ids) > 500:
                raise ValueError("customer sample capped at 500 ids")
            result = plan_queries(args.orders, ids)
            write(f"plan-{args.orders}.json", result)
        elif args.command == "front-door":
            require_contract("front-door")
            api_base = _resolve_api_base(args)
            if not api_base:
                raise ValueError("--api-base or FEDERATED_ACCEPTANCE_API_BASE is required "
                                 "(reserved loopback port; the API itself must already run)")
            api = Api(api_base)
            api.login(args.admin_user, args.admin_password)
            run = AcceptanceRun(args.name, api, args.orders)
            result = run.run()
        elif args.command in ("query", "guards"):
            require_contract(args.command)
            api_base = _resolve_api_base(args)
            if not api_base:
                raise ValueError("--api-base or FEDERATED_ACCEPTANCE_API_BASE is required")
            path = RUNS / f"{args.name}.json"
            if not path.exists():
                raise RuntimeError("run the front-door phase first")
            state = json.loads(path.read_text(encoding="utf-8"))
            if not state.get("front_door_complete"):
                raise RuntimeError("front-door phase must complete before query/guards")
            api = Api(api_base, timeout=_phase_timeout(args.command))
            if args.command == "query":
                result = execute_query_phase(api, state, args)
                write(f"{args.name}-query.json", result)
            else:
                result = execute_guards_phase(api, state, args)
                write(f"{args.name}-guards.json", result)
        else:
            path = RUNS / f"{args.name}.json"
            if not path.exists():
                raise RuntimeError("no such acceptance run")
            state = json.loads(path.read_text(encoding="utf-8"))
            result = build_report(args.name, state)
            write(f"{args.name}-report.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        if args.command == "query" and result["query_result"] != "pass":
            return 1
        if args.command == "guards" and result["guards_result"] != "pass":
            return 1
        if args.command == "report" and not result["functional_acceptance_pass"]:
            return 1
        return 0
    except ContractBlocked as error:
        print(f"federated-acceptance: {error}", file=sys.stderr)
        return 3
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"federated-acceptance: {error}", file=sys.stderr)
        return 1


def _type_ids(state: dict[str, Any]) -> dict[str, str]:
    return {name: type_id for key in OBJECT_MODEL
            for name, type_id in state["steps"]["define_workspace"][key]["object_types"].items()}


def execute_query_phase(api: Api, state: dict[str, Any], args: Any) -> dict[str, Any]:
    api.login(args.admin_user, args.admin_password)
    promote = state["steps"]["promote"]
    assertions = state["steps"]["join_assertions"]["evidence_refs"]
    crm_id = state["steps"]["create_ontologies"]["ontologies"]["crm"]["id"]
    erp_id = state["steps"]["create_ontologies"]["ontologies"]["erp"]["id"]
    before = metadata_fingerprint()
    request = build_canonical_query(promote["crm"]["release_id"], promote["erp"]["release_id"],
                                    assertions, crm_id, erp_id, _type_ids(state))
    rows, meta = collect_all_pages(api, request, max_pages=10_000)
    after = metadata_fingerprint()
    observed = {str(row.get("customer_id")): row for row in rows}
    expected = plan_queries(state["orders"], ["*"])["expected_canonical"]
    mismatches = compare_decimal_exact(expected, observed)
    if len(observed) != len(rows):
        mismatches.append("duplicate customer keys in the complete result")
    source_canary = fixture.canary(state["orders"])["all_canaries_match"]
    canary = persistence_canary(before, after)
    passed = not mismatches and meta.get("truncated") is False and meta.get("partial") is False \
        and canary["row_counts_unchanged"] and source_canary
    return {"rows_observed": len(rows), "pagination_meta": meta,
            "mismatches": mismatches[:50], "mismatch_count": len(mismatches),
            "metadata_canary": canary, "source_canary": source_canary,
            "query_result": "pass" if passed else "fail",
            "result": "query_phase_not_full_e2e", "full_e2e_pass": False}


def execute_guards_phase(api: Api, state: dict[str, Any], args: Any) -> dict[str, Any]:
    api.login(args.admin_user, args.admin_password)
    promote = state["steps"]["promote"]
    assertions = state["steps"]["join_assertions"]["evidence_refs"]
    crm_id = state["steps"]["create_ontologies"]["ontologies"]["crm"]["id"]
    erp_id = state["steps"]["create_ontologies"]["ontologies"]["erp"]["id"]
    results = {key: {"pass": None, "note": "not executed"} for key in GUARD_CASES}
    request = build_canonical_query(promote["crm"]["release_id"], promote["erp"]["release_id"],
                                    assertions, crm_id, erp_id, _type_ids(state), page_size=1)
    first_status, first = api.request("POST", "/api/v2/formal/agent/query", request,
                                      expect=(200, 422, 429, 503))
    data = _payload(first)
    cursor = data.get("cursor") if first_status == 200 else None
    establishment = {"status": first_status, "cursor_available": bool(cursor)}
    if first_status != 200:
        detail = first.get("detail", {}) if isinstance(first, dict) else {}
        establishment["failure"] = {key: detail[key] for key in
            ("code", "budget", "limitBytes", "quota") if key in detail} if isinstance(detail, dict) else {}
        for name in ("owner_session_change", "cursor_expired"):
            results[name]["note"] = "canonical query rejected; no result cursor was created"

    if args.viewer_user and args.viewer_password:
        viewer = Api(api.base_url)
        viewer.login(args.viewer_user, args.viewer_password)
        status, body = viewer.request("POST", "/api/v2/formal/agent/query", request, expect=(200, 403))
        results["permission_denied"] = {"status": status,
                                       "pass": evaluate_guard("permission_denied", status, body)}
    if cursor and args.other_user and args.other_password:
        other = Api(api.base_url)
        other.login(args.other_user, args.other_password)
        # Prove query access first so a menu denial cannot masquerade as
        # owner isolation. The proof must be scale-independent: pageSize alone
        # does NOT bound execution (the memory executor materializes the full
        # Order projection before paging and breached memory_execution_bytes
        # at 1M orders), so pin the deterministic primary key order_id=1 with
        # an eq filter; exactly one row materializes at any fixture scale.
        proof_status, proof_body = other.request(
            "POST", "/api/v2/formal/agent/query", {
                "sources": request["sources"][1]["subquery"]["sources"],
                "where": [{"source": "o", "property": "order_id", "op": "eq", "value": 1}],
                "select": [{"source": "o", "property": "order_id", "as": "order_id"}],
                "pageSize": 1},
            expect=(200, 403, 422))
        proof = _payload(proof_body)
        proof_rows = proof.get("pageRowCount") if isinstance(proof, dict) else None
        access_proof = {"status": proof_status, "pageRowCount": proof_rows}
        if proof_status == 200 and proof_rows == 1:
            status, body = other.request("POST", "/api/v2/formal/agent/query", {"cursor": cursor},
                                         expect=(200, 403, 404))
            results["owner_session_change"] = {"status": status,
                "pass": evaluate_guard("owner_session_change", status, body),
                "access_proof": access_proof,
                "scope": "direct HTTP owner isolation; assistant session isolation is a separate gate"}
        else:
            results["owner_session_change"] = {"pass": None, "status": proof_status,
                "access_proof": access_proof,
                "note": "bounded other-user access proof did not return the pinned "
                        "order_id=1 row; a cursor rejection now would be a menu or "
                        "budget denial, not owner isolation"}

    status, body = api.request("POST", "/api/v2/formal/agent/query", {"cursor": "0" * 16},
                               expect=(400, 404, 410, 422))
    results["cursor_invalid"] = {"status": status,
                                 "pass": evaluate_guard("cursor_invalid", status, body)}
    stale = build_canonical_query("00000000-0000-0000-0000-000000000000",
                                  promote["erp"]["release_id"], assertions, crm_id, erp_id,
                                  _type_ids(state), page_size=1)
    status, body = api.request("POST", "/api/v2/formal/agent/query", stale,
                               expect=(200, 400, 404, 409))
    results["stale_release"] = {"status": status,
                                "pass": evaluate_guard("stale_release", status, body)}
    if cursor and data.get("expiresAt"):
        expiry = datetime.fromisoformat(data["expiresAt"].replace("Z", "+00:00"))
        wait = max(0, (expiry - datetime.now(timezone.utc)).total_seconds()) + 0.1
        if wait <= args.max_expiry_wait:
            time.sleep(wait)
            status, body = api.request("POST", "/api/v2/formal/agent/query", {"cursor": cursor},
                                       expect=(200, 400, 404, 410))
            results["cursor_expired"] = {"status": status,
                "pass": evaluate_guard("cursor_expired", status, body), "waited_seconds": wait}
        else:
            results["cursor_expired"]["note"] = "actual expiry exceeds the allowed wait"
    passed = [key for key, value in results.items() if value["pass"] is True]
    skipped = [key for key, value in results.items() if value["pass"] is None]
    return {"guards": results, "passed": passed, "skipped": skipped,
            "cursor_establishment": establishment,
            "guards_result": "pass" if len(passed) == len(GUARD_CASES) else "incomplete_or_failed",
            "result": "guards_phase_not_full_e2e", "full_e2e_pass": False}


if __name__ == "__main__":
    raise SystemExit(main())
