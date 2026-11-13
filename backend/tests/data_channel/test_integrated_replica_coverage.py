"""replica_coverage 聚焦测试（真实 SQLite；无源 IO/容器）。

契约被测面：coverage cut 与 transport liveness 分离；PG cut 只接受本 capture
的 non-transactional logical message（op=m、topic=<server>.message、
offset.lsn==lsn_proc==lsn_commit、source.lsn 同值、transaction 为空、content
base64(token) 精确匹配）；MySQL cut 优先 GTID completed-set 包含，GTID 不可用时
只按同前缀 binlog 文件数值序 + pos 比较；snapshot/row/END offset 不得由本模块
自动当作 stream proof（ingest 负责筛选）；observed cut 只有在 cohort 全部成员
apply 到 observed_seq 后才 confirmed；generation/token/replay 不得串代；confirmed
时间用 requested_at 保守值；状态只保留单个 pending/confirmed，不累积。

这里也是模块边界的反例集：缺省 pending 超时/回收、applied_probe 不校验
capture.tables_json 成员集合、applied_probe 不 flush、PG frontier 缺 lsn 时
record_probe 不早拒，均以测试或注释显式记录；模块本身不在本测试内修改。
"""
from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from app.data_channel.connections.models import Connection
from app.data_channel.datasets.models import Dataset
from app.data_channel.integrated import replica_coverage as coverage
from app.data_channel.integrated.models import IntegratedAsset
from app.data_channel.integrated.replica_capture_models import IntegratedReplicaCapture
from app.data_channel.integrated.replica_models import IntegratedReplica, IntegratedReplicaRun

GTID_A = "3e11fa47-71ca-11e1-9e33-c80aa9429562"
GTID_B = "ca11ab1e-71ca-11e1-9e33-c80aa9429562"
TS = "2026-11-13T00:00:00+00:00"


# ---------------------------------------------------------------------------
# 最小 cohort（只建 coverage 需要的 capture/run；不建物理 replica 表）
# ---------------------------------------------------------------------------

def make_cohort(db, admin_user, *, run_statuses=("catching_up",),
                applied_seqs=(0,), capture_status="streaming",
                generation=1, committed_seq=0):
    suffix = uuid.uuid4().hex[:8]
    connection = Connection(id=f"cov-conn-{suffix}", name=f"cov {suffix}",
                            kind="postgres", config={}, status="active",
                            created_by=admin_user.id)
    db.add(connection)
    db.flush()
    capture = IntegratedReplicaCapture(
        id=str(uuid.uuid4()), connection_id=connection.id, generation=generation,
        status=capture_status, identity_fingerprint="i" * 64, tables_json=[],
        committed_seq=committed_seq)
    db.add(capture)
    db.flush()
    members = []
    for index, (status, applied) in enumerate(zip(run_statuses, applied_seqs)):
        dataset = Dataset(id=f"cov-ds-{suffix}-{index}", name=f"cov {suffix} {index}",
                          kind="integrated", source_connection_id=connection.id,
                          source_resource=f"t{index}")
        asset = IntegratedAsset(
            id=f"cov-asset-{suffix}-{index}", dataset_id=dataset.id,
            connection_id=connection.id, name=f"cov {suffix} {index}",
            schema_name="public", table_name=f"t{index}",
            schema_json={"columns": [], "primary_key": []},
            schema_fingerprint="f" * 64, writable=False, created_by=admin_user.id)
        replica = IntegratedReplica(
            asset_id=asset.id, status="ready" if status == "active" else "catching_up",
            generation=generation, schema_fingerprint="f" * 64,
            applied_position={"commit_seq": applied})
        run = IntegratedReplicaRun(
            id=f"cov-run-{suffix}-{index}", asset_id=asset.id,
            generation=generation, status=status,
            schema_json={"columns": [], "primary_key": []},
            schema_fingerprint="f" * 64, capture_id=capture.id,
            applied_position={"commit_seq": applied})
        db.add_all([dataset, asset, replica, run])
        members.append(SimpleNamespace(asset=asset, replica=replica, run=run))
    db.flush()
    capture.tables_json = [{"asset_id": m.asset.id, "run_id": m.run.id}
                           for m in members]
    db.commit()
    return SimpleNamespace(connection=connection, capture=capture,
                           members=members, server=coverage.prefix(capture.id)[
                               len("oo_replica_cut_"):])


def reload_capture(db, capture_id):
    db.expire_all()
    return db.scalar(sa.select(IntegratedReplicaCapture).where(
        IntegratedReplicaCapture.id == capture_id)
        .execution_options(populate_existing=True))


def runs_of(world):
    """coverage.applied_probe consumes run rows, never asset/replica wrappers."""
    return [member.run for member in world.members]


def token_of(db, capture):
    probe = coverage.prepare_probe(db, capture.id)
    assert probe is not None
    db.commit()
    return probe["id"]


def pg_message(capture, token, *, prefix=None, topic=None, offset=None,
               source_lsn=None, transaction=None, content=None,
               lsn=900, lsn_proc=None, lsn_commit=None, message_type="MESSAGE"):
    encoded = content if content is not None else base64.b64encode(
        token.encode("ascii")).decode("ascii")
    lsn_proc = lsn if lsn_proc is None else lsn_proc
    lsn_commit = lsn if lsn_commit is None else lsn_commit
    envelope = {
        "schema": {"name": "io.debezium.connector.postgresql.MessageValue"},
        "payload": {
            "op": "m",
            "source": {"lsn": lsn if source_lsn is None else source_lsn},
            "transaction": transaction,
            "message": {"prefix": prefix or coverage.prefix(capture.id),
                        "content": encoded},
        },
    }
    offset = offset or {"lsn": lsn, "lsn_proc": lsn_proc, "lsn_commit": lsn_commit,
                        "messageType": message_type}
    topic = topic or "oo_replica_" + uuid.UUID(capture.id).hex + ".message"
    return envelope, offset, topic


