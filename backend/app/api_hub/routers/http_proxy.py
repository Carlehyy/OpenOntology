"""普通 HTTP 接口发布、代理密钥管理与公共转发入口。"""
from __future__ import annotations

import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import FormData, UploadFile

from .. import config, db, executor, publication
from ..interface_service import _row_to_dict
from ..proxy_keys import (
    ProxyKeyCreate,
    ProxyKeyUpdate,
    _insert_proxy_key,
    _now,
    authenticate_proxy_key as _authenticate_proxy_key,
    create_proxy_key as persist_create_proxy_key,
    delete_proxy_key as persist_delete_proxy_key,
    list_proxy_keys as persist_list_proxy_keys,
    update_proxy_key as persist_update_proxy_key,
)

admin_router = APIRouter(prefix="/proxy", tags=["api-hub-http-proxy-admin"])
public_router = APIRouter(prefix=config.PROXY_PATH, tags=["api-hub-http-proxy"])

_OUTBOUND_RESPONSE_BLOCKLIST = {
    "connection",
    "content-encoding",  # requests 已自动解压，不能继续声明原压缩格式
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


@admin_router.get("/info")
def proxy_info():
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, method, proxy_slug FROM interfaces "
            "WHERE http_enabled = 1 ORDER BY group_name, sort_order, id"
        ).fetchall()
        key_count = conn.execute("SELECT COUNT(*) FROM proxy_keys").fetchone()[0]
    return {
        "path": config.PROXY_PATH,
        "key_header": config.PROXY_KEY_HEADER,
        "port": config.APP_PORT,
        "key_count": int(key_count),
        "published": [dict(row) for row in rows],
    }


@admin_router.get("/keys")
def list_proxy_keys():
    return persist_list_proxy_keys()


@admin_router.post("/keys")
def create_proxy_key(body: ProxyKeyCreate):
    return persist_create_proxy_key(body)


@admin_router.post("/packages/{interface_id}")
def create_proxy_package(interface_id: int):
    """Create a ready-to-share caller credential scoped to one published interface."""
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM interfaces WHERE id = ?", (interface_id,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="接口不存在")
        interface = _row_to_dict(row)
        if not interface.get("http_enabled"):
            raise HTTPException(status_code=409, detail="请先自动生成转发配置")
        generated_at = _now()
        name = f"{interface['name']} · 调用包 · {generated_at.strftime('%Y%m%d-%H%M')}"
        key_view, secret = _insert_proxy_key(
            conn,
            name=name,
            enabled=True,
            valid_from=None,
            expires_at=None,
            scope_all=False,
            interface_ids=[interface_id],
        )

    query_defaults = {
        str(item.get("key")): str(item.get("value", ""))
        for item in interface.get("query_params") or []
        if isinstance(item, dict) and item.get("key")
    }
    return {
        "key_id": key_view["id"],
        "key_name": key_view["name"],
        "secret": secret,
        "path": f"{config.PROXY_PATH}/{interface['proxy_slug']}",
        "key_header": config.PROXY_KEY_HEADER,
        "method": interface["method"],
        "query_params": [
            {"key": key, "value": query_defaults.get(key, "")}
            for key in interface.get("proxy_query_keys") or []
        ],
        # Header values remain platform-owned. Only show placeholders to callers.
        "header_params": [
            {"key": key, "value": ""}
            for key in interface.get("proxy_header_keys") or []
        ],
        "body_type": interface.get("body_type") or "none",
        "body_enabled": bool(interface.get("proxy_body_enabled")),
        "body_template": publication.body_template(interface),
        "editable_body_keys": interface.get("proxy_body_keys") or [],
        "multipart_fields": (
            publication.multipart_text_fields(interface)
            if interface.get("proxy_body_enabled")
            else []
        ),
        "file_fields": (
            publication.multipart_file_fields(interface)
            if interface.get("proxy_body_enabled")
            else []
        ),
        "generated_at": generated_at.isoformat(),
    }


@admin_router.put("/keys/{key_id}")
def update_proxy_key(key_id: int, body: ProxyKeyUpdate):
    return persist_update_proxy_key(key_id, body)


@admin_router.delete("/keys/{key_id}")
def delete_proxy_key(key_id: int):
    return persist_delete_proxy_key(key_id)


def _response_headers(headers: dict) -> dict[str, str]:
    return {
        key: value
        for key, value in (headers or {}).items()
        if key.lower() not in _OUTBOUND_RESPONSE_BLOCKLIST
    }


def _matches_file_accept(filename: str, content_type: str, accept: str) -> bool:
    rules = [item.strip().lower() for item in (accept or "").split(",") if item.strip()]
    if not rules:
        return True
    filename = (filename or "").lower()
    content_type = (content_type or "application/octet-stream").split(";", 1)[0].strip().lower()
    return any(
        rule == "*/*"
        or (rule.startswith(".") and filename.endswith(rule))
        or (rule.endswith("/*") and content_type.startswith(rule[:-1]))
        or rule == content_type
        for rule in rules
    )


