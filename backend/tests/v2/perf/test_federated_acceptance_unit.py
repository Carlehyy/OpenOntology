"""Offline contracts, not real HTTP, source-engine or scale acceptance evidence."""
from __future__ import annotations

import importlib.util
import json
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(scope="module")
def acc():
    root = Path(__file__).resolve().parents[4]
    spec = importlib.util.spec_from_file_location(
        "federated_acceptance_unit", root / "scripts/e2e/federated_acceptance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def type_ids():
    return {name: f"type-{name.lower()}" for name in ("Customer", "Contract", "Order", "Shipment")}


def run_state():
    return {"orders": 1000, "front_door_complete": True, "steps": {
        "create_ontologies": {"ontologies": {"crm": {"id": "ont-crm"}, "erp": {"id": "ont-erp"}}},
        "create_drafts": {"crm": {"version_id": "draft-crm"}, "erp": {"version_id": "draft-erp"}, "ok": True},
        "define_workspace": {
            "crm": {"object_types": {name: type_ids()[name] for name in ("Customer", "Contract")}},
            "erp": {"object_types": {name: type_ids()[name] for name in ("Order", "Shipment")}}},
        "trial": {key: {"run_id": f"trial-{key}", "impact_hash": f"impact-{key}"}
                  for key in ("crm", "erp")},
        "impact": {key: {"summary": {"impactHash": f"impact-{key}"}} for key in ("crm", "erp")},
        "promote": {key: {"release_id": f"rel-{key}"} for key in ("crm", "erp")},
        "join_assertions": {"evidence_refs": ["assertion:a", "assertion:b", "assertion:c"]}}}


def remote_page(rows, *, has_next=False, cursor=None, total=None, **changes):
    data = {"schemaVersion": 2, "queryId": "query-a", "rows": rows,
            "pageRowCount": len(rows), "totalRowCount": len(rows) if total is None else total,
            "hasNext": has_next, "cursor": cursor, "expiresAt": "2026-10-26T08:00:00+00:00",
            "persistence": {"mode": "ephemeral"}, "truncated": False, "partial": False}
    data.update(changes)
    return {"data": data}


def fake_api(acc, pages):
    class FakeApi(acc.Api):
        def __init__(self):
            super().__init__("http://127.0.0.1:52004", token="synthetic-token")
            self.pages = list(pages)
            self.calls = []

        def request(self, method, path, body=None, expect=(200,)):
            self.calls.append((method, path, body, expect))
            page = self.pages.pop(0)
            # Mirror the real Api per-call wire counters so pagination
            # transport metrics are exercised offline.
            self.last_request_bytes = len(json.dumps(body).encode()) if body is not None else 0
            self.last_response_bytes = len(json.dumps(page).encode())
            self.total_request_bytes += self.last_request_bytes
            self.total_response_bytes += self.last_response_bytes
            return 200, page

    return FakeApi()


def test_contract_registry_cites_source_without_claiming_live_evidence(acc):
    for name, entry in acc.CONTRACT.items():
        assert entry.get("evidence"), name
        assert entry.get("method") in ("GET", "POST", "PUT"), name
        assert entry.get("path"), name
    for phase in ("front-door", "query", "guards"):
        assert not acc.pending_contracts(phase)
    body = acc._snake_asset_body("a", "c1", "public", "fed_customers")
    assert set(body) == {"name", "connection_id", "schema_name", "table_name", "writable"}
    assert body["writable"] is False
    assert not any("sync" in step for step in acc.FRONT_DOOR_STEPS)


@pytest.mark.parametrize("orders", [1000, 5000])
def test_plan_queries_matches_independent_full_scan_and_null_semantics(acc, orders):
    fixture = acc.fixture
    customers, contracts = fixture.dimensions(orders)
    wanted = {7, 10, 20, 30, 40, 50, 60, 70}
    expected = {}
    for cid in sorted(wanted):
        if cid > customers or not fixture.customer_row(cid)["is_key"]:
            continue
        valid = [row for k in range(1, contracts + 1)
                 if (row := fixture.contract_row(k, customers))["customer_id"] == cid
                 and row["valid_from"] <= fixture.WINDOW_END <= row["valid_to"]]
        if not valid:
            continue
        recent = [row for o in range(1, orders + 1)
                  if (row := fixture.order_row(o, customers, contracts))["customer_ref"] == cid
                  and fixture.WINDOW_START <= row["created_at"] <= fixture.WINDOW_END]
        pending = sum(fixture.shipment_count(row["order_id"]) == 0 for row in recent)
        expected[str(cid)] = {
            "customer_id": cid, "contract_count": len(valid),
            "sum_signed_amount": str(sum((row["signed_amount"] for row in valid), Decimal(0))),
            "order_count": len(recent) if recent else None,
            "sum_gross_cents": sum(row["gross_cents"] for row in recent) if recent else None,
            "sum_net_amount": str(sum((row["net_amount"] for row in recent), Decimal(0))) if recent else None,
            "pending_order_count": pending or None}
    plan = acc.plan_queries(orders, sorted(wanted))
    assert plan["expected_canonical"] == expected
    assert "10" not in expected and "40" not in expected
    assert expected["30"]["pending_order_count"] is None
    assert expected["30"]["order_count"] == 10
    assert plan["expected_totals_subset"]["created_at_window"]["days"] == 90
    assert plan["full_e2e_pass"] is False
    all_expected = acc.plan_queries(orders, ["*"])["expected_canonical"]
    assert {key: value for key, value in all_expected.items() if int(key) in wanted} == expected


def test_plan_excludes_orders_outside_window_without_zero_filling(acc, monkeypatch):
    original = acc.fixture.order_row

    def outside_window(order_id, customers, contracts):
        row = original(order_id, customers, contracts)
        if row["customer_ref"] == 30:
            row["created_at"] = acc.fixture.WINDOW_START - timedelta(days=1)
        return row

    monkeypatch.setattr(acc.fixture, "order_row", outside_window)
    result = acc.plan_queries(1000, [30])["expected_canonical"]["30"]
    assert result["contract_count"] > 0
    for field in ("order_count", "sum_gross_cents", "sum_net_amount", "pending_order_count"):
        assert result[field] is None


def test_canonical_specs_pin_preaggregation_and_no_flat_join(acc):
    specs = acc.QUERY_SPECS
    assert specs["crm_key_customers_with_contracts"]["role"].startswith("inner anchor")
    assert specs["erp_orders_90d_totals"]["role"].startswith("LEFT subquery")
    assert "GROUP BY order_id" in specs["erp_pending_counts"]["pipeline"]
    assert "PENDING" not in specs["erp_pending_counts"]["pipeline"]
    assert "flat 4-table joins are forbidden" in specs["canonical_composition"]["preaggregation"]
    assert "zero-fill" in specs["canonical_composition"]["null_semantics"]
    assert "join-assertions" in specs["canonical_composition"]["lineage_evidence"]


def test_object_model_and_remote_mapping_contract(acc):
    assert set(acc.OBJECT_MODEL) == {"crm", "erp"}
    assert {name for model in acc.OBJECT_MODEL.values() for name in model["types"]} == set(type_ids())
    for model in acc.OBJECT_MODEL.values():
        for definition in model["types"].values():
            for prop in ("signed_amount", "credit_limit", "net_amount", "ship_cost"):
                if prop in definition["properties"]:
                    assert definition["properties"][prop] == "number"
    customer = acc.OBJECT_MODEL["crm"]["types"]["Customer"]
    entry = acc._remote_mapping_entry("ont-crm", "Customer", "ds-1", customer)
    assert entry["bindingMode"] == "remote" and entry["curatedDatasetId"] == "ds-1"
    assert "__remote__" not in entry and "remote" not in entry
    assert set(entry["fieldMapping"]) == set(customer["properties"])
    # Stable UUID5 namespace over ontology identity + entity class: identical
    # for reruns of one ontology (even with a different dataset id), distinct
    # across independently repeated fixture ontologies sharing the metadata DB.
    expected_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "ont-crm:remote-mapping:Customer"))
    assert entry["id"] == expected_id
    assert uuid.UUID(entry["id"]).version == 5
    assert acc._remote_mapping_entry("ont-crm", "Customer", "ds-2", customer)["id"] == entry["id"]
    other_ontology = acc._remote_mapping_entry(
        "ont-crm-repeat", "Customer", "ds-1", customer)["id"]
    assert other_ontology != entry["id"]
    assert acc._remote_mapping_entry(
        "ont-crm", "Contract", "ds-1", acc.OBJECT_MODEL["crm"]["types"]["Contract"])["id"] != entry["id"]
    workspace_type = acc._workspace_object_type("Customer", acc.OBJECT_MODEL["crm"]["types"]["Customer"])
    assert workspace_type["primaryKey"] == "customer_id"
    assert {"name": "is_key", "type": "boolean"} in workspace_type["properties"]
    order = acc._workspace_object_type("Order", acc.OBJECT_MODEL["erp"]["types"]["Order"])
    assert {"name": "created_at", "type": "date"} in order["properties"]


