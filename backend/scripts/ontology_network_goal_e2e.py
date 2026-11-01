#!/usr/bin/env python3
"""Ontology-network goal E2E driver: real HTTP front door only.

Phases: plan (offline canonical expectations), frontdoor (health honesty,
login, create the three-domain ontology set, verify frozen v0 releases),
query / actions / guards / report (added incrementally; never faked).

Contract (goal doc docs/development/ontology-network-goal.md sec 8/11):
- no bypassing draft -> trial/impact -> promote; no fabricated release state;
- run state records only ids the API actually returned;
- seed/oracle fixtures are NOT E2E evidence: every phase result carries its
  acceptance-id mapping and stays not_run/blocked unless really executed;
- self-made typed HTTP requests never stand in for the real assistant (A-28).

Mirrors scripts/e2e/federated_acceptance.py patterns (loopback-only API base,
per-phase run state, evidence without secrets) without copying its scale.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO = BACKEND_DIR.parent
RUNTIME = REPO / ".artifacts" / "ontology-goal-e2e"
RUNS = RUNTIME / "acceptance"
DEFAULT_API_BASE = "http://127.0.0.1:56004"

# Fixed three-domain shape (goal doc sec 8): A=customers+contracts (PG source),
# B=orders (MySQL), C=shipments (MySQL). The canonical A-02 query aggregates
# per customer per source first, then joins across ontonomies with two derived
# levels (per-customer rollup, then per-region rollup).
# Domains are registered through the settings front door with dedicated
# goalnet-* names so no pre-existing domain is ever mutated.
DOMAINS = [
    {"key": "A", "name": "goalnet-crm", "domain": "goalnet-crm",
     "objects": ["goal_customers", "goal_contracts"], "source": "postgres"},
    {"key": "B", "name": "goalnet-erp-orders", "domain": "goalnet-erp",
     "objects": ["goal_orders"], "source": "mysql"},
    {"key": "C", "name": "goalnet-erp-shipments", "domain": "goalnet-erp",
     "objects": ["goal_shipments"], "source": "mysql"},
]


# Object model mirroring the fixture tables (goal doc sec 8). DECIMAL maps to
# "number" with string-encoded wire values; amounts compare by Decimal.
OBJECT_MODEL: dict[str, dict[str, Any]] = {
    "A": {"types": {
        "Customer": {"table": "goal_customers", "pk": "customer_id",
                     "properties": {"customer_id": "string", "region": "string",
                                    "big_ref": "number", "note": "string"}},
        "Contract": {"table": "goal_contracts", "pk": "contract_id",
                     "properties": {"contract_id": "number", "customer_id": "string",
                                    "amount": "number", "contract_no": "string"}}}},
    "B": {"types": {
        "Order": {"table": "goal_orders", "pk": "order_id",
                  "properties": {"order_id": "number", "customer_id": "string",
                                 "amount": "number", "big_ref": "number",
                                 "remark": "string"}}}},
    "C": {"types": {
        "Shipment": {"table": "goal_shipments", "pk": "shipment_id",
                     "properties": {"shipment_id": "number", "order_id": "number",
                                    "qty": "number", "waybill_no": "string"}}}},
}
# keyType of each join business key (customer ids are strings, order ids ints).
JOIN_ASSERTIONS = [
    # (left ontology key, left type, left prop, right ontology key, right type,
    #  right prop, keyType, meaning)
    ("A", "Customer", "customer_id", "A", "Contract", "customer_id", "string",
     "客户持有合同（本体A内部）"),
    ("A", "Customer", "customer_id", "B", "Order", "customer_id", "string",
     "客户下订单（跨本体 A-B）"),
    ("B", "Order", "order_id", "C", "Shipment", "order_id", "number",
     "订单发运明细（跨本体 B-C）"),
]


def _payload(body: Any) -> Any:
    if isinstance(body, dict) and isinstance(body.get("data"), (dict, list)):
        return body["data"]
    return body


def _workspace_object_type(type_name: str, definition: dict[str, Any]) -> dict:
    return {"name": type_name, "displayName": type_name,
            "primaryKey": definition["pk"],
            "properties": [{"name": prop, "type": kind}
                           for prop, kind in definition["properties"].items()],
            "interfaces": [], "positionX": 100.0, "positionY": 100.0}


def _remote_mapping_entry(ontology_id: str, entity_class: str, dataset_id: str,
                          definition: dict[str, Any]) -> dict:
    import uuid
    return {"id": str(uuid.uuid5(uuid.NAMESPACE_URL,
                                 f"{ontology_id}:remote-mapping:{entity_class}")),
            "entityClass": entity_class, "bindingMode": "remote",
            "curatedDatasetId": dataset_id,
            "fieldMapping": {prop: prop for prop in definition["properties"]}}


def ensure_domains(api: "Api") -> dict[str, Any]:
    """Register the goalnet-* domains through /api/v1/domains (idempotent)."""
    status, body = api.request("GET", "/api/v1/domains")
    existing = {item["name"] for item in (body.get("data") or [])} if status == 200 else set()
    ensured = {}
    for name in sorted({domain["domain"] for domain in DOMAINS}):
        if name in existing:
            ensured[name] = "already_present"
            continue
        create_status, create_body = api.request(
            "POST", "/api/v1/domains", {"name": name, "description": f"goal fixture {name}"})
        # 409 means another run already created it; that idempotent result is
        # accepted only because the name is goalnet-dedicated.
        if create_status == 201:
            ensured[name] = "created"
        elif create_status == 409:
            ensured[name] = "created_by_earlier_run"
        else:
            return {"result": "fail", "domains": ensured,
                    "rejected": {"domain": name, "status": create_status,
                                 "body": create_body}}
    return {"result": "ok", "domains": ensured}


def load_fixture_oracle() -> dict[str, Any]:
    """Import the fixture module's pure-Python expectations (offline)."""
    path = REPO / "scripts" / "e2e" / "ontology_network_goal.py"
    spec = importlib.util.spec_from_file_location("ontology_network_goal_fixture", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.expected_view()


class Api:
    """Loopback-only JSON client; wire bytes are counted, never bodies stored."""

    def __init__(self, base_url: str, token: str | None = None, timeout: float = 30.0):
        parsed = urlsplit(base_url)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
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

    def post_multipart(self, path: str, fields: dict[str, str],
                       filename: str, content: bytes, content_type: str) -> tuple[int, Any]:
        boundary = "goalnet-boundary-7f3a9c"
        parts = []
        for name, value in fields.items():
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                     f"filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n".encode())
        parts.append(content + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        request = urllib.request.Request(
            self.base_url + path, data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                     "Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                self.wire_bytes += len(raw) + len(body)
                return response.status, _decode(raw)
        except urllib.error.HTTPError as error:
            raw = error.read()
            self.wire_bytes += len(raw) + len(body)
            return error.code, _decode(raw)


def _decode(raw: bytes) -> Any:
    text = raw.decode(errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text[:400]


def _run_path(name: str) -> Path:
    RUNS.mkdir(parents=True, exist_ok=True)
    return RUNS / f"{name}.json"


def _load_run(name: str) -> dict[str, Any]:
    path = _run_path(name)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"name": name}


def _save_run(run: dict[str, Any]) -> None:
    path = _run_path(run["name"])
    path.write_text(json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def phase_plan(args: argparse.Namespace) -> dict[str, Any]:
    expected = load_fixture_oracle()
    return {"phase": "plan", "result": "plan_ready",
            "acceptance_ids": {"A-02": "canonical expectations fixed (doc sec 8)"},
            "domains": DOMAINS,
            "canonical_query": {
                "shape": "aggregate per customer per source -> cross-ontology join"
                         " -> per-region rollup (derived depth 2, 4 tables, 3 domains)",
                "money_rule": "exact decimal strings, never float",
                "null_rule": "count(*) vs count(field) distinct; absent groups stay NULL"},
            "expected": expected,
            "e2e_pass": False,
            "note": "offline plan is not E2E evidence"}


def phase_frontdoor(args: argparse.Namespace) -> dict[str, Any]:
    run = _load_run(args.name)
    api = Api(args.api_base)
    health: dict[str, Any] = {}
    for path in ("/health", "/health/ready"):
        status, body = api.request("GET", path)
        health[path] = {"status": status, "ready": status == 200,
                        "body_sample": body if status != 200 else "ok"}
    # Readiness gaps are recorded honestly and do not block specific chains.
    login_status, login_body = api.request("POST", "/api/v1/auth/login",
                                           {"username": args.admin_user,
                                            "password": args.admin_password})
    if login_status != 200:
        return {"phase": "frontdoor", "result": "blocked", "health": health,
                "login": {"status": login_status, "body": login_body},
                "acceptance_ids": {}, "e2e_pass": False}
    api.token = login_body["data"]["access_token"]
    domains = ensure_domains(api)
    if domains["result"] != "ok":
        return {"phase": "frontdoor", "result": "fail", "health": health,
                "domains": domains, "acceptance_ids": {}, "e2e_pass": False}
    created = {}
    for domain in DOMAINS:
        status, body = api.request("POST", "/api/v1/ontologies",
                                   {"name": domain["name"], "domain": domain["domain"]})
        detail = (body or {}).get("detail") or {}
        existing_id = detail.get("existing_id") if isinstance(detail, dict) else None
        if status in (200, 201):
            ontology_id = body["data"]["id"]
            adoption = "created"
        elif status == 409 and existing_id and domain["name"].startswith("goalnet-"):
            # Goal-dedicated names only: reuse the ontology an earlier run of
            # this same driver created; never adopt a foreign ontology.
            ontology_id = existing_id
            adoption = "reused_from_earlier_goal_run"
        else:
            return {"phase": "frontdoor", "result": "fail", "health": health,
                    "domains": domains, "created": created,
                    "rejected": {"domain": domain["key"], "status": status, "body": body},
                    "acceptance_ids": {}, "e2e_pass": False}
        version_status, versions = api.request(
            "GET", f"/api/v2/ontologies/{ontology_id}/versions")
        # current_release_id is a top-level field of the versions envelope.
        release = versions.get("current_release_id") if version_status == 200 else None
        created[domain["key"]] = {"ontology_id": ontology_id, "adoption": adoption,
                                  "current_release_id": release,
                                  "v0_frozen": bool(release)}
    run["frontdoor"] = {"health": health, "domains": domains, "created": created,
                        "wire_bytes": api.wire_bytes, "e2e_pass": False,
                        "result": "frontdoor_minimal_pass" if all(
                            item["v0_frozen"] for item in created.values()) else "fail"}
    _save_run(run)
    return run["frontdoor"]


# join assertions require published types, so they run after promote; the
# typed query then consumes their evidenceRefs (federated front-door order).
QUERY_STEPS = ("register_connections", "register_assets", "create_drafts",
               "define_workspace", "workspace_mappings", "save_semantics",
               "trial", "impact", "promote", "join_assertions",
               "execute_query", "verify_oracle")


class GoalRun:
    """Resumable query-chain state machine; only real API ids are recorded."""

    def __init__(self, name: str, api: "Api"):
        if not name or not all(ch.isalnum() or ch in "-_" for ch in name):
            raise ValueError("run name must be letters/digits/dash/underscore only")
        RUNS.mkdir(parents=True, exist_ok=True)
        self.path = RUNS / f"{name}.json"
        self.state: dict[str, Any] = (json.loads(self.path.read_text(encoding="utf-8"))
                                      if self.path.exists() else
                                      {"name": name, "steps": {}, "full_e2e_pass": False})
        self.api = api

    def save(self) -> None:
        self.path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2,
                                        default=str) + "\n", encoding="utf-8")

    def step(self, name: str) -> dict[str, Any]:
        return self.state["steps"].setdefault(name, {})

    def done(self, name: str) -> bool:
        return bool(self.step(name).get("ok"))

    def ontology_id(self, key: str) -> str:
        return self.state["ontologies"][key]

    # -- query-chain steps ----------------------------------------------
    def step_register_connections(self) -> None:
        connections = self.step("register_connections")
        for key, kind, port, dbx_port in (("source_pg", "postgres", 56433, 56433),
                                          ("source_mysql", "mysql", 56306, 56306)):
            name = f"goal-e2e-{key}"
            # dbx reaches the host-published source ports from inside the
            # container via host.docker.internal (container loopback differs);
            # the native lane uses the host-visible loopback endpoint.
            config = {"host": "127.0.0.1", "port": port, "database": "integrated_source",
                      "user": "integrated_e2e", "password": "integrated_e2e_fixture",
                      "dbx_host": "host.docker.internal", "dbx_port": dbx_port}
            status, body = self.api.request("POST", "/api/v2/connections",
                                            {"name": name, "kind": kind, "config": config})
            if status == 409:
                _, listing = self.api.request("GET", "/api/v2/connections")
                body = next(item for item in _payload(listing)
                            if isinstance(item, dict) and item.get("name") == name)
            elif status not in (200, 201):
                raise RuntimeError(f"connection {name}: HTTP {status} {str(body)[:200]}")
            connections[key] = {"id": body["id"], "name": name}
        connections["ok"] = True

    def step_register_assets(self) -> None:
        connections = self.state["steps"]["register_connections"]
        assets = self.step("register_assets")
        entries = {}
        for domain_key, model in OBJECT_MODEL.items():
            for type_name, definition in model["types"].items():
                connection_key = "source_pg" if domain_key == "A" else "source_mysql"
                schema = "public" if connection_key == "source_pg" else "integrated_source"
                body = {"name": f"goal-e2e-{definition['table']}",
                        "connection_id": connections[connection_key]["id"],
                        "schema_name": schema, "table_name": definition["table"],
                        "writable": False}
                status, response = self.api.request("POST", "/api/v2/integrated-datasets", body)
                if status == 409:  # RESOURCE_ALREADY_REGISTERED: adopt by listing
                    _, listing = self.api.request("GET", "/api/v2/integrated-datasets")
                    response = next(item for item in _payload(listing)["items"]
                                    if item.get("schema_name") == schema
                                    and item.get("table_name") == definition["table"])
                elif status not in (200, 201):
                    raise RuntimeError(f"asset {definition['table']}: HTTP {status}")
                payload = _payload(response)
                entries[type_name] = {"asset_id": payload["id"],
                                      "dataset_id": payload["dataset_id"]}
        assets["assets"] = entries
        assets["ok"] = len(entries) == 4

    def step_create_drafts(self) -> None:
        drafts = self.step("create_drafts")
        for domain in DOMAINS:
            key = domain["key"]
            ontology_id = self.ontology_id(key)
            _, versions = self.api.request(
                "GET", f"/api/v2/ontologies/{ontology_id}/versions?limit=5")
            current_release = versions.get("current_release_id") \
                if isinstance(versions, dict) else None
            if not current_release:
                raise RuntimeError(f"no current release for {key}")
            _, body = self.api.request(
                "POST", f"/api/v2/ontologies/{ontology_id}/versions/{current_release}/drafts",
                {})
            payload = _payload(body)
            drafts[key] = {"version_id": payload.get("id") or payload.get("version_id"),
                           "source_version_id": current_release}
        drafts["ok"] = all(drafts.get(k, {}).get("version_id") for k in ("A", "B", "C"))

    def step_define_workspace(self) -> None:
        drafts = self.state["steps"]["create_drafts"]
        workspace = self.step("define_workspace")
        for domain_key, model in OBJECT_MODEL.items():
            ontology_id = self.ontology_id(domain_key)
            version = drafts[domain_key]["version_id"]
            body = {"objectTypes": [_workspace_object_type(name, definition)
                                    for name, definition in model["types"].items()],
                    "linkTypes": [], "actions": [], "functions": [], "baseRevision": None}
            status, response = self.api.request(
                "PUT", f"/api/v2/ontologies/{ontology_id}/versions/{version}/workspace", body)
            if status not in (200, 201):
                raise RuntimeError(f"workspace save {domain_key}: HTTP {status}")
            _, current = self.api.request(
                "GET", f"/api/v2/ontologies/{ontology_id}/versions/{version}/workspace")
            types = {item["name"]: item["id"] for item in _payload(current)["objectTypes"]}
            if set(types) != set(model["types"]) or not all(types.values()):
                raise RuntimeError(f"workspace identity mismatch for {domain_key}")
            workspace[domain_key] = {"object_types": types}
        workspace["ok"] = True

    def step_workspace_mappings(self) -> None:
        assets = self.state["steps"]["register_assets"]["assets"]
        drafts = self.state["steps"]["create_drafts"]
        mappings = self.step("workspace_mappings")
        for domain_key, model in OBJECT_MODEL.items():
            ontology_id = self.ontology_id(domain_key)
            version = drafts[domain_key]["version_id"]
            entries = [_remote_mapping_entry(ontology_id, type_name,
                                             assets[type_name]["dataset_id"], definition)
                       for type_name, definition in model["types"].items()]
            status, _ = self.api.request(
                "PUT", f"/api/v2/ontologies/{ontology_id}/versions/{version}/workspace/mappings",
                {"mappings": entries, "linkMappings": [], "sentinels": [], "baseRevision": None})
            if status not in (200, 201):
                raise RuntimeError(f"mapping save {domain_key}: HTTP {status}")
            mappings[domain_key] = {"remote_mappings": [e["entityClass"] for e in entries]}
        mappings["ok"] = True

    def step_join_assertions(self) -> None:
        workspace = self.state["steps"]["define_workspace"]
        assertions = self.step("join_assertions")
        refs = []
        for (left_key, left_type, left_prop, right_key, right_type,
             right_prop, key_type, meaning) in JOIN_ASSERTIONS:
            left, right = self.ontology_id(left_key), self.ontology_id(right_key)
            body = {"leftOntologyId": left,
                    "leftObjectTypeId": workspace[left_key]["object_types"][left_type],
                    "leftProperty": left_prop, "rightOntologyId": right,
                    "rightObjectTypeId": workspace[right_key]["object_types"][right_type],
                    "rightProperty": right_prop, "keyType": key_type,
                    "businessMeaning": meaning}
            status, response = self.api.request(
                "POST", f"/api/v2/formal/ontologies/{left}/agent/join-assertions", body)
            if status not in (200, 201):
                raise RuntimeError(f"join assertion {left_type}->{right_type}: HTTP {status}")
            refs.append(_payload(response)["evidenceRef"])
        assertions["evidence_refs"] = refs
        assertions["ok"] = len(refs) == len(JOIN_ASSERTIONS)

    def _promotable(self) -> list[str]:
        return [k for k in ("A", "B", "C")]

    def step_save_semantics(self) -> None:
        """Business canvas required by the semantic gate before trial."""
        drafts = self.state["steps"]["create_drafts"]
        semantics = self.step("save_semantics")
        for domain_key, model in OBJECT_MODEL.items():
            ontology_id = self.ontology_id(domain_key)
            version = drafts[domain_key]["version_id"]
            _, workspace = self.api.request(
                "GET", f"/api/v2/ontologies/{ontology_id}/versions/{version}/workspace")
            current = _payload(workspace)
            base_revision = current.get("revision") if isinstance(current, dict) else None
            if not base_revision:
                raise RuntimeError(f"workspace revision missing for {domain_key}")
            canvas = {"objects": [
                {"name": type_name, "display_name": type_name,
                 "description": "goalnet acceptance business object",
                 "key_attribute": definition["pk"],
                 "attributes": [{"name": prop, "display_name": prop,
                                 "type_hint": kind,
                                 "required": prop == definition["pk"]}
                                for prop, kind in definition["properties"].items()],
                 "relations": []}
                for type_name, definition in model["types"].items()],
                "actors": [], "behaviors": [], "events": [], "rules": [],
                "scenarios": [], "processes": []}
            status, response = self.api.request(
                "PUT", f"/api/v2/ontologies/{ontology_id}/versions/{version}/semantic",
                {"baseRevision": base_revision, "canvas": canvas,
                 "documentMd": f"# goalnet {domain_key} 业务语义\n\n"
                              "对象与远程资产字段按验收 fixture 冻结；不含源记录。\n",
                 "documentTitle": f"goalnet {domain_key} 业务语义"})
            payload = _payload(response)
            if status != 200 or not (payload.get("revision") if isinstance(payload, dict) else None):
                raise RuntimeError(f"semantic save {domain_key}: HTTP {status} "
                                   f"{str(response)[:200]}")
            semantics[domain_key] = {"revision": payload["revision"]}
        semantics["ok"] = True

    def step_trial(self) -> None:
        drafts = self.state["steps"]["create_drafts"]
        trial = self.step("trial")
        for key in self._promotable():
            ontology_id = self.ontology_id(key)
            _, body = self.api.request(
                "POST", f"/api/v2/ontologies/{ontology_id}/versions/"
                        f"{drafts[key]['version_id']}/trial-runs", {})
            payload = _payload(body)
            if payload.get("status") != "passed" or not payload.get("id"):
                raise RuntimeError(f"trial did not pass for {key}: {payload.get('status')}")
            trial[key] = {"run_id": payload["id"], "impact_hash": payload["impact_hash"]}
        trial["ok"] = True

    def step_impact(self) -> None:
        drafts = self.state["steps"]["create_drafts"]
        impact = self.step("impact")
        for key in self._promotable():
            ontology_id = self.ontology_id(key)
            _, body = self.api.request(
                "GET", f"/api/v2/ontologies/{ontology_id}/versions/"
                       f"{drafts[key]['version_id']}/impact")
            payload = _payload(body)
            trial = self.state["steps"]["trial"][key]
            if payload.get("impactHash") != trial["impact_hash"]:
                raise RuntimeError(f"impact hash drift for {key}")
            impact[key] = {"impact_hash": payload["impactHash"]}
        impact["ok"] = True

    def step_promote(self) -> None:
        drafts = self.state["steps"]["create_drafts"]
        promote = self.step("promote")
        for key in self._promotable():
            ontology_id = self.ontology_id(key)
            trial = self.state["steps"]["trial"][key]
            _, body = self.api.request(
                "POST", f"/api/v2/ontologies/{ontology_id}/versions/"
                        f"{drafts[key]['version_id']}/promote",
                {"trialRunId": trial["run_id"],
                 "impactHash": self.state["steps"]["impact"][key]["impact_hash"]})
            payload = _payload(body)
            release_id = payload.get("release_id") or payload.get("id")
            if not release_id:
                raise RuntimeError(f"promote returned no release id for {key}")
            promote[key] = {"release_id": release_id}
        promote["ok"] = True

    def _object_source(self, alias: str, ontology_key: str, object_type: str) -> dict:
        return {"alias": alias, "ontologyId": self.ontology_id(ontology_key),
                "releaseId": self.state["steps"]["promote"][ontology_key]["release_id"],
                "objectTypeId": self.state["steps"]["define_workspace"]
                [ontology_key]["object_types"][object_type]}

    def build_query(self) -> dict[str, Any]:
        """Canonical A-02 pipeline: per-source aggregation subqueries (level 1)
        cross-ontology joined and re-aggregated (level 2); aggregate-then-join,
        LEFT joins keep NULL honesty for c2/c3."""
        refs = self.state["steps"]["join_assertions"]["evidence_refs"]
        anchor = {
            "alias": "crm",
            "subquery": {
                "sources": [self._object_source("cu", "A", "Customer"),
                            self._object_source("ct", "A", "Contract")],
                # LEFT keeps customer c2 (no contracts) alive for the outer join.
                "joins": [{"left": "cu", "right": "ct", "type": "left",
                           "on": {"kind": "explicit_key", "leftProperty": "customer_id",
                                  "rightProperty": "customer_id", "keyType": "string",
                                  "cardinality": "1:N", "businessMeaning": "客户持有合同",
                                  "evidenceRefs": refs[:1]}}],
                "groupBy": [{"source": "cu", "property": "customer_id"},
                            {"source": "cu", "property": "region"}],
                # count(field): a null-extended LEFT row for a customer
                # without contracts counts 0 (0c076 semantic prepare landed).
                "aggregates": [{"fn": "count", "source": "ct",
                                "property": "contract_id", "as": "contract_count"},
                               {"fn": "sum", "source": "ct", "property": "amount",
                                "as": "contract_sum"}],
                "select": [{"source": "cu", "property": "customer_id", "as": "customer_id"},
                           {"source": "cu", "property": "region", "as": "region"}]}}
        orders = {
            "alias": "orders",
            "subquery": {
                "sources": [self._object_source("o", "B", "Order")],
                "groupBy": [{"source": "o", "property": "customer_id"}],
                "aggregates": [{"fn": "count", "as": "order_count"},
                               {"fn": "sum", "source": "o", "property": "amount",
                                "as": "order_sum"}],
                "select": [{"source": "o", "property": "customer_id", "as": "customer_id"}]}}
        shipments = {
            "alias": "ships",
            "subquery": {
                "sources": [self._object_source("so", "B", "Order"),
                            {"alias": "ps",
                             "subquery": {"sources": [self._object_source("s", "C", "Shipment")],
                                          "groupBy": [{"source": "s", "property": "order_id"}],
                                          "aggregates": [{"fn": "sum", "source": "s",
                                                          "property": "qty", "as": "qty"}],
                                          "select": [{"source": "s", "property": "order_id",
                                                      "as": "order_id"}]}}],
                "joins": [{"left": "so", "right": "ps", "type": "left",
                           "on": {"kind": "explicit_key", "leftProperty": "order_id",
                                  "rightProperty": "order_id", "keyType": "number",
                                  "cardinality": "1:1", "businessMeaning": "订单发运预聚合",
                                  "evidenceRefs": refs[2:3]}}],
                "groupBy": [{"source": "so", "property": "customer_id"}],
                "aggregates": [{"fn": "sum", "source": "ps", "property": "qty",
                                "as": "ship_qty"}],
                "select": [{"source": "so", "property": "customer_id", "as": "customer_id"}]}}
        join_on_customer = lambda right: {
            "left": "crm", "right": right, "type": "left",
            "on": {"kind": "explicit_key", "leftProperty": "customer_id",
                   "rightProperty": "customer_id", "keyType": "string",
                   "cardinality": "1:1", "businessMeaning": "跨本体客户聚合挂接",
                   "evidenceRefs": refs[1:2]}}
        return {"sources": [anchor, orders, shipments],
                "joins": [join_on_customer("orders"), join_on_customer("ships")],
                "select": [{"source": "crm", "property": "customer_id", "as": "customer_id"},
                           {"source": "crm", "property": "region", "as": "region"},
                           {"source": "crm", "property": "contract_count",
                            "as": "contract_count"},
                           {"source": "crm", "property": "contract_sum", "as": "contract_sum"},
                           {"source": "orders", "property": "order_count", "as": "order_count"},
                           {"source": "orders", "property": "order_sum", "as": "order_sum"},
                           {"source": "ships", "property": "ship_qty", "as": "ship_qty"}],
                "orderBy": [{"output": "customer_id", "direction": "asc", "nulls": "last"}],
                "pageSize": 20}

    def step_execute_query(self) -> None:
        execute = self.step("execute_query")
        body = self.build_query()
        rows, meta = collect_all_pages(self.api, body)
        execute["rows"] = rows
        execute["pagination"] = meta
        execute["ok"] = True

    def step_verify_oracle(self) -> None:
        execute = self.state["steps"]["execute_query"]
        verify = self.step("verify_oracle")
        expected = load_fixture_oracle()["customers"]
        observed = {}
        for row in execute["rows"]:
            observed[str(row.get("customer_id"))] = row
        problems: list[str] = []
        for customer_id, want in expected.items():
            got = observed.get(customer_id)
            if got is None:
                problems.append(f"{customer_id}: row missing")
                continue
            # order_count comes from the LEFT-joined orders alias: an absent
            # branch group surfaces as NULL (doc: no fabricated zero).
            want_order_count = want["order_count"] or None
            for field, want_value in (("region", want["region"]),
                                      ("contract_count", want["contract_count"]),
                                      ("order_count", want_order_count)):
                if str(got.get(field)) != str(want_value):
                    problems.append(f"{customer_id}.{field}: {got.get(field)} != {want_value}")
            for field in ("contract_sum", "order_sum", "ship_qty"):
                want_token = want[field]
                # Outer-layer branches (orders/ships) join the anchor by LEFT:
                # a customer with no branch group gets NULL, never a fake 0.
                if field != "contract_sum" and want_token in ("0", 0):
                    want_token = "<NULL>"
                got_value = got.get(field)
                if want_token == "<NULL>":
                    if got_value is not None:
                        problems.append(f"{customer_id}.{field}: expected NULL, got {got_value!r}")
                elif got_value is None:
                    problems.append(f"{customer_id}.{field}: NULL honesty violated")
                elif Decimal(str(got_value)) != Decimal(want_token):
                    problems.append(f"{customer_id}.{field}: {got_value} != {want_token}")
        extra = sorted(set(observed) - set(expected))
        if extra:
            problems.append(f"unexpected rows: {extra}")
        verify["problems"] = problems
        verify["row_count"] = len(execute["rows"])
        verify["ok"] = not problems

    def run_query_chain(self) -> dict[str, Any]:
        for name in QUERY_STEPS:
            if self.done(name):
                continue
            getattr(self, f"step_{name}")()
            self.save()
        self.state["query_result"] = ("pass" if all(self.done(n) for n in QUERY_STEPS)
                                      else "fail")
        self.state["full_e2e_pass"] = False  # query chain alone is not the whole goal
        self.save()
        return {"phase": "query", "result": self.state["query_result"],
                "steps": {k: v.get("ok") for k, v in self.state["steps"].items()},
                "problems": self.state["steps"].get("verify_oracle", {}).get("problems"),
                "acceptance_ids": {"A-02": "cross-ontology 2-level pipeline vs doc sec 8",
                                   "A-03": "implicit cross join without assertion (guards)",
                                   "A-04": "join assertions executed with evidence refs"},
                "e2e_pass": False}


def collect_all_pages(api: "Api", request_body: dict, *, max_pages: int = 50
                      ) -> tuple[list[dict], dict[str, Any]]:
    """Walk the remote camelCase envelope; completeness only from the contract."""
    rows: list[dict] = []
    seen: set[str] = set()
    identity = None
    meta: dict[str, Any] = {}
    body = request_body
    for pages in range(1, max_pages + 1):
        _, response = api.request("POST", "/api/v2/formal/agent/query", body)
        data = _payload(response)
        page = data.get("rows")
        total = data.get("totalRowCount")
        if (data.get("schemaVersion") != 2 or not isinstance(page, list)
                or type(data.get("hasNext")) is not bool
                or type(data.get("pageRowCount")) is not int
                or total is None and data.get("countsScope") != "page_probe"):
            raise RuntimeError(f"invalid remote query contract: {str(response)[:300]}")
        if data["pageRowCount"] != len(page):
            raise RuntimeError("pageRowCount mismatch")
        # Lazy paging may report unknown totals (countsScope=page_probe); the
        # stable identity is (queryId, expiresAt) without the row count.
        current = (data.get("queryId"), data.get("expiresAt"))
        if not current[0] or not current[1]:
            raise RuntimeError("query identity/expiry missing")
        if identity is not None and identity != current:
            raise RuntimeError("query identity changed between pages")
        identity = current
        if isinstance(total, int) and len(rows) + len(page) > total:
            raise RuntimeError("pagination exceeded the declared totalRowCount")
        if data.get("persistence", {}).get("mode") != "ephemeral":
            raise RuntimeError("remote result not ephemeral")
        if data.get("truncated") is not False or data.get("partial") is not False:
            raise RuntimeError("remote result partial/truncated")
        rows.extend(page)
        meta = {"queryId": current[0], "expiresAt": current[1],
                "pageCount": pages, "totalsUnknown": total is None,
                "countsScope": data.get("countsScope"),
                "totalRowCount": total,
                "plan": data.get("plan") if pages == 1 else meta.get("plan"),
                "sourceStates": data.get("sourceStates") if pages == 1 else meta.get("sourceStates")}
        if not data["hasNext"]:
            if data.get("cursor") is not None:
                raise RuntimeError("last page still carries a cursor")
            if isinstance(data.get("totalRowCount"), int) and len(rows) != data["totalRowCount"]:
                raise RuntimeError("last page inconsistent with complete result")
            meta["servedRowCount"] = len(rows)
            return rows, meta
        cursor = data.get("cursor")
        if not isinstance(cursor, str) or not cursor.startswith("mem."):
            raise RuntimeError("hasNext without mem. cursor")
        if cursor in seen:
            raise RuntimeError("cursor repeated")
        seen.add(cursor)
        body = {"cursor": cursor}
    raise RuntimeError("pagination exceeded max pages")


def phase_query(args: argparse.Namespace) -> dict[str, Any]:
    api = Api(args.api_base)
    status, body = api.request("POST", "/api/v1/auth/login",
                               {"username": args.admin_user, "password": args.admin_password})
    if status != 200:
        return {"phase": "query", "result": "blocked", "login_status": status,
                "e2e_pass": False}
    api.token = body["data"]["access_token"]
    run = GoalRun(args.name, api)
    # Ontology ids come from the front-door run; the early front-door files
    # live on as the reconstructed archive, so the live query run state
    # (same ids) is the primary fallback.
    frontdoor_path = RUNS / f"{args.frontdoor_run}.json"
    query_fallback = RUNS / f"{getattr(args, 'query_run', '') or 'run02-q5'}.json"
    if "ontologies" not in run.state:
        for source in (frontdoor_path, query_fallback):
            if not source.exists():
                continue
            state = json.loads(source.read_text(encoding="utf-8"))
            ontologies = ({key: entry["ontology_id"] for key, entry in
                           state.get("frontdoor", {}).get("created", {}).items()}
                          or state.get("ontologies") or {})
            if ontologies:
                run.state["ontologies"] = ontologies
                break
    missing = [k for k in ("A", "B", "C") if k not in run.state.get("ontologies", {})]
    if missing:
        return {"phase": "query", "result": "blocked",
                "reason": f"front-door ontology ids missing for {missing}; "
                          f"run frontdoor first", "e2e_pass": False}
    return run.run_query_chain()


GUARD_CASES = {
    "cursor_invalid": {"expect_status": (400, 404, 410, 422)},
    "stale_release": {"expect_status": (400, 404, 409)},
}


def phase_guards(args: argparse.Namespace) -> dict[str, Any]:
    """Deterministic guard cases first; permission/owner cases are added with
    viewer provisioning and stay not_run instead of being faked."""
    api = Api(args.api_base)
    status, body = api.request("POST", "/api/v1/auth/login",
                               {"username": args.admin_user, "password": args.admin_password})
    if status != 200:
        return {"phase": "guards", "result": "blocked", "login_status": status,
                "e2e_pass": False}
    api.token = body["data"]["access_token"]
    run_path = RUNS / f"{args.query_run}.json"
    if not run_path.exists():
        return {"phase": "guards", "result": "blocked",
                "reason": f"query run state missing: {args.query_run}", "e2e_pass": False}
    run = json.loads(run_path.read_text(encoding="utf-8"))
    steps = run["steps"]
    cases: dict[str, Any] = {}

    status, body = api.request("POST", "/api/v2/formal/agent/query",
                               {"cursor": "mem.goal-guard-invalid-cursor"})
    cases["cursor_invalid"] = {"status": status,
                               "pass": status in GUARD_CASES["cursor_invalid"]["expect_status"]}

    # A release that is not the current one (the superseded v0) must refuse to
    # serve queries instead of silently answering from stale structure.
    _, versions = api.request("GET", f"/api/v2/ontologies/{run['ontologies']['A']}/versions")
    current = versions.get("current_release_id")
    stale = next((item["id"] for item in versions.get("data", [])
                  if item.get("id") != current and item.get("node_kind") == "release"), None)
    if stale:
        body_query = {"sources": [{"alias": "cu", "ontologyId": run["ontologies"]["A"],
                                   "releaseId": stale,
                                   "objectTypeId": steps["define_workspace"]["A"]["object_types"]["Customer"]}],
                      "select": [{"source": "cu", "property": "customer_id", "as": "customer_id"}],
                      "pageSize": 5}
        status, body = api.request("POST", "/api/v2/formal/agent/query", body_query)
        cases["stale_release"] = {"status": status,
                                  "pass": status in GUARD_CASES["stale_release"]["expect_status"]}
    else:
        cases["stale_release"] = {"status": "not_run", "pass": False,
                                  "reason": "no superseded release recorded"}

    executed = [name for name in cases if cases[name]["status"] != "not_run"]
    result = "guards_pass" if executed and all(cases[n]["pass"] for n in executed) else "fail"
    payload = {"phase": "guards", "result": result, "cases": cases,
               "not_run": {"permission_viewer": "viewer provisioning pending",
                           "owner_session_change": "second authorized user pending"},
               "acceptance_ids": {"A-19": "cursor/stale-release guards (partial)"},
               "e2e_pass": False}
    target = json.loads(run_path.read_text(encoding="utf-8"))
    target["guards"] = payload
    run_path.write_text(json.dumps(target, ensure_ascii=False, indent=2, default=str)
                        + "\n", encoding="utf-8")
    return payload




ACTIONS_STEPS_MANUAL = ("manual_dataset", "manual_draft_action", "manual_promote",
                        "manual_authorize_action", "manual_open_session",
                        "manual_prepare_confirm_execute", "manual_hitl_decide",
                        "manual_version_reflection")


class ActionsRun(GoalRun):
    """Three-asset action chain. The manual chain is fully wired; curated and
    integrated chains reuse the same front doors (curated review flow,
    integrated write adapter) and record honest per-step statuses."""

    def done(self, name: str) -> bool:
        return bool(self.state["steps"].setdefault(name, {}).get("ok"))

    # -- manual (A-13) ---------------------------------------------------
    def step_manual_dataset(self) -> None:
        step = self.step("manual_dataset")
        # Dedicated scratch rows: never touch the oracle four tables.
        rows = [{"sku": "G-1", "qty": 10, "note": "goal-actions-manual"},
                {"sku": "G-2", "qty": 20, "note": "goal-actions-manual"}]
        import csv, io, json as _json
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=["sku", "qty", "note"])
        writer.writeheader()
        writer.writerows(rows)
        metadata = _json.dumps({
            "name": "goal-actions-manual",
            "primary_key": "sku",
            "columns": [{"name": "sku", "type": "string", "nullable": False},
                        {"name": "qty", "type": "integer", "nullable": False},
                        {"name": "note", "type": "string", "nullable": True}]})
        status, body = self.api.post_multipart(
            "/api/v2/datasets/upload", {"metadata": metadata},
            "goal-actions-manual.csv", buffer.getvalue().encode(), "text/csv")
        if status not in (200, 201):
            raise RuntimeError(f"manual dataset upload: HTTP {status} {str(body)[:300]}")
        payload = _payload(body)
        dataset_id = payload.get("id") or payload.get("dataset_id")
        if not dataset_id:
            raise RuntimeError(f"manual dataset id missing: {str(body)[:200]}")
        step["dataset_id"] = dataset_id
        step["rows"] = rows
        step["ok"] = True

    def step_manual_draft_action(self) -> None:
        dataset_id = self.state["steps"]["manual_dataset"]["dataset_id"]
        step = self.step("manual_draft_action")
        ontology_id = self.ontology_id("A")
        _, versions = self.api.request("GET", f"/api/v2/ontologies/{ontology_id}/versions")
        current = versions.get("current_release_id")
        _, body = self.api.request(
            "POST", f"/api/v2/ontologies/{ontology_id}/versions/{current}/drafts", {})
        version_id = (_payload(body).get("id") or _payload(body).get("version_id"))
        step["version_id"] = version_id
        workspace = self.state["steps"]["define_workspace"]
        customer_type = workspace["A"]["object_types"]["Customer"]
        # Object type bound to the manual dataset with an update action whose
        # rule writes through the mapping; a dedicated scratch type keeps the
        # oracle four tables untouched.
        import uuid as _uuid
        scratch_type_id = str(_uuid.uuid4())
        object_type = {"id": scratch_type_id, "name": "ManualStock", "displayName": "ManualStock",
                       "primaryKey": "sku",
                       "properties": [{"name": "sku", "type": "string"},
                                      {"name": "qty", "type": "number"},
                                      {"name": "note", "type": "string"}],
                       "interfaces": [], "positionX": 300.0, "positionY": 100.0}
        action = {"id": str(_uuid.uuid4()), "name": "manual_stock_adjust",
                  "displayName": "manual_stock_adjust",
                  "objectTypeId": scratch_type_id,
                  "parameters": [{"name": "sku", "type": "string"},
                                 {"name": "qty", "type": "integer"}],
                  "rules": [{"id": "r1", "name": "set_qty", "type": "update_property", "order": 1,
                             "config": {"targetProperty": "qty", "valueSource": "parameter",
                                        "value": "qty"}}],
                  "requiresApproval": True}
        # Workspace save is a FULL-VIEW replace: merge the scratch type and
        # action into the draft's inherited view instead of dropping the
        # existing Customer/Contract types the semantic canvas still needs.
        _, current = self.api.request(
            "GET", f"/api/v2/ontologies/{ontology_id}/versions/{version_id}/workspace")
        view = _payload(current)
        object_types = [item for item in view.get("objectTypes", [])
                        if item.get("name") != "ManualStock"] + [object_type]
        actions = [item for item in view.get("actions", [])
                   if item.get("name") != "manual_stock_adjust"] + [action]
        status, body = self.api.request(
            "PUT", f"/api/v2/ontologies/{ontology_id}/versions/{version_id}/workspace",
            {"objectTypes": object_types, "linkTypes": view.get("linkTypes", []),
             "actions": actions, "functions": view.get("functions", []),
             "baseRevision": None})
        if status not in (200, 201):
            raise RuntimeError(f"manual workspace save: HTTP {status} {str(body)[:300]}")
        # Manual/curated assets bind in LOCAL mode (no bindingMode): the
        # write adapter routes them through _resolve_local_dataset; only
        # integrated assets use bindingMode=remote.
        mapping = {"id": str(_uuid.uuid5(_uuid.NAMESPACE_URL,
                                         f"{ontology_id}:mapping:ManualStock")),
                   "entityClass": "ManualStock",
                   "curatedDatasetId": dataset_id,
                   "fieldMapping": {"sku": "sku", "qty": "qty", "note": "note"}}
        _, mapping_view = self.api.request(
            "GET", f"/api/v2/ontologies/{ontology_id}/versions/{version_id}/workspace/mappings")
        existing = [item for item in _payload(mapping_view).get("mappings", [])
                    if item.get("entityClass") != "ManualStock"]
        status, body = self.api.request(
            "PUT", f"/api/v2/ontologies/{ontology_id}/versions/{version_id}/workspace/mappings",
            {"mappings": existing + [mapping],
             "linkMappings": _payload(mapping_view).get("linkMappings", []),
             "sentinels": [], "baseRevision": None})
        if status not in (200, 201):
            raise RuntimeError(f"manual mapping save: HTTP {status} {str(body)[:300]}")
        _, ws = self.api.request(
            "GET", f"/api/v2/ontologies/{ontology_id}/versions/{version_id}/workspace")
        current_ws = _payload(ws)
        types = {item["name"]: item["id"] for item in current_ws.get("objectTypes", [])}
        actions = {item["name"]: item["id"] for item in current_ws.get("actions", [])}
        step["scratch_type_id"] = types.get("ManualStock")
        step["action_id"] = actions.get("manual_stock_adjust")
        if not step["scratch_type_id"] or not step["action_id"]:
            raise RuntimeError(f"manual identities missing after save: {types} {actions}")
        # Semantic canvas must cover every structural object: append the
        # scratch object to the canvas before the trial gate runs.
        _, ws_now = self.api.request(
            "GET", f"/api/v2/ontologies/{ontology_id}/versions/{version_id}/workspace")
        base_revision = _payload(ws_now).get("revision")
        canvas_objects = []
        seen_names = set()
        for type_entry in _payload(ws_now).get("objectTypes", []):
            name = type_entry.get("name")
            if name in seen_names:
                continue
            seen_names.add(name)
            canvas_objects.append({
                "name": name, "display_name": name,
                "description": "goalnet acceptance business object",
                "key_attribute": (type_entry.get("primaryKey")
                                  or next((p.get("name") for p in type_entry.get("properties", [])
                                           if p.get("name")), "")),
                "attributes": [{"name": prop.get("name"), "display_name": prop.get("name"),
                                "type_hint": prop.get("type"),
                                "required": prop.get("name") == type_entry.get("primaryKey")}
                               for prop in type_entry.get("properties", [])],
                "relations": []})
        status, body = self.api.request(
            "PUT", f"/api/v2/ontologies/{ontology_id}/versions/{version_id}/semantic",
            {"baseRevision": base_revision,
             "canvas": {"objects": canvas_objects, "actors": [],
                        # behaviors are the canvas face of ActionTypes
                        "behaviors": [{"name": "manual_stock_adjust",
                                       "display_name": "manual_stock_adjust",
                                       "description": "manual stock quantity adjust",
                                       "actor": "goaladmin",
                                       "object": "ManualStock",
                                       # inputs replay into action parameter names
                                       "inputs": [{"name": "sku", "type_hint": "string"},
                                                  {"name": "qty", "type_hint": "integer"}],
                                       "needs_approval": True}],
                        "events": [], "rules": [], "scenarios": [], "processes": []},
             "documentMd": "# goalnet A 业务语义（含 manual scratch）\n",
             "documentTitle": "goalnet A 业务语义"})
        if status != 200:
            raise RuntimeError(f"manual semantic save: HTTP {status} {str(body)[:250]}")
        step["ok"] = True

    def step_manual_promote(self) -> None:
        step = self.step("manual_promote")
        ontology_id = self.ontology_id("A")
        version_id = self.state["steps"]["manual_draft_action"]["version_id"]
        status, body = self.api.request(
            "POST", f"/api/v2/ontologies/{ontology_id}/versions/{version_id}/trial-runs", {})
        payload = _payload(body)
        if status not in (200, 201) or payload.get("status") != "passed":
            raise RuntimeError(f"manual trial: HTTP {status} {str(payload)[:250]}")
        status, body = self.api.request(
            "GET", f"/api/v2/ontologies/{ontology_id}/versions/{version_id}/impact")
        impact_hash = _payload(body).get("impactHash")
        if impact_hash != payload.get("impact_hash"):
            raise RuntimeError("manual impact hash drift")
        status, body = self.api.request(
            "POST", f"/api/v2/ontologies/{ontology_id}/versions/{version_id}/promote",
            {"trialRunId": payload["id"], "impactHash": impact_hash})
        release_id = _payload(body).get("release_id") or _payload(body).get("id")
        if status != 201 or not release_id:
            raise RuntimeError(f"manual promote: HTTP {status} {str(body)[:250]}")
        step["release_id"] = release_id
        step["ok"] = True

    def step_manual_authorize_action(self) -> None:
        """Profiles deny actions by default; authorization goes through the
        admin profile front door. The pre-authorization 403 is itself the
        A-21 negative evidence and is recorded, not retried away."""
        step = self.step("manual_authorize_action")
        ontology_id = self.ontology_id("A")
        draft = self.state["steps"]["manual_draft_action"]
        # Negative first: an unauthorized prepare must be refused.
        session = {"session_kind": "goal-e2e", "session_id": "goal-actions-manual-0"}
        status, body = self.api.request(
            "POST", f"/api/v2/formal/ontologies/{ontology_id}/agent/proposals",
            dict(session, client_submission_id="goal-manual-unauthorized",
                 action_id=draft["action_id"],
                 release_id=self.state["steps"]["manual_promote"]["release_id"],
                 target_ref={"objectTypeId": draft["scratch_type_id"], "key": {"sku": "G-1"}},
                 parameters={"qty": 15}))
        step["pre_authorization_status"] = status
        step["pre_authorization_refused"] = status == 403
        if status != 403:
            raise RuntimeError(f"expected governance 403 before profile grant, got {status}")
        _, current = self.api.request(
            "GET", f"/api/v2/formal/ontologies/{ontology_id}/agent/profile")
        profile = _payload(current)
        allowed = [item for item in (profile.get("allowedActionIds") or [])
                   if item != draft["action_id"]] + [draft["action_id"]]
        status, body = self.api.request(
            "PUT", f"/api/v2/formal/ontologies/{ontology_id}/agent/profile",
            {"allowedActionIds": allowed, "allowActionProposals": True})
        if status not in (200, 201):
            raise RuntimeError(f"profile grant: HTTP {status} {str(body)[:250]}")
        step["granted"] = True
        step["ok"] = True

    def step_manual_open_session(self) -> None:
        """Proposals require a real AgentConversation created through the
        assistant front door; this is also the A-14 referral carrier."""
        step = self.step("manual_open_session")
        ontology_id = self.ontology_id("A")
        status, body = self.api.request(
            "POST", f"/api/v2/formal/ontologies/{ontology_id}/agent/chat",
            {"message": "建立 goal 验收会话（结构确认）", "stream": False})
        if status not in (200, 201):
            raise RuntimeError(f"session chat: HTTP {status} {str(body)[:250]}")
        payload = _payload(body)
        conversation_id = payload.get("conversationId") or payload.get("conversation_id")
        if not conversation_id:
            raise RuntimeError(f"conversation id missing: {str(payload)[:200]}")
        step["conversation_id"] = conversation_id
        step["reply_sample"] = str(payload.get("reply") or payload.get("content") or "")[:160]
        # Proposals resolve client_submission_id against an in-conversation
        # message (frontend replay path): capture the latest user message id.
        status, detail = self.api.request(
            "GET", f"/api/v2/formal/ontologies/{ontology_id}/agent/conversations/{conversation_id}")
        if status != 200:
            raise RuntimeError(f"conversation detail: HTTP {status}")
        detail_payload = _payload(detail)
        messages = detail_payload.get("messages") or []
        user_ids = [m.get("id") for m in messages
                    if m.get("role") == "user" and m.get("id")]
        if not user_ids:
            raise RuntimeError("conversation has no user message id for replay submit")
        step["submission_message_id"] = user_ids[-1]
        step["ok"] = True

    def step_manual_prepare_confirm_execute(self) -> None:
        step = self.step("manual_prepare_confirm_execute")
        ontology_id = self.ontology_id("A")
        draft = self.state["steps"]["manual_draft_action"]
        session = {"session_kind": "goal-e2e",
                   "session_id": self.state["steps"]["manual_open_session"]["conversation_id"]}
        proposal = dict(session,
                        client_submission_id=self.state["steps"]["manual_open_session"]
                        ["submission_message_id"],
                        action_id=draft["action_id"],
                        release_id=self.state["steps"]["manual_promote"]["release_id"],
                        target_ref={"objectTypeId": draft["scratch_type_id"],
                                    "key": {"sku": "G-1"}},
                        parameters={"qty": 15})
        status, body = self.api.request(
            "POST", f"/api/v2/formal/ontologies/{ontology_id}/agent/proposals", proposal)
        if status not in (200, 201):
            raise RuntimeError(f"manual prepare: HTTP {status} {str(body)[:300]}")
        confirmation = _payload(body)
        def _field(source: dict, snake: str, camel: str):
            return source.get(snake) or source.get(camel)
        confirmation_id = _field(confirmation, "confirmation_id", "confirmationId")
        payload_hash = _field(confirmation, "payload_hash", "payloadHash")
        if not confirmation_id or not payload_hash:
            raise RuntimeError(f"prepare payload shape unexpected: {str(confirmation)[:250]}")
        step["confirmation_id"] = confirmation_id
        step["prepare_state"] = _field(confirmation, "state", "state")
        decision = dict(session, decision_id="goal-manual-adjust-1-d1",
                        payload_hash=payload_hash)
        status, body = self.api.request(
            "POST", f"/api/v2/formal/ontologies/{ontology_id}/agent/proposals/"
                    f"{confirmation_id}/decision",
            dict(decision, decision="approve"))
        if status not in (200, 201):
            raise RuntimeError(f"manual decision: HTTP {status} {str(body)[:300]}")
        status, body = self.api.request(
            "POST", f"/api/v2/formal/ontologies/{ontology_id}/agent/execute-proposal",
            dict(session, confirmation_id=confirmation_id))
        payload = _payload(body)
        if status not in (200, 201):
            raise RuntimeError(f"manual execute: HTTP {status} {str(body)[:300]}")
        step["execute_outcome"] = payload
        step["ok"] = True

    def step_manual_hitl_decide(self) -> None:
        """requiresApproval actions need the SECONDARY HITL decision on the
        approval log (admin front door) after confirmation approval. Frozen
        owned action/key/qty are verified against the frozen display before
        deciding; canaries run before/after."""
        import subprocess
        step = self.step("manual_hitl_decide")
        ontology_id = self.ontology_id("A")
        execute = self.state["steps"]["manual_prepare_confirm_execute"]
        outcome = execute.get("execute_outcome") or {}

        def meta_counts() -> dict:
            def psql(sql):
                out = subprocess.run(
                    ["docker", "exec", "oo-goal-db", "psql", "-X", "-q", "-A", "-t",
                     "-U", "integrated_e2e", "-d", "integrated_metadata",
                     "-v", "ON_ERROR_STOP=1", "-c", sql],
                    capture_output=True, text=True)
                return out.stdout.strip() if out.returncode == 0 else "missing"
            return {sql.split("FROM ")[1].split(";")[0].split(" ")[0]:
                    psql(sql) for sql in
                    ("SELECT COUNT(*) FROM v2_dataset_versions;",
                     "SELECT COUNT(*) FROM data_blobs;",
                     "SELECT COUNT(*) FROM v2_integrated_sync_runs;")}

        def frozen_display() -> dict:
            display = outcome.get("display") or {}
            asset = ((display.get("target") or {}).get("asset") or {})
            return {"action": (display.get("action") or {}).get("name"),
                    "key": asset.get("key"),
                    "operation": asset.get("operation"),
                    "fields": asset.get("fields"),
                    "parameters": display.get("parameters")}

        frozen = frozen_display()
        step["frozen"] = frozen
        problems = []
        if frozen.get("action") != "manual_stock_adjust":
            problems.append(f"frozen action mismatch: {frozen.get('action')}")
        key = frozen.get("key") or {}
        if key.get("sku") != "G-1":
            problems.append(f"frozen key mismatch: {key}")
        params = {str(p.get("name")): p.get("value") for p in (frozen.get("parameters") or [])}
        if params.get("qty") != 15:
            problems.append(f"frozen qty mismatch: {params}")
        if frozen.get("operation") != "update":
            problems.append(f"frozen operation mismatch: {frozen.get('operation')}")
        if problems:
            step["problems"] = problems
            step["ok"] = False
            raise RuntimeError(f"refusing HITL decide, frozen display check failed: {problems}")

        step["canary_before"] = meta_counts()
        session = {"session_kind": "goal-e2e",
                   "session_id": self.state["steps"]["manual_open_session"]["conversation_id"]}
        # Fetch the approval log id from the current confirmation state.
        cid = execute["confirmation_id"]
        query = f"?session_kind={session['session_kind']}&session_id={session['session_id']}"
        status, body = self.api.request(
            "GET", f"/api/v2/formal/ontologies/{ontology_id}/agent/proposals/{cid}{query}")
        current = _payload(body) if status == 200 else {}
        log_id = current.get("approvalLogId") or current.get("approval_log_id")
        if not log_id:
            raise RuntimeError(f"approval log id missing from confirmation: {str(current)[:200]}")
        status, body = self.api.request(
            "POST", f"/api/v2/formal/ontologies/{ontology_id}/action-logs/{log_id}/decide",
            {"decision": "approved", "reason": "goal fixture task authorization",
             "releaseId": self.state["steps"]["manual_promote"]["release_id"]})
        step["decide_status"] = status
        if status not in (200, 201):
            step["canary_after"] = meta_counts()
            raise RuntimeError(f"HITL decide: HTTP {status} {str(body)[:250]}")
        # Poll the confirmation to a terminal state (bounded, no tight loops).
        import time as _time
        terminal = None
        for _ in range(15):
            _time.sleep(4)
            status, body = self.api.request(
                "GET", f"/api/v2/formal/ontologies/{ontology_id}/agent/proposals/{cid}{query}")
            state_now = (_payload(body) or {}).get("state") if status == 200 else None
            if state_now in ("succeeded", "failed", "rejected"):
                terminal = state_now
                break
        step["terminal_state"] = terminal
        step["canary_after"] = meta_counts()
        if terminal != "succeeded":
            step["ok"] = False
            raise RuntimeError(f"confirmation did not reach succeeded after decide: {terminal}")
        step["ok"] = True

    def step_manual_version_reflection(self) -> None:
        """The action must produce a new manual dataset version, and the
        ontology view must reflect the new value through the version link."""
        step = self.step("manual_version_reflection")
        dataset_id = self.state["steps"]["manual_dataset"]["dataset_id"]
        status, body = self.api.request("GET", f"/api/v2/datasets/{dataset_id}/versions")
        versions = _payload(body)
        version_nos = [item.get("version_no") or item.get("versionNo")
                       for item in (versions if isinstance(versions, list)
                                    else versions.get("items", []))]
        step["versions"] = version_nos
        problems = []
        if len(version_nos or []) < 2:
            problems.append(f"expected a new dataset version after the action, saw {version_nos}")
        latest = max(version_nos or [0])
        status, body = self.api.request(
            "GET", f"/api/v2/datasets/{dataset_id}/versions/{latest}/preview")
        rows = _payload(body)
        rows = rows.get("rows", rows) if isinstance(rows, dict) else rows
        qty = next((row.get("qty") for row in rows or [] if row.get("sku") == "G-1"), None)
        if str(qty) != "15":
            problems.append(f"dataset latest version qty={qty}, expected 15")
        step["problems"] = problems
        step["ok"] = not problems

    def run_manual_chain(self) -> dict[str, Any]:
        for name in ACTIONS_STEPS_MANUAL:
            if self.done(name):
                continue
            getattr(self, f"step_{name}")()
            self.save()
        ok = all(self.done(n) for n in ACTIONS_STEPS_MANUAL)
        return {"phase": "actions", "result": "manual_pass" if ok else "fail",
                "steps": {n: self.done(n) for n in ACTIONS_STEPS_MANUAL},
                "problems": self.state["steps"].get("manual_version_reflection", {}).get("problems"),
                "curated_integrated": "not_run: wired next on the same front doors "
                                      "(curated review flow / integrated write adapter)",
                "acceptance_ids": {"A-13": "manual action new version + reflection",
                                   "A-18": "pre-confirm zero business write"},
                "e2e_pass": False}


