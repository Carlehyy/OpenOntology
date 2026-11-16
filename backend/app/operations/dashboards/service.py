"""监控看板卡片写路径、全量排序与 Grafana 对账（冻结契约 §4/§5）。

并发约定（真实 PostgreSQL）：

- create 最终入库 / delete / 全量 reorder 在同一稳定命名空间取事务级
  advisory lock ``pg_advisory_xact_lock(20303, 147)``，commit/rollback 释放；
- 每个写事务先 ``SET LOCAL lock_timeout='5s'``：等待超时 rollback 并返回
  409 ``dashboard_write_busy``（可重试）；持有锁期间的 Grafana 调用受
  配置的 3..60 秒超时约束，不无限重试；
- 取锁顺序固定：域锁 → 按稳定 ID 顺序取行锁；禁止先行锁再申请域锁；
- 获域锁后重新读取全量集合/max，不使用锁前缓存（READ COMMITTED）。

SQLite 单测没有 advisory lock/lock_timeout（方言跳过）；跨进程并发证明
由真实 PostgreSQL 验收脚本承担，不以 SQLite 代替。
"""
from __future__ import annotations

import logging
import secrets
import uuid

from fastapi import HTTPException
from sqlalchemy import func, text, update as sa_update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.operations import models as m
from app.operations.dashboards import schemas
from app.operations.grafana import client as grafana_client

logger = logging.getLogger("app.operations.dashboards")

STATUS_OK = "ok"
STATUS_MISSING = "missing"
STATUS_UNKNOWN = "unknown"

# 事务级域锁键（固定双 int 命名空间；所有相同域操作必须使用同一键）。
_ADVISORY_LOCK_KEY = (20303, 147)
_LOCK_TIMEOUT_SECONDS = 5


class _WriteBusy(Exception):
    """PG 域锁等待超时（已 rollback，可重试）。"""


def _write_required(user) -> None:
    if str(getattr(user, "role", "viewer") or "viewer") not in {"admin", "editor"}:
        raise HTTPException(
            403,
            detail={"code": "DASHBOARD_WRITE_DENIED", "message": "当前角色只能查看监控看板"},
        )


def _is_lock_timeout(exc: SQLAlchemyError) -> bool:
    original = getattr(exc, "orig", None)
    code = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    if code in {"55P03", "57014"}:
        return True
    return "lock_timeout" in str(original or "")


def _set_write_lock_timeout(db: Session) -> None:
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT_SECONDS}s'"))


def _begin_write_transaction(db: Session) -> None:
    """写事务统一入口：有限 lock_timeout + 事务级域锁（仅 PostgreSQL）。"""
    if db.get_bind().dialect.name != "postgresql":
        return
    try:
        _set_write_lock_timeout(db)
        db.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                f"{_ADVISORY_LOCK_KEY[0]}, {_ADVISORY_LOCK_KEY[1]})"
            )
        )
    except SQLAlchemyError as exc:
        db.rollback()
        if _is_lock_timeout(exc):
            raise _WriteBusy() from exc
        raise


def _write_busy() -> HTTPException:
    return HTTPException(
        409,
        detail={"code": "dashboard_write_busy", "message": "看板写入繁忙，请稍后重试"},
    )


def _raise_grafana_error(exc: Exception) -> None:
    """把客户端异常映射为脱敏业务错误；不携带 URL/凭据/响应体。"""
    if isinstance(exc, grafana_client.GrafanaNotConfigured):
        raise HTTPException(
            503,
            detail={"code": "grafana_not_configured", "message": "Grafana 看板服务未配置，请联系管理员"},
        ) from exc
    if isinstance(exc, grafana_client.GrafanaUnavailable):
        raise HTTPException(
            502,
            detail={"code": "grafana_unavailable", "message": "Grafana 看板服务暂不可用，请稍后重试"},
        ) from exc
    if isinstance(exc, grafana_client.GrafanaOperationFailed):
        raise HTTPException(
            502,
            detail={
                "code": "grafana_operation_failed",
                "message": "Grafana 看板操作失败，请稍后重试",
                "grafana_status": exc.status,
            },
        ) from exc
    raise exc


