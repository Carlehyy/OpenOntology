"""物理湖表性能基准：lake_store 行级 upsert / 分页 / 流式读 / 版本回放。

规模设计（CI shard 预算约束，与已退役的合并引擎基准同一惯例）：
- 默认 CI 规模：10 万行基座 + 1 万行增量。overwrite 全量替换与跨 overwrite
  逆向回放是最重路径（50 万行本机即需 ~114s/~22s），CI 4 vCPU 上会撑爆
  verify-backend shard 的 15 分钟超时（曾导致 run 31499091029 shard 1 被
  取消）——O(N) 成本按 10 万行量测线性外推即可证明量级；
- 本机全量规模：LAKE_PERF_SCALE=full 跑 50 万行基座 + 5 万行增量；
- 基座大头用 Core 批量直插预置（仅基准 fixture 捷径，避开来数字典的
  Python 生成成本——该行级成本在记录里单独标注）；
- 方言 SQLite（本机临时文件）。PG 侧差异：TEMP 暂存与 PK 索引 JOIN 在 PG
  上通常不劣于 SQLite；OFFSET 深分页两方言同构（都需扫过前 N 行）。
- tracemalloc 只追踪 Python 侧分配。

报告写 .artifacts/lake-store-benchmark.json（gitignored，不进 PR 门禁）。
"""
from __future__ import annotations

import gc
import json
import os
import platform
import time
import tracemalloc
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.data_channel.datasets import lake_store
from app.data_channel.datasets.models import Dataset

_FULL_SCALE = os.environ.get("LAKE_PERF_SCALE", "").strip().lower() == "full"
BASE_ROWS = 500_000 if _FULL_SCALE else 100_000
INC_ROWS = 50_000 if _FULL_SCALE else 10_000
COLS = 8

REPORT_PATH = (
    Path(__file__).resolve().parents[4]
    / ".artifacts" / "lake-store-benchmark.json"
)

_records: list[dict] = []


def _row(i: int, *, tag: str = "v") -> dict:
    return {"id": f"k{i:07d}",
            **{f"c{j}": f"{tag}{i % 5000}-{j}" for j in range(COLS - 1)}}


def _measure(fn):
    gc.collect()
    tracemalloc.start()
    start = time.perf_counter()
    result = fn()
    wall = time.perf_counter() - start
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return result, wall, peak


def _iter_batches(rows: list[dict], n: int):
    """把全量行切成 n 行的批次迭代器（L2 流式入湖形态）。"""
    return (rows[i:i + n] for i in range(0, len(rows), n))


def _record(scenario: str, wall: float, peak: int, note: str = "") -> dict:
    rec = {
        "scenario": scenario,
        "wall_s": round(wall, 3),
        "peak_tracemalloc_mb": round(peak / 1024 / 1024, 1),
        "note": note,
    }
    _records.append(rec)
    return rec


