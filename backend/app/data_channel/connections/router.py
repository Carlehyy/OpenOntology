"""
v2 Connection 管理 API
POST   /api/v2/connections
GET    /api/v2/connections
GET    /api/v2/connections/{id}
POST   /api/v2/connections/{id}/test
DELETE /api/v2/connections/{id}
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from app.database import SessionLocal
from app.deps import get_current_user
from app.data_channel.connections.models import Connection
from app.services.connection.registry import get_connector

router = APIRouter(dependencies=[Depends(get_current_user)])
logger = logging.getLogger(__name__)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Pydantic 模式 ─────────────────────────────────────────────

class ConnectionCreate(BaseModel):
    name: str
    kind: str  # file | mysql | postgres | mongo | rest
    config: dict  # 明文连接配置 (服务端加密)


class ConnectionResponse(BaseModel):
    id: str
    name: str
    kind: str
    status: str

    class Config:
        from_attributes = True


# ── 端点 ──────────────────────────────────────────────────────

@router.post("", response_model=ConnectionResponse, status_code=201)
def create_connection(body: ConnectionCreate, db: Session = Depends(get_db)):
    """创建连接。config 加密后存储。"""
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "连接名称不能为空")
    # 重名连接会让依赖连接名的数据集/任务无法区分（商业化审查 D-005）
    if db.query(Connection).filter(Connection.name == name).first():
        raise HTTPException(409, f"已存在同名连接「{name}」，请更换连接名称")
    from app.services import encryption_service
    encrypted_config = {"_encrypted": encryption_service.encrypt(json.dumps(body.config))}

    conn = Connection(
        name=name,
        kind=body.kind,
        config=encrypted_config,
        status="inactive",
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)
    return conn


@router.get("", response_model=list[ConnectionResponse])
def list_connections(db: Session = Depends(get_db)):
    return db.query(Connection).all()


@router.get("/{connection_id}", response_model=ConnectionResponse)
def get_connection(connection_id: str, db: Session = Depends(get_db)):
    conn = db.query(Connection).filter(Connection.id == connection_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    return conn


class TestConfigBody(BaseModel):
    type: str
    config: dict = {}


@router.post("/test-config")
def test_connection_config(body: TestConfigBody):
    """测试连接配置（无需先创建 Connection，供 Builder 使用）"""
    try:
        connector = get_connector(body.type, body.config)
        ok = connector.test_connection()
        return {"success": ok}
    except Exception as e:
        return {"success": False, "detail": str(e)}


@router.post("/{connection_id}/test")
def test_connection(connection_id: str, db: Session = Depends(get_db)):
    """连接测试。尝试真实连接并返回结果。"""
    conn = db.query(Connection).filter(Connection.id == connection_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")

    from app.services import encryption_service
    raw = conn.config.get("_encrypted", "")
    try:
        config = json.loads(encryption_service.decrypt(raw)) if raw else conn.config
    except Exception:
        config = conn.config

    try:
        connector = get_connector(conn.kind, config)
        ok = connector.test_connection()
        conn.status = "active" if ok else "error"
        db.commit()
        return {"success": ok, "status": conn.status}
    except Exception as e:
        conn.status = "error"
        db.commit()
        return {"success": False, "status": "error", "detail": str(e)}


@router.delete("/{connection_id}", status_code=204)
def delete_connection(connection_id: str, db: Session = Depends(get_db)):
    conn = db.query(Connection).filter(Connection.id == connection_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    # 同步产出的数据集以 source_connection_id 引用连接（外键无级联），直接删除
    # 会触发数据库外键错误（生产 500，商业化审查 D-006）；对齐 delete_dataset /
    # delete_pipeline 的先例：被依赖时明确 409 并列出依赖明细。
    from sqlalchemy import func

    from app.data_channel.datasets.models import Dataset
    dependent_count = (
        db.query(func.count(Dataset.id))
        .filter(Dataset.source_connection_id == connection_id)
        .scalar() or 0
    )
    if dependent_count:
        preview = [
            {"id": ds.id, "name": ds.name, "kind": ds.kind}
            for ds in (
                db.query(Dataset)
                .filter(Dataset.source_connection_id == connection_id)
                .order_by(Dataset.created_at.desc())
                .limit(5)
                .all()
            )
        ]
        raise HTTPException(409, detail={
            "message": (
                f"连接被 {dependent_count} 个同步数据集引用，删除前请先在"
                "「数据资产」中删除这些数据集"
            ),
            "datasets": preview,
            "total": dependent_count,
        })
    db.delete(conn)
    db.commit()
    # 数据源元数据/样例缓存随连接删除失效（best-effort，旧键靠 TTL 兜底）
    from app.data_channel.sync_tasks import cache as _sync_cache

    _sync_cache.invalidate_source(connection_id)


@router.post("/{connection_id}/schedule")
def set_schedule(connection_id: str, cron_expr: str, db: Session = Depends(get_db)):
    """为连接设置 Cron 调度表达式"""
    from app.data_channel.sync_tasks.cron_service import CronService
    svc = CronService()
    if not svc.validate_cron(cron_expr):
        raise HTTPException(400, f"无效的 cron 表达式: {cron_expr}")

    conn = db.query(Connection).filter(Connection.id == connection_id).first()
    if not conn:
        raise HTTPException(404, "Connection not found")

    result = svc.schedule_connection_sync(connection_id, cron_expr)
    config = conn.config or {}
    config["schedule_cron"] = cron_expr
    conn.config = config
    db.commit()
    return result


class SyncBody(BaseModel):
    mode: str = "full"           # full | delta
    resource: Optional[str] = None
    async_mode: bool = False     # True 时派发 Celery，否则按用户选择同步执行


@router.post("/{connection_id}/sync")
def trigger_sync(connection_id: str, body: SyncBody | None = None,
                 db: Session = Depends(get_db)):
    """手动触发数据同步，把连接数据落地为 Dataset 版本。

    ``async_mode=False`` 是用户显式选择的同步执行；``async_mode=True`` 必须
    成功投递 Celery，失败时返回 503，不能擅自改成 API 进程内执行。
    """
    conn = db.query(Connection).filter(Connection.id == connection_id).first()
    if not conn:
        raise HTTPException(404, "Connection not found")

    body = body or SyncBody()

    if body.async_mode:
        try:
            from app.data_channel.pipeline_tasks.dispatch import (
                CONNECTION_SYNC_SUBJECT, dispatch_task,
            )
            # 透传 resource；丢失它会退回“第一个资源”，使调用方请求的资源
            # 与最终 Dataset 身份不一致。
            dispatch_task(CONNECTION_SYNC_SUBJECT, {
                "connection_id": connection_id,
                "mode": body.mode,
                "resource": body.resource or "",
            })
        except Exception as exc:
            logger.error(
                "Connection %s 异步同步任务投递失败；任务未执行（%s）",
                connection_id,
                type(exc).__name__,
            )
            raise HTTPException(
                503,
                "NATS 后台任务服务不可用，异步同步任务未投递",
            ) from exc
        return {"connection_id": connection_id, "status": "sync_triggered"}

    # 同步执行
    from app.tasks.v2.connection_sync import sync_connection
    result = sync_connection(connection_id, mode=body.mode,
                             resource=body.resource, db=db)
    if result.get("status") == "error":
        raise HTTPException(502, result.get("error") or "连接同步失败")
    result["connection_id"] = connection_id
    return result