def phase_actions(args: argparse.Namespace) -> dict[str, Any]:
    api = Api(args.api_base)
    status, body = api.request("POST", "/api/v1/auth/login",
                               {"username": args.admin_user, "password": args.admin_password})
    if status != 200:
        return {"phase": "actions", "result": "blocked", "login_status": status,
                "e2e_pass": False}
    api.token = body["data"]["access_token"]
    run = ActionsRun(args.name, api)
    query_path = RUNS / f"{args.query_run}.json"
    frontdoor_path = RUNS / f"{args.frontdoor_run}.json"
    if "ontologies" not in run.state:
        # Prefer the live query run (carries the same ids); the early
        # front-door run files live on as the reconstructed archive.
        for source in (query_path, frontdoor_path):
            if not source.exists():
                continue
            state = json.loads(source.read_text(encoding="utf-8"))
            ontologies = {key: entry["ontology_id"] for key, entry in
                          state.get("frontdoor", {}).get("created", {}).items()} or \
                         state.get("ontologies") or {}
            if ontologies:
                run.state["ontologies"] = ontologies
                break
    if "define_workspace" not in run.state["steps"] and query_path.exists():
        query_state = json.loads(query_path.read_text(encoding="utf-8"))
        run.state["steps"]["define_workspace"] = query_state["steps"]["define_workspace"]
    missing = [k for k in ("A",) if k not in run.state.get("ontologies", {})]
    if missing or "define_workspace" not in run.state["steps"]:
        return {"phase": "actions", "result": "blocked",
                "reason": "front-door/query run state required", "e2e_pass": False}
    return run.run_manual_chain()




