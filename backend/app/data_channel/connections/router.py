"""
v2 Connection 管理 API
POST   /api/v2/connections
GET    /api/v2/connections
GET    /api/v2/connections/{id}
POST   /api/v2/connections/{id}/test
PATCH  /api/v2/connections/{id}/config
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
    if body.kind == "rest":
        # 防御纵深（商业化审查 D-003 遗留建议）：前端已有保存拦截，直连
        # API 仍可持久化坏 config，复用连接器归一化在服务端再拦一道。
        from app.data_channel.connections.rest_connector import RestConnector
        try:
            RestConnector._normalize_config(body.config)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
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


def _decrypt_connection_config(conn: Connection) -> dict:
    """解出连接的明文配置；解不开时 fail-closed，绝不静默覆盖成新配置。"""
    raw = (conn.config or {}).get("_encrypted", "")
    if not raw:
        return dict(conn.config or {})
    from app.services import encryption_service
    try:
        return json.loads(encryption_service.decrypt(raw))
    except Exception as exc:
        raise HTTPException(
            503, "既有连接配置无法解密，凭据轮换未执行；请重建连接",
        ) from exc


class ConnectionConfigUpdate(BaseModel):
    # 部分键合并语义：仅替换请求中出现的键，未出现的键保留服务端现值。
    # 响应永不回填 config（明文/密文都不回显），凭据只进不出。
    config: dict


def _authorize_config_rotation(db: Session, user) -> None:
    """Require asset-lake write access before revealing connection existence."""
    from app.auth.permissions import user_has_menu_access

    if not user or not user.is_active or not user_has_menu_access(db, user, "data.structured"):
        raise HTTPException(403, "没有数据资产湖访问权限，不能轮换连接凭据")
    if user.role not in {"admin", "editor"}:
        raise HTTPException(403, "只有管理员和编辑者可轮换连接凭据")


@router.patch("/{connection_id}/config", response_model=ConnectionResponse)
def rotate_connection_config(connection_id: str, body: ConnectionConfigUpdate,
                             db: Session = Depends(get_db),
                             user=Depends(get_current_user)):
    """连接维护/凭据轮换：部分更新加密配置并把连通状态重置为未激活。

    响应只有 id/name/kind/status，不会以任何形式回显配置内容。
    """
    _authorize_config_rotation(db, user)
    conn = db.query(Connection).filter(Connection.id == connection_id).first()
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    if not body.config:
        raise HTTPException(400, "请至少填写一个要更新的配置项")
    merged = {**_decrypt_connection_config(conn), **body.config}
    if conn.kind == "rest":
        # 与创建路径同一道服务端归一化拦截，防止直连 API 写入坏 config。
        from app.data_channel.connections.rest_connector import RestConnector
        try:
            RestConnector._normalize_config(merged)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    from app.services import encryption_service
    conn.config = {"_encrypted": encryption_service.encrypt(json.dumps(merged))}
    # 配置已变，既有连通结论作废，必须重新测试才可能回到 active。
    conn.status = "inactive"
    db.commit()
    db.refresh(conn)
    # 数据源元数据/样例缓存按连接版本键失效：轮换可能指向不同库/账号，
    # 旧缓存不能冒充新配置的发现结果（best-effort，失败靠 TTL 兜底）。
    from app.data_channel.sync_tasks import cache as _sync_cache

    _sync_cache.invalidate_source(connection_id)
    return conn


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
    # 受控注销：持连接行锁再做依赖检查，与集成资产登记（register_asset 同样
    # 持该行锁）在数据库层串行，避免"检查后新增引用"的竞态。
    conn = (
        db.query(Connection)
        .filter(Connection.id == connection_id)
        .with_for_update()
        .first()
    )
    if not conn:
        raise HTTPException(status_code=404, detail="Connection not found")
    blockers = _connection_decommission_blockers(db, conn)
    if blockers is not None:
        raise HTTPException(status_code=409, detail=blockers)
    db.delete(conn)
    db.commit()
    # 数据源元数据/样例缓存随连接删除失效（best-effort，旧键靠 TTL 兜底）
    from app.data_channel.sync_tasks import cache as _sync_cache

    _sync_cache.invalidate_source(connection_id)


def _connection_decommission_blockers(db: Session, conn: Connection) -> dict | None:
    """受控注销前置检查：零数据集 / 零集成资产 / 零历史 / 零映射消费引用 / 零任务。

    任一非零即返回结构化 409 明细（调用方负责抛出）；注销路径绝不删除任何
    历史或审计记录来"清障"。映射消费与流水线消费沿用 datasets 域既有端口
    （ontologies.mappings.consumers / datasets.consumers），不新增跨域 import。
    """
    from sqlalchemy import func

    from app.data_channel.datasets.models import Dataset, DatasetVersion, IntegratedSyncRun
    from app.data_channel.integrated.models import IntegratedAsset, IntegratedMutation
    from app.data_channel.pipeline_tasks.models import PipelineTask
    from app.data_channel.pipelines.models import Pipeline
    from app.ontologies.mappings.consumers import dataset_mapping_bindings

    datasets = (
        db.query(Dataset)
        .filter(Dataset.source_connection_id == conn.id)
        .order_by(Dataset.created_at.desc())
        .all()
    )
    assets = db.query(IntegratedAsset).filter(IntegratedAsset.connection_id == conn.id).all()
    dataset_ids = [item.id for item in datasets]
    if dataset_ids:
        version_count = (
            db.query(func.count(DatasetVersion.id))
            .filter(DatasetVersion.dataset_id.in_(dataset_ids))
            .scalar() or 0
        )
        run_count = (
            db.query(func.count(IntegratedSyncRun.id))
            .filter(IntegratedSyncRun.dataset_id.in_(dataset_ids))
            .scalar() or 0
        )
    else:
        version_count = run_count = 0
    # 任何状态的写入回执（含 failed/unknown）都算历史，审计不可清障删除。
    mutation_count = (
        db.query(func.count(IntegratedMutation.id))
        .join(IntegratedAsset, IntegratedMutation.asset_id == IntegratedAsset.id)
        .filter(IntegratedAsset.connection_id == conn.id)
        .scalar() or 0
    )
    mapping_references = []
    for dataset in datasets:
        for binding in dataset_mapping_bindings(db, dataset.id):
            mapping_references.append(
                {**binding, "dataset_id": dataset.id, "dataset_name": dataset.name})
    tasks = (
        db.query(PipelineTask)
        .join(Pipeline, PipelineTask.pipeline_id == Pipeline.id)
        .filter(Pipeline.source_dataset_id.in_(dataset_ids))
        .all()
        if dataset_ids else []
    )
    if not (datasets or assets or version_count or run_count or mutation_count
            or mapping_references or tasks):
        return None

    def preview(rows, mapper):
        return [mapper(row) for row in rows[:5]]

    parts = []
    if datasets:
        parts.append(f"{len(datasets)} 个同步数据集（请先在「数据资产」中删除）")
    if assets:
        parts.append(f"{len(assets)} 个集成数据集资产（请先在资产页注销零历史资产）")
    history_total = version_count + run_count + mutation_count
    if history_total:
        parts.append(
            f"{history_total} 条数据历史（版本 {version_count} / 同步运行 {run_count} / 写入回执 {mutation_count}）")
    if mapping_references:
        parts.append(f"{len(mapping_references)} 个本体映射绑定")
    if tasks:
        parts.append(f"{len(tasks)} 个数据任务")
    return {
        "message": (
            "连接仍有依赖，不能注销：" + "、".join(parts)
            + "。受控注销不会删除任何历史或审计记录，请先解除以上依赖。"
        ),
        "datasets": preview(datasets, lambda ds: {"id": ds.id, "name": ds.name, "kind": ds.kind}),
        "datasets_total": len(datasets),
        "integrated_assets": preview(
            assets, lambda a: {"id": a.id, "name": a.name,
                               "schema_name": a.schema_name, "table_name": a.table_name}),
        "integrated_assets_total": len(assets),
        "history": {"dataset_versions": version_count,
                    "sync_runs": run_count, "mutations": mutation_count},
        "mapping_references": preview(
            mapping_references,
            lambda ref: {key: ref.get(key) for key in
                         ("mapping_id", "ontology_id", "kind", "name", "dataset_name")}),
        "mapping_references_total": len(mapping_references),
        "tasks": preview(tasks, lambda task: {"id": task.id, "name": task.name,
                                              "pipeline_id": task.pipeline_id}),
        "tasks_total": len(tasks),
        # 兼容 D-006 既有消费者：total 保持“同步数据集数”的原语义。
        "total": len(datasets),
    }


@router.post("/{connection_id}/schedule")
def set_schedule(connection_id: str, cron_expr: str, db: Session = Depends(get_db)):
    """已退役：该端点只把表达式写进连接配置，从未向调度器注册过 Job。

    返回 410 指引自数据任务池；存量连接 config["schedule_cron"] 键不在
    退役清理范围内，保持原样。
    """
    raise HTTPException(
        status_code=410,
        detail=(
            "连接级 Cron 调度已停用；请使用数据任务池"
            "（/api/v2/pipeline-tasks）按流水线创建调度任务"
        ),
    )


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
