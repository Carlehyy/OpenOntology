"""Automatic HTTP publication inference and safe structured body merging."""
from __future__ import annotations

import copy
import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit


_SENSITIVE_NAME_RE = re.compile(
    r"(authorization|authentication|auth(?:[-_]?(?:code|key|token))?(?:$|[-_])|"
    r"cookie|credential|token|secret|password|passwd|pwd|"
    r"api[-_]?key|api-hub-key|private[-_]?key|"
    r"session|signature|bearer|jwt)",
    re.IGNORECASE,
)
_MANAGED_HEADER_NAMES = {
    "accept",
    "accept-encoding",
    "authorization",
    "connection",
    "content-length",
    "content-type",
    "cookie",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "user-agent",
}


class PublicationBodyError(ValueError):
    """Raised when a caller body does not match the generated publication contract."""


def is_sensitive_name(name: str) -> bool:
    return bool(_SENSITIVE_NAME_RE.search(name or ""))


def slug_suggestion(interface: dict) -> str:
    """Return a stable ASCII slug without asking the administrator to invent one."""
    parsed = urlsplit(interface.get("url") or "")
    url_tail = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    source = url_tail or interface.get("name") or "interface"
    slug = re.sub(r"[^a-z0-9_-]+", "-", source.lower()).strip("-_")
    iid = int(interface.get("id") or 0)
    return (slug[:64] if slug else f"interface-{iid or 'new'}")


def infer_query_keys(interface: dict) -> list[str]:
    return _unique_safe_keys(
        item.get("key", "")
        for item in interface.get("query_params") or []
        if isinstance(item, dict)
    )


def infer_header_keys(interface: dict, proxy_key_header: str) -> list[str]:
    blocked = _MANAGED_HEADER_NAMES | {proxy_key_header.lower()}
    return _unique_safe_keys(
        (
            item.get("key", "")
            for item in interface.get("headers") or []
            if isinstance(item, dict)
            and item.get("key", "").strip().lower() not in blocked
        ),
        lower=True,
    )


def infer_body_keys(interface: dict) -> list[str]:
    body_type = (interface.get("body_type") or "none").lower()
    body = interface.get("body_content") or ""
    if body_type == "json":
        try:
            value = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            return []
        return _json_leaf_paths(value) if isinstance(value, dict) else []
    if body_type == "form":
        return _unique_safe_keys(key for key, _ in parse_saved_form(body))
    if body_type == "multipart":
        return _unique_safe_keys(
            [key for key, _ in parse_saved_form(body)]
            + [
                item.get("key", "")
                for item in interface.get("file_fields") or []
                if isinstance(item, dict)
            ]
        )
    # Raw request bodies cannot be safely decomposed. Keep the saved body fixed.
    return []


def merge_caller_body(
    interface: dict,
    incoming: bytes,
) -> bytes:
    """Merge caller-editable fields into the platform-owned body template."""
    allowed = list(interface.get("proxy_body_keys") or [])
    if not allowed:
        # Compatibility for publications created before field-level contracts existed.
        return incoming

    body_type = (interface.get("body_type") or "none").lower()
    if body_type == "json":
        return _merge_json_body(interface.get("body_content") or "", incoming, allowed)
    if body_type == "form":
        return _merge_form_body(interface.get("body_content") or "", incoming, allowed)
    raise PublicationBodyError("该接口的请求体不能安全地开放给调用方修改")


def body_template(interface: dict) -> str:
    """Return a caller-safe partial body containing only inferred editable fields."""
    allowed = list(interface.get("proxy_body_keys") or [])
    if not allowed:
        return ""
    body_type = (interface.get("body_type") or "none").lower()
    body = interface.get("body_content") or ""
    if body_type == "json":
        try:
            source = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            return ""
        if not isinstance(source, dict):
            return ""
        template: dict[str, Any] = {}
        for path in allowed:
            try:
                _set_pointer(template, path, copy.deepcopy(_get_pointer(source, path)))
            except (KeyError, TypeError, ValueError):
                continue
        return json.dumps(template, ensure_ascii=False, indent=2)
    if body_type == "form":
        defaults = parse_saved_form(body)
        allowed_set = set(allowed)
        return urlencode([(key, value) for key, value in defaults if key in allowed_set])
    return ""