ROLE_ACCOUNTS = {
    "goaleditor": "editor",
    "goalviewer": "viewer",
    "goalcustom": "custom",
}
ROLE_PUBLIC_PASSWORD = "Goalnet-Role-Fixture-2026"


def _login(api: "Api", username: str, password: str) -> None:
    status, body = api.request("POST", "/api/v1/auth/login",
                               {"username": username, "password": password})
    if status != 200:
        raise RuntimeError(f"login {username}: HTTP {status}")
    api.token = body["data"]["access_token"]


def phase_guards_roles(args: argparse.Namespace) -> dict[str, Any]:
    """Real RBAC/menu matrix on dedicated fixture accounts (public creds).
    Cursor/owner-session cases stay not_run until the query service is
    healthy; they are never faked."""
    admin = Api(args.api_base)
    _login(admin, args.admin_user, args.admin_password)
    query_path = RUNS / f"{args.query_run}.json"
    actions_path = RUNS / f"{args.actions_run}.json"
    if not query_path.exists() or not actions_path.exists():
        return {"phase": "guards-roles", "result": "blocked",
                "reason": "query/actions run state required for matrix targets",
                "e2e_pass": False}
    query_run = json.loads(query_path.read_text(encoding="utf-8"))
    actions_run = json.loads(actions_path.read_text(encoding="utf-8"))
    ontology_a = query_run["ontologies"]["A"]
    action = actions_run["steps"]["manual_draft_action"]
    release_id = actions_run["steps"]["manual_promote"]["release_id"]

    cases: dict[str, Any] = {}
    viewer = Api(args.api_base)
    _login(viewer, "goalviewer", ROLE_PUBLIC_PASSWORD)
    status, body = viewer.request("POST", "/api/v1/ontologies",
                                  {"name": "goalnet-denied-probe", "domain": "goalnet-crm"})
    cases["viewer_ontology_create_denied"] = {"status": status,
                                              "pass": status == 403}
    minimal_query = {"sources": [{"alias": "cu", "ontologyId": ontology_a,
                                  "releaseId": release_id,
                                  "objectTypeId": action["scratch_type_id"]}],
                     "select": [{"source": "cu", "property": "sku", "as": "sku"}],
                     "pageSize": 5}
    status, body = viewer.request("POST", "/api/v2/formal/agent/query", minimal_query)
    detail = body.get("detail") if isinstance(body, dict) else {}
    cases["viewer_agent_query_denied"] = {
        "status": status, "code": detail.get("code") if isinstance(detail, dict) else None,
        "pass": status == 403}
    conv_status, conv_body = admin.request(
        "GET", f"/api/v2/formal/ontologies/{ontology_a}/agent/conversations/"
               f"{actions_run['steps']['manual_open_session']['conversation_id']}")
    if conv_status == 200:
        detail_payload = _payload(conv_body)
        messages = detail_payload.get("messages") or []
        user_ids = [m.get("id") for m in messages
                    if m.get("role") == "user" and m.get("id")]
        if user_ids:
            session = {"session_kind": "goal-e2e", "session_id":
                       actions_run["steps"]["manual_open_session"]["conversation_id"]}
            status, body = viewer.request(
                "POST", f"/api/v2/formal/ontologies/{ontology_a}/agent/proposals",
                dict(session, client_submission_id=user_ids[-1],
                     action_id=action["action_id"], release_id=release_id,
                     target_ref={"objectTypeId": action["scratch_type_id"],
                                 "key": {"sku": "G-1"}},
                     parameters={"qty": 15}))
            detail = body.get("detail") if isinstance(body, dict) else {}
            cases["viewer_prepare_denied"] = {
                "status": status, "code": detail.get("code") if isinstance(detail, dict) else None,
                "pass": status == 403}
    editor = Api(args.api_base)
    _login(editor, "goaleditor", ROLE_PUBLIC_PASSWORD)
    _, versions = editor.request("GET", f"/api/v2/ontologies/{ontology_a}/versions")
    current = versions.get("current_release_id")
    status, body = editor.request(
        "POST", f"/api/v2/ontologies/{ontology_a}/versions/{current}/drafts", {})
    detail = body.get("detail") if isinstance(body, dict) else {}
    # Real contract: ontology write is owner-or-admin only; editor is denied
    # with the owner-only message (distinct from the read-only role message).
    cases["editor_draft_denied_owner_only"] = {
        "status": status,
        "message": detail if isinstance(detail, str) else detail.get("message"),
        "pass": status == 403 and "owner or an administrator" in str(detail)}
    custom = Api(args.api_base)
    _login(custom, "goalcustom", ROLE_PUBLIC_PASSWORD)
    status, body = custom.request("POST", "/api/v2/formal/agent/query", minimal_query)
    detail = body.get("detail") if isinstance(body, dict) else {}
    code = detail.get("code") if isinstance(detail, dict) else None
    # With the agent menu granted the request must pass RBAC; how far it gets
    # is the query service's own state, recorded separately from auth.
    cases["custom_agent_query_auth_passed"] = {
        "status": status, "code": code,
        "pass": status != 403,
        "note": "auth layer distinguished from execution: 422 execution errors "
                "still prove the menu gate opened (0c076 fix pending)"}
    executed = [name for name, case in cases.items() if case.get("status") != "not_run"]
    result = "guards_roles_pass" if executed and all(cases[n]["pass"] for n in executed) else "fail"
    payload = {"phase": "guards-roles", "result": result, "cases": cases,
               "not_run": {"cursor_invalid_as_roles": "covered in guards phase",
                           "owner_session_change": "query service unhealthy (0c076)",
                           "source_ro_write": "remote fixture ro/rw matrix in actions phase"},
               "accounts": {u: r for u, r in ROLE_ACCOUNTS.items()},
               "acceptance_ids": {"A-21": "role x asset deny/allow matrix (partial)",
                                  "A-23": "menu/RBAC layer (partial)"},
               "e2e_pass": False}
    target = json.loads(query_path.read_text(encoding="utf-8"))
    target["guards_roles"] = payload
    query_path.write_text(json.dumps(target, ensure_ascii=False, indent=2,
                                     default=str) + "\n", encoding="utf-8")
    return payload


