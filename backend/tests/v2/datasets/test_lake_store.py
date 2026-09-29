"""lake_store 物理湖表 + 变更集的单元测试（SQLite 方言，覆盖建表契约、
三种 write_mode 对拍 merge.py 参考实现、变更集逐行正确性与版本逆向回放）。"""
from __future__ import annotations

import json
import uuid

import pytest
import sqlalchemy as sa

from app.data_channel.datasets.lake_gate import LakeGateError
from app.data_channel.datasets.lake_store import (
    LakeStoreError,
    LakeStoreLegacyVersionError,
    LakeStoreStructureError,
    build_lake_column_mapping,
    count_rows,
    drop_lake_table,
    ensure_lake_table,
    lake_table_name,
    page_rows,
    rows_at_version,
    rows_by_pks,
    sanitize_lake_column,
    stream_rows,
    upsert_run,
    uses_lake_table,
)
from app.data_channel.datasets.models import (
    Dataset,
    DatasetChangeset,
    DatasetChangesetRow,
    DatasetVersion,
    DatasetVersionEvent,
)
from app.data_channel.datasets.service import snapshot_cell_text
from app.data_channel.pipeline_tasks.merge import merge_rows


@pytest.fixture(autouse=True)
def _drop_lake_tables(db):
    """物理湖表不在 Base.metadata 中（运行时 DDL），逐测试清理避免串库。"""
    yield
    conn = db.connection()
    for name in sa.inspect(conn).get_table_names():
        if name.startswith("lake_ds_"):
            conn.execute(sa.text(f'DROP TABLE IF EXISTS "{name}"'))
    db.commit()


def _make_dataset(db, *, pk="id", columns=("id", "name", "amt"),
                  kind="curated") -> Dataset:
    schema: dict = {"columns": list(columns)}
    if pk:
        schema["primary_key"] = pk
    ds = Dataset(
        id=str(uuid.uuid4()),
        name=f"湖表测试-{uuid.uuid4().hex[:8]}",
        kind=kind,
        schema_json=schema,
    )
    db.add(ds)
    db.commit()
    return ds


def _lake_map(db, ds, pk_cols) -> dict:
    """当前湖内容：{主键元组: 行}。"""
    out = {}
    for batch in stream_rows(db, ds, batch_size=7):
        for row in batch:
            out[tuple(row[c] for c in pk_cols)] = row
    return out


def _canonical_map(rows: list[dict], columns, pk_cols) -> dict:
    """merge.py 参考结果 → 规范化文本 {主键元组: 行}（主键值 strip，与入库一致）。"""
    out = {}
    for row in rows:
        canonical = {c: snapshot_cell_text(row.get(c)) for c in columns}
        key = tuple(canonical[c].strip() for c in pk_cols)
        out[key] = canonical
    return out


def _changeset_rows(db, changeset_id) -> list[DatasetChangesetRow]:
    return db.query(DatasetChangesetRow).filter(
        DatasetChangesetRow.changeset_id == changeset_id).all()


BASE_ROWS = [
    {"id": "1", "name": "甲", "amt": 10},
    {"id": "2", "name": "乙", "amt": 20},
    {"id": "3", "name": "丙", "amt": "30"},
]


# ── 建表契约 ────────────────────────────────────────────────
def test_uses_lake_table_only_for_curated(db):
    assert uses_lake_table(_make_dataset(db)) is True
    assert uses_lake_table(_make_dataset(db, kind="structured")) is False


def test_ensure_creates_table_with_contract(db):
    ds = _make_dataset(db)
    mapping = ensure_lake_table(db, ds)
    db.commit()

    assert mapping == {"id": "id", "name": "name", "amt": "amt"}
    table_name = lake_table_name(ds.id)
    assert table_name == f"lake_ds_{ds.id.replace('-', '')}"
    inspector = sa.inspect(db.connection())
    assert [c["name"] for c in inspector.get_columns(table_name)] == [
        "id", "name", "amt"]
    pk = inspector.get_pk_constraint(table_name)
    assert pk["constrained_columns"] == ["id"]
    assert pk["name"] == f"pk_{table_name}"
    # 映射持久化到数据集契约
    assert ds.schema_json["lake_columns"] == mapping


def test_ensure_sanitizes_column_names(db):
    long_name = "很长的列名" * 20  # 远超 48 字节预算
    truncated = long_name.encode("utf-8")[:48].decode("utf-8", "ignore")
    ds = _make_dataset(
        db, columns=("id", "", "  ", long_name, long_name + "尾"))
    mapping = ensure_lake_table(db, ds)
    db.commit()

    physical = list(mapping.values())
    assert len(set(physical)) == len(physical)  # 去重
    assert all(name and "\x00" not in name for name in physical)
    assert physical[1] == "col" and physical[2] == "col_2"
    assert physical[3] == truncated
    assert physical[4] != physical[3]  # 截断撞名加后缀
    inspector = sa.inspect(db.connection())
    assert [c["name"] for c in inspector.get_columns(lake_table_name(ds.id))] == physical