def _compensate_remote(grafana_uid: str, *, stage: str) -> None:
    """远端已创建但本地失败时的尽力补偿：删除该 UID（404 视为成功）。

    补偿仅记录脱敏日志（UID + 阶段 + 结果），绝不记录凭据/URL/响应体。
    网络/崩溃窗口仍可能留下孤儿（延迟提交等模糊成功），不宣称零残留；
    孤儿按日志 UID 与全量 UID 对比后在 Grafana 原生界面人工处置。
    """
    try:
        client = grafana_client.build_client()
        removed = client.delete_dashboard(grafana_uid)
        logger.warning(
            "GRAFANA_COMPENSATION stage=%s uid=%s result=%s",
            stage, grafana_uid, "deleted" if removed else "kept",
        )
    except Exception:  # noqa: BLE001 - 补偿尽力而为，失败必须留痕
        logger.error(
            "GRAFANA_COMPENSATION_FAILED stage=%s uid=%s", stage, grafana_uid
        )


def _require_dashboard(
    db: Session, dashboard_id: str, *, for_update: bool = False
) -> m.OperationsDashboard:
    query = db.query(m.OperationsDashboard).filter(
        m.OperationsDashboard.id == dashboard_id
    )
    if for_update:
        query = query.with_for_update().populate_existing()
    row = query.one_or_none()
    if row is None:
        raise HTTPException(
            404, detail={"code": "dashboard_not_found", "message": "看板卡片不存在"}
        )
    return row


def _next_sort_order(max_order: int | None) -> int:
    # 首条 max() 为 None 得 0；已有 sort_order=0 时必须追加为 1（不能用
    # `max_order or -1`，0 是假值会错误重用 0）。
    return 0 if max_order is None else int(max_order) + 1


def dashboard_card(row: m.OperationsDashboard, grafana_status: str) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description or "",
        "icon": row.icon,
        "grafana_uid": row.grafana_uid,
        "grafana_status": grafana_status,
        "edit_path": f"/grafana/d/{row.grafana_uid}?orgId=1",
        "preview_path": f"/grafana/d/{row.grafana_uid}?orgId=1&kiosk",
        "sort_order": int(row.sort_order),
        "revision": int(row.revision or 1),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def grafana_availability_and_statuses(
    uids: list[str],
) -> tuple[bool, dict[str, str]]:
    """与 Grafana 对账；返回 (可用性, {uid: 三态状态})。

    三态规则：所有块都成功且结构有效，请求的 UID 不在结果集中才判
    ``missing``；任何一块连接/超时/鉴权/格式错误（含凭据缺失），全列表
    ``unknown`` 且 ``grafana_available=false``，不能把成功块与失败块拼成
    虚假 missing。空列表也独立判定可用性。
    """
    try:
        client = grafana_client.build_client()
        if not uids:
            return client.search_available(), {}
        found: set[str] = set()
        chunk_size = grafana_client.SEARCH_CHUNK_SIZE
        for start in range(0, len(uids), chunk_size):
            chunk = uids[start : start + chunk_size]
            found |= client.search_uids(chunk)
    except (
        grafana_client.GrafanaNotConfigured,
        grafana_client.GrafanaUnavailable,
        grafana_client.GrafanaOperationFailed,
    ):
        if not uids:
            return False, {}
        return False, {uid: STATUS_UNKNOWN for uid in uids}
    return True, {
        uid: (STATUS_OK if uid in found else STATUS_MISSING) for uid in uids
    }


def dashboard_status(grafana_uid: str) -> str:
    """单个 UID 的三态状态；探测失败返回 unknown。"""
    _available, statuses = grafana_availability_and_statuses([grafana_uid])
    return statuses.get(grafana_uid, STATUS_UNKNOWN)


def list_dashboards(db: Session) -> dict:
    rows = (
        db.query(m.OperationsDashboard)
        .order_by(
            m.OperationsDashboard.sort_order.asc(),
            m.OperationsDashboard.updated_at.desc(),
            m.OperationsDashboard.id.asc(),
        )
        .all()
    )
    available, statuses = grafana_availability_and_statuses(
        [row.grafana_uid for row in rows]
    )
    return {
        "grafana_available": available,
        "total": len(rows),
        "items": [
            dashboard_card(row, statuses.get(row.grafana_uid, STATUS_UNKNOWN))
            for row in rows
        ],
    }