def apply_message(db, capture, token, **kwargs):
    envelope, offset, topic = pg_message(capture, token, **kwargs)
    coverage.observe_postgres_message(db, capture, envelope, offset, topic)
    db.commit()
    return reload_capture(db, capture.id)


def assert_code(exc, code):
    assert isinstance(exc.value, coverage.ReplicaCoverageError)
    assert exc.value.code == code


# ---------------------------------------------------------------------------
# GTID：多 UUID / interval / 空 / 非法
# ---------------------------------------------------------------------------

def test_gtid_covers_multi_uuid_intervals_and_adjacent_merge():
    assert coverage.gtid_covers(f"{GTID_A}:1-3:5-7,{GTID_B}:1-2",
                                f"{GTID_A}:2-3:5-6,{GTID_B}:1-2")
    # 相邻/重叠 interval 先合并；目标 2-4 由合并后的 1-4 覆盖。
    assert coverage.gtid_covers(f"{GTID_A}:1-2:3-4", f"{GTID_A}:2-4")
    # 目标跨一个真实空洞（缺 4）时，不能由两个不相邻区间拼成覆盖。
    assert not coverage.gtid_covers(f"{GTID_A}:1-3:5-7", f"{GTID_A}:2-6")
    # 跨 UUID 不得互相顶替。
    assert not coverage.gtid_covers(f"{GTID_A}:1-9", f"{GTID_B}:1")
    assert not coverage.gtid_covers(f"{GTID_A}:1-9", f"{GTID_A}:1-10")


def test_gtid_covers_empty_and_whitespace():
    assert coverage.gtid_covers("", "")
    assert coverage.gtid_covers(f"\n{GTID_A}:1-2\n,{GTID_B}:3", f"{GTID_A}:2")
    assert not coverage.gtid_covers("", f"{GTID_A}:1")
    # 目标为空意味着没有 required transaction；completed 非法仍应早拒。
    with pytest.raises(coverage.ReplicaCoverageError) as exc:
        coverage.gtid_covers("not-a-gtid", "")
    assert_code(exc, "REPLICA_COVERAGE_INVALID")


@pytest.mark.parametrize("value", [
    None, 123, "not-a-uuid:1", f"{GTID_A}", f"{GTID_A}:",
    f"{GTID_A}:0-1", f"{GTID_A}:3-2", f"{GTID_A}:1-2-3",
    f"{GTID_A}:x", f"{GTID_A}:1,", f"{GTID_A}:1,,{GTID_B}:2",
])
def test_gtid_rejects_invalid_sets(value):
    with pytest.raises(coverage.ReplicaCoverageError) as exc:
        coverage.gtid_covers(value, f"{GTID_A}:1")
    assert_code(exc, "REPLICA_COVERAGE_INVALID")


def test_gtid_rejects_oversized_set():
    with pytest.raises(coverage.ReplicaCoverageError) as exc:
        coverage.gtid_covers("x" * (1024 * 1024 + 1), f"{GTID_A}:1")
    assert_code(exc, "REPLICA_COVERAGE_INVALID")


# ---------------------------------------------------------------------------
# MySQL：同前缀数值序 / offset<frontier / snapshot 不得自动充 proof
# ---------------------------------------------------------------------------

def test_mysql_binlog_same_prefix_numeric_order_and_position(db, admin_user):
    world = make_cohort(db, admin_user, committed_seq=7)
    token = token_of(db, world.capture)
    coverage.record_probe(db, world.capture.id, token,
                          {"dialect": "mysql", "file": "mysql-bin.000009",
                           "pos": 9999})
    db.commit()
    capture = reload_capture(db, world.capture.id)
    coverage.observe_mysql_offset(db, capture,
                                  {"file": "mysql-bin.000010", "pos": 4})
    db.commit()
    state = reload_capture(db, world.capture.id).coverage_probe
    assert state["pending"]["observed_seq"] == 7
    assert state["pending"]["observed_position"] == {
        "file": "mysql-bin.000010", "pos": 4}


def test_mysql_offset_below_frontier_does_not_observe(db, admin_user):
    world = make_cohort(db, admin_user, committed_seq=7)
    token = token_of(db, world.capture)
    coverage.record_probe(db, world.capture.id, token,
                          {"dialect": "mysql", "file": "mysql-bin.000010",
                           "pos": 100})
    db.commit()
    capture = reload_capture(db, world.capture.id)
    for offset in ({"file": "mysql-bin.000010", "pos": 99},
                   {"file": "mysql-bin.000009", "pos": 99999999}):
        coverage.observe_mysql_offset(db, capture, offset)
        db.commit()
        capture = reload_capture(db, world.capture.id)
        pending = capture.coverage_probe["pending"]
        assert "observed_seq" not in pending
    assert coverage.applied_probe(capture, runs_of(world)) is None


def test_mysql_different_file_prefix_is_not_ordered(db, admin_user):
    world = make_cohort(db, admin_user, committed_seq=3)
    token = token_of(db, world.capture)
    coverage.record_probe(db, world.capture.id, token,
                          {"dialect": "mysql", "file": "mysql-bin.000010",
                           "pos": 1})
    db.commit()
    capture = reload_capture(db, world.capture.id)
    coverage.observe_mysql_offset(db, capture,
                                  {"file": "binlog.999999", "pos": 999999})
    db.commit()
    assert "observed_seq" not in reload_capture(
        db, world.capture.id).coverage_probe["pending"]


