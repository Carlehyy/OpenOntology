"""集成 CDC 事件落地 — 接收端事务内的唯一入口。

``ingest(db, connection_id, generation, envelope)`` 不自行提交：接收端在
事实 + outbox + source 观测（位置/心跳）同事务落库成功后才回 2xx；任何
失败先回滚本事务，再以独立短事务经 ``cdc_control.report_source``（代际
 fenced）持久化健康错误 — 绝不提交部分事实，也绝不假成功。

安全与语义基线：

- source 行 FOR UPDATE 锁定后校验：generation 必须与注册代一致（过代 =
  显式拒绝，安全停连接器，不改写新代状态）、status ∈ {active, starting}、
  连接 active；心跳/事务元数据等非行事件同样先过这套校验再确认，杜绝
  过期接收进程伪造健康证据；
- 每张源表可有多个 active 订阅（不同映射/本体）：同一事务内逐个
  capture；订阅必须锚定在当前代（``source_generation``）且带
  ``activation_position``（无前沿 = 拒绝，fail closed）；事件坐标低于前沿
  的订阅按 stale 跳过；无任何可投递订阅的事件按 skipped 确认（表已下线
  不得拖死整个连接器）；
- MySQL binlog file/pos/row 是全序：last_position 不大于事件坐标时按
  duplicate 确认（事实/发件箱修剪后重放仍幂等）。PG 行 WAL LSN 与提交
  输出顺序可交错：绝不做 ``<= last_position`` 去重，重放幂等只依赖
  source_event_id 唯一约束（保留期内），last_position 仅作观测推进；
- 事件身份哈希含 connection_id + generation 盐：重指向/换代后同坐标同主
  键不会误碰撞；绝不掺时间戳/正文内容；
- 物理列 → 本体属性经订阅冻结的 field_map（键必须是物理 source_key）；
  列集合/类型/scale/可空性对照订阅冻结 contract 校验（漂移即拒绝）；
  只保留映射属性与安全源坐标，整行源数据绝不落库；
- TOAST/不可用占位、快照 READ、schema 漂移全部确定性拒绝并落健康错误；
  错误码封闭集合，不回显源值。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data_channel.integrated.cdc_control import (
    SOURCE_ACTIVE, SOURCE_ERROR, SOURCE_PAUSED, SOURCE_STARTING,
    SUBSCRIPTION_ACTIVE, report_source,
)
from app.data_channel.integrated.cdc_envelope import (
    DecodedEvent, EnvelopeError, UNAVAILABLE, activation_order,
    column_contract, decode_envelope, event_group_id,
)
from app.data_channel.integrated.models import (
    IntegratedCdcSource, IntegratedCdcSubscription,
)
from app.shared.exact_json import dumps_exact

STATUS_CAPTURED = "captured"
STATUS_DUPLICATE = "duplicate"
STATUS_STALE = "stale"
STATUS_HEARTBEAT = "heartbeat"
STATUS_CONTROL = "control"
STATUS_SKIPPED = "skipped"
STATUS_CAPACITY = "capacity_exceeded"

# 接收端可以安全提交并回 2xx 的状态（事实与 outbox 已同事务落库）。
ACK_STATUSES = frozenset({
    STATUS_CAPTURED, STATUS_DUPLICATE, STATUS_STALE,
    STATUS_HEARTBEAT, STATUS_CONTROL, STATUS_SKIPPED,
})

# source 准入闸：error 属可恢复的瞬态观测（无证据/子进程退出/DB 抖动），
# 必须放行事件才能由新证据恢复；paused（含 schema 漂移/容量）、gap（日志
# 缺口，永久）、stopped 与过代一律拒绝。
_SOURCE_ADMITTED_STATUSES = frozenset({
    SOURCE_ACTIVE, SOURCE_STARTING, SOURCE_ERROR,
})

ERROR_CAPACITY_EXCEEDED = "EVENT_CAPACITY_EXCEEDED"

# 生命周期类拒绝（控制面主动停/换代/断连）：属预期而非故障，接收端不得
# 把这类拒绝回报成 error 去覆盖控制面的 desired 状态。
_LIFECYCLE_CODES = frozenset({
    "SOURCE_MISSING", "OBSOLETE_GENERATION", "SOURCE_NOT_ACTIVE",
    "CONNECTION_INACTIVE",
})


class CdcPermanentError(Exception):
    """确定性拒绝：重试不可能成功，连接器应停止。

    ``code`` 封闭集合；``message`` 仅诊断用，绝不含源行值。生命周期类拒
    绝不落 error 状态（``report=False``）。
    """

    def __init__(self, code: str, message: str, *, report: bool = True):
        super().__init__(message)
        self.code = code
        self.message = message
        self.report = report and code not in _LIFECYCLE_CODES

    @property
    def http_status(self) -> int:
        return 409


class CdcTransientError(Exception):
    """暂时性失败：503 重试，不丢事件；source 落健康错误。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def ingest(db: Session, connection_id: str, generation: int,
           envelope: dict) -> dict:
    """在调用方事务内落地一条 CDC 信封；返回封闭状态集合。"""
    connection_id, generation = str(connection_id), int(generation)
    try:
        event = decode_envelope(envelope)
    except EnvelopeError as exc:
        raise CdcPermanentError(exc.code, exc.message) from exc

    source = _verify_source(db, connection_id, generation)
    if event.kind != "change":
        status = STATUS_HEARTBEAT if event.kind == "heartbeat" else STATUS_CONTROL
        if event.kind == "heartbeat":
            # 心跳是最新的在跑证据：刷新 heartbeat_at（观测状态原样透传，
            # 状态恢复由 executor 依据接收证据裁决）。
            _report_observed(db, source, generation)
        return _result(status, event, None)

    subscriptions = _deliverable_subscriptions(
        db, connection_id, generation, event)
    if not subscriptions:
        return _result(STATUS_SKIPPED, event, None)

    stored = _stored_position(source)
    if (event.coordinates.dialect == "mysql" and stored is not None
            and event.coordinates.order <= stored):
        return _result(STATUS_DUPLICATE, event, None)

    deliverable = [
        subscription for subscription in subscriptions
        if event.coordinates.order >= _activation_frontier(subscription)]
    if not deliverable:
        return _result(STATUS_STALE, event, None)

    payloads = []
    for subscription in deliverable:
        mapped = _frozen_mapping(subscription)
        _reject_drift(event, mapped)
        _validate_contract(subscription, mapped, event)
        change = _build_change(
            event, subscription, mapped, connection_id, generation)
        payloads.append(_event_payload(
            subscription, event, change, connection_id, generation))

    statuses = _capture_many(db, payloads)
    if STATUS_CAPACITY in statuses:
        # 容量超限：不推进位置、不刷新事件时间 — 接收端回滚本事件并把全
        # 部 source 置 paused 后 503 重试（事件不丢）。
        return _result(STATUS_CAPACITY, event, None)
    if STATUS_CAPTURED in statuses:
        outcome = STATUS_CAPTURED
    elif STATUS_DUPLICATE in statuses:
        outcome = STATUS_DUPLICATE
    else:
        outcome = STATUS_STALE

    _report_observed(
        db, source, generation,
        position=(dict(event.coordinates.position)
                  if outcome == STATUS_CAPTURED else None),
        last_event_at=datetime.now(timezone.utc))
    return _result(outcome, event, None)


