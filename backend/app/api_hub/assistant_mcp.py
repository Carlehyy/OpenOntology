"""In-process MCP tools for Super Assistant to operate API Hub.

This is the platform builtin (`builtin_key=api_hub`). It is not the retired
public `/api-hub/mcp` endpoint: tools are management verbs over the current
user's interface catalog (admin sees the whole hub, matching the UI).
"""
from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import json
import re
from typing import Any
from urllib.parse import urlencode

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.auth.crypto import decrypt_value, encrypt_value
from app.auth.models import User, UserEnvVar, UserPrivacyVar
from app.auth.permissions import user_has_menu_access

from . import db, executor
from .interface_contracts import (
    DeleteGroupBody,
    FileField,
    InterfaceIn,
    InterfaceParameter,
    KV,
)
from .interface_service import (
    _get_or_404,
    _is_admin,
    _RESERVED_GROUP,
    _row_to_dict,
    apply_http_publication,
    auto_http_publication as persist_auto_http_publication,
    create_interface,
    delete_group,
    delete_interface,
    move_interface,
    rename_group,
    update_interface,
)
from . import proxy_keys


BUILTIN_KEY = "api_hub"
SERVER_NAME = "platform_api_hub"
BUILTIN_URL = "builtin://api-hub"
MENU_KEY = "api_hub.interfaces"
_CALL_BODY_LIMIT = 20_000
_SECRET_NOTICE = (
    "该结果含接口配置明文（含 Header/Query/Body）。"
    "不要把密钥复述进聊天，也不要写入记忆。"
)
_VAR_NOTICE = (
    "该结果含个人变量明文。不要把密钥复述进聊天，也不要写入记忆。"
    "接口 URL/请求头/请求体里用 {{env:KEY}} 或 {{privacy:KEY}} 引用；"
    "HTTP 公开发布与 n8n 不会解析这些占位符。"
)
_VAR_KEY_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_VAR_KEY_MAX = 128
_VAR_VALUE_MAX = 4096
_VAR_MAX_ITEMS = 50


class ApiHubMcpError(ValueError):
    pass


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
    *,
    additional_properties: bool = False,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties or {},
            "required": required or [],
            "additionalProperties": additional_properties,
        },
    }