def test_ensure_idempotent_and_rejects_structure_drift(db):
    ds = _make_dataset(db)
    first = ensure_lake_table(db, ds)
    db.commit()
    assert ensure_lake_table(db, ds) == first

    # 列漂移：契约删一列 → 拒绝
    ds.schema_json = {**ds.schema_json, "lake_columns": {"id": "id", "name": "name"}}
    with pytest.raises(LakeStoreStructureError):
        ensure_lake_table(db, ds)
    db.rollback()

    # 主键漂移：契约换一个主键 → 拒绝
    ds = db.query(Dataset).filter(Dataset.id == ds.id).first()
    ds.schema_json = {**ds.schema_json, "primary_key": "name"}
    with pytest.raises(LakeStoreStructureError):
        ensure_lake_table(db, ds)
    db.rollback()


def test_drop_lake_table_idempotent(db):
    ds = _make_dataset(db)
    ensure_lake_table(db, ds)
    db.commit()
    drop_lake_table(db, ds.id)
    drop_lake_table(db, ds.id)  # 幂等
    db.commit()
    assert not sa.inspect(db.connection()).has_table(lake_table_name(ds.id))


# ── upsert 三态对拍 merge.py 参考实现 ───────────────────────
def test_upsert_run_records_producer_run_lineage(db):
    """湖版本记录产出它的流水线运行（producer_run_id 血缘）。"""
    from app.data_channel.pipelines.models import PipelineRun

    run = PipelineRun(pipeline_id="pl-1", status="success")
    db.add(run)
    db.flush()

    ds = _make_dataset(db)
    ver, _ = upsert_run(db, ds, BASE_ROWS, "overwrite", ["id"],
                         producer_run_id=run.id)
    assert ver.producer_run_id == run.id

    # 无运行的写入路径（人工/导入）不伪造血缘
    ver2, _ = upsert_run(db, ds, BASE_ROWS, "overwrite", ["id"])
    assert ver2.producer_run_id is None


def test_upsert_run_overwrite_matches_reference(db):
    ds = _make_dataset(db)
    upsert_run(db, ds, BASE_ROWS, "overwrite", ["id"])
    incoming = [
        {"id": "2", "name": "乙改", "amt": 21},
        {"id": "4", "name": "丁", "amt": 40},
    ]
    upsert_run(db, ds, incoming, "overwrite", ["id"])

    merged, _ = merge_rows(BASE_ROWS, incoming, {"mode": "overwrite"})
    # overwrite 参考语义：资产 = 本次输出
    assert _lake_map(db, ds, ["id"]) == _canonical_map(
        merged, ["id", "name", "amt"], ["id"])
    assert count_rows(db, ds) == 2


def test_upsert_run_upsert_matches_reference_with_last_wins_dedup(db):
    ds = _make_dataset(db)
    upsert_run(db, ds, BASE_ROWS, "overwrite", ["id"])
    incoming = [
        {"id": "2", "name": "乙改", "amt": 21},
        {"id": "4", "name": "丁", "amt": 40},
        {"id": "2", "name": "乙末现", "amt": 22},  # 同主键末现去重：后者为准
    ]
    upsert_run(db, ds, incoming, "upsert", ["id"])

    merged, _ = merge_rows(BASE_ROWS, incoming,
                           {"mode": "upsert", "primary_key": "id"})
    assert _lake_map(db, ds, ["id"]) == _canonical_map(
        merged, ["id", "name", "amt"], ["id"])
    assert _lake_map(db, ds, ["id"])[("2",)]["name"] == "乙末现"


def test_upsert_run_upsert_composite_pk_matches_reference(db):
    columns = ["id", "sku", "amt"]
    base = [
        {"id": "1", "sku": "a", "amt": 10},
        {"id": "1", "sku": "b", "amt": 11},
        {"id": "2", "sku": "a", "amt": 20},
    ]
    ds = _make_dataset(db, pk="id,sku", columns=columns)
    upsert_run(db, ds, base, "overwrite", ["id", "sku"])
    incoming = [
        {"id": "1", "sku": "b", "amt": 12},
        {"id": "3", "sku": "a", "amt": 30},
    ]
    upsert_run(db, ds, incoming, "upsert", ["id", "sku"])

    merged, _ = merge_rows(base, incoming,
                           {"mode": "upsert", "primary_key": "id,sku"})
    assert _lake_map(db, ds, ["id", "sku"]) == _canonical_map(
        merged, columns, ["id", "sku"])


def test_upsert_run_append_existing_pk_fails_run(db):
    """追加不合并：撞湖中既有主键抛 LakeGateError（口径对齐 validate_merged_lake），
    湖内容与版本史保持不变。"""
    ds = _make_dataset(db)
    upsert_run(db, ds, BASE_ROWS, "overwrite", ["id"])
    with pytest.raises(LakeGateError, match="合并后的全量数据") as exc_info:
        upsert_run(db, ds, [
            {"id": "2", "name": "乙改", "amt": 99},
            {"id": "4", "name": "丁", "amt": 40},
        ], "append", ["id"])
    assert "upsert" in str(exc_info.value)
    assert count_rows(db, ds) == 3
    assert _lake_map(db, ds, ["id"])[("2",)]["name"] == "乙"
    # 未发布新版本
    assert db.query(DatasetVersion).filter(
        DatasetVersion.dataset_id == ds.id).count() == 1