def _report_observed(db: Session, source: IntegratedCdcSource, generation: int,
                     *, position: dict | None = None,
                     last_event_at: datetime | None = None) -> None:
    """成功路径的观测回报：只推进心跳/位置/事件时间，不改写状态与错误码。

    处于 error 的 source 被放行后，其错误码保持原样 — 是否恢复为 active
    由 executor 根据接收端证据（last_accepted_at）统一裁决，这里不越权。
    """
    report_source(
        db, str(source.connection_id), generation=generation,
        status=str(source.status), error_code=source.last_error_code,
        position=position, last_event_at=last_event_at)


# ---------------------------------------------------------------------------
# source / 订阅校验
# ---------------------------------------------------------------------------

def _verify_source(db: Session, connection_id: str,
                   generation: int) -> IntegratedCdcSource:
    source = db.execute(
        select(IntegratedCdcSource)
        .where(IntegratedCdcSource.connection_id == connection_id)
        .with_for_update()
    ).scalar_one_or_none()
    if source is None:
        raise CdcPermanentError(
            "SOURCE_MISSING", "cdc source is not registered")
    if int(source.generation) != generation:
        raise CdcPermanentError(
            "OBSOLETE_GENERATION",
            "connector generation no longer matches the receiver registration")
    if str(source.status) not in _SOURCE_ADMITTED_STATUSES:
        raise CdcPermanentError(
            "SOURCE_NOT_ACTIVE", "cdc source is not accepting events")
    from app.data_channel.connections.models import Connection

    connection = db.get(Connection, connection_id)
    if connection is None or str(connection.status) != "active":
        raise CdcPermanentError(
            "CONNECTION_INACTIVE", "managed connection is not active")
    return source