def test_canonical_query_request_shape_and_validated_schema(acc):
    from app.ontologies.agent_query.schemas import QueryRequest

    query = acc.build_canonical_query("rel-crm", "rel-erp", ["assertion:a", "assertion:b", "assertion:c"],
                                      "ont-crm", "ont-erp", type_ids())
    QueryRequest.model_validate(query)
    assert len(query["sources"]) == 3 and 1 <= query["pageSize"] <= 200
    assert len({source["alias"] for source in query["sources"]}) == 3
    for source in query["sources"]:
        assert source["subquery"] is not None and "ontologyId" not in source
    pending = next(s for s in query["sources"] if s["alias"] == "pending")["subquery"]
    assert any(inner.get("subquery") is not None for inner in pending["sources"])
    assert pending["joins"][0]["on"]["evidenceRefs"] == ["assertion:c"]
    for join in query["joins"]:
        assert join["on"]["kind"] == "explicit_key"
        assert join["on"]["evidenceRefs"] == ["assertion:b"]
    anchor = next(s for s in query["sources"] if s["alias"] == "crm")["subquery"]
    assert anchor["joins"][0]["on"]["evidenceRefs"] == ["assertion:a"]
    assert anchor["sources"][0]["objectTypeId"] == "type-customer"
    assert anchor["sources"][1]["objectTypeId"] == "type-contract"
    assert {item["as"] for item in anchor["aggregates"]} == {"contract_count", "sum_signed_amount"}
    assert {(item["property"], item["op"]) for item in anchor["where"]} == {
        ("is_key", "eq"), ("valid_from", "lte"), ("valid_to", "gte")}
    assert {(item["property"], item["op"]) for item in pending["where"]} == {
        ("order_id", "is_null"), ("created_at", "gte"), ("created_at", "lte")}
    totals = next(s for s in query["sources"] if s["alias"] == "totals")["subquery"]
    assert {(item["property"], item["op"]) for item in totals["where"]} == {
        ("created_at", "gte"), ("created_at", "lte")}
    with pytest.raises(ValueError, match="all three"):
        acc.build_canonical_query("r1", "r2", ["assertion:a", "assertion:b"], "o1", "o2", type_ids())