def test_upsert_run_append_duplicate_pk_within_batch_fails(db):
    ds = _make_dataset(db)
    upsert_run(db, ds, BASE_ROWS, "overwrite", ["id"])
    with pytest.raises(LakeGateError, match="合并后的全量数据"):
        upsert_run(db, ds, [
            {"id": "9", "name": "a", "amt": 1},
            {"id": "9", "name": "b", "amt": 2},
        ], "append", ["id"])
    assert count_rows(db, ds) == 3


def test_upsert_run_append_dedup_skips_identical_rows_only(db):
    ds = _make_dataset(db)
    upsert_run(db, ds, BASE_ROWS, "overwrite", ["id"])
    # 与湖中全同的行被去重跳过；真正的新行入库
    _, changeset = upsert_run(db, ds, [
        {"id": "1", "name": "甲", "amt": 10},
        {"id": "4", "name": "丁", "amt": 40},
    ], "append_dedup", ["id"])
    assert changeset.added_count == 1
    assert count_rows(db, ds) == 4
    # 同主键但内容不同：整行去重无法识别同一业务对象 → 撞键失败
    with pytest.raises(LakeGateError, match="合并后的全量数据"):
        upsert_run(db, ds, [{"id": "1", "name": "甲改", "amt": 11}],
                   "append_dedup", ["id"])
    assert count_rows(db, ds) == 4


def test_upsert_run_evolves_columns_without_contract(db):
    """无发布契约：增量来数的新列走 ALTER TABLE 并集演化（保持现行语义）。"""
    ds = _make_dataset(db)
    upsert_run(db, ds, BASE_ROWS, "overwrite", ["id"])
    upsert_run(db, ds, [{"id": "2", "name": "乙", "amt": 20, "新列": "v"}],
               "upsert", ["id"])
    lake = _lake_map(db, ds, ["id"])
    assert lake[("2",)]["新列"] == "v"
    assert lake[("1",)]["新列"] == ""  # 既有行回填空串（restval 语义）
    ds = db.query(Dataset).filter(Dataset.id == ds.id).first()
    assert ds.schema_json["lake_columns"]["新列"] == "新列"
    assert ds.schema_json["columns"] == ["id", "name", "amt", "新列"]


def test_upsert_run_rejects_unknown_column_with_published_contract(db):
    ds = _make_dataset(db)
    upsert_run(db, ds, BASE_ROWS, "overwrite", ["id"])
    ds = db.query(Dataset).filter(Dataset.id == ds.id).first()
    ds.schema_json = {**ds.schema_json, "contract_definitions": [
        {"source_key": "id", "field_key": "id"}]}
    db.commit()
    with pytest.raises(LakeStoreStructureError):
        upsert_run(db, ds, [{"id": "9", "name": "x", "amt": 1, "新列": "v"}],
                   "upsert", ["id"])
    db.rollback()


def test_upsert_run_rejects_pk_mismatch(db):
    ds = _make_dataset(db)
    upsert_run(db, ds, BASE_ROWS, "overwrite", ["id"])
    with pytest.raises(LakeStoreStructureError):
        upsert_run(db, ds, [{"id": "9", "name": "x", "amt": 1}],
                   "upsert", ["amt"])
    db.rollback()


def test_upsert_run_overwrite_redeclare_pk_rebuilds_table(db):
    """overwrite 重写主键声明（gate 警告路径）：物理表整表重建为新主键。"""
    ds = _make_dataset(db)
    upsert_run(db, ds, BASE_ROWS, "overwrite", ["id"])
    # 调用方（gate + persist_contract allow_redeclare）先把契约改写到新主键
    ds = db.query(Dataset).filter(Dataset.id == ds.id).first()
    ds.schema_json = {**ds.schema_json, "primary_key": "name"}
    db.commit()
    upsert_run(db, ds, [
        {"id": "1", "name": "甲", "amt": 10},
        {"id": "9", "name": "癸", "amt": 90},
    ], "overwrite", ["name"])
    inspector = sa.inspect(db.connection())
    assert inspector.get_pk_constraint(
        lake_table_name(ds.id))["constrained_columns"] == ["name"]
    assert count_rows(db, ds) == 2
    assert _lake_map(db, ds, ["name"])[("癸",)]["amt"] == "90"