_INTERFACE_FIELDS = {
    "name": {"type": "string", "description": "接口名称"},
    "url": {
        "type": "string",
        "description": "HTTP/HTTPS 绝对 URL，或 mcp-bridge://<server_id>/<tool>",
    },
    "method": {
        "type": "string",
        "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    },
    "group": {"type": "string", "description": "分类名；空或「默认分组」表示默认分类"},
    "description": {"type": "string"},
    "query_params": {
        "type": "object",
        "additionalProperties": {"type": "string"},
        "description": "Query 键值；也接受 [{key,value}] 数组",
    },
    "headers": {
        "type": "object",
        "additionalProperties": {"type": "string"},
        "description": "Header 键值，含 Authorization 等密钥",
    },
    "body_type": {
        "type": "string",
        "enum": ["none", "json", "form", "multipart", "raw"],
    },
    "body_content": {"type": "string"},
    "file_fields": {
        "type": "array",
        "items": {"type": "object"},
        "description": "multipart 文件字段：key/accept/multiple",
    },
    "parameters": {
        "type": "array",
        "items": {"type": "object"},
        "description": "动态参数契约 name/location/value_type/required/default/description/dynamic/sensitive",
    },
}


def tool_manifest() -> list[dict[str, Any]]:
    return [
        _tool(
            "list_interfaces",
            "列出当前用户可管理的接口（管理员可见全部）。按名称、说明、URL、方法或分类过滤。",
            {
                "keyword": {"type": "string"},
                "group": {"type": "string", "description": "分类名；默认分组用空串或「默认分组」"},
            },
        ),
        _tool(
            "get_interface",
            "读取一个接口的完整配置（含密钥明文）和 revision。编辑或调用前先读。",
            {"interface_id": {"type": "integer"}},
            ["interface_id"],
        ),
        _tool(
            "create_interface",
            "新建接口。不会自动发布 HTTP；发布请随后调用 set_http_publication 或 auto_http_publication。",
            _INTERFACE_FIELDS,
            ["name", "url"],
        ),
        _tool(
            "update_interface",
            "按字段修改接口并产生新 revision。必须传 get_interface 刚读取的 expected_revision。",
            {
                "interface_id": {"type": "integer"},
                "expected_revision": {"type": "integer"},
                "changes": {
                    "type": "object",
                    "additionalProperties": True,
                    "description": (
                        "可改 name/url/method/group/description/query_params/headers/"
                        "body_type/body_content/file_fields/parameters"
                    ),
                },
            },
            ["interface_id", "expected_revision", "changes"],
        ),
        _tool(
            "delete_interface",
            "删除一个接口及其调用历史。不可恢复。",
            {"interface_id": {"type": "integer"}},
            ["interface_id"],
        ),
        _tool(
            "call_interface",
            "按已保存配置调用指定接口。可覆盖 path/query/header/body；multipart 文件用当前会话 artifact_id。",
            {
                "interface_id": {"type": "integer"},
                "path": {"type": "object", "additionalProperties": {"type": "string"}},
                "query": {"type": "object", "additionalProperties": {"type": "string"}},
                "headers": {"type": "object", "additionalProperties": {"type": "string"}},
                "body": {},
                "files": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {"type": "string"},
                            "artifact_id": {"type": "string"},
                        },
                    },
                    "description": "multipart：field + 当前会话 artifact_id",
                },
            },
            ["interface_id"],
        ),
        _tool(
            "list_groups",
            "列出当前用户可管理接口的分类及每个分类下的接口数量。",
        ),
        _tool(
            "rename_group",
            "把一个分类下的接口全部改到新分类名。默认分组不可作为原名。",
            {
                "old_name": {"type": "string"},
                "new_name": {"type": "string"},
            },
            ["old_name", "new_name"],
        ),
        _tool(
            "delete_group",
            "删除分类：该分类下接口移入默认分组，接口本身不删除。",
            {"group_name": {"type": "string"}},
            ["group_name"],
        ),
        _tool(
            "move_interface",
            "把接口移到指定分类的指定位置（0 起算）。",
            {
                "interface_id": {"type": "integer"},
                "group": {"type": "string"},
                "target_index": {"type": "integer", "minimum": 0},
            },
            ["interface_id"],
        ),
        _tool(
            "set_http_publication",
            "设置或取消该接口的 HTTP 公开发布（公开路径、可透传 query/header/body 字段）。",
            {
                "interface_id": {"type": "integer"},
                "enabled": {"type": "boolean"},
                "slug": {"type": "string", "description": "公开路径，enabled=true 时必填"},
                "query_keys": {"type": "array", "items": {"type": "string"}},
                "header_keys": {"type": "array", "items": {"type": "string"}},
                "body_enabled": {"type": "boolean"},
                "body_keys": {"type": "array", "items": {"type": "string"}},
            },
            ["interface_id", "enabled"],
        ),
        _tool(
            "auto_http_publication",
            "按接口配置自动推断安全的 HTTP 转发契约并发布。",
            {"interface_id": {"type": "integer"}},
            ["interface_id"],
        ),
        _tool(
            "list_proxy_keys",
            "列出 HTTP 调用方密钥（只返回掩码，不含明文）。非管理员只看到作用在自己接口上的密钥。",
        ),
        _tool(
            "create_proxy_key",
            "创建 HTTP 调用方密钥。明文 secret 只在本次返回。非管理员不能授权全部接口。",
            {
                "name": {"type": "string"},
                "enabled": {"type": "boolean"},
                "valid_from": {"type": "string", "description": "ISO 时间，可空"},
                "expires_at": {"type": "string", "description": "ISO 时间，可空"},
                "scope_all": {
                    "type": "boolean",
                    "description": "授权全部已发布接口；仅管理员可用",
                },
                "interface_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "可调用的已发布接口 ID",
                },
            },
            ["name"],
        ),
        _tool(
            "update_proxy_key",
            "更新 HTTP 调用方密钥的名称、启停、有效期或接口范围。不会再次返回明文。",
            {
                "key_id": {"type": "integer"},
                "name": {"type": "string"},
                "enabled": {"type": "boolean"},
                "valid_from": {"type": "string"},
                "expires_at": {"type": "string"},
                "scope_all": {"type": "boolean"},
                "interface_ids": {"type": "array", "items": {"type": "integer"}},
            },
            ["key_id", "name"],
        ),
        _tool(
            "delete_proxy_key",
            "删除一个 HTTP 调用方密钥。调用方立刻失效。",
            {"key_id": {"type": "integer"}},
            ["key_id"],
        ),
        _tool(
            "list_env_vars",
            "列出当前用户的环境变量（含明文）。接口里用 {{env:KEY}} 引用。",
        ),
        _tool(
            "set_env_var",
            "新增或更新当前用户的一条环境变量。不会改动其他人的变量，也不会整表覆盖。",
            {
                "key": {"type": "string", "description": "字母数字、下划线、连字符或点，1-128 位"},
                "value": {"type": "string", "description": "明文值，最长 4096"},
            },
            ["key", "value"],
        ),
        _tool(
            "delete_env_var",
            "删除当前用户的一条环境变量。引用它的接口占位符在调用时会失败。",
            {"key": {"type": "string"}},
            ["key"],
        ),
        _tool(
            "list_privacy_vars",
            "列出当前用户的隐私变量（不含明文，只标明是否已有值）。接口里用 {{privacy:KEY}} 引用。",
        ),
        _tool(
            "get_privacy_var",
            "读取当前用户一条隐私变量的明文。没有值则报错。",
            {"key": {"type": "string"}},
            ["key"],
        ),
        _tool(
            "set_privacy_var",
            "新增或更新当前用户的一条隐私变量明文，调用接口时按 {{privacy:KEY}} 解析。不走上报脚本。",
            {
                "key": {"type": "string", "description": "字母数字、下划线、连字符或点，1-128 位"},
                "value": {"type": "string", "description": "明文值，最长 4096"},
            },
            ["key", "value"],
        ),
        _tool(
            "delete_privacy_var",
            "删除当前用户的一条隐私变量。引用它的接口占位符在调用时会失败。",
            {"key": {"type": "string"}},
            ["key"],
        ),
    ]


