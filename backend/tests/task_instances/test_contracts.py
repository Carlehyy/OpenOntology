"""端口契约解释器测试：子集校验 / 声明界限 / 确定性样本合成。"""
from __future__ import annotations

import pytest

from app.task_instances import contracts as ce


class TestValidate:
    def test_type_and_enum(self):
        assert ce.validate({"type": "string"}, "x") == []
        assert ce.validate({"type": "string"}, 1) != []
        assert ce.validate({"enum": ["a", "b"]}, "a") == []
        assert ce.validate({"enum": ["a", "b"]}, "c") != []

    def test_const(self):
        assert ce.validate({"const": 7}, 7) == []
        assert ce.validate({"const": 7}, 8) != []

    def test_object_required_and_properties(self):
        contract = {
            "type": "object",
            "required": ["category"],
            "properties": {
                "category": {"enum": ["frontend", "backend"]},
                "count": {"type": "integer", "minimum": 0, "maximum": 10},
            },
        }
        assert ce.validate(contract, {"category": "frontend"}) == []
        errors = ce.validate(contract, {})
        assert any("category" in e for e in errors)
        assert ce.validate(contract, {"category": "nope"}) != []
        assert ce.validate(contract, {"category": "frontend", "count": 11}) != []

    def test_additional_properties_false(self):
        contract = {
            "type": "object", "properties": {"a": {"type": "string"}},
            "additionalProperties": False,
        }
        assert ce.validate(contract, {"a": "x"}) == []
        assert ce.validate(contract, {"a": "x", "b": 1}) != []

    def test_array_items_and_bounds(self):
        contract = {"type": "array", "minItems": 1, "maxItems": 2,
                    "items": {"type": "string", "minLength": 2}}
        assert ce.validate(contract, ["ab"]) == []
        assert ce.validate(contract, []) != []
        assert ce.validate(contract, ["ab", "cd", "ef"]) != []
        assert ce.validate(contract, ["a"]) != []

    def test_string_length_and_pattern(self):
        assert ce.validate({"type": "string", "minLength": 2}, "ab") == []
        assert ce.validate({"type": "string", "maxLength": 2}, "abc") != []
        assert ce.validate({"type": "string", "pattern": "^\\d+$"}, "123") == []
        assert ce.validate({"type": "string", "pattern": "^\\d+$"}, "a12") != []

    def test_integer_not_boolean(self):
        assert ce.validate({"type": "integer"}, 3) == []
        assert ce.validate({"type": "integer"}, True) != []
        assert ce.validate({"type": "boolean"}, True) == []


class TestSchemaBounds:
    def test_unknown_keyword_rejected(self):
        with pytest.raises(ce.ContractError):
            ce.validate_contract_schema({"format": "uri"})

    def test_depth_bounded(self):
        node = {"type": "object", "properties": {"a": {"type": "object"}}}
        for _ in range(10):
            node = {"type": "object", "properties": {"a": node}}
        with pytest.raises(ce.ContractError):
            ce.validate_contract_schema(node)

    def test_pattern_length_bounded(self):
        with pytest.raises(ce.ContractError):
            ce.validate_contract_schema(
                {"type": "string", "pattern": "a" * 300})

    def test_invalid_regex_rejected(self):
        with pytest.raises(ce.ContractError):
            ce.validate_contract_schema({"type": "string", "pattern": "["})

    def test_enum_bounds(self):
        with pytest.raises(ce.ContractError):
            ce.validate_contract_schema({"enum": list(range(100))})


class TestSynthesize:
    def test_enum_first_const_direct(self):
        assert ce.synthesize({"enum": ["a", "b"]}) == "a"
        assert ce.synthesize({"const": 42}) == 42

    def test_object_required_properties(self):
        contract = {"type": "object", "required": ["x", "y"], "properties": {
            "x": {"enum": ["frontend"]},
            "y": {"type": "integer", "minimum": 5, "maximum": 9},
        }}
        value = ce.synthesize(contract)
        assert value == {"x": "frontend", "y": 5}
        assert ce.validate(contract, value) == []

    def test_array_min_items(self):
        contract = {"type": "array", "minItems": 2,
                    "items": {"enum": ["k"]}}
        value = ce.synthesize(contract)
        assert value == ["k", "k"]
        assert ce.validate(contract, value) == []

    def test_string_min_length(self):
        value = ce.synthesize({"type": "string", "minLength": 5}, seed="ab")
        assert len(value) == 5

    def test_deterministic(self):
        contract = {"type": "object", "required": ["s"],
                    "properties": {"s": {"type": "string"}}}
        assert (ce.synthesize(contract, seed="x")
                == ce.synthesize(contract, seed="x"))
