"""replica_receiver 聚焦测试（真实回环 HTTP + 签名 + SQLite 会话）。

被测契约：复用 cdc_receiver 交通（HMAC 签名/未知路径/换代 404/过期时间戳/
请求体上限/方法拒绝），batch 数组 1..2048 与 Decimal 精确解析，调用方事务
先 commit 后 2xx（durable-before-ack），全批失败回滚并以独立事务
mark_capture_error 落封闭错误码（409/503，不泄露原异常），旧注册代被
代际栅栏拒绝且不得篡改 capture，stop() 幂等彻底回收端口与线程。
ingest/mark 经 callable 注入替身；缺省 lazy-import 装配单独用 sys.modules
替身验证 —— 全文件不依赖 replica_ingest 真实实现的存在。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import socket
import sys
import threading
import time
import types
import uuid
from contextlib import contextmanager

import pytest
import sqlalchemy as sa
from decimal import Decimal
from sqlalchemy.orm import sessionmaker

from app.data_channel.connections.models import Connection
from app.data_channel.integrated.cdc_receiver import DEFAULT_MAX_REQUEST_BYTES
from app.data_channel.integrated.replica_capture_models import (
    IntegratedReplicaCapture,
)
from app.data_channel.integrated.replica_journal import MAX_BATCH_RECORDS
from app.data_channel.integrated.replica_receiver import (
    OBSOLETE_CAPTURE_GENERATION, UNKNOWN_INGEST_CODE,
    create_replica_receiver,
)

SECRET = b"\x23" * 32
INGEST_MODULE = "app.data_channel.integrated.replica_ingest"


class ReplicaIngestError(RuntimeError):
    """替身异常：与主会话 ReplicaIngestError 同契约（code/retryable）。"""

    def __init__(self, code: str, *, retryable: bool):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class IngestSpy:
    """替身 ingest：记录调用并推进 received_seq，使提交/回滚可观测。"""

    def __init__(self, error: Exception | None = None, result=None):
        self.calls: list[dict] = []
        self.error = error
        self.result = result

    def __call__(self, db, capture_id: str, records: list):
        self.calls.append(
            {"session": db, "capture_id": capture_id, "records": records})
        db.execute(sa.update(IntegratedReplicaCapture).where(
            IntegratedReplicaCapture.id == capture_id
        ).values(received_seq=IntegratedReplicaCapture.received_seq + len(records)))
        if self.error is not None:
            raise self.error
        return self.result


class MarkSpy:
    """替身 mark：记录调用并落 last_error_code，使独立事务可观测。"""

    def __init__(self, error: Exception | None = None):
        self.calls: list[dict] = []
        self.error = error

    def __call__(self, db, capture_id: str, code: str, *, retryable: bool):
        self.calls.append({
            "session": db, "capture_id": capture_id,
            "code": code, "retryable": retryable,
        })
        if self.error is not None:
            raise self.error
        db.execute(sa.update(IntegratedReplicaCapture).where(
            IntegratedReplicaCapture.id == capture_id
        ).values(last_error_code=code))


@pytest.fixture
def capture(db, admin_user):
    connection = Connection(
        id="replica-rx-source", name="Replica receiver fixture",
        kind="postgres", config={}, status="active", created_by=admin_user.id,
    )
    db.add(connection)
    db.flush()
    row = IntegratedReplicaCapture(
        id=str(uuid.uuid4()), connection_id=connection.id, generation=1,
        identity_fingerprint="fp", tables_json=[])
    db.add(row)
    db.commit()
    return row


def _receiver(db, *, ingest=None, mark=None, **overrides):
    options = dict(
        session_factory=sessionmaker(bind=db.get_bind()),
        host="127.0.0.1", port=0,
        ingest_batch=ingest, mark_capture_error=mark,
    )
    options.update(overrides)
    return create_replica_receiver(**options)


@contextmanager
def _running(receiver):
    try:
        yield receiver
    finally:
        receiver.stop()


def _record(ordinal: int, amount=0.1) -> dict:
    # amount 保持原始浮点 token（json.dumps 写出 0.1）：交通层
    # parse_float=Decimal 到达 ingest 时即为 Decimal("0.1")。
    return {"ordinal": ordinal, "payload": {"amount": amount}}


def _post(receiver, path: str, body: bytes, *, secret: bytes = SECRET,
          timestamp: str | None = None, signature: str | None = None,
          method: str = "POST"):
    message_id = f"msg-{uuid.uuid4()}"
    timestamp = timestamp if timestamp is not None else str(int(time.time()))
    if signature is None:
        signature = "v1," + base64.b64encode(hmac.new(
            secret, f"{message_id}.{timestamp}.".encode() + body,
            hashlib.sha256).digest()).decode()
    connection = http.client.HTTPConnection(
        receiver.host, receiver.port, timeout=10)
    try:
        connection.request(method, path, body=body, headers={
            "Content-Type": "application/json",
            "webhook-id": message_id,
            "webhook-timestamp": timestamp,
            "webhook-signature": signature,
        })
        response = connection.getresponse()
        raw = response.read()
        payload = json.loads(raw) if raw else {}
        return response.status, payload
    finally:
        connection.close()


def _capture_row(db, capture_id):
    # 接收端经独立会话提交/回滚后，先失效本会话缓存再断言真实落库状态。
    db.expire_all()
    return db.get(IntegratedReplicaCapture, capture_id)


def test_construction_and_registration_stay_decoupled_from_replica_ingest(
        db, capture, monkeypatch):
    monkeypatch.delitem(sys.modules, INGEST_MODULE, raising=False)
    ingest, mark = IngestSpy(), MarkSpy()
    with _running(_receiver(db, ingest=ingest, mark=mark)) as receiver:
        session = receiver.register(capture.id, 1, SECRET)
        assert session.path.endswith(f"/{capture.id}/1")
    assert INGEST_MODULE not in sys.modules
    assert ingest.calls == [] and mark.calls == []


def test_signed_batch_commits_before_ack_and_preserves_decimal(db, capture):
    ingest, mark = IngestSpy(), MarkSpy()
    with _running(_receiver(db, ingest=ingest, mark=mark)) as receiver:
        session = receiver.register(capture.id, capture.generation, SECRET)
        # 手工构造原始 token：1.10 与 0.1 的字面精度必须原样到达 ingest。
        body = (b'[{"ordinal": 1, "payload": {"amount": 0.1}},'
                b' {"ordinal": 2, "payload": {"amount": 0.1}},'
                b' {"ordinal": 3, "payload": {"amount": 1.10}}]')
        status, payload = _post(receiver, session.path, body)
        assert status == 200, payload
        assert payload == {"status": "committed"}
        assert receiver.last_accepted_at(capture.id, capture.generation)

    assert len(ingest.calls) == 1
    call = ingest.calls[0]
    assert call["capture_id"] == capture.id
    assert [r["ordinal"] for r in call["records"]] == [1, 2, 3]
    # 交通层 parse_float=Decimal：浮点 token 不经 double 损耗直达 ingest。
    assert isinstance(call["records"][0]["payload"]["amount"], Decimal)
    assert call["records"][0]["payload"]["amount"] == Decimal("0.1")
    assert call["records"][2]["payload"]["amount"] == Decimal("1.10")
    # durable-before-ack：2xx 返回后写入对新会话立即可见。
    durable = sessionmaker(bind=db.get_bind())()
    try:
        row = _capture_row(durable, capture.id)
        assert row.received_seq == 3
        assert row.last_error_code is None
    finally:
        durable.close()
    assert mark.calls == []


def test_default_lazy_import_wires_replica_ingest_contract(db, capture, monkeypatch):
    calls, marks = [], []

    def fake_ingest(session, capture_id, records):
        calls.append((capture_id, records))
        session.execute(sa.update(IntegratedReplicaCapture).where(
            IntegratedReplicaCapture.id == capture_id
        ).values(received_seq=IntegratedReplicaCapture.received_seq + len(records)))
        return {"status": "accepted", "accepted": len(records), "duplicates": 0}

    def fake_mark(session, capture_id, code, *, retryable):
        marks.append((capture_id, code, retryable))

    import app.data_channel.integrated as integrated

    substitute = types.SimpleNamespace(
        ingest_batch=fake_ingest, mark_capture_error=fake_mark)
    monkeypatch.setitem(sys.modules, INGEST_MODULE, substitute)
    # Other suites may already have imported the child module and cached it
    # on its package. Replace both import views so test order is irrelevant.
    monkeypatch.setattr(integrated, "replica_ingest", substitute, raising=False)
    factory = sessionmaker(bind=db.get_bind())
    with _running(create_replica_receiver(
            session_factory=factory, host="127.0.0.1", port=0)) as receiver:
        session = receiver.register(capture.id, capture.generation, SECRET)
        status, payload = _post(
            receiver, session.path, json.dumps([_record(7)]).encode())
        assert status == 200, payload
        assert payload == {"status": "accepted"}
    assert len(calls) == 1
    assert calls[0][0] == capture.id
    assert [record["ordinal"] for record in calls[0][1]] == [7]
    assert calls[0][1][0]["payload"]["amount"] == Decimal("0.1")
    assert marks == []
    durable = sessionmaker(bind=db.get_bind())()
    try:
        assert _capture_row(durable, capture.id).received_seq == 1
    finally:
        durable.close()


def test_transport_rejections_never_reach_ingest(db, capture):
    ingest, mark = IngestSpy(), MarkSpy()
    with _running(_receiver(db, ingest=ingest, mark=mark)) as receiver:
        session = receiver.register(capture.id, capture.generation, SECRET)
        body = json.dumps([_record(1)]).encode()

        # 错误密钥签名 → 401。
        status, payload = _post(receiver, session.path, body, secret=b"\x00" * 32)
        assert (status, payload) == (401, {"error": "unauthorized"})

        # 过期时间戳 → 401（沿用交通层容忍窗）。
        stale = str(int(time.time()) - 3600)
        status, payload = _post(receiver, session.path, body, timestamp=stale)
        assert (status, payload) == (401, {"error": "unauthorized"})

        # 未注册路径/未注册代 → 404。
        status, payload = _post(receiver, f"/cdc/{capture.id}/9", body)
        assert (status, payload) == (404, {"error": "not_found"})
        status, payload = _post(receiver, "/replica/unknown/1", body)
        assert (status, payload) == (404, {"error": "not_found"})

        # 非 POST 方法 → 405。
        status, payload = _post(receiver, session.path, body, method="GET")
        assert (status, payload) == (405, {"error": "method_not_allowed"})
    assert ingest.calls == []
    assert mark.calls == []
    assert _capture_row(db, capture.id).received_seq == 0


def test_reregistration_invalidates_old_generation_path(db, capture):
    ingest, mark = IngestSpy(), MarkSpy()
    capture.generation = 2
    db.commit()
    with _running(_receiver(db, ingest=ingest, mark=mark)) as receiver:
        stale_session = receiver.register(capture.id, 1, SECRET)
        session = receiver.register(capture.id, 2, SECRET)
        body = json.dumps([_record(1)]).encode()

        status, payload = _post(receiver, stale_session.path, body)
        assert (status, payload) == (404, {"error": "not_found"})
        # 换代清除旧代健康证据，新代必须自己证明。
        assert receiver.last_accepted_at(capture.id, 1) is None

        status, payload = _post(receiver, session.path, body)
        assert status == 200, payload
        assert receiver.last_accepted_at(capture.id, 2) is not None
    assert len(ingest.calls) == 1
    assert ingest.calls[0]["records"][0]["ordinal"] == 1


def test_stale_registration_cannot_tamper_with_current_capture(db, capture):
    ingest, mark = IngestSpy(), MarkSpy()
    # DB 已换代，但接收端仍只持有旧代注册（如 supervisor 换实例后的残留）。
    capture.generation = 2
    db.commit()
    with _running(_receiver(db, ingest=ingest, mark=mark)) as receiver:
        session = receiver.register(capture.id, 1, SECRET)
        status, payload = _post(
            receiver, session.path, json.dumps([_record(1)]).encode())
        assert (status, payload) == (409, {"error": OBSOLETE_CAPTURE_GENERATION})
    # 生命周期类拒绝：旧代既不写入，也不得经 mark_capture_error 篡改状态。
    assert ingest.calls == []
    assert mark.calls == []
    row = _capture_row(db, capture.id)
    assert row.received_seq == 0
    assert row.status == "starting"
    assert row.last_error_code is None


def test_batch_shape_bounds(db, capture):
    ingest, mark = IngestSpy(), MarkSpy()
    with _running(_receiver(db, ingest=ingest, mark=mark)) as receiver:
        session = receiver.register(capture.id, capture.generation, SECRET)

        status, payload = _post(receiver, session.path, b"[]")
        assert (status, payload) == (400, {"error": "bad_request"})
        status, payload = _post(receiver, session.path, b"{\"not\": \"array\"}")
        assert (status, payload) == (400, {"error": "bad_request"})

        oversized = json.dumps([_record(i) for i in range(MAX_BATCH_RECORDS + 1)])
        status, payload = _post(receiver, session.path, oversized.encode())
        assert (status, payload) == (400, {"error": "bad_request"})
        assert ingest.calls == []

        full = json.dumps([_record(i) for i in range(MAX_BATCH_RECORDS)])
        status, payload = _post(receiver, session.path, full.encode())
        assert status == 200, payload
    assert len(ingest.calls) == 1
    assert len(ingest.calls[0]["records"]) == MAX_BATCH_RECORDS
    assert _capture_row(db, capture.id).received_seq == MAX_BATCH_RECORDS
    assert mark.calls == []


def test_permanent_failure_rolls_back_marks_error_and_returns_409(db, capture):
    ingest = IngestSpy(error=ReplicaIngestError(
        "CAPTURE_NOT_ACCEPTING", retryable=False))
    mark = MarkSpy()
    with _running(_receiver(db, ingest=ingest, mark=mark)) as receiver:
        session = receiver.register(capture.id, capture.generation, SECRET)
        status, payload = _post(
            receiver, session.path, json.dumps([_record(1), _record(2)]).encode())
        assert (status, payload) == (409, {"error": "CAPTURE_NOT_ACCEPTING"})
    assert len(ingest.calls) == 1
    # 全批回滚：替身已推进 received_seq，但 2xx 未返回前一切归零。
    row = _capture_row(db, capture.id)
    assert row.received_seq == 0
    assert row.last_error_code == "CAPTURE_NOT_ACCEPTING"
    # mark 在独立事务执行：不同会话对象，且回滚后仍独立提交可见。
    assert len(mark.calls) == 1
    marked = mark.calls[0]
    assert marked["capture_id"] == capture.id
    assert marked["code"] == "CAPTURE_NOT_ACCEPTING"
    assert marked["retryable"] is False
    assert marked["session"] is not ingest.calls[0]["session"]


def test_transient_failure_rolls_back_and_returns_503(db, capture):
    ingest = IngestSpy(error=ReplicaIngestError(
        "REPLICA_BACKLOG_CAPACITY", retryable=True))
    mark = MarkSpy()
    with _running(_receiver(db, ingest=ingest, mark=mark)) as receiver:
        session = receiver.register(capture.id, capture.generation, SECRET)
        status, payload = _post(
            receiver, session.path, json.dumps([_record(1)]).encode())
        assert (status, payload) == (503, {"error": "unavailable"})
    assert _capture_row(db, capture.id).received_seq == 0
    assert len(mark.calls) == 1
    assert mark.calls[0]["code"] == "REPLICA_BACKLOG_CAPACITY"
    assert mark.calls[0]["retryable"] is True
    assert _capture_row(db, capture.id).last_error_code == (
        "REPLICA_BACKLOG_CAPACITY")


def test_unknown_exception_folds_to_closed_transient_code(db, capture):
    ingest = IngestSpy(error=RuntimeError(
        "boom: SELECT secret FROM credentials -- raw detail"))
    mark = MarkSpy()
    with _running(_receiver(db, ingest=ingest, mark=mark)) as receiver:
        session = receiver.register(capture.id, capture.generation, SECRET)
        status, payload = _post(
            receiver, session.path, json.dumps([_record(1)]).encode())
        assert (status, payload) == (503, {"error": "unavailable"})
        assert "boom" not in json.dumps(payload)
    assert _capture_row(db, capture.id).received_seq == 0
    assert len(mark.calls) == 1
    assert mark.calls[0]["code"] == UNKNOWN_INGEST_CODE
    assert mark.calls[0]["retryable"] is True
    assert _capture_row(db, capture.id).last_error_code == UNKNOWN_INGEST_CODE


def test_mark_failure_keeps_response_semantics(db, capture):
    ingest = IngestSpy(error=ReplicaIngestError(
        "SOURCE_IDENTITY_CHANGED", retryable=False))
    mark = MarkSpy(error=RuntimeError("mark store down"))
    with _running(_receiver(db, ingest=ingest, mark=mark)) as receiver:
        session = receiver.register(capture.id, capture.generation, SECRET)
        status, payload = _post(
            receiver, session.path, json.dumps([_record(1)]).encode())
        assert (status, payload) == (409, {"error": "SOURCE_IDENTITY_CHANGED"})
    assert len(mark.calls) == 1
    assert _capture_row(db, capture.id).received_seq == 0
    assert _capture_row(db, capture.id).last_error_code is None


def test_request_size_cap_is_16mib_and_enforced(db, capture):
    assert DEFAULT_MAX_REQUEST_BYTES == 16 * 1024 * 1024
    ingest, mark = IngestSpy(), MarkSpy()
    with _running(_receiver(
            db, ingest=ingest, mark=mark, max_request_bytes=256)) as receiver:
        session = receiver.register(capture.id, capture.generation, SECRET)
        status, payload = _post(receiver, session.path, b"x" * 300)
        assert (status, payload) == (413, {"error": "payload_too_large"})
    assert ingest.calls == []
    assert mark.calls == []
    assert _capture_row(db, capture.id).received_seq == 0


def test_stop_is_idempotent_and_releases_port_and_threads(db):
    receiver = create_replica_receiver(
        session_factory=sessionmaker(bind=db.get_bind()),
        host="127.0.0.1", port=0)
    try:
        receiver.register("cap-stop-probe", 1, SECRET)
        port = receiver.port
    finally:
        receiver.stop()
    receiver.stop()  # 幂等
    with pytest.raises(RuntimeError):
        receiver.register("cap-stop-probe", 2, SECRET)
    assert not receiver._thread.is_alive()
    probe = socket.socket()
    try:
        probe.settimeout(2)
        with pytest.raises(OSError):
            probe.connect(("127.0.0.1", port))
    finally:
        probe.close()
    assert not [thread for thread in threading.enumerate()
                if thread.name == "cdc-receiver-serve" and thread.is_alive()]
