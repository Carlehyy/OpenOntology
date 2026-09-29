"""Safe API-Hub operations shared by the data-steward Agent.

The UI and system MCP remain the full administrative surfaces.  This adapter
adds the stricter contract needed for an LLM: partial updates, optimistic
revision checks, redacted reads, dynamic-parameter allowlists and bounded call
results.  It deliberately cannot publish an interface or manage caller keys.
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import HTTPException
from pydantic import ValidationError

from . import db, executor
from .interface_contracts import (
    InterfaceIn,
    InterfaceParameter,
    KV,
)
from .interface_service import (
    _row_to_dict,
    create_interface,
    update_interface,
)
from .publication import is_sensitive_name

# 数据管家的「写入拒绝」名单：有意比展示脱敏口径（publication.is_sensitive_name）
# 窄——管家可以代写哪些字段由本名单决定，加宽需产品明确要求，不随展示口径
# 联动（二次审查 8.3）。展示/脱敏路径（_redacted_url/_redact_json_value/
# _agent_view）才使用 is_sensitive_name。
_SENSITIVE_NAME = re.compile(
    r"(?:authorization|cookie|token|secret|password|passwd|api[-_]?key|session)",
    re.IGNORECASE,
)

_CALL_BODY_LIMIT = 20_000


class AgentInterfaceError(ValueError):
    pass


def _error_text(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    if isinstance(exc, ValidationError):
        errors = exc.errors()
        return str(errors[0].get("msg") if errors else exc)
    return str(exc)


def _pairs(value: Any, *, field: str) -> list[dict[str, str]]:
    if value is None:
        return []
    if isinstance(value, dict):
        iterable = value.items()
    elif isinstance(value, list):
        iterable = []
        for item in value:
            if not isinstance(item, dict) or "key" not in item:
                raise AgentInterfaceError(f"{field} 必须是键值对象或 {{key,value}} 数组")
            iterable.append((item.get("key"), item.get("value", "")))
    else:
        raise AgentInterfaceError(f"{field} 必须是键值对象或数组")
    result = []
    for key, raw_value in iterable:
        name = str(key or "").strip()
        if not name:
            continue
        result.append({"key": name, "value": str(raw_value if raw_value is not None else "")})
    return result


def _merge_pairs(
    current: list[dict], patch: Any, removals: list[str] | None, *, field: str,
    case_insensitive: bool = False,
) -> list[dict[str, str]]:
    marker = (lambda value: value.lower()) if case_insensitive else (lambda value: value)
    removed = {marker(str(item).strip()) for item in removals or [] if str(item).strip()}
    incoming = _pairs(patch, field=field)
    replacements = {marker(item["key"]): item for item in incoming}
    output = []
    for item in current or []:
        key = str(item.get("key") or "").strip()
        key_marker = marker(key)
        if not key or key_marker in removed or key_marker in replacements:
            continue
        output.append({"key": key, "value": str(item.get("value") or "")})
    output.extend(replacements.values())
    return output


def _reject_agent_secrets(items: list[dict], *, field: str) -> None:
    blocked = [item["key"] for item in items if _SENSITIVE_NAME.search(item["key"])]
    if blocked:
        raise AgentInterfaceError(
            f"{field} 含敏感字段（{', '.join(blocked)}），数据管家不能接收或回写密钥。"
            "请在接口管理界面安全配置，或使用浏览器捕获登记。"
        )


def _reject_parameter_secrets(parameters: list[dict] | None) -> None:
    blocked = [
        f"{item.get('location')}.{item.get('name')}"
        for item in parameters or []
        if item.get("sensitive") and item.get("default") not in (None, "")
    ]
    if blocked:
        raise AgentInterfaceError(
            "数据管家不能接收敏感参数默认值：" + ", ".join(blocked)
        )


def _reject_url_secrets(value: str) -> None:
    blocked = [
        key for key, _value in parse_qsl(urlsplit(value or "").query, keep_blank_values=True)
        if _SENSITIVE_NAME.search(key)
    ]
    if blocked:
        raise AgentInterfaceError(
            "URL 查询串含敏感字段（" + ", ".join(blocked) +
            "），请在接口管理界面安全配置。"
        )


def _sensitive_object_paths(value: Any, prefix: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key)
            path = f"{prefix}.{key}" if prefix else key
            if _SENSITIVE_NAME.search(key):
                paths.append(path)
            else:
                paths.extend(_sensitive_object_paths(child, path))
    elif isinstance(value, list):
        for item in value:
            paths.extend(_sensitive_object_paths(item, prefix))
    return paths


def _body_sensitive_fields(body_type: str, content: str) -> list[str]:
    content = str(content or "")
    if not content:
        return []
    kind = str(body_type or "none").lower()
    if kind == "json":
        try:
            return _sensitive_object_paths(json.loads(content))
        except ValueError:
            pass
    if kind in {"form", "multipart"}:
        pairs = parse_qsl(content, keep_blank_values=True)
        pairs.extend(
            tuple(line.split("=", 1))
            for line in content.splitlines()
            if "=" in line and not line.lstrip().startswith("#")
        )
        return sorted({key.strip() for key, _value in pairs if _SENSITIVE_NAME.search(key.strip())})
    if re.search(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", content):
        return ["raw Bearer token"]
    return sorted(set(re.findall(
        r"(?i)[\"']?([A-Za-z0-9_-]*(?:token|secret|password|passwd|api[-_]?key)[A-Za-z0-9_-]*)[\"']?\s*[:=]",
        content,
    )))


def _reject_body_secrets(body_type: str, content: str) -> None:
    blocked = _body_sensitive_fields(body_type, content)
    if blocked:
        raise AgentInterfaceError(
            "请求 Body 含敏感字段（" + ", ".join(blocked) +
            "），请在接口管理界面安全配置。"
        )


def _redacted_url(value: str) -> str:
    parsed = urlsplit(value or "")
    query = [
        (key, "***" if is_sensitive_name(key) else item_value)
        for key, item_value in parse_qsl(parsed.query, keep_blank_values=True)
    ]
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query, doseq=True), parsed.fragment)
    )


def _redact_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "***" if is_sensitive_name(str(key)) else _redact_json_value(child)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_json_value(item) for item in value]
    return value


def _redact_response_body(value: str, content_type: str) -> str:
    text = str(value or "")
    if "json" in str(content_type or "").lower() or text.lstrip().startswith(("{", "[")):
        try:
            return json.dumps(_redact_json_value(json.loads(text)), ensure_ascii=False)
        except ValueError:
            pass
    return re.sub(
        r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+", r"\1***", text,
    )


def _load(iid: int) -> dict:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM interfaces WHERE id = ?", (iid,)).fetchone()
    if row is None:
        raise AgentInterfaceError(f"接口 {iid} 不存在")
    return _row_to_dict(row)


def _record_actor(iid: int, actor_user_id: str | None, *, created: bool = False) -> dict:
    actor = str(actor_user_id or "")
    with db.get_conn() as conn:
        if created:
            conn.execute(
                "UPDATE interfaces SET created_by=?, updated_by=? WHERE id=?",
                (actor, actor, iid),
            )
        else:
            conn.execute("UPDATE interfaces SET updated_by=? WHERE id=?", (actor, iid))
    return _load(iid)


def _agent_view(interface: dict, *, detail: bool = True) -> dict:
    headers = []
    for item in interface.get("headers") or []:
        key = str(item.get("key") or "")
        sensitive = bool(is_sensitive_name(key))
        headers.append(
            {
                "key": key,
                "value": None if sensitive else item.get("value", ""),
                "sensitive": sensitive,
                "configured": bool(item.get("value")),
            }
        )
    query = []
    for item in interface.get("query_params") or []:
        key = str(item.get("key") or "")
        sensitive = bool(is_sensitive_name(key))
        query.append(
            {
                "key": key,
                "value": None if sensitive else item.get("value", ""),
                "sensitive": sensitive,
                "configured": bool(item.get("value")),
            }
        )
    parameter_schema = []
    sensitive_body = bool(_body_sensitive_fields(
        interface.get("body_type") or "none", interface.get("body_content") or "",
    ))
    for item in interface.get("parameter_schema") or []:
        safe = dict(item)
        if safe.get("sensitive"):
            safe["configured"] = safe.get("default") not in (None, "")
            safe["default"] = None
            sensitive_body = sensitive_body or safe.get("location") == "body"
        parameter_schema.append(safe)
    base = {
        "id": interface["id"],
        "name": interface["name"],
        "description": interface.get("description") or "",
        "group": interface.get("group_name") or "默认分组",
        "method": interface["method"],
        "url": _redacted_url(interface["url"]),
        "bodyType": interface.get("body_type") or "none",
        "configRevision": int(interface.get("config_revision") or 1),
        "parameterSchema": parameter_schema,
        "exposure": {
            "httpPublished": bool(interface.get("http_enabled")),
        },
        "updatedAt": interface.get("updated_at"),
    }
    if detail:
        base.update(
            {
                "queryParams": query,
                "headers": headers,
                "bodyContent": None if sensitive_body else (interface.get("body_content") or ""),
                "bodyConfigured": bool(interface.get("body_content")),
                "fileFields": interface.get("file_fields") or [],
                "securityNotice": "敏感 Header/Query 值已隐藏；更新未提及它们时会保留原值。",
            }
        )
    return base


def list_interfaces_for_agent(
    *, keyword: str | None = None, group: str | None = None
) -> dict:
    keyword = (keyword or "").strip().lower()
    group = (group or "").strip()
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM interfaces ORDER BY group_name, sort_order, id"
        ).fetchall()
    items = []
    for row in rows:
        interface = _row_to_dict(row)
        if group and (interface.get("group_name") or "默认分组") != group:
            continue
        haystack = " ".join(
            str(interface.get(key) or "")
            for key in ("name", "description", "group_name", "method", "url")
        ).lower()
        if keyword and keyword not in haystack:
            continue
        items.append(_agent_view(interface, detail=False))
    return {"count": len(items), "interfaces": items}


def get_interface_for_agent(iid: int) -> dict:
    return {"interface": _agent_view(_load(int(iid)), detail=True)}


def create_interface_for_agent(
    *, actor_user_id: str | None, name: str, url: str, method: str = "GET",
    group: str = "", description: str = "", query_params: Any = None,
    headers: Any = None, body_type: str = "none", body_content: str = "",
    parameters: list[dict] | None = None,
) -> dict:
    query = _pairs(query_params, field="query_params")
    header_pairs = _pairs(headers, field="headers")
    _reject_agent_secrets(query, field="query_params")
    _reject_agent_secrets(header_pairs, field="headers")
    _reject_parameter_secrets(parameters)
    _reject_url_secrets(url)
    _reject_body_secrets(body_type, body_content)
    try:
        body = InterfaceIn(
            name=name,
            url=url,
            method=method,
            group_name=group,
            description=description,
            query_params=[KV(**item) for item in query],
            headers=[KV(**item) for item in header_pairs],
            body_type=body_type,
            body_content=body_content,
            parameter_schema=[InterfaceParameter(**item) for item in parameters or []],
            # Agent-created interfaces are internal drafts. Exposure remains a
            # separate human-governed action in API Hub.
            mcp_enabled=False,
            open_enabled=False,
            http_enabled=False,
        )
        created = create_interface(body)
    except (HTTPException, ValidationError, ValueError) as exc:
        raise AgentInterfaceError(_error_text(exc)) from exc
    created = _record_actor(int(created["id"]), actor_user_id, created=True)
    return {
        "interface": _agent_view(created, detail=True),
        "notice": "接口已保存为内部草稿，未自动加入开放 MCP 清单，也未发布 HTTP 地址。",
    }


def update_interface_for_agent(
    *, iid: int, actor_user_id: str | None, expected_revision: int,
    changes: dict[str, Any],
) -> dict:
    current = _load(int(iid))
    revision = int(current.get("config_revision") or 1)
    if int(expected_revision) != revision:
        raise AgentInterfaceError(
            f"接口配置已被其他操作更新：当前 revision={revision}，"
            f"本次基于 revision={expected_revision}。请重新读取后再修改。"
        )
    merged = dict(current)
    field_map = {
        "name": "name",
        "url": "url",
        "method": "method",
        "group": "group_name",
        "description": "description",
        "body_type": "body_type",
        "body_content": "body_content",
    }
    for source, target in field_map.items():
        if source in changes:
            merged[target] = changes[source]
    if "url" in changes:
        _reject_url_secrets(str(changes.get("url") or ""))
    if "body_content" in changes or "body_type" in changes:
        _reject_body_secrets(
            str(merged.get("body_type") or "none"),
            str(merged.get("body_content") or ""),
        )

    if "query_params" in changes or changes.get("remove_query_params"):
        incoming = _pairs(changes.get("query_params"), field="query_params")
        _reject_agent_secrets(incoming, field="query_params")
        blocked_removals = [
            item for item in changes.get("remove_query_params") or []
            if _SENSITIVE_NAME.search(str(item))
        ]
        if blocked_removals:
            raise AgentInterfaceError(
                "数据管家不能清除敏感 Query 参数；请在接口管理界面完成该操作。"
            )
        merged["query_params"] = _merge_pairs(
            current.get("query_params") or [], incoming,
            changes.get("remove_query_params"), field="query_params",
        )
    if "headers" in changes or changes.get("remove_headers"):
        incoming = _pairs(changes.get("headers"), field="headers")
        _reject_agent_secrets(incoming, field="headers")
        blocked_removals = [
            item for item in changes.get("remove_headers") or []
            if _SENSITIVE_NAME.search(str(item))
        ]
        if blocked_removals:
            raise AgentInterfaceError(
                "数据管家不能清除敏感 Header；请在接口管理界面完成该操作。"
            )
        merged["headers"] = _merge_pairs(
            current.get("headers") or [], incoming,
            changes.get("remove_headers"), field="headers", case_insensitive=True,
        )
    if "parameters" in changes:
        _reject_parameter_secrets(changes.get("parameters") or [])
        merged["parameter_schema"] = changes.get("parameters") or []

    try:
        updated = update_interface(int(iid), InterfaceIn(**merged))
    except (HTTPException, ValidationError, ValueError) as exc:
        raise AgentInterfaceError(_error_text(exc)) from exc
    updated = _record_actor(int(updated["id"]), actor_user_id)
    return {
        "interface": _agent_view(updated, detail=True),
        "notice": "接口配置已产生新 revision；已发布流水线中的固定 revision 会拒绝静默漂移。",
    }


def _allowed_parameters(interface: dict, location: str) -> dict[str, dict]:
    return {
        str(item.get("name")): item
        for item in interface.get("parameter_schema") or []
        if item.get("location") == location
    }


def validate_runtime_pairs(interface: dict, location: str, values: Any) -> list[dict[str, str]]:
    pairs = _pairs(values, field=location)
    if not pairs:
        return []
    allowed = _allowed_parameters(interface, location)
    if not allowed and location == "query":
        allowed = {
            str(item.get("key")): {"dynamic": True, "sensitive": False}
            for item in interface.get("query_params") or []
            if item.get("key")
        }
    unknown = [item["key"] for item in pairs if item["key"] not in allowed]
    blocked = [
        item["key"] for item in pairs
        if item["key"] in allowed
        and (not allowed[item["key"]].get("dynamic", True) or allowed[item["key"]].get("sensitive"))
    ]
    if unknown:
        raise AgentInterfaceError(
            f"接口未声明可动态覆盖的 {location} 参数：{', '.join(unknown)}"
        )
    if blocked:
        raise AgentInterfaceError(
            f"以下 {location} 参数禁止动态覆盖：{', '.join(blocked)}"
        )
    if location == "header":
        _reject_agent_secrets(pairs, field="headers")
    return pairs


def _flatten_body_fields(value: Any, prefix: str = "") -> set[str]:
    """Return dotted leaf names from a structured runtime body."""
    if not isinstance(value, dict):
        return {prefix} if prefix else set()
    fields: set[str] = set()
    for raw_name, child in value.items():
        name = str(raw_name).strip()
        if not name:
            raise AgentInterfaceError("body 参数名不能为空")
        path = f"{prefix}.{name}" if prefix else name
        if isinstance(child, dict) and child:
            fields.update(_flatten_body_fields(child, path))
        else:
            fields.add(path)
    return fields


def validate_runtime_body(
    interface: dict, body: Any, *, allow_n8n_expression: bool = False,
) -> set[str]:
    """Validate dynamic body leaves against the managed parameter contract.

    Whole-object n8n expressions cannot be inspected at compile time, so the
    compiler verifies that every declared body field is dynamic/non-sensitive;
    the internal proxy validates the resolved object again at execution time.
    """
    if body is None:
        return set()
    allowed = _allowed_parameters(interface, "body")
    if not allowed:
        raise AgentInterfaceError("接口未声明可动态覆盖的 body 参数")

    if (
        allow_n8n_expression
        and isinstance(body, str)
        and body.lstrip().startswith("={{")
    ):
        fields = set(allowed)
    else:
        body_type = str(interface.get("body_type") or "none").lower()
        structured = body
        if body_type == "json" and isinstance(body, str):
            try:
                structured = json.loads(body)
            except ValueError as exc:
                raise AgentInterfaceError("动态 JSON Body 必须是合法 JSON 对象") from exc
        elif body_type in {"form", "multipart"}:
            structured = {
                item["key"]: item["value"]
                for item in _pairs(body, field=f"{body_type} body")
            }
        elif body_type == "raw":
            if len(allowed) != 1:
                raise AgentInterfaceError("raw Body 必须且只能声明一个动态 body 参数")
            structured = {next(iter(allowed)): body}
        if not isinstance(structured, dict):
            raise AgentInterfaceError("动态 Body 必须是对象，并按参数契约传入字段")
        fields = _flatten_body_fields(structured)

    unknown = sorted(fields - set(allowed))
    blocked = sorted(
        name for name in fields & set(allowed)
        if not allowed[name].get("dynamic", True) or allowed[name].get("sensitive")
    )
    if unknown:
        raise AgentInterfaceError("接口未声明可动态覆盖的 body 参数：" + ", ".join(unknown))
    if blocked:
        raise AgentInterfaceError("以下 body 参数禁止动态覆盖：" + ", ".join(blocked))
    return fields


def request_overrides_for_interface(
    interface: dict, *, path: Any = None, query: Any = None,
    headers: Any = None, body: Any = None,
    files: list[executor.RequestFile] | None = None,
    source: str,
) -> executor.RequestOverrides:
    path_pairs = validate_runtime_pairs(interface, "path", path)
    query_pairs = validate_runtime_pairs(interface, "query", query)
    header_pairs = validate_runtime_pairs(interface, "header", headers)
    kwargs: dict[str, Any] = {
        "path_params": [(item["key"], item["value"]) for item in path_pairs] or None,
        "query_params": [(item["key"], item["value"]) for item in query_pairs] or None,
        "headers": [(item["key"], item["value"]) for item in header_pairs] or None,
        "source": source,
    }
    body_type = (interface.get("body_type") or "none").lower()
    if files is not None:
        if body_type != "multipart":
            raise AgentInterfaceError("只有 multipart 接口可以传入会话文件")
        fields = _pairs(body, field="multipart body") if body is not None else []
        kwargs["multipart_fields"] = [(item["key"], item["value"]) for item in fields]
        kwargs["files"] = files
    elif body is not None:
        if body_type == "none":
            raise AgentInterfaceError("该接口未配置请求 Body")
        validate_runtime_body(interface, body)
        if body_type == "json":
            kwargs["body"] = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
            kwargs["content_type"] = "application/json; charset=utf-8"
        elif body_type == "form":
            kwargs["body"] = body if isinstance(body, str) else urlencode(body, doseq=True)
            kwargs["content_type"] = "application/x-www-form-urlencoded"
        elif body_type == "multipart":
            fields = _pairs(body, field="multipart body")
            kwargs["multipart_fields"] = [(item["key"], item["value"]) for item in fields]
            kwargs["files"] = []
        else:
            kwargs["body"] = str(body)
    return executor.RequestOverrides(**kwargs)


def call_interface_for_agent(
    *, iid: int, path: Any = None, query: Any = None, headers: Any = None,
    body: Any = None, files: list[executor.RequestFile] | None = None,
) -> dict:
    interface = _load(int(iid))
    overrides = request_overrides_for_interface(
        interface,
        path=path,
        query=query,
        headers=headers,
        body=body,
        files=files,
        source="steward",
    )
    result = executor.run_interface(interface, overrides)
    response_body = _redact_response_body(
        result.get("response_body") or "", result.get("content_type") or "",
    )
    truncated = len(response_body) > _CALL_BODY_LIMIT
    if truncated:
        response_body = response_body[:_CALL_BODY_LIMIT] + "\n…（响应已截断，可到调用历史查看完整内容）"
    return {
        "interface": {
            "id": interface["id"],
            "name": interface["name"],
            "method": interface["method"],
            "configRevision": interface.get("config_revision") or 1,
        },
        "run": {
            "id": result.get("run_id"),
            "ok": bool(result.get("ok")),
            "statusCode": result.get("status_code"),
            "elapsedMs": result.get("elapsed_ms"),
            "contentType": result.get("content_type") or "",
            "responseBody": response_body,
            "truncated": truncated,
            "error": result.get("error"),
            "relogin": bool(result.get("relogin")),
        },
    }


def load_interface(iid: int) -> dict:
    """Trusted internal read used by the n8n compiler and proxy router."""
    return _load(iid)