def test_mysql_snapshot_source_offset_alone_is_not_stream_proof(db, admin_user):
    """ingest 负责只把完整 streaming 边界交给 observe_mysql_offset。

    模块不得从 capture.source_offset / snapshot offset 自动推导 cut；即使
    source_offset 已越过 frontier，没有显式 observe_mysql_offset 也不得
    observed/applied。
    """
    world = make_cohort(db, admin_user, committed_seq=9)
    token = token_of(db, world.capture)
    coverage.record_probe(db, world.capture.id, token,
                          {"dialect": "mysql", "file": "mysql-bin.000010",
                           "pos": 10})
    capture = reload_capture(db, world.capture.id)
    capture.source_offset = {"file": "mysql-bin.000010", "pos": 10,
                             "gtids": f"{GTID_A}:1-9", "snapshot": True}
    db.commit()
    capture = reload_capture(db, world.capture.id)
    assert "observed_seq" not in capture.coverage_probe["pending"]
    assert coverage.applied_probe(capture, runs_of(world)) is None


def test_mysql_gtid_covers_and_race_with_sql_frontier(db, admin_user):
    world = make_cohort(db, admin_user, committed_seq=11)
    token = token_of(db, world.capture)
    # SQL frontier 响应晚于 offset 持久化：先存 completed offset，再 record。
    coverage.observe_mysql_offset(db, world.capture,
                                  {"file": "mysql-bin.000010", "pos": 50,
                                   "gtids": f"{GTID_A}:1-5"})
    db.commit()
    capture = reload_capture(db, world.capture.id)
    coverage.record_probe(db, world.capture.id, token,
                          {"dialect": "mysql", "gtids": f"{GTID_A}:1-5"})
    db.commit()
    pending = reload_capture(db, world.capture.id).coverage_probe["pending"]
    assert pending["observed_seq"] == 11
    assert pending["observed_position"]["gtids"] == f"{GTID_A}:1-5"


def test_mysql_gtid_incomplete_or_invalid_is_rejected(db, admin_user):
    world = make_cohort(db, admin_user, committed_seq=11)
    token = token_of(db, world.capture)
    coverage.record_probe(db, world.capture.id, token,
                          {"dialect": "mysql", "gtids": f"{GTID_A}:1-5"})
    db.commit()
    capture = reload_capture(db, world.capture.id)
    coverage.observe_mysql_offset(db, capture,
                                  {"file": "mysql-bin.000010", "pos": 50,
                                   "gtids": f"{GTID_A}:1-4"})
    db.commit()
    assert "observed_seq" not in reload_capture(
        db, world.capture.id).coverage_probe["pending"]
    db.rollback()
    capture = reload_capture(db, world.capture.id)
    with pytest.raises(coverage.ReplicaCoverageError) as exc:
        coverage.observe_mysql_offset(db, capture,
                                      {"file": "mysql-bin.000010", "pos": 50,
                                       "gtids": "not-a-gtid"})
    assert_code(exc, "REPLICA_COVERAGE_INVALID")


def test_mysql_malformed_position_rejected(db, admin_user):
    world = make_cohort(db, admin_user)
    capture = reload_capture(db, world.capture.id)
    for offset in ({"file": "mysql-bin", "pos": 1},
                   {"file": "mysql-bin.000001", "pos": -1},
                   {"file": "mysql-bin.000001", "pos": 0},
                   {"file": "mysql-bin.000001"}):
        with pytest.raises(coverage.ReplicaCoverageError) as exc:
            coverage.observe_mysql_offset(db, capture, offset)
        assert_code(exc, "REPLICA_COVERAGE_INVALID")


# ---------------------------------------------------------------------------
# PG：token/prefix/topic/position/dup/race SQL response loss
# ---------------------------------------------------------------------------

def test_pg_message_observes_without_sql_response(db, admin_user):
    """SQL 返回丢失：只持久化了 pending，消息本身即 source-issued proof。"""
    world = make_cohort(db, admin_user, committed_seq=4)
    token = token_of(db, world.capture)
    capture = apply_message(db, reload_capture(db, world.capture.id), token)
    pending = capture.coverage_probe["pending"]
    assert pending["frontier"] == {"dialect": "postgresql", "lsn": 900}
    assert pending["observed_seq"] == 4
    assert pending["observed_position"] == {"lsn": 900}


def test_pg_message_with_recorded_frontier_observes(db, admin_user):
    world = make_cohort(db, admin_user, committed_seq=3)
    token = token_of(db, world.capture)
    coverage.record_probe(db, world.capture.id, token,
                          {"dialect": "postgresql", "lsn": 900})
    db.commit()
    capture = apply_message(db, reload_capture(db, world.capture.id), token)
    assert capture.coverage_probe["pending"]["observed_seq"] == 3


def test_pg_message_lsn_is_authoritative_over_advisory_frontier(db, admin_user):
    """SQL 采样 frontier 只是 advisory；实际消息 LSN 是 source-issued cut。"""
    world = make_cohort(db, admin_user, committed_seq=6)
    token = token_of(db, world.capture)
    coverage.record_probe(db, world.capture.id, token,
                          {"dialect": "postgresql", "lsn": 800})
    db.commit()
    capture = apply_message(db, reload_capture(db, world.capture.id), token, lsn=900)
    pending = capture.coverage_probe["pending"]
    assert pending["frontier"] == {"dialect": "postgresql", "lsn": 900}
    assert pending["observed_seq"] == 6
    assert pending["observed_position"] == {"lsn": 900}