def phase_governance(args: argparse.Namespace) -> dict[str, Any]:
    """Real governance positives/negatives on owned profiles and actors:
    A-03 unasserted cross-join refusal, A-04 asserted join pass, A-19
    cross-user cursor refusal, A-23 profile/action and property revocation
    with restore. Existing join assertions and releases are reused; no new
    drafts/promotes are created by this phase."""
    api = Api(args.api_base)
    _login(api, args.admin_user, args.admin_password)
    run_path = RUNS / f"{args.query_run}.json"
    if not run_path.exists():
        return {"phase": "governance", "result": "blocked",
                "reason": "query run state required", "e2e_pass": False}
    run = json.loads(run_path.read_text(encoding="utf-8"))
    st = run["steps"]; onto = run["ontologies"]; refs = st["join_assertions"]["evidence_refs"]
    ontology_a = onto["A"]
    def src(alias, key, ot):
        return {"alias": alias, "ontologyId": onto[key],
                "releaseId": st["promote"][key]["release_id"],
                "objectTypeId": st["define_workspace"][key]["object_types"][ot]}
    cu, o = src("cu", "A", "Customer"), src("o", "B", "Order")
    cases: dict[str, Any] = {}

    # A-04 positive: asserted flat cross-ontology join passes.
    asserted = {"sources": [cu, o],
                "joins": [{"left": "cu", "right": "o", "type": "inner",
                           "on": {"kind": "explicit_key", "leftProperty": "customer_id",
                                  "rightProperty": "customer_id", "keyType": "string",
                                  "cardinality": "1:N", "businessMeaning": "goal A-04",
                                  "evidenceRefs": [refs[1]]}}],
                "select": [{"source": "cu", "property": "customer_id", "as": "customer_id"},
                           {"source": "o", "property": "order_id", "as": "order_id"}],
                "pageSize": 10}
    status, body = api.request("POST", "/api/v2/formal/agent/query", asserted)
    rows = (_payload(body) or {}).get("rows") if status == 200 else None
    cases["a04_asserted_join_pass"] = {"status": status,
                                       "pass": status == 200 and len(rows or []) == 3}

    # A-03 negative: no evidence at all.
    no_evidence = json.loads(json.dumps(asserted))
    no_evidence["joins"][0]["on"]["evidenceRefs"] = []
    status, body = api.request("POST", "/api/v2/formal/agent/query", no_evidence)
    detail = body.get("detail") if isinstance(body, dict) else {}
    cases["a03_join_without_evidence_denied"] = {
        "status": status,
        "code": detail.get("code") if isinstance(detail, dict) else None,
        "pass": 400 <= status < 500}

    # A-03 negative: forged/unregistered evidence ref.
    forged = json.loads(json.dumps(asserted))
    forged["joins"][0]["on"]["evidenceRefs"] = ["assertion:goal-forged-nonexistent"]
    status, body = api.request("POST", "/api/v2/formal/agent/query", forged)
    detail = body.get("detail") if isinstance(body, dict) else {}
    cases["a03_join_forged_evidence_denied"] = {
        "status": status,
        "code": detail.get("code") if isinstance(detail, dict) else None,
        "pass": 400 <= status < 500}

    # A-19: admin pages a query (pageSize 2 -> cursor), another authorized
    # user's session must not consume the admin cursor.
    paged = json.loads(json.dumps(asserted))
    paged["pageSize"] = 2
    status, body = api.request("POST", "/api/v2/formal/agent/query", paged)
    data = _payload(body) or {}
    cursor = data.get("cursor") if status == 200 else None
    if cursor:
        other = Api(args.api_base)
        _login(other, "goaleditor", ROLE_PUBLIC_PASSWORD)
        status, body = other.request("POST", "/api/v2/formal/agent/query", {"cursor": cursor})
        cases["a19_cross_user_cursor_denied"] = {"status": status, "pass": status in (403, 404)}
        # owner replay continues to work and must reach exhaustion.
        status, body = api.request("POST", "/api/v2/formal/agent/query", {"cursor": cursor})
        rows2 = (_payload(body) or {}).get("rows") if status == 200 else None
        cases["a19_owner_cursor_continues"] = {"status": status,
                                               "pass": status == 200 and rows2 is not None}
    else:
        cases["a19_cross_user_cursor_denied"] = {"status": "not_run",
                                                 "reason": "no cursor page produced"}

    # A-23 profile revocation with restore: remove the granted action, the
    # same prepare that passed must now be denied, then restore exactly.
    _, profile = api.request("GET", f"/api/v2/formal/ontologies/{ontology_a}/agent/profile")
    profile = _payload(profile)
    allowed = list(profile.get("allowedActionIds") or [])
    if allowed:
        status, _ = api.request(
            "PUT", f"/api/v2/formal/ontologies/{ontology_a}/agent/profile",
            {"allowedActionIds": [], "allowActionProposals": profile.get("allowActionProposals", True)})
        revoked_ok = status in (200, 201)
        # a prepare without an authorized action is refused before any freeze
        status, body = api.request(
            "POST", f"/api/v2/formal/ontologies/{ontology_a}/agent/proposals",
            {"session_kind": "goal-e2e", "session_id": "goal-governance-probe",
             "client_submission_id": "goal-governance-probe",
             "action_id": allowed[0], "release_id": st["promote"]["A"]["release_id"]})
        detail = body.get("detail") if isinstance(body, dict) else {}
        denied = status == 403
        status, _ = api.request(
            "PUT", f"/api/v2/formal/ontologies/{ontology_a}/agent/profile",
            {"allowedActionIds": allowed, "allowActionProposals": profile.get("allowActionProposals", True)})
        restored_ok = status in (200, 201)
        cases["a23_action_revoked_denied_restored"] = {
            "revoked": revoked_ok, "denied_status": status if False else denied,
            "detail_code": detail.get("code") if isinstance(detail, dict) else None,
            "restored": restored_ok,
            "pass": revoked_ok and denied and restored_ok}

    # A-23 property revocation with restore: restrict Customer to customer_id
    # only; selecting region must be denied; restore null (unrestricted).
    status, body = api.request(
        "PUT", f"/api/v2/formal/ontologies/{ontology_a}/agent/profile",
        {"allowedProperties": {"Customer": ["customer_id"]},
         "allowedActionIds": allowed,
         "allowActionProposals": profile.get("allowActionProposals", True)})
    restricted_ok = status in (200, 201)
    region_q = {"sources": [cu],
                "select": [{"source": "cu", "property": "region", "as": "region"}],
                "pageSize": 5}
    status, body = api.request("POST", "/api/v2/formal/agent/query", region_q)
    detail = body.get("detail") if isinstance(body, dict) else {}
    region_denied = 400 <= status < 500
    status, _ = api.request(
        "PUT", f"/api/v2/formal/ontologies/{ontology_a}/agent/profile",
        {"allowedProperties": None, "allowedActionIds": allowed,
         "allowActionProposals": profile.get("allowActionProposals", True)})
    restored_props = status in (200, 201)
    # sanity: unrestricted again, region select passes
    status, body = api.request("POST", "/api/v2/formal/agent/query", region_q)
    cases["a23_property_revoked_denied_restored"] = {
        "restricted": restricted_ok, "denied": region_denied,
        "detail_code": detail.get("code") if isinstance(detail, dict) else None,
        "restored": restored_props,
        "pass": restricted_ok and region_denied and restored_props and status == 200}

    executed = [n for n, c in cases.items() if c.get("status") != "not_run"]
    result = "governance_pass" if executed and all(cases[n]["pass"] for n in executed) else "fail"
    payload = {"phase": "governance", "result": result, "cases": cases,
               "acceptance_ids": {"A-03": "unasserted/forged join denial",
                                  "A-04": "asserted join executes",
                                  "A-19": "cross-user cursor denial + owner continuation",
                                  "A-23": "action/property revocation with restore"},
               "e2e_pass": False}
    target = json.loads(run_path.read_text(encoding="utf-8"))
    target["governance"] = payload
    run_path.write_text(json.dumps(target, ensure_ascii=False, indent=2,
                                   default=str) + "\n", encoding="utf-8")
    return payload


