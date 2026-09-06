"""
Connection 同步 — 把连接器数据落地为 Dataset 版本

把已配置的 Connection 通过其 Connector 拉取数据，序列化为 JSON 落成
Dataset + DatasetVersion，作为数据流水线的原始输入。

支持调用方显式同步执行，也支持经 NATS executor 异步派发。
"""
from __future__ import annotations

import hashlib
import json
import logging
from contextlib import nullcontext

logger = logging.getLogger(__name__)

_RESOURCE_ID_MAX_LENGTH = 500


def _decrypt_config(conn) -> dict:
    """解密 Connection.config。"""
    from app.services import encryption_service
    raw = (conn.config or {}).get("_encrypted", "")
    if not raw:
        return conn.config or {}
    try:
        return json.loads(encryption_service.decrypt(raw))
    except Exception:
        return conn.config or {}


def _structured_schema_json(connector, resource: str, rows: list,
                            primary_key_columns: list[str] | None = None) -> dict:
    """结构化连接数据集的列契约：连接器元数据内省优先，值采样兜底。

    内省失败不阻断同步（与 lake_gate「类型推断永不阻断入湖」一致），但
    降级必须留痕：types_source 记录类型来源，source_schema 仅内省成功时
    存在。columns_typed 保持 {name, type} 瘦形态——persist_contract /
    normalize_definitions 按白名单重建列清单，塞进额外字段会被剥掉，
    源类型与方言标志因此走 schema_json["source_schema"] 平行键。

    主键契约（comma 分隔，split_pk 口径）在内省出主键列时写入；浮点列
    不做主键（精度语义，Foundry 同款规则），拒绝并告警而不是静默放行。
    """
    from app.data_channel.connections.type_normalization import (
        introspected_columns_typed,
    )
    from app.data_channel.datasets.lake_gate import infer_columns_typed

    columns = None
    introspect = getattr(connector, "introspect_schema", None)
    if introspect is not None:
        try:
            columns = introspect(resource)
        except NotImplementedError:
            # 不支持内省的连接器（rest/file/aihot 及存量实现）静默走采样，
            # 这不是故障；只有「声明支持但执行失败」才值得告警留痕。
            columns = None
        except Exception as exc:  # noqa: BLE001 — 内省失败降级采样，但必须留痕
            logger.warning(
                "schema introspection failed for %r (%s); "
                "falling back to sample inference",
                resource, exc,
            )

    if columns:
        typed, source_schema = introspected_columns_typed(columns)
        schema = {
            "columns": [column["name"] for column in typed],
            "columns_typed": typed,
            "types_source": "connector_introspection",
            "source_schema": source_schema,
        }
    else:
        # REST 端点可能返回标量数组（['a', 1]）——infer_columns_typed 只接受
        # dict 行，非 dict 行混入会让同步本身崩溃（父提交可正常同步）。
        # 标量载荷没有列概念，落空契约即可，绝不阻断入湖。
        typed = infer_columns_typed(
            [row for row in rows if isinstance(row, dict)])
        schema = {
            "columns": [column["name"] for column in typed],
            "columns_typed": typed,
            "types_source": "sample_inference",
        }

    pk = [str(column) for column in (primary_key_columns or []) if str(column)]
    if pk:
        lake_types = {c.get("name"): c.get("type") for c in schema["columns_typed"]}
        float_pks = [c for c in pk if lake_types.get(c) == "float"]
        if float_pks:
            logger.warning(
                "浮点列 %s 不适合作为主键契约（精度语义），未写入 primary_key；"
                "请改用整型/文本主键，或经流水线派生稳定键", float_pks)
        else:
            schema["primary_key"] = ",".join(pk)
    return schema


