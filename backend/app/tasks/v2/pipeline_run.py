"""Pipeline 执行任务体 — 引擎注册表分发 + 共用资产湖入湖（NATS executor 消费）"""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _compute_quality_score(rows: list[dict], meta: dict) -> float:
    if not rows:
        return 0.0
    rows_before = meta.get("rows_before", len(rows)) or len(rows)
    rows_after = meta.get("rows_after", len(rows)) or len(rows)
    retention = rows_after / rows_before if rows_before > 0 else 1.0
    total_cells = sum(len(r) for r in rows) or 1
    filled_cells = sum(1 for r in rows for v in r.values() if v is not None and str(v).strip() != "")
    fill_rate = filled_cells / total_cells
    return round(retention * 0.4 + fill_rate * 0.6, 3)


def _quality_from_counts(filled_cells: int, total_cells: int,
                         row_count: int, meta: dict) -> float:
    """_compute_quality_score 的计数形态：流式管道跨批累积供给，公式一致。"""
    if not row_count:
        return 0.0
    rows_before = meta.get("rows_before", row_count) or row_count
    rows_after = meta.get("rows_after", row_count) or row_count
    retention = rows_after / rows_before if rows_before > 0 else 1.0
    fill_rate = filled_cells / (total_cells or 1)
    return round(retention * 0.4 + fill_rate * 0.6, 3)


def _strip_content(rows: list[dict]) -> list[dict]:
    """去掉行数据里的原始文件 bytes 列——它从不入 CSV，也无法进 JSON 暂存。"""
    return [{k: v for k, v in r.items() if k != "content"} for r in rows]


def _slim_ctx_meta(meta: dict | None) -> dict:
    """ctx.meta 落入 run.stats 前的白名单裁剪。

    ctx.meta 可能携带大体积执行明细，原样写进 v2_pipeline_runs.stats 会让
    单行 JSON 膨胀失控。dry-run 与真实运行统一只保留质量分记账用的两个
    标量键。
    """
    return {k: v for k, v in (meta or {}).items() if k in ("rows_before", "rows_after")}


def _lake_impact_from_changeset(db, changeset, pk_cols: list[str],
                                rows_before: int, rows_after: int) -> dict:
    """从行级变更集生成审计 diff，形状与 merge.compute_lake_impact 完全一致。

    计数取变更集真实计数；样本按 row_pk 排序取前 50（确定性顺序），
    超长值截断沿用 merge._slim_row；sample_truncated 由计数判断。
    """
    from app.data_channel.datasets.models import DatasetChangesetRow
    from app.data_channel.pipeline_tasks.merge import _slim_row

    def _sample(change_type: str) -> list:
        return (db.query(DatasetChangesetRow)
                .filter(DatasetChangesetRow.changeset_id == changeset.id,
                        DatasetChangesetRow.change_type == change_type)
                .order_by(DatasetChangesetRow.row_pk)
                .limit(50)
                .all())

    added = _sample("added")
    updated = _sample("updated")
    deleted = _sample("deleted")
    return {
        "keyed_by": list(pk_cols) if pk_cols else None,
        "total_before": rows_before,
        "total_after": rows_after,
        "added_count": changeset.added_count,
        "updated_count": changeset.updated_count,
        "deleted_count": changeset.deleted_count,
        "unchanged_count": max(
            0, rows_after - changeset.added_count - changeset.updated_count),
        "added_sample": [_slim_row(r.new_row or {}) for r in added],
        "updated_sample": [{"before": _slim_row(r.old_row or {}),
                            "after": _slim_row(r.new_row or {})}
                           for r in updated],
        "deleted_sample": [_slim_row(r.old_row or {}) for r in deleted],
        "sample_truncated": (changeset.added_count > 50
                             or changeset.updated_count > 50
                             or changeset.deleted_count > 50),
    }


def _curated_name(pl, source: dict, multi_source: bool, table_name: str | None = None) -> str:
    """产物 curated 数据集的命名规则——入湖与 dry-run 预检必须用同一套派生。"""
    stem = Path(source["filename"]).stem
    name_parts = [pl.name]
    if multi_source:
        name_parts.append(stem)
    if table_name:
        name_parts.append(table_name)
    name_parts.append("curated")
    return " ".join(name_parts)