def multipart_text_fields(interface: dict) -> list[dict[str, str]]:
    if (interface.get("body_type") or "none").lower() != "multipart":
        return []
    allowed = set(interface.get("proxy_body_keys") or [])
    return [
        {"key": key, "value": value}
        for key, value in parse_saved_form(interface.get("body_content") or "")
        if not allowed or key in allowed
    ]


def multipart_file_fields(interface: dict) -> list[dict]:
    if (interface.get("body_type") or "none").lower() != "multipart":
        return []
    allowed = set(interface.get("proxy_body_keys") or [])
    return [
        item
        for item in interface.get("file_fields") or []
        if isinstance(item, dict)
        and item.get("key")
        and (not allowed or item.get("key") in allowed)
    ]


def _unique_safe_keys(values, *, lower: bool = False) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = str(value or "").strip()
        marker = key.lower() if lower else key
        if not key or marker in seen or is_sensitive_name(key):
            continue
        seen.add(marker)
        result.append(key)
    return result


def parse_saved_form(body: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for line in (body or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            result.append((key, value.strip()))
    return result


def _escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _unescape_pointer(value: str) -> str:
    return value.replace("~1", "/").replace("~0", "~")


def _json_leaf_paths(value: dict, prefix: str = "") -> list[str]:
    result: list[str] = []
    for raw_key, item in value.items():
        key = str(raw_key)
        if is_sensitive_name(key):
            continue
        path = f"{prefix}/{_escape_pointer(key)}"
        if isinstance(item, dict) and item:
            result.extend(_json_leaf_paths(item, path))
        else:
            # Lists are treated as one editable value; their internal shape stays opaque.
            result.append(path)
    return result


def _incoming_json_paths(value: dict, prefix: str = "") -> list[str]:
    result: list[str] = []
    for raw_key, item in value.items():
        key = str(raw_key)
        path = f"{prefix}/{_escape_pointer(key)}"
        if isinstance(item, dict) and item:
            result.extend(_incoming_json_paths(item, path))
        else:
            result.append(path)
    return result


def _pointer_parts(path: str) -> list[str]:
    if not path.startswith("/"):
        raise ValueError("JSON 字段路径必须以 / 开头")
    return [_unescape_pointer(part) for part in path[1:].split("/") if part != ""]


def _get_pointer(value: dict, path: str) -> Any:
    current: Any = value
    for part in _pointer_parts(path):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(path)
        current = current[part]
    return current


def _set_pointer(value: dict, path: str, item: Any) -> None:
    parts = _pointer_parts(path)
    if not parts:
        raise ValueError("JSON 字段路径不能为空")
    current = value
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = item


def _merge_json_body(default_body: str, incoming: bytes, allowed: list[str]) -> bytes:
    try:
        default_value = json.loads(default_body)
        incoming_value = json.loads(incoming.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        raise PublicationBodyError("请求 Body 必须是有效的 JSON") from exc
    if not isinstance(default_value, dict) or not isinstance(incoming_value, dict):
        raise PublicationBodyError("请求 Body 必须是 JSON 对象")

    incoming_paths = _incoming_json_paths(incoming_value)
    denied = sorted(set(incoming_paths) - set(allowed))
    if denied:
        raise PublicationBodyError("以下 Body 字段未开放：" + ", ".join(denied))

    merged = copy.deepcopy(default_value)
    for path in incoming_paths:
        _set_pointer(merged, path, copy.deepcopy(_get_pointer(incoming_value, path)))
    return json.dumps(merged, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _merge_form_body(default_body: str, incoming: bytes, allowed: list[str]) -> bytes:
    try:
        incoming_pairs = parse_qsl(
            incoming.decode("utf-8"), keep_blank_values=True, strict_parsing=False
        )
    except UnicodeDecodeError as exc:
        raise PublicationBodyError("请求 Body 必须是 UTF-8 表单数据") from exc
    denied = sorted({key for key, _ in incoming_pairs if key not in set(allowed)})
    if denied:
        raise PublicationBodyError("以下 Body 字段未开放：" + ", ".join(denied))
    replaced = {key for key, _ in incoming_pairs}
    merged = [item for item in parse_saved_form(default_body) if item[0] not in replaced]
    merged.extend(incoming_pairs)
    return urlencode(merged, doseq=True).encode("utf-8")