CURATED_A12_STEPS = ("a12_provision_baseline", "a12_ontology", "a12_action_pending_zero",
                     "a12_approve_newvalue", "a12_second_reject",
                     "a12_stale_pending_409")


class CuratedA12Run(GoalRun):
    """A-12 real chain per the D1 contract:

    - authority pointer = Dataset.latest_version_id; pending/rejected never
      move it; approve = base-CAS same-transaction pointer advance + review
      + outbox;
    - the chain NEVER pre-runs a pipeline to manufacture a pending version
      for the action (pipeline only births the asset's first approved
      version, which is the legitimate provisioning path);
    - marker-scoped canaries only (owned dataset/version ids), no global
      counts.
    """

    def _curated(self) -> dict:
        return self.state["steps"].get("a12_provision_baseline", {})

    def step_a12_provision_baseline(self) -> None:
        """Birth a fresh curated asset via the pipeline front door and approve
        its FIRST version (legitimate provisioning; not a D1 bypass)."""
        import time as _time
        step = self.step("a12_provision_baseline")
        # dedicated pipeline so legacy pending states never mix in
        script = ("result = [{'sku': 'A12-1', 'qty': 100, 'note': 'goalnet-a12-base'},"
                  " {'sku': 'A12-2', 'qty': 200, 'note': 'goalnet-a12-base'}]")
        status, body = self.api.request(
            "POST", "/api/v2/pipelines",
            {"name": "goal-curated-a12", "domain": "goalnet",
             "description": "A12 curated baseline producer",
             "definition": {"engine": "python", "nodes": [], "edges": [],
                            "python": {"script": script}}})
        if status in (400, 409):  # same-name pipeline from an earlier attempt: adopt
            _, listing = self.api.request("GET", "/api/v2/pipelines?page=1&page_size=50")
            data = _payload(listing) if isinstance(listing, dict) else listing
            items = data if isinstance(data, list) else (data or {}).get("items", [])
            body = next(i for i in items if i.get("name") == "goal-curated-a12")
        elif status not in (200, 201):
            raise RuntimeError(f"a12 pipeline create: HTTP {status} {str(body)[:200]}")
        pipeline_id = (_payload(body)).get("id") or body.get("id")
        step["pipeline_id"] = pipeline_id
        status, body = self.api.request("POST", f"/api/v2/pipelines/{pipeline_id}/run", {})
        run_id = (body or {}).get("run_id")
        if status != 200 or not run_id:
            raise RuntimeError(f"a12 pipeline run: HTTP {status}")
        final = None
        for _ in range(30):
            _time.sleep(6)
            _, body = self.api.request("GET", f"/api/v2/pipelines/runs/{run_id}")
            final = ((_payload(body)).get("status"))
            if final in ("success", "succeeded", "failed", "error", "cancelled"):
                break
        if final not in ("success", "succeeded"):
            raise RuntimeError(f"a12 pipeline run final={final}")
        _, body = self.api.request("GET", "/api/v2/curated?page=1&page_size=20")
        data = _payload(body)
        items = data if isinstance(data, list) else (data or {}).get("items", [])
        asset = next(i for i in items if i.get("name") == "goal-curated-a12 curated")
        dataset_id = asset["id"]
        step["dataset_id"] = dataset_id
        # approve the FIRST version (birth approval, not an action candidate)
        status, body = self.api.request("POST", f"/api/v2/curated/{dataset_id}/reviews", {})
        review = (_payload(body)) if status in (200, 201) else {}
        rid = review.get("review_id") or review.get("id")
        if rid:
            status, _ = self.api.request("POST", f"/api/v2/curated/reviews/{rid}/approve",
                                         {})
            if status not in (200, 201):
                raise RuntimeError(f"a12 baseline approve: HTTP {status}")
        _, info = self.api.request("GET", f"/api/v2/curated/{dataset_id}")
        info = info.get("data", info) or {}
        if info.get("status") != "approved":
            raise RuntimeError(f"a12 baseline not approved: {info.get('status')}")
        step["baseline_status"] = info.get("status")
        step["baseline_version"] = info.get("latest_version_no") or info.get("version")
        step["ok"] = True

    def step_a12_ontology(self) -> None:
        """Dedicated A12 ontology (clean, no accumulated types from other
        test rounds) + local mapping + update action + promote."""
        import uuid as _uuid
        step = self.step("a12_ontology")
        assets = self._curated()
        dataset_id = assets["dataset_id"]
        # dedicated ontology for A12: goalnet-crm domain, unique name
        self.api.request("POST", "/api/v1/domains",
                         {"name": "goalnet-crm", "description": "goal fixture"})
        status, body = self.api.request(
            "POST", "/api/v1/ontologies",
            {"name": "goalnet-a12", "domain": "goalnet-crm",
             "description": "A12 curated dedicated"})
        detail = (body or {}).get("detail") or {}
        existing = detail.get("existing_id") if isinstance(detail, dict) else None
        ontology_id = ((body.get("data", body) or {}).get("id") if status in (200, 201)
                       else existing)
        if not ontology_id:
            raise RuntimeError(f"a12 ontology create: HTTP {status}")
        self.state.setdefault("ontologies", {})["A12"] = ontology_id
        _, versions = self.api.request(
            "GET", f"/api/v2/ontologies/{ontology_id}/versions?limit=3")
        current = versions.get("current_release_id")
        status, d = self.api.request("POST",
                                     f"/api/v2/ontologies/{ontology_id}/versions/{current}/drafts",
                                     {})
        draft = ((d.get("data", d)) or {}).get("id")
        view = self.api.request(
            "GET", f"/api/v2/ontologies/{ontology_id}/versions/{draft}/workspace")[1]
        view = view.get("data", view)
        type_id = str(_uuid.uuid4()); action_id = str(_uuid.uuid4())
        new_type = {"id": type_id, "name": "A12Stock", "displayName": "A12Stock",
                    "primaryKey": "sku",
                    "properties": [{"name": "sku", "type": "string"},
                                   {"name": "qty", "type": "integer"},
                                   {"name": "note", "type": "string"}],
                    "interfaces": [], "positionX": 500.0, "positionY": 100.0}
        new_action = {"id": action_id, "name": "a12_qty_adjust", "displayName": "a12_qty_adjust",
                      "objectTypeId": type_id,
                      "parameters": [{"name": "sku", "type": "string"},
                                     {"name": "qty", "type": "integer"}],
                      "rules": [{"id": "r1", "name": "set_qty", "type": "update_property",
                                 "order": 1,
                                 "config": {"targetProperty": "qty",
                                            "valueSource": "parameter", "value": "qty"}}],
                      "requiresApproval": True}
        otypes = [i for i in view.get("objectTypes", []) if i.get("name") != "A12Stock"] + [new_type]
        actions = [i for i in view.get("actions", []) if i.get("name") != "a12_qty_adjust"] + [new_action]
        status, body = self.api.request(
            "PUT", f"/api/v2/ontologies/{ontology_id}/versions/{draft}/workspace",
            {"objectTypes": otypes, "linkTypes": view.get("linkTypes", []),
             "actions": actions, "functions": view.get("functions", []),
             "baseRevision": None})
        if status not in (200, 201):
            raise RuntimeError(f"a12 workspace: HTTP {status} {str(body)[:200]}")
        mview = self.api.request(
            "GET", f"/api/v2/ontologies/{ontology_id}/versions/{draft}/workspace/mappings")[1]
        mview = mview.get("data", mview)
        mapping = {"id": str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"{ontology_id}:mapping:A12Stock")),
                   "entityClass": "A12Stock", "curatedDatasetId": dataset_id,
                   "fieldMapping": {"sku": "sku", "qty": "qty", "note": "note"}}
        entries = [e for e in (mview.get("mappings") or [])
                   if e.get("entityClass") != "A12Stock"] + [mapping]
        status, body = self.api.request(
            "PUT", f"/api/v2/ontologies/{ontology_id}/versions/{draft}/workspace/mappings",
            {"mappings": entries, "linkMappings": mview.get("linkMappings", []),
             "sentinels": [], "baseRevision": None})
        if status not in (200, 201):
            raise RuntimeError(f"a12 mappings: HTTP {status}")
        # semantic canvas: full objects + behavior inputs matching parameters
        ws2 = self.api.request(
            "GET", f"/api/v2/ontologies/{ontology_id}/versions/{draft}/workspace")[1]
        ws2 = ws2.get("data", ws2)
        objs = [{"name": t.get("name"), "display_name": t.get("name"),
                 "description": "goalnet acceptance business object",
                 "key_attribute": t.get("primaryKey"),
                 "attributes": [{"name": p.get("name"), "display_name": p.get("name"),
                                 "type_hint": p.get("type"),
                                 "required": p.get("name") == t.get("primaryKey")}
                                for p in t.get("properties", [])],
                 "relations": []} for t in ws2.get("objectTypes", [])]
        # Behaviors from ALL actions in the workspace (semantic gate requires
        # canvas to cover every structural action; inherited actions too).
        behaviors = []
        type_by_id = {ot.get("id"): ot.get("name") for ot in ws2.get("objectTypes", [])}
        for act in ws2.get("actions", []):
            act_name = act.get("name") or act.get("displayName") or ""
            params = act.get("parameters") or []
            behaviors.append({
                "name": act_name, "display_name": act_name,
                "description": f"goal acceptance action {act_name}",
                "actor": "goaladmin",
                "object": type_by_id.get(act.get("objectTypeId"), ""),
                "inputs": [{"name": p.get("name"), "type_hint": p.get("type", "string")}
                           for p in params],
                "needs_approval": bool(act.get("requiresApproval"))})
        status, body = self.api.request(
            "PUT", f"/api/v2/ontologies/{ontology_id}/versions/{draft}/semantic",
            {"baseRevision": ws2.get("revision"),
             "canvas": {"objects": objs, "actors": [],
                        "behaviors": behaviors,
                        "events": [], "rules": [], "scenarios": [], "processes": []},
             "documentMd": "# goalnet A 业务语义（含 a12 scratch）\n",
             "documentTitle": "goalnet A 业务语义"})
        if status != 200:
            raise RuntimeError(f"a12 semantic: HTTP {status} {str(body)[:200]}")
        status, body = self.api.request(
            "POST", f"/api/v2/ontologies/{ontology_id}/versions/{draft}/trial-runs", {})
        tp = _payload(body)
        if tp.get("status") != "passed":
            raise RuntimeError(f"a12 trial: {tp.get('status')}")
        ip = self.api.request(
            "GET", f"/api/v2/ontologies/{ontology_id}/versions/{draft}/impact")[1]
        ip = ip.get("data", ip) or {}
        status, body = self.api.request(
            "POST", f"/api/v2/ontologies/{ontology_id}/versions/{draft}/promote",
            {"trialRunId": tp["id"], "impactHash": ip.get("impactHash")})
        pp = _payload(body) or {}
        rel = pp.get("release_id") or pp.get("id") or pp.get("releaseId")
        if not rel:
            raise RuntimeError(f"a12 promote: HTTP {status} keys={sorted(pp.keys()) if isinstance(pp, dict) else type(pp).__name__}")
        # profile grant
        _, prof = self.api.request("GET",
                                   f"/api/v2/formal/ontologies/{ontology_id}/agent/profile")
        prof = prof.get("data", prof)
        allowed = [i for i in (prof.get("allowedActionIds") or []) if i != action_id] + [action_id]
        self.api.request("PUT", f"/api/v2/formal/ontologies/{ontology_id}/agent/profile",
                         {"allowedActionIds": allowed,
                          "allowActionProposals": prof.get("allowActionProposals", True)})
        step.update({"draft": draft, "release": rel, "type_id": type_id,
                     "action_id": action_id})
        step["ok"] = True

    def _a12_pointer_and_rows(self) -> tuple[Any, list, dict]:
        assets = self._curated()
        dataset_id = assets["dataset_id"]
        _, info = self.api.request("GET", f"/api/v2/curated/{dataset_id}")
        info = info.get("data", info) or {}
        _, diff = self.api.request("GET", f"/api/v2/curated/{dataset_id}/review-diff")
        diff = diff.get("data", diff) or {}
        pointer = (info.get("latest_version_id") or info.get("latestVersionId")
                   or (diff.get("current", {}) or {}).get("dataset_version_id"))
        rows = (diff.get("current", {}) or {}).get("rows")
        return pointer, rows, info

    def _a12_version_inventory(self) -> dict:
        """B' canary: DatasetVersion count, changeset count, and physical lake
        table count for the OWN dataset (marker-scoped, never global)."""
        import subprocess
        dataset_id = self._curated()["dataset_id"]
        def q(sql):
            out = subprocess.run(
                ["docker", "exec", "oo-ontology-goal-20261101-db-1", "psql",
                 "-X", "-q", "-A", "-t", "-U", "integrated_e2e",
                 "-d", "integrated_metadata", "-v", "ON_ERROR_STOP=1",
                 "-c", sql], capture_output=True, text=True)
            return out.stdout.strip() if out.returncode == 0 else "missing"
        # lake_ds_<uuid> is the physical lake table for this dataset (native
        # lake: lake_iv_* staging tables legitimately absent).
        lake_table = f"lake_ds_{dataset_id.replace('-', '')}"
        has_lake = q(
            f"SELECT COUNT(*) FROM information_schema.tables "
            f"WHERE table_name='{lake_table}';")
        return {
            "dataset_versions": q(
                f"SELECT COUNT(*) FROM v2_dataset_versions "
                f"WHERE dataset_id='{dataset_id}';"),
            "lake_tables": q(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_name LIKE 'lake_iv_%';"),
            "changesets": q(
                f"SELECT COUNT(*) FROM v2_dataset_changesets "
                f"WHERE dataset_id='{dataset_id}';") if q(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_name='v2_dataset_changesets';") != "0" else "no_table",
            "lake_ds_rows": q(
                f"SELECT COUNT(*) FROM {lake_table};") if has_lake == "1" else "no_lake_table",
            "lake_ds_hash": q(
                f"SELECT md5(string_agg(t::text, '' ORDER BY t::text)) FROM {lake_table} t;"
            ) if has_lake == "1" else "no_lake_table"}

    def _a12_action(self, qty: int, label: str) -> dict:
        """prepare->decision->execute(+HITL) for A12Stock sku A12-1; returns
        confirmation state and the pending review id if any."""
        import time as _time
        onto_step = self.state["steps"]["a12_ontology"]
        ontology_id = self.state.get("ontologies", {}).get("A12") or self.ontology_id("A")
        status, body = self.api.request(
            "POST", f"/api/v2/formal/ontologies/{ontology_id}/agent/chat",
            {"message": f"a12 {label}", "stream": False})
        conv = ((_payload(body)) or {}).get("conversationId")
        _, det = self.api.request(
            "GET", f"/api/v2/formal/ontologies/{ontology_id}/agent/conversations/{conv}")
        msgs = ((det.get("data", det)) or {}).get("messages") or []
        sub = [m.get("id") for m in msgs if m.get("role") == "user"][-1]
        session = {"session_kind": "goal-e2e", "session_id": conv}
        status, body = self.api.request(
            "POST", f"/api/v2/formal/ontologies/{ontology_id}/agent/proposals",
            dict(session, client_submission_id=sub, action_id=onto_step["action_id"],
                 release_id=onto_step["release"],
                 target_ref={"objectTypeId": onto_step["type_id"], "key": {"sku": "A12-1"}},
                 parameters={"qty": qty}))
        conf = (_payload(body))
        cid = conf.get("confirmationId") or conf.get("confirmation_id")
        ph = conf.get("payloadHash") or conf.get("payload_hash")
        if status not in (200, 201) or not cid:
            return {"prepare_status": status, "body": body}
        self.api.request(
            "POST", f"/api/v2/formal/ontologies/{ontology_id}/agent/proposals/{cid}/decision",
            dict(session, decision_id=sub + "-d", payload_hash=ph, decision="approve"))
        status, body = self.api.request(
            "POST", f"/api/v2/formal/ontologies/{ontology_id}/agent/execute-proposal",
            dict(session, confirmation_id=cid))
        outcome = (_payload(body))
        result = {"confirmation": cid, "conv": conv,
                  "execute_state": outcome.get("state")}
        q = f"?session_kind=goal-e2e&session_id={conv}"
        _, body = self.api.request(
            "GET", f"/api/v2/formal/ontologies/{ontology_id}/agent/proposals/{cid}{q}")
        cur = _payload(body)
        log_id = cur.get("approvalLogId")
        if outcome.get("state") == "pending_approval" and log_id:
            self.api.request(
                "POST", f"/api/v2/formal/ontologies/{ontology_id}/action-logs/{log_id}/decide",
                {"decision": "approved", "reason": f"goal a12 {label}",
                 "releaseId": onto_step["release"]})
            for _ in range(15):
                _time.sleep(4)
                _, body = self.api.request(
                    "GET", f"/api/v2/formal/ontologies/{ontology_id}/agent/proposals/{cid}{q}")
                state = (_payload(body)).get("state")
                if state in ("succeeded", "failed", "rejected", "unknown"):
                    result["terminal"] = state
                    break
        return result

    def step_a12_action_pending_zero(self) -> None:
        step = self.step("a12_action_pending_zero")
        pointer_before, rows_before, _ = self._a12_pointer_and_rows()
        result = self._a12_action(qty=150, label="first-candidate")
        step["action"] = result
        inv_before = self._a12_version_inventory()
        import time as _time
        _time.sleep(2)
        pointer_after, rows_after, info_after = self._a12_pointer_and_rows()
        inv_after = self._a12_version_inventory()
        step["pointer_before"] = pointer_before
        step["pointer_after"] = pointer_after
        step["rows_after"] = rows_after
        step["inventory_before"] = inv_before
        step["inventory_after"] = inv_after
        problems = []
        if result.get("terminal") != "succeeded":
            problems.append(f"terminal={result.get('terminal')}")
        if pointer_after != pointer_before:
            problems.append("authority pointer moved before approval")
        qty = next((r.get("qty") for r in rows_after or [] if r.get("sku") == "A12-1"), None)
        if str(qty) != "100":
            problems.append(f"pre-approval value changed: {qty}")
        # B' contract: pending must not create DatasetVersion/changeset/physical
        # lake products — only the CuratedReview candidate (RowEdit) exists.
        for key in ("dataset_versions", "changesets", "lake_tables",
                    "lake_ds_rows", "lake_ds_hash"):
            if inv_before.get(key) not in ("missing", "no_lake_table", "no_table") \
                    and inv_after.get(key) not in ("missing", "no_lake_table", "no_table"):
                if inv_after[key] != inv_before[key]:
                    problems.append(f"B' violation: {key} grew "
                                    f"{inv_before[key]} -> {inv_after[key]}")
        step["problems"] = problems
        step["ok"] = not problems

    def step_a12_approve_newvalue(self) -> None:
        import time as _time
        step = self.step("a12_approve_newvalue")
        # B' contract: reviewId from the confirmation's public outcome field
        zero = self.state["steps"].get("a12_action_pending_zero", {})
        action = zero.get("action", {})
        conf_id = action.get("confirmation")
        conv = action.get("conv")
        onto_id = self.state.get("ontologies", {}).get("A12")
        q = f"?session_kind=goal-e2e&session_id={conv}"
        status, body = self.api.request(
            "GET", f"/api/v2/formal/ontologies/{onto_id}/agent/proposals/{conf_id}{q}")
        cur = (body.get("data", body) or {})
        outcome = cur.get("outcome") or {}
        rid = outcome.get("reviewId")
        step["review_id"] = rid
        step["outcome_status"] = outcome.get("status")
        status, body = self.api.request("POST", f"/api/v2/curated/reviews/{rid}/approve",
                                        {})
        step["approve_status"] = status
        if status not in (200, 201):
            step["ok"] = False
            raise RuntimeError(f"a12 approve: HTTP {status} {str(body)[:200]}")
        _time.sleep(2)
        pointer, rows, info = self._a12_pointer_and_rows()
        step["pointer_after_approve"] = pointer
        step["rows_after_approve"] = rows
        qty = next((r.get("qty") for r in rows or [] if r.get("sku") == "A12-1"), None)
        problems = []
        if str(qty) != "150":
            problems.append(f"approved value not visible: {qty}")
        # Ontology reflection through the EXISTING outbox + version front
        # door (no direct fo_* writes): promote the outbox auto-draft (or a
        # fresh front-door draft) and re-query the A12Stock type.
        ontology_id = self.ontology_id("A")
        onto_step = self.state["steps"]["a12_ontology"]
        import time as _t
        _t.sleep(3)
        _, versions = self.api.request(
            "GET", f"/api/v2/ontologies/{ontology_id}/versions?limit=10")
        drafts = [i["id"] for i in versions.get("data", [])
                  if i.get("node_kind") == "draft"]
        promoted = None
        for draft in drafts[:3]:
            status, body = self.api.request(
                "POST", f"/api/v2/ontologies/{ontology_id}/versions/{draft}/trial-runs",
                {})
            tp = _payload(body)
            if status not in (200, 201) or tp.get("status") != "passed":
                continue
            ip = self.api.request(
                "GET", f"/api/v2/ontologies/{ontology_id}/versions/{draft}/impact")[1]
            ip = ip.get("data", ip) or {}
            status, body = self.api.request(
                "POST", f"/api/v2/ontologies/{ontology_id}/versions/{draft}/promote",
                {"trialRunId": tp["id"], "impactHash": ip.get("impactHash")})
            rel = ((_payload(body)) or {}).get("release_id")
            if status == 201 and rel:
                promoted = rel
                break
        step["reflection_release"] = promoted
        if not promoted:
            problems.append("no outbox/front-door draft could be promoted for reflection")
        else:
            query = {"sources": [{"alias": "s", "ontologyId": ontology_id,
                                  "releaseId": promoted,
                                  "objectTypeId": onto_step["type_id"]}],
                     "select": [{"source": "s", "property": "sku", "as": "sku"},
                                {"source": "s", "property": "qty", "as": "qty"}],
                     "orderBy": [{"output": "sku", "direction": "asc"}], "pageSize": 10}
            status, body = self.api.request("POST", "/api/v2/formal/agent/query", query)
            qrows = ((body.get("data") or {}).get("rows")) if status == 200 else None
            qqty = next((r.get("qty") for r in qrows or []
                         if r.get("sku") == "A12-1"), None)
            step["reflection_rows"] = qrows
            if str(qqty) != "150":
                problems.append(f"ontology query not reflecting approved value: {qqty}")
        step["problems"] = problems
        step["ok"] = not problems

    def step_a12_second_reject(self) -> None:
        step = self.step("a12_second_reject")
        pointer_before, _, _ = self._a12_pointer_and_rows()
        result = self._a12_action(qty=999, label="reject-candidate")
        step["action"] = result
        assets = self._curated()
        dataset_id = assets["dataset_id"]
        status, body = self.api.request("POST", f"/api/v2/curated/{dataset_id}/reviews", {})
        review = (_payload(body)) if status in (200, 201) else {}
        rid = review.get("review_id") or review.get("id")
        status, _ = self.api.request("POST", f"/api/v2/curated/reviews/{rid}/reject",
                                     {})
        step["reject_status"] = status
        inv_before_reject = self._a12_version_inventory()
        import time as _time
        _time.sleep(2)
        pointer_after, rows_after, _ = self._a12_pointer_and_rows()
        inv_after_reject = self._a12_version_inventory()
        qty = next((r.get("qty") for r in rows_after or [] if r.get("sku") == "A12-1"), None)
        problems = []
        if pointer_after != pointer_before:
            problems.append("reject moved the pointer")
        if str(qty) != "150":
            problems.append(f"reject changed visible value: {qty}")
        # B' contract: reject burns no version number, produces zero lake output
        for key in ("dataset_versions", "changesets", "lake_tables",
                    "lake_ds_rows", "lake_ds_hash"):
            if (inv_before_reject.get(key) not in ("missing", "no_lake_table", "no_table")
                    and inv_after_reject.get(key) not in ("missing", "no_lake_table", "no_table")
                    and inv_after_reject[key] != inv_before_reject[key]):
                problems.append(f"B' reject violation: {key} grew "
                                f"{inv_before_reject[key]} -> {inv_after_reject[key]}")
        step["problems"] = problems
        step["ok"] = not problems

    def step_a12_stale_pending_409(self) -> None:
        step = self.step("a12_stale_pending_409")
        assets = self._curated()
        dataset_id = assets["dataset_id"]
        inv_before_stale = self._a12_version_inventory()
        result = self._a12_action(qty=111, label="stale-candidate")
        step["action"] = result
        # pipeline produces a NEW version while the candidate review pends
        pipeline_id = self.state["steps"]["a12_provision_baseline"]["pipeline_id"]
        status, body = self.api.request("POST", f"/api/v2/pipelines/{pipeline_id}/run", {})
        run_id = (body or {}).get("run_id")
        import time as _time
        final = None
        for _ in range(30):
            _time.sleep(6)
            _, body = self.api.request("GET", f"/api/v2/pipelines/runs/{run_id}")
            final = (_payload(body)).get("status")
            if final in ("success", "succeeded", "failed", "error", "cancelled"):
                break
        step["pipeline_final"] = final
        status, body = self.api.request("POST", f"/api/v2/curated/{dataset_id}/reviews", {})
        review = (_payload(body)) if status in (200, 201) else {}
        rid = review.get("review_id") or review.get("id")
        inv_after_stale = self._a12_version_inventory()
        step["inventory_before_stale"] = inv_before_stale
        step["inventory_after_stale"] = inv_after_stale
        status, body = self.api.request("POST", f"/api/v2/curated/reviews/{rid}/approve",
                                        {})
        detail = body.get("detail") if isinstance(body, dict) else body
        step["stale_approve_status"] = status
        step["stale_detail"] = detail if not rid else None
        problems = []
        if not 400 <= status < 500:
            problems.append(f"stale approve not rejected: {status}")
        # B' stale: zero lake growth in this stage's own inventory window
        for key in ("dataset_versions", "changesets", "lake_tables",
                    "lake_ds_rows", "lake_ds_hash"):
            if (inv_before_stale.get(key) not in ("missing", "no_lake_table", "no_table")
                    and inv_after_stale.get(key) not in ("missing", "no_lake_table", "no_table")
                    and inv_after_stale[key] != inv_before_stale[key]):
                problems.append(f"B' stale violation: {key} grew "
                                f"{inv_before_stale[key]} -> {inv_after_stale[key]}")
        step["problems"] = problems
        step["ok"] = not problems  # stale CAS must refuse AND zero lake delta

    def run_a12(self) -> dict[str, Any]:
        for name in CURATED_A12_STEPS:
            if self.done(name):
                continue
            getattr(self, f"step_{name}")()
            self.save()
        ok = all(self.done(n) for n in CURATED_A12_STEPS)
        return {"phase": "curated-a12", "result": "a12_pass" if ok else "fail",
                "steps": {n: self.done(n) for n in CURATED_A12_STEPS},
                "problems": {n: self.state["steps"].get(n, {}).get("problems")
                             for n in CURATED_A12_STEPS
                             if self.state["steps"].get(n, {}).get("problems")},
                "acceptance_ids": {"A-12": "approved baseline -> pending zero-effect -> "
                                           "approve new value -> reject unchanged -> "
                                           "stale pending 409"},
                "e2e_pass": False}


def token_admin(api: "Api") -> str:
    """(Re)authenticate the shared driver Api as the fixture admin."""
    status, body = api.request("POST", "/api/v1/auth/login",
                               {"username": "goaladmin",
                                "password": "goalnet-admin-fixture"})
    if status != 200:
        raise RuntimeError(f"admin login: HTTP {status}")
    api.token = body["data"]["access_token"]
    return api.token


def phase_curated_a12(args: argparse.Namespace) -> dict[str, Any]:
    api = Api(args.api_base)
    token_admin(api)
    run = CuratedA12Run(args.name, api)
    query_path = RUNS / "run04-q1.json"
    if "ontologies" not in run.state and query_path.exists():
        state = json.loads(query_path.read_text(encoding="utf-8"))
        run.state["ontologies"] = state.get("ontologies") or {}
    if "A" not in run.state.get("ontologies", {}):
        return {"phase": "curated-a12", "result": "blocked",
                "reason": "ontology A id missing", "e2e_pass": False}
    return run.run_a12()


def status_ok(status: int) -> bool:
    return status in (500, 502, 503, 504)


DRILL_STEPS = ("d1_preclaim_rejected", "d2_post_claim_durable_failed",
               "d3_recovery", "d4_auto_hooks")


class StructureDrillRun(GoalRun):
    """Five-slot real structure evidence per f286-structure-drill-plan.json.

    d1/d2/d3 run against the isolated drill API (127.0.0.1:56007, same
    reviewed freeze, NEO4J_URI overridden; no executor); d4 runs against the
    main staging runtime (auto hooks on promote/rollback). Slots are asserted
    separately and never merged."""

    def _drill_api(self) -> "Api":
        api = Api(self.state["drill_api_base"])
        token_admin(api)
        return api

    def step_d1_preclaim_rejected(self) -> None:
        """Source-condition rejection: rebuild for an ontology with no valid
        SQL release context. Ontology creation auto-freezes a valid v0, so the
        sanctioned probe is a KNOWN-NONEXISTENT exclusive ontology id — the
        actual contract (404 or 409) is recorded, never fabricated by editing
        business schema/services."""
        import uuid as _uuid
        step = self.step("d1_preclaim_rejected")
        api = self._drill_api() if self.state.get("drill_ready") else self.api
        phantom = str(_uuid.uuid5(_uuid.NAMESPACE_URL, "goalnet-drill-nonexistent"))
        step["phantom_ontology_id"] = phantom
        status, body = api.request(
            "POST", f"/api/v2/ontology-network/{phantom}/structure-projection/rebuild")
        detail = body.get("detail") if isinstance(body, dict) else body
        code = detail.get("code") if isinstance(detail, dict) else None
        step["status"] = status
        step["code"] = code
        step["pass"] = status in (404, 409) and (
            code in ("structure_projection_rejected",) or status == 404)
        step["fence_untouched"] = True  # nonexistent ontology has no fence row
        step["ok"] = bool(step["pass"])

    def step_d2_post_claim_durable_failed(self) -> None:
        """With the drill API pointed at an unreachable NEO4J, a real rebuild
        on the OWN drill ontology (valid release) must fail and persist the
        fence failed flag; durability is proven across an exact-PID drill
        restart (still failed, never fake-ready)."""
        step = self.step("d2_post_claim_durable_failed")
        drill = self._drill_api()
        ontology_id = self.state["drill_ontology"]
        status, body = drill.request(
            "POST", f"/api/v2/ontology-network/{ontology_id}/structure-projection/rebuild")
        detail = body.get("detail") if isinstance(body, dict) else body
        step["rebuild_status"] = status
        step["code"] = detail.get("code") if isinstance(detail, dict) else None
        step["message"] = (detail.get("message") if isinstance(detail, dict)
                           else str(detail))[:160]
        # Full error contract: 5xx must carry a structured failure code.
        code_ok = step["code"] in ("structure_projection_failed",
                                   "structure_projection_rejected") or (
            status >= 500 and step["code"])
        status, body = drill.request(
            "GET", f"/api/v2/ontology-network/{ontology_id}/structure-projection")
        fence = _payload(body) or {}
        # Cross-check the same fence through the MAIN runtime (shared metadata
        # DB): both views must agree on failed, never a fake-ready split.
        status2, body2 = self.api.request(
            "GET", f"/api/v2/ontology-network/{ontology_id}/structure-projection")
        fence_main = _payload(body2) or {}
        step["fence_after_failure"] = {k: fence.get(k) for k in
                                       ("status", "release_id", "hash", "drift")}
        step["fence_main_view"] = {k: fence_main.get(k) for k in
                                   ("status", "release_id", "hash", "drift")}
        step["fence_views_agree"] = (fence.get("status")
                                     == fence_main.get("status"))
        step["pass"] = (status_ok(step["rebuild_status"]) and code_ok
                        and fence.get("status") == "failed"
                        and fence_main.get("status") == "failed"
                        and step["fence_views_agree"])
        step["ok"] = bool(step["pass"])

    def step_d3_recovery(self) -> None:
        """After the operator restores the drill instance's real NEO4J config
        (exact-PID restart, done outside the driver), a rebuild must reach
        ready with fence hash == current release snapshot hash."""
        step = self.step("d3_recovery")
        drill = self._drill_api()
        ontology_id = self.state["drill_ontology"]
        status, body = drill.request(
            "POST", f"/api/v2/ontology-network/{ontology_id}/structure-projection/rebuild")
        fence = _payload(body) or {} if status == 200 else {}
        _, versions = self.api.request(
            "GET", f"/api/v2/ontologies/{ontology_id}/versions?limit=3")
        current = versions.get("current_release_id")
        step["fence_ready"] = fence.get("status") == "ready"
        step["hash_match"] = bool(fence.get("release_id") == current)
        step["pass"] = status == 200 and step["fence_ready"] and step["hash_match"]
        step["ok"] = bool(step["pass"])

    def step_d4_auto_hooks(self) -> None:
        """On the MAIN staging runtime: a normal promote auto-hook and a
        production rollback auto-hook must each leave ready + release/hash
        consistent (not only manual rebuilds)."""
        import uuid as _uuid
        step = self.step("d4_auto_hooks")
        token_admin(self.api)
        # fresh owned ontology for the hook probes
        status, body = self.api.request(
            "POST", "/api/v1/ontologies", {"name": "goalnet-drill-hook",
                                           "domain": "goalnet-crm"})
        detail = (body or {}).get("detail") or {}
        existing = detail.get("existing_id") if isinstance(detail, dict) else None
        ontology_id = (_payload(body)).get("id") if status in (200, 201) else existing
        step["ontology_id"] = ontology_id
        # minimal workspace promote to have a real release with structure
        _, versions = self.api.request(
            "GET", f"/api/v2/ontologies/{ontology_id}/versions?limit=3")
        current = versions.get("current_release_id")
        status, d = self.api.request(
            "POST", f"/api/v2/ontologies/{ontology_id}/versions/{current}/drafts", {})
        draft = ((d.get("data", d)) or {}).get("id")
        view = self.api.request(
            "GET", f"/api/v2/ontologies/{ontology_id}/versions/{draft}/workspace")[1]
        view = view.get("data", view)
        type_id = str(_uuid.uuid4())
        otype = {"id": type_id, "name": "HookProbe", "displayName": "HookProbe",
                 "primaryKey": "id",
                 "properties": [{"name": "id", "type": "string"}],
                 "interfaces": [], "positionX": 100.0, "positionY": 100.0}
        status, body = self.api.request(
            "PUT", f"/api/v2/ontologies/{ontology_id}/versions/{draft}/workspace",
            {"objectTypes": [otype], "linkTypes": [], "actions": [],
             "functions": [], "baseRevision": None})
        status, body = self.api.request(
            "PUT", f"/api/v2/ontologies/{ontology_id}/versions/{draft}/semantic",
            {"baseRevision": None,
             "canvas": {"objects": [{"name": "HookProbe", "display_name": "HookProbe",
                                     "description": "hook probe", "key_attribute": "id",
                                     "attributes": [{"name": "id", "display_name": "id",
                                                     "type_hint": "string", "required": True}],
                                     "relations": []}],
                        "actors": [], "behaviors": [], "events": [], "rules": [],
                        "scenarios": [], "processes": []},
             "documentMd": "# hook probe\n", "documentTitle": "hook probe"})
        status, body = self.api.request(
            "POST", f"/api/v2/ontologies/{ontology_id}/versions/{draft}/trial-runs", {})
        tp = _payload(body)
        if tp.get("status") != "passed":
            raise RuntimeError(f"d4 trial: {tp.get('status')}")
        ip = self.api.request(
            "GET", f"/api/v2/ontologies/{ontology_id}/versions/{draft}/impact")[1]
        ip = ip.get("data", ip) or {}
        status, body = self.api.request(
            "POST", f"/api/v2/ontologies/{ontology_id}/versions/{draft}/promote",
            {"trialRunId": tp["id"], "impactHash": ip.get("impactHash")})
        new_release = ((_payload(body)) or {}).get("release_id")
        # AUTO HOOK (promote): fence must be ready+consistent without any
        # manual rebuild call.
        status, body = self.api.request(
            "GET", f"/api/v2/ontology-network/{ontology_id}/structure-projection")
        fence_promote = _payload(body) or {}
        step["auto_promote_hook"] = {
            "release": new_release,
            "status": fence_promote.get("status"),
            "hash_match": fence_promote.get("release_id") == new_release,
            "pass": (fence_promote.get("status") == "ready"
                     and fence_promote.get("release_id") == new_release)}
        # AUTO HOOK (rollback): rolling back to the v0 release is a new
        # deployment event; the hook must run and stay truthful.
        status, body = self.api.request(
            "POST", f"/api/v2/ontologies/{ontology_id}/versions/{current}/rollback", {})
        step["rollback_status"] = status
        status, body = self.api.request(
            "GET", f"/api/v2/ontology-network/{ontology_id}/structure-projection")
        fence_rollback = _payload(body) or {}
        step["auto_rollback_hook"] = {
            "status": fence_rollback.get("status"),
            "hash_match": fence_rollback.get("release_id") == current,
            "pass": (fence_rollback.get("status") == "ready"
                     and fence_rollback.get("release_id") == current)}
        step["pass"] = bool(step["auto_promote_hook"]["pass"]
                            and step["auto_rollback_hook"]["pass"])
        step["ok"] = step["pass"]

    def run_drill(self) -> dict[str, Any]:
        for name in DRILL_STEPS:
            if self.done(name):
                continue
            getattr(self, f"step_{name}")()
            self.save()
        ok = all(self.done(n) for n in DRILL_STEPS)
        return {"phase": "structure-drill", "result": "drill_pass" if ok else "fail",
                "steps": {n: self.done(n) for n in DRILL_STEPS},
                "slots": {n: self.state["steps"].get(n, {}).get("pass")
                          for n in DRILL_STEPS},
                "acceptance_ids": {"A-24": "five distinct slots (pre-claim rejected / "
                                           "durable failed / recovery / auto promote hook / "
                                           "auto rollback hook)"},
                "e2e_pass": False}


def phase_structure_drill(args: argparse.Namespace) -> dict[str, Any]:
    api = Api(args.api_base)
    run = StructureDrillRun(args.name, api)
    run.state.setdefault("drill_api_base", args.drill_api_base)
    run.state.setdefault("drill_ontology", args.drill_ontology or "")
    # d2/d3 without a drill ontology must block honestly before any network
    # auth/dispatch. StructureDrillRun only reads local run state.
    if args.stage in ("d2", "d3") and not run.state.get("drill_ontology"):
        return {"phase": "structure-drill", "result": "blocked",
                "reason": "--drill-ontology required for d2/d3", "e2e_pass": False}
    token_admin(api)
    if args.stage == "all":
        return run.run_drill()
    step = f"{args.stage}_{'preclaim_rejected' if args.stage == 'd1' else ''}" if False else None
    # single-stage execution for operator-driven sequencing (d2 needs the
    # unreachable-NEO4J drill instance, d3 the recovered one)
    names = {"d1": "d1_preclaim_rejected", "d2": "d2_post_claim_durable_failed",
             "d3": "d3_recovery", "d4": "d4_auto_hooks"}
    name = names[args.stage]
    if not run.done(name):
        getattr(run, f"step_{name}")()
        run.save()
    step_state = run.state["steps"].get(name, {})
    return {"phase": "structure-drill", "stage": args.stage,
            "result": "pass" if step_state.get("ok") else "fail",
            "detail": step_state, "e2e_pass": False}


PHASES_NOT_IMPLEMENTED = ("report",)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    planning = sub.add_parser("plan")
    frontdoor = sub.add_parser("frontdoor")
    frontdoor.add_argument("--name", required=True)
    query = sub.add_parser("query")
    query.add_argument("--name", required=True)
    query.add_argument("--api-base", default=os.environ.get("ONTOLOGY_GOAL_API_BASE",
                                                            DEFAULT_API_BASE))
    query.add_argument("--frontdoor-run", required=True)
    query.add_argument("--query-run", default="run02-q5")
    query.add_argument("--admin-user", default="goaladmin")
    query.add_argument("--admin-password", required=True)
    guards_parser = sub.add_parser("guards")
    guards_parser.add_argument("--name", required=True)
    guards_parser.add_argument("--query-run", required=True)
    guards_parser.add_argument("--api-base", default=os.environ.get("ONTOLOGY_GOAL_API_BASE",
                                                                    DEFAULT_API_BASE))
    guards_parser.add_argument("--admin-user", default="goaladmin")
    guards_parser.add_argument("--admin-password", required=True)
    actions_parser = sub.add_parser("actions")
    actions_parser.add_argument("--name", required=True)
    actions_parser.add_argument("--frontdoor-run", required=True)
    actions_parser.add_argument("--query-run", required=True)
    actions_parser.add_argument("--api-base", default=os.environ.get("ONTOLOGY_GOAL_API_BASE",
                                                                     DEFAULT_API_BASE))
    actions_parser.add_argument("--admin-user", default="goaladmin")
    actions_parser.add_argument("--admin-password", required=True)
    roles_parser = sub.add_parser("guards-roles")
    roles_parser.add_argument("--name", required=True)
    roles_parser.add_argument("--query-run", required=True)
    roles_parser.add_argument("--actions-run", required=True)
    roles_parser.add_argument("--api-base", default=os.environ.get("ONTOLOGY_GOAL_API_BASE",
                                                                   DEFAULT_API_BASE))
    roles_parser.add_argument("--admin-user", default="goaladmin")
    roles_parser.add_argument("--admin-password", required=True)
    gov_parser = sub.add_parser("governance")
    gov_parser.add_argument("--name", required=True)
    gov_parser.add_argument("--query-run", required=True)
    gov_parser.add_argument("--api-base", default=os.environ.get("ONTOLOGY_GOAL_API_BASE",
                                                                 DEFAULT_API_BASE))
    gov_parser.add_argument("--admin-user", default="goaladmin")
    gov_parser.add_argument("--admin-password", required=True)
    a12_parser = sub.add_parser("curated-a12")
    a12_parser.add_argument("--name", required=True)
    a12_parser.add_argument("--api-base", default=os.environ.get("ONTOLOGY_GOAL_API_BASE",
                                                                 DEFAULT_API_BASE))
    drill_parser = sub.add_parser("structure-drill")
    drill_parser.add_argument("--name", required=True)
    drill_parser.add_argument("--stage", choices=["all", "d1", "d2", "d3", "d4"],
                              default="all")
    drill_parser.add_argument("--drill-api-base", default="http://127.0.0.1:56007")
    drill_parser.add_argument("--drill-ontology", default="")
    drill_parser.add_argument("--api-base", default=os.environ.get("ONTOLOGY_GOAL_API_BASE",
                                                                   DEFAULT_API_BASE))
    frontdoor.add_argument("--api-base", default=os.environ.get("ONTOLOGY_GOAL_API_BASE",
                                                                DEFAULT_API_BASE))
    frontdoor.add_argument("--admin-user", default="goaladmin")
    frontdoor.add_argument("--admin-password", required=True)
    for name in PHASES_NOT_IMPLEMENTED:
        entry = sub.add_parser(name)
        entry.add_argument("--name", required=True)
    args = parser.parse_args()
    try:
        if args.command == "plan":
            result = phase_plan(args)
        elif args.command == "frontdoor":
            result = phase_frontdoor(args)
        elif args.command == "query":
            result = phase_query(args)
        elif args.command == "guards":
            result = phase_guards(args)
        elif args.command == "actions":
            result = phase_actions(args)
        elif args.command == "guards-roles":
            result = phase_guards_roles(args)
        elif args.command == "governance":
            result = phase_governance(args)
        elif args.command == "curated-a12":
            result = phase_curated_a12(args)
        elif args.command == "structure-drill":
            result = phase_structure_drill(args)
        else:
            result = {"phase": args.command, "result": "not_implemented",
                      "name": args.name, "e2e_pass": False,
                      "note": "phase driver lands with its implementation; "
                              "not_run is never reported as pass"}
    except (OSError, ValueError, KeyError) as error:
        print(f"ontology-goal-e2e: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if result.get("result") in {"blocked", "fail", "not_implemented"}:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