def create_dashboard(
    db: Session, body: schemas.DashboardCreate, user
) -> m.OperationsDashboard:
    """创建卡片：远端先建空看板，域锁内线性化追加，失败补偿。"""
    _write_required(user)
    name = body.name.strip()
    if not name:
        raise HTTPException(
            422,
            detail={"code": "dashboard_name_required", "message": "看板卡片名称不能为空"},
        )
    if body.icon not in schemas.DASHBOARD_ICONS:
        raise HTTPException(
            422,
            detail={"code": "dashboard_icon_invalid", "message": "看板卡片图标无效"},
        )
    dashboard_id = str(uuid.uuid4())
    grafana_uid = f"oo-{secrets.token_hex(6)}"
    try:
        client = grafana_client.build_client()
        client.ensure_folder()
        client.create_dashboard(grafana_uid, name)
    except (
        grafana_client.GrafanaNotConfigured,
        grafana_client.GrafanaUnavailable,
        grafana_client.GrafanaOperationFailed,
    ) as exc:
        # UID 已在远端请求前生成：网络超时可能是模糊成功，必须留出
        # 脱敏 UID 供后续 Grafana 孤儿对账；不记录异常文本（可能含 URL/响应体）。
        logger.warning(
            "GRAFANA_CREATE_FAILED stage=create uid=%s error_type=%s",
            grafana_uid,
            type(exc).__name__,
        )
        _raise_grafana_error(exc)
    try:
        # 并发创建可并行请求 Grafana，最终追加在域锁内线性化。
        _begin_write_transaction(db)
        max_order = db.query(func.max(m.OperationsDashboard.sort_order)).scalar()
        row = m.OperationsDashboard(
            id=dashboard_id,
            name=name,
            description=body.description.strip(),
            icon=body.icon,
            grafana_uid=grafana_uid,
            sort_order=_next_sort_order(max_order),
        )
        db.add(row)
        db.commit()
    except _WriteBusy as exc:
        db.rollback()
        # 远端已创建而锁等待超时：仍执行补偿后返回 409。
        _compensate_remote(grafana_uid, stage="create_lock_timeout")
        raise _write_busy() from exc
    except Exception as exc:
        db.rollback()
        if isinstance(exc, SQLAlchemyError) and _is_lock_timeout(exc):
            _compensate_remote(grafana_uid, stage="create_lock_timeout")
            raise _write_busy() from exc
        _compensate_remote(grafana_uid, stage="create_persist_failed")
        raise HTTPException(
            500,
            detail={
                "code": "dashboard_persist_failed",
                "message": "看板卡片本地保存失败，已尝试清理远端看板，请稍后重试",
            },
        ) from exc
    db.refresh(row)
    return row


def update_dashboard(
    db: Session,
    dashboard_id: str,
    body: schemas.DashboardUpdate,
    user,
) -> m.OperationsDashboard:
    """仅更新本地元数据与 revision；不请求任何 Grafana dashboard 写 API。"""
    _write_required(user)
    try:
        _set_write_lock_timeout(db)
        row = _require_dashboard(db, dashboard_id, for_update=True)
        if body.expected_revision is not None and body.expected_revision != row.revision:
            raise HTTPException(
                409,
                detail={
                    "code": "dashboard_revision_conflict",
                    "message": "看板卡片已被修改，请刷新后重试",
                    "revision": row.revision,
                },
            )
        changed = False
        if body.name is not None:
            name = body.name.strip()
            if not name:
                raise HTTPException(
                    422,
                    detail={"code": "dashboard_name_required", "message": "看板卡片名称不能为空"},
                )
            row.name = name
            changed = True
        if body.description is not None:
            row.description = body.description.strip()
            changed = True
        if body.icon is not None:
            if body.icon not in schemas.DASHBOARD_ICONS:
                raise HTTPException(
                    422,
                    detail={"code": "dashboard_icon_invalid", "message": "看板卡片图标无效"},
                )
            row.icon = body.icon
            changed = True
        if not changed:
            # 空对象为无操作：不改 revision/updated_at。
            return row
        # 有效非空元数据更新即使值相同也按一次接受的写入增加 revision（与
        # 运营报告写入语义一致）。
        row.revision = int(row.revision or 1) + 1
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        if _is_lock_timeout(exc):
            raise _write_busy() from exc
        raise
    db.refresh(row)
    return row


