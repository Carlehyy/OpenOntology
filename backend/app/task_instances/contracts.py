"""任务实例 — 端口契约：有界 JSON Schema 子集解释器（设计方案 §4.4）。

自研解释器（pydantic 之外的纯函数实现），零新依赖。白名单制：
允许 type/enum/const/required/properties/additionalProperties/items/
minItems/maxItems/minLength/maxLength/minimum/maximum/pattern，
越界关键字即契约本身不合法。

界限（防深度递归与 ReDoS）：嵌套深度 ≤8、总属性 ≤64、enum ≤64 项、
pattern ≤256 字符且只匹配 ≤4096 字符的字符串。

`synthesize` 供确定性假执行器使用：按契约生成首个可满足的样本值。
"""
from __future__ import annotations

import re
from typing import Any

MAX_DEPTH = 8
MAX_PROPERTIES = 64
MAX_ENUM_ITEMS = 64
MAX_PATTERN_LENGTH = 256
MAX_PATTERN_SUBJECT_LENGTH = 4096

_TYPES = ("object", "array", "string", "number", "integer", "boolean")

_ALLOWED_KEYWORDS = frozenset({
    "type", "enum", "const", "required", "properties",
    "additionalProperties", "items", "minItems", "maxItems",
    "minLength", "maxLength", "minimum", "maximum", "pattern",
    "description",
})


class ContractError(Exception):
    """契约本身不合法（模板校验期抛出）。"""


def validate_contract_schema(contract: Any) -> None:
    """校验契约声明本身：白名单关键字 + 类型正确 + 界限。深度优先。"""
    _validate_schema_node(contract, depth=0, path="$")


def _validate_schema_node(node: Any, *, depth: int, path: str) -> None:
    if depth > MAX_DEPTH:
        raise ContractError(f"{path}: 契约嵌套深度超过 {MAX_DEPTH}")
    if not isinstance(node, dict):
        raise ContractError(f"{path}: 契约必须是映射")
    unknown = set(node) - _ALLOWED_KEYWORDS
    if unknown:
        raise ContractError(
            f"{path}: 不支持的关键字 {sorted(unknown)}（白名单外一律拒绝）")
    if (node.get("type") is not None and node["type"] not in _TYPES
            and not isinstance(node["type"], list)):
        raise ContractError(f"{path}: type 必须是 { '/'.join(_TYPES) } 或其数组")
    if "enum" in node:
        enum = node["enum"]
        if not isinstance(enum, list) or not enum or len(enum) > MAX_ENUM_ITEMS:
            raise ContractError(
                f"{path}: enum 必须是 1~{MAX_ENUM_ITEMS} 个元素的数组")
    if "pattern" in node:
        pattern = node["pattern"]
        if not isinstance(pattern, str) or not pattern or len(pattern) > MAX_PATTERN_LENGTH:
            raise ContractError(
                f"{path}: pattern 必须是 1~{MAX_PATTERN_LENGTH} 字符")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ContractError(f"{path}: pattern 非法: {exc}") from exc
    for count_key in ("minItems", "maxItems", "minLength", "maxLength"):
        if count_key in node and (not isinstance(node[count_key], int)
                                  or node[count_key] < 0):
            raise ContractError(f"{path}: {count_key} 必须是非负整数")
    for bound_key in ("minimum", "maximum"):
        if bound_key in node and not isinstance(node[bound_key], (int, float)):
            raise ContractError(f"{path}: {bound_key} 必须是数值")
    properties = node.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            raise ContractError(f"{path}: properties 必须是映射")
        if len(properties) > MAX_PROPERTIES:
            raise ContractError(f"{path}: 属性数超过 {MAX_PROPERTIES}")
        for key, child in properties.items():
            _validate_schema_node(child, depth=depth + 1,
                                  path=f"{path}.{key}")
    required = node.get("required")
    if required is not None and (
            not isinstance(required, list)
            or not all(isinstance(k, str) for k in required)):
        raise ContractError(f"{path}: required 必须是字符串数组")
    items = node.get("items")
    if items is not None:
        _validate_schema_node(items, depth=depth + 1, path=f"{path}[]")


def validate(contract: dict, value: Any, *, path: str = "$") -> list[str]:
    """校验 value 是否满足契约；返回违规列表（空 = 通过）。"""
    errors: list[str] = []
    if "type" in contract:
        expected = contract["type"]
        expected_list = expected if isinstance(expected, list) else [expected]
        if not any(_matches_type(t, value) for t in expected_list):
            errors.append(f"{path}: 期望类型 {expected}，实际 {_json_type(value)}")
    if "const" in contract and value != contract["const"]:
        errors.append(f"{path}: 必须等于常量 {contract['const']!r}")
    if "enum" in contract and value not in contract["enum"]:
        errors.append(f"{path}: 值不在 enum 允许范围内")
    if isinstance(value, str):
        errors.extend(_validate_string(contract, value, path))
    elif isinstance(value, bool):
        pass
    elif isinstance(value, (int, float)):
        errors.extend(_validate_number(contract, value, path))
    elif isinstance(value, list):
        errors.extend(_validate_array(contract, value, path))
    elif isinstance(value, dict):
        errors.extend(_validate_object(contract, value, path))
    return errors


