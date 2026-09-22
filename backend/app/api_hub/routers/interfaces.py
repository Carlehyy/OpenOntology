import json
import math
from datetime import datetime, timedelta, timezone
from typing import List

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.datastructures import FormData, UploadFile

from app.auth.models import User
from app.deps import get_current_user
from .. import config, db, executor
from ..interface_contracts import (
    _ALLOWED_BODY_TYPES,
    _ALLOWED_METHODS,
    _HEADER_NAME_RE,
    DeleteGroupBody,
    FileField,
    InterfaceIn,
    InterfaceParameter,
    KV,
    PreviewInterfaceIn,
)
from ..interface_service import (
    _PROXY_RESERVED_HEADERS,
    _PROXY_SLUG_RE,
    _RESERVED_GROUP,
    _check_group_name,
    _dump_kv,
    _get_or_404,
    _is_admin,
    _load_json_list,
    _normalize_publish_keys,
    _row_to_dict,
    _validate_proxy_publish,
    apply_http_publication,
    auto_http_publication as persist_auto_http_publication,
    create_interface,
    delete_group,
    delete_interface,
    move_interface as persist_move_interface,
    update_interface,
)

router = APIRouter(prefix="/interfaces", tags=["api-hub-interfaces"])

_SLOW_RUN_MS = 500
_RAW_RESPONSE_BLOCKLIST = _PROXY_RESERVED_HEADERS | {
    "content-encoding",  # requests transparently decompresses upstream content
    "set-cookie",       # never write an upstream cookie into the platform origin
}


def _body_form_pairs(text: str) -> list[tuple[str, str]]:
    pairs = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            pairs.append((key, value.strip()))
    return pairs


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


def _raw_run_response(result: dict) -> Response:
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
    headers = {
        key: value
        for key, value in (result.get("response_headers") or {}).items()
        if key.lower() not in _RAW_RESPONSE_BLOCKLIST
    }
    headers.update(
        {
            "X-Api-Hub-Upstream": "1",
            "X-Api-Hub-Run-Id": str(result.get("run_id") or ""),
            "X-Api-Hub-Elapsed-Ms": str(result.get("elapsed_ms") or 0),
            "X-Api-Hub-Relogin": "1" if result.get("relogin") else "0",
        }
    )
    return Response(
        content=result.get("response_content") or b"",
        status_code=result["status_code"],
        headers=headers,
        media_type=None,
    )


@router.get("")
def list_interfaces(current_user: User = Depends(get_current_user)):
    with db.get_conn() as conn:
        if _is_admin(current_user):
            rows = conn.execute(
                "SELECT * FROM interfaces ORDER BY group_name, sort_order, id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM interfaces WHERE created_by = ? "
                "ORDER BY group_name, sort_order, id",
                (current_user.id,),
            ).fetchall()
    return [_row_to_dict(r) for r in rows]


@router.post("")
def create(body: InterfaceIn, current_user: User = Depends(get_current_user)):
    return create_interface(body, user=current_user)


@router.post("/preview-run")
def preview_run(body: PreviewInterfaceIn, current_user: User = Depends(get_current_user)):
    """执行当前编辑器草稿，不隐式保存接口配置。"""
    if body.id is not None:
        with db.get_conn() as conn:
            _get_or_404(conn, body.id, user=current_user)
    iface = body.model_dump(mode="json")
    return executor.run_interface(
        iface, executor.RequestOverrides(source="ui", actor=current_user)
    )