def sync_connection(connection_id: str, mode: str = "full",
                    resource: str | None = None, db=None) -> dict:
    """
    同步 Connection 数据，落地为 Dataset 版本。

    Args:
        connection_id: 要同步的 Connection ID
        mode: "full" | "delta"
        resource: 指定资源（端点/表/集合）；缺省取连接器第一个资源
        db: 可选外部 session（不传则自建）

    Returns:
        {"status": "ok", "rows": int, "dataset_id": str, "version_no": int}
        或 {"status": "error", "error": str}
    """
    from app.database import SessionLocal
    from app.data_channel.connections.models import Connection
    from app.data_channel.datasets.models import Dataset, DatasetVersion  # noqa: F401
    from app.services.connection.registry import get_connector
    from app.data_channel.datasets.service import DatasetService

    own_db = db is None
    db = db or SessionLocal()
    try:
        conn = db.query(Connection).filter(Connection.id == connection_id).first()
        if not conn:
            return {"status": "error", "error": f"Connection {connection_id} not found"}

        config = _decrypt_config(conn)
        try:
            connector = get_connector(conn.kind, config)
        except Exception as e:
            conn.status = "error"
            db.commit()
            return {"status": "error", "error": f"connector init failed: {e}"}

        # 选资源
        res = resource
        if not res:
            try:
                resources = connector.list_resources()
                res = resources[0] if resources else ""
            except Exception:
                res = ""

        # resource 是 Connection 内部的数据集身份，不是展示名称。保持原字符串
        # （不 strip/改大小写），同时拒绝无法被 schema 无损保存的连接器返回值。
        if not isinstance(res, str):
            conn.status = "error"
            db.commit()
            return {
                "status": "error",
                "error": "connector resource identity must be a string",
            }
        if not res:
            conn.status = "error"
            db.commit()
            return {
                "status": "error",
                "error": "connector did not provide a resource to synchronize",
            }
        if len(res) > _RESOURCE_ID_MAX_LENGTH:
            conn.status = "error"
            db.commit()
            return {
                "status": "error",
                "error": (
                    "connector resource identity exceeds "
                    f"{_RESOURCE_ID_MAX_LENGTH} characters"
                ),
            }

        # 拉数据
        try:
            if mode == "delta":
                rows = connector.pull_delta(res)
            else:
                rows = connector.pull_full(res)
        except Exception as e:
            conn.status = "error"
            db.commit()
            return {"status": "error", "error": f"pull failed: {e}"}

        # 主键内省（元数据优先）：失败/不支持不阻断同步，映射创建时会以
        # 「尚未声明主键契约」明确提示。
        pk_columns: list[str] = []
        pk_introspect = getattr(connector, "introspect_primary_key", None)
        if pk_introspect is not None:
            try:
                pk_columns = [str(c) for c in (pk_introspect(res) or [])]
            except NotImplementedError:
                pass
            except Exception as exc:  # noqa: BLE001 — 主键内省失败降级，但必须留痕
                logger.warning(
                    "primary key introspection failed for %r (%s)", res, exc)

        # 归一化为行列表。datetime/Decimal/bytes 等数据库原生对象必须先做
        # 值规范化——json.dumps 对它们直接 TypeError，含日期/金额/二进制列
        # 的表曾因此整次同步失败（default=str 只兜底剩余未知类型）。主键列
        # 的值同步文本化（跨源 join 口径 + 投影侧不受 float64 表示影响）。
        schema_json = None
        if isinstance(rows, bytes):
            content = rows
            rowcount = None
            kind = "unstructured"
        elif isinstance(rows, list):
            from app.data_channel.connections.type_normalization import normalize_rows

            rows = normalize_rows(rows, primary_key_columns=pk_columns)
            content = json.dumps(rows, ensure_ascii=False, default=str).encode("utf-8")
            rowcount = len(rows)
            kind = "structured"
            schema_json = _structured_schema_json(connector, res, rows, pk_columns)
        else:
            content = json.dumps(rows, ensure_ascii=False, default=str).encode("utf-8")
            rowcount = None
            kind = "semi"

        # 解析/首建使用稳定的 connection+resource 锁，避免两个进程同时制造双胞胎。
        # 已有数据集还要进入 dataset 锁，与上传/其他写入共享完整版本序列。
        from app.data_channel.datasets.lock import dataset_write_lock
        ds_svc = DatasetService(db)
        resource_digest = hashlib.sha256(res.encode("utf-8")).hexdigest()
        stable_key = f"connection-sync::{connection_id}::{resource_digest}"
        try:
            with dataset_write_lock(stable_key, bind=db.get_bind(), wait_timeout=30):
                ds = (db.query(Dataset)
                      .filter(
                          Dataset.source_connection_id == connection_id,
                          Dataset.source_resource == res,
                      )
                      .order_by(Dataset.created_at.desc()).first())
                if ds is None:
                    ds_name = f"{conn.name}:{res}" if res else conn.name
                    # name 只用于展示；截断不能影响由 connection+resource 保存的身份。
                    if len(ds_name) > 200:
                        ds_name = f"{ds_name[:197]}..."
                    ds = ds_svc.create_dataset(
                        name=ds_name, kind=kind, connection_id=connection_id,
                        source_resource=res,
                        commit=False)
                    version_guard = nullcontext()
                else:
                    version_guard = dataset_write_lock(
                        f"dataset::{ds.id}", bind=db.get_bind(), wait_timeout=30)
                with version_guard:
                    ver = ds_svc.create_version(
                        ds.id, content, rowcount=rowcount,
                        schema_json=schema_json, _lock_held=True)
        except Exception:
            db.rollback()
            failed_conn = db.query(Connection).filter(
                Connection.id == connection_id).first()
            if failed_conn is not None:
                failed_conn.status = "error"
                db.commit()
            raise

        conn.status = "active"
        db.commit()
        return {
            "status": "ok",
            "rows": rowcount if rowcount is not None else 0,
            "dataset_id": ds.id,
            "version_no": ver.version_no,
            "resource": res,
        }
    finally:
        if own_db:
            db.close()


def sync_all_connections() -> list[dict]:
    """顺序同步所有处于激活状态的 Connection。"""
    from app.database import SessionLocal
    from app.data_channel.connections.models import Connection

    db = SessionLocal()
    try:
        conns = db.query(Connection).filter(Connection.status == "active").all()
        return [sync_connection(c.id, db=db) for c in conns]
    finally:
        db.close()