def _curated_output_key(source: dict, multi_source: bool,
                        table_name: str | None = None) -> str:
    """同一流水线内产物的稳定键，不含可修改的流水线名称。

    单产物固定为 default；多源/拆表产物把源数据集和表名都纳入键，避免两个
    源恰好产出同名拆表时串湖。文件名只用于极少数没有 dataset_id 的兼容路径。
    """
    parts: list[str] = []
    if multi_source:
        source_key = source.get("dataset_id") or source.get("filename") or "unknown-source"
        parts.append(f"source:{source_key}")
    if table_name:
        parts.append(f"table:{table_name}")
    return "|".join(parts) or "default"


def _legacy_output_matches(schema: dict, source: dict, multi_source: bool,
                           table_name: str | None) -> bool:
    """判断没有 output_key 的历史资产是否与当前产物槽位一致。"""
    if schema.get("output_key"):
        return schema.get("output_key") == _curated_output_key(
            source, multi_source, table_name)
    old_table = schema.get("transform_output_table")
    old_source = schema.get("source_dataset_id")
    if table_name and old_table != table_name:
        return False
    if not table_name and old_table:
        return False
    if multi_source and old_source and old_source != source.get("dataset_id"):
        return False
    return True


def _disambiguated_curated_name(db, base_name: str, pipeline_id: str,
                                output_key: str) -> str:
    """同名已被其他产物占用时生成确定、可诊断且满足 200 字符限制的名字。"""
    from app.data_channel.datasets.models import Dataset as _DS

    digest = hashlib.sha256(f"{pipeline_id}:{output_key}".encode("utf-8")).hexdigest()[:8]
    suffix = f" [{digest}]"
    candidate = f"{base_name[:200 - len(suffix)].rstrip()}{suffix}"
    n = 2
    while db.query(_DS).filter(_DS.kind == "curated", _DS.name == candidate).first():
        numbered = f" [{digest}-{n}]"
        candidate = f"{base_name[:200 - len(numbered)].rstrip()}{numbered}"
        n += 1
    return candidate


def resolve_curated_target(db, pl, source: dict, multi_source: bool,
                           table_name: str | None = None):
    """按 (pipeline_id, output_key) 找产物；名字只作经归属校验的兼容兜底。

    流水线名称可修改，也可在归档后被另一条新流水线复用，不能充当资产身份。
    历史资产尚无 output_key 时，仅允许同流水线或 target_curated_ids 中明确绑定
    的无主资产按旧槽位规则认领。发现同名属于别的流水线时，返回一个不冲突的
    展示名创建新资产，绝不复用对方的版本史、主键契约和下游映射。
    """
    from app.data_channel.datasets.models import Dataset as _DS
    from app.data_channel.datasets.lake_gate import LakeGateError

    ds_name = _curated_name(pl, source, multi_source, table_name)
    output_key = _curated_output_key(source, multi_source, table_name)
    target_ids = [c for c in (pl.target_curated_ids or []) if c]

    # 稳定身份是数据库真实列 + 唯一索引；JSON 仅保留展示/兼容元数据。
    stable = db.query(_DS).filter(
        _DS.kind == "curated",
        _DS.producer_pipeline_id == pl.id,
        _DS.output_key == output_key,
    ).all()
    if len(stable) > 1:
        ids = [c.id for c in stable]
        raise LakeGateError(
            f"流水线「{pl.name}」产物身份 ({pl.id}, {output_key}) 对应多个资产 {ids}，"
            f"无法安全决定写入目标。请先保留正确资产并解除其余重复绑定。")
    if stable:
        return stable[0], ds_name

    # 迁移前 JSON 身份只作为一次性 legacy 兼容：命中后写入路径会在同一事务补齐
    # 真实列。不能用名称替代，也不能容忍一对多。
    legacy_stable = db.query(_DS).filter(
        _DS.kind == "curated",
        _DS.producer_pipeline_id.is_(None),
        _DS.schema_json["pipeline_id"].as_string() == pl.id,
        _DS.schema_json["output_key"].as_string() == output_key,
    ).all()
    if len(legacy_stable) > 1:
        raise LakeGateError(
            f"流水线「{pl.name}」的 legacy 产物身份 ({pl.id}, {output_key}) 存在重复 "
            f"{[c.id for c in legacy_stable]}，拒绝按名称猜测。")
    if legacy_stable:
        return legacy_stable[0], ds_name

    # 兼容历史数据：只从流水线显式绑定的 target ids 中认领没有 output_key 的资产。
    legacy_bound = []
    bound_candidates = (db.query(_DS).filter(
        _DS.kind == "curated", _DS.id.in_(target_ids)).all()
        if target_ids else [])
    for c in bound_candidates:
        if c.producer_pipeline_id not in (None, "", pl.id):
            continue
        schema = dict(c.schema_json or {})
        owner = schema.get("pipeline_id")
        if owner not in (None, "", pl.id):
            continue
        if _legacy_output_matches(schema, source, multi_source, table_name):
            legacy_bound.append(c)
    if len(legacy_bound) > 1:
        ids = [c.id for c in legacy_bound]
        raise LakeGateError(
            f"流水线「{pl.name}」的历史产物绑定存在歧义：槽位 {output_key} 命中 {ids}。"
            f"为避免串湖，本次运行已停止；请清理 target_curated_ids 后重试。")
    if legacy_bound:
        return legacy_bound[0], ds_name

    # 名称兜底也必须验证归属；仅同一流水线且槽位兼容时可复用。
    by_name = db.query(_DS).filter(
        _DS.kind == "curated", _DS.name == ds_name).first()
    if by_name is None:
        return None, ds_name
    schema = dict(by_name.schema_json or {})
    if ((by_name.producer_pipeline_id == pl.id or schema.get("pipeline_id") == pl.id)
            and _legacy_output_matches(schema, source, multi_source, table_name)):
        return by_name, ds_name
    return None, _disambiguated_curated_name(db, ds_name, pl.id, output_key)


