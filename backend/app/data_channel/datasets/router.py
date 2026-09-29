"""v2 Dataset API"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, UploadFile, File, Form, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.data_channel.datasets import migration_service, mutation_service, query_service
from app.data_channel.datasets.consumers import (
    dataset_consumer_map as _consumer_map,
    dataset_consumers as _dataset_consumers,
)
from app.data_channel.datasets.manual_contract import (
    MANUAL_FIELD_CONTRACT_VERSION,
    MANUAL_FIELD_KEY_RE,
    ContractRequest,
    CreateTableRequest,
    DatasetResponse,
    RowEditOp,
    RowEditsRequest,
    TableColumnDef,
    build_manual_schema as _build_manual_schema,
    normalize_manual_contract_upload as _normalize_manual_contract_upload,
    require_manual_dataset as _require_manual_dataset,
    serialize_manual_contract_rows as _serialize_manual_contract_rows,
    validate_manual_rows as _validate_manual_rows,
)
from app.deps import get_current_user, require_admin

router = APIRouter(dependencies=[Depends(get_current_user)])
logger = logging.getLogger(__name__)


_check_upload_file = mutation_service.check_upload_file
_check_manual_import_extension = mutation_service.check_manual_import_extension
_estimate_rowcount = mutation_service.estimate_rowcount
_require_curated_preview_approved = (
    query_service.require_curated_preview_approved
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _dispatch_dataset_import_task(
    job_id: str,
    *,
    kind: str,
    operation: str,
) -> dict:
    """Dispatch one import task through the NATS work queue."""
    from app.config import settings

    return mutation_service.dispatch_dataset_import_task(
        job_id,
        kind=kind,
        operation=operation,
        settings_obj=settings,
        logger_obj=logger,
    )


@router.post("/upload", status_code=201)
async def upload_dataset(
    file: UploadFile = File(...),
    metadata: str = Form(""),
    db: Session = Depends(get_db),
):
    """上传 CSV/Excel 文件并创建人工数据集。

    metadata 为空时兼容旧上传入口；新建表格弹窗会携带字段显示名、类型、
    非空与主键契约，使上传文件直接成为 v1，不产生无意义的空白版本。
    """
    return await mutation_service.upload_dataset(
        file,
        metadata,
        db,
        check_upload_file_fn=_check_upload_file,
        estimate_rowcount_fn=_estimate_rowcount,
        build_manual_schema_fn=_build_manual_schema,
        normalize_manual_contract_upload_fn=_normalize_manual_contract_upload,
        validate_manual_rows_fn=_validate_manual_rows,
    )

@router.post("/imports", status_code=202)
async def start_dataset_import(
    file: UploadFile = File(...),
    current_user=Depends(get_current_user),
):
    """Stream one spreadsheet to an isolated directory and queue server parsing."""
    return await mutation_service.start_dataset_import(
        file,
        current_user,
        check_manual_import_extension_fn=_check_manual_import_extension,
        dispatch_dataset_import_task_fn=_dispatch_dataset_import_task,
    )

@router.get("/imports/{job_id}")
def get_dataset_import(
    job_id: str,
    current_user=Depends(get_current_user),
):
    return mutation_service.get_dataset_import(job_id, current_user)

@router.post("/imports/{job_id}/commit", status_code=202)
def commit_dataset_import_job(
    job_id: str,
    body: CreateTableRequest,
    current_user=Depends(get_current_user),
):
    return mutation_service.commit_dataset_import_job(
        job_id,
        body,
        current_user,
        build_manual_schema_fn=_build_manual_schema,
        dispatch_dataset_import_task_fn=_dispatch_dataset_import_task,
    )


# ── 成品数据集 → 人工数据集 异步迁移 ─────────────────────────────
class StartMigrationRequest(BaseModel):
    curated_dataset_id: str


@router.post("/migrations", status_code=202)
def start_dataset_migration(
    body: StartMigrationRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """把一个成品数据集异步拷贝为人工数据集（结构与最新版本数据一致）。

    返回 202 与迁移任务状态；进度通过 GET /migrations 列表或单任务端点
    轮询，任务详情亦可见于成品数据集页的「迁移任务」弹窗。
    """
    return migration_service.start_migration(
        db,
        body.curated_dataset_id,
        current_user,
    )


@router.get("/migrations")
def list_dataset_migrations(
    limit: int = Query(20, ge=1, le=100),
    current_user=Depends(get_current_user),
):
    """当前用户最近创建的成品→人工迁移任务列表。"""
    return migration_service.list_migrations(current_user, limit=limit)


@router.get("/migrations/{job_id}")
def get_dataset_migration(
    job_id: str,
    current_user=Depends(get_current_user),
):
    return migration_service.get_migration(job_id, current_user)

@router.post("/create-table", status_code=201)
def create_online_table(body: CreateTableRequest, db: Session = Depends(get_db)):
    """在线新建空表格（人工数据集）：定义列名/类型/主键，无需上传文件。

    与上传创建的数据集能力完全一致：在「维护数据」中逐行录入（每次保存
    生成新版本）、声明主键后可被本体映射灌入、可作为流水线数据源、也可
    上传文件批量补数。列类型由用户声明（types_source=declared），在线编辑
    时按声明校验，不再随数据重新推断。
    """
    return mutation_service.create_online_table(
        body,
        db,
        build_manual_schema_fn=_build_manual_schema,
    )

@router.post("/{dataset_id}/upload", status_code=201)
async def upload_dataset_version(
    dataset_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """给已有数据集上传新数据文件，追加为新版本（数据集 ID 保持不变，
    流水线中的绑定不需要改动，下次运行自动读取最新版本）。"""
    return await mutation_service.upload_dataset_version(
        dataset_id,
        file,
        db,
        require_manual_dataset_fn=_require_manual_dataset,
        check_upload_file_fn=_check_upload_file,
        persist_uploaded_version_fn=_persist_uploaded_version,
    )


def _persist_uploaded_version(
    db: Session,
    svc,
    ds,
    content: bytes,
    ext: str,
) -> dict:
    """持锁执行人工数据集的新版本校验与落盘。"""
    return mutation_service.persist_uploaded_version(
        db,
        svc,
        ds,
        content,
        ext,
        estimate_rowcount_fn=_estimate_rowcount,
        normalize_manual_contract_upload_fn=_normalize_manual_contract_upload,
        validate_manual_rows_fn=_validate_manual_rows,
        dataset_consumers_fn=_dataset_consumers,
    )

@router.put("/{dataset_id}/contract")
def declare_contract(dataset_id: str, body: ContractRequest, db: Session = Depends(get_db)):
    """声明人工数据集的主键契约（存在·非空·唯一三校验，全量数据上验证）。

    声明后：上传新版本/在线编辑都会校验主键；本体映射可直接绑定该数据集，
    实例身份 = 主键值（否则退化为整行哈希，字段一变就堆积新实例）。
    已被本体映射绑定后主键锁定——改主键 = 整批实例身份作废。
    """
    return mutation_service.declare_contract(
        dataset_id,
        body,
        db,
        require_manual_dataset_fn=_require_manual_dataset,
    )


@router.post("/{dataset_id}/rows/edit")
def edit_rows(dataset_id: str, body: RowEditsRequest, db: Session = Depends(get_db)):
    """人工数据集在线维护：改单元格 / 新增行 / 删除行，整体生成一个新版本。

    update/delete 按声明的主键定位行（未声明主键只能追加）；编辑后的全量
    数据重新过主键三校验，坏身份的数据不落盘。base_version_no 不等于当前
    最新版本时返回 409——说明期间有人上传/编辑过，客户端须刷新重做。
    """
    return mutation_service.edit_rows(
        dataset_id,
        body,
        db,
        require_manual_dataset_fn=_require_manual_dataset,
    )


@router.get("/overview")
def datasets_overview(
    db: Session = Depends(get_db),
    source: str = "",
    search: str = "",
    sort_by: str = "updated_at",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    paginated: bool = False,
):
    """原始数据集总览；人工资产可按创建时间倒序分页。"""
    return query_service.datasets_overview(
        db,
        source,
        search,
        sort_by,
        page,
        page_size,
        paginated,
        consumer_map_fn=_consumer_map,
    )


@router.get("/{dataset_id}/consumers")
def dataset_consumers(dataset_id: str, db: Session = Depends(get_db)):
    """查询哪些流水线使用该数据集作为数据源"""
    return query_service.dataset_consumers(
        dataset_id,
        db,
        dataset_consumers_fn=_dataset_consumers,
    )


@router.delete("/{dataset_id}")
def delete_dataset(dataset_id: str, force: bool = False, db: Session = Depends(get_db),
                   _admin=Depends(require_admin)):
    """删除原始数据集及其版本（仅管理员，与成品数据集删除权限对齐）。
    被流水线 / 本体映射引用时始终返回 409；force 已禁用，避免数据库外键与页面
    “强删成功”语义不一致。
    若数据集由旧版同步任务（DataSyncTask）驱动，自动禁用该任务防止重建。"""
    return mutation_service.delete_dataset(
        dataset_id,
        force,
        db,
        dataset_consumers_fn=_dataset_consumers,
        logger_obj=logger,
    )


@router.get("", response_model=list[DatasetResponse])
def list_datasets(kind: str | None = None, db: Session = Depends(get_db)):
    return query_service.list_datasets(kind, db)


@router.get("/{dataset_id}", response_model=DatasetResponse)
def get_dataset(dataset_id: str, db: Session = Depends(get_db)):
    return query_service.get_dataset(dataset_id, db)


@router.get("/{dataset_id}/versions")
def list_versions(dataset_id: str, db: Session = Depends(get_db)):
    return query_service.list_versions(dataset_id, db)


@router.get("/{dataset_id}/versions/{version_no}/preview")
def preview_data(dataset_id: str, version_no: int, limit: int = 100, db: Session = Depends(get_db)):
    return query_service.preview_data(
        dataset_id,
        version_no,
        limit,
        db,
        require_curated_preview_approved_fn=_require_curated_preview_approved,
    )


@router.get("/{dataset_id}/schema")
def get_schema(dataset_id: str, db: Session = Depends(get_db)):
    """返回数据集字段契约：标识、中文显示名、类型、空值约束与主键。"""
    return query_service.get_schema(dataset_id, db)


@router.get("/{dataset_id}/export")
def export_dataset(dataset_id: str, format: str = Query("csv", pattern="^(csv|xlsx)$"),
                   db: Session = Depends(get_db)):
    """导出人工数据集最新版本的全部行，格式为 CSV 或 Excel。"""
    return query_service.export_dataset(
        dataset_id,
        format,
        db,
        require_manual_dataset_fn=_require_manual_dataset,
    )


@router.get("/{dataset_id}/stats")
def get_stats(dataset_id: str, db: Session = Depends(get_db)):
    """返回数据集统计信息"""
    return query_service.get_stats(dataset_id, db)


@router.get("/{dataset_id}/preview")
def preview_dataset(dataset_id: str, limit: int = 20, offset: int = 0, db: Session = Depends(get_db)):
    """预览数据集最新版本的数据，支持 offset/limit 分页。默认前 20 行。"""
    return query_service.preview_dataset(
        dataset_id,
        limit,
        offset,
        db,
        require_curated_preview_approved_fn=_require_curated_preview_approved,
    )