def test_collect_all_pages_walks_memory_cursors(acc):
    payloads = [
        remote_page([{"a": 1}], total=3, has_next=True, cursor="mem.one",
                    plan={"strategy": "memory_federated", "operators": ["join", "stats"],
                          "sourceRows": {"o": 1000000}, "joinRows": 2135},
                    sourceStates=[{"alias": "o", "consistency": "immutable_version"}]),
        remote_page([{"a": 2}], total=3, has_next=True, cursor="mem.two"),
        remote_page([{"a": 3}], total=3)]
    api = fake_api(acc, payloads)
    rows, meta = acc.collect_all_pages(api, {"sources": []})
    assert [row["a"] for row in rows] == [1, 2, 3]
    assert api.calls[1][2] == {"cursor": "mem.one"}
    assert api.calls[2][2] == {"cursor": "mem.two"}
    assert meta["truncated"] is False and meta["partial"] is False
    assert meta["totalRowCount"] == 3
    # Execution evidence stays metrics-only: first-response plan/sourceStates
    # (never rows), the page count and the JSON wire bytes of the walk.
    assert meta["pageCount"] == 3
    assert meta["plan"] == payloads[0]["data"]["plan"]
    assert meta["sourceStates"] == [{"alias": "o", "consistency": "immutable_version"}]
    assert "rows" not in json.dumps(meta)
    assert meta["transportBytes"]["request"] == sum(
        len(json.dumps(body).encode()) for body in
        ({"sources": []}, {"cursor": "mem.one"}, {"cursor": "mem.two"}))
    assert meta["transportBytes"]["response"] == sum(len(json.dumps(p).encode()) for p in payloads)