def delete_dashboard(db: Session, dashboard_id: str, user) -> None:
    """删除：域锁 → 行锁 → Grafana 删除（404 幂等成功）→ 本地删除提交。"""
    _write_required(user)
    row: m.OperationsDashboard | None = None
    try:
        _begin_write_transaction(db)
        row = _require_dashboard(db, dashboard_id, for_update=True)
        try:
            client = grafana_client.build_client()
            client.delete_dashboard(row.grafana_uid)
        except (
            grafana_client.GrafanaNotConfigured,
            grafana_client.GrafanaUnavailable,
            grafana_client.GrafanaOperationFailed,
        ) as exc:
            _raise_grafana_error(exc)
        db.delete(row)
        db.commit()
    except _WriteBusy as exc:
        db.rollback()
        raise _write_busy() from exc
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        # Grafana 已删但本地提交失败：保留卡片（后续列表显示 missing，
        # 重试遇远端 404 完成删除）。
        db.rollback()
        if isinstance(exc, SQLAlchemyError) and _is_lock_timeout(exc):
            raise _write_busy() from exc
        logger.warning(
            "DASHBOARD_DELETE_PERSIST_FAILED id=%s uid=%s",
            dashboard_id,
            row.grafana_uid if row is not None else "unknown",
        )
        raise HTTPException(
            500,
            detail={
                "code": "dashboard_persist_failed",
                "message": "看板卡片本地删除失败，卡片已保留，请稍后重试",
            },
        ) from exc


def reorder_dashboards(
    db: Session, body: schemas.DashboardReorder, user
) -> list[m.OperationsDashboard]:
    """全量重排：必须为库内全部 ID 的精确排列，整体提交或整体失败。"""
    _write_required(user)
    try:
        _begin_write_transaction(db)
        # 域锁后按稳定 ID 顺序取行锁。
        rows = (
            db.query(m.OperationsDashboard)
            .with_for_update()
            .order_by(m.OperationsDashboard.id.asc())
            .all()
        )
        by_id = {row.id: row for row in rows}
        requested = list(body.dashboard_ids)
        if len(requested) != len(rows) or set(requested) != set(by_id):
            raise HTTPException(
                409,
                detail={
                    "code": "dashboard_reorder_mismatch",
                    "message": "排序列表必须包含全部看板卡片且不能重复或包含未知卡片",
                },
            )
        # 当前列表顺序 = sort_order ASC, updated_at DESC, id ASC（稳定多趟排序）。
        ordered = sorted(rows, key=lambda row: row.id)
        ordered.sort(key=lambda row: row.updated_at, reverse=True)
        ordered.sort(key=lambda row: row.sort_order)
        current_position = {row.id: index for index, row in enumerate(ordered)}
        requested_position = {
            dashboard_id: index for index, dashboard_id in enumerate(requested)
        }
        for dashboard_id, row in by_id.items():
            new_index = requested_position[dashboard_id]
            if current_position[dashboard_id] != new_index:
                row.sort_order = new_index
                row.revision = int(row.revision or 1) + 1
            elif row.sort_order != new_index:
                # 位置未变但 sort_order 有删除遗留空洞：仅压缩序号，不增加
                # revision/updated_at（显式保留原 updated_at，抑制 ORM onupdate）。
                db.execute(
                    sa_update(m.OperationsDashboard)
                    .where(m.OperationsDashboard.id == dashboard_id)
                    .values(sort_order=new_index, updated_at=row.updated_at)
                )
        db.commit()
    except _WriteBusy as exc:
        db.rollback()
        raise _write_busy() from exc
    except HTTPException:
        db.rollback()
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        if _is_lock_timeout(exc):
            raise _write_busy() from exc
        raise
    db.expire_all()
    refreshed = (
        db.query(m.OperationsDashboard)
        .order_by(
            m.OperationsDashboard.sort_order.asc(),
            m.OperationsDashboard.updated_at.desc(),
            m.OperationsDashboard.id.asc(),
        )
        .all()
    )
    return refreshed