async def _multipart_request_parts(
    request: Request,
    interface: dict,
) -> tuple[FormData, list[tuple[str, str]], list[executor.RequestFile], bool]:
    """Parse a caller multipart body and close its temporary files on validation errors."""
    form = await request.form(max_files=50, max_fields=200)
    try:
        allowed = set(interface.get("proxy_body_keys") or [])
        incoming_fields: list[tuple[str, str]] = []
        incoming_files: list[executor.RequestFile] = []
        incoming_names: set[str] = set()
        file_counts: dict[str, int] = {}
        file_config = {
            item.get("key"): item
            for item in interface.get("file_fields") or []
            if isinstance(item, dict) and item.get("key")
        }
        total_size = 0
        for field_name, value in form.multi_items():
            incoming_names.add(field_name)
            if allowed and field_name not in allowed:
                raise HTTPException(status_code=400, detail=f"Body 字段未开放：{field_name}")
            if isinstance(value, UploadFile):
                definition = file_config.get(field_name)
                if definition is None:
                    raise HTTPException(status_code=400, detail=f"文件字段未在接口中配置：{field_name}")
                file_counts[field_name] = file_counts.get(field_name, 0) + 1
                if file_counts[field_name] > 1 and not definition.get("multiple"):
                    raise HTTPException(status_code=400, detail=f"文件字段不允许多文件：{field_name}")
                if not _matches_file_accept(
                    value.filename or "", value.content_type or "", definition.get("accept") or ""
                ):
                    raise HTTPException(status_code=400, detail=f"文件类型不符合字段限制：{field_name}")
                if value.size is not None:
                    total_size += value.size
                incoming_files.append(
                    executor.RequestFile(
                        field_name=field_name,
                        filename=value.filename or "upload",
                        stream=value.file,
                        content_type=value.content_type or "application/octet-stream",
                        size=value.size,
                    )
                )
            else:
                text_value = str(value)
                total_size += len(text_value.encode("utf-8"))
                incoming_fields.append((field_name, text_value))
        if total_size > config.PROXY_MAX_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="请求体超过平台代理上限")
        defaults = [
            item
            for item in publication.parse_saved_form(interface.get("body_content") or "")
            if item[0] not in incoming_names
        ]
        fields = defaults + incoming_fields
        return form, fields, incoming_files, bool(incoming_fields or incoming_files)
    except Exception:
        await form.close()
        raise


@public_router.api_route(
    "/{slug}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def call_published_interface(slug: str, request: Request):
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM interfaces WHERE proxy_slug = ? AND http_enabled = 1",
            (slug.lower(),),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="HTTP 接口不存在或未发布")
        iface = _row_to_dict(row)
        key_row = _authenticate_proxy_key(
            conn,
            (request.headers.get(config.PROXY_KEY_HEADER) or "").strip(),
            iface["id"],
        )

    expected_method = (iface.get("method") or "GET").upper()
    if request.method.upper() != expected_method:
        return JSONResponse(
            status_code=405,
            content={"detail": f"该接口只允许使用 {expected_method} 方法"},
            headers={"Allow": expected_method},
        )

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > config.PROXY_MAX_REQUEST_BYTES:
                raise HTTPException(status_code=413, detail="请求体超过平台代理上限")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Content-Length 无效") from exc

    allowed_query = set(iface.get("proxy_query_keys") or [])
    query_items = list(request.query_params.multi_items())
    denied_query = sorted({key for key, _ in query_items if key not in allowed_query})
    if denied_query:
        raise HTTPException(
            status_code=400,
            detail="以下 Query 参数未在发布配置中开放：" + ", ".join(denied_query),
        )

    allowed_headers = {key.lower() for key in (iface.get("proxy_header_keys") or [])}
    header_items = [
        (key, value)
        for key, value in request.headers.items()
        if key.lower() in allowed_headers
    ]

    body_type = (iface.get("body_type") or "none").lower()
    body_announced = "content-length" in request.headers or "transfer-encoding" in request.headers
    body = b""
    form: FormData | None = None
    multipart_fields: list[tuple[str, str]] | None = None
    multipart_files: list[executor.RequestFile] | None = None

    if body_type == "multipart" and body_announced:
        request_content_type = (request.headers.get("content-type") or "").lower()
        if not request_content_type.startswith("multipart/form-data"):
            raise HTTPException(status_code=400, detail="该接口要求 multipart/form-data 请求")
        form, multipart_fields, multipart_files, has_body = await _multipart_request_parts(
            request, iface
        )
    else:
        body = await request.body() if body_announced else b""
        if len(body) > config.PROXY_MAX_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="请求体超过平台代理上限")
        has_body = bool(body)
        if has_body:
            try:
                body = publication.merge_caller_body(iface, body)
            except publication.PublicationBodyError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    if has_body and not iface.get("proxy_body_enabled"):
        if form is not None:
            await form.close()
        raise HTTPException(status_code=400, detail="该接口未开放请求 Body")

    managed_content_type = {
        "json": "application/json; charset=utf-8",
        "form": "application/x-www-form-urlencoded",
    }.get(body_type)
    override_args = {
        "query_params": query_items,
        "headers": header_items,
        "content_type": managed_content_type if has_body else None,
        "source": "http_proxy",
        "proxy_key_id": key_row["id"],
        "proxy_key_name": key_row["name"],
        "source_ip": request.client.host if request.client else None,
    }
    if multipart_fields is not None or multipart_files is not None:
        override_args["multipart_fields"] = multipart_fields or []
        override_args["files"] = multipart_files or []
    elif has_body:
        override_args["body"] = body
    overrides = executor.RequestOverrides(**override_args)
    try:
        result = await anyio.to_thread.run_sync(
            lambda: executor.run_interface(
                iface, overrides, include_response_content=True
            )
        )
    finally:
        if form is not None:
            await form.close()

    if result.get("status_code") is None:
        status = {
            "timeout": 504,
            "overloaded": 503,
        }.get(result.get("error_type"), 502)
        headers = {"Retry-After": "1"} if result.get("error_type") == "overloaded" else None
        return JSONResponse(
            status_code=status,
            content={
                "detail": result.get("error") or "真实接口调用失败",
                "run_id": result.get("run_id"),
            },
            headers=headers,
        )

    return Response(
        content=(
            b""
            if request.method.upper() == "HEAD"
            else (result.get("response_content") or b"")
        ),
        status_code=result["status_code"],
        headers=_response_headers(result.get("response_headers") or {}),
        media_type=None,
    )
