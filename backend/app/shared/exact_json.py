"""Exact-decimal JSON serialization for the SQLAlchemy engine serializer.

Number-typed ontology properties are read back from the stored JSON token
(``properties ->> name`` cast to ``NUMERIC``), so the persisted token decides
the aggregate scale. ``json.dumps`` renders floats canonically (``100.5000``
→ ``100.5``); ``dumps_exact`` adds exactly one capability: ``Decimal`` is
rendered as its exact numeric token, never a quoted string.

Every payload without ``Decimal`` is delegated to stdlib ``json.dumps``
unchanged, so separators, key coercion, escaping, NaN/Infinity and nesting
remain byte-identical. Payloads containing ``Decimal`` use the bounded
fallback renderer, which still renders native leaves/keys with ``json.dumps``
and preserves the stdlib key allowlist. Unsupported types and invalid keys
raise ``TypeError``; loading stays plain ``json.loads`` (int/float).
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

_MAX_DEPTH = 500


def _exact(obj: Any, out: list[str], depth: int) -> None:
    if depth > _MAX_DEPTH:
        raise ValueError(f"JSON structure nested deeper than {_MAX_DEPTH} levels")
    if obj is None or obj is True or obj is False:
        out.append(json.dumps(obj))
    elif isinstance(obj, Decimal):
        if not obj.is_finite():
            raise TypeError(
                f"Decimal {obj!r} is not JSON serializable (non-finite)")
        # Exact token: str(Decimal) preserves the source literal and scale
        # ("100.5000", "1E+2" — both valid JSON number grammar).
        out.append(str(obj))
    elif isinstance(obj, (str, int, float)):
        # Delegate every scalar to the stdlib so repr/escaping/NaN stay
        # byte-identical to json.dumps.
        out.append(json.dumps(obj))
    elif isinstance(obj, dict):
        items = []
        for key, value in obj.items():
            if not isinstance(key, str):
                # Same key allowlist as stdlib json: str/int/float/bool/None.
                # Anything else (tuple/list/bytes/Decimal...) must stay a
                # TypeError instead of being silently stringified.
                if key is not None and not isinstance(key, (int, float, bool)):
                    raise TypeError(
                        "keys must be str, int, float, bool or None, "
                        f"not {type(key).__name__}")
                key = json.dumps(key)  # bare token, quoted below
            chunk = [json.dumps(key) + ": "]
            _exact(value, chunk, depth + 1)
            items.append("".join(chunk))
        out.append("{" + ", ".join(items) + "}")
    elif isinstance(obj, (list, tuple)):
        items = []
        for value in obj:
            chunk: list[str] = []
            _exact(value, chunk, depth + 1)
            items.append("".join(chunk))
        out.append("[" + ", ".join(items) + "]")
    else:
        raise TypeError(
            f"Object of type {type(obj).__name__} is not JSON serializable")


def dumps_exact(obj: Any) -> str:
    """Serialize with exact Decimal numeric tokens (json.dumps compatible)."""
    try:
        return json.dumps(obj)
    except TypeError:
        pass
    out: list[str] = []
    _exact(obj, out, 1)
    return "".join(out)


def loads_exact(raw: str | None) -> Any:
    """Decode stored JSON text keeping fractional numeric tokens exact.

    ``json.loads`` without ``parse_float`` decodes every fractional token to
    float, so ``100.5000`` becomes ``100.5`` before the value reaches another
    write boundary. Callers that must preserve a stored token use this loader
    and pass the resulting ``Decimal`` values to the ordinary JSON column;
    integer tokens stay int and strings/booleans/null/containers are unchanged.
    """
    if raw is None:
        return None
    return json.loads(raw, parse_float=Decimal)