def _deliverable_subscriptions(db: Session, connection_id: str, generation: int,
                               event: DecodedEvent) -> list:
    subscriptions = db.execute(
        select(IntegratedCdcSubscription).where(
            IntegratedCdcSubscription.connection_id == connection_id,
            IntegratedCdcSubscription.schema_name == event.schema_name,
            IntegratedCdcSubscription.table_name == event.table_name,
            IntegratedCdcSubscription.status == SUBSCRIPTION_ACTIVE,
        )
    ).scalars().all()
    # 订阅必须锚定在当前代：旧代订阅的激活前沿对新代事件无意义，跳过而
    # 非用过期前沿投递（表下线/待重锚不拖死整个连接器）。
    return [subscription for subscription in subscriptions
            if int(getattr(subscription, "source_generation", 0) or 0)
            == generation]


def _activation_frontier(subscription: IntegratedCdcSubscription) -> tuple:
    activation = subscription.activation_position
    if activation is None:
        raise CdcPermanentError(
            "SUBSCRIPTION_NOT_ACTIVATED",
            "subscription has no activation position; refusing history")
    try:
        return activation_order(activation)
    except EnvelopeError as exc:
        raise CdcPermanentError(exc.code, exc.message) from exc


def _stored_position(source: IntegratedCdcSource) -> tuple | None:
    stored = source.last_position
    if stored is None:
        return None
    try:
        return activation_order(stored)
    except EnvelopeError as exc:
        raise CdcPermanentError(
            "SOURCE_POSITION_INVALID", exc.message) from exc


# ---------------------------------------------------------------------------
# 冻结映射与契约 → 本体事件
# ---------------------------------------------------------------------------

def _frozen_mapping(subscription: IntegratedCdcSubscription) -> dict:
    field_map = subscription.field_map
    pk_columns = subscription.primary_key_columns
    if (not isinstance(field_map, dict) or not field_map
            or not isinstance(pk_columns, list) or not pk_columns):
        raise CdcPermanentError(
            "SUBSCRIPTION_CONFIG_INVALID",
            "frozen subscription mapping is malformed")
    columns: dict[str, str] = {}
    for column, prop in field_map.items():
        if not isinstance(column, str) or not isinstance(prop, str) or not prop:
            raise CdcPermanentError(
                "SUBSCRIPTION_CONFIG_INVALID",
                "frozen field_map must map physical columns to property names")
        columns[column] = prop
    if len(set(columns.values())) != len(columns):
        raise CdcPermanentError(
            "SUBSCRIPTION_CONFIG_INVALID",
            "frozen field_map maps one property from multiple columns")
    keys = [str(column) for column in pk_columns
            if isinstance(column, str) and column]
    if len(keys) != len(pk_columns) or not keys:
        raise CdcPermanentError(
            "SUBSCRIPTION_CONFIG_INVALID", "frozen primary key is malformed")
    return {"columns": columns, "primary_key": keys}


