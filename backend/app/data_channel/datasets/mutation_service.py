"""Dataset creation, import, version, contract, and row mutations."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.data_channel.datasets.manual_contract import (
    MANUAL_FIELD_CONTRACT_VERSION,
    ContractRequest,
    CreateTableRequest,
    RowEditsRequest,
)
from app.data_channel.datasets.service import DatasetService


def dispatch_dataset_import_task(
    job_id: str,
    *,
    kind: str,
    operation: str,
    settings_obj: Any,
    logger_obj: Any,
) -> dict:
    """Dispatch one import task through the NATS work queue (fail-closed)."""
    from app.data_channel.datasets.import_jobs import update_status
    from app.data_channel.pipeline_tasks.dispatch import (
        DATASET_IMPORT_SUBJECT,
        dispatch_task,
    )

    try:
        dispatch_task(DATASET_IMPORT_SUBJECT, {"job_id": job_id, "kind": kind})
    except Exception as exc:  # noqa: BLE001 - dispatch failures are fail-closed
        update_status(
            job_id,
            status="failed",
            execution_mode="nats",
            progress=5,
            phase="后台任务投递失败",
            error="后台任务通道不可用",
        )
        logger_obj.error(
            "后台任务通道无法投递数据集%s任务 %s；任务未执行（%s）",
            operation,
            job_id,
            type(exc).__name__,
        )
        raise HTTPException(
            503,
            "后台任务通道不可用，数据集导入任务未投递",
        ) from exc
    return update_status(job_id, execution_mode="nats")


def check_upload_file(filename: str | None, content: bytes) -> str:
    """校验上传文件扩展名与大小，返回小写扩展名"""
    from app.config import settings

    ext = (filename or "").rsplit(".", 1)[-1].lower()
    allowed = {e.strip() for e in settings.allowed_upload_extensions.split(",") if e.strip()}
    if ext not in allowed:
        raise HTTPException(400, f"不支持的文件类型: .{ext} (允许: {settings.allowed_upload_extensions})")
    if len(content) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"文件超过大小限制 {settings.max_upload_mb}MB")
    return ext


def check_manual_import_extension(filename: str | None) -> str:
    """Validate the lightweight upload metadata before streaming to staging."""
    ext = (filename or "").rsplit(".", 1)[-1].lower()
    if ext not in {"csv", "xlsx", "xls"}:
        raise HTTPException(400, "在线新建表格仅支持 CSV、XLSX 或 XLS 文件")
    return ext


def estimate_rowcount(content: bytes, ext: str) -> int | None:
    """估算数据行数（CSV/Excel/JSON）。

    行数元数据会被流水线的数据可用性校验消费，不能把可正常解析的 JSON
    数组留成 ``None``，否则预览有数据而发布链路会误判为空。
    """
    try:
        if ext == "csv":
            return max(0, content.count(b"\n") - 1)
        if ext in ("xlsx", "xls"):
            # 与正式导入共用格式嗅探和空白行语义；同时覆盖旧版 OLE/BIFF XLS。
            from app.data_channel.datasets.service import _parse_stored_rows
            return len(_parse_stored_rows(content, limit=None))
        if ext == "json":
            import json
            parsed = json.loads(content.decode("utf-8-sig"))
            if isinstance(parsed, list):
                return len(parsed)
            if isinstance(parsed, dict):
                return 1
            return 0
    except Exception:
        pass
    return None


async def upload_dataset(
    file: UploadFile,
    metadata: str,
    db: Session,
    *,
    check_upload_file_fn: Callable[[str | None, bytes], str],
    estimate_rowcount_fn: Callable[[bytes, str], int | None],
    build_manual_schema_fn: Callable[..., tuple[str, dict]],
    normalize_manual_contract_upload_fn: Callable[..., tuple[list[dict], bytes]],
    validate_manual_rows_fn: Callable[..., None],
):
    """上传 CSV/Excel 文件并创建人工数据集。

    metadata 为空时兼容旧上传入口；新建表格弹窗会携带字段显示名、类型、
    非空与主键契约，使上传文件直接成为 v1，不产生无意义的空白版本。
    """
    import json
    import os

    name = os.path.splitext(file.filename or "upload")[0]
    content = await file.read()
    ext = check_upload_file_fn(file.filename, content)
    if metadata:
        if ext not in ("csv", "xlsx", "xls"):
            raise HTTPException(400, "在线新建表格仅支持 CSV、XLSX 或 XLS 文件")
        try:
            body = CreateTableRequest.model_validate(json.loads(metadata))
        except Exception as exc:
            raise HTTPException(400, f"字段设置格式无效：{exc}")
        name, schema = build_manual_schema_fn(body, origin="upload")
        from app.data_channel.datasets.service import _parse_stored_rows, stored_columns
        try:
            rows = _parse_stored_rows(content, limit=None)
            physical_columns = stored_columns(content)
        except Exception as exc:
            raise HTTPException(400, f"表格解析失败：{exc}")
        if (
            schema.get("manual_field_contract_version")
            == MANUAL_FIELD_CONTRACT_VERSION
        ):
            rows, content = normalize_manual_contract_upload_fn(
                rows,
                physical_columns,
                schema,
                dataset_name=name,
                scope="上传数据",
                allow_field_key_headers=False,
            )
        else:
            expected_columns = schema["columns"]
            if physical_columns != expected_columns:
                raise HTTPException(
                    400,
                    "上传文件列结构已发生变化"
                    f"（文件：{physical_columns}；当前设置：{expected_columns}），请重新选择文件",
                )
            validate_manual_rows_fn(
                rows, schema, dataset_name=name, scope="上传数据")

        svc = DatasetService(db)
        ds = svc.create_dataset(
            name=name, kind="structured", schema_json=schema, commit=False)
        version = svc.create_version(
            ds.id, content, rowcount=len(rows), schema_json=schema, _lock_held=True)
        return {"data": {
            "id": ds.id, "name": ds.name, "kind": ds.kind,
            "columns": schema["columns"],
            "primary_key": schema.get("primary_key", ""),
            "version_no": version.version_no, "rowcount": len(rows),
            "source": "upload",
        }}

    # 推断 kind
    if ext in ("csv", "xlsx", "xls"):
        kind = "structured"
    elif ext in ("json", "xml"):
        kind = "semi"
    else:
        kind = "unstructured"

    svc = DatasetService(db)
    # 数据集与首版本必须原子出现；首版本失败时不能在资产湖留下空壳。
    ds = svc.create_dataset(name=name, kind=kind, commit=False)
    svc.create_version(
        ds.id,
        content,
        rowcount=estimate_rowcount_fn(content, ext),
        _lock_held=True,
    )
    return {"data": {"id": ds.id, "name": ds.name, "kind": ds.kind, "dataset_type": "raw_dataset", "schema_type": "tabular"}}


async def start_dataset_import(
    file: UploadFile,
    current_user: Any,
    *,
    check_manual_import_extension_fn: Callable[[str | None], str],
    dispatch_dataset_import_task_fn: Callable[..., dict],
):
    """Stream one spreadsheet to an isolated directory and queue server parsing."""
    import aiofiles
    from app.config import settings
    from app.data_channel.datasets.import_jobs import (
        create_import_job, remove_job, source_path, update_manifest, update_status)

    ext = check_manual_import_extension_fn(file.filename)
    manifest = create_import_job(
        owner_id=current_user.id,
        filename=file.filename or f"upload.{ext}",
        extension=ext,
    )
    job_id = manifest["job_id"]
    target = source_path(job_id, ext)
    max_bytes = settings.max_upload_mb * 1024 * 1024
    total = 0
    try:
        async with aiofiles.open(target, "wb") as output:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(
                        413, f"文件超过大小限制 {settings.max_upload_mb}MB")
                await output.write(chunk)
        update_manifest(job_id, file_size=total)
        update_status(
            job_id,
            status="queued",
            file_size=total,
            progress=5,
            phase="文件上传完成，等待后台解析",
            error=None,
        )
        status = dispatch_dataset_import_task_fn(
            job_id,
            kind="inspect",
            operation="解析",
        )
        return {"data": status}
    except HTTPException:
        if total > max_bytes:
            remove_job(job_id)
        raise
    except Exception:
        remove_job(job_id)
        raise
    finally:
        await file.close()


def get_dataset_import(
    job_id: str,
    current_user: Any,
):
    from app.data_channel.datasets.import_jobs import (
        assert_job_owner, read_status)

    try:
        assert_job_owner(job_id, current_user.id)
        return {"data": read_status(job_id)}
    except FileNotFoundError:
        raise HTTPException(404, "导入任务不存在或已被清理")
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


def commit_dataset_import_job(
    job_id: str,
    body: CreateTableRequest,
    current_user: Any,
    *,
    build_manual_schema_fn: Callable[..., tuple[str, dict]],
    dispatch_dataset_import_task_fn: Callable[..., dict],
):
    from app.data_channel.datasets.import_jobs import (
        assert_job_owner, read_status, update_status, write_metadata)

    try:
        assert_job_owner(job_id, current_user.id)
        status = read_status(job_id)
    except FileNotFoundError:
        raise HTTPException(404, "导入任务不存在或已被清理")
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    # ready 可提交；failed 且已完成过解析（columns 仍在状态里）的任务同样
    # 允许修正字段后直接重新 commit——导入失败发生在 DB 写入之前，无脏数据，
    # 仅 ready 态可提交会把用户逼去整文件重传。解析阶段就失败的任务没有
    # 列信息，重新提交也无从修正，仍须重新上传。
    current = str(status.get("status") or "")
    if current != "ready" and not (current == "failed" and status.get("columns")):
        raise HTTPException(
            409, f"导入任务当前状态为 {current or 'unknown'}，不能提交")

    # Validate the small field contract synchronously; full-file validation runs
    # in the NATS pipeline executor after the response.
    build_manual_schema_fn(body, origin="upload")
    write_metadata(job_id, body.model_dump())
    update_status(
        job_id,
        status="import_queued",
        progress=5,
        phase="字段设置已确认，等待后台导入",
        error=None,
    )
    queued = dispatch_dataset_import_task_fn(
        job_id,
        kind="commit",
        operation="导入",
    )
    return {"data": queued}


def create_online_table(
    body: CreateTableRequest,
    db: Session,
    *,
    build_manual_schema_fn: Callable[..., tuple[str, dict]],
):
    """在线新建空表格（人工数据集）：定义列名/类型/主键，无需上传文件。

    与上传创建的数据集能力完全一致：在「维护数据」中逐行录入（每次保存
    生成新版本）、声明主键后可被本体映射灌入、可作为流水线数据源、也可
    上传文件批量补数。列类型由用户声明（types_source=declared），在线编辑
    时按声明校验，不再随数据重新推断。
    """
    name, schema = build_manual_schema_fn(body, origin="manual")

    svc = DatasetService(db)
    ds = svc.create_dataset(
        name=name, kind="structured", schema_json=schema, commit=False)

    # 初始版本 = 只有表头的空表：保持「数据集至少有一个版本」的不变式，
    # 存储对象自描述列结构（解析后 0 行，是合法的空数据集状态）
    import csv
    import io
    buf = io.StringIO()
    csv.writer(buf).writerow(schema["columns"])
    ver = svc.create_version(
        ds.id, buf.getvalue().encode("utf-8"), rowcount=0,
        schema_json=schema, _lock_held=True)

    return {"data": {
        "id": ds.id, "name": ds.name, "kind": ds.kind,
        "columns": schema["columns"], "primary_key": schema.get("primary_key", ""),
        "version_no": ver.version_no, "rowcount": 0, "source": "manual",
    }}


async def upload_dataset_version(
    dataset_id: str,
    file: UploadFile,
    db: Session,
    *,
    require_manual_dataset_fn: Callable[[Any, str], None],
    check_upload_file_fn: Callable[[str | None, bytes], str],
    persist_uploaded_version_fn: Callable[..., dict],
):
    """给已有数据集上传新数据文件，追加为新版本（数据集 ID 保持不变，
    流水线中的绑定不需要改动，下次运行自动读取最新版本）。"""
    svc = DatasetService(db)
    ds = svc.get_dataset(dataset_id)
    if not ds:
        raise HTTPException(404, "Dataset not found")
    if ds.kind == "curated":
        raise HTTPException(400, "成品数据集由流水线生成，不支持手动上传数据")
    require_manual_dataset_fn(ds, "手动上传")

    content = await file.read()
    ext = check_upload_file_fn(file.filename, content)

    from app.data_channel.datasets.lock import DatasetLockTimeout, dataset_write_lock
    try:
        # 上传、在线编辑和流水线入湖必须竞争同一把数据集锁。否则两个请求可能
        # 同时选择 vN+1 并覆盖同一个对象键，造成 checksum 与实际内容错配。
        with dataset_write_lock(f"dataset::{dataset_id}", bind=db.get_bind(), wait_timeout=30):
            db.refresh(ds)
            return persist_uploaded_version_fn(
                db,
                svc,
                ds,
                content,
                ext,
            )
    except DatasetLockTimeout as e:
        raise HTTPException(423, str(e))


def persist_uploaded_version(
    db: Session,
    svc: DatasetService,
    ds,
    content: bytes,
    ext: str,
    *,
    estimate_rowcount_fn: Callable[[bytes, str], int | None],
    normalize_manual_contract_upload_fn: Callable[..., tuple[list[dict], bytes]],
    validate_manual_rows_fn: Callable[..., None],
    dataset_consumers_fn: Callable[[Session, str], list[dict]],
) -> dict:
    """持锁执行人工数据集的新版本校验与落盘。"""
    dataset_id = ds.id

    # 契约管理的数据集（声明过主键 / 在线建表 / 编辑过）需要全量行做校验与列刷新
    schema = dict(ds.schema_json or {})
    declared_pk = str(schema.get("primary_key") or "").strip()
    has_manual_field_contract = (
        schema.get("manual_field_contract_version")
        == MANUAL_FIELD_CONTRACT_VERSION
        and bool(schema.get("contract_definitions"))
    )
    full_rows: list[dict] | None = None
    tabular_ext = ext in ("csv", "xlsx", "xls", "json")
    if not tabular_ext and (declared_pk or schema.get("columns")):
        raise HTTPException(
            400, "已定义字段契约的数据集只支持上传 CSV、XLSX、XLS 或 JSON 数据文件")
    from app.data_channel.datasets.service import stored_columns
    try:
        physical_columns = stored_columns(content) if tabular_ext else []
    except Exception as exc:
        raise HTTPException(400, f"表格解析失败：{exc}")
    if declared_pk or schema.get("columns"):
        from app.data_channel.datasets.service import _parse_stored_rows
        try:
            full_rows = _parse_stored_rows(content, limit=None)
        except Exception as e:
            # 只要存在主键或声明类型契约，就必须在解析后的全量行上校验；
            # 解析失败时放行会让“声明 integer、实际任意文本”的坏版本落湖。
            if declared_pk or schema.get("types_source") == "declared":
                raise HTTPException(400, f"文件解析失败，无法校验数据契约：{e}")
            full_rows = None

    if has_manual_field_contract:
        if full_rows is None:
            raise HTTPException(400, "文件解析失败，无法校验人工数据集字段契约")
        full_rows, content = normalize_manual_contract_upload_fn(
            full_rows,
            physical_columns,
            schema,
            dataset_name=ds.name,
            scope="上传的新版本",
            allow_field_key_headers=True,
        )
        physical_columns = list(schema.get("columns") or [])

    # 已声明主键契约的数据集：即使零行也必须携带完整主键表头。
    if declared_pk and not has_manual_field_contract:
        from app.data_channel.datasets.lake_gate import split_pk
        missing_pk_columns = [c for c in split_pk(declared_pk) if c not in physical_columns]
        if missing_pk_columns:
            raise HTTPException(
                400,
                f"上传的新版本缺少主键列 {missing_pk_columns}；即使文件为零行，表头也必须包含完整主键",
            )
    # 在线建表声明的类型/非空/主键都是数据契约，文件批量补数也必须遵守。
    if (
        not has_manual_field_contract
        and (declared_pk or schema.get("types_source") == "declared")
    ):
        validate_manual_rows_fn(
            full_rows or [], schema, dataset_name=ds.name, scope="上传的新版本")

    # 记录旧列，供列变化提示（契约管理的数据集用契约列，比首行采样更可靠）
    old_cols = set(schema.get("columns") or [])
    if not old_cols:
        old_rows = svc.preview(dataset_id, None, limit=1)
        old_cols = set(old_rows[0].keys()) if old_rows else set()

    rowcount = (
        len(full_rows)
        if full_rows is not None
        else estimate_rowcount_fn(content, ext)
    )
    # full_rows 已是将要发布的内容；先算 schema，再与版本同一事务提交。
    new_cols = set(full_rows[0].keys()) if full_rows else set(physical_columns)
    columns_added = sorted(new_cols - old_cols) if old_cols else []
    columns_removed = sorted(old_cols - new_cols) if old_cols else []

    # 契约管理的数据集：新文件落盘后同步刷新列结构，否则预览表头/在线编辑器
    # 仍按旧列渲染。声明类型的列保留用户声明，新出现的列按数据推断
    if schema.get("columns") and full_rows and not has_manual_field_contract:
        from app.data_channel.datasets.lake_gate import infer_columns_typed
        inferred = infer_columns_typed(full_rows)
        if schema.get("types_source") == "declared":
            declared = {c.get("name"): c
                        for c in (schema.get("columns_typed") or []) if isinstance(c, dict)}
            inferred = [{
                **(declared.get(c["name"]) or {}),
                "name": c["name"],
                "type": (declared.get(c["name"]) or {}).get("type") or c["type"],
            } for c in inferred]
        schema["columns"] = [c["name"] for c in inferred]
        schema["columns_typed"] = inferred

    ver = svc.create_version(
        dataset_id, content, rowcount=rowcount,
        schema_json=schema if schema.get("columns") and full_rows is not None else None,
        _lock_held=True)

    return {
        "dataset_id": dataset_id,
        "dataset_name": ds.name,
        "version_no": ver.version_no,
        "rowcount": ver.rowcount,
        "columns_added": columns_added,
        "columns_removed": columns_removed,
        "consumers": dataset_consumers_fn(db, dataset_id),
    }


def declare_contract(
    dataset_id: str,
    body: ContractRequest,
    db: Session,
    *,
    require_manual_dataset_fn: Callable[[Any, str], None],
):
    """声明人工数据集的主键契约（存在·非空·唯一三校验，全量数据上验证）。

    声明后：上传新版本/在线编辑都会校验主键；本体映射可直接绑定该数据集，
    实例身份 = 主键值（否则退化为整行哈希，字段一变就堆积新实例）。
    已被本体映射绑定后主键锁定——改主键 = 整批实例身份作废。
    """
    from app.data_channel.datasets.lake_gate import (
        LakeGateError, infer_columns_typed, split_pk, validate_pk)
    from app.data_channel.datasets.service import DatasetReadError

    svc = DatasetService(db)
    ds = svc.get_dataset(dataset_id)
    if not ds:
        raise HTTPException(404, "Dataset not found")
    require_manual_dataset_fn(ds, "主键契约声明")

    pk_cols = split_pk(body.primary_key)
    if not pk_cols:
        raise HTTPException(400, "主键不能为空（支持逗号分隔的复合主键）")
    new_pk = ",".join(pk_cols)

    schema = dict(ds.schema_json or {})
    old_pk = str(schema.get("primary_key") or "").strip()
    if old_pk and old_pk != new_pk:
        from app.ontologies.mappings.consumers import dataset_mapping_bindings

        bindings = dataset_mapping_bindings(db, dataset_id)
        if bindings:
            object_count = sum(
                binding.get("kind") == "object" for binding in bindings)
            link_count = sum(
                binding.get("kind") == "link" for binding in bindings)
            raise HTTPException(400,
                f"该数据集已被 {len(bindings)} 个本体映射绑定"
                f"（对象映射 {object_count} 个、关系映射 {link_count} 个），"
                "主键已锁定（改主键会让对象、关系端点或关系实例身份作废）。"
                f"如确需变更，请先删除相关映射")

    has_declared_column_contract = (
        schema.get("manual_field_contract_version")
        == MANUAL_FIELD_CONTRACT_VERSION
        or schema.get("types_source") == "declared"
    )
    try:
        rows = svc.load_all_rows(dataset_id)
    except DatasetReadError as e:
        raise HTTPException(502, str(e))
    declared_columns = list(schema.get("columns") or [])
    if rows:
        try:
            validate_pk(rows, pk_cols, dataset_name=ds.name, scope="现有数据")
        except LakeGateError as e:
            raise HTTPException(400, str(e))
        # A stable/manual declared contract is authoritative.  Re-inferring it
        # from one snapshot turns string identifiers such as "001" into
        # integers and also drops display/source/nullability metadata.  Only
        # untyped legacy uploads are migrated by inference here.
        if not has_declared_column_contract:
            schema["columns"] = list(rows[0].keys())
            schema["columns_typed"] = infer_columns_typed(rows)
    else:
        missing = [c for c in pk_cols if c not in declared_columns]
        if missing:
            raise HTTPException(
                400,
                f"空数据集也必须先声明包含主键的列结构；当前缺少主键列 {missing}",
            )

    if has_declared_column_contract:
        pk_set = set(pk_cols)
        typed_columns = []
        typed_nullable: dict[str, bool] = {}
        for raw_column in schema.get("columns_typed") or []:
            if not isinstance(raw_column, dict):
                typed_columns.append(raw_column)
                continue
            column = dict(raw_column)
            column_name = str(column.get("name") or "")
            if column_name in pk_set:
                column["nullable"] = False
            if column_name and "nullable" in column:
                typed_nullable[column_name] = bool(column["nullable"])
            typed_columns.append(column)
        schema["columns_typed"] = typed_columns

        definitions = []
        for raw_definition in schema.get("contract_definitions") or []:
            if not isinstance(raw_definition, dict):
                definitions.append(raw_definition)
                continue
            definition = dict(raw_definition)
            field_key = str(definition.get("field_key") or "")
            definition["is_primary_key"] = field_key in pk_set
            if field_key in pk_set:
                definition["nullable"] = False
            elif field_key in typed_nullable:
                definition["nullable"] = typed_nullable[field_key]
            definitions.append(definition)
        if "contract_definitions" in schema:
            schema["contract_definitions"] = definitions

    schema["primary_key"] = new_pk
    schema["pk_source"] = "manual"
    ds.schema_json = schema  # 赋新 dict, 原地改 JSON 列不会被 SQLAlchemy 跟踪
    db.commit()
    # 主键变化直接反映在总览列表里，尽力失效缓存（失败静默降级）。
    from app.data_channel.datasets import cache

    cache.invalidate_overview()
    return {"dataset_id": dataset_id, "primary_key": new_pk, "rows_validated": len(rows)}


def edit_rows(
    dataset_id: str,
    body: RowEditsRequest,
    db: Session,
    *,
    require_manual_dataset_fn: Callable[[Any, str], None],
):
    """人工数据集在线维护：改单元格 / 新增行 / 删除行，整体生成一个新版本。

    update/delete 按声明的主键定位行（未声明主键只能追加）；编辑后的全量
    数据重新过主键三校验，坏身份的数据不落盘。base_version_no 不等于当前
    最新版本时返回 409——说明期间有人上传/编辑过，客户端须刷新重做。
    """
    from app.data_channel.datasets.lock import DatasetLockTimeout, dataset_write_lock
    from app.data_channel.datasets.service import rows_to_csv_bytes
    from app.data_channel.datasets.models import DatasetVersion
    from app.data_channel.datasets.edit_service import build_edited_snapshot

    svc = DatasetService(db)
    ds = svc.get_dataset(dataset_id)
    if not ds:
        raise HTTPException(404, "Dataset not found")
    require_manual_dataset_fn(ds, "在线编辑")
    if not (body.updates or body.inserts or body.deletes):
        raise HTTPException(400, "没有任何修改")

    try:
        with dataset_write_lock(f"dataset::{dataset_id}", bind=db.get_bind(), wait_timeout=30):
            latest = db.query(DatasetVersion).filter(
                DatasetVersion.dataset_id == dataset_id).order_by(DatasetVersion.version_no.desc()).first()
            latest_no = latest.version_no if latest else 0
            if body.base_version_no != latest_no:
                raise HTTPException(409, detail={
                    "message": f"数据已更新到 v{latest_no}（你正在编辑的是 v{body.base_version_no}），请刷新后重新修改",
                    "current_version_no": latest_no,
                })

            new_rows, columns, schema = build_edited_snapshot(db, svc, ds, body)
            ver = svc.create_version(
                dataset_id, rows_to_csv_bytes(new_rows, columns),
                rowcount=len(new_rows),
                schema_json=schema if new_rows else None,
                _lock_held=True)
    except DatasetLockTimeout as e:
        raise HTTPException(423, str(e))

    return {
        "dataset_id": dataset_id,
        "version_no": ver.version_no,
        "rowcount": len(new_rows),
        "updated": len(body.updates),
        "inserted": len(body.inserts),
        "deleted": len(body.deletes),
    }


def delete_dataset(
    dataset_id: str,
    force: bool,
    db: Session,
    *,
    dataset_consumers_fn: Callable[[Session, str], list[dict]],
    logger_obj: Any,
):
    """删除原始数据集及其版本（仅管理员，与成品数据集删除权限对齐）。
    被流水线 / 本体映射引用时始终返回 409；force 已禁用，避免数据库外键与页面
    “强删成功”语义不一致。
    若数据集由旧版同步任务（DataSyncTask）驱动，自动禁用该任务防止重建。"""
    from app.data_channel.datasets.models import Dataset, DatasetVersion, MediaItem
    from app.data_channel.file_assets.models import PipelineFileAsset
    from app.data_channel.sync_tasks.models import DataSyncTask
    from app.data_channel.datasets.service import (
        drain_storage_deletion_outbox,
        enqueue_dataset_storage_deletions,
    )

    ds = db.query(Dataset).filter(Dataset.id == dataset_id).first()
    if not ds:
        raise HTTPException(404, "Dataset not found")
    if ds.kind == "curated":
        raise HTTPException(400, "成品数据集请在资产湖「成品数据集」中删除")
    if force:
        raise HTTPException(400, "force 强制删除已禁用；请先解除流水线和本体映射依赖")

    consumers = dataset_consumers_fn(db, dataset_id)
    from app.ontologies.mappings.consumers import dataset_mapping_bindings
    mappings = dataset_mapping_bindings(db, dataset_id)
    if consumers or mappings:
        raise HTTPException(409, detail={
            "message": f"数据集被 {len(consumers)} 条流水线、{len(mappings)} 个本体映射引用，"
                       f"删除后这些流水线将无法运行、本体投影将断源",
            "consumers": consumers,
            "mappings": mappings,
        })

    # ── 联动禁用关联的 DataSyncTask（防止 _get_or_create_dataset 重建）──
    disabled_sync_task: str | None = None
    if ds.name.startswith("SYNC::"):
        legacy_task_name = ds.name.removeprefix("SYNC::")
        sync_task = db.query(DataSyncTask).filter(DataSyncTask.name == legacy_task_name).first()
        if sync_task and sync_task.enabled:
            sync_task.enabled = False
            disabled_sync_task = sync_task.name
            logger_obj.info(
                "DELETE dataset %s → 已自动禁用关联同步任务「%s」(id=%s)",
                dataset_id, sync_task.name, sync_task.id,
            )
            # 通知调度器移除此 Job
            try:
                from app.data_channel.sync_tasks.scheduler import SyncScheduler
                sched = SyncScheduler.get()
                if sched.started:
                    sched.reload_task(sync_task.id)
            except Exception:
                # 清理失败无数据风险：旧引擎对一切执行请求返回「已停用」，
                # 残留 Job 最多触发一次被拒绝的空调度
                logger_obj.warning(
                    "DELETE dataset %s → 调度器清理旧 Job 失败（引擎已拒绝旧任务执行）",
                    dataset_id,
                )

    # 必须在删版本/媒体元数据之前收集 URI，并与这些删除共用一次 commit。
    # commit 失败则 outbox 与元数据删除一起回滚；commit 成功后再 best-effort drain。
    enqueue_dataset_storage_deletions(db, dataset_id)
    ver_ids = [v.id for v in db.query(DatasetVersion).filter(DatasetVersion.dataset_id == dataset_id).all()]
    if ver_ids:
        db.query(MediaItem).filter(MediaItem.dataset_version_id.in_(ver_ids)).delete(synchronize_session=False)
        db.query(PipelineFileAsset).filter(
            PipelineFileAsset.dataset_version_id.in_(ver_ids)
        ).delete(synchronize_session=False)
    # 湖表资产防御性清理：curated 不会走到这里（上方 400 拦截），但变更集与
    # 物理表按数据集维度显式清理不依赖 FK 开关，幂等无副作用
    from app.data_channel.datasets import lake_store
    from app.data_channel.datasets.models import (
        DatasetChangeset, DatasetChangesetRow)
    changeset_ids = [
        row[0]
        for row in db.query(DatasetChangeset.id)
        .filter(DatasetChangeset.dataset_id == dataset_id)
        .all()
    ]
    if changeset_ids:
        db.query(DatasetChangesetRow).filter(
            DatasetChangesetRow.changeset_id.in_(changeset_ids)
        ).delete(synchronize_session=False)
        db.query(DatasetChangeset).filter(
            DatasetChangeset.dataset_id == dataset_id
        ).delete(synchronize_session=False)
    lake_store.drop_lake_table(db, dataset_id)
    db.query(DatasetVersion).filter(DatasetVersion.dataset_id == dataset_id).delete(synchronize_session=False)
    db.delete(ds)
    db.commit()
    # 尽力失效资产湖总览缓存（失败静默降级，不影响删除主流程）。
    from app.data_channel.datasets import cache

    cache.invalidate_overview()
    storage_cleanup = drain_storage_deletion_outbox(db)

    result: dict = {
        "status": "deleted", "id": dataset_id,
        "storage_cleanup": storage_cleanup,
    }
    if disabled_sync_task:
        result["disabled_sync_task"] = disabled_sync_task
        result["message"] = f"已同时禁用同步任务「{disabled_sync_task}」，该数据集不会在下次调度时重建"
    return result