def execute_tool(
    db_session: Session,
    *,
    user: User,
    name: str,
    arguments: dict[str, Any] | None,
    conversation_id: str | None = None,
) -> str:
    if not user_has_menu_access(db_session, user, MENU_KEY):
        raise ApiHubMcpError("当前用户没有「接口代理 → 接口管理」权限")
    args = dict(arguments or {})
    handlers = {
        "list_interfaces": lambda: _list_interfaces(user, args),
        "get_interface": lambda: _get_interface(user, args),
        "create_interface": lambda: _create_interface(user, args),
        "update_interface": lambda: _update_interface(user, args),
        "delete_interface": lambda: _delete_interface(user, args),
        "call_interface": lambda: _call_interface(user, args, conversation_id),
        "list_groups": lambda: _list_groups(user),
        "rename_group": lambda: _rename_group(user, args),
        "delete_group": lambda: _delete_group(user, args),
        "move_interface": lambda: _move_interface(user, args),
        "set_http_publication": lambda: _set_http_publication(user, args),
        "auto_http_publication": lambda: _auto_http_publication(user, args),
        "list_proxy_keys": lambda: _list_proxy_keys(user),
        "create_proxy_key": lambda: _create_proxy_key(user, args),
        "update_proxy_key": lambda: _update_proxy_key(user, args),
        "delete_proxy_key": lambda: _delete_proxy_key(user, args),
        "list_env_vars": lambda: _list_env_vars(db_session, user),
        "set_env_var": lambda: _set_env_var(db_session, user, args),
        "delete_env_var": lambda: _delete_env_var(db_session, user, args),
        "list_privacy_vars": lambda: _list_privacy_vars(db_session, user),
        "get_privacy_var": lambda: _get_privacy_var(db_session, user, args),
        "set_privacy_var": lambda: _set_privacy_var(db_session, user, args),
        "delete_privacy_var": lambda: _delete_privacy_var(db_session, user, args),
    }
    handler = handlers.get(name)
    if handler is None:
        raise ApiHubMcpError(f"未知工具: {name}")
    try:
        result = handler()
    except HTTPException as exc:
        raise ApiHubMcpError(_http_detail(exc)) from exc
    except ValidationError as exc:
        errors = exc.errors()
        raise ApiHubMcpError(str(errors[0].get("msg") if errors else exc)) from exc
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ApiHubMcpError):
            raise
        raise ApiHubMcpError(str(exc)) from exc
    return json.dumps(result, ensure_ascii=False, default=str)


def _http_detail(exc: HTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, str):
        return detail
    try:
        return json.dumps(detail, ensure_ascii=False)
    except TypeError:
        return str(detail)


def _int_arg(args: dict[str, Any], key: str) -> int:
    if key not in args or args[key] in (None, ""):
        raise ApiHubMcpError(f"缺少参数 {key}")
    try:
        return int(args[key])
    except (TypeError, ValueError) as exc:
        raise ApiHubMcpError(f"{key} 必须是整数") from exc


def _optional_int(args: dict[str, Any], key: str, default: int = 0) -> int:
    if args.get(key) in (None, ""):
        return default
    try:
        return int(args[key])
    except (TypeError, ValueError) as exc:
        raise ApiHubMcpError(f"{key} 必须是整数") from exc


def _bool_arg(
    args: dict[str, Any],
    key: str,
    *,
    required: bool = True,
    default: bool | None = None,
) -> bool:
    if key not in args or args[key] is None:
        if required:
            raise ApiHubMcpError(f"缺少参数 {key}")
        if default is None:
            raise ApiHubMcpError(f"缺少参数 {key}")
        return default
    value = args[key]
    if value is True or value is False:
        return value
    if value == 1 or value == 0:
        if type(value) is bool:
            return value
        if type(value) is int:
            return bool(value)
    raise ApiHubMcpError(f"{key} 必须是 true 或 false")