def _reject_drift(event: DecodedEvent, mapped: dict) -> None:
    """冻结列存在性：权威像必须携带全部冻结列 — 事实快照要完整行
    （PG 以 replica identity FULL 为前置），局部像一律拒绝而非臆造省略。"""
    required = set(mapped["columns"]) | set(mapped["primary_key"])
    present = set(event.after_column_schemas if event.after is not None
                  else event.before_column_schemas)
    missing = sorted(required - present)
    if missing:
        raise CdcPermanentError(
            "SCHEMA_DRIFT", "frozen columns are absent from the event schema")


def _validate_contract(subscription: IntegratedCdcSubscription, mapped: dict,
                       event: DecodedEvent) -> None:
    """冻结列契约比对：类型/.scale/可空性漂移即拒绝（fail closed）。

    可空性只对带 after 像的事件校验：部分 before 形态（replica identity
    DEFAULT）的列 optional 标记不可作为可空性事实。
    """
    contract = getattr(subscription, "contract", None)
    columns = contract.get("columns") if isinstance(contract, dict) else None
    if not isinstance(columns, list):
        return
    frozen: dict[str, dict] = {}
    for entry in columns:
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            frozen[entry["name"]] = entry
    schemas = event.authoritative_schemas
    for column in sorted(set(mapped["columns"]) | set(mapped["primary_key"])):
        expected = frozen.get(column)
        schema = schemas.get(column)
        if expected is None or schema is None:
            continue  # 契约未覆盖的列只做存在性检查（_reject_drift）
        try:
            actual = column_contract(schema)
        except EnvelopeError as exc:
            raise CdcPermanentError(exc.code, exc.message) from exc
        problems = []
        if str(expected.get("type") or "") != actual["type"]:
            problems.append("type")
        scale = expected.get("scale")
        if (scale is not None and actual["scale"] is not None
                and int(scale) != actual["scale"]):
            problems.append("scale")
        nullable = expected.get("nullable")
        if (nullable is not None and event.after is not None
                and bool(nullable) != actual["nullable"]):
            problems.append("nullability")
        if problems:
            raise CdcPermanentError(
                "SCHEMA_DRIFT", f"frozen contract mismatch on {problems}")


def _build_change(event: DecodedEvent,
                  subscription: IntegratedCdcSubscription, mapped: dict,
                  connection_id: str, generation: int) -> dict:
    columns, pk_columns = mapped["columns"], mapped["primary_key"]
    before = event.before or {}
    after = event.after or {}
    image = after if event.op in {"c", "u"} else before

    properties: dict[str, Any] = {}
    for column, prop in columns.items():
        value = image[column]
        if value is UNAVAILABLE:
            raise CdcPermanentError(
                "VALUE_UNAVAILABLE",
                "unavailable TOAST placeholder on a mapped column")
        properties[prop] = value

    pk: dict[str, Any] = {}
    for column in pk_columns:
        value = image.get(column)
        if value is UNAVAILABLE:
            raise CdcPermanentError(
                "VALUE_UNAVAILABLE",
                "unavailable TOAST placeholder on a primary key column")
        if value is None:
            raise CdcPermanentError(
                "PRIMARY_KEY_INVALID", "primary key column is null")
        pk[column] = value

    changes: dict[str, dict] = {}
    if event.op in {"c", "d"}:
        for prop, value in properties.items():
            changes[prop] = ({"old": None, "new": value} if event.op == "c"
                             else {"old": value, "new": None})
    else:
        for column, prop in columns.items():
            old = before.get(column)
            new = after.get(column)
            if old is UNAVAILABLE or new is UNAVAILABLE:
                raise CdcPermanentError(
                    "VALUE_UNAVAILABLE",
                    "unavailable TOAST placeholder on a mapped column")
            if dumps_exact(old) != dumps_exact(new):
                changes[prop] = {"old": old, "new": new}

    # 行身份命名空间 = 订阅冻结本体身份 + 连接/代：换连接或换代（重指向）
    # 后同物理主键不误碰撞；"remote:" 前缀与本地实例空间隔离。
    scope = ":".join((
        str(subscription.ontology_id), str(subscription.object_type_id),
        str(subscription.asset_id), str(connection_id),
        str(generation))).encode("utf-8")
    return {
        "instance_id": "remote:" + _hash_pairs(
            scope, [(column, pk[column]) for column in pk_columns]),
        "properties": properties,
        "changes": changes,
        "pk": pk,
    }