def test_pg_late_sql_frontier_does_not_override_first_observed(db, admin_user):
    world = make_cohort(db, admin_user, committed_seq=6)
    token = token_of(db, world.capture)
    capture = apply_message(db, reload_capture(db, world.capture.id), token, lsn=900)
    # SQL 响应晚到（或丢失后重试的采样）不得改写 first observed cut。
    coverage.record_probe(db, world.capture.id, token,
                          {"dialect": "postgresql", "lsn": 800})
    db.commit()
    pending = reload_capture(db, world.capture.id).coverage_probe["pending"]
    assert pending["frontier"] == {"dialect": "postgresql", "lsn": 900}
    assert pending["observed_position"] == {"lsn": 900}


def test_pg_message_wrong_prefix_or_token_ignored(db, admin_user):
    world = make_cohort(db, admin_user)
    token = token_of(db, world.capture)
    capture = reload_capture(db, world.capture.id)
    # 其他 capture 的 prefix 直接忽略。
    coverage.observe_postgres_message(
        db, capture, *pg_message(capture, token, prefix="oo_replica_cut_" + "0" * 32))
    db.commit()
    capture = reload_capture(db, world.capture.id)
    assert "observed_seq" not in capture.coverage_probe["pending"]
    # 本 capture prefix 但 token 不匹配：忽略，不误发布。
    coverage.observe_postgres_message(
        db, capture, *pg_message(capture, token,
                                 content=base64.b64encode(b"other").decode()))
    db.commit()
    capture = reload_capture(db, world.capture.id)
    assert "observed_seq" not in capture.coverage_probe["pending"]


@pytest.mark.parametrize("kwargs,code", [
    ({"topic": "wrong.topic"}, "REPLICA_COVERAGE_INVALID"),
    ({"lsn": 900, "lsn_proc": 899}, "REPLICA_COVERAGE_INVALID"),
    ({"lsn": 900, "lsn_commit": 899}, "REPLICA_COVERAGE_INVALID"),
    ({"lsn": 900, "message_type": "COMMIT"}, "REPLICA_COVERAGE_INVALID"),
    ({"lsn": 900, "source_lsn": 899}, "REPLICA_COVERAGE_INVALID"),
    ({"lsn": 900, "transaction": {"id": "1"}}, "REPLICA_COVERAGE_INVALID"),
    ({"lsn": 0}, "REPLICA_COVERAGE_INVALID"),
    ({"lsn": -1}, "REPLICA_COVERAGE_INVALID"),
    ({"lsn": True}, "REPLICA_COVERAGE_INVALID"),
    ({"content": "not-base64!"}, "REPLICA_COVERAGE_INVALID"),
    ({"content": base64.b64encode(b"\xff").decode()}, "REPLICA_COVERAGE_INVALID"),
])
def test_pg_message_invalid_shape_rejected(db, admin_user, kwargs, code):
    world = make_cohort(db, admin_user)
    token = token_of(db, world.capture)
    capture = reload_capture(db, world.capture.id)
    envelope, offset, topic = pg_message(capture, token, **kwargs)
    with pytest.raises(coverage.ReplicaCoverageError) as exc:
        coverage.observe_postgres_message(db, capture, envelope, offset, topic)
    assert_code(exc, code)


def test_pg_message_duplicate_is_idempotent(db, admin_user):
    world = make_cohort(db, admin_user, committed_seq=5)
    token = token_of(db, world.capture)
    capture = apply_message(db, reload_capture(db, world.capture.id), token)
    first = capture.coverage_probe["pending"]["observed_seq"]
    capture = apply_message(db, reload_capture(db, world.capture.id), token)
    assert capture.coverage_probe["pending"]["observed_seq"] == first
    assert len(capture.coverage_probe["pending"]["observed_position"]) == 1


def test_pg_same_token_changed_lsn_retry_is_ignored(db, admin_user):
    world = make_cohort(db, admin_user, committed_seq=5)
    token = token_of(db, world.capture)
    capture = apply_message(db, reload_capture(db, world.capture.id), token, lsn=900)
    assert capture.coverage_probe["pending"]["observed_position"] == {"lsn": 900}
    # emit 重试可能插入第二条 WAL 消息；同 token、不同 LSN 不得覆盖 first cut，
    # 也不得让整批失败/阻塞。
    capture = apply_message(db, reload_capture(db, world.capture.id), token, lsn=901)
    assert capture.coverage_probe["pending"]["observed_position"] == {"lsn": 900}
    assert capture.coverage_probe["pending"]["observed_seq"] == 5


def test_pg_generation_mismatch_ignored_and_not_applied(db, admin_user):
    world = make_cohort(db, admin_user, committed_seq=5)
    token = token_of(db, world.capture)
    capture = reload_capture(db, world.capture.id)
    capture.generation = 2
    db.commit()
    capture = reload_capture(db, world.capture.id)
    coverage.observe_postgres_message(db, capture, *pg_message(capture, token))
    db.commit()
    capture = reload_capture(db, world.capture.id)
    assert "observed_seq" not in capture.coverage_probe["pending"]
    assert coverage.applied_probe(capture, runs_of(world)) is None


# ---------------------------------------------------------------------------
# applied all-member gate / 保守 timestamp / replay
# ---------------------------------------------------------------------------