def _pairs(value: Any, *, field: str) -> list[dict[str, str]]:
    if value is None:
        return []
    if isinstance(value, dict):
        iterable = value.items()
    elif isinstance(value, list):
        iterable = []
        for item in value:
            if not isinstance(item, dict) or "key" not in item:
                raise ApiHubMcpError(f"{field} 必须是键值对象或 {{key,value}} 数组")
            iterable.append((item.get("key"), item.get("value", "")))
    else:
        raise ApiHubMcpError(f"{field} 必须是键值对象或数组")
    result = []
    for key, raw_value in iterable:
        name = str(key or "").strip()
        if not name:
            continue
        result.append({"key": name, "value": str(raw_value if raw_value is not None else "")})
    return result


def _group_name(value: Any) -> str:
    name = "" if value is None else str(value).strip()
    return "" if name == _RESERVED_GROUP else name


def _load_owned(iid: int, user: User) -> dict:
    with db.get_conn() as conn:
        return _row_to_dict(_get_or_404(conn, iid, user=user))


def _owned_ids(user: User) -> set[int]:
    with db.get_conn() as conn:
        if _is_admin(user):
            rows = conn.execute("SELECT id FROM interfaces").fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM interfaces WHERE created_by = ?",
                (user.id,),
            ).fetchall()
    return {int(row["id"]) for row in rows}


def _list_owned_rows(user: User) -> list[dict]:
    with db.get_conn() as conn:
        if _is_admin(user):
            rows = conn.execute(
                "SELECT * FROM interfaces ORDER BY group_name, sort_order, id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM interfaces WHERE created_by = ? "
                "ORDER BY group_name, sort_order, id",
                (user.id,),
            ).fetchall()
    return [_row_to_dict(row) for row in rows]


def _view(interface: dict, *, detail: bool) -> dict:
    base: dict[str, Any] = {}
    if detail:
        base["notice"] = _SECRET_NOTICE
    base.update(
        {
            "id": interface["id"],
            "name": interface["name"],
            "description": interface.get("description") or "",
            "group": interface.get("group_name") or _RESERVED_GROUP,
            "method": interface["method"],
            "url": interface.get("url") or "",
            "bodyType": interface.get("body_type") or "none",
            "httpPublished": bool(interface.get("http_enabled")),
            "proxySlug": interface.get("proxy_slug") or "",
            "configRevision": int(interface.get("config_revision") or 1),
            "updatedAt": interface.get("updated_at"),
        }
    )
    if not detail:
        return base
    base.update(
        {
            "queryParams": interface.get("query_params") or [],
            "headers": interface.get("headers") or [],
            "bodyContent": interface.get("body_content") or "",
            "fileFields": interface.get("file_fields") or [],
            "parameterSchema": interface.get("parameter_schema") or [],
            "proxyQueryKeys": interface.get("proxy_query_keys") or [],
            "proxyHeaderKeys": interface.get("proxy_header_keys") or [],
            "proxyBodyEnabled": bool(interface.get("proxy_body_enabled")),
            "proxyBodyKeys": interface.get("proxy_body_keys") or [],
        }
    )
    return base


def _parameters(value: Any) -> list[InterfaceParameter]:
    if not value:
        return []
    if not isinstance(value, list):
        raise ApiHubMcpError("parameters 必须是数组")
    return [InterfaceParameter(**item) for item in value]


def _file_fields(value: Any) -> list[FileField]:
    if not value:
        return []
    if not isinstance(value, list):
        raise ApiHubMcpError("file_fields 必须是数组")
    return [FileField(**item) for item in value]


def _kv_models(value: Any, *, field: str) -> list[KV]:
    return [KV(**item) for item in _pairs(value, field=field)]


def _merge_kv(
    current: list,
    patch: Any,
    *,
    field: str,
    case_insensitive: bool = False,
) -> list[dict[str, str]]:
    if isinstance(patch, list):
        return _pairs(patch, field=field)
    incoming = _pairs(patch, field=field)
    marker = (lambda value: value.lower()) if case_insensitive else (lambda value: value)
    replacements = {marker(item["key"]): item for item in incoming}
    output = []
    for item in current or []:
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        key_marker = marker(key)
        if key_marker in replacements:
            output.append(replacements.pop(key_marker))
        else:
            output.append({"key": key, "value": str(item.get("value") or "")})
    output.extend(replacements.values())
    return output


def _list_interfaces(user: User, args: dict[str, Any]) -> dict:
    keyword = str(args.get("keyword") or "").strip().lower()
    group = _group_name(args["group"]) if "group" in args and args.get("group") is not None else None
    items = []
    for interface in _list_owned_rows(user):
        if group is not None and (interface.get("group_name") or "") != group:
            continue
        haystack = " ".join(
            str(interface.get(key) or "")
            for key in ("name", "description", "group_name", "method", "url")
        ).lower()
        if keyword and keyword not in haystack:
            continue
        items.append(_view(interface, detail=False))
    return {"count": len(items), "interfaces": items}


