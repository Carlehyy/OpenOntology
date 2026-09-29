"""成品数据集物理湖表存储层（仅 kind='curated'）。

每个成品数据集对应一张真实物理表，版本语义 = 版本元数据 + 行级变更集
（v2_dataset_changesets / v2_dataset_changeset_rows），替代整份 Parquet
blob 快照。历史 data_blob/storage_uri 版本一律保留，读取由调用方按
``data_blob IS NULL`` 分流（迁移期混合态）。

命名与结构约定（一经发布不可变）：
- 物理表名：``lake_ds_<dataset_id 去横线>``（40 字符，低于 PG 63 标识符上限）；
- 列全部 TEXT NOT NULL（非主键列 server_default ''，缺省值/缺列一律写 ""，
  对齐快照 restval 语义），逻辑列名 → 物理列名做净化/去重（空名、NUL、
  超长截断、截断后重名加序号后缀），映射按契约列序持久化在
  ``Dataset.schema_json['lake_columns']``（{逻辑列名: 物理列名}）；
- 契约主键（schema_json['primary_key']，逗号分隔）对应的物理列建复合
  PRIMARY KEY 约束 ``pk_lake_ds_<id 去横线>``，DB 级唯一性承接 lake_gate
  的主键去重职责；未声明主键的资产不建约束（无行级身份，回放按整行签名，
  完全相同重复行的回放在该场景下不保证逐条精确）；
- 结构发布后不可变：ensure_lake_table 校验物理表列/主键与契约一致，不一致
  抛 LakeStoreStructureError；来数含契约外列时 upsert_run 同样抛错，绝不
  静默丢列（变更结构须先重建契约）。

写路径（upsert_run）：
- 方言可移植（SQLite/PG 均可）：每次运行建 TEMP 暂存表（同一连接/事务），
  批量灌入规范化来数，SQL 计算 added/updated/deleted，再按主键
  DELETE + INSERT 应用，不用 PG 专属 ON CONFLICT；
- write_mode 语义对齐参考实现 pipeline_tasks/merge.py 及其闸门链：
    overwrite     全量替换（来数按主键末现去重后即为湖；incoming 外的主键删除）。
                  结构漂移（列集合/主键与契约不一致）时整表重建（DROP+CREATE），
                  与「全量覆盖是变更主键/列契约的唯一受控通道」对齐
    append        仅新增；撞上既有主键或批内重复主键 = 运行失败（抛
                  LakeGateError，口径对齐 validate_merged_lake：追加不合并，
                  有主键的资产应改用 upsert），不是静默跳过
    append_dedup  先按整行内容（快照文本签名）对来数批内去重、跳过与湖中全同
                  的行，再按 append 同样口径撞键失败
    upsert        新增 + 更新既有（主键末现去重，后者覆盖前者）；
                  soft_delete_column 非空时按 merge._apply_soft_delete 语义对
                  来数就地打 __deleted__/__deleted_at__ 标记而非物理删除，
                  湖中未触及行也按同一词表重评估（truthy 未打标的补打、非
                  truthy 的摘除标记=复活；已正确打标的保持原标记与时间戳）；
                  标记列视作普通输出列进物理表（走列演化）
- 列演化：无发布字段契约（schema_json 无 contract_definitions）的资产，增量
  来数的新列经 evolve_lake_table_columns 做 ALTER TABLE ADD COLUMN
  （TEXT NOT NULL DEFAULT ''）并入 lake_columns/columns 并集，保持现行
  「湖中列=历史并集」语义；有契约时契约外列已在 lake_gate 硬失败，这里兜底
  抛 LakeStoreStructureError；
- 行值规范化沿用 snapshot_text.snapshot_cell_text 的湖内文本语义（None→""，
  dict/list→JSON 文本，bytes→"<N bytes>"）；主键列额外 strip，与审核
  row_pk 编码（encode_row_pk：单主键纯文本 / 复合紧凑 JSON 数组）保持同一
  身份口径，changeset_rows.row_pk 即该编码；无主键资产的 row_pk 退化为
  整行签名（紧凑 JSON），仅供回放定位；
- 每次运行发布一个 DatasetVersion（data_blob/data_size/storage_uri=NULL，
  rowcount=应用后表行数，checksum=变更集规范哈希）+ DatasetVersionEvent
  （version_published）同事务 outbox，发布终局对齐
  DatasetService._create_version_locked（Dataset 行锁 + populate_existing +
  版本号撞 (dataset_id, version_no) 唯一约束时回滚重试）；空湖上的空运行
  （无行无契约无物理表）只发空版本与空变更集，不建表；
- 事务与锁：upsert_run 自行 commit；调用方必须已持有 dataset_write_lock
  （dataset::{id}）串行化完整读改写。运行时调用方是
  pipeline_run._save_curated_dataset_in_lock；迁移未覆盖数据集（无物理表
  但有 blob 历史）的基座引导由 DatasetService.bootstrap_lake_base 执行，
  本模块拒绝在「有快照历史而无物理表」的资产上直接入湖（防存量被当空湖
  丢失）；版本保留窗口的联动清理由调用方经
  DatasetService._prune_versions_best_effort 执行（存储层不反向依赖服务层）；
- overwrite + rows=[] 会清空物理表：空输出保护（skip_empty）属于调用方闸门，
  存储层不二次猜测。

读路径：count_rows / page_rows（主键序真分页）/ stream_rows（分批生成器）/
rows_by_pks（row_pk → 行）/ rows_at_version（物理表当前状态 + 变更集逆向
回放，只物化变化行；目标为迁移前 blob 版本时抛 LakeStoreLegacyVersionError，
由调用方走 DatasetService 遗留解析路径）。物理表尚不存在时读作空湖。
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.data_channel.datasets.lake_gate import LakeGateError, split_pk
from app.data_channel.datasets.models import (
    Dataset,
    DatasetChangeset,
    DatasetChangesetRow,
    DatasetVersion,
    DatasetVersionEvent,
)
from app.data_channel.datasets.snapshot_text import snapshot_cell_text
from app.data_channel.pipeline_tasks.write_modes import (
    SOFT_DELETE_TRUTHY,
    _apply_soft_delete,
    normalize_write_mode,
)

LAKE_TABLE_PREFIX = "lake_ds_"
# PG 标识符上限 63 字节；物理列名按字节预算截断，给去重后缀留余量
_COLUMN_NAME_MAX_BYTES = 48
# 批量写入/按键批取的参数包大小（远低于 SQLite 变量上限）
_BATCH_SIZE = 500
# 软删除 truthy 词表与 write_modes._apply_soft_delete 共用同一常量：仅用于
# 湖中候选行的 SQL 预筛，精确判定仍在 Python 侧走 _apply_soft_delete。
# 引用而非复制——单边改词表会让「打标」与「湖中重评估」口径分裂且无测试会红
_SOFT_DELETE_TRUTHY = SOFT_DELETE_TRUTHY
_SOFT_MARKER_COLS = ("__deleted__", "__deleted_at__")

logger = logging.getLogger(__name__)


class LakeStoreError(RuntimeError):
    """物理湖表读写失败。"""


class LakeStoreStructureError(LakeStoreError):
    """物理表结构与数据集契约（列/主键）不一致——结构发布后不可变。"""


class LakeStoreLegacyVersionError(LakeStoreError):
    """目标版本是迁移前的整份快照（data_blob/storage_uri），须走遗留解析路径。"""


# ── 命名与契约 ──────────────────────────────────────────────
def uses_lake_table(dataset) -> bool:
    """只有成品数据集（kind='curated'）使用物理湖表。"""
    return str(getattr(dataset, "kind", "") or "").strip().lower() == "curated"


def version_uses_lake(dataset, version) -> bool:
    """版本内容是否在物理湖表：curated 且无 blob/storage_uri 载荷。

    用非 deferred 的 data_size 列判定（避免触发 deferred blob 加载）：湖表
    版本 data_size/storage_uri 均为 NULL；历史 blob 版本 data_size 非空、
    迁移前版本有 storage_uri。
    """
    return (version is not None and uses_lake_table(dataset)
            and version.data_size is None and not version.storage_uri)


def lake_table_name(dataset_id: str) -> str:
    """物理表名：lake_ds_<dataset_id 去横线>（40 字符）。"""
    return f"{LAKE_TABLE_PREFIX}{str(dataset_id).replace('-', '')}"


def sanitize_lake_column(name, taken: set[str]) -> str:
    """逻辑列名 → 物理列名：去 NUL/两端空白、按字节截断（不切字符）、重名加后缀。"""
    base = str(name if name is not None else "").replace("\x00", "").strip()
    if not base:
        base = "col"
    base = base.encode("utf-8")[:_COLUMN_NAME_MAX_BYTES].decode("utf-8", "ignore")
    candidate = base
    index = 2
    while candidate in taken:
        suffix = f"_{index}"
        head = base.encode("utf-8")[: _COLUMN_NAME_MAX_BYTES - len(suffix)].decode(
            "utf-8", "ignore")
        candidate = f"{head}{suffix}"
        index += 1
    taken.add(candidate)
    return candidate


def build_lake_column_mapping(columns: list[str]) -> dict[str, str]:
    """按契约列序生成「逻辑列名 → 物理列名」映射（同名逻辑列首现为准）。"""
    mapping: dict[str, str] = {}
    taken: set[str] = set()
    for col in columns:
        logical = str(col)
        if logical in mapping:
            continue
        mapping[logical] = sanitize_lake_column(logical, taken)
    return mapping


def lake_columns_from_rows(rows: list[dict]) -> list[str]:
    """从行数据按首现顺序提取逻辑列（跳过 content 二进制列，与快照语义一致）。"""
    columns: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row.keys():
            logical = str(key)
            if logical == "content" or logical in seen:
                continue
            seen.add(logical)
            columns.append(logical)
    return columns


def lake_table_definition(table_name: str, mapping: dict[str, str],
                          pk_cols: list[str]) -> sa.Table:
    """物理湖表的 Core 定义（供 DDL 与 DML；Alembic 数据迁移同样经此建表）。"""
    pk_physical = [mapping[c] for c in pk_cols]
    columns: list[sa.Column] = []
    for logical, physical in mapping.items():
        if physical in pk_physical:
            columns.append(sa.Column(physical, sa.Text, nullable=False))
        else:
            columns.append(
                sa.Column(physical, sa.Text, nullable=False, server_default=""))
    constraints: list = []
    if pk_physical:
        constraints.append(
            sa.PrimaryKeyConstraint(*pk_physical, name=f"pk_{table_name}"))
    return sa.Table(table_name, sa.MetaData(), *columns, *constraints)


def _connection(db):
    """Session 取其事务内连接（DDL/DML 与 ORM 同事务同连接）；Connection 原样返回。"""
    return db.connection() if isinstance(db, Session) else db


def _contract(dataset) -> tuple[dict, dict[str, str] | None, list[str]]:
    """(schema, lake_columns 映射或 None, 契约主键列)。"""
    schema = dict(getattr(dataset, "schema_json", None) or {})
    raw_mapping = schema.get("lake_columns")
    mapping = ({str(k): str(v) for k, v in raw_mapping.items()}
               if isinstance(raw_mapping, dict) else None)
    return schema, mapping, split_pk(schema.get("primary_key"))


def _assert_structure(conn, table_name: str, mapping: dict[str, str],
                      pk_cols: list[str]) -> None:
    inspector = sa.inspect(conn)
    actual_cols = [c["name"] for c in inspector.get_columns(table_name)]
    expected_cols = list(mapping.values())
    if actual_cols != expected_cols:
        raise LakeStoreStructureError(
            f"物理湖表 {table_name} 的列 {actual_cols} 与契约 {expected_cols} 不一致。"
            "湖表结构发布后不可变；如确需变更列，请以全量覆盖重建该资产。")
    actual_pk = sorted(
        inspector.get_pk_constraint(table_name).get("constrained_columns") or [])
    expected_pk = sorted(mapping[c] for c in pk_cols)
    if actual_pk != expected_pk:
        raise LakeStoreStructureError(
            f"物理湖表 {table_name} 的主键 {actual_pk} 与契约主键 {expected_pk} 不一致。"
            "主键是资产的身份契约，不能以增量方式改写；如确需变更，请以全量覆盖重建。")


def ensure_lake_table(db, dataset, columns: list[str] | None = None) -> dict[str, str]:
    """确保物理湖表存在且与契约一致，返回「逻辑列名 → 物理列名」映射。

    表不存在时在同一事务内 CREATE TABLE（PG/SQLite DDL 均可回滚），并把列
    映射固化进 Dataset.schema_json['lake_columns']（随调用方事务提交，本函数
    不 commit）；显式传入的 columns 优先于 schema_json['columns'] 作为建表
    列契约。表已存在则校验列/主键与契约一致，不一致抛
    LakeStoreStructureError。
    """
    if not uses_lake_table(dataset):
        raise LakeStoreError(
            f"数据集 {getattr(dataset, 'id', None)} 不是成品数据集（kind='curated'），"
            "不使用物理湖表。")
    schema, mapping, pk_cols = _contract(dataset)
    if mapping is None:
        logical_cols = [str(c) for c in (columns or schema.get("columns") or [])]
        if not logical_cols:
            raise LakeStoreError(
                f"数据集「{getattr(dataset, 'name', '')}」尚无列契约，无法建立物理湖表。")
        mapping = build_lake_column_mapping(logical_cols)
    unknown_pk = [c for c in pk_cols if c not in mapping]
    if unknown_pk:
        raise LakeStoreStructureError(
            f"数据集「{getattr(dataset, 'name', '')}」的契约主键列 {unknown_pk} "
            "不在列契约中，无法建立/校验物理湖表。")
    conn = _connection(db)
    table_name = lake_table_name(dataset.id)
    if sa.inspect(conn).has_table(table_name):
        _assert_structure(conn, table_name, mapping, pk_cols)
    else:
        lake_table_definition(table_name, mapping, pk_cols).create(bind=conn)
    if schema.get("lake_columns") != mapping:
        schema["lake_columns"] = mapping
        dataset.schema_json = schema
        if isinstance(db, Session):
            db.flush()
    return mapping


def lake_table_exists(db, dataset_id: str) -> bool:
    """物理湖表是否存在（未入湖/迁移跳过/已删除均为 False）。"""
    return bool(sa.inspect(_connection(db)).has_table(lake_table_name(dataset_id)))


def drop_lake_table(db, dataset_id: str) -> None:
    """幂等删除物理湖表（随调用方事务提交，本函数不 commit）。"""
    _connection(db).execute(
        sa.text(f'DROP TABLE IF EXISTS "{lake_table_name(dataset_id)}"'))


def evolve_lake_table_columns(db, dataset, new_cols: list[str]) -> dict[str, str]:
    """无发布字段契约资产的列并集演化：ALTER TABLE ADD COLUMN。

    新列按首现序追加到既有契约列序末尾（TEXT NOT NULL DEFAULT ''，既有行
    自动回填空串，对齐快照 restval 语义）；schema_json 的 lake_columns /
    columns 同步为并集，随调用方事务提交（本函数不 commit）。返回更新后的
    「逻辑列名 → 物理列名」映射。主键与既有列不可经此变更；有发布契约
    （contract_definitions）的资产不允许走这里（gate 已先行硬失败）。
    """
    schema, mapping, pk_cols = _contract(dataset)
    if mapping is None:
        raise LakeStoreError(
            f"数据集「{getattr(dataset, 'name', '')}」尚无湖表列映射，"
            "请先 ensure_lake_table。")
    conn = _connection(db)
    table_name = lake_table_name(dataset.id)
    if not sa.inspect(conn).has_table(table_name):
        raise LakeStoreError(
            f"物理湖表 {table_name} 不存在，请先 ensure_lake_table。")
    taken = set(mapping.values())
    for logical in [str(c) for c in new_cols]:
        if not logical or logical in mapping:
            continue
        physical = sanitize_lake_column(logical, taken)
        quoted = '"' + physical.replace('"', '""') + '"'
        conn.execute(sa.text(
            f'ALTER TABLE "{table_name}" ADD COLUMN {quoted}'
            " TEXT NOT NULL DEFAULT ''"))
        mapping[logical] = physical
    schema["lake_columns"] = mapping
    schema["columns"] = list(mapping.keys())
    dataset.schema_json = schema
    if isinstance(db, Session):
        db.flush()
    return mapping


def _table_pk_columns(conn, table_name: str) -> list[str]:
    return list(sa.inspect(conn).get_pk_constraint(table_name)
                .get("constrained_columns") or [])


def _rebuild_lake_table(db, conn, dataset, schema: dict, columns: list[str],
                        pk_cols: list[str]) -> dict[str, str]:
    """overwrite 结构漂移时的整表重建（DROP + CREATE + 契约重写）。

    与「全量覆盖是变更主键/列契约的唯一受控通道」对齐：旧表数据随 DROP
    放弃——overwrite 本就全量替换，来数随后灌入新表。
    """
    if not columns:
        raise LakeStoreError(
            f"数据集「{getattr(dataset, 'name', '')}」overwrite 重建需要至少一列"
            "契约（来数与 schema_json['columns'] 均为空）。")
    logger.warning(
        "物理湖表整表重建（DROP+CREATE）: dataset=%s name=%s 新契约列数=%d "
        "主键=%s —— 旧表数据随 DROP 放弃",
        dataset.id, getattr(dataset, "name", ""), len(columns),
        pk_cols or "(无)",
    )
    drop_lake_table(db, dataset.id)
    mapping = build_lake_column_mapping(columns)
    unknown_pk = [c for c in pk_cols if c not in mapping]
    if unknown_pk:
        raise LakeStoreStructureError(
            f"数据集「{getattr(dataset, 'name', '')}」的契约主键列 {unknown_pk} "
            "不在重建列契约中。")
    lake_table_definition(lake_table_name(dataset.id), mapping,
                          pk_cols).create(bind=conn)
    schema["lake_columns"] = mapping
    schema["columns"] = list(mapping.keys())
    dataset.schema_json = schema
    if isinstance(db, Session):
        db.flush()
    return mapping


def _reconcile_structure(db, conn, dataset, schema: dict, mode: str,
                         pk_cols: list[str], source_columns: list[str],
                         ) -> tuple[dict[str, str], bool]:
    """ upsert 前的结构对齐，返回 (生效的列映射, 是否整表重建)。

    - 表不存在：按契约列（或来数列并集）建表；
    - overwrite：列集合或主键与契约漂移时整表重建（rebuilt=True，调用方
      据此把合并基座行数归零——旧表数据随 DROP 放弃）；
    - 增量模式：主键漂移拒绝（口径对齐 validate_upsert_base 的重建指引）；
      无契约时新列走并集演化，有契约时契约外列兜底抛错（gate 已先行拦截）。
    source_columns：来数列并集（首现序），由调用方在灌入暂存时累积供给。
    """
    table_name = lake_table_name(dataset.id)
    if not sa.inspect(conn).has_table(table_name):
        return ensure_lake_table(
            db, dataset,
            columns=schema.get("columns") or source_columns), False
    _, mapping, _ = _contract(dataset)
    if mapping is None:
        raise LakeStoreError(
            f"数据集「{getattr(dataset, 'name', '')}」的物理湖表存在，但 "
            "schema_json['lake_columns'] 缺失，无法安全对齐结构。")
    actual_pk = _table_pk_columns(conn, table_name)
    pk_drift = ([c for c in pk_cols if c not in mapping]
                or sorted(actual_pk) != sorted(mapping[c] for c in pk_cols))
    if mode == "overwrite":
        target_columns = [str(c) for c in (schema.get("columns") or [])]
        column_drift = (bool(target_columns)
                        and target_columns != list(mapping.keys()))
        if pk_drift or column_drift:
            return _rebuild_lake_table(
                db, conn, dataset, schema,
                target_columns or source_columns, pk_cols), True
        _assert_structure(conn, table_name, mapping, pk_cols)
        return mapping, False
    if pk_drift:
        raise LakeGateError(
            f"数据集「{getattr(dataset, 'name', '')}」湖中存量数据的物理主键"
            f"（{actual_pk or '无'}）与契约主键 {pk_cols} 不一致，无法安全执行"
            f"合并（{mode}）。请先用 overwrite 方式重建该资产，让全量数据经过"
            "主键校验后再切回增量模式。")
    extra = [c for c in source_columns if c not in mapping]
    if extra:
        markers = [c for c in extra if c in _SOFT_MARKER_COLS]
        business = [c for c in extra if c not in _SOFT_MARKER_COLS]
        if business and schema.get("contract_definitions"):
            raise LakeStoreStructureError(
                f"数据集「{getattr(dataset, 'name', '')}」存在已发布字段契约，"
                f"来数出现契约外列 {business[:20]}。已发布契约不允许额外列静默入湖，"
                "请重新试跑并发布流水线契约。")
        mapping = evolve_lake_table_columns(db, dataset, extra)
    _assert_structure(conn, table_name, mapping, pk_cols)
    return mapping, False


# ── 行规范化与行身份 ────────────────────────────────────────
def normalize_lake_rows(rows: list[dict], mapping: dict[str, str],
                        pk_cols: list[str]) -> list[dict]:
    """来数行 → 物理列文本行（snapshot_cell_text 语义；主键列额外 strip）。

    契约外列抛 LakeStoreStructureError（结构不可变，不静默丢列）；缺列补 ""
    （对齐快照 restval）。content 二进制列与快照序列化同一口径：从不入湖，
    不参与契约外列判定。
    """
    pk_physical = {mapping[c] for c in pk_cols}
    out: list[dict] = []
    for i, raw_row in enumerate(rows):
        row = {str(k): v for k, v in raw_row.items()}
        extra = sorted(set(row.keys()) - set(mapping) - {"content"})
        if extra:
            raise LakeStoreStructureError(
                f"来数第 {i + 1} 行起出现契约外列 {extra[:20]}。湖表结构发布后不可变，"
                "请核对流水线输出或以全量覆盖重建资产契约。")
        physical_row: dict[str, str] = {}
        for logical, physical in mapping.items():
            text = snapshot_cell_text(row.get(logical))
            if physical in pk_physical:
                text = text.strip()
            physical_row[physical] = text
        out.append(physical_row)
    return out


def _logical_row(physical_row: dict, mapping: dict[str, str]) -> dict[str, str]:
    return {logical: physical_row.get(physical, "")
            for logical, physical in mapping.items()}


def lake_row_pk(logical_row: dict, pk_cols: list[str]) -> str:
    """行的审核契约 row_pk：单主键纯文本，复合主键紧凑 JSON 数组。

    与 curated.review_row_identity.encode_row_pk 同一编码口径（主键值入库
    前已 strip，此处不再重复）。无主键资产退化为整行签名，仅供回放定位。
    """
    if pk_cols:
        values = [str(logical_row.get(c) or "") for c in pk_cols]
        if len(values) == 1:
            return values[0]
        return json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(logical_row, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def decode_lake_row_pk(row_pk: str, pk_cols: list[str]) -> tuple[str, ...]:
    """row_pk → 主键值元组（编码的逆运算，供 rows_by_pks 定位物理行）。"""
    if not pk_cols:
        raise LakeStoreError("无主键资产不支持按 row_pk 取行。")
    if len(pk_cols) == 1:
        return (str(row_pk),)
    try:
        parsed = json.loads(row_pk)
    except (TypeError, ValueError) as exc:
        raise LakeStoreError(
            f"复合主键 {pk_cols} 的 row_pk 不是合法 JSON 数组：{row_pk!r}") from exc
    if not isinstance(parsed, list) or len(parsed) != len(pk_cols):
        raise LakeStoreError(
            f"row_pk {row_pk!r} 与复合主键 {pk_cols} 的形状不一致。")
    return tuple(str(v) for v in parsed)


def _physical_signature(row: dict) -> str:
    """物理行的整行内容签名（无主键资产的行身份，对齐 merge._dedup_by_row）。"""
    return json.dumps(row, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def compute_changeset_checksum(dataset_id: str, change_type: str, added_count: int,
                               updated_count: int, deleted_count: int,
                               entries: list[dict]) -> str:
    """变更集规范哈希（计数 + 按 row_pk 排序的逐行明细之紧凑 JSON 的 SHA-256）。

    兼作对应 DatasetVersion.checksum：版本不可变性的新锚点。
    """
    payload = {
        "dataset_id": str(dataset_id),
        "change_type": change_type,
        "added_count": int(added_count),
        "updated_count": int(updated_count),
        "deleted_count": int(deleted_count),
        "rows": sorted(
            ([e["row_pk"], e["change_type"], e.get("old_row"), e.get("new_row")]
             for e in entries),
            key=lambda item: item[0],
        ),
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── 写入：upsert_run ────────────────────────────────────────
def _key_match(table: sa.Table, pk_physical: list[str],
               keys: list[tuple]) -> sa.sql.ColumnElement:
    """主键元组集合的 WHERE 条件（单列 IN / 复合行值 IN，双方言可移植）。"""
    if len(pk_physical) == 1:
        return table.c[pk_physical[0]].in_([key[0] for key in keys])
    return sa.tuple_(*[table.c[p] for p in pk_physical]).in_(keys)


def _chunked(values: list, size: int = _BATCH_SIZE):
    for start in range(0, len(values), size):
        yield values[start:start + size]


# ── 流式暂存 ─────────────────────────────────────────────
# 来数（list 或批次迭代器）统一先灌 JSON payload 暂存表（__seq 全程序号 +
# 单元格文本），列并集收齐、结构对齐后再逐批规范化为物理暂存表：任意大小
# 的来数批在 Python 侧只占一个批次块的内存，与来数总量脱钩。
_INGEST_CHUNK = 2000


class PayloadStageWriter:
    """来数 payload 暂存表写入器（与调用方同一连接/事务，随其回滚/提交）。

    单元格在写入时即按 snapshot_cell_text 文本化（与 normalize_lake_rows
    同一规范化函数）；content 二进制列从不入暂存（与快照序列化口径一致）。
    columns_union 按首现序累积（lake_columns_from_rows 同口径）。
    """

    def __init__(self, db):
        self._conn = _connection(db)
        self.stage = sa.Table(
            f"lake_payload_{uuid.uuid4().hex[:12]}", sa.MetaData(),
            sa.Column("__seq", sa.BigInteger, nullable=False),
            sa.Column("payload", sa.Text, nullable=False),
            prefixes=["TEMPORARY"],
        )
        self.stage.create(bind=self._conn)
        self.total = 0
        self.columns_union: list[str] = []
        self._seen_cols: set[str] = set()

    def write(self, rows: list[dict]) -> None:
        buf = []
        for row in rows:
            self.total += 1
            text_row = {str(k): snapshot_cell_text(v)
                        for k, v in row.items() if str(k) != "content"}
            for key in text_row:
                if key not in self._seen_cols:
                    self._seen_cols.add(key)
                    self.columns_union.append(key)
            buf.append({"__seq": self.total,
                        "payload": json.dumps(text_row, ensure_ascii=False)})
            if len(buf) >= _BATCH_SIZE:
                self._conn.execute(self.stage.insert(), buf)
                buf.clear()
        if buf:
            self._conn.execute(self.stage.insert(), buf)

    def drop(self) -> None:
        self.stage.drop(bind=self._conn)


def _materialize_physical_stage(conn, payload_stage: sa.Table, table: sa.Table,
                                mapping: dict[str, str], pk_cols: list[str],
                                dataset_name: str) -> tuple[sa.Table, str]:
    """payload 逐批读回 → 物理暂存表（附 __seq 序号列），返回 (stage, seq 列名)。

    契约外列在首个违例行抛出（行号/文案与旧全量 normalize_lake_rows 一致）；
    主键非空校验收集最小违例行号、全量灌完后抛出（与旧「先列校验、后主键
    校验」的两段顺序一致）。
    """
    seq_col = "__seq"
    suffix = 2
    while seq_col in set(mapping.values()):
        seq_col = f"__seq_{suffix}"
        suffix += 1
    stage = sa.Table(
        f"lake_stage_{uuid.uuid4().hex[:12]}", sa.MetaData(),
        *[sa.Column(c.name, sa.Text, nullable=True) for c in table.columns],
        sa.Column(seq_col, sa.BigInteger, nullable=False),
        prefixes=["TEMPORARY"],
    )
    stage.create(bind=conn)
    pk_physical = [mapping[c] for c in pk_cols]
    pk_physical_set = set(pk_physical)
    first_pk_violation: tuple[int, str] | None = None
    out: list[dict] = []
    # 单次顺序扫描 + 分批 fetch（暂存表静态不变，无需键集翻页）
    result = conn.execute(
        sa.select(payload_stage).order_by(payload_stage.c.__seq)
    ).yield_per(_INGEST_CHUNK)
    for seq, payload in result:
        row = json.loads(payload)
        extra = sorted(set(row.keys()) - set(mapping))
        if extra:
            raise LakeStoreStructureError(
                f"来数第 {seq} 行起出现契约外列 {extra[:20]}。湖表结构发布后不可变，"
                "请核对流水线输出或以全量覆盖重建资产契约。")
        physical_row: dict = {}
        for logical, physical in mapping.items():
            text = row.get(logical, "")
            if physical in pk_physical_set:
                text = text.strip()
            physical_row[physical] = text
        if first_pk_violation is None:
            for logical, physical in zip(pk_cols, pk_physical):
                if not physical_row[physical]:
                    first_pk_violation = (seq, logical)
                    break
        physical_row[seq_col] = seq
        out.append(physical_row)
        if len(out) >= _INGEST_CHUNK:
            conn.execute(stage.insert(), out)
            out.clear()
    if out:
        conn.execute(stage.insert(), out)
    if first_pk_violation is not None:
        seq, logical = first_pk_violation
        raise LakeGateError(
            f"数据集「{dataset_name}」本次输出第 {seq} 行的主键列"
            f"「{logical}」为空。主键值必须非空，否则该行无法获得"
            "稳定身份。请在流水线中过滤或补全该列。")
    return stage, seq_col


def _stage_dedup(conn, stage: sa.Table, seq_col: str,
                 mapping: dict[str, str], pk_cols: list[str], mode: str) -> None:
    """暂存表内去重（DB 集合级，与旧 Python 全量去重同语义）：

    - 有主键 overwrite/upsert：同主键保留 __seq 最大者（末现，对齐
      _dedup_last_by_pk）；
    - append_dedup：全列相同的行保留 __seq 最小者（首现，对齐
      _dedup_first_by_signature）。

    用 GROUP BY 子查询一次物化保留键（O(n)），不用相关子查询（O(n²)）。
    """
    if pk_cols and mode in ("overwrite", "upsert"):
        pk_physical = [mapping[c] for c in pk_cols]
        keep = sa.select(sa.func.max(stage.c[seq_col])).group_by(
            *[stage.c[p] for p in pk_physical])
        conn.execute(stage.delete().where(~stage.c[seq_col].in_(keep)))
    if mode == "append_dedup":
        all_physical = [p for p in mapping.values()]
        keep = sa.select(sa.func.min(stage.c[seq_col])).group_by(
            *[stage.c[p] for p in all_physical])
        conn.execute(stage.delete().where(~stage.c[seq_col].in_(keep)))


def _publish_run(db, dataset, *, rowcount: int, entries: list[dict],
                 added: int, updated: int, deleted: int,
                 producer_run_id: str | None = None,
                 ) -> tuple[DatasetVersion, DatasetChangeset]:
    """版本发布终局，对齐 DatasetService._create_version_locked。

    Dataset 行锁 + populate_existing + 版本号查最大值+1（撞
    (dataset_id, version_no) 唯一约束由外层回滚重试）+ DatasetVersionEvent
    （version_published）同事务 outbox。湖表版本无 blob 载荷，checksum =
    变更集规范哈希。
    """
    checksum = compute_changeset_checksum(
        dataset.id, "run", added, updated, deleted, entries)
    ds = (db.query(Dataset)
          .filter(Dataset.id == dataset.id)
          .with_for_update(of=Dataset)
          .populate_existing()
          .first())
    if ds is None:
        raise LakeStoreError(f"Dataset {dataset.id} not found")
    last_ver = db.query(DatasetVersion).filter(
        DatasetVersion.dataset_id == dataset.id
    ).order_by(DatasetVersion.version_no.desc()).first()
    version_no = (last_ver.version_no + 1) if last_ver else 1
    version = DatasetVersion(
        id=str(uuid.uuid4()),
        dataset_id=dataset.id,
        version_no=version_no,
        rowcount=rowcount,
        data_blob=None,
        data_size=None,
        storage_uri=None,
        checksum=checksum,
        producer_run_id=producer_run_id,
    )
    db.add(version)
    db.flush()
    changeset = DatasetChangeset(
        id=str(uuid.uuid4()),
        dataset_id=dataset.id,
        version_id=version.id,
        change_type="run",
        added_count=added,
        updated_count=updated,
        deleted_count=deleted,
        checksum=checksum,
    )
    db.add(changeset)
    db.flush()
    for batch in _chunked(entries):
        db.execute(DatasetChangesetRow.__table__.insert(), [
            {"changeset_id": changeset.id, **entry} for entry in batch])
    ds.latest_version_id = version.id
    # DatasetVersion 与发布事件同事务提交（outbox 语义与 service.py 一致）。
    db.add(DatasetVersionEvent(
        dataset_id=dataset.id,
        dataset_version_id=version.id,
        event_type="version_published",
    ))
    db.commit()
    db.refresh(version)
    db.refresh(changeset)
    # 尽力失效资产湖总览缓存（失败静默降级，不影响发布主流程）。
    from app.data_channel.datasets import cache

    cache.invalidate_overview()
    return version, changeset


def _normalize_single_row(logical: dict, mapping: dict[str, str],
                          pk_physical: set[str]) -> dict:
    """单个逻辑行 → 物理文本行（与 normalize_lake_rows 同一规范化）。"""
    physical_row: dict[str, str] = {}
    for log, phys in mapping.items():
        text = snapshot_cell_text(logical.get(log))
        if phys in pk_physical:
            text = text.strip()
        physical_row[phys] = text
    return physical_row


def _upsert_run_once(db, dataset, rows, mode: str,
                     pk_cols: list[str],
                     soft_delete_column: str = "",
                     rows_before: int | None = None,
                     staged_input: tuple | None = None,
                     producer_run_id: str | None = None,
                     ) -> tuple[DatasetVersion, DatasetChangeset]:
    conn = _connection(db)
    schema = dict(dataset.schema_json or {})

    contract_pk = split_pk(schema.get("primary_key"))
    if list(pk_cols) != contract_pk:
        raise LakeStoreStructureError(
            f"数据集「{dataset.name}」的契约主键为 {contract_pk}，本次入库携带 "
            f"{list(pk_cols)}。主键仲裁（lake_gate.resolve_pk）是调用方职责，"
            "存储层不接受与契约不一致的主键。")

    # 来数统一灌入 payload 暂存表：rows 为 list[dict] 或批次迭代器；
    # staged_input = (payload_stage, 列并集, 总行数) 时来数已由调用方
    # （pipeline_run 流式管道）完成闸门校验/软删除打标/文本化并灌好，
    # 传入后暂存表归本函数清理。软删除打标对齐 merge_rows 作用于合并后
    # 全量时的就地语义之来数部分；湖中未触及行的重评估在暂存后做。
    if staged_input is not None:
        payload_stage, columns_union, total_rows = staged_input
    else:
        writer = PayloadStageWriter(db)
        payload_stage = writer.stage
        batches = [rows] if isinstance(rows, list) else rows
        for batch in batches:
            if mode == "upsert" and soft_delete_column:
                _apply_soft_delete(batch, soft_delete_column)
            writer.write(batch)
        columns_union = writer.columns_union
        total_rows = writer.total

    stage: sa.Table | None = None
    try:
        table_name = lake_table_name(dataset.id)
        if (not schema.get("lake_columns")
                and not sa.inspect(conn).has_table(table_name)):
            # 有 blob 历史而未引导物理表时，任何模式的入湖都会丢审计基座（增量
            # 丢存量、overwrite 把删除/更新误记为新增）——拒绝执行并指引引导入口
            last_ver = (db.query(DatasetVersion)
                        .filter(DatasetVersion.dataset_id == dataset.id)
                        .order_by(DatasetVersion.version_no.desc()).first())
            if last_ver is not None and (
                    last_ver.data_size is not None or last_ver.storage_uri):
                raise LakeStoreError(
                    f"数据集「{dataset.name}」存在迁移前的快照版本（v"
                    f"{last_ver.version_no}）但尚未引导物理湖表，直接入湖会把湖中"
                    "存量当空湖丢失。请先经 DatasetService.bootstrap_lake_base "
                    "引导遗留基座后再入湖。")
        if (not total_rows
                and not (schema.get("lake_columns") or schema.get("columns"))
                and not sa.inspect(conn).has_table(table_name)):
            # 空湖上的空运行：无行无契约无物理表，只发空版本与空变更集
            # （_publish_run 会 commit 使连接失效，暂存表须先显式清理）
            payload_stage.drop(bind=conn)
            payload_stage = None
            return _publish_run(db, dataset, rowcount=0, entries=[],
                                added=0, updated=0, deleted=0,
                                producer_run_id=producer_run_id)

        mapping, rebuilt = _reconcile_structure(
            db, conn, dataset, schema, mode, pk_cols, columns_union)

        # 软删除标记列进物理表：来数携带的标记列已随 reconcile 演化；湖中存量
        # truthy 行需要标记列存在才能重评估（无打标需求时不引入标记列，对齐
        # merge._apply_soft_delete 只在有删除行时出现标记键的行为）
        if (mode == "upsert" and soft_delete_column
                and soft_delete_column in mapping
                and _SOFT_MARKER_COLS[0] not in mapping):
            probe = lake_table_definition(table_name, mapping, pk_cols)
            flag_p = mapping[soft_delete_column]
            has_truthy = conn.execute(
                sa.select(probe.c[flag_p]).where(
                    sa.func.lower(sa.func.trim(probe.c[flag_p])).in_(
                        sorted(_SOFT_DELETE_TRUTHY))).limit(1)).first()
            if has_truthy:
                mapping = evolve_lake_table_columns(
                    db, dataset, list(_SOFT_MARKER_COLS))

        table = lake_table_definition(table_name, mapping, pk_cols)
        pk_physical = [mapping[c] for c in pk_cols]
        # payload 逐批规范化为物理暂存表（契约外列/主键非空校验与旧全量
        # 路径同文案同行号），再做暂存表内 SQL 去重（末现/首现语义不变）
        stage, seq_col = _materialize_physical_stage(
            conn, payload_stage, table, mapping, pk_cols, dataset.name)
        _stage_dedup(conn, stage, seq_col, mapping, pk_cols, mode)
        # 合并基座行数：overwrite 整表重建后为 0（旧表已 DROP）；调用方在写锁内
        # 已计数的直接复用；否则此处一次性计数。结尾 rowcount 由各分支的精确
        # 物理增删推导，不再另做一次全表 count(*)。
        if rebuilt:
            rows_before = 0
        elif rows_before is None and (pk_physical or mode != "overwrite"):
            rows_before = conn.execute(
                sa.select(sa.func.count()).select_from(table)).scalar_one()
        added_keys: list = []
        updated_keys: list = []
        deleted_keys: list = []
        old_rows_by_key: dict = {}
        new_rows_by_key: dict = {}
        new_rows_by_sig: dict = {}
        insert_rows: list[dict] = []

        if pk_physical:
            t = table.alias("t")
            s = stage.alias("s")
            join_cond = sa.and_(*[t.c[p] == s.c[p] for p in pk_physical])
            stage_pk = [s.c[p] for p in pk_physical]

            if mode in ("append", "append_dedup"):
                # append_dedup 先把与湖中全同的来数行从暂存中摘除（对齐
                # merge._dedup_by_row(old+new) 保留首次出现的整行去重），
                # 再做撞键预检：批内重复或撞湖中既有主键 = 运行失败（口径
                # 对齐 validate_merged_lake；预检在任何应用之前）
                if mode == "append_dedup":
                    all_physical = [p for p in mapping.values()]
                    identical_keys = [tuple(record) for record in conn.execute(
                        sa.select(*stage_pk).select_from(s.join(
                            t, sa.and_(*[t.c[p] == s.c[p]
                                         for p in all_physical])))).all()]
                    for batch in _chunked(identical_keys):
                        conn.execute(stage.delete().where(
                            _key_match(stage, pk_physical, batch)))
                dup_batch = [tuple(record) for record in conn.execute(
                    sa.select(*stage_pk).group_by(*stage_pk)
                    .having(sa.func.count() > 1).limit(5)).all()]
                dup_lake = [tuple(record) for record in conn.execute(
                    sa.select(*stage_pk).select_from(s.join(t, join_cond))
                    .limit(5)).all()]
                if dup_batch or dup_lake:
                    examples = [dict(zip(pk_cols, key))
                                for key in (dup_batch + dup_lake)[:5]]
                    raise LakeGateError(
                        f"数据集「{dataset.name}」合并后的全量数据存在主键重复"
                        f"（{examples}）。同一主键值代表同一业务对象，追加模式"
                        "不会合并同主键行；当前使用追加模式，有主键的资产应改用 "
                        "upsert，或确保各批次主键互不重复。为保护既有版本，"
                        "本次数据不会入湖。")

            added_keys = [tuple(record) for record in conn.execute(
                sa.select(*stage_pk)
                .select_from(s.outerjoin(t, join_cond))
                .where(t.c[pk_physical[0]].is_(None))).all()]

            non_pk_physical = [p for logical, p in mapping.items()
                               if logical not in set(pk_cols)]
            if non_pk_physical and mode in ("overwrite", "upsert"):
                diff_cond = sa.or_(*[s.c[p] != t.c[p] for p in non_pk_physical])
                updated_keys = [tuple(record) for record in conn.execute(
                    sa.select(*stage_pk)
                    .select_from(s.join(t, join_cond))
                    .where(diff_cond)).all()]

            # updated 的旧行内容（变更集 old_row），只按键物化变化行
            for batch in _chunked(updated_keys):
                for record in conn.execute(
                        sa.select(t).where(_key_match(t, pk_physical, batch))
                ).mappings():
                    record = dict(record)
                    old_rows_by_key[tuple(record[p] for p in pk_physical)] = record
            # overwrite 的湖中删除行：分批 fetch（deleted 行本身即变更集
            # old_row 所需，峰值 ∝ 删除行数，不再一次性 .all() 出中间列表）
            if mode == "overwrite":
                for record in conn.execute(
                        sa.select(t)
                        .select_from(t.outerjoin(s, join_cond))
                        .where(s.c[pk_physical[0]].is_(None))
                ).mappings().yield_per(_BATCH_SIZE):
                    record = dict(record)
                    key = tuple(record[p] for p in pk_physical)
                    deleted_keys.append(key)
                    old_rows_by_key[key] = record

            new_rows_by_key = {}  # 软删除重评估直接入账；来数行在下文按键回捞

            # 软删除湖中重评估：未被本次来数触及的行按同一词表打标/摘除标记。
            # 已正确打标的 truthy 行保持原标记与时间戳（不每次运行刷新 ts 制造
            # 全量 updated 噪音）——与现行链的可观测统计一致（merge_rows 的
            # 就地打标因 before/after 共享对象不会把存量重评估记为变更）
            if (mode == "upsert" and soft_delete_column
                    and soft_delete_column in mapping
                    and all(c in mapping for c in _SOFT_MARKER_COLS)):
                flag_p = mapping[soft_delete_column]
                marker_p = mapping[_SOFT_MARKER_COLS[0]]
                pk_physical_set = set(pk_physical)
                candidates = [dict(record) for record in conn.execute(
                    sa.select(t)
                    .select_from(t.outerjoin(s, join_cond))
                    .where(s.c[pk_physical[0]].is_(None))
                    .where(sa.or_(
                        sa.func.lower(sa.func.trim(t.c[flag_p])).in_(
                            sorted(_SOFT_DELETE_TRUTHY)),
                        t.c[marker_p] == "True"))).mappings().all()]
                for old_phys in candidates:
                    logical = _logical_row(old_phys, mapping)
                    flag_value = logical.get(soft_delete_column)
                    is_deleted = (str(flag_value).strip().lower()
                                  in _SOFT_DELETE_TRUTHY) if flag_value is not None else False
                    already_marked = logical.get(_SOFT_MARKER_COLS[0]) == "True"
                    if is_deleted and already_marked:
                        continue  # 打标状态已正确：保持原样（含首次打标时间戳）
                    _apply_soft_delete([logical], soft_delete_column)
                    new_phys = _normalize_single_row(
                        logical, mapping, pk_physical_set)
                    if new_phys != old_phys:
                        key = tuple(old_phys[p] for p in pk_physical)
                        updated_keys.append(key)
                        old_rows_by_key[key] = old_phys
                        new_rows_by_key[key] = new_phys

            # 来数行内容（变更集 new_row 与回写行）：按 added+updated 键从
            # 暂存回捞（软删除重评估的构造行不在暂存，已在上文直接入账），
            # 不再持有全量来数字典
            wanted = [key for key in added_keys + updated_keys
                      if key not in new_rows_by_key]
            for batch in _chunked(wanted):
                for record in conn.execute(
                        sa.select(stage).where(
                            _key_match(stage, pk_physical, batch))).mappings():
                    record = dict(record)
                    record.pop(seq_col, None)
                    new_rows_by_key[
                        tuple(record[p] for p in pk_physical)] = record

            # 应用：先 DELETE（updated + deleted 涉及的主键），再 INSERT
            doomed = updated_keys + deleted_keys
            for batch in _chunked(doomed):
                conn.execute(
                    table.delete().where(_key_match(table, pk_physical, batch)))
            insert_rows = [new_rows_by_key[key] for key in added_keys + updated_keys]
        else:
            # 无主键：无行级身份。overwrite 全量替换；append/upsert 直接追加
            # （对齐 merge.py 无主键时的退化语义）；append_dedup 再跳过湖中全同行。
            # 与湖的比较全部下沉 DB 集合运算（湖表全 TEXT 列无 NULL，逐列等值
            # ⟺ 整行签名等值），变更集只物化实际变化行，不把整湖读进 Python。
            all_physical = [p for p in mapping.values()]
            t = table.alias("t")
            s = stage.alias("s")
            row_match = sa.and_(*[t.c[p] == s.c[p] for p in all_physical])
            if mode == "overwrite":
                # 变更集 = 集合级求差（EXCEPT 自带去重，与整行签名集合 diff
                # 的重复折叠口径一致，对齐 compute_lake_impact 无主键口径）；
                # 物理重写全程 DB 端（来数重复行经 stage 原样保留）
                stage_cols = [s.c[p] for p in all_physical]
                lake_cols = [t.c[p] for p in all_physical]
                added_physical = [dict(zip(all_physical, record))
                                  for record in conn.execute(
                                      sa.select(*stage_cols).except_(
                                          sa.select(*lake_cols)))]
                deleted_physical = [dict(zip(all_physical, record))
                                    for record in conn.execute(
                                        sa.select(*lake_cols).except_(
                                            sa.select(*stage_cols)))]
                conn.execute(table.delete())
                conn.execute(table.insert().from_select(
                    all_physical,
                    sa.select(*[stage.c[p] for p in all_physical])))
                new_rows_by_sig = {_physical_signature(r): r
                                   for r in added_physical}
                old_rows_by_key = {_physical_signature(r): r
                                   for r in deleted_physical}
                added_keys = list(new_rows_by_sig)
                deleted_keys = list(old_rows_by_key)
            else:
                # 无主键 + 软删除：湖中存量按词表定向重评估（打标/摘标两条
                # UPDATE，只触及状态迁移的行，不再整表物化+重写），再追加来数
                if (mode == "upsert" and soft_delete_column
                        and soft_delete_column in mapping
                        and all(c in mapping for c in _SOFT_MARKER_COLS)):
                    flag_p = mapping[soft_delete_column]
                    marker_p = mapping[_SOFT_MARKER_COLS[0]]
                    ts_p = mapping[_SOFT_MARKER_COLS[1]]
                    flag_expr = sa.func.lower(sa.func.trim(table.c[flag_p]))
                    # 已正确打标的 truthy 行不在命中集（保持原标记与首次打标
                    # 时间戳，不每次运行刷新 ts 制造变更噪音）
                    to_mark = sa.and_(flag_expr.in_(sorted(_SOFT_DELETE_TRUTHY)),
                                      table.c[marker_p] != "True")
                    to_unmark = sa.and_(~flag_expr.in_(sorted(_SOFT_DELETE_TRUTHY)),
                                        table.c[marker_p] != "")
                    # 变更集旧值：只物化状态迁移行（逐实例，重复行逐份记录）
                    changed_old = [dict(record) for record in conn.execute(
                        sa.select(table).where(
                            sa.or_(to_mark, to_unmark))).mappings()]
                    run_ts = datetime.utcnow().isoformat()
                    conn.execute(table.update().where(to_mark).values(
                        {marker_p: "True", ts_p: run_ts}))
                    conn.execute(table.update().where(to_unmark).values(
                        {marker_p: "", ts_p: ""}))
                    for old_phys in changed_old:
                        logical = _logical_row(old_phys, mapping)
                        flag_value = logical.get(soft_delete_column)
                        is_deleted = (str(flag_value).strip().lower()
                                      in _SOFT_DELETE_TRUTHY) if flag_value is not None else False
                        if is_deleted:
                            new_logical = {**logical,
                                           _SOFT_MARKER_COLS[0]: "True",
                                           _SOFT_MARKER_COLS[1]: run_ts}
                        else:
                            new_logical = {**logical,
                                           _SOFT_MARKER_COLS[0]: "",
                                           _SOFT_MARKER_COLS[1]: ""}
                        new_phys = _normalize_single_row(logical, mapping, set())
                        old_sig = _physical_signature(old_phys)
                        new_sig = _physical_signature(new_phys)
                        deleted_keys.append(old_sig)
                        old_rows_by_key[old_sig] = old_phys
                        added_keys.append(new_sig)
                        new_rows_by_sig[new_sig] = new_phys
                if mode == "append_dedup":
                    # 与湖中全同的行经反连接在 DB 端过滤（来数已批内首现去重，
                    # 过滤结果逐行即逐签名）
                    insert_rows = [dict(zip(all_physical, record))
                                   for record in conn.execute(
                                       sa.select(*[s.c[p] for p in all_physical])
                                       .select_from(s.outerjoin(t, row_match))
                                       .where(t.c[all_physical[0]].is_(None)))]
                else:
                    # 无主键 append/upsert：暂存内容即追加内容（无去重），
                    # 按序号回扫（与来数顺序一致）
                    insert_rows = [dict((k, v) for k, v in record.items()
                                        if k != seq_col)
                                   for record in conn.execute(
                                       sa.select(stage)
                                       .order_by(stage.c[seq_col])).mappings()]
                for row in insert_rows:
                    sig = _physical_signature(row)
                    added_keys.append(sig)
                    new_rows_by_sig[sig] = row

        for batch in _chunked(insert_rows):
            conn.execute(table.insert(), batch)

        # 变更集逐行明细（逻辑列名 + 契约 row_pk）
        entries: list[dict] = []
        for key in added_keys:
            physical = (new_rows_by_key if pk_physical else new_rows_by_sig)[key]
            logical = _logical_row(physical, mapping)
            entries.append({"row_pk": lake_row_pk(logical, pk_cols),
                            "change_type": "added", "old_row": None,
                            "new_row": logical})
        for key in updated_keys:
            logical = _logical_row(new_rows_by_key[key], mapping)
            entries.append({"row_pk": lake_row_pk(logical, pk_cols),
                            "change_type": "updated",
                            "old_row": _logical_row(old_rows_by_key[key], mapping),
                            "new_row": logical})
        for key in deleted_keys:
            entries.append({"row_pk": lake_row_pk(
                _logical_row(old_rows_by_key[key], mapping), pk_cols),
                "change_type": "deleted",
                "old_row": _logical_row(old_rows_by_key[key], mapping),
                "new_row": None})
    finally:
        if stage is not None:
            stage.drop(bind=conn)
        if payload_stage is not None:
            payload_stage.drop(bind=conn)

    # rowcount 由各分支的精确物理增删推导，不再全表 count(*)：
    # 有主键 = 基座 − 删除键 + 新增键（主键级精确）；无主键 overwrite =
    # 来数行数（全量替换）；无主键追加系 = 基座 + 实际追加行数
    if pk_physical:
        rowcount = rows_before - len(deleted_keys) + len(added_keys)
    elif mode == "overwrite":
        rowcount = total_rows
    else:
        rowcount = rows_before + len(insert_rows)
    return _publish_run(db, dataset, rowcount=rowcount, entries=entries,
                        added=len(added_keys), updated=len(updated_keys),
                        deleted=len(deleted_keys),
                        producer_run_id=producer_run_id)


def upsert_run(db, dataset, rows, write_mode: str,
               pk_cols: list[str], *, soft_delete_column: str = "",
               rows_before: int | None = None,
               staged_input: tuple | None = None,
               producer_run_id: str | None = None,
               ) -> tuple[DatasetVersion, DatasetChangeset]:
    """按入库方式把来数合并进物理湖表，发布新版本并记录行级变更集。

    返回 (DatasetVersion, DatasetChangeset)。调用方必须已持有
    dataset_write_lock（dataset::{dataset.id}）；本函数自行 commit，
    版本号撞唯一约束时整体回滚重试（与 _create_version_locked 同策略）。
    soft_delete_column 仅对 upsert 生效：truthy 行打 __deleted__ 标记而非
    物理删除（词表与 write_modes._apply_soft_delete 一致）。
    rows：list[dict] 或批次迭代器（Iterable[list[dict]]）——统一经 payload
    暂存表分批灌入，Python 峰值内存与来数总量脱钩。
    rows_before：调用方在同一写锁/事务内已计数的基座行数（仅追加系与有主键
    分支使用；overwrite 重建或 None 时按需要在合并前一次性计数）。
    staged_input：(payload_stage, 列并集, 总行数)——pipeline_run 流式管道
    在闸门侧完成校验/软删除打标/文本化后的预灌形态；传入时 rows 忽略，
    暂存表归本函数清理。
    """
    if rows is None and staged_input is None:
        raise LakeStoreError("upsert_run 需要 rows 或 staged_input 之一。")
    if not uses_lake_table(dataset):
        raise LakeStoreError(
            f"数据集 {getattr(dataset, 'id', None)} 不是成品数据集（kind='curated'），"
            "不使用物理湖表。")
    mode = normalize_write_mode(write_mode)
    normalized_pk = [str(c).strip() for c in (pk_cols or []) if str(c).strip()]
    for attempt in range(3):
        try:
            return _upsert_run_once(db, dataset, rows, mode, normalized_pk,
                                    soft_delete_column, rows_before,
                                    staged_input, producer_run_id)
        except IntegrityError:
            db.rollback()
            # 回滚会销毁 TEMP 暂存表：非 list 形态（迭代器/预灌暂存）无法
            # 安全重灌，直接抛出而不是重试出错误数据
            if (attempt == 2 or staged_input is not None
                    or not isinstance(rows, list)):
                raise
        except Exception:
            # 与 _create_version_locked 的失败语义一致：入湖失败不残留任何
            # 未提交状态（建表/契约/行变更整体回滚），由调用方记运行失败
            db.rollback()
            raise
    raise LakeStoreError("unreachable")  # pragma: no cover


# ── 读取 ────────────────────────────────────────────────────
def _readable_table(db, dataset) -> tuple[sa.Table, dict[str, str], list[str]] | None:
    """契约对应的物理表 Core 定义；映射缺失或表不存在（未入湖）返回 None。"""
    _, mapping, pk_cols = _contract(dataset)
    if mapping is None:
        return None
    conn = _connection(db)
    table_name = lake_table_name(dataset.id)
    if not sa.inspect(conn).has_table(table_name):
        return None
    return lake_table_definition(table_name, mapping, pk_cols), mapping, pk_cols


def count_rows(db, dataset) -> int:
    """物理表当前行数；表不存在（未入湖/迁移跳过）按空湖计 0。"""
    found = _readable_table(db, dataset)
    if found is None:
        return 0
    table, _, _ = found
    return _connection(db).execute(
        sa.select(sa.func.count()).select_from(table)).scalar_one()


def page_rows(db, dataset, offset: int = 0, limit: int = 100,
              order_by_pk: bool = True) -> list[dict]:
    """主键升序真分页（逻辑列名行）。无主键资产不保证稳定行序。"""
    found = _readable_table(db, dataset)
    if found is None:
        return []
    table, mapping, pk_cols = found
    stmt = sa.select(table)
    if order_by_pk and pk_cols:
        stmt = stmt.order_by(*[table.c[mapping[c]] for c in pk_cols])
    stmt = stmt.offset(max(0, int(offset))).limit(max(0, int(limit)))
    return [_logical_row(dict(record), mapping) for record in
            _connection(db).execute(stmt).mappings()]


def stream_rows(db, dataset, batch_size: int = 5000):
    """分批流式读全表（生成器，每批 list[dict]，逻辑列名行）。"""
    batch_size = max(1, int(batch_size))
    offset = 0
    while True:
        batch = page_rows(db, dataset, offset, batch_size)
        if not batch:
            break
        yield batch
        if len(batch) < batch_size:
            break
        offset += batch_size


def rows_by_pks(db, dataset, pks: list[str]) -> dict[str, dict]:
    """按审核 row_pk 批量取行，返回 {row_pk: 行（逻辑列名）}；未命中的键缺席。"""
    found = _readable_table(db, dataset)
    if found is None:
        return {}
    table, mapping, pk_cols = found
    keys = [decode_lake_row_pk(row_pk, pk_cols) for row_pk in pks]
    if not keys:
        return {}
    pk_physical = [mapping[c] for c in pk_cols]
    out: dict[str, dict] = {}
    for batch in _chunked(keys):
        for record in _connection(db).execute(
                sa.select(table).where(_key_match(table, pk_physical, batch))
        ).mappings():
            logical = _logical_row(dict(record), mapping)
            out[lake_row_pk(logical, pk_cols)] = logical
    return out


def rows_at_version(db, dataset, version_no: int) -> list[dict]:
    """物理表当前状态 + 变更集逆向回放到目标版本的全量行（逻辑列名）。

    只物化变化行：从新到旧逐个撤销 run 变更集（added→摘除，updated→换回
    old_row，deleted→补回 old_row），未触及的行直接透传。回放结果不保证
    原始行序（当前物理序 + 恢复行追加）。目标版本是迁移前的整份快照
    （data_blob/storage_uri 非空）时抛 LakeStoreLegacyVersionError，由调用方
    走 DatasetService 遗留解析路径。
    """
    found = _readable_table(db, dataset)
    version = db.query(DatasetVersion).filter(
        DatasetVersion.dataset_id == dataset.id,
        DatasetVersion.version_no == version_no).first()
    if version is None:
        raise LakeStoreError(f"数据集 {dataset.id} 不存在版本 v{version_no}")
    if version.data_blob is not None or version.storage_uri:
        raise LakeStoreLegacyVersionError(
            f"数据集 {dataset.id} v{version_no} 是迁移前的整份快照版本，"
            "请走 DatasetService 遗留读取路径。")
    later = (db.query(DatasetChangeset)
             .join(DatasetVersion, DatasetChangeset.version_id == DatasetVersion.id)
             .filter(DatasetChangeset.dataset_id == dataset.id,
                     DatasetChangeset.change_type == "run",
                     DatasetVersion.version_no > version_no)
             .order_by(DatasetVersion.version_no.desc())
             .all())
    if found is None:
        # 空湖空运行发布的空版本没有物理表：没有更晚的 run 变更集时读作空湖；
        # 有变更集却无表属于数据损坏，硬失败
        if later:
            raise LakeStoreError(
                f"数据集 {dataset.id} 存在变更集但物理湖表缺失，数据不一致，"
                f"无法回放到 v{version_no}。")
        return []
    _, mapping, pk_cols = found

    rows_by_changeset: dict[str, list] = {}
    if later:
        for row in db.query(DatasetChangesetRow).filter(
                DatasetChangesetRow.changeset_id.in_([cs.id for cs in later])):
            rows_by_changeset.setdefault(row.changeset_id, []).append(row)

    # 逆向补丁：row_pk → 旧行（None 表示该行在目标版本不存在）。新版本变更集
    # 先撤销，旧版本变更集对同一行的更旧状态直接覆盖。
    patch: dict[str, dict | None] = {}
    for changeset in later:
        for row in rows_by_changeset.get(changeset.id, []):
            patch[row.row_pk] = (None if row.change_type == "added"
                                 else row.old_row)

    out: list[dict] = []
    remaining = dict(patch)
    for batch in stream_rows(db, dataset, batch_size=5000):
        for logical in batch:
            row_pk = lake_row_pk(logical, pk_cols)
            if row_pk in remaining:
                restored = remaining.pop(row_pk)
                if restored is not None:
                    out.append(restored)
            else:
                out.append(logical)
    out.extend(restored for restored in remaining.values() if restored is not None)
    return out


def _version_reverse_patch(db, dataset, version_no: int,
                           later: list) -> dict[str, dict | None]:
    """目标版本之后全部 run 变更集的逆向补丁（row_pk → 旧行 | None）。

    与 rows_at_version 同一语义：变更集从新到旧覆盖，最旧者（最接近目标
    版本的状态）胜出；None 表示该行在目标版本不存在。
    """
    patch: dict[str, dict | None] = {}
    if not later:
        return patch
    rows_by_changeset: dict[str, list] = {}
    for row in db.query(DatasetChangesetRow).filter(
            DatasetChangesetRow.changeset_id.in_([cs.id for cs in later])):
        rows_by_changeset.setdefault(row.changeset_id, []).append(row)
    for changeset in later:
        for row in rows_by_changeset.get(changeset.id, []):
            patch[row.row_pk] = (None if row.change_type == "added"
                                 else row.old_row)
    return patch


def page_rows_at_version(db, dataset, version_no: int, offset: int = 0,
                         limit: int = 100) -> tuple[list[dict], int]:
    """按页读取目标湖表版本：物理表 PK 序扫描 + 逆向补丁，只物化窗口。

    审核三视图的分页回读入口。补丁语义与 rows_at_version 完全一致
    （added→摘除，updated/deleted→原位换回 old_row），但不整版物化：
    - 只把「目标版本之后」变更集涉及的变化行载入内存（通常远小于全量）；
    - 物理表现存行按主键序流式扫描，命中补丁的原位替换/跳过，攒满当前
      窗口即停止；
    - 目标版本存在、物理表已无的恢复行按 row_pk 排序追加在尾部——追加
      位置与全量回放一致，排序使跨请求分页顺序稳定。
    返回 (窗口行, 目标版本总行数)。总行数按「物理表计数 − 后续新增现存行
    + 后续消失恢复行」精确计算，不依赖发布时的 rowcount 快照。
    """
    found = _readable_table(db, dataset)
    version = db.query(DatasetVersion).filter(
        DatasetVersion.dataset_id == dataset.id,
        DatasetVersion.version_no == version_no).first()
    if version is None:
        raise LakeStoreError(f"数据集 {dataset.id} 不存在版本 v{version_no}")
    if version.data_blob is not None or version.storage_uri:
        raise LakeStoreLegacyVersionError(
            f"数据集 {dataset.id} v{version_no} 是迁移前的整份快照版本，"
            "请走 DatasetService 遗留读取路径。")
    later = (db.query(DatasetChangeset)
             .join(DatasetVersion, DatasetChangeset.version_id == DatasetVersion.id)
             .filter(DatasetChangeset.dataset_id == dataset.id,
                     DatasetChangeset.change_type == "run",
                     DatasetVersion.version_no > version_no)
             .order_by(DatasetVersion.version_no.desc())
             .all())
    if found is None:
        # 与 rows_at_version 一致：空湖无更晚变更集读作空；有变更集无表硬失败
        if later:
            raise LakeStoreError(
                f"数据集 {dataset.id} 存在变更集但物理湖表缺失，数据不一致，"
                f"无法回放到 v{version_no}。")
        return [], 0
    _, _, pk_cols = found

    patch = _version_reverse_patch(db, dataset, version_no, later)
    added_keys = {key for key, old in patch.items() if old is None}
    restored_map = {key: old for key, old in patch.items() if old is not None}

    # 存在性校验只针对被补丁触及的行，绝不扫全表。
    present_pks: set[str] = set()
    if patch:
        present_pks = set(rows_by_pks(db, dataset, list(patch.keys())).keys())
    restored_only_map = {
        key: old for key, old in restored_map.items() if key not in present_pks}
    total = (count_rows(db, dataset)
             - len(added_keys & present_pks)
             + len(restored_only_map))

    def _target_rows():
        for batch in stream_rows(db, dataset, batch_size=5000):
            for logical in batch:
                key = lake_row_pk(logical, pk_cols)
                if key in added_keys:
                    continue
                yield restored_map.get(key, logical)
        for key in sorted(restored_only_map):
            yield restored_only_map[key]

    out: list[dict] = []
    skipped = 0
    wanted_offset = max(0, int(offset))
    wanted_limit = max(0, int(limit))
    for row in _target_rows():
        if skipped < wanted_offset:
            skipped += 1
            continue
        out.append(row)
        if len(out) >= wanted_limit:
            break
    return out, total