def test_applied_requires_every_passed_member_at_observed_seq(db, admin_user):
    world = make_cohort(db, admin_user, run_statuses=("catching_up", "catching_up"),
                        applied_seqs=(5, 5), committed_seq=5)
    token = token_of(db, world.capture)
    capture = apply_message(db, reload_capture(db, world.capture.id), token)
    pending = capture.coverage_probe["pending"]

    # 先验证 gate：成员落后 / loading / 空 runs 都不得消费 pending。
    world.members[1].run.applied_position = {"commit_seq": 4}
    assert coverage.applied_probe(capture, runs_of(world)) is None
    world.members[1].run.status = "loading"
    assert coverage.applied_probe(capture, runs_of(world)) is None
    assert coverage.applied_probe(capture, []) is None
    # 全部成员到齐后才确认一次。
    world.members[1].run.status = "catching_up"
    world.members[1].run.applied_position = {"commit_seq": 5}
    confirmed = coverage.applied_probe(capture, runs_of(world))
    assert confirmed == {
        "capture_id": capture.id, "generation": capture.generation,
        "id": pending["id"], "commit_seq": 5,
        "confirmed_at": pending["requested_at"],
        "position": pending["observed_position"]}
    # 保守 coverage 时间：requested_at，绝不是 observe 时刻。
    assert confirmed["confirmed_at"] == pending["requested_at"]
    assert confirmed["confirmed_at"] != pending["observed_at"]


def test_applied_rejects_subset_of_capture_members(db, admin_user):
    world = make_cohort(db, admin_user, run_statuses=("catching_up", "catching_up"),
                        applied_seqs=(5, 0), committed_seq=5)
    token = token_of(db, world.capture)
    capture = apply_message(db, reload_capture(db, world.capture.id), token)
    # 只传第一个成员：frozen 成员集合不完整，绝不确认。
    assert coverage.applied_probe(capture, runs_of(world)[:1]) is None


