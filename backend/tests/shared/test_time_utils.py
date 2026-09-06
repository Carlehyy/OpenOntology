"""shared.time_utils.parse_temporal_text 单元测试。

该解析器是投影 date/datetime coercion 与实例契约校验之间的归一层：
输入端容忍数据库驱动/CSV 的空格分隔与 Z 后缀形态，输出端保证
datetime.fromisoformat 可解析。
"""
import datetime as dt

import pytest

from app.shared.time_utils import parse_temporal_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-01-15 10:30:00", dt.datetime(2026, 1, 15, 10, 30, 0)),
        ("2026-01-15T10:30:00", dt.datetime(2026, 1, 15, 10, 30, 0)),
        ("2026-01-15", dt.datetime(2026, 1, 15)),
        (
            "2026-01-15T10:30:00Z",
            dt.datetime(2026, 1, 15, 10, 30, 0,
                        tzinfo=dt.timezone(dt.timedelta(hours=0))),
        ),
        (
            "2026-01-15 10:30:00+08:00",
            dt.datetime(2026, 1, 15, 10, 30,
                        tzinfo=dt.timezone(dt.timedelta(hours=8))),
        ),
        ("  2026-01-15T10:30:00  ", dt.datetime(2026, 1, 15, 10, 30, 0)),
    ],
)
def test_parse_temporal_text_accepts_pragmatic_iso_forms(raw, expected):
    assert parse_temporal_text(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["15/01/2026", "not-a-time", "", "2026-13-45", "Jan 15 2026"],
)
def test_parse_temporal_text_rejects_non_iso(raw):
    with pytest.raises(ValueError):
        parse_temporal_text(raw)