@pytest.mark.parametrize(("pages", "message"), [
    ([remote_page([], has_next=True, cursor="mem.loop", total=1)] * 2, "repeated"),
    ([remote_page([{}] * 201)], "cap"),
    ([remote_page([], total=1, has_next=True)], "memory cursor"),
    ([remote_page([], total=1, has_next=True, cursor="legacy")], "memory cursor"),
    ([remote_page([], pageRowCount=1)], "contents"),
    ([remote_page([], total=1)], "complete result"),
    ([remote_page([], cursor="mem.last")], "complete result"),
    ([remote_page([{}], total=0)], "exceeded"),
    ([remote_page([], partial=True)], "partial"),
    ([remote_page([], truncated=True)], "truncated"),
    ([remote_page([], persistence={"mode": "persisted"})], "ephemeral"),
    ([remote_page([], queryId=None)], "identity"),
    ([remote_page([], pageRowCount=True)], "contract"),
    ([remote_page([], hasNext="false")], "contract"),
    ([{"data": {"rows": [], "page_row_count": 0, "has_next": False}}], "contract"),
    ([remote_page([{}], total=2, has_next=True, cursor="mem.one"),
      remote_page([{}], total=2, queryId="different")], "identity changed"),
    ([remote_page([{}], total=2, has_next=True, cursor="mem.one"),
      remote_page([{}], total=3)], "identity changed"),
])
def test_collect_all_pages_rejects_incomplete_or_changed_evidence(acc, pages, message):
    with pytest.raises(RuntimeError, match=message):
        acc.collect_all_pages(fake_api(acc, pages), {})


def test_collect_all_pages_enforces_max_pages(acc):
    with pytest.raises(RuntimeError, match="without exhaustion"):
        acc.collect_all_pages(fake_api(acc, [remote_page([], total=1, has_next=True, cursor="mem.one")]),
                              {}, max_pages=1)


def test_compare_decimal_exact_reports_every_mismatch(acc):
    expected = {"10": {"sum_net_amount": "955.0955", "pending_order_count": None},
                "7": {"sum_net_amount": "655.0655", "pending_order_count": None}}
    observed = {"10": {"sum_net_amount": "955.0956", "pending_order_count": 0},
                "9": {"sum_net_amount": "1.0000", "pending_order_count": None}}
    mismatches = acc.compare_decimal_exact(expected, observed)
    assert any("sum_net_amount: expected 955.0955" in item for item in mismatches)
    assert any("NULL honesty" in item and "10.pending_order_count" in item for item in mismatches)
    honest = {"10": {"sum_net_amount": "955.09550", "pending_order_count": None},
              "7": {"sum_net_amount": "655.0655", "pending_order_count": None}}
    assert acc.compare_decimal_exact(expected, honest) == []


def test_guard_evaluation_requires_exact_status_and_code(acc):
    assert acc.evaluate_guard("permission_denied", 403, {"detail": {"code": "MENU_ACCESS_DENIED"}})
    for code in ("OTHER", "prefix_MENU_ACCESS_DENIED", "MENU_ACCESS_DENIED_suffix"):
        assert not acc.evaluate_guard("permission_denied", 403, {"detail": {"code": code}})
    assert not acc.evaluate_guard("permission_denied", 200, {"detail": {"code": "MENU_ACCESS_DENIED"}})
    for case, status in (("cursor_invalid", 422), ("cursor_expired", 410),
                         ("owner_session_change", 403), ("stale_release", 409)):
        assert acc.evaluate_guard(case, status, {})
        assert not acc.evaluate_guard(case, 200, {})
    assert not acc.evaluate_guard("cursor_expired", 400, {})
    assert not acc.evaluate_guard("cursor_expired", 404, {})


def test_persistence_canary_requires_zero_delta_including_query_tables(acc):
    before = {table: 10 for table in acc.CANARY_ZERO_DELTA_TABLES}
    ok = acc.persistence_canary(before, dict(before))
    assert ok["row_counts_unchanged"] is True and ok["verdict"] == "pass"
    assert ok["no_detail_persistence_proven"] is False and not ok["missing_tables"]
    query_tables = {"v2_unified_query_snapshots", "v2_unified_query_rows",
                    "fo_agent_query_snapshots", "fo_agent_query_rows"}
    assert query_tables <= set(before)
    for table in query_tables | {"fo_property_facts"}:
        failed = acc.persistence_canary(before, {**before, table: 11})
        assert failed["verdict"] == "fail"
        assert failed["zero_delta_violations"] == {table: 1}
    assert "fo_agent_join_assertions" not in before and "v2_integrated_assets" not in before