def test_applied_confirmation_is_single_pending_and_respects_interval(db, admin_user):
    world = make_cohort(db, admin_user, applied_seqs=(5,), committed_seq=5)
    token = token_of(db, world.capture)
    capture = apply_message(db, reload_capture(db, world.capture.id), token)
    assert coverage.applied_probe(capture, runs_of(world)) is not None
    db.commit()
    capture = reload_capture(db, world.capture.id)
    assert "pending" not in capture.coverage_probe
    assert capture.coverage_probe["confirmed"]["commit_seq"] == 5
    # confirmed 后 10s 内不再建 probe。
    assert coverage.prepare_probe(db, capture.id) is None
    # 把 confirmed_at 推到窗口外 -> 允许新 probe。
    state = dict(capture.coverage_probe)
    state["confirmed"] = dict(state["confirmed"])
    state["confirmed"]["confirmed_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=11)).isoformat()
    capture.coverage_probe = state
    db.commit()
    capture = reload_capture(db, world.capture.id)
    next_probe = coverage.prepare_probe(db, capture.id)
    assert next_probe is not None
    assert next_probe["id"] != token


def test_replay_after_confirmation_does_not_recreate_probe(db, admin_user):
    world = make_cohort(db, admin_user, applied_seqs=(5,), committed_seq=5)
    token = token_of(db, world.capture)
    capture = apply_message(db, reload_capture(db, world.capture.id), token)
    assert coverage.applied_probe(capture, runs_of(world)) is not None
    db.commit()
    capture = reload_capture(db, world.capture.id)
    coverage.observe_postgres_message(db, capture, *pg_message(capture, token))
    db.commit()
    capture = reload_capture(db, world.capture.id)
    assert "pending" not in capture.coverage_probe
    assert capture.coverage_probe["confirmed"]["id"] == token


# ---------------------------------------------------------------------------
# bounded state / 回收 / 生命周期
# ---------------------------------------------------------------------------

def test_prepare_probe_requires_nonloading_runs_and_accepting_capture(db, admin_user):
    loading = make_cohort(db, admin_user, run_statuses=("loading",), applied_seqs=(0,))
    assert coverage.prepare_probe(db, loading.capture.id) is None
    blocked = make_cohort(db, admin_user, capture_status="blocked")
    assert coverage.prepare_probe(db, blocked.capture.id) is None
    empty = make_cohort(db, admin_user, run_statuses=())
    assert coverage.prepare_probe(db, empty.capture.id) is None
    assert coverage.prepare_probe(db, "missing") is None


def test_prepare_probe_does_not_overlap_and_state_stays_bounded(db, admin_user):
    world = make_cohort(db, admin_user, committed_seq=5)
    first = coverage.prepare_probe(db, world.capture.id)
    second = coverage.prepare_probe(db, world.capture.id)
    assert first["id"] == second["id"]
    capture = reload_capture(db, world.capture.id)
    assert set(capture.coverage_probe) == {"pending"}
    assert len(json.dumps(capture.coverage_probe)) < 4096
    coverage.record_probe(db, world.capture.id, first["id"],
                          {"dialect": "postgresql", "lsn": 900})
    db.commit()
    # frontier 已记录但未 observe：当前模块没有超时/回收，下一次 probe 返回 None。
    assert coverage.prepare_probe(db, world.capture.id) is None
    capture = reload_capture(db, world.capture.id)
    assert set(capture.coverage_probe) == {"pending"}
    assert set(capture.coverage_probe["pending"]) >= {"id", "frontier"}


def test_repeated_mysql_offsets_do_not_accumulate_state(db, admin_user):
    world = make_cohort(db, admin_user, committed_seq=5)
    token = token_of(db, world.capture)
    coverage.record_probe(db, world.capture.id, token,
                          {"dialect": "mysql", "file": "mysql-bin.000010",
                           "pos": 100})
    db.commit()
    capture = reload_capture(db, world.capture.id)
    for pos in range(1, 20):
        coverage.observe_mysql_offset(db, capture,
                                      {"file": "mysql-bin.000010", "pos": pos})
        db.commit()
        capture = reload_capture(db, world.capture.id)
    state = capture.coverage_probe
    assert set(state) == {"pending", "mysql_completed_offset"}
    assert len(state["mysql_completed_offset"]) == 2
    assert "observed_seq" not in state["pending"]


def test_record_probe_token_and_generation_mismatch_ignored(db, admin_user):
    world = make_cohort(db, admin_user)
    token = token_of(db, world.capture)
    coverage.record_probe(db, world.capture.id, "wrong-token",
                          {"dialect": "postgresql", "lsn": 900})
    db.commit()
    capture = reload_capture(db, world.capture.id)
    assert "frontier" not in capture.coverage_probe["pending"]
    capture.generation = 2
    db.commit()
    capture = reload_capture(db, world.capture.id)
    coverage.record_probe(db, world.capture.id, token,
                          {"dialect": "postgresql", "lsn": 900})
    db.commit()
    assert "frontier" not in reload_capture(
        db, world.capture.id).coverage_probe["pending"]


def test_record_probe_rejects_bad_dialect(db, admin_user):
    world = make_cohort(db, admin_user)
    token = token_of(db, world.capture)
    with pytest.raises(coverage.ReplicaCoverageError) as exc:
        coverage.record_probe(db, world.capture.id, token, {"dialect": "oracle"})
    assert_code(exc, "REPLICA_COVERAGE_INVALID")


def test_record_probe_rejects_pg_frontier_without_lsn(db, admin_user):
    world = make_cohort(db, admin_user)
    token = token_of(db, world.capture)
    with pytest.raises(coverage.ReplicaCoverageError) as exc:
        coverage.record_probe(db, world.capture.id, token,
                              {"dialect": "postgresql"})
    assert_code(exc, "REPLICA_COVERAGE_INVALID")


def test_record_probe_rejects_mysql_frontier_without_coordinate(db, admin_user):
    world = make_cohort(db, admin_user)
    token = token_of(db, world.capture)
    with pytest.raises(coverage.ReplicaCoverageError) as exc:
        coverage.record_probe(db, world.capture.id, token, {"dialect": "mysql"})
    assert_code(exc, "REPLICA_COVERAGE_INVALID")


# ---------------------------------------------------------------------------
# 模块边界反例（全部为硬断言，不允许 xfail）
# ---------------------------------------------------------------------------

def test_prepare_probe_after_generation_change_is_not_stuck(db, admin_user):
    world = make_cohort(db, admin_user, committed_seq=1)
    first = coverage.prepare_probe(db, world.capture.id)
    assert first["generation"] == 1
    db.commit()
    capture = reload_capture(db, world.capture.id)
    capture.generation = 2
    db.commit()
    second = coverage.prepare_probe(db, world.capture.id)
    assert second is not None
    assert second["generation"] == 2
    assert second["id"] != first["id"]


def test_applied_rejects_foreign_capture_run_cursor(db, admin_user):
    world = make_cohort(db, admin_user, run_statuses=("catching_up", "catching_up"),
                        applied_seqs=(5, 5), committed_seq=5)
    token = token_of(db, world.capture)
    capture = apply_message(db, reload_capture(db, world.capture.id), token)
    world.members[1].run.capture_id = "other-capture"
    with pytest.raises(coverage.ReplicaCoverageError) as exc:
        coverage.applied_probe(capture, runs_of(world))
    assert_code(exc, "REPLICA_COVERAGE_INVALID")


def test_pg_non_dict_message_fails_closed(db, admin_user):
    world = make_cohort(db, admin_user)
    token = token_of(db, world.capture)
    capture = reload_capture(db, world.capture.id)
    with pytest.raises(coverage.ReplicaCoverageError) as exc:
        coverage.observe_postgres_message(
            db, capture, {"payload": {"message": "not-a-dict"}},
            {"lsn": 900, "lsn_proc": 900, "lsn_commit": 900,
             "messageType": "MESSAGE"},
            "oo_replica_" + uuid.UUID(capture.id).hex + ".message")
    assert_code(exc, "REPLICA_COVERAGE_INVALID")


@pytest.mark.parametrize("envelope", [
    {"payload": "not-a-dict"},
    {"payload": {"message": {"prefix": "x", "content": ""},
                 "source": "not-a-dict"}},
])
def test_pg_malformed_payload_or_source_fails_closed(db, admin_user, envelope):
    world = make_cohort(db, admin_user)
    token = token_of(db, world.capture)
    capture = reload_capture(db, world.capture.id)
    payload = envelope.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("message"), dict):
        payload["message"]["prefix"] = coverage.prefix(capture.id)
        payload["message"]["content"] = base64.b64encode(
            token.encode("ascii")).decode("ascii")
    with pytest.raises(coverage.ReplicaCoverageError) as exc:
        coverage.observe_postgres_message(
            db, capture, envelope,
            {"lsn": 900, "lsn_proc": 900, "lsn_commit": 900,
             "messageType": "MESSAGE"},
            "oo_replica_" + uuid.UUID(capture.id).hex + ".message")
    assert_code(exc, "REPLICA_COVERAGE_INVALID")


def test_mysql_late_frontier_does_not_override_observed(db, admin_user):
    world = make_cohort(db, admin_user, committed_seq=5)
    token = token_of(db, world.capture)
    first = {"dialect": "mysql", "file": "mysql-bin.000010", "pos": 100}
    coverage.record_probe(db, world.capture.id, token, first)
    db.commit()
    capture = reload_capture(db, world.capture.id)
    coverage.observe_mysql_offset(db, capture,
                                  {"file": "mysql-bin.000010", "pos": 100})
    db.commit()
    capture = reload_capture(db, world.capture.id)
    assert capture.coverage_probe["pending"]["observed_seq"] == 5
    coverage.record_probe(db, world.capture.id, token,
                          {"dialect": "mysql", "file": "mysql-bin.000010",
                           "pos": 200})
    db.commit()
    pending = reload_capture(db, world.capture.id).coverage_probe["pending"]
    assert pending["frontier"] == first
    assert pending["observed_position"] == {
        "file": "mysql-bin.000010", "pos": 100}