def test_upsert_run_rebuild_logs_destructive_warning(db, caplog):
    """整表重建（DROP+CREATE）必须留 warning：它是本模块破坏性最强的结构
    操作（旧表数据随 DROP 放弃），成功路径此前完全不可观测。"""
    import logging

    ds = _make_dataset(db)
    upsert_run(db, ds, BASE_ROWS, "overwrite", ["id"])
    ds = db.query(Dataset).filter(Dataset.id == ds.id).first()
    ds.schema_json = {**ds.schema_json, "primary_key": "name"}
    db.commit()

    with caplog.at_level(
            logging.WARNING,
            logger="app.data_channel.datasets.lake_store"):
        upsert_run(db, ds, [
            {"id": "1", "name": "甲", "amt": 10},
        ], "overwrite", ["name"])

    rebuild_records = [
        record for record in caplog.records
        if "整表重建" in record.getMessage() and ds.id in record.getMessage()
    ]
    assert rebuild_records, "湖表整表重建必须输出含 dataset id 的 warning"
    message = rebuild_records[0].getMessage()
    assert "name" in message  # 记录生效主键
    assert "1" in message  # 记录新契约列数（本例单列）


def test_upsert_run_soft_delete_marks_instead_of_deleting(db):
    ds = _make_dataset(db, columns=("id", "name", "flag"))
    upsert_run(db, ds, [
        {"id": "1", "name": "甲", "flag": "0"},
        {"id": "2", "name": "乙", "flag": "1"},
    ], "overwrite", ["id"])
    # 首版无标记列；第二次 upsert：id=1 改判删除（打标入湖），id=2 保持删除
    #（湖中 truthy 行未触及也按词表重评估打标）
    incoming = [{"id": "1", "name": "甲", "flag": "是"}]
    v2, cs2 = upsert_run(db, ds, incoming, "upsert", ["id"],
                         soft_delete_column="flag")
    lake = _lake_map(db, ds, ["id"])
    assert lake[("1",)]["__deleted__"] == "True"
    assert lake[("1",)]["__deleted_at__"]
    assert lake[("2",)]["__deleted__"] == "True"  # 湖中 truthy 行重评估打标
    # 来数就地打标（与 merge_rows 的就地语义一致）
    assert incoming[0]["__deleted__"] is True
    by_type = {}
    for row in _changeset_rows(db, cs2.id):
        by_type.setdefault(row.change_type, {})[row.row_pk] = row
    assert set(by_type["updated"]) == {"1", "2"}
    assert by_type["updated"]["1"].new_row["__deleted__"] == "True"

    # 复活：flag 改回 falsy → 摘除标记（物理列回填空串）
    v3, cs3 = upsert_run(db, ds, [{"id": "1", "name": "甲", "flag": "0"}],
                         "upsert", ["id"], soft_delete_column="flag")
    lake = _lake_map(db, ds, ["id"])
    assert lake[("1",)]["__deleted__"] == ""
    assert lake[("1",)]["__deleted_at__"] == ""
    assert lake[("2",)]["__deleted__"] == "True"  # 未触及的删除行保持打标


def test_upsert_run_normalizes_values_and_strips_pk(db):
    ds = _make_dataset(db, columns=("id", "name", "meta"))
    upsert_run(db, ds, [
        {"id": 1, "name": None, "meta": {"a": 1}},
        {"id": " 2 ", "name": "乙", "meta": [1, 2]},
    ], "overwrite", ["id"])
    lake = _lake_map(db, ds, ["id"])
    assert lake[("1",)] == {"id": "1", "name": "", "meta": '{"a": 1}'}
    # 主键列 strip：与审核 row_pk 身份口径一致
    assert lake[("2",)]["meta"] == "[1, 2]"


def test_upsert_run_rejects_non_curated(db):
    ds = _make_dataset(db, kind="structured")
    with pytest.raises(LakeStoreError):
        upsert_run(db, ds, BASE_ROWS, "overwrite", ["id"])


# ── 变更集逐行 old/new 与版本发布 ───────────────────────────
def test_changeset_rows_old_new_and_outbox_event(db):
    ds = _make_dataset(db)
    v1, cs1 = upsert_run(db, ds, BASE_ROWS, "overwrite", ["id"])
    assert (cs1.change_type, cs1.added_count) == ("run", 3)
    entries1 = _changeset_rows(db, cs1.id)
    assert {r.change_type for r in entries1} == {"added"}
    assert all(r.old_row is None and r.new_row for r in entries1)
    assert {r.row_pk for r in entries1} == {"1", "2", "3"}

    v2, cs2 = upsert_run(db, ds, [
        {"id": "2", "name": "乙改", "amt": 21},  # updated
        {"id": "4", "name": "丁", "amt": 40},    # added
    ], "upsert", ["id"])
    assert (cs2.added_count, cs2.updated_count, cs2.deleted_count) == (1, 1, 0)
    by_pk = {r.row_pk: r for r in _changeset_rows(db, cs2.id)}
    assert by_pk["2"].change_type == "updated"
    assert by_pk["2"].old_row == {"id": "2", "name": "乙", "amt": "20"}
    assert by_pk["2"].new_row == {"id": "2", "name": "乙改", "amt": "21"}
    assert by_pk["4"].change_type == "added"
    assert by_pk["4"].old_row is None

    # overwrite 删除 incoming 外的主键，deleted 记 old_row
    v3, cs3 = upsert_run(db, ds, [
        {"id": "2", "name": "乙改", "amt": 21},
    ], "overwrite", ["id"])
    assert (cs3.added_count, cs3.updated_count, cs3.deleted_count) == (0, 0, 3)
    deleted = {r.row_pk: r for r in _changeset_rows(db, cs3.id)}
    assert set(deleted) == {"1", "3", "4"}
    assert deleted["1"].old_row == {"id": "1", "name": "甲", "amt": "10"}
    assert deleted["1"].new_row is None

    # 复合主键 row_pk 走紧凑 JSON 数组编码
    ds2 = _make_dataset(db, pk="id,sku", columns=("id", "sku", "amt"))
    _, cs_c = upsert_run(db, ds2, [
        {"id": "1", "sku": "a", "amt": 10},
    ], "overwrite", ["id", "sku"])
    assert _changeset_rows(db, cs_c.id)[0].row_pk == '["1","a"]'

    # 版本发布：湖表版本无 blob 载荷，checksum = 变更集规范哈希
    assert v2.data_blob is None and v2.data_size is None and v2.storage_uri is None
    assert v2.rowcount == 4 and v2.checksum == cs2.checksum
    assert v2.version_no == v1.version_no + 1
    ds = db.query(Dataset).filter(Dataset.id == ds.id).first()
    assert ds.latest_version_id == v3.id
    # outbox：版本行与发布事件同事务
    events = db.query(DatasetVersionEvent).filter(
        DatasetVersionEvent.dataset_version_id == v3.id).all()
    assert len(events) == 1
    assert events[0].event_type == "version_published"
    assert events[0].status == "pending"
    assert events[0].dataset_id == ds.id