def _save_curated_dataset(db, svc, pl, source: dict, data, ctx, multi_source: bool, table_name: str | None = None, write_opts: dict | None = None, contract_columns: list[str] | None = None, run_params: dict | None = None, producer_run_id: str | None = None) -> dict:
    """入湖是物理湖表上的行级 upsert（lake_store），全程持数据集写锁。

    任务调度与手动运行并发落同一 curated 数据集时后到者等待；不锁则双方
    各自基于旧状态写入，先提交的增量被后提交者静默覆盖。锁键优先用已绑定
    数据集的 id（改名后名字会变、id 不变）；首建场景退回名字锁，
    get-or-create 也在锁内，同名数据集的创建竞争一并串行化。
    data：全量 list[dict] 或批次迭代器 Iterable[list[dict]]（流式入湖）。
    """
    from app.data_channel.datasets.lock import dataset_write_lock
    from app.data_channel.pipeline_tasks.merge import normalize_write_mode

    # 在任何空输出短路/目标创建之前验证，未知模式不能借 skip_empty 伪装成功。
    if write_opts is not None:
        write_opts = {**write_opts, "mode": normalize_write_mode(write_opts.get("mode"))}

    bound_ds, ds_name = resolve_curated_target(db, pl, source, multi_source, table_name)
    # 已有资产与 DatasetService/人工维护共用 dataset::{id} 锁；首建尚无 id，
    # 以稳定产物身份（pipeline + output key）锁住创建竞争，而非展示名。
    output_key = _curated_output_key(source, multi_source, table_name)
    lock_key = (f"dataset::{bound_ds.id}" if bound_ds is not None
                else f"curated-output::{pl.id}::{output_key}")
    with dataset_write_lock(lock_key, bind=db.get_bind()):
        return _save_curated_dataset_in_lock(
            db, svc, pl, source, data, ctx, multi_source, table_name,
            write_opts, contract_columns, ds_name,
            bound_ds_id=(bound_ds.id if bound_ds is not None else None),
            run_params=run_params, producer_run_id=producer_run_id)