def _write_report():
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps({
        "title": f"lake_store 物理湖表基准（SQLite，{BASE_ROWS // 10_000} 万行基座）",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "dialect": "sqlite",
        },
        "method": "墙钟 = time.perf_counter；峰值 = tracemalloc（仅 Python 侧）",
        "records": _records,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture(scope="module")
def lake_db(tmp_path_factory):
    """独立 SQLite 文件库 + 50 万行基座（模块级，整个基准共用）。"""
    from app.database import Base
    from app.model_registry import import_all_models

    import_all_models()
    db_path = tmp_path_factory.mktemp("lake_perf") / "bench.db"
    engine = sa.create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    ds = Dataset(
        id=str(uuid.uuid4()), name="基准数据集", kind="curated",
        schema_json={"primary_key": "id",
                     "columns": ["id"] + [f"c{j}" for j in range(COLS - 1)]})
    session.add(ds)
    session.commit()

    # v1：经 upsert_run 建立物理表与首个版本（小批 5000 行）
    lake_store.upsert_run(session, ds, [_row(i) for i in range(5000)],
                          "overwrite", ["id"])
    # 基座大头：Core 批量直插预置（基准 fixture 捷径，不产生变更集；
    # 避免为造数先支付一次 50 万行 dict 的 Python 物化）
    _, mapping, _ = lake_store._contract(ds)
    table = lake_store.lake_table_definition(
        lake_store.lake_table_name(ds.id), mapping, ["id"])
    gen_start = time.perf_counter()
    conn = session.connection()
    batch = []
    for i in range(5000, BASE_ROWS):
        batch.append(lake_store.normalize_lake_rows([_row(i)], mapping, ["id"])[0])
        if len(batch) >= 5000:
            conn.execute(table.insert(), batch)
            batch.clear()
    if batch:
        conn.execute(table.insert(), batch)
    session.commit()
    gen_wall = time.perf_counter() - gen_start
    _record(f"base_build_{BASE_ROWS}_direct_insert", gen_wall, 0,
            note="直插预置基座（含逐行规范化），仅供 fixture，不走 upsert_run")
    yield session, ds
    session.close()
    engine.dispose()


def test_benchmark_lake_store(lake_db):
    session, ds = lake_db

    # ── 5 万行增量 upsert（全命中既有主键，最重合并形态）────────────
    inc_rows = [_row(i % BASE_ROWS, tag="n") for i in range(INC_ROWS)]
    (version, changeset), wall, peak = _measure(
        lambda: lake_store.upsert_run(session, ds, inc_rows, "upsert", ["id"]))
    assert changeset.updated_count == INC_ROWS
    _record(f"upsert_inc_{INC_ROWS}_on_base_{BASE_ROWS}", wall, peak,
            note="TEMP 暂存 + SQL diff + DELETE/INSERT + 增量条数级变更集逐行")

    # ── 50 万行 overwrite 全量替换 ────────────────────────────────
    overwrite_rows = [_row(i, tag="w") for i in range(BASE_ROWS)]
    (v_over, cs_over), wall, peak = _measure(
        lambda: lake_store.upsert_run(
            session, ds, overwrite_rows, "overwrite", ["id"]))
    assert v_over.rowcount == BASE_ROWS
    assert cs_over.added_count + cs_over.updated_count > 0
    _record(f"overwrite_{BASE_ROWS}_full_replace", wall, peak,
            note="全量替换：整表 staged + 删除旧行 + 变更集逐行（最重路径）")

    # ── 深分页 vs 首页 ───────────────────────────────────────────
    page, wall_first, _ = _measure(
        lambda: lake_store.page_rows(session, ds, 0, 100))
    assert len(page) == 100
    _record("page_rows_first_page_100", wall_first, 0, note="主键序首页")
    page, wall_deep, _ = _measure(
        lambda: lake_store.page_rows(session, ds, BASE_ROWS - 10_000, 100))
    assert len(page) == 100
    rec = _record(f"page_rows_deep_offset_{BASE_ROWS - 10_000}", wall_deep, 0,
                  note=f"OFFSET 深分页（两方言同构需扫过前 {BASE_ROWS - 10_000} 行）")
    assert wall_first < 5 and wall_deep < 30

    # ── 全量流式读 ───────────────────────────────────────────────
    total, wall, peak = _measure(lambda: sum(
        len(batch) for batch in lake_store.stream_rows(
            session, ds, batch_size=5000)))
    assert total == BASE_ROWS
    _record(f"stream_rows_full_{BASE_ROWS}", wall, peak,
            note="5000/批全量流式收集计数（不持有全量列表）")

    # ── 逆向回放到上一版本（当前 50 万表 + 撤销 overwrite 变更集）────
    target = v_over.version_no - 1
    rows, wall, peak = _measure(
        lambda: lake_store.rows_at_version(session, ds, target))
    assert len(rows) == BASE_ROWS
    _record("rows_at_version_replay_one_step", wall, peak,
            note=f"回放到 v{target}：全表流式 + 撤销 1 个 overwrite 变更集")

    # ── 同一 overwrite 以批次迭代器供给（L2 流式形态；置于回放基准
    #    之后，避免多读一个变更集干扰回放口径）────────────────────
    stream_rows_in = [_row(i, tag="s") for i in range(BASE_ROWS)]
    (v_stream, cs_stream), wall, peak = _measure(
        lambda: lake_store.upsert_run(
            session, ds, _iter_batches(stream_rows_in, 5000),
            "overwrite", ["id"]))
    assert v_stream.rowcount == BASE_ROWS
    assert cs_stream.updated_count == BASE_ROWS  # 全量取代（tag w→s）
    _record(f"overwrite_{BASE_ROWS}_stream_batches_5000", wall, peak,
            note="L2 流式：同一全量替换以 5000/批迭代器供给；"
            "来数侧峰值应与分块大小而非总量相关")

    _write_report()


def _nopk_row(i: int, *, tag: str = "v") -> dict:
    return {"k": f"k{i:07d}",
            **{f"c{j}": f"{tag}{i % 5000}-{j}" for j in range(COLS - 2)},
            # 1% truthy：供软删除重评估场景产生小比例存量变更
            "flag": "1" if i % 100 == 0 else "0"}


@pytest.fixture(scope="module")
def lake_db_nopk(tmp_path_factory):
    """无主键数据集基座（独立库）：10 万行 × 8 列，直插预置。"""
    from app.database import Base
    from app.model_registry import import_all_models

    import_all_models()
    db_path = tmp_path_factory.mktemp("lake_perf_nopk") / "bench.db"
    engine = sa.create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    ds = Dataset(
        id=str(uuid.uuid4()), name="基准数据集-无主键", kind="curated",
        schema_json={"columns": ["k"] + [f"c{j}" for j in range(COLS - 2)]
                     + ["flag"]})
    session.add(ds)
    session.commit()

    lake_store.upsert_run(session, ds, [_nopk_row(i) for i in range(5000)],
                          "overwrite", [])
    _, mapping, _ = lake_store._contract(ds)
    table = lake_store.lake_table_definition(
        lake_store.lake_table_name(ds.id), mapping, [])
    conn = session.connection()
    batch = []
    for i in range(5000, BASE_ROWS):
        batch.append(lake_store.normalize_lake_rows([_nopk_row(i)], mapping, [])[0])
        if len(batch) >= 5000:
            conn.execute(table.insert(), batch)
            batch.clear()
    if batch:
        conn.execute(table.insert(), batch)
    session.commit()
    yield session, ds
    session.close()
    engine.dispose()


def test_benchmark_lake_store_no_pk(lake_db_nopk):
    """无主键分支基准：核心验收 = Python 峰值与墙钟不再随湖总量增长（只随
    当批来数 + 实际变化行增长）。"""
    session, ds = lake_db_nopk

    # 湖内容读回一次（测量外），供构造"源未变"批次
    readback = [row for batch in lake_store.stream_rows(session, ds)
                for row in batch]
    assert len(readback) == BASE_ROWS

    # ── overwrite 同内容重跑（定时任务源未变，生产最常见形态）────────
    (v_same, cs_same), wall, peak = _measure(
        lambda: lake_store.upsert_run(session, ds, list(readback),
                                      "overwrite", []))
    assert v_same.rowcount == BASE_ROWS
    assert (cs_same.added_count, cs_same.updated_count,
            cs_same.deleted_count) == (0, 0, 0)
    _record(f"nopk_overwrite_same_content_{BASE_ROWS}", wall, peak,
            note="同内容重跑：变更集应为空；峰值理想值 ≈ 0（不读湖进 Python）")

    # ── append_dedup 全重复批次（零追加）──────────────────────────
    dup_batch = readback[:INC_ROWS]
    (v_dup, cs_dup), wall, peak = _measure(
        lambda: lake_store.upsert_run(session, ds, list(dup_batch),
                                      "append_dedup", []))
    assert cs_dup.added_count == 0
    assert v_dup.rowcount == BASE_ROWS
    _record(f"nopk_append_dedup_all_identical_{INC_ROWS}_on_{BASE_ROWS}",
            wall, peak,
            note="全重复批次：反连接过滤应在 DB 端完成，峰值理想值 ≈ 批大小")

    # ── upsert + 软删除：1 万新行追加 + 湖中 1% truthy 存量打标 ─────
    novel = [{"k": f"n{i:07d}",
              **{f"c{j}": f"n{i % 5000}-{j}" for j in range(COLS - 2)},
              "flag": "0"} for i in range(INC_ROWS)]
    (v_soft, cs_soft), wall, peak = _measure(
        lambda: lake_store.upsert_run(session, ds, novel, "upsert", [],
                                      soft_delete_column="flag"))
    truthy = BASE_ROWS // 100
    assert cs_soft.deleted_count == truthy           # 打标前行
    assert cs_soft.added_count == INC_ROWS + truthy  # 追加 + 打标后行
    assert v_soft.rowcount == BASE_ROWS + INC_ROWS
    _record(f"nopk_upsert_soft_reeval_{truthy}_flips_on_{BASE_ROWS}", wall, peak,
            note="存量小比例打标：应定向 UPDATE 命中行而非整表物化+重写")

    _write_report()
