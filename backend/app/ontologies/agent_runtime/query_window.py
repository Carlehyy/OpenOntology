"""服务端相对时间窗口解析 — LLM 只表达语义，平台固定 now 与 [start, end)。

为什么不让模型计算“当前时间”：模型训练截止、时区与设备时钟都不可信；
同一条问题换一台机器必须得到同一绝对窗口。本模块把
``{property, timezone, last: {unit, count}}`` 解析成带显式时区的绝对
``resolvedWindow``，并把窗口翻译成 ``gte start`` / ``lt end`` 两个绑定参数
过滤条件。窗口一旦解析即冻结在 querySpec 中，后续 query-view 重放/下钻
只使用绝对时间，不再二次解释“最近 N 天”。

时间单位分两类：

- duration（second/minute/hour）：用 UTC 瞬时相减，跨夏令时仍得到精确
  的 1 小时 / 30 分钟，不受本地墙钟跳变影响；
- calendar（day/week/month/quarter/year）：按本地日历回退并保留墙钟
  时刻，跨夏令时的“1 天”可能是 23/25 个 UTC 小时，这是日历语义。

时间桶下钻同样使用本模块的日历算术：桶值只作为边界锚点（如 2026-10-01），
绝不把 "2026-10" 之类的展示串按字符拆成月/日。
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.ontologies.agent_runtime.boundary import ToolError

_DATE_TYPES = {"date"}
_UNITS = {
    "second": "second", "seconds": "second",
    "minute": "minute", "minutes": "minute",
    "hour": "hour", "hours": "hour",
    "day": "day", "days": "day",
    "week": "week", "weeks": "week",
    "month": "month", "months": "month",
    "quarter": "quarter", "quarters": "quarter",
    "year": "year", "years": "year",
}
_DURATION_SECONDS = {"second": 1, "minute": 60, "hour": 3600}
_UNIT_TEXT = {
    "second": "秒", "minute": "分钟", "hour": "小时", "day": "天",
    "week": "周", "month": "个月", "quarter": "个季度", "year": "年",
}
_MAX_COUNT = 1000
_MAX_FUTURE_SKEW = timedelta(days=1)


def utc_now() -> datetime:
    """时钟缝：测试固定时钟时 monkeypatch 本函数即可。"""
    return datetime.now(timezone.utc)


def _fail(code: str, message: str) -> ToolError:
    return ToolError(f"{code}: {message}")


def _overflow(label: str, exc: BaseException) -> ToolError:
    return _fail(
        "window_overflow",
        f"{label} 超出可表示的日期/时间范围，请缩小窗口或改用明确 start/end",
    )


def _zone(name) -> tuple[str, ZoneInfo]:
    text = str(name or "").strip()
    if not text:
        raise _fail("window_timezone_required",
                    "窗口必须显式提供 timezone（如 Asia/Shanghai），"
                    "平台不使用服务器本地时区或模型自报的当前时间")
    try:
        return text, ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise _fail("window_timezone_invalid", f"未知时区「{text}」") from exc


def _to_utc(value: datetime, name: str) -> datetime:
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError) as exc:
        raise _overflow(name, exc) from exc


def _to_zone(value: datetime, zone: ZoneInfo, name: str) -> datetime:
    try:
        return value.astimezone(zone)
    except (OverflowError, ValueError) as exc:
        raise _overflow(name, exc) from exc


def _validate_local_wall(aware: datetime, zone: ZoneInfo, name: str) -> datetime:
    """校验无 offset 的本地时间在 zone 中唯一且真实存在。

    DST 回拨的重复时刻有两个 offset；DST 跳变的缺口时刻不存在。两种情况
    都不猜，交由调用方提供带 offset 的 ISO 时间。
    """
    first = aware.replace(fold=0)
    second = aware.replace(fold=1)
    roundtrip = _to_zone(_to_utc(first, name), zone, name)
    if roundtrip.replace(tzinfo=None) != first.replace(tzinfo=None):
        raise _fail(
            "window_boundary_nonexistent",
            f"{name} 本地时间 {first.replace(tzinfo=None).isoformat()} 位于夏令时"
            "跳变缺口，该时间不存在；请提供带 offset 的 ISO 时间或改用显式 "
            "start/end",
        )
    if first.utcoffset() != second.utcoffset():
        raise _fail(
            "window_boundary_ambiguous",
            f"{name} 本地时间 {first.replace(tzinfo=None).isoformat()} 位于夏令时"
            "回拨重复时段，存在两个时区偏移；请提供带 offset 的 ISO 时间",
        )
    return first


def _parse_temporal(value, name: str) -> tuple[datetime, bool]:
    """解析 ISO 日期/时间为 (datetime, 是否仅日期)。

    整数时间戳等非 ISO 形态一律拒绝，不做隐式猜测。
    """
    if isinstance(value, datetime):
        return value, False
    if isinstance(value, date):
        return datetime.combine(value, time.min), True
    if not isinstance(value, str):
        raise _fail("window_boundary_invalid", f"{name} 必须是 ISO-8601 日期或时间")
    text = value.strip()
    if not text:
        raise _fail("window_boundary_invalid", f"{name} 不能为空")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed_date = date.fromisoformat(text)
        except ValueError as exc:
            raise _fail("window_boundary_invalid",
                        f"{name} 必须是 ISO-8601 日期或时间，收到「{value}」") from exc
        return datetime.combine(parsed_date, time.min), True
    date_only = "T" not in text.upper() and " " not in text
    return parsed, date_only


def _parse_boundary(value, zone: ZoneInfo, name: str,
                    *, is_date_property: bool) -> tuple[datetime, bool]:
    parsed, date_only = _parse_temporal(value, name)
    if parsed.tzinfo is None:
        if is_date_property and date_only:
            # date 列只比较日期值，不需要把 00:00 解释成某个瞬时；少数时区
            # 的本地零点落在 DST 缺口，但日期边界本身仍然成立。
            parsed = parsed.replace(tzinfo=zone)
        else:
            parsed = _validate_local_wall(
                parsed.replace(tzinfo=zone), zone, name)
    else:
        parsed = _to_zone(parsed, zone, name)
    return parsed, date_only


def _add_months(anchor: datetime, months: int) -> datetime:
    total = anchor.year * 12 + (anchor.month - 1) + months
    year, month_index = divmod(total, 12)
    month = month_index + 1
    day = min(anchor.day, monthrange(year, month)[1])
    return anchor.replace(year=year, month=month, day=day)


def _shift(anchor: datetime, unit: str, count: int) -> datetime:
    try:
        if unit == "day":
            return anchor + timedelta(days=-count)
        if unit == "week":
            return anchor + timedelta(days=-7 * count)
        if unit == "month":
            return _add_months(anchor, -count)
        if unit == "quarter":
            return _add_months(anchor, -3 * count)
        if unit == "year":
            return _add_months(anchor, -12 * count)
    except (OverflowError, ValueError) as exc:
        raise _overflow(f"last.count={count} {unit}", exc) from exc
    raise _fail("window_unit_invalid", f"不支持的时间单位「{unit}」")


def _relative_count(raw: dict) -> int:
    value = raw.get("count")
    if type(value) is not int:
        raise _fail("window_relative_invalid",
                    "last.count 必须是整数（不接受布尔/浮点/字符串）")
    count = value
    if count < 1 or count > _MAX_COUNT:
        raise _fail("window_relative_invalid",
                    f"last.count 需在 1..{_MAX_COUNT}，收到 {count}")
    return count


def _relative_span(raw, zone: ZoneInfo, now_utc: datetime, now_local: datetime,
                   *, validate_calendar: bool
                   ) -> tuple[datetime, datetime, dict]:
    if not isinstance(raw, dict):
        raise _fail("window_relative_invalid", "last 必须是 {unit, count}")
    unit = _UNITS.get(str(raw.get("unit") or "").strip().lower())
    if unit is None:
        raise _fail("window_unit_invalid",
                    "last.unit 需为 second/minute/hour/day/week/month/quarter/year"
                    "（可复数）")
    count = _relative_count(raw)
    if unit in _DURATION_SECONDS:
        # duration 按 UTC 瞬时相减：跨 DST 回拨/跳变仍是精确 N 小时/分钟。
        try:
            start_utc = now_utc - timedelta(
                seconds=_DURATION_SECONDS[unit] * count)
        except (OverflowError, ValueError) as exc:
            raise _overflow(f"last.count={count} {unit}", exc) from exc
        return (_to_zone(start_utc, zone, "start"), now_local,
                {"unit": unit, "count": count, "elapsed": True})
    # calendar 单位保留本地墙钟：1 天跨 DST 可能是 23/25 个 UTC 小时。
    start = _shift(now_local, unit, count)
    if validate_calendar:
        # datetime 边界若落在 DST 缺口/重复时段，不猜，要求显式 start/end。
        _validate_local_wall(start, zone, "start")
    return (start, now_local,
            {"unit": unit, "count": count, "elapsed": False})


def _absolute_span(window: dict, zone: ZoneInfo,
                   *, is_date_property: bool
                   ) -> tuple[datetime, datetime, bool, bool]:
    if "start" not in window or "end" not in window:
        raise _fail("window_boundary_required",
                    "绝对窗口必须同时提供 start 与 end（[start, end) 左闭右开）")
    start, start_date_only = _parse_boundary(
        window.get("start"), zone, "start", is_date_property=is_date_property)
    end, end_date_only = _parse_boundary(
        window.get("end"), zone, "end", is_date_property=is_date_property)
    return start, end, start_date_only, end_date_only


def _future_limit(anchor: datetime) -> datetime:
    try:
        return anchor + _MAX_FUTURE_SKEW
    except OverflowError:
        return datetime.max.replace(tzinfo=anchor.tzinfo)


def _date_end_exclusive(bound: datetime) -> date:
    """date 列的上界：恰在本地零点时停在当日，否则才进位到次日。

    显式 [start, end) 的 end 是排他边界，不得无条件 +1 天；只有 end 带
    非零时刻、date 精度无法表达时才按自然日进位。
    """
    if bound.time() == time.min:
        return bound.date()
    try:
        return bound.date() + timedelta(days=1)
    except OverflowError as exc:
        raise _fail("window_overflow",
                    "日期窗口 end 超出可表示的日历范围") from exc


def _relative_text(info: dict) -> str:
    semantics = "UTC 精确时长" if info["elapsed"] else "本地日历回退"
    unit = info["unit"]
    count = info["count"]
    return (f"最近 {count} {_UNIT_TEXT[unit]}"
            f"（last.unit={unit}, count={count}，按{semantics}）")


def resolve_window(window: dict, *, property_name: str | None = None,
                   property_type: str | None = None) -> dict:
    """解析窗口为冻结的 absolute 口径。

    ``property_type`` 为发布声明类型时，日期属性按本地自然日边界表达：
    relative 窗口把日期值扩到覆盖整个自然日；显式 [start, end) 的 end
    保持排他日期（仅当 end 带非零时刻时按自然日进位），不再无条件
    end+1 day。date 列不会被 ISO datetime 字符串误伤。
    """
    if not isinstance(window, dict):
        raise _fail("window_invalid", "window 必须是对象")
    unknown = sorted(set(window) - {"property", "timezone", "last", "start", "end"})
    if unknown:
        raise _fail("window_unknown_field", f"window 含未知字段：{', '.join(unknown)}")
    prop = str(window.get("property") or property_name or "").strip()
    if not prop:
        raise _fail("window_property_required", "window 必须提供时间属性 property")
    timezone_name, zone = _zone(window.get("timezone"))
    clock = utc_now()
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    now_utc = _to_utc(clock, "now")
    now_local = _to_zone(now_utc, zone, "now")

    has_relative = "last" in window
    has_absolute = "start" in window or "end" in window
    if has_relative and has_absolute:
        raise _fail("window_shape_invalid", "window 不能同时提供 last 与 start/end")
    if not has_relative and not has_absolute:
        raise _fail("window_shape_invalid", "window 需要 last 或 start/end")

    declared = str(property_type or "").lower()
    is_date = declared in _DATE_TYPES
    relative_info: dict | None = None
    if has_relative:
        start, end, relative_info = _relative_span(
            window.get("last"), zone, now_utc, now_local,
            validate_calendar=not is_date)
        expression_kind = "relative"
    else:
        start, end, start_date_only, end_date_only = _absolute_span(
            window, zone, is_date_property=is_date)
        expression_kind = "absolute"

    # 统一用 UTC 瞬时判序/判未来：同一 tzinfo 的 aware datetime 比较看墙钟，
    # DST 回拨时“01:30-04:00”与“01:30-05:00”墙钟相同但瞬时不同。
    start_utc = _to_utc(start, "start")
    end_utc = _to_utc(end, "end")
    if start_utc >= end_utc:
        raise _fail("window_boundary_invalid", "窗口 start 必须早于 end")
    if end_utc > _future_limit(now_utc):
        raise _fail("window_future_invalid", "窗口 end 不能晚于服务端当前时间")

    resolved = {
        "property": prop,
        "propertyType": declared or None,
        "timezone": timezone_name,
        "kind": expression_kind,
        "now": now_local.isoformat(),
        "exclusiveEnd": True,
    }

    if is_date:
        start_date = start.date()
        end_date = _date_end_exclusive(end)
        if start_date >= end_date:
            raise _fail("window_boundary_invalid",
                        "日期窗口 start 必须早于排他 end 日期")
        start_label = datetime.combine(start_date, time.min, tzinfo=zone)
        end_label = datetime.combine(end_date, time.min, tzinfo=zone)
        start_value = start_date.isoformat()
        end_value = end_date.isoformat()
        resolved.update({
            "start": start_label.isoformat(),
            "end": end_label.isoformat(),
            "startUtc": _to_utc(start_label, "start").isoformat(),
            "endUtc": _to_utc(end_label, "end").isoformat(),
            "startValue": start_value,
            "endValue": end_value,
            "expression": f"{prop} >= {start_value} AND {prop} < {end_value}",
        })
        if relative_info is not None:
            covered_last = (end_date - timedelta(days=1)).isoformat()
            resolved["display"] = (
                f"日期属性 {prop} 按自然日筛选 {start_value} ≤ 值 < {end_value}"
                f"（{timezone_name}；{_relative_text(relative_info)}；"
                f"date 精度只能按天覆盖 {start_value} 至 {covered_last} 全天；"
                f"服务端 now={now_local.isoformat()}；左闭右开）"
            )
        else:
            rounded = (not start_date_only or not end_date_only
                       or start.time() != time.min)
            note = "；输入边界含时间，已按自然日取整" if rounded else ""
            resolved["display"] = (
                f"日期属性 {prop} 按显式日期边界筛选 {start_value} ≤ 值 < {end_value}"
                f"（{timezone_name}；end 为排他日期、不自动延展{note}；"
                f"服务端 now={now_local.isoformat()}；左闭右开）"
            )
        return resolved

    start_value = start_utc.isoformat()
    end_value = end_utc.isoformat()
    relative_note = (_relative_text(relative_info)
                     if relative_info is not None else "显式 start/end 窗口")
    resolved.update({
        "start": start.isoformat(),
        "end": end.isoformat(),
        "startUtc": start_value,
        "endUtc": end_value,
        "startValue": start_value,
        "endValue": end_value,
        "expression": f"{prop} >= {start_value} AND {prop} < {end_value}",
        "display": (
            f"{prop} 过滤 [{start.isoformat()}, {end.isoformat()})"
            f"（{timezone_name}；{relative_note}；"
            f"服务端 now={now_local.isoformat()}；左闭右开）"
        ),
    })
    return resolved


def window_filters(resolved: dict, *, source: str) -> list[dict]:
    """把 resolvedWindow 翻译为团队查询过滤条件（AND）。"""
    if not resolved:
        return []
    return [
        {"source": source, "property": resolved["property"],
         "op": "gte", "value": resolved["startValue"]},
        {"source": source, "property": resolved["property"],
         "op": "lt", "value": resolved["endValue"]},
    ]


def window_for_result(resolved: dict | None) -> dict | None:
    if not resolved:
        return None
    return {
        key: resolved.get(key)
        for key in ("property", "propertyType", "timezone", "kind", "now",
                    "start", "end", "startUtc", "endUtc", "startValue",
                    "endValue", "exclusiveEnd", "expression", "display")
        if key in resolved
    }


_BUCKET_PARSE_LIMIT = 64


def bucket_bounds(value, bucket: str) -> tuple[object, object] | None:
    """时间桶值 → [start, end) 边界；无法解析返回 None（不猜测）。

    只把桶值当时间锚点解析一次，再用日历算术得到边界；不支持把
    "2026-10" / "2026-Q4" 之类的展示串拆字符推导边界。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or len(text) > _BUCKET_PARSE_LIMIT:
        return None
    parsed: datetime | date | None = None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        is_date_only = len(text) == 10
    except ValueError:
        try:
            parsed = date.fromisoformat(text)
            is_date_only = True
        except ValueError:
            return None
    try:
        if isinstance(parsed, datetime):
            anchor = parsed
            if bucket == "day":
                start, end = anchor, anchor + timedelta(days=1)
            elif bucket == "week":
                start = anchor - timedelta(days=anchor.weekday())
                end = start + timedelta(days=7)
            elif bucket == "month":
                start = anchor.replace(day=1)
                end = _add_months(start, 1)
            elif bucket == "quarter":
                start = anchor.replace(month=((anchor.month - 1) // 3) * 3 + 1, day=1)
                end = _add_months(start, 3)
            elif bucket == "year":
                start = anchor.replace(month=1, day=1)
                end = _add_months(start, 12)
            else:
                return None
            if is_date_only:
                return start.date(), end.date()
            return start, end
        # date 对象
        if bucket == "day":
            start, end = parsed, parsed + timedelta(days=1)
        elif bucket == "week":
            start = parsed - timedelta(days=parsed.weekday())
            end = start + timedelta(days=7)
        elif bucket == "month":
            start, end = parsed.replace(day=1), _add_months(
                datetime.combine(parsed.replace(day=1), time.min), 1).date()
        elif bucket == "quarter":
            start = parsed.replace(month=((parsed.month - 1) // 3) * 3 + 1, day=1)
            end = _add_months(datetime.combine(start, time.min), 3).date()
        elif bucket == "year":
            start, end = parsed.replace(month=1, day=1), parsed.replace(
                year=parsed.year + 1, month=1, day=1)
        else:
            return None
        return start, end
    except (OverflowError, ValueError):
        # 桶边界超出可表示范围时同样返回 None：展示层不猜。
        return None