# ── 读取：分页 / 批读 / 按键取行 ────────────────────────────
def test_page_stream_and_count_rows(db):
    ds = _make_dataset(db, columns=("id", "name"))
    rows = [{"id": f"{i:03d}", "name": f"n{i}"} for i in range(25)]
    upsert_run(db, ds, rows, "overwrite", ["id"])

    assert count_rows(db, ds) == 25
    page1 = page_rows(db, ds, offset=0, limit=10)
    page2 = page_rows(db, ds, offset=10, limit=10)
    page3 = page_rows(db, ds, offset=20, limit=10)
    assert (len(page1), len(page2), len(page3)) == (10, 10, 5)
    assert [r["id"] for r in page1] == [f"{i:03d}" for i in range(10)]
    assert page3[-1]["id"] == "024"
    assert page_rows(db, ds, offset=25, limit=10) == []

    batches = list(stream_rows(db, ds, batch_size=10))
    assert [len(b) for b in batches] == [10, 10, 5]
    assert [r["id"] for b in batches for r in b] == [r["id"] for r in rows]


def test_rows_by_pks_single_and_composite(db):
    ds = _make_dataset(db)
    upsert_run(db, ds, BASE_ROWS, "overwrite", ["id"])
    found = rows_by_pks(db, ds, ["1", "3", "不存在"])
    assert set(found) == {"1", "3"}
    assert found["1"] == {"id": "1", "name": "甲", "amt": "10"}

    ds2 = _make_dataset(db, pk="id,sku", columns=("id", "sku", "amt"))
    upsert_run(db, ds2, [
        {"id": "1", "sku": "a", "amt": 10},
        {"id": "1", "sku": "b", "amt": 11},
    ], "overwrite", ["id", "sku"])
    found2 = rows_by_pks(db, ds2, ['["1","b"]', '["9","z"]'])
    assert set(found2) == {'["1","b"]'}
    assert found2['["1","b"]']["amt"] == "11"


def test_reads_on_missing_table_are_empty(db):
    ds = _make_dataset(db)  # 契约存在但从未建表
    assert count_rows(db, ds) == 0
    assert page_rows(db, ds, 0, 10) == []
    assert list(stream_rows(db, ds)) == []
    assert rows_by_pks(db, ds, ["1"]) == {}


# ── 版本逆向回放 ────────────────────────────────────────────
def test_rows_at_version_reverse_replay(db):
    ds = _make_dataset(db)
    v1, _ = upsert_run(db, ds, BASE_ROWS, "overwrite", ["id"])
    v2, _ = upsert_run(db, ds, [
        {"id": "2", "name": "乙改", "amt": 21},
        {"id": "4", "name": "丁", "amt": 40},
    ], "upsert", ["id"])
    v3, _ = upsert_run(db, ds, [
        {"id": "2", "name": "乙再改", "amt": 22},
        {"id": "4", "name": "丁", "amt": 40},
        {"id": "5", "name": "戊", "amt": 50},
    ], "overwrite", ["id"])

    def at(version_no):
        return {r["id"]: r for r in rows_at_version(db, ds, version_no)}

    assert at(v3.version_no) == {
        "2": {"id": "2", "name": "乙再改", "amt": "22"},
        "4": {"id": "4", "name": "丁", "amt": "40"},
        "5": {"id": "5", "name": "戊", "amt": "50"},
    }
    assert at(v2.version_no) == {
        "1": {"id": "1", "name": "甲", "amt": "10"},
        "2": {"id": "2", "name": "乙改", "amt": "21"},
        "3": {"id": "3", "name": "丙", "amt": "30"},
        "4": {"id": "4", "name": "丁", "amt": "40"},
    }
    assert at(v1.version_no) == {
        "1": {"id": "1", "name": "甲", "amt": "10"},
        "2": {"id": "2", "name": "乙", "amt": "20"},
        "3": {"id": "3", "name": "丙", "amt": "30"},
    }
    with pytest.raises(LakeStoreError):
        rows_at_version(db, ds, 999)