@router.post("/preview-run/raw")
async def preview_run_raw(request: Request, current_user: User = Depends(get_current_user)):
    """Execute the editor draft and return the upstream bytes unchanged.

    JSON requests cover ordinary bodies. Multipart requests carry the serialized
    draft in ``__interface`` and runtime files under their configured field names.
    """
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > config.PROXY_MAX_REQUEST_BYTES:
                raise HTTPException(status_code=413, detail="请求体超过平台调用上限")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Content-Length 无效") from exc

    form: FormData | None = None
    overrides = executor.RequestOverrides(source="ui", actor=current_user)
    try:
        content_type = (request.headers.get("content-type") or "").lower()
        if content_type.startswith("multipart/form-data"):
            form = await request.form(max_files=50, max_fields=200)
            draft_json = form.get("__interface")
            if not isinstance(draft_json, str):
                raise HTTPException(status_code=422, detail="缺少接口调用配置")
            try:
                body = PreviewInterfaceIn.model_validate_json(draft_json)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="接口调用配置无效") from exc

            configured = {item.key: item for item in body.file_fields if item.key}
            file_counts: dict[str, int] = {}
            files: list[executor.RequestFile] = []
            total_size = len(draft_json.encode("utf-8"))
            for field_name, value in form.multi_items():
                if field_name == "__interface":
                    continue
                if not isinstance(value, UploadFile):
                    raise HTTPException(status_code=400, detail=f"文件字段 {field_name} 格式无效")
                definition = configured.get(field_name)
                if definition is None:
                    raise HTTPException(status_code=400, detail=f"文件字段未在接口中配置：{field_name}")
                file_counts[field_name] = file_counts.get(field_name, 0) + 1
                if file_counts[field_name] > 1 and not definition.multiple:
                    raise HTTPException(status_code=400, detail=f"文件字段不允许多文件：{field_name}")
                if not _matches_file_accept(
                    value.filename or "", value.content_type or "", definition.accept
                ):
                    raise HTTPException(status_code=400, detail=f"文件类型不符合字段限制：{field_name}")
                size = value.size
                if size is not None:
                    total_size += size
                files.append(
                    executor.RequestFile(
                        field_name=field_name,
                        filename=value.filename or "upload",
                        stream=value.file,
                        content_type=value.content_type or "application/octet-stream",
                        size=size,
                    )
                )
            if total_size > config.PROXY_MAX_REQUEST_BYTES:
                raise HTTPException(status_code=413, detail="请求体超过平台调用上限")
            overrides.multipart_fields = _body_form_pairs(body.body_content)
            overrides.files = files
        else:
            try:
                body = PreviewInterfaceIn.model_validate(await request.json())
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="接口调用配置无效") from exc
            if body.body_type == "multipart":
                overrides.multipart_fields = _body_form_pairs(body.body_content)
                overrides.files = []

        if body.id is not None:
            with db.get_conn() as conn:
                _get_or_404(conn, body.id, user=current_user)
        iface = body.model_dump(mode="json")
        result = await anyio.to_thread.run_sync(
            lambda: executor.run_interface(
                iface, overrides, include_response_content=True
            )
        )
        return _raw_run_response(result)
    finally:
        if form is not None:
            await form.close()


@router.get("/{iid}")
def get_interface(iid: int, current_user: User = Depends(get_current_user)):
    with db.get_conn() as conn:
        row = _get_or_404(conn, iid, user=current_user)
    return _row_to_dict(row)


@router.put("/{iid}")
def update(iid: int, body: InterfaceIn, current_user: User = Depends(get_current_user)):
    return update_interface(iid, body, user=current_user)


@router.delete("/{iid}")
def remove(iid: int, current_user: User = Depends(get_current_user)):
    return delete_interface(iid, user=current_user)


class HttpPublishIn(BaseModel):
    enabled: bool = False
    slug: str = ""
    query_keys: List[str] = Field(default_factory=list)
    header_keys: List[str] = Field(default_factory=list)
    body_enabled: bool = False
    body_keys: List[str] = Field(default_factory=list)


class MoveBody(BaseModel):
    group_name: str = ""
    target_index: int = 0  # 0-based


@router.put("/{iid}/move")
def move_interface(iid: int, body: MoveBody, current_user: User = Depends(get_current_user)):
    """移动接口到指定分组的指定位置。后端重排该组所有接口的 sort_order。"""
    return persist_move_interface(
        iid,
        group_name=body.group_name,
        target_index=body.target_index,
        user=current_user,
    )


@router.post("/groups/delete")
def remove_group(body: DeleteGroupBody, current_user: User = Depends(get_current_user)):
    return delete_group(body, user=current_user)


@router.put("/{iid}/http-publication")
def set_http_publication(iid: int, body: HttpPublishIn, current_user: User = Depends(get_current_user)):
    """独立更新普通 HTTP 发布配置，不覆盖编辑器里其它接口字段。"""
    return apply_http_publication(
        iid,
        enabled=body.enabled,
        slug=body.slug,
        query_keys=body.query_keys,
        header_keys=body.header_keys,
        body_enabled=body.body_enabled,
        body_keys=body.body_keys,
        user=current_user,
    )


@router.post("/{iid}/http-publication/auto")
def auto_http_publication(iid: int, current_user: User = Depends(get_current_user)):
    """Infer a safe forwarding contract and publish without exposing protocol details."""
    return persist_auto_http_publication(iid, user=current_user)


@router.post("/{iid}/run")
def run(iid: int, current_user: User = Depends(get_current_user)):
    with db.get_conn() as conn:
        iface = _row_to_dict(_get_or_404(conn, iid, user=current_user))
    return executor.run_interface(
        iface, executor.RequestOverrides(source="ui", actor=current_user)
    )


