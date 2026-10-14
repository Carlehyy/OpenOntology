"""WorkflowSpec 解析/编译/语义校验测试（§4.3 校验规则清单）。"""
from __future__ import annotations

import pytest

from tests.task_instances.conftest import make_spec
from app.task_instances.spec import (
    SpecError,
    canonical_hash,
    canonical_json,
    compile_workflow_yaml,
    parse_workflow_yaml,
)
from app.task_instances.validator import validate_workflow


def _compile(text: str):
    compiled = compile_workflow_yaml(text)
    errors = validate_workflow(compiled.spec)
    return compiled, errors


def test_design_example_compiles_clean():
    compiled, errors = _compile(make_spec())
    assert errors == []
    assert len(compiled.canonical_hash) == 64


def test_yaml_on_key_stays_string():
    """YAML 1.1 陷阱：裸键 on 不得被解析为布尔 True。"""
    spec = parse_workflow_yaml(make_spec())
    assert "route" in spec.nodes
    assert spec.nodes["route"].on == "analyze.done"


def test_canonical_hash_idempotent_and_sensitive():
    one = compile_workflow_yaml(make_spec())
    two = compile_workflow_yaml(make_spec())
    assert one.canonical_hash == two.canonical_hash
    changed = compile_workflow_yaml(make_spec(corrections=3))
    assert changed.canonical_hash != one.canonical_hash


def test_defaults_normalized_in_canonical():
    spec = parse_workflow_yaml(make_spec())
    canonical = canonical_json(spec)
    assert canonical["policies"]["corrections_per_node"] == 2
    assert canonical["contracts"] != None  # noqa: E711 — None 归一为 {}


def test_bad_api_version_rejected():
    text = make_spec().replace("openontology.task/v1", "v0/other")
    with pytest.raises(SpecError):
        parse_workflow_yaml(text)


def test_bad_yaml_syntax_reports_error():
    with pytest.raises(SpecError):
        parse_workflow_yaml(":\n\t- ]]")


def test_unknown_node_field_rejected():
    text = make_spec().replace(
        "  analyze:\n    kind: agent",
        "  analyze:\n    kind: agent\n    bogus_field: 1")
    with pytest.raises(SpecError):
        parse_workflow_yaml(text)


def _swap(text: str, old: str, new: str) -> str:
    assert old in text
    return text.replace(old, new)


def test_cycle_detected_with_path():
    text = _swap(make_spec(),
                 "  end_ok: { kind: terminal, outcome: success }",
                 "  end_ok: { kind: terminal, outcome: success }\n"
                 "  loop_a: { kind: agent, system: a }\n"
                 "  loop_b: { kind: agent, system: b }")
    text = _swap(text, "edges:", "edges:\n"
                 "  - { from: loop_a.done, to: loop_b }\n"
                 "  - { from: loop_b.done, to: loop_a }")
    _, errors = _compile(text)
    codes = {e["code"] for e in errors}
    assert "CYCLE" in codes
    cycle = next(e for e in errors if e["code"] == "CYCLE")
    assert "->" in cycle["message"]


def test_edge_to_missing_node():
    text = _swap(make_spec(), "to: gate }", "to: ghost }")
    _, errors = _compile(text)
    assert any(e["code"] == "EDGE_REF" for e in errors)


def test_bad_port_reference():
    text = _swap(make_spec(), "from: gate.approved", "from: gate.whatever")
    _, errors = _compile(text)
    assert any(e["code"] == "PORT_REF" for e in errors)


def test_approval_edge_requires_explicit_port():
    text = _swap(make_spec(), "from: gate.approved, to: end_ok",
                 "from: gate, to: end_ok")
    _, errors = _compile(text)
    assert any(e["code"] == "PORT_REQUIRED" for e in errors)


def test_condition_requires_default():
    text = _swap(make_spec(), "    default: [human_review]", "")
    _, errors = _compile(text)
    assert any(e["code"] == "CONDITION_DEFAULT" for e in errors)


def test_contract_reference_must_exist():
    text = _swap(make_spec(), "contract: ChangeSummary", "contract: NoSuch")
    _, errors = _compile(text)
    assert any(e["code"] == "CONTRACT_REF" for e in errors)


def test_contract_schema_whitelist_enforced():
    text = _swap(make_spec(),
                 "      category: { enum: [frontend, backend, defect_fix] }",
                 "      category: { enum: [frontend], format: uri }")
    _, errors = _compile(text)
    assert any(e["code"] == "CONTRACT_INVALID" for e in errors)


def test_rework_target_must_be_redoable():
    text = _swap(make_spec(), "to: auto_fix, rework: true",
                 "to: gate, rework: true")
    _, errors = _compile(text)
    assert any(e["code"] == "REWORK_TARGET" for e in errors)


def test_rework_only_cycle_detected():
    # 两条合法端口的打回边构成环：auto_fix ⇄ human_review 互相打回
    text = _swap(make_spec(),
                 "  - { from: gate.rejected, to: auto_fix, rework: true }",
                 "  - { from: gate.rejected, to: auto_fix, rework: true }\n"
                 "  - { from: auto_fix.done, to: human_review, rework: true }\n"
                 "  - { from: human_review.done, to: auto_fix, rework: true }")
    _, errors = _compile(text)
    assert any(e["code"] == "REWORK_CYCLE" for e in errors)


def test_condition_cannot_have_incoming_edge():
    text = _swap(make_spec(), "edges:", "edges:\n"
                 "  - { from: analyze.done, to: route }")
    _, errors = _compile(text)
    assert any(e["code"] == "EDGE_TO_CONDITION" for e in errors)


def test_terminal_unreachable_detected():
    text = _swap(make_spec(),
                 "  - { from: gate.approved, to: end_ok }",
                 "  - { from: gate.approved, to: human_review }")
    _, errors = _compile(text)
    assert any(e["code"] == "TERMINAL_UNREACHABLE" for e in errors)