def test_rows_at_version_rejects_legacy_blob_version(db):
    ds = _make_dataset(db)
    ver = DatasetVersion(
        id=str(uuid.uuid4()), dataset_id=ds.id, version_no=1,
        rowcount=1, data_blob=b'[{"id":"1"}]', data_size=11,
        checksum="legacy")
    db.add(ver)
    db.commit()
    with pytest.raises(LakeStoreLegacyVersionError):
        rows_at_version(db, ds, 1)


# ── 无主键资产 ──────────────────────────────────────────────
def test_no_pk_overwrite_and_append_dedup(db):
    ds = _make_dataset(db, pk=None, columns=("a", "b"))
    v1, _ = upsert_run(db, ds, [
        {"a": "1", "b": "x"},
        {"a": "1", "b": "x"},  # 无主键：完全相同的行不去重
        {"a": "2", "b": "y"},
    ], "overwrite", [])
    assert count_rows(db, ds) == 3

    # append_dedup：湖中全同的行与批内全同的行都跳过
    v2, cs2 = upsert_run(db, ds, [
        {"a": "1", "b": "x"},
        {"a": "3", "b": "z"},
        {"a": "3", "b": "z"},
    ], "append_dedup", [])
    assert cs2.added_count == 1
    assert count_rows(db, ds) == 4

    # 无主键回放按整行签名：v1 内容完整恢复
    replayed = sorted(json.dumps(r, sort_keys=True)
                      for r in rows_at_version(db, ds, v1.version_no))
    assert replayed == sorted(json.dumps(r, sort_keys=True) for r in [
        {"a": "1", "b": "x"}, {"a": "1", "b": "x"}, {"a": "2", "b": "y"}])
    with pytest.raises(LakeStoreError):
        rows_by_pks(db, ds, ["1"])


def test_no_pk_overwrite_multiset_and_set_level_changeset(db):
    """无主键 overwrite：物理表保留来数重复行（多重集），变更集按整行签名
    集合级求差（重复折叠），rowcount 按物理行数记账。"""
    ds = _make_dataset(db, pk=None, columns=("a", "b"))
    upsert_run(db, ds, [
        {"a": "1", "b": "x"},
        {"a": "1", "b": "x"},  # 湖中重复行
        {"a": "2", "b": "y"},
        {"a": "3", "b": "z"},
    ], "overwrite", [])

    v2, cs2 = upsert_run(db, ds, [
        {"a": "1", "b": "x"},  # 与湖中重复行同签名 → 集合级 unchanged
        {"a": "3", "b": "z"},
        {"a": "3", "b": "z"},  # 来数重复：物理保留两份，变更集只记一个 added
        {"a": "4", "b": "w"},
    ], "overwrite", [])
    rows = [r for batch in stream_rows(db, ds) for r in batch]
    assert sorted(json.dumps(r, sort_keys=True) for r in rows) == sorted(
        json.dumps(r, sort_keys=True) for r in [
            {"a": "1", "b": "x"}, {"a": "3", "b": "z"},
            {"a": "3", "b": "z"}, {"a": "4", "b": "w"}])
    assert v2.rowcount == 4
    # 集合级求差：added={4,w}、deleted={2,y}，各 1 条（重复折叠、无 updated）
    assert (cs2.added_count, cs2.updated_count, cs2.deleted_count) == (1, 0, 1)
    entries = _changeset_rows(db, cs2.id)
    added = [r for r in entries if r.change_type == "added"]
    deleted = [r for r in entries if r.change_type == "deleted"]
    assert [r.new_row for r in added] == [{"a": "4", "b": "w"}]
    assert [r.old_row for r in deleted] == [{"a": "2", "b": "y"}]


