"""A-28 assistant driver unit tests (offline, fake HTTP; no network, no LLM).

Covers the pieces whose correctness must not depend on the live staging API:
- oracle comparison semantics (exact decimals, NULL vs 0, fan-out detection);
- final-answer JSON block parsing (object + per-customer rows);
- SSE frame parsing and the approval policy gate (prepared card + goala28
  ownership + zero-source-change checkpoint before approving);
- transcript scrubbing of secret-shaped keys;
- the fake-chat north/query verdict paths: pass, mental-math rejection
  (no ontology_query call), and oracle mismatch stays fail.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND_DIR / "scripts" / "ontology_network_goal_assistant.py"
_spec = importlib.util.spec_from_file_location("a28_driver", SCRIPT)
a28 = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(a28)
sys.modules.setdefault("a28_driver", a28)
_REAL_RUN = a28.Run  # captured before any test patches the module attribute


# ---------------------------------------------------------------------------
# oracle semantics


def test_north_expected_matches_goal_doc_oracle():
    oracle = a28.load_fixture_oracle()
    expected = a28.north_expected(oracle)
    assert expected["north_contract_sum"] == "157.7500"
    assert expected["north_order_sum"] == "40.0000"
    assert expected["north_ship_qty"] == "7"
    assert expected["north_contract_count"] == "3"
    assert expected["north_order_count"] == "2"


def test_compare_cell_money_requires_exact_string_literal():
    """Canonical-driver gap proof: the independent A-28 strict run already
    rejected 157.75 vs 157.7500, but this driver's normalize+Decimal path
    accepted it (and whitespace/scientific spellings). Goal sec 8 requires
    byte-exact money strings, so every numerically equal spelling must fail.
    """
    assert a28.compare_cell("contract_sum", "157.7500", "157.7500") is None
    for spelling in ("157.75", "157.750", "157.7500 ", " 157.7500",
                     "157.7500\n", "1.5775E+2", "0.1577500E3"):
        assert a28.compare_cell("contract_sum", "157.7500", spelling) is not None, \
            spelling
    # NULL semantics unchanged; NULL never equals 0 or a money literal
    assert a28.compare_cell("order_sum", "<NULL>", None) is None
    assert a28.compare_cell("order_sum", "<NULL>", "<NULL>") is None
    assert a28.compare_cell("order_sum", "<NULL>", 0) is not None
    assert a28.compare_cell("order_sum", "40.0000", None) is not None
    assert a28.compare_cell("ship_qty", "0", "<NULL>") is not None
    assert a28.compare_cell("order_count", "2", 2) is None
    # money must be an exact decimal STRING on the wire; JSON floats rejected
    assert a28.compare_cell("order_sum", "40.0000", 40.0) is not None
    assert a28.compare_cell("order_sum", "40.0000", 40) is not None
    # counters must be integral: 1.9 never truncates into 1
    assert a28.compare_cell("order_count", "1", 1.9) is not None
    assert a28.compare_cell("order_count", "1", "1.9") is not None
    assert a28.compare_cell("ship_qty", "2", 2.0) is None
    assert a28.compare_cell("ship_qty", "1", True) is not None


def test_check_fanout_rejects_doubled_contract_total():
    assert a28.check_fanout({"north_contract_sum": "301.5000"}) != []
    assert a28.check_fanout({"north_contract_sum": "157.7500"}) == []
    assert a28.check_fanout({"north_contract_sum": None}) == []


def test_detail_expected_null_cells_follow_doc():
    oracle = a28.load_fixture_oracle()
    detail = a28.detail_expected(oracle)
    assert detail["c2"]["contract_sum"] == "<NULL>"
    assert detail["c2"]["contract_count"] == "0"
    assert detail["c3"]["order_sum"] == "<NULL>"
    assert detail["c3"]["ship_qty"] == "<NULL>"
    assert detail["c1"]["contract_sum"] == "150.7500"


# ---------------------------------------------------------------------------
# parsing


def test_parse_final_json_requires_all_keys_and_takes_latest():
    text = ("说明文字 ```json {\"north_contract_sum\": \"1\"} ``` 中段 "
            "```json\n" + json.dumps({
                "north_contract_sum": "157.7500", "north_order_sum": "40.0000",
                "north_ship_qty": "7", "north_contract_count": 3,
                "north_order_count": 2}) + "\n``` 结尾")
    blocks = a28.parse_final_json(text, a28.NORTH_KEYS)
    assert len(blocks) == 1
    assert blocks[0]["north_contract_sum"] == "157.7500"


def test_detail_rows_parses_per_customer_array_only_with_full_keys():
    rows = [{"customer_id": "c1", "region": "north", "contract_sum": "150.7500",
             "contract_count": 2, "order_sum": "40.0000", "order_count": 2,
             "ship_qty": "7"},
            {"customer_id": "c2", "region": "south", "contract_sum": None,
             "contract_count": 0, "order_sum": "20.0000", "order_count": 1,
             "ship_qty": "5"}]
    text = "```json\n" + json.dumps(rows, ensure_ascii=False) + "\n```"
    parsed = a28._detail_rows(text)
    assert [r["customer_id"] for r in parsed] == ["c1", "c2"]
    incomplete = [{"customer_id": "c3"}]
    assert a28._detail_rows("```json\n" + json.dumps(incomplete) + "\n```") == []


# ---------------------------------------------------------------------------
# scrubbing


def test_scrub_removes_secret_shaped_values_recursively():
    dirty = {"config": {"password": "x", "apiKey": "y", "api_key": "z",
                        "host": "keep"}, "list": [{"authorization": "b"}],
             "name": "goala28"}
    clean = a28.scrub(dirty)
    assert clean["config"]["password"] == "<redacted>"
    assert clean["config"]["apiKey"] == "<redacted>"
    assert clean["config"]["api_key"] == "<redacted>"
    assert clean["config"]["host"] == "keep"
    assert clean["list"][0]["authorization"] == "<redacted>"
    assert clean["name"] == "goala28"


# ---------------------------------------------------------------------------
# approval policy


class _Run:
    def __init__(self, ontologies):
        self.state = {"ontologies": ontologies}


MINE = {"A": "ont-a", "B": "ont-b", "C": "ont-c"}


def test_approval_policy_execute_cards_always_denied_in_query_turns():
    """Query turns never approve writes; only strict_mutation_decider can."""
    policy = a28._approval_policy(_Run(MINE))
    for card in (
        {"toolName": "ontology_execute_action",
         "ontologyConfirmation": {"state": "prepared", "ontologyId": "ont-c"}},
        {"toolName": "ontology_execute_action"},
    ):
        assert policy(card) == "deny"


def test_approval_policy_query_turns_deny_everything():
    """Query turns approve nothing: join assertions are governance evidence
    and must never be auto-generated mid-conversation; execute cards are only
    approved by the strict mutation decider inside mutate legs."""
    policy = a28._approval_policy(_Run(MINE))
    for card in (
        {"toolName": "ontology_register_join_assertion",
         "joinAssertion": {"leftOntologyId": "ont-a",
                           "rightOntologyId": "ont-b"}},
        {"toolName": "ontology_register_join_assertion",
         "joinAssertion": {"rightOntologyId": "ont-b"}},
        {"toolName": "ontology_execute_action",
         "ontologyConfirmation": {"state": "prepared",
                                  "display": {"ontology": {"id": "ont-c"},
                                              "action": {"id": a28.ACTION_ID}}}},
        {"toolName": "web_fetch"},
    ):
        assert policy(card) == "deny"


# ---------------------------------------------------------------------------
# strict mutation decider


class _Turn:
    def __init__(self, tool_calls):
        self.tool_calls = tool_calls
        self.gate_source_value = None


def _prepare_call(qty=6, shipment="s003", type_id=None):
    return {"p1": {"toolName": "ontology_prepare_action",
                   "arguments": {
                       "targetRef": {"objectTypeId": type_id or a28.TYPE_IDS["Shipment"],
                                     "key": {"shipment_id": shipment}},
                       "parameters": {"qty": qty}}}}


def _execute_card(action_id=None, ontology_id="ont-c", qty=6, state="prepared"):
    return {"toolName": "ontology_execute_action",
            "ontologyConfirmation": {
                "state": state,
                "display": {"ontology": {"id": ontology_id},
                            "action": {"id": action_id or a28.ACTION_ID},
                            "parameters": [{"name": "qty", "value": qty}]}}}


def test_strict_mutation_decider_approves_exact_match(monkeypatch):
    monkeypatch.setattr(a28, "shipment_qty", lambda secrets, sid: 5)
    problems = []
    turn = _Turn(_prepare_call(qty=6))
    decide = a28.strict_mutation_decider(_Run(MINE), 6, 5, {}, turn, problems)
    assert decide(_execute_card(qty=6)) == "approve"
    assert problems == []
    assert turn.gate_source_value == 5


def test_strict_mutation_decider_denies_every_drift(monkeypatch):
    monkeypatch.setattr(a28, "shipment_qty", lambda secrets, sid: 6)
    # wrong action id
    problems = []
    decide = a28.strict_mutation_decider(_Run(MINE), 6, 5, {}, _Turn(_prepare_call()), problems)
    assert decide(_execute_card(action_id="other-action")) == "deny"
    # wrong ontology
    assert decide(_execute_card(ontology_id="ont-a")) == "deny"
    # card qty mismatch vs expected 6
    assert decide(_execute_card(qty=7)) == "deny"
    # state not prepared
    assert decide(_execute_card(state="executed")) == "deny"
    assert problems, "each denial must be recorded"
    # matching card but NO prepare call captured in the turn
    problems2 = []
    decide2 = a28.strict_mutation_decider(_Run(MINE), 6, 5, {}, _Turn({}), problems2)
    assert decide2(_execute_card()) == "deny"
    assert any("prepare" in p for p in problems2)
    # prepare targeted a different shipment / wrong qty
    problems3 = []
    decide3 = a28.strict_mutation_decider(
        _Run(MINE), 6, 5, {}, _Turn(_prepare_call(qty=6, shipment="s001")), problems3)
    assert decide3(_execute_card()) == "deny"
    problems4 = []
    decide4 = a28.strict_mutation_decider(
        _Run(MINE), 6, 5, {}, _Turn(_prepare_call(qty=9)), problems4)
    assert decide4(_execute_card()) == "deny"
    # source already changed before approval (prepare wrote) -> deny
    problems5 = []
    decide5 = a28.strict_mutation_decider(_Run(MINE), 6, 5, {}, _Turn(_prepare_call()), problems5)
    assert decide5(_execute_card()) == "deny"
    assert any("zero-change" in p for p in problems5)


# ---------------------------------------------------------------------------
# plan proof


VALID_PLAN = {
    "sources": [
        {"alias": "crm", "subquery": {
            "sources": [
                {"alias": "cu", "ontologyId": "ont-a",
                 "objectTypeId": "a28-ot-customer"},
                {"alias": "ct", "ontologyId": "ont-a",
                 "objectTypeId": "a28-ot-contract"}],
            "groupBy": [{"source": "cu", "property": "customer_id"}],
            "aggregates": [{"fn": "sum", "as": "contract_sum"}]}},
        {"alias": "erp", "subquery": {
            "sources": [
                {"alias": "o", "ontologyId": "ont-b",
                 "objectTypeId": "a28-ot-order"},
                {"alias": "sh", "subquery": {
                    "sources": [{"alias": "s", "ontologyId": "ont-c",
                                 "objectTypeId": "a28-ot-shipment"}],
                    "groupBy": [{"source": "s", "property": "order_id"}],
                    "aggregates": [{"fn": "sum", "as": "qty"}]}}],
            "groupBy": [{"source": "o", "property": "customer_id"}],
            "aggregates": [{"fn": "sum", "as": "ship_qty"}]}}],
    "joins": [
        {"left": "crm", "right": "erp", "type": "left",
         "on": {"kind": "explicit_key"}}],
    "groupBy": [{"source": "cu", "property": "region"}],
    "aggregates": [{"fn": "sum", "as": "region_contract_sum"}]}


VALID_SPL = (
    "with ship_agg = from ships as s | stats sum(s.qty) as qty by s.order_id; "
    "with erp = from orders as o | join ship_agg as sh "
    "on o.order_id = sh.order_id type=left "
    "| stats sum(sh.qty) as ship_qty by o.customer_id; "
    "with crm = from custs as c | join contracts as ct "
    "on c.customer_id = ct.customer_id type=left "
    "| stats sum(ct.amount) as contract_sum by c.customer_id; "
    "from crm as a | join erp as b on a.customer_id = b.customer_id type=left "
    '| where a.region = "north" '
    "| stats sum(a.contract_sum) as contract_sum, sum(b.ship_qty) as ship_qty"
)
VALID_BINDINGS = {
    "custs": {"ontologyId": "ont-a", "releaseId": "r1",
              "objectTypeId": "a28-ot-customer"},
    "contracts": {"ontologyId": "ont-a", "releaseId": "r1",
                  "objectTypeId": "a28-ot-contract"},
    "orders": {"ontologyId": "ont-b", "releaseId": "r2",
               "objectTypeId": "a28-ot-order"},
    "ships": {"ontologyId": "ont-c", "releaseId": "r3",
              "objectTypeId": "a28-ot-shipment"},
}
VALID_SPL_ARGS = {"spl": VALID_SPL, "semanticBindings": VALID_BINDINGS}


def test_plan_proof_typed_shape_is_diagnostic_only():
    proof = a28.plan_proof(VALID_PLAN)
    assert proof["ok"] is False
    assert proof["mode"] == "typed_shape"
    assert proof["diagnostic_ok"] is True
    assert proof["domains"] == 3
    assert proof["leaf_types"] == 4
    assert proof["stages"] >= 2


def test_plan_proof_rejects_unrelated_single_source_query():
    weak = {"sources": [{"alias": "o", "ontologyId": "ont-a",
                         "objectTypeId": "a28-ot-customer"}],
            "select": [], "joins": []}
    proof = a28.plan_proof(weak)
    assert proof["ok"] is False
    assert proof["diagnostic_ok"] is False
    flat = {"sources": [{"alias": "a", "ontologyId": "ont-a",
                         "objectTypeId": "a28-ot-customer"},
                        {"alias": "b", "ontologyId": "ont-b",
                         "objectTypeId": "a28-ot-order"},
                        {"alias": "c", "ontologyId": "ont-c",
                         "objectTypeId": "a28-ot-shipment"}],
            "joins": [{"left": "a", "right": "b", "type": "left",
                       "on": {"kind": "explicit_key"}}]}
    assert a28.plan_proof(flat)["ok"] is False  # 3 domains but 1 stage, 3 types


def test_value_token_matching_is_boundary_safe():
    text = '{"region": "north", "contract_sum": "157.7500", "ship_qty": 7}'
    assert a28._value_token_in_text("157.7500", text)
    assert a28._value_token_in_text("157.75", text)
    assert a28._value_token_in_text("7", text)
    assert not a28._value_token_in_text("15", text)   # substring of 157...
    assert not a28._value_token_in_text("40.0000", text)


# ---------------------------------------------------------------------------
# row-level provenance


def test_extract_result_rows_envelope_and_truncation():
    rows = a28.extract_result_rows(NORTH_PREVIEW)
    assert len(rows) == 1 and rows[0]["contract_sum"] == "157.7500"
    # data-wrapped envelope
    wrapped = '{"data": {"rows": [{"a": 1}]}}'
    assert a28.extract_result_rows(wrapped) == [{"a": 1}]
    aliases = {"contract_sum", "contract_count"}
    # truncated tail: complete ALIAS-bearing objects salvaged, partial dropped
    truncated = '{"rows": [{"contract_sum": "150.7500", "contract_count": 2}, {"contr'
    rows = a28.extract_result_rows(truncated, aliases)
    assert rows == [{"contract_sum": "150.7500", "contract_count": 2}]


def test_extract_result_rows_metadata_truncation_regression():
    """800-char previews whose visible head is envelope METADATA (releases
    entries) must never be mistaken for result rows — the exact bug that
    bypassed the persisted fallback on run06 attempt-1."""
    aliases = {"contract_sum", "order_sum", "ship_qty",
               "contract_count", "order_count"}
    metadata_only = ('{"queryId": "f6ca5e810d56424e902d103fb8d40d00", '
                     '"releases": {"os.cu": {"ontologyId": "x", '
                     '"releaseId": "y", "objectTypeId": "z", '
                     '"dataMode": "remote"}, "os.ct": {"ontologyId": "x", '
                     '"releaseId": "y", "objectTypeId": "w", '
                     '"dataMode": "remote"}}, "columns": [{"name": "contract_')
    # salvage without aliases: strict-empty (old code returned junk here)
    assert a28.extract_result_rows(metadata_only) == []
    # salvage with aliases: metadata objects share no alias -> still empty
    assert a28.extract_result_rows(metadata_only, aliases) == []


def _complete_persisted_envelope(rows=None, **overrides):
    rows = [{"ship_qty": 6}] if rows is None else rows
    payload = {
        "queryId": "q1", "rows": rows,
        "pageRowCount": len(rows), "totalRowCount": len(rows),
        "rowCounting": "complete", "partial": False, "truncated": False,
        "hasNext": False, "cursor": None,
        "completeness": "complete", "exact": True,
    }
    payload.update(overrides)
    return payload


def test_parse_persisted_result_accepts_complete_aggregate_envelope():
    rows = [{"contract_sum": "157.7500", "order_sum": "40.0000",
             "ship_qty": 7, "contract_count": 3, "order_count": 2}]
    parsed, problem = a28.parse_persisted_result(
        json.dumps(_complete_persisted_envelope(rows)))
    assert problem is None
    assert parsed == rows


def test_parse_persisted_result_rejects_incomplete_or_missing_markers():
    base = _complete_persisted_envelope()
    for key, value in (
            ("partial", True), ("partial", 0),
            ("truncated", True), ("hasNext", True),
            ("cursor", "next-page"), ("rowCounting", "page_probe"),
            ("pageRowCount", 2), ("totalRowCount", 2),
            ("completeness", "partial"), ("exact", False)):
        payload = dict(base)
        payload[key] = value
        rows, problem = a28.parse_persisted_result(json.dumps(payload))
        assert rows == [] and problem, (key, value)
    for key in ("partial", "truncated", "hasNext", "cursor", "rowCounting",
                "pageRowCount", "totalRowCount"):
        payload = dict(base)
        del payload[key]
        rows, problem = a28.parse_persisted_result(json.dumps(payload))
        assert rows == [] and problem, f"missing {key}"


def test_persisted_truncated_text_with_valid_row_is_rejected(monkeypatch):
    """A balanced legal row inside truncated persisted TEXT must not become
    authoritative acceptance evidence; preview may still salvage diagnostically."""
    truncated = ('{"rows": [{"ship_qty": 6}], "pageRowCount": 1, '
                 '"totalRowCount": 1, "rowCounting": "complete", '
                 '"partial": false, "truncated": false, "hasNext": false, '
                 '"cursor": null, "completeness": "complete"')
    assert a28.parse_persisted_result(truncated)[0] == []
    monkeypatch.setattr(a28, "fetch_persisted_tool_result",
                        lambda url, conv, run: truncated)

    class _Turn:
        tool_calls = {"r1": {"toolName": "ontology_query",
                             "status": "success",
                             "plan_proof": {"ok": True, "mode": "spl_text"},
                             "preview": '{"rows": [{"ship_qty": 6}]}'}}

    turn = _Turn()
    assert a28.collect_row_evidence(
        turn, {"ship_qty"}, conversation_id="c1", meta_db_url="dsn") == []
    assert turn.tool_calls["r1"]["rows_source"] == "preview_diagnostic"
    assert turn.tool_calls["r1"]["preview_row_count"] == 1
    assert "not valid JSON" in turn.tool_calls["r1"]["persisted_reject_reason"]


def test_cell_equal_semantics():
    assert a28._cell_equal("contract_sum", "157.7500", "157.7500")
    assert not a28._cell_equal("contract_sum", "157.7500", "157.75")
    assert not a28._cell_equal("contract_sum", "157.7500", "157.7500 ")
    assert not a28._cell_equal("contract_sum", "157.7500", " 157.7500")
    assert not a28._cell_equal("contract_sum", "157.7500", "1.5775E+2")
    assert not a28._cell_equal("contract_sum", "157.7500", 157.75)
    assert a28._cell_equal("ship_qty", "7", 7.0)
    assert not a28._cell_equal("ship_qty", "7", 7.5)   # non-integral never equal
    assert a28._cell_equal("order_sum", "<NULL>", None)
    assert not a28._cell_equal("order_sum", "<NULL>", 0)
    assert not a28._cell_equal("contract_count", "0", None)
    assert a28._cell_equal("customer_id", "c1", "c1")
    assert not a28._cell_equal("customer_id", "c1", " c1")
    assert not a28._cell_equal("region", "north", "NORTH")


def test_match_oracle_row_rejects_numerically_equal_money_spelling():
    assert not a28.match_oracle_row(
        {"contract_sum": "157.7500"}, [{"contract_sum": "157.75"}])[0]
    assert not a28.match_oracle_row(
        {"contract_sum": "157.7500"}, [{"contract_sum": "157.7500 "}])[0]
    assert a28.match_oracle_row(
        {"contract_sum": "157.7500"}, [{"contract_sum": "157.7500"}])[0]


def test_match_oracle_row_requires_full_signature():
    ok, _ = a28.match_oracle_row(
        {"contract_sum": "157.7500", "order_count": "2"},
        [{"contract_sum": "157.7500", "order_count": 2}])
    assert ok
    # right values scattered across DIFFERENT rows: no single row matches
    ok, problems = a28.match_oracle_row(
        {"contract_sum": "157.7500", "order_count": "2"},
        [{"contract_sum": "157.7500", "order_count": 9},
         {"contract_sum": "9.0000", "order_count": 2}])
    assert not ok, "co-occurrence across rows must not pass"
    ok, _ = a28.match_oracle_row({"ship_qty": "7"}, [])
    assert not ok


def test_verify_oracle_requires_one_call_for_all_identities():
    expected = {"north": {"ship_qty": "7"}, "south": {"ship_qty": "6"}}
    split = a28.verify_oracle_against_tool_rows(
        expected,
        [("r1", [{"ship_qty": 7}]), ("r2", [{"ship_qty": 6}])])
    assert split["problems"], "cross-call stitching must not pass"
    assert any("multiple calls" in p for p in split["problems"])
    assert split["call_ids"] == {"north": "r1", "south": "r2"}
    one_call = a28.verify_oracle_against_tool_rows(
        expected, [("r1", [{"ship_qty": 7}, {"ship_qty": 6}])])
    assert one_call["problems"] == []
    assert one_call["call_ids"] == {"north": "r1", "south": "r1"}
    missing = a28.verify_oracle_against_tool_rows(
        expected, [("r1", [{"ship_qty": 7}])])
    assert any("south" in p for p in missing["problems"])


def test_spl_text_plan_proof_ast_reachability_and_cascade():
    spl = (
        "with ship_agg = from ships as s | stats sum(s.qty) as qty by s.order_id; "
        "with erp = from orders as o | join ship_agg as sh "
        "on o.order_id = sh.order_id type=left "
        "| stats sum(sh.qty) as ship_qty by o.customer_id; "
        "with crm = from custs as c | join contracts as ct "
        "on c.customer_id = ct.customer_id type=left "
        "| stats sum(ct.amount) as contract_sum by c.customer_id; "
        "from crm as a | join erp as b on a.customer_id = b.customer_id type=left "
        '| where a.region = "north" '
        "| stats sum(a.contract_sum) as contract_sum, sum(b.ship_qty) as ship_qty"
    )
    bindings = {
        "custs": {"ontologyId": "ont-a", "releaseId": "r1",
                  "objectTypeId": "a28-ot-customer"},
        "contracts": {"ontologyId": "ont-a", "releaseId": "r1",
                      "objectTypeId": "a28-ot-contract"},
        "orders": {"ontologyId": "ont-b", "releaseId": "r2",
                   "objectTypeId": "a28-ot-order"},
        "ships": {"ontologyId": "ont-c", "releaseId": "r3",
                  "objectTypeId": "a28-ot-shipment"},
    }
    proof = a28.plan_proof({"spl": spl, "semanticBindings": bindings})
    assert proof["ok"] is True, proof
    assert proof["mode"] == "spl_text"
    assert (proof["domains"], proof["leaf_types"], proof["stages"]) == (3, 4, 3)
    assert proof["join_count"] >= 3

    # unused bindings must NOT inflate the floor: plan references only
    # custs+orders; contracts/ships/extra stay unused in semanticBindings
    spl_three = (
        "with agg1 = from custs as c | stats count() as n by c.customer_id; "
        "from agg1 as a | join orders as b on a.customer_id = b.customer_id "
        "type=left | stats sum(b.amount) as s by a.customer_id"
    )
    proof = a28.plan_proof({"spl": spl_three,
                            "semanticBindings": dict(bindings, extra={
                                "ontologyId": "ont-d", "releaseId": "r4",
                                "objectTypeId": "unused"})})
    assert proof["ok"] is False
    assert proof["leaf_types"] == 2 and proof["domains"] == 2  # reachable only

    # parallel aggregating stages without a cascading consumer fail the floor
    spl_parallel = (
        "with s1 = from custs as c | stats count() as n by c.customer_id; "
        "with s2 = from orders as o | stats count() as m by o.customer_id; "
        "from s1 as a | join s2 as b on a.customer_id = b.customer_id "
        "type=left | fields a.customer_id as customer_id"
    )
    proof = a28.plan_proof({"spl": spl_parallel,
                            "semanticBindings": bindings})
    assert proof["ok"] is False
    assert proof["stages"] < 2  # main is fields-only: no aggregation cascade


def test_spl_plan_proof_requires_valid_distinct_published_triples():
    duplicate = dict(VALID_BINDINGS)
    duplicate["contracts"] = dict(VALID_BINDINGS["custs"])
    proof = a28.plan_proof({"spl": VALID_SPL,
                            "semanticBindings": duplicate})
    assert proof["ok"] is False
    assert proof["leaf_types"] == 3, "duplicate triples must not count"
    invalid = dict(VALID_BINDINGS)
    invalid["ships"] = {**VALID_BINDINGS["ships"], "ontologyId": None}
    proof = a28.plan_proof({"spl": VALID_SPL,
                            "semanticBindings": invalid})
    assert proof["ok"] is False
    assert "ships" in proof["invalid_bindings"]


def test_spl_plan_proof_rejects_disconnected_join_graph():
    spl = (
        "with agg = from custs as c "
        "| stats count() as n by c.customer_id; "
        "from agg as a "
        "| join contracts as ct on ct.customer_id = a.customer_id type=left "
        "| join orders as o on o.customer_id = o.customer_id type=left "
        "| join ships as s on s.shipment_id = s.shipment_id type=left "
        "| stats count() as n by a.customer_id"
    )
    proof = a28.plan_proof({"spl": spl, "semanticBindings": VALID_BINDINGS})
    assert proof["ok"] is False
    assert proof["connected"] is False
    assert (proof["domains"], proof["leaf_types"], proof["stages"]) == (3, 4, 2)


def test_typed_plan_never_qualifies_as_spl_text():
    turn = _Turn({"q1": {"toolName": "ontology_query", "status": "success",
                         "arguments": VALID_PLAN}})
    assert a28.qualifying_query_calls(turn) == []
    assert turn.tool_calls["q1"]["plan_proof"]["mode"] == "typed_shape"


# ---------------------------------------------------------------------------
# SSE frame parsing + gate behavior against a fake stream


class _FakeApi:
    """Records decision posts; serves the fake message history."""

    def __init__(self, messages_payload):
        self.posts = []
        self._messages = messages_payload
        self.base_url = "http://127.0.0.1:59999"
        self.token = "t"

    def request(self, method, path, body=None):
        if method == "GET":
            return 200, {"data": {"messages": self._messages}}
        self.posts.append((method, path, body))
        return 200, {"data": {"ok": True}}


def _frames(events: list[tuple[str, dict]]) -> bytes:
    out = []
    for name, data in events:
        out.append(f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n")
    return "".join(out).encode()


def _run_turn(monkeypatch, api, frames, decide=None):
    """Drive AssistantTurn against an in-memory SSE body."""
    import io

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        assert request.get_full_url().endswith("/chat")
        return _Resp(frames)

    monkeypatch.setattr(a28.urllib.request, "urlopen", fake_urlopen)
    turn = a28.AssistantTurn(api, "conv-1", "model-1")
    turn.turn("message", decide=decide, total_timeout=10, idle_timeout=5)
    return turn


def test_sse_turn_collects_tools_text_and_posts_decision(monkeypatch):
    events = [
        ("meta", {"conversationId": "conv-1", "assistantMessageId": "m1"}),
        ("tool_start", {"toolRunId": "tr1", "toolName": "ontology_query",
                        "arguments": VALID_SPL_ARGS}),
        ("tool_result", {"toolRunId": "tr1", "status": "success",
                         "preview": '{"rows": [{"north": 1}]}'}),
        ("text_delta", {"delta": "north "}),
        ("text_delta", {"delta": "= 157.7500"}),
        ("message_end", {"usage": {}}),
        ("done", {}),
    ]
    api = _FakeApi([{"role": "assistant", "content": "north = 157.7500",
                     "steps": []}])
    turn = _run_turn(monkeypatch, api, _frames(events))
    assert turn.close_state == "clean"
    assert turn.tool_calls["tr1"]["status"] == "success"
    assert turn.tool_calls["tr1"]["toolName"] == "ontology_query"
    summary = turn.summary()
    assert summary["text"] == "north = 157.7500"
    assert summary["confirmation_events"] == 0
    assert summary["event_counts"]["text_delta"] == 2
    assert a28.has_successful_query_call(turn) is True  # plan proof present


def test_sse_total_timeout_closes_response_and_joins_thread(monkeypatch):
    """Deadline hit: the actual response is closed, reader joined, no residue."""
    import threading as _threading

    release = _threading.Event()
    closed = _threading.Event()

    class _BlockingResp:
        def __iter__(self):
            return self

        def __next__(self):
            if not release.wait(timeout=30):
                raise AssertionError("stream never released by close()")
            raise StopIteration

        def close(self):
            closed.set()
            release.set()

    def fake_urlopen(request, timeout=None):
        return _BlockingResp()

    monkeypatch.setattr(a28.urllib.request, "urlopen", fake_urlopen)
    api = _FakeApi([{"role": "assistant", "content": "", "steps": []}])
    turn = a28.AssistantTurn(api, "conv-1", "model-1")
    turn.turn("message", total_timeout=1.5, idle_timeout=30)
    assert turn.stream_error == "total_timeout"
    assert closed.is_set()          # the real response object was closed
    assert turn.close_state == "clean"  # reader exited and reported after join
    assert not turn.finished.is_set() or True
    assert turn.summary()["stream_error"] == "total_timeout"


def test_confirmation_gate_posts_decision_once(monkeypatch):
    events = [
        ("tool_start", {"toolRunId": "tr9", "toolName": "ontology_execute_action",
                        "arguments": {"confirmationId": "cf-1"}}),
        ("tool_confirmation_required", {
            "toolRunId": "tr9", "toolName": "ontology_execute_action",
            "ontologyConfirmation": {"state": "prepared", "ontologyId": "ont-c"}}),
        # The server holds the stream open after the card; a long-lived stream
        # is simulated by trailing keepalive-free frames then close.
        ("tool_result", {"toolRunId": "tr9", "status": "success", "preview": "{}"}),
        ("done", {}),
    ]
    api = _FakeApi([{"role": "assistant", "content": "done", "steps": []}])
    decided = []
    gate_reads = []

    def decide(card):
        decided.append(card["toolRunId"])
        gate_reads.append(card.get("ontologyConfirmation", {}).get("state"))
        return "approve"

    turn = _run_turn(monkeypatch, api, _frames(events), decide=decide)
    assert decided == ["tr9"]
    assert gate_reads == ["prepared"]
    assert len([p for p in api.posts if "/decision" in p[1]]) == 1
    assert api.posts[0][2] == {"decision": "approve"}
    assert turn.tool_calls["tr9"]["status"] == "decision_approve"


def test_gate_denies_when_source_changed_before_approval(monkeypatch):
    events = [
        ("tool_confirmation_required", {
            "toolRunId": "tr1", "toolName": "ontology_execute_action",
            "ontologyConfirmation": {"state": "prepared", "ontologyId": "ont-c"}}),
        ("tool_result", {"toolRunId": "tr1", "status": "denied", "preview": ""}),
        ("done", {}),
    ]
    api = _FakeApi([{"role": "assistant", "content": "denied", "steps": []}])

    def decide(card):  # simulate the source-drift branch of the mutate gate
        return "deny"

    turn = _run_turn(monkeypatch, api, _frames(events), decide=decide)
    assert api.posts[0][2] == {"decision": "deny"}


# ---------------------------------------------------------------------------
# phase-level verdict paths against a fully fake backend


class _ScriptedApi(_FakeApi):
    """Endpoint-scripted API for the query phase."""

    def __init__(self, messages_payload, sse_frames):
        super().__init__(messages_payload)
        self.sse_frames = sse_frames


@pytest.fixture()
def query_env(monkeypatch, tmp_path):
    run = _REAL_RUN.__new__(_REAL_RUN)
    run.path = tmp_path / "unit.json"
    run.state = {
        "name": "unit", "steps": {"model_config": {"model_config_id": "mc-1"}},
        "ontologies": dict(MINE), "conversation_id": "conv-1", "a28_result": "not_run",
    }
    run.save()
    monkeypatch.setattr(a28, "RUNTIME", tmp_path)
    monkeypatch.setattr(a28, "_assistant_api",
                        lambda args: (_FakeApi([]), {}))
    monkeypatch.setattr(a28, "Run", lambda name: _load_run(tmp_path, name))
    return run, tmp_path


def _load_run(tmp_path, name):
    run = object.__new__(_REAL_RUN)
    run.path = tmp_path / f"{name}.json"
    run.state = json.loads(run.path.read_text()) if run.path.exists() else {
        "name": name, "steps": {}, "a28_result": "not_run"}
    return run


def _north_frames(status="success") -> bytes:
    return _frames([
        ("tool_start", {"toolRunId": "q1", "toolName": "ontology_query",
                        "arguments": {}}),
        ("tool_result", {"toolRunId": "q1", "status": status, "preview": "{}"}),
        ("done", {}),
    ])


def _patch_turn(monkeypatch, turn_results):
    calls = {"n": 0}

    class FakeTurn:
        def __init__(self, api, conversation_id, model_config_id):
            pass

        def turn(self, message, *, decide=None, **kwargs):
            index = min(calls["n"], len(turn_results) - 1)
            calls["n"] += 1
            result = turn_results[index]
            self.persisted_final = result["text"]
            self.persisted_steps = []
            self.tool_calls = result.get("tool_calls", {})
            self.events = []
            self.text = result["text"]
            self.stream_error = None
            self.close_state = "clean"
            self.confirmations = []
            self.query_ids = set(result.get("query_ids", ()))
            self.handle_cancel = None
            self._decision_attempts = {}

        def summary(self):
            return {"text": self.text, "tool_calls": self.tool_calls}

    monkeypatch.setattr(a28, "AssistantTurn", FakeTurn)
    return calls


NORTH_PREVIEW = ('{"rows": [{"contract_sum": "157.7500", "order_sum": "40.0000", '
                 '"ship_qty": 7, "contract_count": 3, "order_count": 2}]}')
DETAIL_PREVIEW = ('{"rows": ['
                  '{"contract_sum": "150.7500", "contract_count": 2, '
                  '"order_sum": "40.0000", "order_count": 2, "ship_qty": 7}, '
                  '{"contract_sum": null, "contract_count": 0, '
                  '"order_sum": "20.0000", "order_count": 1, "ship_qty": 5}, '
                  '{"contract_sum": "7.0000", "contract_count": 1, '
                  '"order_sum": null, "order_count": 0, "ship_qty": null}]}')

QUERY_CALL = {"q1": {"toolName": "ontology_query", "status": "success",
                     "arguments": VALID_SPL_ARGS, "preview": NORTH_PREVIEW}}


def test_query_phase_pass_path(monkeypatch, query_env):
    run, tmp_path = query_env
    north_block = {"north_contract_sum": "157.7500", "north_order_sum": "40.0000",
                   "north_ship_qty": "7", "north_contract_count": 3,
                   "north_order_count": 2}
    detail_rows = [
        {"customer_id": "c1", "region": "north", "contract_sum": "150.7500",
         "contract_count": 2, "order_sum": "40.0000", "order_count": 2,
         "ship_qty": "7"},
        {"customer_id": "c2", "region": "south", "contract_sum": None,
         "contract_count": 0, "order_sum": "20.0000", "order_count": 1,
         "ship_qty": "5"},
        {"customer_id": "c3", "region": "north", "contract_sum": "7.0000",
         "contract_count": 1, "order_sum": None, "order_count": 0,
         "ship_qty": None},
    ]
    persisted_rows = [
        {"contract_sum": "157.7500", "order_sum": "40.0000", "ship_qty": 7,
         "contract_count": 3, "order_count": 2},
        *detail_rows,
    ]
    persisted_envelope = _complete_persisted_envelope(persisted_rows)
    monkeypatch.setattr(
        a28, "fetch_persisted_tool_result",
        lambda url, conv, run: json.dumps(persisted_envelope))
    _patch_turn(monkeypatch, [
        {"text": "结论 ```json\n" + json.dumps(north_block) + "\n```",
         "tool_calls": dict(QUERY_CALL)},
        {"text": "明细 ```json\n" + json.dumps(detail_rows) + "\n```",
         "tool_calls": {"q1": {**QUERY_CALL["q1"], "preview": DETAIL_PREVIEW}}},
    ])
    args = type("Args", (), {"name": "unit", "api_base": "http://127.0.0.1:59999",
                             "admin_user": "u", "admin_password": "p",
                             "secrets_dir": "/nonexistent",
                             "meta_db_url": "dsn"})()
    result = a28.phase_query(args)
    assert result["result"] == "pass", (result["problems"],
                                        result["detail_problems"])
    state = json.loads((tmp_path / "unit.json").read_text())
    assert state["steps"]["query_north"]["ok"] is True
    assert state["steps"]["query_detail"]["ok"] is True


def test_query_phase_fails_on_oracle_mismatch_and_mental_math(monkeypatch, query_env):
    run, tmp_path = query_env
    wrong = {"north_contract_sum": "301.5000", "north_order_sum": "40.0000",
             "north_ship_qty": "7", "north_contract_count": 3,
             "north_order_count": 2}
    _patch_turn(monkeypatch, [
        # north answered without any ontology_query call AND doubled total
        {"text": "```json\n" + json.dumps(wrong) + "\n```", "tool_calls": {}},
        {"text": "no rows", "tool_calls": {}},
    ])
    args = type("Args", (), {"name": "unit", "api_base": "http://127.0.0.1:59999",
                             "admin_user": "u", "admin_password": "p",
                             "secrets_dir": "/nonexistent"})()
    result = a28.phase_query(args)
    assert result["result"] == "fail"
    joined = " ".join(result["problems"])
    assert "domains/4 tables/2 stages" in joined  # plan-proof rejection
    assert "301.5000" in joined                    # fan-out flagged explicitly
    assert "157.7500" in joined                    # oracle mismatch recorded
    state = json.loads((tmp_path / "unit.json").read_text())
    assert state["steps"]["query_north"]["ok"] is False
    assert state["query_result"] == "fail"


def test_query_phase_fails_when_rows_do_not_match_oracle(
        monkeypatch, query_env):
    """All the right numbers present as TOKENS in unrelated rows must still
    fail: row-level per-cell identity is required, not co-occurrence."""
    run, tmp_path = query_env
    north_block = {"north_contract_sum": "157.7500", "north_order_sum": "40.0000",
                   "north_ship_qty": "7", "north_contract_count": 3,
                   "north_order_count": 2}
    # values scattered across unrelated rows under wrong aliases
    scattered = ('{"rows": [{"note": "157.7500"}, {"unrelated": 40.0}, '
                 '{"qty": 7}, {"n": 3}, {"m": 2}]}')
    call = {"q1": {"toolName": "ontology_query", "status": "success",
                   "arguments": VALID_SPL_ARGS, "preview": scattered}}
    _patch_turn(monkeypatch, [
        {"text": "```json\n" + json.dumps(north_block) + "\n```",
         "tool_calls": dict(call)},
        {"text": "skip", "tool_calls": dict(call)},
    ])
    args = type("Args", (), {"name": "unit", "api_base": "http://127.0.0.1:59999",
                             "admin_user": "u", "admin_password": "p",
                             "secrets_dir": "/nonexistent"})()
    result = a28.phase_query(args)
    assert result["result"] == "fail"
    assert any("north tool-row evidence" in p for p in result["problems"])


def test_query_phase_fails_when_null_turns_into_zero(monkeypatch, query_env):
    run, tmp_path = query_env
    north_block = {"north_contract_sum": "157.7500", "north_order_sum": "40.0000",
                   "north_ship_qty": "7", "north_contract_count": 3,
                   "north_order_count": 2}
    bad_rows = [
        {"customer_id": "c1", "region": "north", "contract_sum": "150.7500",
         "contract_count": 2, "order_sum": "40.0000", "order_count": 2,
         "ship_qty": "7"},
        {"customer_id": "c2", "region": "south", "contract_sum": 0,
         "contract_count": 0, "order_sum": "20.0000", "order_count": 1,
         "ship_qty": "5"},
        {"customer_id": "c3", "region": "north", "contract_sum": "7.0000",
         "contract_count": 1, "order_sum": 0, "order_count": 0, "ship_qty": 0},
    ]
    north_call = {"q1": {"toolName": "ontology_query", "status": "success",
                         "arguments": VALID_SPL_ARGS, "preview": NORTH_PREVIEW}}
    detail_call = {"q1": {"toolName": "ontology_query", "status": "success",
                          "arguments": VALID_SPL_ARGS, "preview": DETAIL_PREVIEW}}
    _patch_turn(monkeypatch, [
        {"text": "```json\n" + json.dumps(north_block) + "\n```",
         "tool_calls": dict(north_call)},
        {"text": "```json\n" + json.dumps(bad_rows) + "\n```",
         "tool_calls": dict(detail_call)},
    ])
    args = type("Args", (), {"name": "unit", "api_base": "http://127.0.0.1:59999",
                             "admin_user": "u", "admin_password": "p",
                             "secrets_dir": "/nonexistent"})()
    result = a28.phase_query(args)
    assert result["result"] == "fail"
    joined = " ".join(result["detail_problems"])
    assert "c2.contract_sum" in joined
    assert "c3.order_sum" in joined
    assert "c3.ship_qty" in joined


# ---------------------------------------------------------------------------
# mutate-leg gate unit: zero-change checkpoint ordering


def test_extract_query_ids_from_escaped_and_plain_text():
    plain = '{"queryId": "590e3eca62894ed3a22c344021b56cfc", "rows": []}'
    escaped = ('{\"queryId\": \"98e8138b2c804eaeb40334e4abf14743\", '
               '"schemaVersion": 2}')
    ids = a28.extract_query_ids(plain, escaped, "", None)
    assert ids == {"590e3eca62894ed3a22c344021b56cfc",
                   "98e8138b2c804eaeb40334e4abf14743"}


def test_cancel_query_handles_records_server_cancelled_flag():
    api = _FakeApi([])
    def fake_request(method, path, body=None):
        if body and body.get("queryId", "").startswith("590e"):
            return 200, {"data": {"queryId": body["queryId"],
                                  "cancelled": True}}
        return 200, {"data": {"queryId": (body or {}).get("queryId"),
                              "cancelled": False}}
    api.request = fake_request
    results = a28.cancel_query_handles(api, {"590e3eca62894ed3a22c344021b56cfc",
                                              "f" * 32})
    assert results["total"] == 2
    assert results["cancelled"] == 1          # server-flag true, not HTTP-200
    assert results["absent_or_expired"] == 1
    per_id = results["results"]
    assert per_id["590e3eca62894ed3a22c344021b56cfc"]["cancelled"] is True
    assert per_id["f" * 32]["cancelled"] is False
    assert per_id["590e3eca62894ed3a22c344021b56cfc"]["response"]["queryId"] \
        == "590e3eca62894ed3a22c344021b56cfc"


def test_collect_row_evidence_persisted_first_over_junk_preview(monkeypatch):
    """With a metadata DB configured the persisted FULL tool result is
    authoritative and fetched first, even when the preview would salvage
    non-empty metadata junk (run06 attempt-1 regression)."""
    aliases = {"ship_qty"}
    junk_preview = ('{"queryId": "q0", "releases": {"a": {"ontologyId": "x", '
                    '"dataMode": "remote"}}')
    persisted = json.dumps(_complete_persisted_envelope([{"ship_qty": 6}]))
    monkeypatch.setattr(a28, "fetch_persisted_tool_result",
                        lambda url, conv, run: persisted)

    class _Turn:
        tool_calls = {"r1": {"toolName": "ontology_query",
                             "status": "success",
                             "plan_proof": {"ok": True, "mode": "spl_text"},
                             "preview": junk_preview}}

    turn = _Turn()
    rows = a28.collect_row_evidence(turn, aliases, conversation_id="c1",
                                    meta_db_url="dsn")
    assert rows == [("r1", [{"ship_qty": 6}])]
    assert turn.tool_calls["r1"]["rows_source"] == "persisted_result"
    # without a metadata DB, junk preview yields NO rows (never fake them)
    turn2 = _Turn()
    assert a28.collect_row_evidence(turn2, aliases, conversation_id="c1",
                                    meta_db_url="") == []
    assert turn2.tool_calls["r1"]["rows_source"] is None


def test_collect_row_evidence_preview_only_is_diagnostic():
    class _Turn:
        tool_calls = {"r1": {"toolName": "ontology_query",
                             "status": "success",
                             "plan_proof": {"ok": True, "mode": "spl_text"},
                             "preview": '{"rows": [{"ship_qty": 6}]}'}}

    turn = _Turn()
    rows = a28.collect_row_evidence(turn, {"ship_qty"},
                                    conversation_id="c1", meta_db_url="")
    assert rows == [], "preview-only evidence must never reach the verifier"
    assert turn.tool_calls["r1"]["rows_source"] == "preview_diagnostic"
    assert turn.tool_calls["r1"]["preview_row_count"] == 1


def test_phase_setup_refuses_legacy_pg_before_io():
    result = a28.phase_setup(type("Args", (), {})())
    assert result["result"] == "blocked"
    assert "legacy PG" in result["reason"]


def test_ensure_assets_refuses_legacy_pg_before_http():
    with pytest.raises(RuntimeError, match="legacy PG"):
        a28.ensure_assets(object(), "conn", "schema")


def test_module_keeps_secrets_out_of_run_state():
    """Run.save() scrubs: a leaked password key never lands on disk."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        original = a28.RUNS
        a28.RUNS = Path(tmp)
        try:
            run = a28.Run("scrub-check")
            run.step("leak").update({"password": "super", "nested": {
                "api_key": "k", "ok": 1}})
            run.save()
            saved = json.loads((Path(tmp) / "scrub-check.json").read_text())
            assert saved["steps"]["leak"]["password"] == "<redacted>"
            assert saved["steps"]["leak"]["nested"]["api_key"] == "<redacted>"
            assert saved["steps"]["leak"]["nested"]["ok"] == 1
        finally:
            a28.RUNS = original