def _save_curated_dataset_in_lock(db, svc, pl, source: dict, data, ctx, multi_source: bool, table_name: str | None, write_opts: dict | None, contract_columns: list[str] | None, ds_name: str, bound_ds_id: str | None = None, run_params: dict | None = None, producer_run_id: str | None = None) -> dict:
    # 复用既有 curated 数据集追加版本：同一管道反复运行不再无限增殖新数据集，
    # 下游 mapping 绑定的 curated id 保持稳定、能持续收到新版本。
    # 绑定关系按 id（resolve_curated_target），流水线改名不影响归属
    from app.data_channel.datasets.models import Dataset as _DS
    from app.data_channel.datasets.lake_gate import LakeGateError
    from app.data_channel.pipeline_tasks.merge import normalize_write_mode

    if write_opts is not None:
        write_opts = {**write_opts, "mode": normalize_write_mode(write_opts.get("mode"))}
    output_key = _curated_output_key(source, multi_source, table_name)
    curated_ds = db.query(_DS).filter(_DS.id == bound_ds_id).first() if bound_ds_id else None
    if curated_ds is None:
        same_name = db.query(_DS).filter(
            _DS.kind == "curated", _DS.name == ds_name).first()
        if same_name is not None:
            schema = dict(same_name.schema_json or {})
            if (schema.get("pipeline_id") != pl.id
                    or not _legacy_output_matches(schema, source, multi_source, table_name)):
                raise LakeGateError(
                    f"成品资产名称「{ds_name}」在获取写锁后已被其他流水线/产物占用，"
                    f"本次运行停止以避免串湖，请重试让系统重新分配安全名称。")
            curated_ds = same_name
    if curated_ds is None:
        # 首建即写入身份，不留“先建空资产、稍后才补 pipeline_id”的并发窗口。
        curated_ds = svc.create_dataset(
            name=ds_name, kind="curated", schema_json={
                "pipeline_id": pl.id,
                "output_key": output_key,
                "source_dataset_id": source.get("dataset_id"),
                **({"transform_output_table": table_name} if table_name else {}),
            }, producer_pipeline_id=pl.id, output_key=output_key, commit=False)
    elif curated_ds.producer_pipeline_id is None:
        # 仅对经过 target id / 同 owner 校验的 legacy 资产补齐稳定身份；随本次
        # 新版本同事务提交，唯一约束会拒绝任何并发重复认领。
        curated_ds.producer_pipeline_id = pl.id
        curated_ds.output_key = output_key

    # ── 资产湖准入闸门：行格式规范化 + 主键契约（声明仲裁/三校验）+ 列漂移检测。
    # 主键违规抛 LakeGateError → 运行失败，错误身份的数据不入湖。
    # 流式管道：闸门按批校验（LakeGateStream），来数逐批灌入 payload 暂存表，
    # Python 峰值内存只与一个批次块相关，与来数总量脱钩。
    from app.data_channel.datasets.lake_gate import (
        LakeGateStream, persist_contract, split_pk, infer_columns_typed)
    # 流水线字段契约（改名/非空/主键）仅适用于单产物运行：多源/拆分的
    # 契约粒度是「每个数据集一个」，流水线级契约对不上，跳过并在警告里说明
    contract_applicable = table_name is None and not multi_source
    column_defs = pl.column_definitions if contract_applicable else None

    # 入库方式：任务触发时按 write_mode 与资产湖已有数据合并；
    # 手动运行不带 write_opts，保持原行为（本次输出即新版本 = 全量覆盖）。
    # 存储层是 lake_store 物理湖表（行级 upsert + 版本元数据 + 行级变更集），
    # 不再读整份基座 blob、也不再写整份 Parquet 快照。
    from app.data_channel.datasets import lake_store
    from app.data_channel.pipeline_tasks.merge import _apply_soft_delete

    merge_meta: dict = {}
    lake_impact: dict | None = None
    mode = "overwrite"  # 手动/预览确认运行 = 全量覆盖（现行语义）
    soft_col = ""
    if write_opts is not None:
        mode = write_opts["mode"]  # 上游已 normalize_write_mode
        soft_col = str(write_opts.get("soft_delete_column") or "")

    gate_stream = LakeGateStream(
        curated_ds, write_opts, engine_contract_cols=contract_columns,
        column_definitions=column_defs)
    writer = lake_store.PayloadStageWriter(db)
    row_count = 0
    filled_cells = 0
    total_cells = 0
    sample_rows: list[dict] = []  # 前 50 行（output_sample 与类型推断共用）
    # 增量游标水位：取当次产出游标列的词法最大值（湖内全 TEXT 存储口径，
    # 经同一 snapshot_cell_text 文本化后比较）；空产出不推进
    from app.data_channel.datasets.service import snapshot_cell_text
    cursor_column = str((run_params or {}).get("cursor_column") or "").strip()
    watermark_max: str | None = None
    # 单批来数超上限即拒绝执行（流式后它是总量保险丝：累计超限即中断，
    # 部分暂存随失败回滚清理）：让 pipeline_max_in_memory_rows 名副其实。
    from app.config import settings
    max_in_memory = int(getattr(settings, "pipeline_max_in_memory_rows", 0) or 0)
    try:
        batches = [data] if isinstance(data, list) else data
        for batch in batches:
            gated = gate_stream.accept_batch(batch)
            # 软删除来数打标前置于契约固化（与现行链就地打标后再记账一致）；
            # staged_input 形态下入湖侧不再重复应用
            if mode == "upsert" and soft_col:
                _apply_soft_delete(gated, soft_col)
            for r in gated:
                total_cells += len(r)
                filled_cells += sum(1 for v in r.values()
                                    if v is not None and str(v).strip() != "")
            if len(sample_rows) < 50:
                sample_rows.extend(gated[:50 - len(sample_rows)])
            if cursor_column:
                for r in gated:
                    text = snapshot_cell_text(r.get(cursor_column))
                    if text and (watermark_max is None or text > watermark_max):
                        watermark_max = text
            row_count += len(gated)
            if max_in_memory > 0 and row_count > max_in_memory:
                from app.data_channel.datasets.lake_gate import LakeGateError
                raise LakeGateError(
                    f"本次流水线输出超过平台单批内存处理上限 "
                    f"pipeline_max_in_memory_rows={max_in_memory}（环境变量 "
                    f"PIPELINE_MAX_IN_MEMORY_ROWS，已读取 {row_count} 行）。"
                    "为保护执行进程，本次运行已拒绝；"
                    "请在流水线中过滤/拆分来数，或联系管理员调大该配置。")
            writer.write(gated)
        gate_result = gate_stream.finish()
    except Exception:
        # 拒绝/失败不留锁内未提交状态（含首建的数据集行），对齐旧「锁前
        # 拒绝」的零副作用语义；暂存表随回滚一并清理
        writer.drop()
        db.rollback()
        raise
    effective_pk = gate_result["pk"]
    gate_warnings = list(gate_result["warnings"])
    if not contract_applicable and (pl.column_definitions or []):
        gate_warnings.append(
            "该产物来自多源/多表拆分，流水线级字段契约未应用（契约仅适用于单产物流水线）")
    pk_cols = split_pk(effective_pk)
    ctx.rows_in = row_count
    ctx.rows_out = row_count
    # 类型推断/输出列：软删除打标后的前 50 行样本 + 全量首现列并集
    # （与旧 infer_columns_typed(全量行) 同口径：列序并集、类型只看前 50 行）
    batch_typed = infer_columns_typed(
        sample_rows + [{c: "" for c in writer.columns_union}]) if row_count else []

    if write_opts:
        if not row_count and write_opts.get("skip_empty", True):
            # 空输出保护：本次流水线输出 0 行，跳过入库，避免误清空资产
            logger.info(
                "入湖跳过[空输出保护]: pipeline=%s dataset=%s 本次输出 0 行，"
                "按 skip_empty 跳过入库（湖中数据保持不变）",
                pl.id, curated_ds.id)
            writer.drop()
            return {
                "source_dataset_id": source["dataset_id"],
                "source_file": source["filename"],
                "route": source["route"],
                "table_name": table_name,
                "curated_dataset_id": curated_ds.id,
                "dataset_version_id": None,
                "rows_in": ctx.rows_in,
                "rows_out": 0,
                "lake_rows": None,
                "skipped": "empty_output",
                "meta": _slim_ctx_meta(ctx.meta),
            }

    # 迁移未覆盖的数据集（无物理表但有 blob 历史）：先以遗留基座懒引导，
    # rows_before/审计 diff 才能取到真实基座（overwrite 的审计口径也是
    # 「与上一版本内容的差异」，同样需要基座）
    svc.bootstrap_lake_base(curated_ds)
    lake_rows_before = lake_store.count_rows(db, curated_ds)

    schema_to_publish: dict | None = None
    if row_count or (mode != "overwrite" and lake_rows_before):
        # 契约字段与当前版本内容一起发布；任何错误都必须让整次入湖失败。
        # 湖中列预算与 lake_store 内部演化同一并集规则：overwrite = 本批输出
        # 列（资产重建）；增量 = 历史并集（既有列保留既有类型，新列按本批推断）
        if mode == "overwrite":
            lake_columns_typed = batch_typed
        else:
            old_schema = dict(curated_ds.schema_json or {})
            existing_cols = [str(c) for c in old_schema.get("columns") or []]
            prev_typed = {str(item.get("name")): item
                          for item in old_schema.get("columns_typed") or []
                          if isinstance(item, dict) and item.get("name")}
            batch_by_name = {item["name"]: item for item in batch_typed}
            projected = existing_cols + [
                item["name"] for item in batch_typed
                if item["name"] not in existing_cols]
            lake_columns_typed = [
                prev_typed.get(name) or batch_by_name.get(name)
                or {"name": name, "type": "string"}
                for name in projected]
        schema_to_publish = persist_contract(
            curated_ds, pk=effective_pk,
            pk_source=gate_result["pk_source"],
            lake_columns_typed=lake_columns_typed,
            output_columns=writer.columns_union,
            column_definitions=column_defs,
            allow_redeclare=(write_opts or {}).get("mode") in (None, "", "overwrite"))
        # 质量分：非空来数按跨批累积的填充率精确计算（与旧全量口径一致）；
        # 空增量时质量分以湖中现状为样本（首页近似，避免物化全湖）
        if row_count:
            schema_to_publish["quality_score"] = _quality_from_counts(
                filled_cells, total_cells, row_count, ctx.meta)
        else:
            sample = lake_store.page_rows(db, curated_ds, 0, 1000)
            schema_to_publish["quality_score"] = _compute_quality_score(
                sample, ctx.meta)
        schema_to_publish["route"] = source["route"]
        schema_to_publish["source_dataset_id"] = source["dataset_id"]
        schema_to_publish["pipeline_id"] = pl.id
        schema_to_publish["output_key"] = output_key
        if write_opts:
            schema_to_publish["write_mode"] = mode
        if table_name:
            schema_to_publish["transform_output_table"] = table_name
    if schema_to_publish is not None:
        # 版本内容与解释它的逻辑契约必须同一事务发布（upsert_run 内提交）。
        curated_ds.schema_json = schema_to_publish
        db.flush()

    ver, changeset = lake_store.upsert_run(
        db, curated_ds, None, mode, pk_cols, soft_delete_column=soft_col,
        rows_before=lake_rows_before,
        staged_input=(writer.stage, writer.columns_union, row_count),
        producer_run_id=producer_run_id)
    # 版本保留窗口（元数据 + 变更集；回放链完整性规则见 _prune_versions）：
    # 与 blob 路径的 _create_version_locked 尾部一致，机会式清理
    svc._prune_versions_best_effort(curated_ds.id)
    lake_rowcount = int(ver.rowcount or 0)
    if write_opts:
        merge_meta = {"mode": mode,
                      # legacy 口径：overwrite 的合并基座固定为空，rows_before 恒 0
                      "rows_before": (0 if mode == "overwrite" else lake_rows_before),
                      "rows_new": row_count, "rows_after": lake_rowcount}
        lake_impact = _lake_impact_from_changeset(
            db, changeset, pk_cols, lake_rows_before, lake_rowcount)
        logger.info(
            "入湖完成: pipeline=%s dataset=%s version_no=%s mode=%s 来数=%d "
            "新增=%d 更新=%d 删除=%d 湖中行数=%d",
            pl.id, curated_ds.id, ver.version_no, mode, row_count,
            changeset.added_count, changeset.updated_count,
            changeset.deleted_count, lake_rowcount)

    # 审计：本次流水线输出样本（入库前的产物），供执行记录追溯「流水线的输出是什么」
    from app.data_channel.pipeline_tasks.merge import _slim_row
    output_columns = [c["name"] for c in batch_typed]
    output_sample = [_slim_row(r) for r in sample_rows]

    return {
        "source_dataset_id": source["dataset_id"],
        "source_file": source["filename"],
        "route": source["route"],
        "table_name": table_name,
        "curated_dataset_id": curated_ds.id,
        "curated_dataset_name": curated_ds.name,
        "dataset_version_id": ver.id,
        "version_no": ver.version_no,
        "rows_in": ctx.rows_in,
        "rows_out": row_count,
        "lake_rows": lake_rowcount,
        "output_columns": output_columns,
        "output_sample": output_sample,
        "lake_impact": lake_impact,
        "merge": merge_meta or None,
        "primary_key": effective_pk or None,
        "pk_source": gate_result["pk_source"] or None,
        "schema_drift": gate_result["drift"],
        "gate_warnings": gate_warnings or None,
        "watermark_after": watermark_max,
        "meta": _slim_ctx_meta(ctx.meta),
    }