def _validate_string(contract: dict, value: str, path: str) -> list[str]:
    errors: list[str] = []
    if "minLength" in contract and len(value) < contract["minLength"]:
        errors.append(f"{path}: 长度 {len(value)} 小于 minLength {contract['minLength']}")
    if "maxLength" in contract and len(value) > contract["maxLength"]:
        errors.append(f"{path}: 长度 {len(value)} 超过 maxLength {contract['maxLength']}")
    if "pattern" in contract and len(value) <= MAX_PATTERN_SUBJECT_LENGTH:
        if re.search(contract["pattern"], value) is None:
            errors.append(f"{path}: 不满足 pattern {contract['pattern']!r}")
    return errors


def _validate_number(contract: dict, value: float, path: str) -> list[str]:
    errors: list[str] = []
    if "minimum" in contract and value < contract["minimum"]:
        errors.append(f"{path}: {value} 小于 minimum {contract['minimum']}")
    if "maximum" in contract and value > contract["maximum"]:
        errors.append(f"{path}: {value} 超过 maximum {contract['maximum']}")
    return errors


def _validate_array(contract: dict, value: list, path: str) -> list[str]:
    errors: list[str] = []
    if "minItems" in contract and len(value) < contract["minItems"]:
        errors.append(f"{path}: 元素数 {len(value)} 小于 minItems {contract['minItems']}")
    if "maxItems" in contract and len(value) > contract["maxItems"]:
        errors.append(f"{path}: 元素数 {len(value)} 超过 maxItems {contract['maxItems']}")
    items = contract.get("items")
    if items is not None:
        for index, item in enumerate(value):
            errors.extend(validate(items, item, path=f"{path}[{index}]"))
    return errors


def _validate_object(contract: dict, value: dict, path: str) -> list[str]:
    errors: list[str] = []
    for key in contract.get("required", []):
        if key not in value:
            errors.append(f"{path}: 缺少必填字段 {key!r}")
    properties = contract.get("properties")
    if properties:
        for key, child in properties.items():
            if key in value:
                errors.extend(validate(child, value[key], path=f"{path}.{key}"))
    additional = contract.get("additionalProperties")
    if additional is False and properties is not None:
        extra = set(value) - set(properties)
        if extra:
            errors.append(f"{path}: 存在未声明属性 {sorted(extra)}")
    return errors


def _matches_type(expected: str, value: Any) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return False


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def synthesize(contract: dict, *, seed: str = "sample") -> Any:
    """按契约生成确定性样本值（假执行器 / 校验自检用）。

    pattern 无法通用构造，best-effort 后由调用方 validate 兜底；
    enum 取首项，const 直取，数值边界取 min(max(1, minimum), maximum)。
    """
    return _synthesize(contract, seed=seed, depth=0)


def _synthesize(contract: dict, *, seed: str, depth: int) -> Any:
    if depth > MAX_DEPTH:
        return None
    if "const" in contract:
        return contract["const"]
    if "enum" in contract:
        return contract["enum"][0]
    expected = contract.get("type")
    if isinstance(expected, list):
        expected = expected[0] if expected else None
    if expected is None:
        if "properties" in contract or "required" in contract:
            expected = "object"
        elif "items" in contract:
            expected = "array"
        else:
            expected = "string"
    if expected == "object":
        properties = contract.get("properties") or {}
        required = contract.get("required") or list(properties)
        result: dict[str, Any] = {}
        for key in required:
            if key in properties:
                result[key] = _synthesize(properties[key], seed=seed,
                                          depth=depth + 1)
            else:
                result[key] = seed
        return result
    if expected == "array":
        count = contract.get("minItems") or 1
        items = contract.get("items") or {}
        return [_synthesize(items, seed=seed, depth=depth + 1) for _ in range(count)]
    if expected == "integer":
        minimum = contract.get("minimum", 1)
        maximum = contract.get("maximum", minimum)
        return max(minimum, min(maximum, 1))
    if expected == "number":
        minimum = contract.get("minimum", 1.0)
        maximum = contract.get("maximum", minimum)
        return float(max(minimum, min(maximum, 1.0)))
    if expected == "boolean":
        return True
    # string：seed 完整参与样本（不足 minLength 补重复、超 maxLength 截断），
    # 使重做/纠正后的产出天然不同于上次（下游 inputs_hash 才会变化）
    text = seed
    min_length = contract.get("minLength", 1)
    if len(text) < min_length:
        text = (text * (min_length // max(len(text), 1) + 1))[:min_length]
    max_length = contract.get("maxLength")
    if max_length is not None and len(text) > max_length:
        text = text[:max_length]
    return text