def test_persistence_canary_rejects_missing_tables(acc):
    before = {table: 0 for table in acc.CANARY_ZERO_DELTA_TABLES}
    after = dict(before)
    del after["v2_unified_query_rows"]
    result = acc.persistence_canary(before, after)
    assert result["verdict"] == "fail" and result["row_counts_unchanged"] is False
    assert result["missing_tables"] == ["v2_unified_query_rows"]


def test_front_door_ordering_and_resume_without_secrets(acc, tmp_path, monkeypatch):
    monkeypatch.setattr(acc, "RUNS", tmp_path)
    steps = list(acc.FRONT_DOOR_STEPS)
    assert steps[0] == "login" and steps[-1] == "join_assertions"
    assert steps.index("register_assets") < steps.index("workspace_mappings")
    assert steps.index("trial") < steps.index("impact") < steps.index("promote") < steps.index("join_assertions")

    class FakeRun(acc.AcceptanceRun):
        def step_login(self):
            raise AssertionError("a completed step must never re-execute")

        def step_register_connections(self):
            self.step("register_connections").update({"ok": True})

        def step_register_assets(self):
            raise RuntimeError("stop here for the resume test")

    run = FakeRun("run-a", acc.Api("http://127.0.0.1:52004", token="synthetic-token"), 500)
    run.step("login").update({"ok": True})
    run.save()
    with pytest.raises(RuntimeError, match="stop here"):
        run.run()
    assert run.done("login") and run.done("register_connections") and not run.done("register_assets")
    saved = run.path.read_text()
    assert "synthetic-token" not in saved and "password" not in saved and "admin_user" not in saved
    with pytest.raises(RuntimeError, match="orders"):
        acc.AcceptanceRun("run-a", run.api, 10000)


def test_front_door_uses_workspace_ids_and_passed_trial_for_promotion(acc, tmp_path, monkeypatch):
    monkeypatch.setattr(acc, "RUNS", tmp_path)

    class FrontApi(acc.Api):
        def __init__(self):
            super().__init__("http://127.0.0.1:52004", token="synthetic-token")
            self.calls = []
            self.trial_status = "passed"

        def request(self, method, path, body=None, expect=(200,)):
            self.calls.append((method, path, body, expect))
            domain = "crm" if "ont-crm" in path else "erp"
            if method == "GET" and path.endswith("/workspace"):
                return 200, {"data": {"objectTypes": [
                    {"id": type_ids()[name], "name": name} for name in acc.OBJECT_MODEL[domain]["types"]]}}
            if path.endswith("/trial-runs"):
                return 201, {"data": {"id": f"trial-{domain}", "status": self.trial_status,
                                       "impact_hash": f"impact-{domain}"}}
            if path.endswith("/promote"):
                return 201, {"data": {"id": f"rel-{domain}"}}
            if path.endswith("/join-assertions"):
                return 201, {"data": {"evidenceRef": f"assertion:{len(self.calls)}"}}
            return 200, {"data": {"revision": "1:hash"}}

    api = FrontApi()
    run = acc.AcceptanceRun("front-contract", api, 1000)
    run.state = run_state()
    run.step_define_workspace()
    assert acc._type_ids(run.state) == type_ids()
    assert len([call for call in api.calls if call[0] == "GET"]) == 2
    run.step_trial()
    run.step_promote()
    promotions = [call for call in api.calls if call[1].endswith("/promote")]
    assert len(promotions) == 2
    for _, path, body, expect in promotions:
        domain = "crm" if "ont-crm" in path else "erp"
        assert body == {"trialRunId": f"trial-{domain}", "impactHash": f"impact-{domain}"}
        assert expect == (201,)
    run.step_join_assertions()
    assertions = [call for call in api.calls if call[1].endswith("/join-assertions")]
    assert len(assertions) == 3
    assert [(call[2]["leftObjectTypeId"], call[2]["rightObjectTypeId"]) for call in assertions] == [
        ("type-customer", "type-contract"), ("type-customer", "type-order"), ("type-order", "type-shipment")]
    assert assertions[-1][1].startswith("/api/v2/formal/ontologies/ont-erp/")
    api.trial_status = "failed"
    with pytest.raises(RuntimeError, match="did not pass"):
        run.step_trial()
    run.state["steps"]["impact"]["crm"]["summary"]["impactHash"] = "changed"
    with pytest.raises(RuntimeError, match="no longer matches"):
        run.step_promote()


