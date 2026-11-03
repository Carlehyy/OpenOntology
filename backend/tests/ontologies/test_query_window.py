"""服务端相对时间窗口：固定 now、显式 timezone、绝对 [start,end)。"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.ontologies.agent_runtime.boundary import ToolError
from app.ontologies.agent_runtime import query_window as W

FIXED_NOW = datetime(2026, 11, 3, 4, 0, tzinfo=timezone.utc)  # 12:00 Asia/Shanghai


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(W, "utc_now", lambda: FIXED_NOW)


def _relative(last, *, property_type="datetime", timezone_name="Asia/Shanghai",
              prop="occurred_at"):
    return W.resolve_window({
        "property": prop,
        "timezone": timezone_name,
        "last": last,
    }, property_type=property_type)


def _absolute(start, end, *, property_type="datetime",
              timezone_name="Asia/Shanghai", prop="occurred_at"):
    return W.resolve_window({
        "property": prop,
        "timezone": timezone_name,
        "start": start,
        "end": end,
    }, property_type=property_type)


def test_relative_window_freezes_absolute_boundaries_in_timezone():
    resolved = W.resolve_window({
        "property": "occurred_at",
        "timezone": "Asia/Shanghai",
        "last": {"unit": "day", "count": 7},
    }, property_type="datetime")

    assert resolved["kind"] == "relative"
    assert resolved["timezone"] == "Asia/Shanghai"
    assert resolved["start"] == "2026-10-27T12:00:00+08:00"
    assert resolved["end"] == "2026-11-03T12:00:00+08:00"
    # datetime 过滤值统一 UTC，保证本地字符串比较的时间顺序。
    assert resolved["startValue"] == "2026-10-27T04:00:00+00:00"
    assert resolved["endValue"] == "2026-11-03T04:00:00+00:00"
    assert resolved["exclusiveEnd"] is True
    leaves = W.window_filters(resolved, source="s0")
    assert [(leaf["op"], leaf["value"]) for leaf in leaves] == [
        ("gte", "2026-10-27T04:00:00+00:00"),
        ("lt", "2026-11-03T04:00:00+00:00"),
    ]


def test_last_30_minutes_is_the_supported_business_case():
    resolved = _relative({"unit": "minute", "count": 30})

    assert resolved["start"] == "2026-11-03T11:30:00+08:00"
    assert resolved["end"] == "2026-11-03T12:00:00+08:00"
    assert resolved["startUtc"] == "2026-11-03T03:30:00+00:00"
    assert resolved["endUtc"] == "2026-11-03T04:00:00+00:00"
    assert "最近 30 分钟" in resolved["display"]
    assert "UTC 精确时长" in resolved["display"]


@pytest.mark.parametrize("unit,expected_start", [
    ("second", "2026-11-03T11:59:58+08:00"),
    ("seconds", "2026-11-03T11:59:58+08:00"),
    ("minute", "2026-11-03T11:58:00+08:00"),
    ("minutes", "2026-11-03T11:58:00+08:00"),
    ("MINUTES", "2026-11-03T11:58:00+08:00"),
    ("hour", "2026-11-03T10:00:00+08:00"),
    ("hours", "2026-11-03T10:00:00+08:00"),
])
def test_duration_units_accept_singular_and_plural_aliases(unit, expected_start):
    resolved = _relative({"unit": unit, "count": 2})
    assert resolved["start"] == expected_start


def test_hour_window_across_dst_fall_back_uses_elapsed_time(monkeypatch):
    # 2026-11-01 02:00 America/New_York 回拨：06:30Z 已是第二次 01:30。
    monkeypatch.setattr(
        W, "utc_now",
        lambda: datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc))
    resolved = _relative(
        {"unit": "hour", "count": 1}, timezone_name="America/New_York")

    assert resolved["startUtc"] == "2026-11-01T05:30:00+00:00"
    assert resolved["endUtc"] == "2026-11-01T06:30:00+00:00"
    assert resolved["start"] == "2026-11-01T01:30:00-04:00"
    assert resolved["end"] == "2026-11-01T01:30:00-05:00"
    elapsed = (datetime.fromisoformat(resolved["endUtc"])
               - datetime.fromisoformat(resolved["startUtc"]))
    assert elapsed == timedelta(hours=1)


def test_hour_window_across_dst_spring_forward_uses_elapsed_time(monkeypatch):
    # 2026-03-08 02:00 America/New_York 跳变：07:30Z 是 03:30 EDT。
    monkeypatch.setattr(
        W, "utc_now",
        lambda: datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc))
    resolved = _relative(
        {"unit": "hour", "count": 2}, timezone_name="America/New_York")

    assert resolved["startUtc"] == "2026-03-08T05:30:00+00:00"
    assert resolved["endUtc"] == "2026-03-08T07:30:00+00:00"
    assert resolved["start"] == "2026-03-08T00:30:00-05:00"
    assert resolved["end"] == "2026-03-08T03:30:00-04:00"


def test_day_window_keeps_calendar_wall_clock_across_dst(monkeypatch):
    # 本地 11-02 00:30 EST 回退 1 天 = 11-01 00:30 EDT（回拨前），真实经过 25 小时。
    monkeypatch.setattr(
        W, "utc_now",
        lambda: datetime(2026, 11, 2, 5, 30, tzinfo=timezone.utc))
    resolved = _relative(
        {"unit": "day", "count": 1}, timezone_name="America/New_York")

    assert resolved["start"] == "2026-11-01T00:30:00-04:00"
    assert resolved["end"] == "2026-11-02T00:30:00-05:00"
    assert resolved["startUtc"] == "2026-11-01T04:30:00+00:00"
    assert resolved["endUtc"] == "2026-11-02T05:30:00+00:00"
    elapsed = (datetime.fromisoformat(resolved["endUtc"])
               - datetime.fromisoformat(resolved["startUtc"]))
    assert elapsed == timedelta(hours=25)
    assert "本地日历回退" in resolved["display"]


def test_hour_window_crosses_local_midnight_without_losing_boundaries(monkeypatch):
    # 本地 now=2026-11-03 00:15；最近 30 分钟应横跨 11-02/11-03。
    monkeypatch.setattr(
        W, "utc_now",
        lambda: datetime(2026, 11, 2, 16, 15, tzinfo=timezone.utc))
    resolved = _relative({"unit": "minute", "count": 30})

    assert resolved["start"] == "2026-11-02T23:45:00+08:00"
    assert resolved["end"] == "2026-11-03T00:15:00+08:00"
    assert resolved["startValue"] == "2026-11-02T15:45:00+00:00"
    assert resolved["endValue"] == "2026-11-02T16:15:00+00:00"


def test_date_property_window_uses_natural_day_boundaries():
    resolved = _relative({"unit": "day", "count": 1}, property_type="date",
                         prop="occurred_on")
    # 本地 now=11-03 12:00；[11-02 00:00, 11-04 00:00) 覆盖 11-02/11-03。
    assert resolved["startValue"] == "2026-11-02"
    assert resolved["endValue"] == "2026-11-04"
    assert resolved["start"].startswith("2026-11-02T00:00:00")
    assert resolved["end"].startswith("2026-11-04T00:00:00")
    assert "自然日" in resolved["display"]
    assert "全天" in resolved["display"]


def test_relative_date_window_ending_at_midnight_excludes_next_day(monkeypatch):
    # 本地 now=11-03 00:00；“最近 1 天”恰好是 11-02 全天，不能再扩到 11-04。
    monkeypatch.setattr(
        W, "utc_now",
        lambda: datetime(2026, 11, 2, 16, 0, tzinfo=timezone.utc))
    resolved = _relative({"unit": "day", "count": 1}, property_type="date",
                         prop="occurred_on")

    assert resolved["startValue"] == "2026-11-02"
    assert resolved["endValue"] == "2026-11-03"
    assert resolved["end"] == "2026-11-03T00:00:00+08:00"


def test_explicit_date_boundary_is_already_exclusive():
    resolved = _absolute("2026-11-01", "2026-11-02", property_type="date",
                         prop="occurred_on")

    # 显式 [start, end) 只覆盖 11-01；end 日期 11-02 本身必须排除。
    assert resolved["startValue"] == "2026-11-01"
    assert resolved["endValue"] == "2026-11-02"
    assert resolved["start"] == "2026-11-01T00:00:00+08:00"
    assert resolved["end"] == "2026-11-02T00:00:00+08:00"
    assert resolved["expression"] == (
        "occurred_on >= 2026-11-01 AND occurred_on < 2026-11-02")
    assert W.window_filters(resolved, source="s0") == [
        {"source": "s0", "property": "occurred_on",
         "op": "gte", "value": "2026-11-01"},
        {"source": "s0", "property": "occurred_on",
         "op": "lt", "value": "2026-11-02"},
    ]
    assert "不自动延展" in resolved["display"]


def test_explicit_date_boundary_at_max_date_does_not_overflow(monkeypatch):
    monkeypatch.setattr(
        W, "utc_now",
        lambda: datetime(9999, 12, 31, 12, 0, tzinfo=timezone.utc))
    resolved = _absolute("9999-12-30", "9999-12-31", property_type="date",
                         timezone_name="UTC", prop="occurred_on")

    assert resolved["startValue"] == "9999-12-30"
    assert resolved["endValue"] == "9999-12-31"
    assert resolved["end"] == "9999-12-31T00:00:00+00:00"


def test_relative_date_window_overflow_is_tool_error(monkeypatch):
    # 相对窗口需要把 date 上界进位到 10000-01-01，必须转成 ToolError。
    monkeypatch.setattr(
        W, "utc_now",
        lambda: datetime(9999, 12, 31, 12, 0, tzinfo=timezone.utc))
    with pytest.raises(ToolError, match="window_overflow"):
        _relative({"unit": "day", "count": 1}, property_type="date",
                  timezone_name="UTC", prop="occurred_on")


def test_duration_underflow_is_tool_error(monkeypatch):
    monkeypatch.setattr(
        W, "utc_now",
        lambda: datetime(1, 1, 1, 0, 0, 30, tzinfo=timezone.utc))
    with pytest.raises(ToolError, match="window_overflow"):
        _relative({"unit": "minute", "count": 1}, timezone_name="UTC")


@pytest.mark.parametrize("now,unit,count,expected_start", [
    (datetime(2026, 3, 31, 4, 0, tzinfo=timezone.utc),
     "month", 1, "2026-02-28T12:00:00+08:00"),
    (datetime(2024, 3, 31, 4, 0, tzinfo=timezone.utc),
     "month", 1, "2024-02-29T12:00:00+08:00"),
    (datetime(2024, 2, 29, 4, 0, tzinfo=timezone.utc),
     "year", 1, "2023-02-28T12:00:00+08:00"),
    (datetime(2026, 5, 31, 4, 0, tzinfo=timezone.utc),
     "quarter", 1, "2026-02-28T12:00:00+08:00"),
])
def test_calendar_month_and_year_shifts_clamp_end_of_month(
        monkeypatch, now, unit, count, expected_start):
    monkeypatch.setattr(W, "utc_now", lambda: now)
    resolved = _relative({"unit": unit, "count": count})
    assert resolved["start"] == expected_start


def test_absolute_window_is_left_closed_right_open():
    resolved = _absolute("2026-11-01T00:00:00+08:00",
                         "2026-11-02T00:00:00+08:00")
    assert resolved["startUtc"] == "2026-10-31T16:00:00+00:00"
    assert resolved["endUtc"] == "2026-11-01T16:00:00+00:00"
    assert resolved["exclusiveEnd"] is True


def test_absolute_window_with_offsets_is_normalized_to_declared_timezone():
    resolved = _absolute("2026-10-31T16:00:00Z", "2026-11-01T16:00:00Z")
    assert resolved["start"] == "2026-11-01T00:00:00+08:00"
    assert resolved["end"] == "2026-11-02T00:00:00+08:00"
    assert resolved["startUtc"] == "2026-10-31T16:00:00+00:00"
    assert resolved["endUtc"] == "2026-11-01T16:00:00+00:00"


def test_dst_nonexistent_local_boundary_is_rejected():
    # 2026-03-08 02:00 America/New_York 跳变，02:30 本地时间不存在。
    with pytest.raises(ToolError, match="window_boundary_nonexistent"):
        _absolute("2026-03-08T02:30:00", "2026-03-08T04:00:00",
                  timezone_name="America/New_York")


def test_dst_ambiguous_local_boundary_is_rejected():
    # 2026-11-01 02:00 America/New_York 回拨，01:30 本地时间出现两次。
    with pytest.raises(ToolError, match="window_boundary_ambiguous"):
        _absolute("2026-11-01T01:30:00", "2026-11-01T03:00:00",
                  timezone_name="America/New_York")


def test_explicit_offset_resolves_dst_ambiguity():
    resolved = _absolute("2026-11-01T01:30:00-04:00",
                         "2026-11-01T03:00:00-05:00",
                         timezone_name="America/New_York")
    assert resolved["startUtc"] == "2026-11-01T05:30:00+00:00"
    assert resolved["endUtc"] == "2026-11-01T08:00:00+00:00"
    assert resolved["start"] == "2026-11-01T01:30:00-04:00"
    assert resolved["end"] == "2026-11-01T03:00:00-05:00"


@pytest.mark.parametrize("bad_count", [
    True, False, 1.5, 30.0, "3", "3.5", "thirty", None, [30], {"n": 30},
    0, -1, 1001, 10**6,
])
def test_relative_count_must_be_bounded_integer(bad_count):
    with pytest.raises(ToolError, match="window_relative_invalid"):
        _relative({"unit": "minute", "count": bad_count})


@pytest.mark.parametrize("bad_unit", ["min", "fortnight", "", None])
def test_relative_unit_must_be_supported(bad_unit):
    with pytest.raises(ToolError, match="window_unit_invalid"):
        _relative({"unit": bad_unit, "count": 1})


def test_relative_last_must_be_object():
    with pytest.raises(ToolError, match="window_relative_invalid"):
        _relative("30 minutes")


def test_timezone_is_mandatory_and_shape_is_strict():
    with pytest.raises(ToolError, match="window_timezone_required"):
        W.resolve_window({"property": "t", "last": {"unit": "day", "count": 1}},
                         property_type="datetime")
    with pytest.raises(ToolError, match="window_shape_invalid"):
        W.resolve_window({"property": "t", "timezone": "UTC"},
                         property_type="datetime")
    with pytest.raises(ToolError, match="window_shape_invalid"):
        W.resolve_window({"property": "t", "timezone": "UTC",
                          "last": {"unit": "day", "count": 1},
                          "start": "2026-11-01T00:00:00Z"},
                         property_type="datetime")
    with pytest.raises(ToolError, match="window_unknown_field"):
        W.resolve_window({"property": "t", "timezone": "UTC",
                          "last": {"unit": "day", "count": 1}, "now": "x"},
                         property_type="datetime")


def test_future_end_is_rejected():
    with pytest.raises(ToolError, match="window_future_invalid"):
        W.resolve_window({
            "property": "t", "timezone": "UTC",
            "start": "2026-11-01T00:00:00+00:00",
            "end": "2026-11-10T00:00:00+00:00",
        }, property_type="datetime")


def test_bucket_bounds_use_calendar_arithmetic_not_string_split():
    assert W.bucket_bounds("2026-10-01", "month") == (
        date(2026, 10, 1), date(2026, 11, 1))
    assert W.bucket_bounds("2026-10-01T00:00:00+00:00", "month") == (
        datetime(2026, 10, 1, tzinfo=timezone.utc),
        datetime(2026, 11, 1, tzinfo=timezone.utc),
    )
    # 展示串不能被拆字符推导边界；无法解析即拒绝，交给上游显式标记。
    assert W.bucket_bounds("2026-10", "month") is None
    assert W.bucket_bounds("2026-Q4", "quarter") is None