def test_no_pk_upsert_soft_delete_reeval_multiset(db):
    """无主键 upsert+软删除：湖中存量逐实例重评估（重复行逐份记变更集）；
    已正确打标的行保持原时间戳且不重复记变更。"""
    ds = _make_dataset(db, pk=None, columns=("v", "flag"))
    upsert_run(db, ds, [
        {"v": "1", "flag": "0"},
        {"v": "2", "flag": "是"},
        {"v": "2", "flag": "是"},  # 重复实例：两份都要打标
    ], "overwrite", [])
    v2, cs2 = upsert_run(db, ds, [{"v": "3", "flag": "no"}], "upsert", [],
                         soft_delete_column="flag")
    rows = [r for batch in stream_rows(db, ds) for r in batch]
    assert len(rows) == 4  # 3 存量 + 1 追加
    marked = [r for r in rows if r.get("__deleted__") == "True"]
    assert len(marked) == 2  # 两份重复实例都打上标
    # 变更集逐实例：2 组 打标前行 deleted + 打标后行 added，外加 1 条追加 added
    assert (cs2.added_count, cs2.updated_count, cs2.deleted_count) == (3, 0, 2)
    marked_ts = sorted(r["__deleted_at__"] for r in marked)

    # 再次运行：已打标行保持原时间戳，不产生打标变更（仅追加 1 行）
    v3, cs3 = upsert_run(db, ds, [{"v": "4", "flag": "0"}], "upsert", [],
                         soft_delete_column="flag")
    rows_after = [r for batch in stream_rows(db, ds) for r in batch]
    assert sorted(r["__deleted_at__"] for r in rows_after
                  if r.get("__deleted__") == "True") == marked_ts
    assert (cs3.added_count, cs3.updated_count, cs3.deleted_count) == (1, 0, 0)
    assert v3.rowcount == 5


def test_no_pk_upsert_soft_delete_clears_stale_marker_text(db):
    """无主键软删除重评估：falsy 行携带非空非 'True' 标记文本时回填空串，
    按 deleted(old)+added(new) 对记变更集。"""
    ds = _make_dataset(db, pk=None,
                       columns=("v", "flag", "__deleted__", "__deleted_at__"))
    upsert_run(db, ds, [
        {"v": "1", "flag": "0", "__deleted__": "False",
         "__deleted_at__": "2020-01-01"},
    ], "overwrite", [])
    v2, cs2 = upsert_run(db, ds, [{"v": "2", "flag": "0"}], "upsert", [],
                         soft_delete_column="flag")
    rows = [r for batch in stream_rows(db, ds) for r in batch]
    by_v = {r["v"]: r for r in rows}
    assert by_v["1"]["__deleted__"] == ""
    assert by_v["1"]["__deleted_at__"] == ""
    # 追加 1 added + 清除重写 1 组 deleted+added
    assert (cs2.added_count, cs2.updated_count, cs2.deleted_count) == (2, 0, 1)
    entries = _changeset_rows(db, cs2.id)
    cleared = [r for r in entries if r.change_type == "deleted"]
    assert cleared[0].old_row["__deleted__"] == "False"


def test_no_pk_append_dedup_multiset_skip(db):
    """湖中已有两份全同行时，来数全同行仍整体跳过（集合语义），不产生变更。"""
    ds = _make_dataset(db, pk=None, columns=("a", "b"))
    upsert_run(db, ds, [
        {"a": "1", "b": "x"}, {"a": "1", "b": "x"}], "overwrite", [])
    v2, cs2 = upsert_run(db, ds, [{"a": "1", "b": "x"}], "append_dedup", [])
    assert (cs2.added_count, cs2.updated_count, cs2.deleted_count) == (0, 0, 0)
    assert count_rows(db, ds) == 2
    assert v2.rowcount == 2


def _as_batches(rows: list[dict], n: int):
    return (rows[i:i + n] for i in range(0, len(rows), n))


def _scrub_marker_ts(row: dict) -> dict:
    return {k: ("<ts>" if k == "__deleted_at__" else v) for k, v in row.items()}


@pytest.mark.parametrize("mode", ["overwrite", "append", "append_dedup", "upsert"])
@pytest.mark.parametrize("pk", [None, "id"])
def test_upsert_run_iterable_batches_match_list_path(db, mode, pk):
    """批次迭代器形态与 list 形态同场景入湖：湖内容/变更集明细/rowcount 一致
    （含跨批重复主键末现、批内全同首现、软删除打标与存量重评估、错误一致）。"""
    import re
    pk_cols = [pk] if pk else []
    soft = "flag" if mode == "upsert" else ""
    base = [
        {"id": f"k{i:03d}", "v": f"旧{i % 5}",
         "flag": "1" if i % 4 == 0 else "0"}
        for i in range(37)]
    incoming = (
        [{"id": f"k{i:03d}", "v": "新", "flag": "0" if i % 2 else "是"}
         for i in range(0, 37, 3)]
        + [{"id": f"k{i:03d}", "v": "新2", "flag": "0"}
           for i in range(0, 37, 3)]  # 同主键再现（末现为准）
        + [{"id": f"n{j:02d}", "v": "x", "flag": "0"} for j in range(11)]
    )

    def _scrub_pk(row_pk: str) -> str:
        # 无主键资产的 row_pk 是整行 JSON，内嵌打标时间戳须一并掩码
        return re.sub(r'("__deleted_at__":")[^"]*(")', r"\1<TS>\2", row_pk)

    outcomes = []
    for payload in ([dict(r) for r in incoming],
                    _as_batches([dict(r) for r in incoming], 5)):
        ds = _make_dataset(db, pk=pk, columns=("id", "v", "flag"))
        upsert_run(db, ds, [dict(r) for r in base], "overwrite", pk_cols)
        try:
            version, changeset = upsert_run(
                db, ds, payload, mode, pk_cols, soft_delete_column=soft)
        except Exception as exc:  # noqa: BLE001 — 等价性比较错误身份
            outcomes.append(("error", type(exc).__name__,
                             str(exc).replace(ds.name, "<ds>")))
            continue
        rows = sorted(
            json.dumps(_scrub_marker_ts(r), sort_keys=True)
            for batch in stream_rows(db, ds, batch_size=7) for r in batch)
        entries = sorted(
            json.dumps({"t": r.change_type, "pk": _scrub_pk(r.row_pk),
                        "old": _scrub_marker_ts(r.old_row or {}),
                        "new": _scrub_marker_ts(r.new_row or {})},
                       sort_keys=True, default=str)
            for r in _changeset_rows(db, changeset.id))
        outcomes.append(("ok", int(version.rowcount), changeset.added_count,
                         changeset.updated_count, changeset.deleted_count,
                         rows, entries))
    assert outcomes[0] == outcomes[1]