def guard_args(**changes):
    values = {"admin_user": "fixture-admin", "admin_password": "synthetic-password",
              "viewer_user": "", "viewer_password": "", "other_user": "", "other_password": "",
              "max_expiry_wait": 1}
    values.update(changes)
    return SimpleNamespace(**values)


def test_guards_wait_for_actual_expiry_and_separate_owner_from_menu(acc, monkeypatch):
    calls, sleeps = [], []
    expiry = acc.datetime.now(acc.timezone.utc) + timedelta(seconds=0.5)

    class GuardApi(acc.Api):
        def __init__(self, base_url):
            super().__init__(base_url)
            self.username = None

        def login(self, username, password):
            self.username = username

        def request(self, method, path, body=None, expect=(200,)):
            calls.append((self.username, body))
            if self.username == "fixture-viewer":
                return 403, {"detail": {"code": "MENU_ACCESS_DENIED"}}
            if body == {"cursor": "mem.owner-result"}:
                if self.username == "fixture-other":
                    return 404, {}
                assert sleeps, "expiry must be awaited, not replaced by token tampering"
                return 410, {}
            if "cursor" in body:
                return 422, {}
            if body["sources"][0].get("subquery", {}).get("sources", [{}])[0].get("releaseId", "").startswith("0000"):
                return 409, {}
            return 200, remote_page([{}], total=2, has_next=True, cursor="mem.owner-result",
                                    expiresAt=expiry.isoformat())

    monkeypatch.setattr(acc, "Api", GuardApi)
    monkeypatch.setattr(acc.time, "sleep", sleeps.append)
    result = acc.execute_guards_phase(GuardApi("http://127.0.0.1:52004"), run_state(),
        guard_args(viewer_user="fixture-viewer", viewer_password="fixture", other_user="fixture-other",
                   other_password="fixture"))
    assert result["guards_result"] == "pass" and not result["skipped"]
    assert result["full_e2e_pass"] is False
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 1
    other_calls = [body for username, body in calls if username == "fixture-other"]
    assert "sources" in other_calls[0] and other_calls[1] == {"cursor": "mem.owner-result"}
    # Scale-independent access proof: an eq filter pins the deterministic
    # primary key order_id=1 so exactly one row materializes at any table
    # size (pageSize alone still materializes the full Order projection and
    # breached the memory_execution_bytes budget at 1M orders).
    probe = other_calls[0]
    assert len(probe["sources"]) == 1 and probe["sources"][0]["alias"] == "o"
    assert probe["where"] == [{"source": "o", "property": "order_id", "op": "eq", "value": 1}]
    assert probe["select"] == [{"source": "o", "property": "order_id", "as": "order_id"}]
    assert probe["pageSize"] == 1
    assert result["guards"]["owner_session_change"]["access_proof"] == {"status": 200,
                                                                        "pageRowCount": 1}
    assert "separate gate" in result["guards"]["owner_session_change"]["scope"]


def test_missing_guard_inputs_cannot_pass(acc, monkeypatch):
    api = fake_api(acc, [remote_page([]), {}, {}])
    monkeypatch.setattr(api, "login", lambda *_: None)
    result = acc.execute_guards_phase(api, run_state(), guard_args(max_expiry_wait=0))
    assert result["guards_result"] == "incomplete_or_failed"
    assert set(result["skipped"]) == {"permission_denied", "owner_session_change", "cursor_expired"}
    rejected = fake_api(acc, [{"detail": {"code": "query_budget_exceeded",
                                         "budget": "memory_execution_bytes",
                                         "limitBytes": 134217728}}, {}, {}])
    original_request = rejected.request
    rejected_calls = []

    def request_with_rejection(method, path, body=None, expect=(200,)):
        status, payload = original_request(method, path, body, expect)
        rejected_calls.append(body)
        return (422 if len(rejected_calls) == 1 else status), payload

    monkeypatch.setattr(rejected, "login", lambda *_: None)
    monkeypatch.setattr(rejected, "request", request_with_rejection)
    result = acc.execute_guards_phase(rejected, run_state(), guard_args(max_expiry_wait=0))
    assert result["guards_result"] == "incomplete_or_failed"
    assert result["cursor_establishment"] == {
        "status": 422, "cursor_available": False,
        "failure": {"code": "query_budget_exceeded", "budget": "memory_execution_bytes",
                    "limitBytes": 134217728}}
    assert "no result cursor" in result["guards"]["cursor_expired"]["note"]
    assert len(rejected_calls) == 3


