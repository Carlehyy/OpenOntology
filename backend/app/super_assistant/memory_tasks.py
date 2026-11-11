"""跨会话记忆的可靠同步任务（NATS handler + 对账 drain）。

消费侧入口 ``process_message(payload)``（executor 经 asyncio.to_thread 调
用，payload 仅 ``source_id``——正文留在数据库，防重投靠数据库栅栏）；
``drain_once()`` 由独立 APScheduler 每 5 秒调用，只做有界的 NATS 补派发
（丢件兜底 / 租约过期恢复 / 遗忘推进），不在调度线程内执行任何网络轮询。

并发安全两条铁律：

1. CAS 栅栏：worker 认领时铸造 ``lease_token``，此后一切状态转移都以
   ``UPDATE ... WHERE status=预期 AND lease_token=本 worker`` 完成；栅栏
   失败（0 行）说明行已被遗忘/其他 worker 接管，当前 worker 立即放弃，
   不会把 forgetting 覆写回 pending/synced（遗忘后复活）。
2. operation_id 先持久化再上网络：retain 提交 ACK 丢失后重试复用同一
   operation_id（上游幂等去重），只有轮询到明确 terminal failed 才清除
   并铸造新 id（新 generation）。

attempt 语义：仅由 ``_record_failure`` 递增（本 generation 的失败计数，
耗尽置 dead 人工重试）。认领不递增——配置等待、正常 pending operation
轮询续租都不消耗重试额度；瞬态不可用按失败计，不会无限 _defer。

事务纪律：每步独立短事务（SessionLocal），网络调用期间不持有事务。
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, update

from app.super_assistant.hindsight_client import (
    HindsightConflictError,
    HindsightNotFoundError,
    HindsightUnavailableError,
)
from app.super_assistant.memory_models import SuperAssistantMemorySource

logger = logging.getLogger(__name__)

_LEASE_SECONDS = 90
_MAX_ATTEMPTS = 5
_BACKOFF_BASE_SECONDS = 5.0
_BACKOFF_CAP_SECONDS = 300.0
_POLL_INTERVAL_SECONDS = 1.0
_POLL_HTTP_TIMEOUT_SECONDS = 8.0
_SUBMIT_TIMEOUT_SECONDS = 20.0
# 提交阶段（ensure_bank×2 + retain×1，各 ≤20s）的有界租约窗：forget_source
# 把该租约拷贝为遗忘等待窗，旧 worker 的在途 HTTP 最多截止在窗内
_SUBMIT_LEASE_SECONDS = 60
_DEFER_UNCONFIGURED_SECONDS = 300
_DISPATCH_STALE_SECONDS = 30
_DRAIN_DISPATCH_BATCH = 50
_DRAIN_STUCK_BATCH = 10
_DRAIN_FORGET_BATCH = 10

_STATUS_PENDING = "pending"
_STATUS_PROCESSING = "processing"
_STATUS_SYNCED = "synced"
_STATUS_FAILED = "failed"
_STATUS_DEAD = "dead"
_STATUS_FORGETTING = "forgetting"
_STATUS_FORGOTTEN = "forgotten"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def _backoff_seconds(attempt: int) -> float:
    return min(_BACKOFF_CAP_SECONDS, (2 ** max(attempt - 1, 0)) * _BACKOFF_BASE_SECONDS)


def _session():
    from app.database import SessionLocal

    return SessionLocal()


# ---------------------------------------------------------------------------
# CAS 状态转移（所有 worker 侧写入都走这里）
# ---------------------------------------------------------------------------


def _transition(
    source_id: str,
    lease_token: str | None,
    from_statuses: tuple[str, ...],
    values: dict,
) -> bool:
    """带 (status, lease_token) 栅栏的条件更新；0 行即被并发接管。"""
    db = _session()
    try:
        statement = update(SuperAssistantMemorySource).where(
            SuperAssistantMemorySource.id == source_id,
            SuperAssistantMemorySource.status.in_(from_statuses),
        )
        if lease_token is None:
            statement = statement.where(SuperAssistantMemorySource.lease_token.is_(None))
        else:
            statement = statement.where(SuperAssistantMemorySource.lease_token == lease_token)
        result = db.execute(statement.values(**values))
        db.commit()
        return result.rowcount == 1
    finally:
        db.close()


def _claim(source_id: str) -> SuperAssistantMemorySource | None:
    """原子认领：pending/failed 到点，或租约过期的 processing。

    铸造 lease_token、attempt 自增；未认领（他人持有/状态不符）返回 None。
    """
    now = _now()
    token = uuid.uuid4().hex
    db = _session()
    try:
        result = db.execute(
            update(SuperAssistantMemorySource)
            .where(
                SuperAssistantMemorySource.id == source_id,
                (
                    (SuperAssistantMemorySource.status == _STATUS_PENDING)
                    & (
                        (SuperAssistantMemorySource.next_attempt_at.is_(None))
                        | (SuperAssistantMemorySource.next_attempt_at <= now)
                    )
                )
                | (
                    (SuperAssistantMemorySource.status == _STATUS_FAILED)
                    & (SuperAssistantMemorySource.next_attempt_at <= now)
                )
                | (
                    (SuperAssistantMemorySource.status == _STATUS_PROCESSING)
                    & (
                        (SuperAssistantMemorySource.lease_expires_at.is_(None))
                        | (SuperAssistantMemorySource.lease_expires_at <= now)
                    )
                ),
            )
            .values(
                status=_STATUS_PROCESSING,
                # attempt 不在此递增：它是失败计数（见 _record_failure），
                # 租约过期的续跑/纯轮询不消耗重试额度
                lease_expires_at=now + timedelta(seconds=_LEASE_SECONDS),
                lease_token=token,
                next_attempt_at=None,
            )
        )
        db.commit()
        if result.rowcount != 1:
            return None
        row = db.get(SuperAssistantMemorySource, source_id)
        if row is None or row.lease_token != token:
            return None
        return row
    finally:
        db.close()


def _defer(
    source_id: str, lease_token: str, seconds: float, *, error: str | None = None,
) -> bool:
    """processing → pending 并延后（配置未就绪等待 / 瞬态不可用退避）。"""
    values: dict = {
        "status": _STATUS_PENDING,
        "lease_token": None,
        "lease_expires_at": None,
        "next_attempt_at": _now() + timedelta(seconds=seconds),
    }
    if error is not None:
        values["error"] = error
    return _transition(source_id, lease_token, (_STATUS_PROCESSING,), values)


def _record_failure(source_id: str, lease_token: str, message: str) -> str:
    """processing → failed（退避）/ dead（失败计数耗尽，人工 retry）。

    attempt 在转移内原子 +1（本 generation 失败计数）：退避时长按新值，
    达到 _MAX_ATTEMPTS 置 dead。配置等待与纯轮询不经过此函数。
    """
    db = _session()
    try:
        source = db.get(SuperAssistantMemorySource, source_id)
        attempt = (source.attempt if source is not None else 0) + 1
    finally:
        db.close()
    dead = attempt >= _MAX_ATTEMPTS
    ok = _transition(
        source_id, lease_token, (_STATUS_PROCESSING,),
        {
            "status": _STATUS_DEAD if dead else _STATUS_FAILED,
            "attempt": attempt,
            "error": str(message)[:500],
            "lease_token": None,
            "lease_expires_at": None,
            "next_attempt_at": None
            if dead
            else _now() + timedelta(seconds=_backoff_seconds(attempt)),
        },
    )
    if not ok:
        return "taken_over"
    return _STATUS_DEAD if dead else _STATUS_FAILED


def _persist_operation(
    source_id: str, lease_token: str, operation_id: str, explicit: bool,
) -> bool:
    """网络提交前持久化 operation_id 与本次实际提交的 explicit 快照。

    ACK 丢失后重试复用同一 id；快照供 finalize 比对在途显式变更。
    """
    return _transition(
        source_id, lease_token, (_STATUS_PROCESSING,),
        {"operation_id": operation_id, "operation_explicit": bool(explicit)},
    )


def _clear_operation(source_id: str, lease_token: str) -> bool:
    """terminal failed 后清除 operation_id 与 explicit 快照，下次认领铸新代。"""
    return _transition(
        source_id, lease_token, (_STATUS_PROCESSING,),
        {"operation_id": None, "operation_explicit": None},
    )


def _finalize_synced(
    source_id: str, lease_token: str, operation_id: str | None, document_id: str,
) -> bool:
    """operation completed → synced；在途 explicit 翻转则转 pending 重提取。

    比对行当前 explicit 与 operation_explicit 快照：不一致（用户在提交后、
    完成前翻转显式标记）不落 synced，而是 CAS 置 pending：清 op 与快照、
    重置失败计数、换派发代次——下一次以同 document_id 新 generation 用
    oo-explicit 策略重提取（上游 replace-upsert），不产生重复 document。
    """
    source = SuperAssistantMemorySource
    mismatch = source.explicit != func.coalesce(source.operation_explicit, False)
    return _transition(
        source_id, lease_token, (_STATUS_PROCESSING,),
        {
            # 在同一 UPDATE 内读取策略，防止“读取后、完成前”显式标记变化。
            "status": case((mismatch, _STATUS_PENDING), else_=_STATUS_SYNCED),
            "operation_id": case((mismatch, None), else_=operation_id),
            "operation_explicit": case((mismatch, None), else_=source.operation_explicit),
            "attempt": case((mismatch, 0), else_=source.attempt),
            # 旧提取已生成文档：即使等待显式重提取，遗忘也必须能定位它。
            "document_id": document_id,
            "last_synced_at": case((mismatch, source.last_synced_at), else_=_now()),
            "dispatch_id": case((mismatch, uuid.uuid4().hex), else_=source.dispatch_id),
            "error": None,
            "lease_token": None,
            "lease_expires_at": None,
            "next_attempt_at": None,
        },
    )


def _extend_lease(source_id: str, lease_token: str) -> bool:
    """上游仍在处理：保持 processing，租约顺延，等下一轮轮询。"""
    return _extend_lease_to(source_id, lease_token, _LEASE_SECONDS)


def _extend_lease_to(source_id: str, lease_token: str, seconds: float) -> bool:
    """CAS 续租到指定窗长；0 行 = 已被遗忘/接管（fresh 检查失败）。"""
    return _transition(
        source_id, lease_token, (_STATUS_PROCESSING,),
        {"lease_expires_at": _now() + timedelta(seconds=seconds)},
    )


# ---------------------------------------------------------------------------
# 上游交互（不持有 DB 事务；deadline 贯穿，单次 HTTP 不越总预算）
# ---------------------------------------------------------------------------


def _client(timeout: float | None = None):
    """构造上游客户端；提交阶段按剩余租约窗收窄单请求超时。

    注意 httpx Timeout 是分相位（connect/read/write/pool）而非硬总时长：
    read 超时作用于每次 socket 读，慢滴流响应的理论总时长可超名义值。
    此处以 min(默认, remaining) 限定 + 提交前 fresh CAS + 遗忘等待窗兜底
    （在途 HTTP 超窗后 forget 流仍会在 op 终态后删除，不复活）。
    """
    from app.super_assistant.hindsight_memory import make_client

    return make_client(timeout=max(1.0, min(_SUBMIT_TIMEOUT_SECONDS, timeout or _SUBMIT_TIMEOUT_SECONDS)))


def _poll_operation(client, bank_id: str, operation_id: str, deadline: float) -> str:
    """轮询到终态或预算耗尽；返回 completed/failed/pending/not_found。

    not_found 与 failed 语义不同：前者沿用原 id 重新提交（提交可能从未
    落地），后者只有清除旧 id 后才允许新 generation。
    """
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0.05:
            return "pending"
        try:
            operation = client.get_operation(
                bank_id, operation_id,
                timeout=min(remaining, _POLL_HTTP_TIMEOUT_SECONDS),
            )
        except HindsightNotFoundError:
            return "not_found"
        status = str(operation.get("status") or "")
        if status == "completed":
            return "completed"
        if status in ("failed", "cancelled"):
            return "failed"
        if status == "not_found":
            return "not_found"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "pending"
        time.sleep(min(_POLL_INTERVAL_SECONDS, remaining))


def _run_sync(client, claimed: SuperAssistantMemorySource, poll_budget: float) -> None:
    """同步主体：先核对已提交 operation，再按需提交新 retain 并跟随。"""
    from app.super_assistant.hindsight_memory import (
        bank_id_for,
        build_retain_item,
        ensure_bank,
    )

    source_id = claimed.id
    token = claimed.lease_token or ""
    bank_id = bank_id_for(claimed.owner_id)
    deadline = time.monotonic() + poll_budget

    operation_id = claimed.operation_id
    if operation_id:
        outcome = _poll_operation(client, bank_id, operation_id, deadline)
        if outcome == "completed":
            _finalize_synced(source_id, token, operation_id, source_id)
            return
        if outcome == "pending":
            _extend_lease(source_id, token)
            return
        if outcome == "failed":
            # 明确 terminal failed：清除旧 operation，新认领铸造新 id
            if not _clear_operation(source_id, token):
                return  # 行已被遗忘/接管，交给遗忘流处理
            operation_id = None
        # not_found：沿用原 id 重新提交（ACK 丢失场景，上游按 id 幂等）

    if not operation_id:
        operation_id = str(uuid.uuid4())
        if not _persist_operation(source_id, token, operation_id, bool(claimed.explicit)):
            return  # 持久化失败（被接管）不得发起网络提交

    # 提交前 fresh 租约检查：ensure_bank 网络期间可能已被遗忘/接管；
    # 续租到提交窗（forget_source 会把该租约拷为遗忘等待窗，在途 HTTP
    # 最多截止在窗内），失败即放弃提交。后续单请求超时按剩余窗收窄
    if not _extend_lease_to(source_id, token, _SUBMIT_LEASE_SECONDS):
        return
    submit_deadline = time.monotonic() + _SUBMIT_LEASE_SECONDS
    try:
        ensure_bank(_client((submit_deadline - time.monotonic()) / 2), claimed.owner_id)
        if not _extend_lease_to(source_id, token, _SUBMIT_LEASE_SECONDS):
            return  # ensure 网络后、retain 提交前的最后一刻检查
        retain_client = _client(submit_deadline - time.monotonic())
        retain_client.retain_async(bank_id, [build_retain_item(claimed)], operation_id)
    except HindsightConflictError:
        # 该 operation_id 已存在（重复提交/重放）：核实后跟随，不新建
        outcome = _poll_operation(client, bank_id, operation_id, deadline)
        if outcome == "completed":
            _finalize_synced(source_id, token, operation_id, source_id)
        elif outcome == "failed":
            if _clear_operation(source_id, token):
                _record_failure(source_id, token, "retain operation 终态失败")
        else:
            _extend_lease(source_id, token)
        return
    except HindsightUnavailableError:
        # 提交结果未知（可能已入队）：operation_id 已持久化，重试先轮询
        # 同一 id 不重复入队；按失败计数退避，不可用期不会无限 defer
        _record_failure(source_id, token, "hindsight 不可达（提交结果未知）")
        return

    outcome = _poll_operation(client, bank_id, operation_id, deadline)
    if outcome == "completed":
        _finalize_synced(source_id, token, operation_id, source_id)
    elif outcome == "failed":
        if _clear_operation(source_id, token):
            _record_failure(source_id, token, f"retain operation 终态失败 (op={operation_id})")
    else:
        _extend_lease(source_id, token)


# ---------------------------------------------------------------------------
# 消息入口（NATS handler）
# ---------------------------------------------------------------------------


def process_message(payload: dict, *, poll_budget: float = 20.0) -> None:
    """super_assistant.memory.sync 消费入口；幂等，业务异常不外抛。

    synced/forgotten 直接 ack（完成重放不重新 retain）；forgetting 走遗忘
    流；dead 等人工 retry；配置未就绪回到 pending 等待；瞬态不可用退避；
    其余异常记失败。
    """
    source_id = str((payload or {}).get("source_id") or "").strip()
    if not source_id:
        logger.error("记忆同步消息缺少 source_id，丢弃")
        return

    db = _session()
    try:
        source = db.get(SuperAssistantMemorySource, source_id)
        if source is None:
            logger.warning("记忆来源不存在（可能已清理）: %s", source_id)
            return
        status = source.status
        lease_valid = (
            source.status == _STATUS_PROCESSING
            and (_aware(source.lease_expires_at) or _now()) > _now()
        )
        retry_due = source.status == _STATUS_FAILED and (
            _aware(source.next_attempt_at) is None
            or _aware(source.next_attempt_at) <= _now()
        )
    finally:
        db.close()

    if status in (_STATUS_SYNCED, _STATUS_FORGOTTEN, _STATUS_DEAD):
        return
    if status == _STATUS_FORGETTING:
        advance_forget(source_id, poll_budget=poll_budget)
        return
    if status == _STATUS_PROCESSING and lease_valid:
        return  # 其他 worker 持有租约（重复投递）
    if status == _STATUS_FAILED and not retry_due:
        return  # 退避未到点

    claimed = _claim(source_id)
    if claimed is None:
        return

    if not _sync_gate(claimed):
        _defer(source_id, claimed.lease_token or "", _DEFER_UNCONFIGURED_SECONDS)
        return

    try:
        _run_sync(_client(), claimed, poll_budget)
    except HindsightUnavailableError:
        _record_failure(
            source_id, claimed.lease_token or "", "hindsight 不可达",
        )
    except Exception:
        logger.exception("记忆同步未预期失败（source=%s）", source_id)
        _record_failure(source_id, claimed.lease_token or "", "同步未预期异常")


def _sync_gate(source: SuperAssistantMemorySource) -> bool:
    """配置门槛：平台 base_url 就绪且用户 enabled；未就绪等待而非失败。"""
    from app.super_assistant.hindsight_memory import get_settings, platform_config

    base_url, _, _, _ = platform_config()
    if not base_url:
        logger.info("记忆同步等待平台配置（source=%s）", source.id)
        return False
    db = _session()
    try:
        setting = get_settings(db, source.owner_id)
        if not setting.enabled or not setting.model_config_id:
            logger.info("记忆同步等待配置（source=%s）", source.id)
            return False
        return True
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 遗忘推进
# ---------------------------------------------------------------------------


def advance_forget(source_id: str, *, poll_budget: float = 10.0) -> None:
    """forgetting → forgotten：等上游 operation 终态，删 document 并核实。

    tombstone 已由 forget_source 先落；这里以 forgetting 租约串行化多
    worker；核实失败保持 forgetting 由 drain 重试——上游未完成前绝不置
    forgotten，已提交 operation 的终态始终被跟踪（含 completed 后删除）。
    """
    token = uuid.uuid4().hex
    now = _now()
    db = _session()
    try:
        result = db.execute(
            update(SuperAssistantMemorySource)
            .where(
                SuperAssistantMemorySource.id == source_id,
                SuperAssistantMemorySource.status == _STATUS_FORGETTING,
                (
                    SuperAssistantMemorySource.next_attempt_at.is_(None)
                    | (SuperAssistantMemorySource.next_attempt_at <= now)
                ),
                (
                    SuperAssistantMemorySource.lease_token.is_(None)
                    | (SuperAssistantMemorySource.lease_expires_at <= now)
                ),
            )
            .values(
                lease_token=token,
                lease_expires_at=now + timedelta(seconds=_LEASE_SECONDS),
            )
        )
        db.commit()
        if result.rowcount != 1:
            return
        source = db.get(SuperAssistantMemorySource, source_id)
        if source is None:
            return
        owner_id = source.owner_id
        operation_id = source.operation_id
        # retain 恒以 source.id 作 document_id：即使 finalize 前被遗忘，
        # 只要提交过 operation 就按该 id 删除上游文档
        document_id = source.document_id or (source.id if source.operation_id else None)
    finally:
        db.close()

    client = _client()
    from app.super_assistant.hindsight_memory import bank_id_for

    bank_id = bank_id_for(owner_id)
    if operation_id:
        try:
            outcome = _poll_operation(
                client, bank_id, operation_id, time.monotonic() + poll_budget,
            )
        except HindsightUnavailableError:
            _extend_forget_lease(source_id, token)
            return
        if outcome == "pending":
            # 上游 retain 仍在进行：等完成后再删，防删除与 retain 竞争复活
            _extend_forget_lease(source_id, token)
            return
        if outcome == "not_found":
            # 404 不能证明原请求不会晚到。以同一幂等 ID 提交不含原文的
            # 占位操作：上游以 parent PK 串行化首次提交，晚到的旧请求只能
            # 重放这一个操作，不会再次提取已撤回正文。
            try:
                from app.super_assistant.hindsight_memory import ensure_bank

                ensure_bank(client, owner_id)
                client.retain_async(bank_id, [{
                    "content": "此来源已撤回，不包含可保留的事实。",
                    "document_id": document_id or source_id,
                    "metadata": {"source_id": source_id, "withdrawn": "true"},
                }], operation_id)
                outcome = _poll_operation(
                    client, bank_id, operation_id, time.monotonic() + poll_budget,
                )
            except (HindsightUnavailableError, HindsightConflictError):
                _extend_forget_lease(source_id, token)
                return
            if outcome not in ("completed", "failed"):
                _extend_forget_lease(source_id, token)
                return
        # 只在已核实的终态后删除；不存在的操作先经幂等占位封住晚到请求。

    if not document_id:
        _mark_forgotten(source_id, token)
        return

    try:
        client.delete_document(bank_id, document_id)
    except HindsightNotFoundError:
        pass  # 已不存在视为删除成功，继续核实
    except HindsightUnavailableError:
        logger.warning("记忆遗忘删除暂不可达（source=%s）", source_id)
        _extend_forget_lease(source_id, token)
        return

    # 核实：列表 q 过滤后不得再出现该 document id
    try:
        listing = client.list_documents(bank_id, q=document_id, limit=100)
    except HindsightUnavailableError:
        _extend_forget_lease(source_id, token)
        return
    leftover = [
        item for item in listing.get("items") or [] if str(item.get("id")) == document_id
    ]
    if leftover:
        logger.error("记忆遗忘核实失败：document 仍存在（source=%s）", source_id)
        _extend_forget_lease(source_id, token)
        return
    _mark_forgotten(source_id, token)


def _extend_forget_lease(source_id: str, token: str) -> bool:
    return _transition(
        source_id, token, (_STATUS_FORGETTING,),
        {"lease_expires_at": _now() + timedelta(seconds=_LEASE_SECONDS)},
    )


def _mark_forgotten(source_id: str, token: str) -> bool:
    """上游 document 确认不存在后置 forgotten：擦除正文/上下文（平台侧
    不再保留已遗忘原文），保留 id/source_message_id/version 作 tombstone
    防完成重放；forgetting 阶段不提前擦除，便于在途处理与审计。"""
    return _transition(
        source_id, token, (_STATUS_FORGETTING,),
        {
            "status": _STATUS_FORGOTTEN,
            "content": "",
            "context": "",
            "lease_token": None,
            "lease_expires_at": None,
            "next_attempt_at": None,
        },
    )


# ---------------------------------------------------------------------------
# 对账（APScheduler 每 5 秒；只派发，不做网络轮询/昂贵操作）
# ---------------------------------------------------------------------------


def _dispatch(source_id: str, dispatch_id: str) -> bool:
    """按持久代次令牌派发（Msg-Id 幂等键 = 行内 dispatch_id）。"""
    try:
        from app.data_channel.pipeline_tasks.dispatch import (
            dispatch_super_assistant_memory_sync,
        )

        dispatch_super_assistant_memory_sync(source_id, dispatch_id)
        return True
    except Exception:
        logger.warning("记忆同步派发失败（source=%s）", source_id, exc_info=True)
        return False


def _bump_dispatch(source_id: str) -> str | None:
    """换新派发代次并返回令牌（对账补发必须新代次，保证送达）。"""
    token = uuid.uuid4().hex
    db = _session()
    try:
        db.execute(
            update(SuperAssistantMemorySource)
            .where(SuperAssistantMemorySource.id == source_id)
            .values(dispatch_id=token, updated_at=_now())
        )
        db.commit()
    finally:
        db.close()
    return token


def drain_once() -> dict:
    """有界补派发：丢件 pending/failed、租约过期 processing/forgetting。

    全部经 NATS 送回 executor worker 执行（本函数零网络、零 LLM）。
    """
    report = {"dispatched": 0, "redispatched": 0, "recovered": 0, "forgot": 0}
    now = _now()
    stale_before = now - timedelta(seconds=_DISPATCH_STALE_SECONDS)

    db = _session()
    try:
        due = [
            row.id
            for row in db.query(SuperAssistantMemorySource)
            .filter(
                SuperAssistantMemorySource.status.in_((_STATUS_PENDING, _STATUS_FAILED)),
                (
                    SuperAssistantMemorySource.next_attempt_at.is_(None)
                    | (SuperAssistantMemorySource.next_attempt_at <= now)
                ),
                SuperAssistantMemorySource.updated_at <= stale_before,
            )
            .order_by(SuperAssistantMemorySource.updated_at.asc())
            .limit(_DRAIN_DISPATCH_BATCH)
            .all()
        ]
        stuck = [
            row.id
            for row in db.query(SuperAssistantMemorySource)
            .filter(
                SuperAssistantMemorySource.status == _STATUS_PROCESSING,
                (
                    SuperAssistantMemorySource.lease_expires_at.is_(None)
                    | (SuperAssistantMemorySource.lease_expires_at <= now)
                ),
            )
            .limit(_DRAIN_STUCK_BATCH)
            .all()
        ]
        forgetting = [
            row.id
            for row in db.query(SuperAssistantMemorySource)
            .filter(
                SuperAssistantMemorySource.status == _STATUS_FORGETTING,
                (
                    SuperAssistantMemorySource.next_attempt_at.is_(None)
                    | (SuperAssistantMemorySource.next_attempt_at <= now)
                ),
                (
                    SuperAssistantMemorySource.lease_expires_at.is_(None)
                    | (SuperAssistantMemorySource.lease_expires_at <= now)
                ),
            )
            .limit(_DRAIN_FORGET_BATCH)
            .all()
        ]
    finally:
        db.close()

    for source_id in due:
        token = _bump_dispatch(source_id)
        if token and _dispatch(source_id, token):
            report["redispatched"] += 1
    for source_id in stuck:
        token = _bump_dispatch(source_id)
        if token and _dispatch(source_id, token):
            report["recovered"] += 1
    for source_id in forgetting:
        token = _bump_dispatch(source_id)
        if token and _dispatch(source_id, token):
            report["forgot"] += 1
    report["dispatched"] = (
        report["redispatched"] + report["recovered"] + report["forgot"]
    )
    return report
