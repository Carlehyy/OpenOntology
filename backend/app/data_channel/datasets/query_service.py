"""Dataset overview, preview, schema, export, and dependency queries."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.data_channel.datasets import cache as lake_cache
from app.data_channel.datasets.service import DatasetService


def _as_int(value, fallback: int) -> int:
    """把直接调用路由函数时可能出现的 FastAPI Query 默认值对象归一化为整数。"""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    default = getattr(value, "default", None)
    if isinstance(default, int) and not isinstance(default, bool):
        return default
    return fallback


def datasets_overview(
    db: Session,
    source: str = "",
    search: str = "",
    sort_by: str = "updated_at",
    page: int = 1,
    page_size: int = 20,
    paginated: bool = False,
    *,
    consumer_map_fn: Callable[[Session], dict[str, list[dict]]],
):
    """原始数据集总览；人工资产可按创建时间倒序分页。

    缓存键覆盖全部查询参数（含分页/排序/搜索词）并携带总览版本号，
    写路径 bump 版本即整体失效；短 TTL 兜底，Redis 不可用时自动回退
    数据库查询。
    """
    # 直接调用路由函数时 page/page_size 可能是 FastAPI Query 默认值对象，
    # 先归一化为整数，保证缓存键与分页计算行为一致。
    page = _as_int(page, 1)
    page_size = _as_int(page_size, 20)
    cache_key = lake_cache.overview_key({
        "source": source,
        "search": search,
        "sort_by": sort_by,
        "page": page,
        "page_size": page_size,
        "paginated": bool(paginated),
    })

    def _build() -> dict:
        return _datasets_overview_from_db(
            db,
            source,
            search,
            sort_by,
            page,
            page_size,
            paginated,
            consumer_map_fn=consumer_map_fn,
        )

    return lake_cache.cached_call(
        cache_key, lake_cache.OVERVIEW_TTL_SECONDS, _build
    )


def _datasets_overview_from_db(
    db: Session,
    source: str,
    search: str,
    sort_by: str,
    page: int,
    page_size: int,
    paginated: bool,
    *,
    consumer_map_fn: Callable[[Session], dict[str, list[dict]]],
) -> dict:
    from app.data_channel.datasets.models import Dataset, DatasetVersion
    from app.data_channel.connections.models import Connection

    q = db.query(Dataset).filter(Dataset.kind != "curated")
    if source == "manual":
        # “人工数据集”同时包含文件上传和在线创建，排除历史同步资产。
        q = q.filter(
            Dataset.source_connection_id.is_(None),
            ~Dataset.name.startswith("SYNC::"),
        )
    elif source == "sync":
        q = q.filter(or_(
            Dataset.source_connection_id.is_not(None),
            Dataset.name.startswith("SYNC::"),
        ))
    keyword = search.strip()
    if keyword:
        q = q.filter(Dataset.name.ilike(f"%{keyword}%"))
    total = q.count()
    order_column = Dataset.created_at if sort_by == "created_at" else Dataset.updated_at
    ordered = q.order_by(order_column.desc(), Dataset.id.desc())
    datasets = (ordered.offset((page - 1) * page_size).limit(page_size).all()
                if paginated else ordered.all())
    consumer_map = consumer_map_fn(db)
    conn_names = {c.id: c.name for c in db.query(Connection).all()}

    # 一次取全部版本，避免 N+1
    versions_by_ds: dict[str, list[DatasetVersion]] = {}
    dataset_ids = [dataset.id for dataset in datasets]
    version_query = db.query(DatasetVersion)
    if dataset_ids:
        version_query = version_query.filter(DatasetVersion.dataset_id.in_(dataset_ids))
    else:
        version_query = version_query.filter(False)
    for v in version_query.order_by(DatasetVersion.version_no).all():
        versions_by_ds.setdefault(v.dataset_id, []).append(v)

    items = []
    for ds in datasets:
        vers = versions_by_ds.get(ds.id, [])
        latest = vers[-1] if vers else None
        is_sync = ds.name.startswith("SYNC::") or bool(ds.source_connection_id)
        is_manual = (ds.schema_json or {}).get("origin") == "manual"
        items.append({
            "id": ds.id,
            "name": ds.name.removeprefix("SYNC::"),
            "raw_name": ds.name,
            "kind": ds.kind,
            "primary_key": str((ds.schema_json or {}).get("primary_key") or ""),
            # sync 优先于 manual：同步维护的数据集不允许被在线编辑语义覆盖
            "source": "sync" if is_sync else ("manual" if is_manual else "upload"),
            "connection_name": conn_names.get(ds.source_connection_id or "", ""),
            "version_count": len(vers),
            "latest_version_no": latest.version_no if latest else 0,
            "rowcount": latest.rowcount if latest else None,
            "consumers": consumer_map.get(ds.id, []),
            "created_at": ds.created_at.isoformat() if ds.created_at else None,
            "updated_at": ds.updated_at.isoformat() if ds.updated_at else None,
        })
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def dataset_consumers(
    dataset_id: str,
    db: Session,
    *,
    dataset_consumers_fn: Callable[[Session, str], list[dict]],
):
    """查询哪些流水线使用该数据集作为数据源"""
    svc = DatasetService(db)
    if not svc.get_dataset(dataset_id):
        raise HTTPException(404, "Dataset not found")
    return {
        "dataset_id": dataset_id,
        "consumers": dataset_consumers_fn(db, dataset_id),
    }


def list_datasets(kind: str | None, db: Session):
    svc = DatasetService(db)
    return svc.list_datasets(kind=kind)


def get_dataset(dataset_id: str, db: Session):
    svc = DatasetService(db)
    ds = svc.get_dataset(dataset_id)
    if not ds:
        raise HTTPException(404, "Dataset not found")
    return ds


def list_versions(dataset_id: str, db: Session):
    svc = DatasetService(db)
    versions = svc.list_versions(dataset_id)
    from app.data_channel.datasets.models import DatasetVersionEvent
    events = {
        event.dataset_version_id: event
        for event in db.query(DatasetVersionEvent).filter(
            DatasetVersionEvent.dataset_id == dataset_id,
            DatasetVersionEvent.event_type == "version_published",
        ).all()
    }
    return [{
        "id": version.id,
        "version_no": version.version_no,
        "rowcount": version.rowcount,
        "storage_uri": version.storage_uri,
        "automation": ({
            "status": events[version.id].status,
            "attempts": events[version.id].attempts,
            "last_error": events[version.id].last_error,
            "result": events[version.id].result_json,
            "processed_at": (
                events[version.id].processed_at.isoformat()
                if events[version.id].processed_at else None),
        } if version.id in events else None),
    } for version in versions]


def require_curated_preview_approved(
    db: Session,
    dataset,
    version,
) -> None:
    """通用 Dataset preview 不得绕过成品资产审核门禁。"""
    if dataset.kind != "curated":
        return
    from app.data_channel.curated.approved_version_reader import (
        ReviewApprovalError,
        require_version_approved,
        version_review,
    )

    if version is None:
        raise HTTPException(409, detail={
            "code": "dataset_version_not_approved",
            "message": "该成品数据集尚无可预览的数据版本。",
        })
    try:
        require_version_approved(db, dataset.id, version)
    except ReviewApprovalError as exc:
        review = version_review(db, dataset.id, version)
        rejected = review is not None and review.status == "rejected"
        raise HTTPException(409, detail={
            "code": (
                "dataset_version_rejected"
                if rejected else "dataset_version_not_approved"
            ),
            "message": (
                f"数据版本 v{version.version_no} 已拒绝，仅可通过审核差异查看审计快照，"
                "不能用于普通预览或进入本体。"
                if rejected else
                f"数据版本 v{version.version_no} 尚未通过审核，不能用于普通预览或进入本体。"
            ),
            "dataset_version_id": version.id,
            "version_no": version.version_no,
            "review_status": review.status if review is not None else "pending_review",
        }) from exc


def preview_data(
    dataset_id: str,
    version_no: int,
    limit: int,
    db: Session,
    *,
    require_curated_preview_approved_fn: Callable[..., None],
):
    from app.data_channel.datasets.models import DatasetVersion

    svc = DatasetService(db)
    ds = svc.get_dataset(dataset_id)
    if not ds:
        raise HTTPException(404, "Dataset not found")
    version = db.query(DatasetVersion).filter(
        DatasetVersion.dataset_id == dataset_id,
        DatasetVersion.version_no == version_no,
    ).first()
    require_curated_preview_approved_fn(db, ds, version)
    if version is None:
        return []
    # 版本内容不可变：键携带 version id，新版本自动换键，无需失效。
    cache_key = f"ob:lake:previewv:{dataset_id}:{version.id}:{limit}"
    return lake_cache.cached_call(
        cache_key,
        lake_cache.VERSION_TTL_SECONDS,
        lambda: svc.preview(dataset_id, version_no, limit),
    )


def get_schema(dataset_id: str, db: Session):
    """返回数据集字段契约：标识、中文显示名、类型、空值约束与主键。"""
    svc = DatasetService(db)
    ds = svc.get_dataset(dataset_id)
    if not ds:
        raise HTTPException(404, "Dataset not found")

    # 人工建表或已发布流水线声明过类型的数据集：类型是权威契约，直接返回
    # 声明值而非从物理快照重推断。成品快照为兼容历史 CSV 语义会把标量保存
    # 为字符串；若在这里重推断，会把 float(integer 样值)、timestamp 和
    # boolean 分别误报为 integer/string/string。
    schema_json = ds.schema_json or {}
    from app.data_channel.datasets.lake_gate import split_pk
    pk_columns = set(split_pk(schema_json.get("primary_key")))
    field_names = schema_json.get("field_names") or {}
    definitions = {
        str(item.get("field_key")): item
        for item in (schema_json.get("contract_definitions") or [])
        if isinstance(item, dict) and item.get("field_key")
    }
    typed_columns = {
        str(item.get("name")): item
        for item in (schema_json.get("columns_typed") or [])
        if isinstance(item, dict) and item.get("name")
    }

    def column_contract(name: str, column: dict | None = None) -> dict:
        column = {**(typed_columns.get(name) or {}), **(column or {})}
        definition = definitions.get(name) or {}
        display_name_configured = any((
            bool(str(column.get("display_name") or "").strip()),
            bool(str(column.get("field_name") or "").strip()),
            name in field_names and bool(str(field_names.get(name) or "").strip()),
            bool(str(definition.get("field_name") or "").strip()),
        ))
        display_name = str(
            column.get("display_name")
            or column.get("field_name")
            or field_names.get(name)
            or definition.get("field_name")
            or name
        )
        nullable = bool(column.get(
            "nullable", definition.get("nullable", name not in pk_columns)))
        return {
            "name": name,
            "display_name": display_name,
            "display_name_configured": display_name_configured,
            "type": column.get("type") or definition.get("field_type") or "string",
            "nullable": False if name in pk_columns else nullable,
            "is_primary_key": name in pk_columns,
        }

    # 契约缓存键 = 数据集 + 最新版本 + 契约指纹：列声明（主键/类型/显示名）
    # 变了或版本变了都会换键；旧键靠 TTL 自然回收。
    versions = svc.list_versions(dataset_id)
    latest_version_id = versions[-1].id if versions else "none"
    schema_fingerprint = hashlib.sha1(
        json.dumps(
            schema_json, ensure_ascii=False, sort_keys=True, default=str
        ).encode("utf-8")
    ).hexdigest()[:16]
    cache_key = (
        f"ob:lake:schema:{dataset_id}:{latest_version_id}:{schema_fingerprint}"
    )

    def _build() -> dict:
        if (
            schema_json.get("types_source")
            in {
                "declared",
                "published_pipeline_contract",
                # 连接同步写入的列契约：元数据内省/全量采样，比预览 10 行的
                # 现场推断更可信（connection_sync._structured_schema_json）。
                "connector_introspection",
                "sample_inference",
            }
            and schema_json.get("columns_typed")
        ):
            rows = svc.preview(dataset_id, None, limit=10)
            columns = []
            for c in schema_json["columns_typed"]:
                if not isinstance(c, dict) or not c.get("name"):
                    continue
                name = c["name"]
                samples = [row.get(name) for row in rows if row.get(name) not in (None, "")][:5]
                columns.append({**column_contract(name, c), "sample_values": samples})
            return {"dataset_id": dataset_id, "columns": columns}

        # Use latest version for schema inference
        if not versions:
            return {"dataset_id": dataset_id, "columns": []}

        latest_version_no = versions[-1].version_no
        rows = svc.preview(dataset_id, latest_version_no, limit=10)
        if not rows:
            return {"dataset_id": dataset_id, "columns": []}

        columns = []
        all_keys = list(rows[0].keys()) if rows else []
        for key in all_keys:
            sample_values = [row.get(key) for row in rows if row.get(key) is not None][:5]
            # Infer type from sample values
            col_type = "string"
            for val in sample_values:
                if isinstance(val, bool):
                    col_type = "boolean"
                    break
                elif isinstance(val, int):
                    col_type = "integer"
                    break
                elif isinstance(val, float):
                    col_type = "float"
                    break
                elif isinstance(val, str):
                    try:
                        int(val)
                        col_type = "integer"
                    except ValueError:
                        try:
                            float(val)
                            col_type = "float"
                        except ValueError:
                            col_type = "string"
                    break
            columns.append({
                **column_contract(key, {"type": col_type}),
                "sample_values": sample_values,
            })

        return {"dataset_id": dataset_id, "columns": columns}

    return lake_cache.cached_call(cache_key, lake_cache.VERSION_TTL_SECONDS, _build)


def export_dataset(
    dataset_id: str,
    format: str,
    db: Session,
    *,
    require_manual_dataset_fn: Callable[..., None],
):
    """导出人工数据集最新版本的全部行，格式为 CSV 或 Excel。"""
    import io
    from urllib.parse import quote
    from app.data_channel.datasets.service import DatasetReadError, rows_to_csv_bytes

    svc = DatasetService(db)
    ds = svc.get_dataset(dataset_id)
    if not ds:
        raise HTTPException(404, "Dataset not found")
    require_manual_dataset_fn(ds, "导出")
    try:
        rows = svc.load_all_rows(dataset_id)
    except DatasetReadError as exc:
        raise HTTPException(502, str(exc))

    schema_columns = list((ds.schema_json or {}).get("columns") or [])
    columns = schema_columns or (list(rows[0].keys()) if rows else [])
    safe_name = "".join(c for c in ds.name if c not in '\\/:*?"<>|').strip() or "人工数据集"
    filename = f"{safe_name}.{format}"
    disposition = f"attachment; filename*=UTF-8''{quote(filename)}"

    if format == "csv":
        # UTF-8 BOM 让 Excel 直接打开中文 CSV 时不乱码。
        data = b"\xef\xbb\xbf" + rows_to_csv_bytes(rows, columns)
        return StreamingResponse(
            io.BytesIO(data), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": disposition},
        )

    import openpyxl
    workbook = openpyxl.Workbook(write_only=True)
    sheet = workbook.create_sheet(title="数据")
    sheet.append(columns)
    for row in rows:
        values = []
        for column in columns:
            value = row.get(column, "")
            if isinstance(value, (dict, list)):
                import json
                value = json.dumps(value, ensure_ascii=False)
            values.append(value)
        sheet.append(values)
    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": disposition},
    )


def get_stats(dataset_id: str, db: Session):
    """返回数据集统计信息"""
    svc = DatasetService(db)
    ds = svc.get_dataset(dataset_id)
    if not ds:
        raise HTTPException(404, "Dataset not found")

    versions = svc.list_versions(dataset_id)
    # 统计只随最新版本变化：键携带 latest version id，无需显式失效。
    latest_version_id = versions[-1].id if versions else "none"
    cache_key = f"ob:lake:stats:{dataset_id}:{latest_version_id}"

    def _build() -> dict:
        version_count = len(versions)

        # Use latest version for row/column counts and null rates
        row_count = 0
        column_count = 0
        null_rates: dict = {}

        if versions:
            latest = versions[-1]
            row_count = latest.rowcount or 0
            rows = svc.preview(dataset_id, latest.version_no, limit=100)
            if rows:
                column_count = len(rows[0].keys())
                # Compute null rates per column
                for key in rows[0].keys():
                    null_count = sum(1 for row in rows if row.get(key) is None or row.get(key) == "")
                    null_rates[key] = round(null_count / len(rows), 4)

        return {
            "dataset_id": dataset_id,
            "row_count": row_count,
            "column_count": column_count,
            "null_rates": null_rates,
            "version_count": version_count,
        }

    return lake_cache.cached_call(cache_key, lake_cache.VERSION_TTL_SECONDS, _build)


def preview_dataset(
    dataset_id: str,
    limit: int,
    offset: int,
    db: Session,
    *,
    require_curated_preview_approved_fn: Callable[..., None],
):
    """预览数据集最新版本的数据，支持 offset/limit 分页。默认前 20 行。"""
    svc = DatasetService(db)
    ds = svc.get_dataset(dataset_id)
    if not ds:
        raise HTTPException(404, "Dataset not found")
    versions = svc.list_versions(dataset_id)
    if not versions:
        return {"dataset_id": dataset_id, "rows": [], "columns": [], "total_rows": 0,
                "offset": 0, "limit": limit}
    latest = versions[-1]
    # 审核门禁每次请求都执行，缓存不能绕过它。
    require_curated_preview_approved_fn(db, ds, latest)
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    # 版本内容不可变：键携带 latest version id 与分页参数，编辑/上传新版本后
    # 自动换键，读取失败时回退原路径。
    cache_key = f"ob:lake:preview:{dataset_id}:{latest.id}:{offset}:{limit}"

    def _build() -> dict:
        rows = svc.preview(dataset_id, latest.version_no, limit=limit, offset=offset)
        # 分页表头稳定性：offset>0 的页可能因该页某列全空而缺列，优先用契约列
        schema_cols = (ds.schema_json or {}).get("columns") if ds.schema_json else None
        columns = list(schema_cols) if schema_cols else (list(rows[0].keys()) if rows else [])
        return {
            "dataset_id": dataset_id,
            "dataset_name": ds.name,
            "version_no": latest.version_no,
            "total_rows": latest.rowcount or 0,
            "offset": offset,
            "limit": limit,
            "columns": columns,
            "rows": rows,
        }

    return lake_cache.cached_call(cache_key, lake_cache.VERSION_TTL_SECONDS, _build)