@router.get("/{iid}/runs")
def list_runs(iid: int, current_user: User = Depends(get_current_user)):
    with db.get_conn() as conn:
        _get_or_404(conn, iid, user=current_user)
        rows = conn.execute(
            "SELECT id, ok, status_code, elapsed_ms, error, relogin, created_at "
            "FROM runs WHERE interface_id = ? ORDER BY id DESC",
            (iid,),
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("/{iid}/runs/{run_id}")
def get_run(iid: int, run_id: int, current_user: User = Depends(get_current_user)):
    with db.get_conn() as conn:
        _get_or_404(conn, iid, user=current_user)
        row = conn.execute(
            "SELECT * FROM runs WHERE id = ? AND interface_id = ?", (run_id, iid)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="调用记录不存在")
    d = dict(row)
    d["ok"] = bool(d["ok"])
    d["relogin"] = bool(d["relogin"])
    for k in ("request_snapshot", "response_headers"):
        try:
            d[k] = json.loads(d[k]) if d[k] else None
        except (json.JSONDecodeError, TypeError):
            pass
    return d


# ============================================================
#  全局调用历史（跨所有接口，供右栏日志面板使用）
#  独立 router，挂在 /api/runs，与上面的接口 CRUD 分开。
# ============================================================
runs_router = APIRouter(prefix="/runs", tags=["api-hub-runs"])


@runs_router.get("/overview")
def run_overview(timezone_offset_minutes: int = 0, current_user: User = Depends(get_current_user)):
    timezone_offset_minutes = max(-840, min(840, timezone_offset_minutes))
    local_now = datetime.now(timezone.utc) - timedelta(
        minutes=timezone_offset_minutes
    )
    today = local_now.date()
    start = today - timedelta(days=6)
    days = [(start + timedelta(days=i)).isoformat() for i in range(7)]
    sqlite_modifier = f"{-timezone_offset_minutes:+d} minutes"
    # Non-admin users only see their own interfaces and the runs against them.
    admin = _is_admin(current_user)
    with db.get_conn() as conn:
        if admin:
            total_interfaces = conn.execute("SELECT COUNT(*) FROM interfaces").fetchone()[0]
            executed = conn.execute("SELECT COUNT(DISTINCT interface_id) FROM runs").fetchone()[0]
            today_traffic = conn.execute(
                "SELECT COUNT(*) FROM runs "
                "WHERE date(datetime(created_at), ?) = ?",
                (sqlite_modifier, today.isoformat()),
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT date(datetime(created_at), ?) AS day, COUNT(*) AS count, "
                "SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) AS failed "
                "FROM runs WHERE date(datetime(created_at), ?) >= ? "
                "AND date(datetime(created_at), ?) <= ? "
                "GROUP BY date(datetime(created_at), ?)",
                (
                    sqlite_modifier,
                    sqlite_modifier, start.isoformat(),
                    sqlite_modifier, today.isoformat(),
                    sqlite_modifier,
                ),
            ).fetchall()
            recent_rows = conn.execute(
                "SELECT ok, elapsed_ms FROM runs "
                "WHERE date(datetime(created_at), ?) >= ? "
                "AND date(datetime(created_at), ?) <= ?",
                (
                    sqlite_modifier, start.isoformat(),
                    sqlite_modifier, today.isoformat(),
                ),
            ).fetchall()
        else:
            uid = current_user.id
            total_interfaces = conn.execute(
                "SELECT COUNT(*) FROM interfaces WHERE created_by = ?",
                (uid,),
            ).fetchone()[0]
            executed = conn.execute(
                "SELECT COUNT(DISTINCT r.interface_id) FROM runs r "
                "JOIN interfaces i ON i.id = r.interface_id "
                "WHERE i.created_by = ?",
                (uid,),
            ).fetchone()[0]
            today_traffic = conn.execute(
                "SELECT COUNT(*) FROM runs r "
                "JOIN interfaces i ON i.id = r.interface_id "
                "WHERE i.created_by = ? "
                "AND date(datetime(r.created_at), ?) = ?",
                (uid, sqlite_modifier, today.isoformat()),
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT date(datetime(r.created_at), ?) AS day, COUNT(*) AS count, "
                "SUM(CASE WHEN r.ok = 0 THEN 1 ELSE 0 END) AS failed "
                "FROM runs r JOIN interfaces i ON i.id = r.interface_id "
                "WHERE i.created_by = ? "
                "AND date(datetime(r.created_at), ?) >= ? "
                "AND date(datetime(r.created_at), ?) <= ? "
                "GROUP BY date(datetime(r.created_at), ?)",
                (
                    sqlite_modifier,
                    uid,
                    sqlite_modifier, start.isoformat(),
                    sqlite_modifier, today.isoformat(),
                    sqlite_modifier,
                ),
            ).fetchall()
            recent_rows = conn.execute(
                "SELECT r.ok, r.elapsed_ms FROM runs r "
                "JOIN interfaces i ON i.id = r.interface_id "
                "WHERE i.created_by = ? "
                "AND date(datetime(r.created_at), ?) >= ? "
                "AND date(datetime(r.created_at), ?) <= ?",
                (
                    uid,
                    sqlite_modifier, start.isoformat(),
                    sqlite_modifier, today.isoformat(),
                ),
            ).fetchall()
    by_day = {
        row["day"]: {"count": int(row["count"]), "failed": int(row["failed"] or 0)}
        for row in rows
    }
    daily = [
        {
            "date": day,
            "count": by_day.get(day, {}).get("count", 0),
            "failed": by_day.get(day, {}).get("failed", 0),
        }
        for day in days
    ]
    seven_day_traffic = len(recent_rows)
    seven_day_success = sum(1 for row in recent_rows if bool(row["ok"]))
    seven_day_failed = seven_day_traffic - seven_day_success
    latencies = sorted(
        int(row["elapsed_ms"])
        for row in recent_rows
        if row["elapsed_ms"] is not None
    )
    p95_elapsed_ms = (
        latencies[max(0, math.ceil(len(latencies) * 0.95) - 1)]
        if latencies else None
    )
    return {
        "total_interfaces": int(total_interfaces),
        "executed_interfaces": int(executed),
        "unexecuted_interfaces": max(0, int(total_interfaces) - int(executed)),
        "today_traffic": int(today_traffic),
        "seven_day_traffic": seven_day_traffic,
        "seven_day_success": seven_day_success,
        "seven_day_failed": seven_day_failed,
        "success_rate": round(
            seven_day_success * 100 / seven_day_traffic, 1
        ) if seven_day_traffic else 0,
        "p95_elapsed_ms": p95_elapsed_ms,
        "slow_threshold_ms": _SLOW_RUN_MS,
        "retention_limit_per_interface": config.MAX_RUNS_PER_INTERFACE,
        "daily": daily,
    }


@runs_router.get("")
def list_all_runs(
    keyword: str = "",
    start: str = "",
    end: str = "",
    result: str = "",
    page: int = 1,
    size: int = 20,
    current_user: User = Depends(get_current_user),
):
    """分页查询全局调用记录，按时间倒序（最新在顶）。

    - keyword：按接口名称模糊匹配（LIKE %keyword%）
    - start / end：时间范围，前端传入的完整 ISO 时间戳（已按用户本地
      时区换算为 UTC 边界）。用 SQLite datetime() 比较，兼容 Z / +00:00。
    - result：success / failed / slow，slow 表示耗时不低于 500ms。
    - page / size：分页，size 上限 100
    """
    page = max(page, 1)
    size = max(min(size, 100), 1)

    where = []
    params: list = []
    # Non-admin users only see runs against their own interfaces.
    if not _is_admin(current_user):
        where.append("i.created_by = ?")
        params.append(current_user.id)
    kw = keyword.strip()
    if kw:
        # H25：详情抽屉展示/复制的编号是原生数字 id——纯数字关键词在按接口名
        # 模糊匹配之外同时按 runs.id 精确匹配，复制给同事的编号可直接搜回。
        if kw.isdigit():
            where.append("(i.name LIKE ? OR r.id = ?)")
            params.extend([f"%{kw}%", int(kw)])
        else:
            where.append("i.name LIKE ?")
            params.append(f"%{kw}%")
    s = start.strip()
    if s:
        where.append("datetime(r.created_at) >= datetime(?)")
        params.append(s)
    e = end.strip()
    if e:
        where.append("datetime(r.created_at) <= datetime(?)")
        params.append(e)
    result_mode = result.strip().lower()
    if result_mode not in {"", "all", "success", "failed", "slow"}:
        raise HTTPException(status_code=400, detail="不支持的调用结果筛选")
    if result_mode == "success":
        where.append("r.ok = 1")
    elif result_mode == "failed":
        where.append("r.ok = 0")
    elif result_mode == "slow":
        where.append("r.elapsed_ms >= ?")
        params.append(_SLOW_RUN_MS)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    base_from = "FROM runs r JOIN interfaces i ON i.id = r.interface_id"
    with db.get_conn() as conn:
        total = conn.execute(
            "SELECT COUNT(*) " + base_from + where_sql, params
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT r.id, r.interface_id, i.name, i.method, r.ok, r.status_code, "
            "r.elapsed_ms, r.error, r.relogin, r.source, r.proxy_key_name, "
            "r.source_ip, r.created_at "
            + base_from + where_sql +
            " ORDER BY r.id DESC LIMIT ? OFFSET ?",
            params + [size, (page - 1) * size],
        ).fetchall()
    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "size": size,
    }