def _hash_pairs(scope: bytes, pairs: list[tuple[str, Any]]) -> str:
    digest = hashlib.sha256()
    digest.update(scope)
    for key, value in pairs:
        digest.update(b"\x00")
        digest.update(key.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(dumps_exact(value).encode("utf-8"))
    return digest.hexdigest()


def _event_payload(subscription: IntegratedCdcSubscription,
                   event: DecodedEvent, change: dict, connection_id: str,
                   generation: int) -> dict:
    source_event_id = event.event_id(
        change["pk"], connection_id=connection_id, generation=generation)
    return {
        "ontology_id": str(subscription.ontology_id),
        "release_id": str(subscription.release_id),
        "object_type_id": str(subscription.object_type_id),
        "mapping_id": str(subscription.mapping_id),
        "asset_id": str(subscription.asset_id),
        "instance_id": change["instance_id"],
        "source_event_id": source_event_id,
        "event_group_id": event_group_id(
            ontology_id=str(subscription.ontology_id),
            release_id=str(subscription.release_id),
            mapping_id=str(subscription.mapping_id),
            object_type_id=str(subscription.object_type_id),
            source_event_id=source_event_id),
        "change_kind": str(event.change_kind),
        "changes": change["changes"],
        "snapshot": {
            "id": change["instance_id"],
            "objectTypeId": str(subscription.object_type_id),
            "properties": change["properties"],
            "computed": {},
            "sourcePosition": dict(event.coordinates.position),
        },
        "occurred_at": event.occurred_at,
    }


def _capture_many(db: Session, payloads: list[dict]) -> list[str]:
    """一次批量写入（端口内预锁全部本体 project，避免逐条取锁的锁序问题）。"""
    from app.data_channel.integrated import cdc_port

    try:
        port = cdc_port.get_cdc_ontology_port()
        results = port.capture_many(db, payloads)
    except cdc_port.CdcOntologyPortError as exc:
        raise CdcTransientError(
            "CAPTURE_PORT_UNAVAILABLE", exc.message) from exc
    except Exception as exc:  # noqa: BLE001 - 端口异常不回显源值
        raise CdcTransientError(
            "CAPTURE_FAILED", "ontology capture rejected the event") from exc
    if not isinstance(results, list) or len(results) != len(payloads):
        raise CdcTransientError(
            "CAPTURE_RESULT_INVALID",
            "ontology capture returned an unusable result shape")
    statuses = []
    for result in results:
        status = result.get("status") if isinstance(result, dict) else None
        if status not in cdc_port.CAPTURE_STATUSES:
            raise CdcTransientError(
                "CAPTURE_RESULT_INVALID",
                "ontology capture returned an unusable status")
        statuses.append(str(status))
    return statuses


def _result(status: str, event: DecodedEvent, instance_id: str | None) -> dict:
    return {
        "status": status,
        "change_kind": event.change_kind,
        "instance_id": instance_id,
    }


# ---------------------------------------------------------------------------
# 健康落库（接收端在回滚主事务后的独立短事务调用）
# ---------------------------------------------------------------------------

def record_source_error(db: Session, connection_id: str, code: str,
                        *, generation: int, permanent: bool) -> None:
    """观测错误回报（代际 fenced）：旧代迟到错误不改写新代状态。

    确定性拒绝（schema 漂移/契约违例/不可用占位等）落 ``paused``：必须
    人工处置，准入闸不再放行；瞬态失败落 ``error``：准入闸继续放行，
    由后续成功证据恢复。
    """
    report_source(
        db, str(connection_id), generation=int(generation),
        status=SOURCE_PAUSED if permanent else SOURCE_ERROR,
        error_code=str(code)[:80])


def mark_all_sources_paused(db: Session,
                            code: str = ERROR_CAPACITY_EXCEEDED) -> int:
    paused = 0
    for source in db.execute(select(IntegratedCdcSource)).scalars():
        report_source(
            db, str(source.connection_id), generation=int(source.generation),
            status=SOURCE_PAUSED, error_code=str(code)[:80])
        paused += 1
    return paused
