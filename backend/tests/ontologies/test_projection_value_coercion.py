"""投影值 coercion 的类型保真测试（PR-C）。

钉住三件事：
1. number 整数优先——bigint 主键/编号不经 float64，2^53 之上不静默变值；
2. date/datetime 分支——空格分隔/Z 后缀等入库形态归一为实例契约可接受的
   ISO 输出（修复"保存放行、投影后校验整批回滚"）；
3. 输出与 formal_modeling.validation 的 fromisoformat 口径一致。
"""
import datetime as dt

import pytest

from app.ontologies.mappings.formal_projection_contract import (
    _coerce_props_to_type,
)


def _typed(**name_type: str) -> list[dict]:
    return [{"name": name, "type": type_} for name, type_ in name_type.items()]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 大整数必须精确：雪花 ID 常见量级，曾因 float64 中转静默变值
        ("9007199254740993", 9007199254740993),
        # numeric(38,2) 列经归一化产出的带小数尾缀整值文本（复核发现的
        # 残余缺口：int() 解析不了而 float64 会变值，整值判定走 Decimal）
        ("9007199254740993.00", 9007199254740993),
        ("9,007,199,254,740,993", 9007199254740993),
        ("9223372036854775807", 9223372036854775807),
        ("-9007199254740993", -9007199254740993),
        ("123.45", 123.45),
        ("3.0", 3),
        ("1,234", 1234),
        ("1e3", 1000),
        # Python 数值文本下划线语义：int()/float() 本就接受，钉住防"收紧"
        ("1_0", 10),
    ],
)
def test_number_coercion_integer_first(raw, expected):
    props = _coerce_props_to_type(
        {"amount": raw}, _typed(amount="number"))
    assert props["amount"] == expected
    assert isinstance(props["amount"], type(expected))


def test_number_coercion_rejects_garbage():
    with pytest.raises(ValueError, match="amount"):
        _coerce_props_to_type({"amount": "abc"}, _typed(amount="number"))


def test_native_values_pass_through_untouched():
    big = 2**63 - 1
    props = _coerce_props_to_type(
        {"amount": big, "flag": True, "tags": [1, 2]},
        _typed(amount="number", flag="boolean", tags="array"))
    assert props["amount"] == big
    assert props["flag"] is True
    assert props["tags"] == [1, 2]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 数据库驱动/CSV 的空格分隔形态 → 截断到日（R2 回归：此前透传导致
        # 实例契约 fromisoformat 校验失败、投影整批回滚）
        ("2026-01-15 10:30:00", "2026-01-15"),
        ("2026-01-15T10:30:00", "2026-01-15"),
        ("2026-01-15", "2026-01-15"),
        ("2026-01-15T10:30:00+08:00", "2026-01-15"),
        # 钉住"按值自带偏移的墙面日期截断"语义：UTC 视角这是 01-14，
        # 业务日语义下取墙面日是设计选择（docstring 已声明）
        ("2026-01-15T00:30:00+08:00", "2026-01-15"),
    ],
)
def test_date_coercion_truncates_to_day(raw, expected):
    props = _coerce_props_to_type({"d": raw}, _typed(d="date"))
    assert props["d"] == expected
    # 输出必须能通过实例契约的 date 校验口径
    dt.date.fromisoformat(props["d"])


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-01-15 10:30:00", "2026-01-15T10:30:00"),
        ("2026-01-15T10:30:00", "2026-01-15T10:30:00"),
        ("2026-01-15", "2026-01-15T00:00:00"),
        ("2026-01-15T10:30:00Z", "2026-01-15T10:30:00+00:00"),
        ("2026-01-15 10:30:00Z", "2026-01-15T10:30:00+00:00"),
        ("2026-01-15 10:30:00+08:00", "2026-01-15T10:30:00+08:00"),
    ],
)
def test_datetime_coercion_normalizes_separator(raw, expected):
    props = _coerce_props_to_type({"ts": raw}, _typed(ts="datetime"))
    assert props["ts"] == expected
    # 输出必须能通过实例契约的 datetime 校验口径
    dt.datetime.fromisoformat(props["ts"].replace("Z", "+00:00"))


def test_native_datetime_objects_pass_through_for_temporal_props():
    native = dt.datetime(2026, 1, 15, 10, 30)
    props = _coerce_props_to_type(
        {"d": native, "ts": native}, _typed(d="date", ts="datetime"))
    assert props["d"] is native  # 契约层 isinstance(datetime) 接受，不误拦
    assert props["ts"] is native


def test_temporal_coercion_error_is_actionable():
    with pytest.raises(ValueError, match="ISO 格式"):
        _coerce_props_to_type({"d": "15/01/2026"}, _typed(d="date"))
    with pytest.raises(ValueError, match="ISO 格式"):
        _coerce_props_to_type({"ts": "not-a-time"}, _typed(ts="datetime"))


def test_empty_string_still_means_null_for_typed_props():
    props = _coerce_props_to_type(
        {"amount": " ", "d": "", "name": "", "flag": ""},
        _typed(amount="number", d="date", name="string", flag="boolean"))
    assert props["amount"] is None
    assert props["d"] is None
    assert props["flag"] is None
    assert props["name"] == ""  # string 保留业务文本