# ── 遗留 blob 基座的懒引导 ──────────────────────────────────
def test_upsert_without_bootstrap_on_legacy_history_is_rejected(db):
    """有 blob 历史而无物理表：直接入湖拒绝（防存量被当空湖丢失）。"""
    from app.data_channel.datasets.service import (
        DatasetService, rows_to_parquet_bytes)

    ds = _make_dataset(db)
    DatasetService(db).create_version(
        ds.id, rows_to_parquet_bytes([{"id": "1", "name": "甲", "amt": "10"}]),
        rowcount=1)
    with pytest.raises(LakeStoreError, match="bootstrap_lake_base"):
        upsert_run(db, ds, [{"id": "2", "name": "乙", "amt": "20"}],
                   "upsert", ["id"])
    db.rollback()


def test_bootstrap_from_legacy_blob_on_first_incremental_run(db):
    """迁移未覆盖的数据集（无物理表、有 blob 版本）：首次增量运行先经
    DatasetService.bootstrap_lake_base 以遗留基座灌表 + baseline 变更集
    （挂在 blob 版本上），增量按基座正确求差。"""
    from app.data_channel.datasets.service import (
        DatasetService, rows_to_parquet_bytes)

    ds = _make_dataset(db)
    svc = DatasetService(db)
    svc.create_version(ds.id, rows_to_parquet_bytes([
        {"id": "1", "name": "甲", "amt": "10"},
        {"id": "2", "name": "乙", "amt": "20"},
    ]), rowcount=2)  # v1：blob 历史版本
    blob_version_id = ds.latest_version_id

    # 懒引导：基座进物理表；随后的增量按基座求差
    assert svc.bootstrap_lake_base(ds) is True
    v2, cs2 = upsert_run(db, ds, [
        {"id": "2", "name": "乙改", "amt": "21"},
        {"id": "3", "name": "丙", "amt": "30"},
    ], "upsert", ["id"])

    assert count_rows(db, ds) == 3
    assert _lake_map(db, ds, ["id"])[("2",)]["name"] == "乙改"
    # baseline 变更集挂在 blob 版本上（仅计数）；本次运行只记真实增量
    from app.data_channel.datasets.models import DatasetChangeset
    baseline = db.query(DatasetChangeset).filter(
        DatasetChangeset.version_id == blob_version_id).one()
    assert baseline.change_type == "baseline"
    assert (baseline.added_count, baseline.updated_count,
            baseline.deleted_count) == (2, 0, 0)
    assert (cs2.added_count, cs2.updated_count, cs2.deleted_count) == (1, 1, 0)
    assert v2.version_no == 2
    # 幂等：重复引导返回 False
    assert svc.bootstrap_lake_base(ds) is False
    # 回放 v2 = 当前；v1 是 blob 版本走遗留异常
    assert len(rows_at_version(db, ds, v2.version_no)) == 3
    with pytest.raises(LakeStoreLegacyVersionError):
        rows_at_version(db, ds, 1)


def test_bootstrap_overwrite_also_bootstraps_for_audit(db):
    """overwrite 也先懒引导：审计 diff 的口径是「与上一版本内容的差异」，
    空基座会把删除/更新误记为新增。"""
    from app.data_channel.datasets.service import (
        DatasetService, rows_to_parquet_bytes)
    from app.data_channel.datasets.models import DatasetChangeset

    ds = _make_dataset(db)
    svc = DatasetService(db)
    legacy_version = svc.create_version(
        ds.id, rows_to_parquet_bytes([{"id": "1", "name": "甲", "amt": "10"}]),
        rowcount=1)
    assert svc.bootstrap_lake_base(ds) is True
    v2, cs2 = upsert_run(db, ds, [{"id": "9", "name": "癸", "amt": "90"}],
                         "overwrite", ["id"])
    assert count_rows(db, ds) == 1
    baseline = db.query(DatasetChangeset).filter(
        DatasetChangeset.version_id == legacy_version.id,
        DatasetChangeset.change_type == "baseline").one()
    assert baseline.added_count == 1
    # 运行变更集如实记录：遗留行删除、新行新增
    assert (cs2.added_count, cs2.updated_count, cs2.deleted_count) == (1, 0, 1)