def test_mysql_completed_offset_does_not_regress(db, admin_user):
    world = make_cohort(db, admin_user, committed_seq=5)
    token = token_of(db, world.capture)
    coverage.record_probe(db, world.capture.id, token,
                          {"dialect": "mysql", "file": "mysql-bin.000010",
                           "pos": 100})
    db.commit()
    capture = reload_capture(db, world.capture.id)
    coverage.observe_mysql_offset(db, capture,
                                  {"file": "mysql-bin.000010", "pos": 200})
    db.commit()
    capture = reload_capture(db, world.capture.id)
    coverage.observe_mysql_offset(db, capture,
                                  {"file": "mysql-bin.000010", "pos": 50})
    db.commit()
    state = reload_capture(db, world.capture.id).coverage_probe
    assert state["mysql_completed_offset"]["pos"] == 200


@pytest.mark.parametrize("kwargs", [
    {"lsn": 1, "lsn_proc": True},
    {"lsn": 1, "lsn_commit": True},
    {"lsn": 1, "source_lsn": True},
])
def test_pg_three_lsn_positions_reject_bool(db, admin_user, kwargs):
    world = make_cohort(db, admin_user)
    token = token_of(db, world.capture)
    capture = reload_capture(db, world.capture.id)
    envelope, offset, topic = pg_message(capture, token, **kwargs)
    with pytest.raises(coverage.ReplicaCoverageError) as exc:
        coverage.observe_postgres_message(db, capture, envelope, offset, topic)
    assert_code(exc, "REPLICA_COVERAGE_INVALID")


def test_prepare_probe_malformed_confirmed_at_fails_closed(db, admin_user):
    world = make_cohort(db, admin_user)
    capture = reload_capture(db, world.capture.id)
    capture.coverage_probe = {"confirmed": {"confirmed_at": "not-a-timestamp"}}
    db.commit()
    with pytest.raises(coverage.ReplicaCoverageError) as exc:
        coverage.prepare_probe(db, world.capture.id)
    assert_code(exc, "REPLICA_COVERAGE_INVALID")


def test_applied_bool_commit_seq_is_not_proof(db, admin_user):
    world = make_cohort(db, admin_user, committed_seq=1, applied_seqs=(1,))
    token = token_of(db, world.capture)
    capture = apply_message(db, reload_capture(db, world.capture.id), token)
    world.members[0].run.applied_position = {"commit_seq": True}
    with pytest.raises(coverage.ReplicaCoverageError) as exc:
        coverage.applied_probe(capture, runs_of(world))
    assert_code(exc, "REPLICA_COVERAGE_INVALID")


def test_module_never_commits_caller_transaction(db, admin_user):
    """Lock-release contract: prepare/record flush only; caller commits early."""
    world = make_cohort(db, admin_user, committed_seq=1)
    probe = coverage.prepare_probe(db, world.capture.id)
    assert probe is not None
    assert db.in_transaction()  # module must not commit the caller's transaction
    db.commit()
    assert not db.in_transaction()
    coverage.record_probe(db, world.capture.id, probe["id"],
                          {"dialect": "postgresql", "lsn": 900})
    assert db.in_transaction()
    db.commit()


def test_separate_sessions_release_capture_lock_between_steps(db, admin_user):
    """Producer -> receiver -> executor in independent sessions, no nested locks."""
    from sqlalchemy.orm import sessionmaker

    world = make_cohort(db, admin_user, committed_seq=3,
                        applied_seqs=(3,))
    Session = sessionmaker(bind=db.get_bind())
    producer = Session()
    try:
        probe = coverage.prepare_probe(producer, world.capture.id)
        assert probe is not None
        producer.commit()
        token = probe["id"]
    finally:
        producer.close()

    receiver = Session()
    try:
        capture = receiver.get(IntegratedReplicaCapture, world.capture.id)
        coverage.record_probe(receiver, world.capture.id, token,
                              {"dialect": "postgresql", "lsn": 900})
        receiver.commit()
        capture = receiver.get(IntegratedReplicaCapture, world.capture.id)
        coverage.observe_postgres_message(
            receiver, capture, *pg_message(capture, token, lsn=900))
        receiver.commit()
    finally:
        receiver.close()

    executor = Session()
    try:
        capture = executor.get(IntegratedReplicaCapture, world.capture.id)
        runs = list(executor.scalars(sa.select(IntegratedReplicaRun).where(
            IntegratedReplicaRun.capture_id == world.capture.id)))
        confirmed = coverage.applied_probe(capture, runs)
        assert confirmed is not None and confirmed["commit_seq"] == 3
        executor.commit()
    finally:
        executor.close()


def test_applied_empty_iterator_is_not_confirmation(db, admin_user):
    world = make_cohort(db, admin_user, committed_seq=1, applied_seqs=(1,))
    token = token_of(db, world.capture)
    capture = apply_message(db, reload_capture(db, world.capture.id), token)
    with pytest.raises(coverage.ReplicaCoverageError) as exc:
        coverage.applied_probe(capture, iter(()))
    assert_code(exc, "REPLICA_COVERAGE_INVALID")


# ---------------------------------------------------------------------------
# 冻结成员 / 独立 generation / deadline / flush 收敛
# ---------------------------------------------------------------------------

