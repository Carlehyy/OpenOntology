"""CDC 控制面：期望订阅 reconcile、运行时观测回报与安全生命周期钩子。

纯控制面模块：不做任何源端 IO（不连 MySQL/PostgreSQL、不探测复制槽），
不自行 commit（调用方持有事务）。

加锁序（冻结，防 ABBA）：凡修改源状态的路径都先取 ``IntegratedCdcSource``
行锁（``SELECT ... FOR UPDATE``，代际比较在锁内完成 = generation CAS），
再触碰订阅行；本模块绝不对 ``ontology_projects`` 等项目行加锁（接收侧的
序是 源行锁 → 项目 FOR KEY SHARE，控制面只取源行锁即与之相容）。

职责划分（冻结契约）：

- ``reconcile(db)``：从不可变已发布远程映射（经 ``cdc_port`` 适配器）推导
  期望订阅全集并与存量行做差分。发布 promote/rollback/delete 都体现为期望
  全集变化，由运行时周期性（约 2s）调用本函数收敛，无需脆弱的发布钩子。
- 运行时读：``desired_sources``（应维持流式消费的源）、``cleanup_sources``
  （应停流并清理复制槽后确认的源）、``desired_subscriptions``。
- 运行时回报：``report_source``（观测状态 + 心跳）、``activate_subscription``
  （锚定覆盖前沿）、``confirm_subscriptions_stopped``（停流确认）。
- 生命周期钩子：连接凭据轮换（仅凭据轮换：同代暂停等待重测；物理重定向
  在未观测到停止+槽清理确认前结构化 409 拒绝）、连接/资产受控注销的
  CDC 阻塞项与 STOPPED 控制行的安全清理。
- 状态读模型：``asset_status`` 输出前端契约（禁用/心跳过期必须如实不健康）。

安全不变式：明文连接配置只在进程内瞬时解密用于单向摘要，绝不落库、绝不上
日志、绝不进入 API 响应；物理身份摘要排除凭据与订阅表集合。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.data_channel.integrated.cdc_port import CdcOntologyPortError, get_cdc_ontology_port
from app.data_channel.connections.models import Connection
from app.data_channel.integrated.contracts import fail
from app.data_channel.integrated.models import (
    IntegratedAsset,
    IntegratedCdcSource,
    IntegratedCdcSubscription,
)

# ── 状态词表（冻结） ────────────────────────────────────────────────

SOURCE_STARTING = "starting"
SOURCE_ACTIVE = "active"
SOURCE_PAUSED = "paused"
SOURCE_ERROR = "error"
SOURCE_GAP = "gap"
SOURCE_STOPPED = "stopped"
SOURCE_STATUSES = (SOURCE_STARTING, SOURCE_ACTIVE, SOURCE_PAUSED,
                   SOURCE_ERROR, SOURCE_GAP, SOURCE_STOPPED)

SUBSCRIPTION_STARTING = "starting"
SUBSCRIPTION_ACTIVE = "active"
SUBSCRIPTION_SUPERSEDED = "superseded"
SUBSCRIPTION_STOPPED = "stopped"
SUBSCRIPTION_STATUSES = (SUBSCRIPTION_STARTING, SUBSCRIPTION_ACTIVE,
                         SUBSCRIPTION_SUPERSEDED, SUBSCRIPTION_STOPPED)
# 期望存续（运行时应在其计划内）的订阅状态。
SUBSCRIPTION_DESIRED_STATUSES = (SUBSCRIPTION_STARTING, SUBSCRIPTION_ACTIVE)

# 运行时约 2s 一拍；容许数拍抖动后即判过期，状态读模型据此如实降健康。
HEARTBEAT_STALE_AFTER_SECONDS = 15
# 心跳来自未来（时钟漂移之外）同样不可信：只容许小幅正向偏差。
HEARTBEAT_FUTURE_SKEW_SECONDS = 5

# 期望订阅条目（cdc_port 协议）必填键与形状。
_DESIRED_REQUIRED_KEYS = (
    "ontology_id", "release_id", "mapping_id", "object_type_id",
    "asset_id", "connection_id", "schema_name", "table_name",
    "schema_fingerprint",
)
# 同键内容漂移时需要整列采纳的订阅字段（activation_position 除外——
# 它只在显式新覆盖转换时重锚，绝不随内容更新被静默改写）。
_SUBSCRIPTION_CONTENT_KEYS = (
    "asset_id", "connection_id", "object_type_id", "field_map",
    "primary_key_columns", "contract", "schema_name", "table_name",
    "schema_fingerprint", "logical_fingerprint",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return _aware(value).isoformat() if value is not None else None


def _dormant(source: IntegratedCdcSource) -> bool:
    """休眠源：观测到停止 *且* 复制槽清理已确认（无槽可泄漏）。"""
    return (source.status == SOURCE_STOPPED
            and source.cleanup_completed_at is not None)


def _locked_source(db: Session, connection_id: str) -> IntegratedCdcSource | None:
    """取源行锁并越过身份映射读到当前行（锁内比较代际 = generation CAS）。"""
    return db.scalars(
        sa.select(IntegratedCdcSource)
        .where(IntegratedCdcSource.connection_id == connection_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()


# ── 物理身份摘要 ────────────────────────────────────────────────────

def _decrypted_config(config: dict) -> dict:
    """进程内瞬时解密；失败 fail-closed，绝不把密文/明文带出本函数。"""
    raw = dict(config or {})
    encrypted = raw.get("_encrypted")
    if not encrypted:
        return raw
    from app.shared.encryption import decrypt

    try:
        return json.loads(decrypt(encrypted))
    except Exception as exc:  # noqa: BLE001 - 解不开即拒绝，不猜测身份
        raise ValueError("source config unavailable") from exc


def source_identity_from_config(kind: str, config: dict) -> str:
    """物理端点身份的单向摘要（host/port/database + 连接种类）。

    排除凭据（用户/口令）与订阅表集合：仅凭据轮换身份不变（同代保留偏移），
    重定向到不同端点身份改变（新覆盖代际、丢弃旧偏移）。连接串形态取 URL
    的 host/port/database 投影。摘要之外不产生任何输出，不写日志。
    """
    config = _decrypted_config(config)
    kind = str(kind or "")
    if config.get("connection_string"):
        from sqlalchemy.engine import make_url

        try:
            url = make_url(str(config["connection_string"]))
        except Exception as exc:  # noqa: BLE001 - 坏连接串无法确定身份
            raise ValueError("invalid connection_string for identity") from exc
        material = {"kind": kind, "host": url.host or "",
                    "port": str(url.port or ""), "database": url.database or ""}
    else:
        material = {"kind": kind, "host": str(config.get("host") or ""),
                    "port": str(config.get("port") or ""),
                    "database": str(config.get("database") or "")}
    return hashlib.sha256(json.dumps(
        material, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def source_identity(connection: Connection) -> str:
    """连接行形态的物理身份摘要（见 :func:`source_identity_from_config`）。"""
    return source_identity_from_config(
        getattr(connection, "kind", ""), connection.config or {})


# ── 期望订阅 reconcile ──────────────────────────────────────────────

def _validated_desired(entries) -> dict[tuple[str, str, str], dict]:
    """校验适配器输出并按唯一键索引；非法/重复即抛 ValueError（fail-closed）。"""
    desired: dict[tuple[str, str, str], dict] = {}
    for raw in entries or []:
        if not isinstance(raw, dict):
            raise ValueError("cdc desired subscription entry must be a dict")
        entry = dict(raw)
        for key in _DESIRED_REQUIRED_KEYS:
            value = entry.get(key)
            if not isinstance(value, str) or not value:
                raise ValueError(f"cdc desired subscription missing {key}")
        field_map = entry.get("field_map")
        if not isinstance(field_map, dict) or not field_map:
            raise ValueError("cdc desired subscription requires a physical field_map")
        for column, prop in field_map.items():
            if (not isinstance(column, str) or not column
                    or not isinstance(prop, str) or not prop):
                raise ValueError(
                    "cdc desired field_map must map physical columns to property names")
        primary = entry.get("primary_key_columns")
        if (not isinstance(primary, list) or not primary
                or not all(isinstance(name, str) and name for name in primary)):
            raise ValueError("cdc desired subscription requires physical primary_key_columns")
        contract = entry.get("contract")
        if (not isinstance(contract, dict)
                or not isinstance(contract.get("columns"), list)
                or not isinstance(contract.get("object_properties"), list)):
            raise ValueError("cdc desired subscription requires a frozen contract")
        fingerprint = entry.get("logical_fingerprint")
        if fingerprint is not None and (not isinstance(fingerprint, str) or not fingerprint):
            raise ValueError(
                "cdc desired logical_fingerprint must be a non-empty string or None")
        entry["primary_key_columns"] = list(primary)
        entry["field_map"] = dict(field_map)
        entry["contract"] = dict(contract)
        key = (entry["ontology_id"], entry["release_id"], entry["mapping_id"])
        if key in desired:
            raise ValueError("duplicate cdc desired subscription key")
        desired[key] = entry
    return desired


def _reanchor(row: IntegratedCdcSubscription, generation: int) -> None:
    """显式新覆盖：清空激活前沿与覆盖起点，挂到当前代际等待运行时重锚。"""
    row.activation_position = None
    row.activated_at = None
    row.status = SUBSCRIPTION_STARTING
    row.source_generation = generation
    row.last_error_code = None


def reconcile(db: Session) -> dict:
    """差分收敛期望订阅；无源端 IO、无自身 commit、无项目行锁。

    覆盖语义：
    - 新期望（新发布/新映射）：创建 ``starting`` 订阅，锚定交由运行时
      ``activate_subscription``（显式 no-data 覆盖起点）。
    - 期望存续且内容一致：不动（重启/表集合变化均保留偏移与激活前沿）。
    - 期望存续但曾离开计划（superseded/stopped，回滚复活）或内容漂移：
      采纳期望并重锚（``activation_position=None`` + ``starting``）——
      覆盖缺口只能显式开新覆盖，绝不静默续接旧前沿。
    - 期望消失：``superseded``，等待运行时停流确认（``stopped``）；已停流
      确认且不再期望的订阅是纯控制行，直接回收，旧发布订阅不会永久滞留。
    - 源已停止 *且槽清理已确认* 时再次出现期望：显式新覆盖代际
      （generation+1、丢弃 last_position）。停止但清理未确认时保持
      cleanup-pending：不升代、不放行激活，待运行时确认清理后的下一拍收敛；
      同代静默重置偏移被禁止。
    """
    port = get_cdc_ontology_port()
    desired = _validated_desired(port.subscriptions(db))

    summary: dict = {
        "desired": 0, "created": [], "replanned": [], "reactivated": [],
        "superseded": [], "removed": [], "skipped": [],
        "sources_created": [], "epochs": [], "cleanup_pending": [],
    }

    # 平台库内的存在性校验（非源端 IO）：期望引用的资产/连接被注销时，
    # 跳过该条并如实计数，绝不因 FK 失败陷死 reconcile 循环。
    asset_ids = {entry["asset_id"] for entry in desired.values()}
    connection_ids = {entry["connection_id"] for entry in desired.values()}
    known_assets = set(db.scalars(sa.select(IntegratedAsset.id).where(
        IntegratedAsset.id.in_(asset_ids)))) if asset_ids else set()
    known_connections = {
        row.id: row for row in db.scalars(sa.select(Connection).where(
            Connection.id.in_(connection_ids)))} if connection_ids else {}
    usable: dict[tuple[str, str, str], dict] = {}
    for key, entry in desired.items():
        if entry["asset_id"] not in known_assets:
            reason = "missing_asset"
        elif entry["connection_id"] not in known_connections:
            reason = "missing_connection"
        else:
            usable[key] = entry
            continue
        summary["skipped"].append({
            "ontology_id": key[0], "release_id": key[1], "mapping_id": key[2],
            "asset_id": entry["asset_id"],
            "connection_id": entry["connection_id"], "reason": reason,
        })
    summary["desired"] = len(usable)

    # 源行锁先行（确定序防并发 reconcile 互锁）：代际/偏移决策在锁内完成，
    # 与运行时回报、接收侧的“源行锁优先”序一致。
    sources = {
        row.connection_id: row for row in db.scalars(
            sa.select(IntegratedCdcSource)
            .order_by(IntegratedCdcSource.connection_id)
            .with_for_update()
            .execution_options(populate_existing=True))}
    existing = {
        (row.ontology_id, row.release_id, row.mapping_id): row
        for row in db.scalars(
            sa.select(IntegratedCdcSubscription)
            .execution_options(populate_existing=True))}

    # 1) 期望消失：存续 → superseded（等运行时停流确认，控制面绝不代其
    #    宣告停止）；已停流确认 → 回收控制行。
    for key, row in existing.items():
        if key in usable:
            continue
        if row.status in SUBSCRIPTION_DESIRED_STATUSES:
            row.status = SUBSCRIPTION_SUPERSEDED
            row.last_error_code = None
            summary["superseded"].append(row.id)
        elif row.status == SUBSCRIPTION_STOPPED:
            db.delete(row)
            summary["removed"].append(row.id)

    # 2) 源行：缺则建；休眠源复燃 → 显式新覆盖代际；停止未确认清理 →
    #    cleanup-pending（不升代、不放行激活）。
    for connection_id in sorted({entry["connection_id"] for entry in usable.values()}):
        row = sources.get(connection_id)
        if row is None:
            try:
                identity = source_identity(known_connections[connection_id])
            except ValueError:
                identity = None
            row = IntegratedCdcSource(
                connection_id=connection_id, generation=0,
                status=SOURCE_STARTING, identity_fingerprint=identity)
            db.add(row)
            sources[connection_id] = row
            summary["sources_created"].append(connection_id)
        elif _dormant(row):
            row.generation = row.generation + 1
            row.last_position = None
            row.cleanup_completed_at = None
            row.last_error_code = None
            row.status = SOURCE_STARTING
            summary["epochs"].append({"connection_id": connection_id,
                                      "generation": row.generation})
        elif row.status == SOURCE_STOPPED:
            summary["cleanup_pending"].append(connection_id)

    # 3) 逐条收敛订阅行。
    for key, entry in usable.items():
        row = existing.get(key)
        generation = sources[entry["connection_id"]].generation
        if row is None:
            row = IntegratedCdcSubscription(
                id=str(uuid.uuid4()),
                connection_id=entry["connection_id"],
                asset_id=entry["asset_id"],
                ontology_id=entry["ontology_id"],
                release_id=entry["release_id"],
                mapping_id=entry["mapping_id"],
                object_type_id=entry["object_type_id"],
                field_map=entry["field_map"],
                primary_key_columns=entry["primary_key_columns"],
                contract=entry["contract"],
                schema_name=entry["schema_name"],
                table_name=entry["table_name"],
                schema_fingerprint=entry["schema_fingerprint"],
                logical_fingerprint=entry.get("logical_fingerprint"),
                activation_position=None,
                source_generation=generation,
                status=SUBSCRIPTION_STARTING)
            db.add(row)
            summary["created"].append(row.id)
            continue
        content_changed = any(
            getattr(row, field) != entry.get(field)
            for field in _SUBSCRIPTION_CONTENT_KEYS)
        left_plan = row.status in (SUBSCRIPTION_SUPERSEDED, SUBSCRIPTION_STOPPED)
        # 代际边界（休眠复燃 / 物理重定向）之前的前沿不属于当前流时间线。
        stale_generation = row.source_generation != generation
        if not (left_plan or content_changed or stale_generation):
            continue  # 存续一致：保留偏移/前沿，等待运行时锚定或继续流式
        for field in _SUBSCRIPTION_CONTENT_KEYS:
            setattr(row, field, entry.get(field))
        _reanchor(row, generation)
        summary["reactivated" if left_plan or stale_generation
                else "replanned"].append(row.id)
    return summary


# ── 运行时读接口 ────────────────────────────────────────────────────

def _desired_connection_ids(db: Session) -> set[str]:
    return set(db.scalars(
        sa.select(IntegratedCdcSubscription.connection_id).where(
            IntegratedCdcSubscription.status.in_(SUBSCRIPTION_DESIRED_STATUSES))
        .distinct()))


def desired_sources(db: Session) -> list[IntegratedCdcSource]:
    """运行时应当维持流式消费的源行；不解密任何连接配置。

    条件：存在 starting/active 订阅，且源未处于 paused（等待重测）或
    stopped（已停：休眠等待新代际，或 cleanup-pending 等待清理确认）。
    """
    connection_ids = _desired_connection_ids(db)
    if not connection_ids:
        return []
    return list(db.scalars(sa.select(IntegratedCdcSource).where(
        IntegratedCdcSource.connection_id.in_(connection_ids),
        IntegratedCdcSource.status.notin_((SOURCE_PAUSED, SOURCE_STOPPED)),
    ).order_by(IntegratedCdcSource.connection_id)))


def cleanup_sources(db: Session) -> list[IntegratedCdcSource]:
    """运行时必须停流并清理复制槽、随后确认的源行；不解密任何连接配置。

    两类：(1) 已无期望订阅且尚未休眠（未观测到停止+清理确认）的源；
    (2) 已停止但清理未确认的源（cleanup-pending，即使又出现期望订阅也要
    先清理确认，新代际才会放行）。确认方式：
    ``report_source(..., status="stopped", cleanup_completed=True)``。
    """
    connection_ids = _desired_connection_ids(db)
    rows = db.scalars(sa.select(IntegratedCdcSource).order_by(
        IntegratedCdcSource.connection_id)).all()
    return [
        row for row in rows
        if not _dormant(row) and (
            row.status == SOURCE_STOPPED
            or row.connection_id not in connection_ids)
    ]


def desired_subscriptions(db: Session, connection_id: str) -> list[IntegratedCdcSubscription]:
    """某一连接上期望存续（starting/active）的订阅行，按创建序。"""
    return list(db.scalars(sa.select(IntegratedCdcSubscription).where(
        IntegratedCdcSubscription.connection_id == connection_id,
        IntegratedCdcSubscription.status.in_(SUBSCRIPTION_DESIRED_STATUSES),
    ).order_by(IntegratedCdcSubscription.created_at, IntegratedCdcSubscription.id)))


# ── 运行时回报接口 ──────────────────────────────────────────────────

def report_source(db: Session, connection_id: str, *, generation: int,
                  status: str, error_code: str | None = None,
                  position: dict | None = None,
                  last_event_at: datetime | None = None,
                  cleanup_completed: bool = False) -> IntegratedCdcSource | None:
    """运行时观测回报（心跳 + 状态）；不 commit。

    源行锁内做代际比较（generation CAS）：回报代际与当前行不一致即整体
    no-op 并返回 None——旧代进程既不能复活已丢弃的偏移，也不能刷新心跳
    把新代运行时伪装成健康。代际一致时 ``heartbeat_at`` 无条件刷新（含无
    活跃订阅的就绪心跳），使状态读模型能如实区分“在跑但无事件”与“停摆”。

    ``status="stopped"`` 是代际终态：仅在为清理而主动停流时回报（优雅重启
    保留槽与偏移，不应回报 stopped）。``cleanup_completed=True`` 记录槽清理
    确认时刻——它是连接注销/重定向/新代际放行的唯一观测证据；同时该连接
    上等待停流确认的 superseded 订阅随整流停止一并确认。
    """
    if status not in SOURCE_STATUSES:
        raise ValueError(f"unknown cdc source status: {status}")
    row = _locked_source(db, connection_id)
    if row is None or int(generation) != row.generation:
        return None
    row.heartbeat_at = _now()
    row.status = status
    row.last_error_code = error_code
    if position is not None:
        row.last_position = dict(position)
    if last_event_at is not None:
        row.last_event_at = last_event_at
    if cleanup_completed and status == SOURCE_STOPPED:
        row.cleanup_completed_at = _now()
        _confirm_stopped(db, connection_id)
    return row


def activate_subscription(db: Session, subscription_id: str, *,
                          position: dict) -> IntegratedCdcSubscription | None:
    """运行时锚定覆盖前沿（starting → active）并记录覆盖起点；不 commit。

    位置形状冻结：MySQL ``{"dialect":"mysql","file":str,"pos":int,"row":int}``、
    PostgreSQL ``{"dialect":"postgresql","lsn":int,"data_collection_order":int}``。
    拒绝激活（原样返回，不锚定）：订阅非 ``starting``；源缺失、paused 或
    stopped（含 cleanup-pending）；订阅挂的代际与源当前代际不一致。
    """
    if not isinstance(position, dict) or not position:
        raise ValueError("activation position must be a non-empty dict")
    connection_id = db.scalar(sa.select(IntegratedCdcSubscription.connection_id).where(
        IntegratedCdcSubscription.id == subscription_id))
    if connection_id is None:
        return None
    source = _locked_source(db, connection_id)
    row = db.scalars(
        sa.select(IntegratedCdcSubscription)
        .where(IntegratedCdcSubscription.id == subscription_id)
        .with_for_update()
        .execution_options(populate_existing=True)).first()
    if row is None:
        return None
    if (row.status != SUBSCRIPTION_STARTING or source is None
            or source.status in (SOURCE_PAUSED, SOURCE_STOPPED)
            or row.source_generation != source.generation):
        return row
    row.activation_position = dict(position)
    row.activated_at = _now()
    row.status = SUBSCRIPTION_ACTIVE
    return row


def _confirm_stopped(db: Session, connection_id: str,
                      subscription_ids: list[str] | None = None) -> int:
    statement = sa.select(IntegratedCdcSubscription).where(
        IntegratedCdcSubscription.connection_id == connection_id,
        IntegratedCdcSubscription.status == SUBSCRIPTION_SUPERSEDED,
    ).execution_options(populate_existing=True)
    if subscription_ids is not None:
        ids = [str(value) for value in subscription_ids]
        statement = statement.where(IntegratedCdcSubscription.id.in_(ids))
    rows = list(db.scalars(statement))
    for row in rows:
        row.status = SUBSCRIPTION_STOPPED
    return len(rows)


def confirm_subscriptions_stopped(db: Session, connection_id: str,
                                  subscription_ids: list[str] | None = None) -> int:
    """运行时停流确认：该连接上 superseded 订阅 → stopped；不 commit。

    运行时在子流真正停止 / 加宽后的新计划生效后调用；可传 ``subscription_ids``
    只确认其中一部分。只触碰 ``superseded`` 行：新的期望订阅
    （starting/active）绝不被误停，已确认的 stopped 幂等跳过。确认后的订阅
    成为纯控制行，可被 reconcile / 注销路径安全回收。
    """
    if _locked_source(db, connection_id) is None:
        return 0
    return _confirm_stopped(db, connection_id, subscription_ids)


# ── 连接生命周期钩子（凭据轮换 / 重测 / 受控注销） ─────────────────

def assert_repoint_allowed(db: Session, connection: Connection,
                           merged_config: dict) -> None:
    """物理重定向守卫：未观测到停止+槽清理确认前结构化 409 拒绝。

    旧端点的复制槽只能用旧配置清理；连接行一旦被新端点配置覆盖，旧凭据即
    不可再取，将造成旧源 WAL 泄漏。因此仅允许“休眠源”（stopped 且
    cleanup_completed_at 已确认）重定向；凭据轮换（身份不变）不受限。
    必须在连接配置被覆盖 *之前* 调用（merged_config 是候选新配置）。
    """
    row = _locked_source(db, connection.id)
    if row is None or _dormant(row):
        return
    new_identity = source_identity_from_config(connection.kind, merged_config)
    if new_identity == (row.identity_fingerprint or ""):
        return
    pending = db.scalar(sa.select(sa.func.count()).select_from(
        IntegratedCdcSubscription).where(
        IntegratedCdcSubscription.connection_id == connection.id,
        IntegratedCdcSubscription.status != SUBSCRIPTION_STOPPED)) or 0
    fail(409, "CDC_REPOINT_BLOCKED",
         "连接正承载 CDC 复制流：物理端点变更会让旧端点复制槽无法清理"
         "（旧凭据将被覆盖）；请先解除订阅并等待运行时停止且确认清理复制槽后再重定向",
         cdc={"status": row.status, "generation": row.generation,
              "pending_subscriptions": int(pending),
              "cleanup_completed_at": _iso(row.cleanup_completed_at)})


def note_connection_config_rotated(db: Session, connection: Connection) -> None:
    """轮换落库后的控制面收尾（连接行已携带新配置，未提交）；不 commit。

    调用前提：``assert_repoint_allowed`` 已放行（物理重定向仅休眠源可达）。
    - 身份不变（仅凭据）：同代暂停，偏移保留，等待 POST /connections/{id}/test
      成功后经 ``note_connection_retested`` 恢复 starting。
    - 身份变化（休眠源重定向）：新代际 + 丢弃旧偏移；保持 stopped/清理确认，
      复燃由 reconcile 以显式新覆盖代际接手。
    """
    row = _locked_source(db, connection.id)
    if row is None:
        return
    new_identity = source_identity(connection)
    if new_identity != (row.identity_fingerprint or ""):
        row.identity_fingerprint = new_identity
        row.generation = row.generation + 1
        row.last_position = None
    if row.status != SOURCE_STOPPED:
        row.status = SOURCE_PAUSED
        row.last_error_code = None


def note_connection_retested(db: Session, connection_id: str, *,
                             success: bool) -> None:
    """连接重测结果：成功才解除轮换暂停（同代恢复，偏移保留）；不 commit。"""
    if not success:
        return
    row = _locked_source(db, connection_id)
    if row is not None and row.status == SOURCE_PAUSED:
        row.status = SOURCE_STARTING
        row.last_error_code = None


def _connection_cdc_state(db: Session, connection_id: str) -> dict | None:
    row = _locked_source(db, connection_id)
    if row is None:
        return None
    subscriptions = list(db.scalars(sa.select(IntegratedCdcSubscription).where(
        IntegratedCdcSubscription.connection_id == connection_id,
    ).execution_options(populate_existing=True)))
    pending = [item for item in subscriptions
               if item.status != SUBSCRIPTION_STOPPED]
    return {
        "row": row,
        "subscriptions": subscriptions,
        "pending": pending,
        # 安全清理 = 观测到停止 + 槽清理确认 + 订阅全部停流确认。
        "safe_to_clean": _dormant(row) and not pending,
    }


def connection_blockers(db: Session, connection_id: str) -> dict | None:
    """连接注销的 CDC 阻塞项；None = 无 CDC 或可安全清理控制行。

    已停止但未确认清理复制槽的源同样阻塞：注销绝不留下无人清理的槽。
    """
    state = _connection_cdc_state(db, connection_id)
    if state is None or state["safe_to_clean"]:
        return None
    row = state["row"]
    return {
        "code": "CONNECTION_CDC_ACTIVE",
        "message": (
            f"连接仍承载 CDC 复制（状态 {row.status}，generation {row.generation}）；"
            "已停止但未确认清理复制槽或仍有未停流确认的订阅时不予注销，"
            "请先解除本体订阅并等待运行时停止且确认清理"),
        "cdc": {
            "status": row.status,
            "generation": row.generation,
            "heartbeat_at": _iso(row.heartbeat_at),
            "cleanup_completed_at": _iso(row.cleanup_completed_at),
            "subscriptions_total": len(state["subscriptions"]),
            "pending_subscriptions": len(state["pending"]),
            "subscriptions": [
                {"ontology_id": item.ontology_id, "release_id": item.release_id,
                 "mapping_id": item.mapping_id, "status": item.status}
                for item in state["pending"][:5]],
        },
    }


def connection_cleanup(db: Session, connection_id: str) -> int:
    """安全前提下的控制行清理（仅 STOPPED 订阅 + 源行）；不 commit。

    非安全状态返回 0 且不动任何行；调用方必须先以
    ``connection_blockers`` 判定。绝不做源端 IO（槽清理只能由运行时观测）。
    """
    state = _connection_cdc_state(db, connection_id)
    if state is None or not state["safe_to_clean"]:
        return 0
    for item in state["subscriptions"]:
        db.delete(item)
    db.delete(state["row"])
    # 控制行 FK RESTRICT 指向连接：先落删除，调用方随后才能删连接行。
    db.flush()
    return len(state["subscriptions"])


# ── 资产生命周期（注销阻塞 + 停流控制行清理） ──────────────────────

def asset_blockers(db: Session, asset_id: str) -> dict | None:
    """资产注销的 CDC 阻塞项；None = 无订阅或已全部停流确认。

    superseded（期望已消失、运行时尚未确认停流）仍阻塞：停流必须被观测到；
    确认后的 stopped 订阅是纯控制行，不阻塞且随注销回收。
    """
    pending = list(db.scalars(sa.select(IntegratedCdcSubscription).where(
        IntegratedCdcSubscription.asset_id == asset_id,
        IntegratedCdcSubscription.status != SUBSCRIPTION_STOPPED,
    ).order_by(IntegratedCdcSubscription.created_at, IntegratedCdcSubscription.id)
        .execution_options(populate_existing=True)))
    if not pending:
        return None
    return {
        "code": "ASSET_CDC_ACTIVE",
        "total": len(pending),
        "subscriptions": [
            {"ontology_id": item.ontology_id, "release_id": item.release_id,
             "mapping_id": item.mapping_id, "object_type_id": item.object_type_id,
             "status": item.status}
            for item in pending[:5]],
    }


def asset_cleanup(db: Session, asset_id: str) -> int:
    """删除该资产已停流确认（stopped）的订阅控制行；不 commit，无源端 IO。"""
    stopped = list(db.scalars(sa.select(IntegratedCdcSubscription).where(
        IntegratedCdcSubscription.asset_id == asset_id,
        IntegratedCdcSubscription.status == SUBSCRIPTION_STOPPED,
    ).execution_options(populate_existing=True)))
    for item in stopped:
        db.delete(item)
    if stopped:
        # 订阅 FK RESTRICT 指向资产：先落删除，调用方随后才能删资产行。
        db.flush()
    return len(stopped)


# ── 状态读模型（前端契约） ──────────────────────────────────────────

def heartbeat_fresh(source: IntegratedCdcSource, *,
                    at: datetime | None = None) -> bool:
    """心跳新鲜度：既不过期，也不来自超出容许漂移的未来。"""
    if source.heartbeat_at is None:
        return False
    age = _aware(at or _now()) - _aware(source.heartbeat_at)
    return (timedelta(seconds=-HEARTBEAT_FUTURE_SKEW_SECONDS) <= age
            <= timedelta(seconds=HEARTBEAT_STALE_AFTER_SECONDS))


def asset_status(db: Session, asset: IntegratedAsset) -> dict:
    """GET /{asset_id}/cdc 契约；禁用/心跳过期必须如实不健康。

    只反映 *当前期望* 的订阅（starting/active）：已被取代或已停流的历史
    控制行不算启用，也不出现在响应里，因此响应内的状态恒在源六态词表内。
    ``coverage_started_at`` 取已锚定订阅的最早真实激活时刻；尚未锚定
    （starting）不构成覆盖，全部未锚定时为 None。健康 = 源 active、无
    错误、心跳新鲜，且该资产全部当前订阅均已在当前代际锚定 active。
    不返回任何原始事件值、连接配置或凭据；仅控制面状态与标识 id。
    """
    subscriptions = list(db.scalars(sa.select(IntegratedCdcSubscription).where(
        IntegratedCdcSubscription.asset_id == asset.id,
        IntegratedCdcSubscription.status.in_(SUBSCRIPTION_DESIRED_STATUSES),
    ).order_by(IntegratedCdcSubscription.created_at, IntegratedCdcSubscription.id)
        .execution_options(populate_existing=True)))
    payload = {
        "asset_id": asset.id,
        "enabled": bool(subscriptions),
        "status": SOURCE_STOPPED,
        "healthy": False,
        "last_event_at": None,
        "last_error_code": None,
        "subscriptions": [
            {"ontology_id": item.ontology_id, "release_id": item.release_id,
             "mapping_id": item.mapping_id, "object_type_id": item.object_type_id,
             "status": item.status, "last_error_code": item.last_error_code}
            for item in subscriptions],
        "coverage_started_at": None,
    }
    if not subscriptions:
        return payload
    source = db.scalars(
        sa.select(IntegratedCdcSource)
        .where(IntegratedCdcSource.connection_id == asset.connection_id)
        .execution_options(populate_existing=True)).first()
    if source is None:
        return payload
    payload["status"] = source.status
    payload["last_event_at"] = source.last_event_at
    payload["last_error_code"] = source.last_error_code
    anchored = [
        item for item in subscriptions
        if item.status == SUBSCRIPTION_ACTIVE
        and item.source_generation == source.generation
        and item.activation_position is not None
        and item.activated_at is not None]
    if anchored:
        payload["coverage_started_at"] = min(
            _aware(item.activated_at) for item in anchored)
    # “无事件”绝不冒充“在跑”：心跳过期即使没有任何源事件也不健康。
    payload["healthy"] = (
        source.status == SOURCE_ACTIVE
        and source.last_error_code is None
        and heartbeat_fresh(source)
        and len(anchored) == len(subscriptions))
    return payload