@pytest.mark.parametrize(("phase", "outcome"), [("query", "fail"), ("guards", "incomplete_or_failed")])
def test_cli_returns_failure_for_failed_or_incomplete_phases(acc, tmp_path, monkeypatch, phase, outcome):
    monkeypatch.setattr(acc, "RUNS", tmp_path)
    monkeypatch.setenv("FEDERATED_ACCEPTANCE_API_TIMEOUT", "12")
    (tmp_path / "fixture.json").write_text(json.dumps(run_state()))
    captured = []
    monkeypatch.setattr(acc, f"execute_{phase}_phase",
                        lambda api, state, args: captured.append(api) or {f"{phase}_result": outcome})
    # --api-base before the subcommand must survive the subparser (SUPPRESS
    # default); the guards case keeps the after-subcommand placement.
    tail = [phase, "--name", "fixture", "--orders", "1000", "--admin-password", "synthetic"]
    if phase == "query":
        argv = ["federated_acceptance.py", "--api-base", "http://127.0.0.1:52004"] + tail
    else:
        argv = ["federated_acceptance.py"] + tail + ["--api-base", "http://127.0.0.1:52004"]
    monkeypatch.setattr(acc.sys, "argv", argv)
    assert acc.main() == 1
    assert captured[0].base_url == "http://127.0.0.1:52004"
    assert captured[0].timeout == 12.0  # explicit env override of the 300/360 defaults
    monkeypatch.delenv("FEDERATED_ACCEPTANCE_API_TIMEOUT")
    assert acc._phase_timeout("query") == 300.0 and acc._phase_timeout("guards") == 360.0
    # Report rolls up the actual artifacts only: incomplete evidence must
    # exit nonzero even after both phase files exist.
    monkeypatch.setattr(acc.sys, "argv", ["federated_acceptance.py", "report", "--name", "fixture"])
    assert acc.main() == 1
    report = json.loads((tmp_path / "fixture-report.json").read_text())
    assert report["functional_acceptance_pass"] is False and report["full_e2e_pass"] is False
    assert report["missing_artifacts"] == ["guards" if phase == "query" else "query"]
    assert report["unresolved_broader_evidence"] and "rows" not in report
    failure = {"status": 422, "detail": {"code": "query_budget_exceeded",
                                         "budget": "memory_execution_bytes"}}
    (tmp_path / "fixture-query.json").write_text(json.dumps(
        {"query_result": "rejected", "failure": failure, "rows_observed": None}))
    assert acc.main() == 1
    rejected = json.loads((tmp_path / "fixture-report.json").read_text())
    assert rejected["functional_acceptance_pass"] is False
    assert rejected["query"]["failure"] == failure
    (tmp_path / "fixture-query.json").write_text(json.dumps(
        {"query_result": "pass", "rows_observed": 2, "mismatch_count": 0, "mismatches": [],
         "pagination_meta": {"pageCount": 1, "plan": {"strategy": "memory_federated"},
                             "transportBytes": {"request": 10, "response": 20}},
         "metadata_canary": {"row_counts_unchanged": True}, "source_canary": True}))
    (tmp_path / "fixture-guards.json").write_text(json.dumps(
        {"guards": {"cursor_expired": {"pass": True}}, "passed": ["cursor_expired"], "skipped": [],
         "guards_result": "pass"}))
    assert acc.main() == 0
    report = json.loads((tmp_path / "fixture-report.json").read_text())
    assert report["functional_acceptance_pass"] is True
    assert report["full_e2e_pass"] is False  # broader coordinated evidence still unresolved
    assert report["missing_artifacts"] == [] and report["query"]["rows_observed"] == 2
    assert "rows" not in report and '"rows"' not in json.dumps(report)


@pytest.mark.parametrize("base", ["http://0.0.0.0:52004", "https://example.internal",
    "http://127.0.0.1.example.internal:52004", "http://localhost@remote.invalid",
    "http://user:pass@localhost:52004", "http://localhost:52004/path", "http://localhost:52004/?secret=1"])
def test_api_base_must_be_explicit_loopback(acc, base):
    with pytest.raises(ValueError):
        acc.Api(base)


@pytest.mark.parametrize("base", ["http://127.0.0.1:52004", "http://localhost:52004/", "http://[::1]:52004"])
def test_api_accepts_loopback_hosts(acc, base):
    assert acc.Api(base).base_url == base.rstrip("/")
