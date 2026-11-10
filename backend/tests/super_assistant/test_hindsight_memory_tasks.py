"""跨会话记忆同步任务专项测试：CAS 栅栏、ACK 丢失、遗忘竞争、对账派发。"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from app.super_assistant import hindsight_memory as hm
from app.super_assistant import memory_tasks as mt
from app.super_assistant.hindsight_client import HindsightError, HindsightNotFoundError
from app.super_assistant.memory_models import (
    SuperAssistantMemorySetting,
    SuperAssistantMemorySource,
)
from tests.conftest import TestSession


class FakeClient:
    """可编程的 Hindsight 契约假实现（retain/operation/documents）。"""

    def __init__(self, *, op_status="completed"):
        self.op_status = op_status
        self.retains: list[tuple[str, list[dict], str]] = []
        self.ensured = 0
        self.op_calls = 0
        self.deleted: list[str] = []
        self.documents: list[str] = []
        self.retain_hook = None  # callable(items, op_id) 在提交瞬间回调
        self.ensure_hook = None  # callable() 在 ensure_bank 网络期间回调
        self.listed: list[str] | None = None
        self.strict_ops = False  # True: 未提交过的 op 一律 404（贴近真实）

    def probe(self, *, timeout=2.0):
        return {"status": "healthy"}

    def ensure_bank(self, bank_id, body):
        self.ensured += 1
        if self.ensure_hook:
            self.ensure_hook()

    def update_bank_config(self, bank_id, updates):
        pass

    def retain_async(self, bank_id, items, operation_id):
        if self.retain_hook:
            self.retain_hook(items, operation_id)
        self.retains.append((bank_id, items, operation_id))
        document_id = items[0]["document_id"]
        if document_id not in self.documents:
            self.documents.append(document_id)

    def get_operation(self, bank_id, operation_id, *, timeout=None):
        self.op_calls += 1
        return {"operation_id": operation_id, "status": self.op_status}

    def delete_document(self, bank_id, document_id):
        if document_id not in self.documents:
            raise HindsightNotFoundError(404, "not_found")
        self.deleted.append(document_id)
        if self.listed is None:
            self.documents.remove(document_id)

    def list_documents(self, bank_id, *, q=None, limit=100, offset=0):
        remaining = self.listed if self.listed is not None else self.documents
        items = [{"id": doc} for doc in remaining if q is None or q in doc]
        return {"items": items, "total": len(items)}


@pytest.fixture(autouse=True)
def worker_env(monkeypatch):
    """平台已配置 + worker 会话指向测试库。"""
    from app.shared.config import settings as platform_settings

    monkeypatch.setattr(platform_settings, "hindsight_base_url", "http://hindsight", raising=False)
    monkeypatch.setattr(platform_settings, "hindsight_api_key", "key", raising=False)
    monkeypatch.setattr("app.database.SessionLocal", TestSession)
    hm._BANK_CONFIG_CACHE.clear()
    yield
    hm._BANK_CONFIG_CACHE.clear()


def _seed(owner_id="owner-1", *, status="pending", model_config_id="mc-1", **kwargs) -> str:
    with TestSession() as db:
        if db.get(SuperAssistantMemorySetting, owner_id) is None:
            db.add(SuperAssistantMemorySetting(
                owner_id=owner_id, enabled=True, model_config_id=model_config_id,
            ))
        source = SuperAssistantMemorySource(
            owner_id=owner_id, source_message_id=f"m-{uuid4_hex()}", version=1,
            role="user", content="用户偏好深色主题",
            occurred_at=datetime.now(timezone.utc), status=status, **kwargs,
        )
        db.add(source)
        db.commit()
        return source.id


def uuid4_hex():
    import uuid

    return uuid.uuid4().hex


def _row(source_id):
    with TestSession() as db:
        row = db.get(SuperAssistantMemorySource, source_id)
        assert row is not None
        return row


def _make_due(source_id):
    _set_time(source_id, "next_attempt_at", -1)


def _raise_not_found():
    raise HindsightNotFoundError(404, "not_found")


def _aware(moment):
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def _hold_next_attempt(source_id, seconds=60):
    _set_time(source_id, "next_attempt_at", seconds)


def _set_time(source_id, field, offset_seconds):
    with TestSession() as db:
        db.execute(
            update(SuperAssistantMemorySource)
            .where(SuperAssistantMemorySource.id == source_id)
            .values(**{field: datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)})
        )
        db.commit()


def _expire_lease(source_id):
    with TestSession() as db:
        db.execute(
            update(SuperAssistantMemorySource)
            .where(SuperAssistantMemorySource.id == source_id)
            .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
        db.commit()


def _age_row(source_id, seconds):
    """绕过 ORM onupdate 直接改 updated_at（drain 陈旧度判定用）。"""
    with TestSession() as db:
        db.execute(
            update(SuperAssistantMemorySource)
            .where(SuperAssistantMemorySource.id == source_id)
            .values(updated_at=datetime.now(timezone.utc) - timedelta(seconds=seconds))
        )
        db.commit()


class TestHappyPath:
    def test_full_sync_persists_op_before_network(self, monkeypatch):
        source_id = _seed()
        fake = FakeClient()

        def hook(items, op_id):
            # 网络提交瞬间：operation_id 必须已持久化（ACK 丢失防护的前提）
            with TestSession() as db:
                row = db.get(SuperAssistantMemorySource, source_id)
                assert row.operation_id == op_id

        fake.retain_hook = hook
        monkeypatch.setattr(mt, "_client", lambda timeout=None: fake)
        mt.process_message({"source_id": source_id})
        row = _row(source_id)
        assert row.status == "synced"
        assert row.document_id == source_id
        assert row.last_synced_at is not None
        assert row.error is None
        assert len(fake.retains) == 1
        bank_id, items, op_id = fake.retains[0]
        assert bank_id.startswith("openontology-")
        assert items[0]["document_id"] == source_id
        assert items[0]["content"].startswith("【用户｜")
        assert items[0]["metadata"]["source_id"] == source_id

    def test_completion_replay_does_not_retain(self, monkeypatch):
        source_id = _seed(status="synced", document_id="doc", operation_id="op-1")
        fake = FakeClient()
        monkeypatch.setattr(mt, "_client", lambda timeout=None: fake)
        mt.process_message({"source_id": source_id})
        assert fake.retains == []
        assert _row(source_id).status == "synced"

    def test_duplicate_delivery_with_valid_lease_ignored(self, monkeypatch):
        source_id = _seed(status="processing", operation_id="op-1")
        with TestSession() as db:
            row = db.get(SuperAssistantMemorySource, source_id)
            row.lease_token = "other-worker"
            row.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
            db.commit()
        fake = FakeClient()
        monkeypatch.setattr(mt, "_client", lambda timeout=None: fake)
        mt.process_message({"source_id": source_id})
        assert fake.retains == []
        assert _row(source_id).lease_token == "other-worker"


class TestConfigGate:
    def test_no_model_waits_without_burning_retry(self, monkeypatch):
        """未选记忆模型：等待而非失败，且不消耗重试额度。"""
        source_id = _seed(model_config_id=None)
        fake = FakeClient()
        monkeypatch.setattr(mt, "_client", lambda timeout=None: fake)
        mt.process_message({"source_id": source_id})
        row = _row(source_id)
        assert row.status == "pending"
        assert row.error is None
        assert fake.retains == []
        assert row.attempt == 0
        assert _aware(row.next_attempt_at) > datetime.now(timezone.utc) + timedelta(minutes=4)


class TestFailureCap:
    def test_unavailable_is_finite_never_infinite_defer(self, monkeypatch):
        """持续不可用必须耗尽上限进 dead，而不是无限自动 _defer。"""
        from app.super_assistant.hindsight_client import HindsightUnavailableError

        source_id = _seed()
        fake = FakeClient()
        fake.get_operation = lambda bank_id, op_id, *, timeout=None: _raise_not_found()
        fake.retain_async = lambda *a, **k: (_ for _ in ()).throw(
            HindsightUnavailableError("down"),
        )
        monkeypatch.setattr(mt, "_client", lambda timeout=None: fake)
        statuses = []
        for index in range(mt._MAX_ATTEMPTS):
            if index:
                _make_due(source_id)
            mt.process_message({"source_id": source_id})
            statuses.append(_row(source_id).status)
        assert statuses[-1] == "dead"
        assert _row(source_id).attempt == mt._MAX_ATTEMPTS
        assert "不可达" in _row(source_id).error

    def test_pending_op_polling_does_not_burn_retry_cap(self, monkeypatch):
        """正常 pending operation 的跨租约续轮询不消耗失败上限。"""
        source_id = _seed(status="processing", operation_id="op-slow")
        _expire_lease(source_id)
        fake = FakeClient(op_status="processing")
        monkeypatch.setattr(mt, "_client", lambda timeout=None: fake)
        for _ in range(3):
            mt.process_message({"source_id": source_id}, poll_budget=0.2)
            _expire_lease(source_id)
        row = _row(source_id)
        assert row.status == "processing"
        assert row.attempt == 0
        assert fake.retains == []  # 未重新提交


class TestFreshLeaseBeforeSubmit:
    def test_forget_during_ensure_skips_retain(self, monkeypatch):
        """ensure_bank 网络期间被遗忘：fresh 检查失败，retain 不得提交。"""
        source_id = _seed()
        fake = FakeClient()

        def racing_ensure():
            with TestSession() as db:
                hm.forget_source(db, "owner-1", source_id)
                db.commit()

        fake.ensure_hook = racing_ensure
        monkeypatch.setattr(mt, "_client", lambda timeout=None: fake)
        mt.process_message({"source_id": source_id})
        assert fake.ensured == 1
        assert fake.retains == []  # 遗忘后未再提交
        row = _row(source_id)
        assert row.status == "forgetting"
        # forget_source 已把提交租约窗拷为遗忘等待窗
        assert _aware(row.next_attempt_at) > datetime.now(timezone.utc)

    def test_submit_lease_bounded_within_forget_wait_window(self, monkeypatch):
        """提交阶段租约有界，且遗忘等待窗 ≥ 在途租约（旧 HTTP 截止窗内）。"""
        source_id = _seed()
        captured: dict[str, object] = {}

        def hook(items, op_id):
            with TestSession() as db:
                row = db.get(SuperAssistantMemorySource, source_id)
                captured["lease_expires_at"] = row.lease_expires_at
                hm.forget_source(db, "owner-1", source_id)  # 提交瞬间遗忘
                db.commit()

        fake = FakeClient()
        fake.retain_hook = hook
        monkeypatch.setattr(mt, "_client", lambda timeout=None: fake)
        mt.process_message({"source_id": source_id})
        row = _row(source_id)
        assert row.status == "forgetting"
        assert _aware(row.next_attempt_at) >= _aware(
            captured["lease_expires_at"]) - timedelta(seconds=1)


class TestAckLoss:
    def test_ack_lost_reuses_operation_id(self, monkeypatch):
        from app.super_assistant.hindsight_client import HindsightUnavailableError

        source_id = _seed()
        fake = FakeClient(op_status="completed")

        def lost_ack(bank_id, items, operation_id):
            fake.retains.append((bank_id, items, operation_id))  # 上游已受理
            raise HindsightUnavailableError("ack lost")

        fake.retain_async = lost_ack
        monkeypatch.setattr(mt, "_client", lambda timeout=None: fake)
        mt.process_message({"source_id": source_id})
        row = _row(source_id)
        # 瞬态不可用按失败计（有限重试），op_id 已留存供重试轮询复用
        assert row.status == "failed"
        assert row.operation_id is not None
        saved_op = row.operation_id
        _make_due(source_id)

        # 恢复可达：先轮询既有 op（completed）→ 直接 finalize，不再 retain
        fake2 = FakeClient(op_status="completed")
        monkeypatch.setattr(mt, "_client", lambda timeout=None: fake2)
        mt.process_message({"source_id": source_id})
        assert fake2.retains == []
        row = _row(source_id)
        assert row.status == "synced"
        assert row.operation_id == saved_op

    def test_op_not_found_resubmits_same_id(self, monkeypatch):
        source_id = _seed(status="failed", operation_id="op-gone")
        _make_due(source_id)
        fake = FakeClient(op_status="not_found")
        monkeypatch.setattr(mt, "_client", lambda timeout=None: fake)
        mt.process_message({"source_id": source_id})
        # 沿用遗留 id 重新提交（提交从未落地），且提交后 not_found 保持租约等待
        assert [op for _, _, op in fake.retains] == ["op-gone"]
        row = _row(source_id)
        assert row.operation_id == "op-gone"
        assert row.status == "processing"

    def test_terminal_failed_mints_new_generation(self, monkeypatch):
        source_id = _seed(status="failed", operation_id="op-old")
        _make_due(source_id)
        fake = FakeClient(op_status="failed")
        monkeypatch.setattr(mt, "_client", lambda timeout=None: fake)
        mt.process_message({"source_id": source_id})
        row = _row(source_id)
        assert row.status == "failed"
        assert row.operation_id is None  # 旧 op 已清除 → 下次认领铸新 id
        assert row.attempt == 1

        _make_due(source_id)
        fake2 = FakeClient(op_status="completed")
        monkeypatch.setattr(mt, "_client", lambda timeout=None: fake2)
        mt.process_message({"source_id": source_id})
        assert fake2.retains, "新 generation 必须重新提交"
        new_op = fake2.retains[0][2]
        assert new_op != "op-old"
        assert _row(source_id).status == "synced"

    def test_conflict_verifies_then_follows_same_op(self, monkeypatch):
        from app.super_assistant.hindsight_client import HindsightConflictError

        source_id = _seed()
        fake = FakeClient(op_status="processing")

        def retain(bank_id, items, operation_id):
            raise HindsightConflictError(409, "operation_conflict")

        fake.retain_async = retain
        monkeypatch.setattr(mt, "_client", lambda timeout=None: fake)
        mt.process_message({"source_id": source_id})
        row = _row(source_id)
        assert row.status == "processing"  # 409 核实后跟随同一 op
        assert row.operation_id is not None
        assert fake.retains == []


class TestFailurePolicy:
    def test_dead_after_max_attempts(self, monkeypatch):
        source_id = _seed()
        fake = FakeClient()
        fake.get_operation = lambda bank_id, op_id, *, timeout=None: _raise_not_found()
        fake.retain_async = lambda *a, **k: (_ for _ in ()).throw(
            HindsightError(400, "client_error_400"),
        )
        monkeypatch.setattr(mt, "_client", lambda timeout=None: fake)
        statuses = []
        for index in range(mt._MAX_ATTEMPTS):
            if index:
                _make_due(source_id)
            mt.process_message({"source_id": source_id})
            statuses.append(_row(source_id).status)
        assert statuses[-1] == "dead"
        row = _row(source_id)
        assert row.attempt == mt._MAX_ATTEMPTS
        assert row.next_attempt_at is None
        assert row.error
        # dead 不再自动认领
        mt.process_message({"source_id": source_id})
        assert _row(source_id).status == "dead"

    def test_unconfigured_platform_defers_without_failure(self, monkeypatch):
        from app.shared.config import settings as platform_settings

        monkeypatch.setattr(platform_settings, "hindsight_base_url", "", raising=False)
        source_id = _seed()
        fake = FakeClient()
        monkeypatch.setattr(mt, "_client", lambda timeout=None: fake)
        mt.process_message({"source_id": source_id})
        row = _row(source_id)
        assert row.status == "pending"
        assert row.error is None
        assert row.attempt == 0  # 配置等待不消耗重试额度
        assert _aware(row.next_attempt_at) > datetime.now(timezone.utc) + timedelta(minutes=4)
        assert fake.retains == []
        # 退避未到点：重复投递不再认领、attempt 不增长
        mt.process_message({"source_id": source_id})
        assert _row(source_id).attempt == 0

    def test_disabled_user_waits(self, monkeypatch):
        source_id = _seed()
        with TestSession() as db:
            db.get(SuperAssistantMemorySetting, "owner-1").enabled = False
            db.commit()
        fake = FakeClient()
        monkeypatch.setattr(mt, "_client", lambda timeout=None: fake)
        mt.process_message({"source_id": source_id})
        assert _row(source_id).status == "pending"
        assert fake.retains == []



class TestExplicitSnapshot:
    def test_inflight_explicit_change_requeues_new_generation(self, monkeypatch):
        """提交后/完成前翻转 explicit：旧 op 完成不落 synced，转 pending 以
        同 document_id 用 oo-explicit 策略重提取，两轮后 synced 无重复 doc。"""
        source_id = _seed()  # explicit=False 默认策略
        fake = FakeClient(op_status="completed")

        def flip_explicit(items, op_id):
            # retain 已受理（快照 False 已在提交前持久化），用户此刻翻标记
            with TestSession() as db:
                db.get(SuperAssistantMemorySource, source_id).explicit = True
                db.commit()

        fake.retain_hook = flip_explicit
        monkeypatch.setattr(mt, "_client", lambda timeout=None: fake)
        mt.process_message({"source_id": source_id}, poll_budget=5.0)
        row = _row(source_id)
        assert row.status == "pending"  # 不 synced：转 pending 重提取
        assert row.operation_id is None and row.operation_explicit is None
        assert row.attempt == 0 and row.error is None
        first_doc = fake.retains[0][1][0]["document_id"]
        assert "strategy" not in fake.retains[0][1][0]  # 首轮默认策略

        # 第二轮：同 document 新 generation，oo-explicit 策略
        mt.process_message({"source_id": source_id}, poll_budget=5.0)
        row = _row(source_id)
        assert row.status == "synced"
        assert len(fake.retains) == 2
        second_item = fake.retains[1][1][0]
        assert second_item["document_id"] == first_doc  # 同 doc，无重复
        assert second_item["strategy"] == "oo-explicit"
        assert {doc for doc in fake.documents} == {first_doc}

    def test_restart_old_op_completes_then_requeues_explicit(self, monkeypatch):
        """重启恢复：崩溃时已提交旧 op（快照 False），租约过期后新 worker
        轮询旧 op 完成 → 发现 explicit 已翻 → 重排 → 新 generation 重提取。"""
        source_id = _seed(
            status="processing", operation_id="op-old",
            operation_explicit=False, explicit=True,
        )  # 崩溃期间用户翻了 explicit（enqueue_explicit 对 processing 只翻标记）
        _expire_lease(source_id)
        fake = FakeClient(op_status="completed")
        monkeypatch.setattr(mt, "_client", lambda timeout=None: fake)
        mt.process_message({"source_id": source_id}, poll_budget=5.0)
        row = _row(source_id)
        assert row.status == "pending"  # 旧 op 完成但快照不符 → 重排
        assert row.operation_id is None
        assert row.attempt == 0
        assert fake.retains == []  # 本轮只轮询未提交

        mt.process_message({"source_id": source_id}, poll_budget=5.0)
        row = _row(source_id)
        assert row.status == "synced"
        assert len(fake.retains) == 1
        item = fake.retains[0][1][0]
        assert item["document_id"] == source_id
        assert item["strategy"] == "oo-explicit"
        assert fake.retains[0][2] != "op-old"  # 新 generation
        assert {doc for doc in fake.documents} == {source_id}  # 无重复 doc

class TestForget:
    def test_forget_during_submit_does_not_resurrect(self, monkeypatch):
        source_id = _seed()
        fake = FakeClient(op_status="completed")

        def racing_retain(items, op_id):
            # 提交进行中用户发起遗忘：tombstone 先落并保存等待窗
            with TestSession() as db:
                hm.forget_source(db, "owner-1", source_id)
                db.commit()

        fake.retain_hook = racing_retain
        monkeypatch.setattr(mt, "_client", lambda timeout=None: fake)
        mt.process_message({"source_id": source_id})
        row = _row(source_id)
        assert row.status == "forgetting"  # CAS 拒绝 stale worker 写回 synced
        assert row.lease_token is None
        assert row.content  # forgetting 阶段不提前擦除原文

        # 等待窗内：不认领、不轮询、不删除（防暂 404 的在途提交复活）
        probe_client = FakeClient(op_status="not_found")
        probe_client.documents = [source_id]  # 上游已有该 doc（首个 fake 提交）
        monkeypatch.setattr(mt, "_client", lambda timeout=None: probe_client)
        mt.advance_forget(source_id, poll_budget=0.3)
        assert probe_client.op_calls == 0
        assert probe_client.deleted == []
        assert _row(source_id).status == "forgetting"

        # 等待窗到点：暂 404 的操作先用同一 ID 封住延迟请求，再删除。
        probe_client.retain_hook = lambda items, op_id: setattr(probe_client, "op_status", "completed")
        _make_due(source_id)
        mt.advance_forget(source_id, poll_budget=1.0)
        assert probe_client.retains[0][2] == row.operation_id
        assert "用户偏好深色主题" not in probe_client.retains[0][1][0]["content"]
        assert source_id in probe_client.deleted
        row = _row(source_id)
        assert row.status == "forgotten"
        assert row.content == ""  # 完成后擦除原文（tombstone 保留 ID）
        assert row.context == ""
        assert row.source_message_id  # 防完成重放的 tombstone 仍在

    def test_forget_wait_window_holds_despite_op_404(self, monkeypatch):
        """operation 暂 404 但旧提交租约未到：不删、不标 forgotten。"""
        source_id = _seed(
            status="forgetting", document_id="doc-1", operation_id="op-1",
        )
        _hold_next_attempt(source_id, seconds=60)  # forget_source 保存的等待窗
        fake = FakeClient(op_status="not_found")
        fake.documents = ["doc-1"]
        monkeypatch.setattr(mt, "_client", lambda timeout=None: fake)
        mt.advance_forget(source_id, poll_budget=0.5)
        assert fake.op_calls == 0  # 等待窗内不认领，连轮询都不发起
        assert fake.deleted == []
        assert _row(source_id).status == "forgetting"

        fake.retain_hook = lambda items, op_id: setattr(fake, "op_status", "completed")
        _make_due(source_id)
        mt.advance_forget(source_id, poll_budget=1.0)
        assert fake.op_calls >= 2
        assert fake.retains[0][2] == "op-1"
        assert fake.retains[0][1][0]["metadata"]["withdrawn"] == "true"
        assert fake.deleted == ["doc-1"]
        row = _row(source_id)
        assert row.status == "forgotten"
        assert row.content == ""

    def test_advance_waits_for_pending_op_before_delete(self, monkeypatch):
        source_id = _seed(
            status="forgetting", document_id="doc-1", operation_id="op-run",
        )
        fake = FakeClient(op_status="processing")
        fake.documents = ["doc-1"]
        monkeypatch.setattr(mt, "_client", lambda timeout=None: fake)
        mt.advance_forget(source_id, poll_budget=0.2)
        assert fake.deleted == []
        assert _row(source_id).status == "forgetting"

        # 租约到期后再推进：op 已完成 → 删除并核实
        _expire_lease(source_id)
        fake.op_status = "completed"
        mt.advance_forget(source_id, poll_budget=1.0)
        assert fake.deleted == ["doc-1"]
        row = _row(source_id)
        assert row.status == "forgotten"
        assert row.content == "" and row.context == ""

    def test_verify_failure_keeps_forgetting(self, monkeypatch):
        source_id = _seed(
            status="forgetting", document_id="doc-1", operation_id=None,
        )
        fake = FakeClient()
        fake.documents = ["doc-1"]
        fake.listed = ["doc-1"]  # 删除后列表仍返回（核实失败）
        monkeypatch.setattr(mt, "_client", lambda timeout=None: fake)
        mt.advance_forget(source_id, poll_budget=1.0)
        assert _row(source_id).status == "forgetting"
        assert _row(source_id).lease_expires_at is not None

    def test_never_synced_forgets_immediately(self, monkeypatch):
        source_id = _seed(status="forgetting", document_id=None, operation_id=None)
        fake = FakeClient()
        monkeypatch.setattr(mt, "_client", lambda timeout=None: fake)
        mt.advance_forget(source_id, poll_budget=1.0)
        row = _row(source_id)
        assert row.status == "forgotten"
        assert row.content == ""
        assert fake.deleted == []

    def test_stale_worker_cannot_overwrite_forgetting(self, monkeypatch):
        source_id = _seed()
        claimed = mt._claim(source_id)
        assert claimed is not None and claimed.lease_token
        token_a = claimed.lease_token
        with TestSession() as db:
            hm.forget_source(db, "owner-1", source_id)
            db.commit()
        # stale worker 的一切转移都必须失败，行保持 forgetting
        assert not mt._defer(source_id, token_a, 10)
        assert mt._record_failure(source_id, token_a, "late") == "taken_over"
        assert not mt._extend_lease(source_id, token_a)
        assert not mt._finalize_synced(source_id, token_a, "op", "doc")
        assert _row(source_id).status == "forgetting"


class TestDrain:
    def test_drain_dispatch_only_no_network(self, monkeypatch):
        pending_id = _seed()
        stuck_id = _seed(status="processing", lease_expires_at=None)
        forgetting_id = _seed(status="forgetting", document_id=None)
        # 等待窗内的 forgetting 不派发（防重复/防抢跑在途提交）
        held_id = _seed(status="forgetting", document_id=None)
        _hold_next_attempt(held_id, seconds=60)
        _age_row(pending_id, mt._DISPATCH_STALE_SECONDS + 5)

        def boom():
            raise AssertionError("drain 不得执行网络调用")

        monkeypatch.setattr(mt, "_client", boom)
        sent: list[tuple[str, str]] = []

        monkeypatch.setattr(
            mt, "_dispatch", lambda sid, token: sent.append((sid, token)) or True,
        )
        report = mt.drain_once()
        assert report["dispatched"] == 3
        assert {sid for sid, _ in sent} == {pending_id, stuck_id, forgetting_id}
        # 代次令牌先持久化再派发（Msg-Id 幂等键）
        with TestSession() as db:
            for sid, token in sent:
                assert db.get(SuperAssistantMemorySource, sid).dispatch_id == token

    def test_drain_skips_fresh_pending(self, monkeypatch):
        _seed()  # 刚入队（updated_at 新鲜），首发由调用方负责
        sent: list[str] = []
        monkeypatch.setattr(mt, "_dispatch", lambda sid, token: sent.append(sid) or True)
        assert mt.drain_once()["dispatched"] == 0

    def test_poll_deadline_bounds_http_timeout(self):
        client = FakeClient(op_status="processing")
        seen: list[float | None] = []

        def get_operation(bank_id, op_id, *, timeout=None):
            seen.append(timeout)
            return {"status": "processing"}

        client.get_operation = get_operation
        deadline = time.monotonic() + 0.3
        outcome = mt._poll_operation(client, "bank", "op", deadline)
        assert outcome == "pending"
        assert seen and all(
            t is not None and t <= mt._POLL_HTTP_TIMEOUT_SECONDS for t in seen
        )