def _save_curated_outputs(db, svc, pl, source: dict, data, ctx, multi_source: bool, write_opts: dict | None = None, contract_columns: list[str] | None = None, run_params: dict | None = None, producer_run_id: str | None = None) -> list[dict]:
    split_tables = ctx.meta.get("split_tables")
    if isinstance(split_tables, dict) and split_tables:
        outputs = []
        for table_name, rows in split_tables.items():
            outputs.append(_save_curated_dataset(
                db, svc, pl, source, rows or [], ctx, multi_source=True, table_name=str(table_name), write_opts=write_opts, contract_columns=contract_columns, run_params=run_params, producer_run_id=producer_run_id
            ))
        return outputs
    return [_save_curated_dataset(db, svc, pl, source, data, ctx, multi_source, write_opts=write_opts, contract_columns=contract_columns, run_params=run_params, producer_run_id=producer_run_id)]


def pipeline_run_task(pipeline_id: str, run_id: str, write_opts: dict | None = None):
    """Pipeline 执行任务 — 经 engine_registry 分发到采集引擎（n8n / python / 运行时注册）"""
    from app.database import SessionLocal
    from app.data_channel.pipelines.models import Pipeline, PipelineRun, PipelineVersion
    from app.config import settings

    db = SessionLocal()
    run = None  # except 块引用；首个查询即抛异常时不能 NameError 掩盖原始错误
    try:
        run = db.query(PipelineRun).filter(PipelineRun.id == run_id).first()
        if not run:
            return
        run.status = "running"
        run.started_at = datetime.now(timezone.utc)
        db.commit()

        pl = db.query(Pipeline).filter(Pipeline.id == pipeline_id).first()
        if not pl:
            run.status = "failed"
            run.error_log = "Pipeline not found"
            db.commit()
            return

        if settings.environment == "production":
            if (pl.status or "") != "published":
                raise ValueError("生产运行拒绝草稿流水线：必须先发布不可变版本")
            if not bool(pl.enabled):
                raise ValueError("生产运行拒绝未启用流水线")

        published_version = db.query(PipelineVersion).filter(
            PipelineVersion.pipeline_id == pl.id,
            PipelineVersion.version == pl.version,
            PipelineVersion.status == "published",
        ).order_by(PipelineVersion.created_at.desc()).first()
        if settings.environment == "production" and published_version is None:
            raise ValueError(f"Pipeline v{pl.version} 缺少发布快照，拒绝执行")
        if published_version is not None:
            if ((published_version.definition or {}) != (pl.definition or {})
                    or (published_version.column_definitions or []) != (pl.column_definitions or [])):
                raise ValueError("Pipeline live 配置与发布快照不一致，拒绝执行漂移版本")
        # stats 只写轻量标量：definition/spec/契约快照与发布版本表重复、
        # 无任何消费方，逐 run 复制只会让 v2_pipeline_runs 单行体积失控。
        run.stats = {**(run.stats or {}), "pipeline_version": pl.version}
        db.commit()

        # ── 采集引擎分发：definition.engine 决定行数据来源，入湖通道共用 ──
        from app.data_channel.pipelines.engine_registry import (
            get_engine_runner, known_engines)
        engine = (pl.definition or {}).get("engine")
        runner = get_engine_runner(engine)
        if runner is None:
            run.status = "failed"
            run.error_log = (f"未知采集引擎「{engine}」，已注册引擎：{known_engines()}。"
                             "系统自定义（canvas）与 route A/B/C 流水线已下线；"
                             "接入新引擎请在 engine_registry 登记 runner。")
            run.finished_at = datetime.now(timezone.utc)
            db.commit()
            return
        runner(db, pl, run, write_opts)

    except Exception as e:
        logger.error(f"Pipeline run failed: {e}")
        if run:
            run.status = "failed"
            run.error_log = str(e)
            run.finished_at = datetime.now(timezone.utc)
            # 运行失败不夺走发布态：failed 属于这次 run，不属于流水线生命周期
            db.commit()
    finally:
        db.close()