def _get_interface(user: User, args: dict[str, Any]) -> dict:
    return {"interface": _view(_load_owned(_int_arg(args, "interface_id"), user), detail=True)}


def _create_interface(user: User, args: dict[str, Any]) -> dict:
    body = InterfaceIn(
        name=str(args.get("name") or ""),
        url=str(args.get("url") or ""),
        method=str(args.get("method") or "GET"),
        group_name=_group_name(args.get("group")),
        description=str(args.get("description") or ""),
        query_params=_kv_models(args.get("query_params"), field="query_params"),
        headers=_kv_models(args.get("headers"), field="headers"),
        body_type=str(args.get("body_type") or "none"),
        body_content=str(args.get("body_content") or ""),
        file_fields=_file_fields(args.get("file_fields")),
        parameter_schema=_parameters(args.get("parameters")),
        http_enabled=False,
        open_enabled=False,
        mcp_enabled=False,
    )
    created = create_interface(body, user=user)
    return {
        "interface": _view(created, detail=True),
        "notice": "接口已保存。HTTP 公开发布需另调 set_http_publication 或 auto_http_publication。",
    }


def _update_interface(user: User, args: dict[str, Any]) -> dict:
    iid = _int_arg(args, "interface_id")
    expected = _int_arg(args, "expected_revision")
    changes = args.get("changes")
    if not isinstance(changes, dict) or not changes:
        raise ApiHubMcpError("changes 必须包含至少一个要修改的字段")
    current = _load_owned(iid, user)
    revision = int(current.get("config_revision") or 1)
    if expected != revision:
        raise ApiHubMcpError(
            f"接口配置已被其他操作更新：当前 revision={revision}，"
            f"本次基于 revision={expected}。请重新读取后再修改。"
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
            merged[target] = (
                _group_name(changes[source]) if source == "group" else changes[source]
            )
    if "query_params" in changes:
        merged["query_params"] = _merge_kv(
            current.get("query_params") or [], changes.get("query_params"), field="query_params",
        )
    if "headers" in changes:
        merged["headers"] = _merge_kv(
            current.get("headers") or [], changes.get("headers"), field="headers",
            case_insensitive=True,
        )
    if "file_fields" in changes:
        merged["file_fields"] = [
            item.model_dump() for item in _file_fields(changes.get("file_fields"))
        ]
    if "parameters" in changes:
        merged["parameter_schema"] = [
            item.model_dump(mode="json") for item in _parameters(changes.get("parameters"))
        ]
    updated = update_interface(iid, InterfaceIn(**merged), user=user)
    return {"interface": _view(updated, detail=True)}


def _delete_interface(user: User, args: dict[str, Any]) -> dict:
    iid = _int_arg(args, "interface_id")
    _load_owned(iid, user)
    return delete_interface(iid, user=user)


def _call_body_kwargs(interface: dict, body: Any) -> dict[str, Any]:
    body_type = (interface.get("body_type") or "none").lower()
    if body_type == "none":
        raise ApiHubMcpError("该接口未配置请求 Body")
    if body_type == "json":
        payload = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        return {"body": payload, "content_type": "application/json; charset=utf-8"}
    if body_type == "form":
        payload = body if isinstance(body, str) else urlencode(body, doseq=True)
        return {"body": payload, "content_type": "application/x-www-form-urlencoded"}
    if body_type == "multipart":
        fields = _pairs(body, field="multipart body")
        return {
            "multipart_fields": [(item["key"], item["value"]) for item in fields],
            "files": [],
        }
    return {"body": str(body)}


def _call_interface(user: User, args: dict[str, Any], conversation_id: str | None) -> dict:
    iid = _int_arg(args, "interface_id")
    interface = _load_owned(iid, user)
    path_pairs = _pairs(args.get("path"), field="path")
    query_pairs = _pairs(args.get("query"), field="query")
    header_pairs = _pairs(args.get("headers"), field="headers")
    kwargs: dict[str, Any] = {
        "source": "super_assistant",
        "actor": user,
        "path_params": [(item["key"], item["value"]) for item in path_pairs] or None,
        "query_params": [(item["key"], item["value"]) for item in query_pairs] or None,
        "headers": [(item["key"], item["value"]) for item in header_pairs] or None,
    }
    files_spec = args.get("files")
    body = args.get("body")
    with ExitStack() as stack:
        if files_spec is not None:
            kwargs.update(_runtime_files(interface, files_spec, conversation_id, stack, body))
        elif body is not None:
            kwargs.update(_call_body_kwargs(interface, body))
        result = executor.run_interface(interface, executor.RequestOverrides(**kwargs))
    response_body = str(result.get("response_body") or "")
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


def _runtime_files(
    interface: dict,
    files_spec: Any,
    conversation_id: str | None,
    stack: ExitStack,
    body: Any,
) -> dict[str, Any]:
    if (interface.get("body_type") or "none").lower() != "multipart":
        raise ApiHubMcpError("只有 multipart 接口可以传入会话文件")
    if not conversation_id:
        raise ApiHubMcpError("multipart 文件需要在超级助手会话中调用")
    if not isinstance(files_spec, list):
        raise ApiHubMcpError("files 必须是 field/artifact_id 对象数组")
    from app.super_assistant import files_workspace

    configured = {
        str(item.get("key")): item
        for item in interface.get("file_fields") or []
        if item.get("key")
    }
    runtime_files: list[executor.RequestFile] = []
    counts: dict[str, int] = {}
    workspace = files_workspace.session_workspace()
    for item in files_spec:
        if not isinstance(item, dict):
            raise ApiHubMcpError("files 必须是 field/artifact_id 对象数组")
        field = str(item.get("field") or "").strip()
        artifact_id = str(item.get("artifact_id") or "").strip()
        definition = configured.get(field)
        if definition is None:
            raise ApiHubMcpError(f"接口未配置 multipart 文件字段：{field or '(空)'}")
        counts[field] = counts.get(field, 0) + 1
        if counts[field] > 1 and not definition.get("multiple"):
            raise ApiHubMcpError(f"文件字段 {field} 不允许多文件")
        try:
            artifact, file_path = workspace.require_file(conversation_id, artifact_id)
        except Exception as exc:  # workspace errors share a safe public message
            raise ApiHubMcpError(str(exc)) from exc
        stream = stack.enter_context(open(file_path, "rb"))
        runtime_files.append(
            executor.RequestFile(
                field_name=field,
                filename=artifact.get("filename") or file_path.name,
                stream=stream,
                content_type=artifact.get("mimeType")
                or artifact.get("mime_type")
                or "application/octet-stream",
                size=file_path.stat().st_size,
            )
        )
    fields = _pairs(body, field="multipart body") if body is not None else []
    return {
        "multipart_fields": [(item["key"], item["value"]) for item in fields],
        "files": runtime_files,
    }


def _list_groups(user: User) -> dict:
    counts: dict[str, int] = {}
    for interface in _list_owned_rows(user):
        name = interface.get("group_name") or _RESERVED_GROUP
        counts[name] = counts.get(name, 0) + 1
    groups = [
        {"name": name, "count": counts[name]}
        for name in sorted(counts, key=lambda item: (item == _RESERVED_GROUP, item))
    ]
    return {"count": len(groups), "groups": groups}


def _rename_group(user: User, args: dict[str, Any]) -> dict:
    return rename_group(
        old_name=str(args.get("old_name") or ""),
        new_name=str(args.get("new_name") or ""),
        user=user,
    )


def _delete_group(user: User, args: dict[str, Any]) -> dict:
    return delete_group(
        DeleteGroupBody(group_name=str(args.get("group_name") or "")),
        user=user,
    )


def _move_interface(user: User, args: dict[str, Any]) -> dict:
    return move_interface(
        _int_arg(args, "interface_id"),
        group_name=_group_name(args.get("group")),
        target_index=_optional_int(args, "target_index", 0),
        user=user,
    )


def _set_http_publication(user: User, args: dict[str, Any]) -> dict:
    iid = _int_arg(args, "interface_id")
    current = _load_owned(iid, user)
    enabled = _bool_arg(args, "enabled")
    updated = apply_http_publication(
        iid,
        enabled=enabled,
        slug=str(args["slug"]) if "slug" in args else (current.get("proxy_slug") or ""),
        query_keys=(
            list(args.get("query_keys") or [])
            if "query_keys" in args
            else list(current.get("proxy_query_keys") or [])
        ),
        header_keys=(
            list(args.get("header_keys") or [])
            if "header_keys" in args
            else list(current.get("proxy_header_keys") or [])
        ),
        body_enabled=(
            _bool_arg(args, "body_enabled")
            if "body_enabled" in args
            else bool(current.get("proxy_body_enabled"))
        ),
        body_keys=(
            list(args.get("body_keys") or [])
            if "body_keys" in args
            else list(current.get("proxy_body_keys") or [])
        ),
        user=user,
    )
    return {"interface": _view(updated, detail=True)}


def _auto_http_publication(user: User, args: dict[str, Any]) -> dict:
    iid = _int_arg(args, "interface_id")
    _load_owned(iid, user)
    updated = persist_auto_http_publication(iid, user=user)
    return {"interface": _view(updated, detail=True)}


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApiHubMcpError("时间必须是 ISO 8601") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _assert_key_scope(user: User, *, scope_all: bool, interface_ids: list[int]) -> None:
    if _is_admin(user):
        return
    if scope_all:
        raise ApiHubMcpError("非管理员不能创建或修改「授权全部接口」的调用方密钥")
    owned = _owned_ids(user)
    foreign = [item for item in interface_ids if item not in owned]
    if foreign:
        raise ApiHubMcpError(
            "以下接口不属于当前用户，不能写入调用方密钥："
            + ", ".join(str(item) for item in foreign)
        )


def _key_visible(user: User, key: dict) -> bool:
    if _is_admin(user):
        return True
    if key.get("scope_all"):
        return False
    owned = _owned_ids(user)
    ids = [int(item) for item in key.get("interface_ids") or []]
    return bool(ids) and all(item in owned for item in ids)


def _list_proxy_keys(user: User) -> dict:
    keys = [item for item in proxy_keys.list_proxy_keys() if _key_visible(user, item)]
    return {"count": len(keys), "keys": keys}


def _create_proxy_key(user: User, args: dict[str, Any]) -> dict:
    scope_all = _bool_arg(args, "scope_all", required=False, default=False)
    interface_ids = [int(item) for item in args.get("interface_ids") or []]
    _assert_key_scope(user, scope_all=scope_all, interface_ids=interface_ids)
    created = proxy_keys.create_proxy_key(
        proxy_keys.ProxyKeyCreate(
            name=str(args.get("name") or ""),
            enabled=_bool_arg(args, "enabled", required=False, default=True),
            valid_from=_parse_dt(args.get("valid_from")),
            expires_at=_parse_dt(args.get("expires_at")),
            scope_all=scope_all,
            interface_ids=interface_ids,
        )
    )
    return {
        "notice": "明文密钥只出现这一次，请立即交给调用方，不要写入聊天或记忆。",
        "key": created,
    }


def _load_visible_key(user: User, key_id: int) -> dict:
    for item in proxy_keys.list_proxy_keys():
        if int(item["id"]) == key_id:
            if not _key_visible(user, item):
                raise ApiHubMcpError("密钥不存在")
            return item
    raise ApiHubMcpError("密钥不存在")


def _update_proxy_key(user: User, args: dict[str, Any]) -> dict:
    current = _load_visible_key(user, _int_arg(args, "key_id"))
    scope_all = (
        _bool_arg(args, "scope_all")
        if "scope_all" in args
        else bool(current.get("scope_all"))
    )
    interface_ids = (
        [int(item) for item in args.get("interface_ids") or []]
        if "interface_ids" in args
        else list(current.get("interface_ids") or [])
    )
    _assert_key_scope(user, scope_all=scope_all, interface_ids=interface_ids)
    updated = proxy_keys.update_proxy_key(
        int(current["id"]),
        proxy_keys.ProxyKeyUpdate(
            name=str(args.get("name") or current["name"]),
            enabled=(
                _bool_arg(args, "enabled")
                if "enabled" in args
                else bool(current.get("enabled"))
            ),
            valid_from=_parse_dt(args["valid_from"]) if "valid_from" in args else _parse_dt(current.get("valid_from")),
            expires_at=_parse_dt(args["expires_at"]) if "expires_at" in args else _parse_dt(current.get("expires_at")),
            scope_all=scope_all,
            interface_ids=interface_ids,
        ),
    )
    return {"key": updated}


def _delete_proxy_key(user: User, args: dict[str, Any]) -> dict:
    current = _load_visible_key(user, _int_arg(args, "key_id"))
    return proxy_keys.delete_proxy_key(int(current["id"]))


def _orm_user(db_session: Session, user: User) -> User:
    row = db_session.get(User, getattr(user, "id", None))
    if row is None:
        raise ApiHubMcpError("用户不存在")
    return row


def _var_key(args: dict[str, Any], field: str = "key") -> str:
    value = str(args.get(field) or "").strip()
    if not value or len(value) > _VAR_KEY_MAX or not _VAR_KEY_RE.fullmatch(value):
        raise ApiHubMcpError(
            "变量名只能包含字母、数字、下划线、连字符和点，长度 1-128"
        )
    return value


def _var_value(args: dict[str, Any], field: str = "value") -> str:
    if field not in args or args[field] is None:
        raise ApiHubMcpError(f"缺少参数 {field}")
    value = str(args[field])
    if len(value) > _VAR_VALUE_MAX:
        raise ApiHubMcpError(f"{field} 不能超过 {_VAR_VALUE_MAX} 个字符")
    return value


def _list_env_vars(db_session: Session, user: User) -> dict:
    owner = _orm_user(db_session, user)
    rows = (
        db_session.query(UserEnvVar)
        .filter(UserEnvVar.user_id == owner.id)
        .order_by(UserEnvVar.key)
        .all()
    )
    return {
        "notice": _VAR_NOTICE,
        "count": len(rows),
        "variables": [
            {
                "key": row.key,
                "value": decrypt_value(row.value_encrypted),
                "placeholder": f"{{{{env:{row.key}}}}}",
            }
            for row in rows
        ],
    }


def _set_env_var(db_session: Session, user: User, args: dict[str, Any]) -> dict:
    owner = _orm_user(db_session, user)
    key = _var_key(args)
    value = _var_value(args)
    row = (
        db_session.query(UserEnvVar)
        .filter(UserEnvVar.user_id == owner.id, UserEnvVar.key == key)
        .first()
    )
    created = row is None
    if created:
        count = (
            db_session.query(UserEnvVar)
            .filter(UserEnvVar.user_id == owner.id)
            .count()
        )
        if count >= _VAR_MAX_ITEMS:
            raise ApiHubMcpError(f"环境变量已达上限（{_VAR_MAX_ITEMS}）")
        row = UserEnvVar(user_id=owner.id, key=key, value_encrypted=encrypt_value(value))
        db_session.add(row)
    else:
        row.value_encrypted = encrypt_value(value)
    db_session.commit()
    db_session.refresh(row)
    return {
        "notice": _VAR_NOTICE,
        "variable": {
            "key": row.key,
            "value": decrypt_value(row.value_encrypted),
            "placeholder": f"{{{{env:{row.key}}}}}",
            "created": created,
        },
    }


def _delete_env_var(db_session: Session, user: User, args: dict[str, Any]) -> dict:
    owner = _orm_user(db_session, user)
    key = _var_key(args)
    row = (
        db_session.query(UserEnvVar)
        .filter(UserEnvVar.user_id == owner.id, UserEnvVar.key == key)
        .first()
    )
    if row is None:
        raise ApiHubMcpError("环境变量不存在")
    db_session.delete(row)
    db_session.commit()
    return {"ok": True, "key": key}


def _list_privacy_vars(db_session: Session, user: User) -> dict:
    owner = _orm_user(db_session, user)
    rows = (
        db_session.query(UserPrivacyVar)
        .filter(UserPrivacyVar.user_id == owner.id)
        .order_by(UserPrivacyVar.key)
        .all()
    )
    return {
        "notice": "列表不含明文。读取请用 get_privacy_var，写入请用 set_privacy_var。",
        "count": len(rows),
        "variables": [
            {
                "key": row.key,
                "hasValue": bool(row.value_encrypted),
                "placeholder": f"{{{{privacy:{row.key}}}}}",
                "lastReportedAt": row.last_reported_at,
            }
            for row in rows
        ],
    }


def _get_privacy_var(db_session: Session, user: User, args: dict[str, Any]) -> dict:
    owner = _orm_user(db_session, user)
    key = _var_key(args)
    row = (
        db_session.query(UserPrivacyVar)
        .filter(UserPrivacyVar.user_id == owner.id, UserPrivacyVar.key == key)
        .first()
    )
    if row is None or not row.value_encrypted:
        raise ApiHubMcpError("隐私变量不存在")
    return {
        "notice": _VAR_NOTICE,
        "variable": {
            "key": row.key,
            "value": decrypt_value(row.value_encrypted),
            "placeholder": f"{{{{privacy:{row.key}}}}}",
            "lastReportedAt": row.last_reported_at,
        },
    }


def _set_privacy_var(db_session: Session, user: User, args: dict[str, Any]) -> dict:
    owner = _orm_user(db_session, user)
    key = _var_key(args)
    value = _var_value(args)
    row = (
        db_session.query(UserPrivacyVar)
        .filter(UserPrivacyVar.user_id == owner.id, UserPrivacyVar.key == key)
        .first()
    )
    created = row is None
    now = datetime.now(timezone.utc)
    if created:
        count = (
            db_session.query(UserPrivacyVar)
            .filter(UserPrivacyVar.user_id == owner.id)
            .count()
        )
        if count >= _VAR_MAX_ITEMS:
            raise ApiHubMcpError(f"隐私变量已达上限（{_VAR_MAX_ITEMS}）")
        row = UserPrivacyVar(
            user_id=owner.id,
            key=key,
            value_encrypted=encrypt_value(value),
            last_reported_at=now if value else None,
        )
        db_session.add(row)
    else:
        row.value_encrypted = encrypt_value(value)
        row.last_reported_at = now if value else row.last_reported_at
    db_session.commit()
    db_session.refresh(row)
    return {
        "notice": _VAR_NOTICE,
        "variable": {
            "key": row.key,
            "hasValue": bool(row.value_encrypted),
            "placeholder": f"{{{{privacy:{row.key}}}}}",
            "created": created,
        },
    }


def _delete_privacy_var(db_session: Session, user: User, args: dict[str, Any]) -> dict:
    owner = _orm_user(db_session, user)
    key = _var_key(args)
    row = (
        db_session.query(UserPrivacyVar)
        .filter(UserPrivacyVar.user_id == owner.id, UserPrivacyVar.key == key)
        .first()
    )
    if row is None:
        raise ApiHubMcpError("隐私变量不存在")
    db_session.delete(row)
    db_session.commit()
    return {"ok": True, "key": key}
