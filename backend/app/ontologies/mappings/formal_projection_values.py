"""Pure, fail-closed value coercion for both Trial and live Formal mappings."""
from __future__ import annotations

import json

from app.shared.numeric_utils import parse_numeric_text
from app.shared.time_utils import parse_temporal_text


def _display_value(value, limit: int = 100) -> str:
    """报错用值展示：截断超长载荷，错误消息不被对抗性巨型输入放大。"""
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…(截断)"


def _coerce_props_to_type(props: dict, type_props: list[dict]) -> dict:
    """按类型的属性定义做值转换（CSV 来的全是字符串——number 属性不转数字，
    哨兵的数值条件、派生函数、动作校验会整体失灵）。声明过的类型转换失败
    必须阻止投影，不能把错误字符串悄悄塞进已发布本体。

    CSV/Excel 解析器会把空单元格表示成空字符串。对 number/boolean/date
    等非字符串属性，这表示业务空值而不是一个类型为 string 的值，必须在
    试跑和正式投影共用的这一层归一为 None。array/object 则只接受对应的
    原生容器或严格 JSON 文本。必填约束仍由实例契约校验拦截；string 属性
    保留原值。

    number 先走 int、整值 Decimal 再走 float：数据库 numeric 列归一化后
    产出 "9007199254740993.00" 这类带小数尾缀的整值文本，int() 解析不了
    而 float64 会静默变值，所以整值判定用 Decimal（任意精度）。超出
    float64 量级（1e308）的极端值不在此路，照旧落 float 由实例契约拦截。
    date 按"值自带偏移的墙面日期"截断（不折 UTC，跨时区混源的同一 date
    属性可能差一天——业务日语义下正确）；datetime 统一 ISO "T" 分隔，
    输出保证能通过实例契约的 fromisoformat 校验。"""
    kind_by_name = {p.get("name"): (p.get("type") or "string")
                    for p in (type_props or []) if isinstance(p, dict)}
    structured_types = {"array": list, "object": dict}
    out = dict(props)
    for k, v in props.items():
        t = kind_by_name.get(k)
        if v is None:
            continue
        expected_native_type = structured_types.get(t)
        if t in ("object", "array", "number", "integer", "float"):
            from app.shared.value_types import JsonText
            if isinstance(v, JsonText):
                raise ValueError(
                    f"属性 {k} 的原生 raw JSON 请映射 string；结构化/数值语义未支持，已拒绝有损转换")
        if expected_native_type is not None and not isinstance(v, str):
            if not isinstance(v, expected_native_type):
                raise ValueError(f"属性 {k} 的值 {v!r} 无法转换为 {t}")
            continue
        if not isinstance(v, str):
            continue
        s = v.strip()
        if s == "" and t not in (None, "string"):
            out[k] = None
            continue
        try:
            if t in ("number", "integer", "float"):
                out[k] = parse_numeric_text(s)
            elif t == "boolean":
                low = s.lower()
                if low in ("true", "1", "yes", "是"):
                    out[k] = True
                elif low in ("false", "0", "no", "否"):
                    out[k] = False
                elif s:
                    raise ValueError(
                        f"属性 {k} 的值 {_display_value(v)!r} 无法转换为 boolean")
            elif t in ("date", "datetime"):
                parsed = parse_temporal_text(s)
                out[k] = (parsed.date().isoformat() if t == "date"
                          else parsed.isoformat())
            elif expected_native_type is not None:
                decoded = json.loads(s)
                if decoded is None:
                    out[k] = None
                elif isinstance(decoded, expected_native_type):
                    out[k] = decoded
                else:
                    raise ValueError(
                        f"属性 {k} 的值 {_display_value(v)!r} 无法转换为 {t}")
        except (ValueError, TypeError, ArithmeticError, RecursionError) as exc:
            # ArithmeticError/RecursionError：巨型 Decimal/深嵌套 JSON 等对抗
            # 载荷会从解析器逃出这两族异常——必须翻译成带属性名的业务报错，
            # 不能以未分类异常击穿整批投影
            raise ValueError(
                f"属性 {k} 的值 {_display_value(v)!r} 无法转换为 {t}"
                f"（{t} 需要 ISO 格式，如 2026-01-15 或 2026-01-15T10:30:00）"
                if t in ("date", "datetime") else
                f"属性 {k} 的值 {_display_value(v)!r} 无法转换为 {t}"
            ) from exc
    return out