def test_applied_detached_excluded_and_run_generation_is_independent(db, admin_user):
    """detached 成员排除；run.generation 与 capture.generation 不可等值校验。"""
    world = make_cohort(db, admin_user, run_statuses=("catching_up", "catching_up"),
                        applied_seqs=(5, 0), committed_seq=5)
    token = token_of(db, world.capture)
    capture = apply_message(db, reload_capture(db, world.capture.id), token)
    tables = [dict(item) for item in capture.tables_json]
    tables[1]["detached"] = True
    capture.tables_json = tables
    db.commit()
    capture = reload_capture(db, world.capture.id)
    # run 的 generation 是资产重建次数，与 capture cohort generation 不同也合法。
    world.members[0].run.generation = 99
    confirmed = coverage.applied_probe(capture, [world.members[0].run])
    assert confirmed is not None and confirmed["commit_seq"] == 5


def test_applied_non_detached_missing_member_never_confirms(db, admin_user):
    world = make_cohort(db, admin_user, run_statuses=("catching_up", "catching_up"),
                        applied_seqs=(5, 5), committed_seq=5)
    token = token_of(db, world.capture)
    capture = apply_message(db, reload_capture(db, world.capture.id), token)
    assert coverage.applied_probe(capture, [world.members[0].run]) is None


def test_applied_extra_detached_run_is_ignored(db, admin_user):
    world = make_cohort(db, admin_user, run_statuses=("catching_up", "catching_up"),
                        applied_seqs=(5, 0), committed_seq=5)
    token = token_of(db, world.capture)
    capture = apply_message(db, reload_capture(db, world.capture.id), token)
    tables = [dict(item) for item in capture.tables_json]
    tables[1]["detached"] = True
    capture.tables_json = tables
    db.commit()
    capture = reload_capture(db, world.capture.id)
    # runtime 的 live_runs 在过渡期仍可能包含已 detached 的终态旧成员；它必须
    # 被排除而不是阻塞或冒充未完成成员。
    confirmed = coverage.applied_probe(capture, runs_of(world))
    assert confirmed is not None and confirmed["commit_seq"] == 5


def test_pending_deadline_new_token_and_late_old_ignored(db, admin_user):
    world = make_cohort(db, admin_user, committed_seq=5)
    first = coverage.prepare_probe(db, world.capture.id)
    capture = reload_capture(db, world.capture.id)
    state = dict(capture.coverage_probe)
    state["pending"] = dict(state["pending"])
    state["pending"]["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    capture.coverage_probe = state
    db.commit()
    second = coverage.prepare_probe(db, world.capture.id)
    assert second is not None and second["id"] != first["id"]
    capture = reload_capture(db, world.capture.id)
    # 旧 token 的迟到消息不得 observe 新 pending。
    coverage.observe_postgres_message(db, capture, *pg_message(capture, first["id"]))
    db.commit()
    capture = reload_capture(db, world.capture.id)
    assert "observed_seq" not in capture.coverage_probe["pending"]
    # 新 token 正常 observe。
    capture = apply_message(db, capture, second["id"])
    assert capture.coverage_probe["pending"]["observed_seq"] == 5


def test_observed_pending_survives_deadline_and_confirms(db, admin_user):
    world = make_cohort(db, admin_user, committed_seq=5, applied_seqs=(5,))
    token = token_of(db, world.capture)
    capture = apply_message(db, reload_capture(db, world.capture.id), token)
    state = dict(capture.coverage_probe)
    state["pending"] = dict(state["pending"])
    state["pending"]["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    capture.coverage_probe = state
    db.commit()
    capture = reload_capture(db, world.capture.id)
    # observed cut 不因 deadline 被丢弃/替换；仍可 applied。
    assert coverage.prepare_probe(db, world.capture.id) is None
    assert coverage.applied_probe(capture, runs_of(world)) is not None


def test_mysql_completed_offset_updates_flush_only_on_change(db, admin_user):
    world = make_cohort(db, admin_user)
    capture = reload_capture(db, world.capture.id)
    flushes = []

    def before_flush(*_):
        flushes.append(1)

    sa.event.listen(db, "before_flush", before_flush)
    try:
        coverage.observe_mysql_offset(db, capture,
                                      {"file": "mysql-bin.000010", "pos": 100})
        assert len(flushes) == 1
        coverage.observe_mysql_offset(db, capture,
                                      {"file": "mysql-bin.000010", "pos": 100})
        assert len(flushes) == 1
        coverage.observe_mysql_offset(db, capture,
                                      {"file": "mysql-bin.000010", "pos": 50})
        assert len(flushes) == 1
        coverage.observe_mysql_offset(db, capture,
                                      {"file": "mysql-bin.000010", "pos": 101})
        assert len(flushes) == 2
    finally:
        sa.event.remove(db, "before_flush", before_flush)


def test_mysql_different_prefix_after_stored_is_invalid(db, admin_user):
    world = make_cohort(db, admin_user)
    capture = reload_capture(db, world.capture.id)
    coverage.observe_mysql_offset(db, capture,
                                  {"file": "mysql-bin.000010", "pos": 100})
    db.commit()
    capture = reload_capture(db, world.capture.id)
    with pytest.raises(coverage.ReplicaCoverageError) as exc:
        coverage.observe_mysql_offset(db, capture,
                                      {"file": "binlog.000010", "pos": 100})
    assert_code(exc, "REPLICA_COVERAGE_INVALID")


def test_prepare_probe_accepts_independent_run_generation(db, admin_user):
    """run.generation 是资产重建次数；capture.generation 是连接 cohort 次数。

    二者不相等是合法状态，prepare_probe 不得把它当作 generation 漂移拒绝。
    """
    world = make_cohort(db, admin_user, committed_seq=1, applied_seqs=(1,))
    world.members[0].run.generation = 99
    db.commit()
    capture = reload_capture(db, world.capture.id)
    probe = coverage.prepare_probe(db, capture.id)
    assert probe is not None
    assert probe["generation"] == capture.generation == 1
    capture = apply_message(db, capture, probe["id"])
    assert coverage.applied_probe(capture, runs_of(world)) is not None
