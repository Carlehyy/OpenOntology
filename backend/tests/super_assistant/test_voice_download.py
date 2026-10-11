"""语音模型在线下载（管理端显式触发 + 进度/取消）的单元/契约测试。

下载线程经 monkeypatch 的 urllib 与小尺寸假 FILES 驱动，不触真实网络。
"""
from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.deps import get_current_user
from app.super_assistant import voice as voice_router
from app.super_assistant import voice_model_download

_PREFIX = "/api/v2/super-assistant"


class _FakeResponse:
    def __init__(self, payload: bytes, block_event: threading.Event | None = None) -> None:
        self._payload = payload
        self._offset = 0
        self._block_event = block_event

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if self._block_event is not None:
            # 模拟阻塞式网络读：被唤醒后持续吐数据块（非 EOF），
            # 让下载循环回到顶部的取消检查点
            self._block_event.wait(timeout=5)
            return b"z" * 1024
        chunk = self._payload[self._offset:self._offset + max(size, 1)]
        self._offset += len(chunk)
        return chunk


def _install_fake_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, corrupt: bool = False
) -> tuple[dict[str, bytes], dict[str, tuple[str, int]]]:
    payloads = {
        "model.int8.onnx": b"fake-model-" + b"x" * 64,
        "tokens.txt": b"fake-tokens-" + b"y" * 32,
    }
    files: dict[str, tuple[str, int]] = {}
    for name, payload in payloads.items():
        digest = hashlib.sha256(b"wrong" if corrupt else payload).hexdigest()
        files[name] = (digest, len(payload))
    monkeypatch.setattr(voice_model_download, "FILES", files)
    monkeypatch.setattr(voice_model_download, "model_dir", lambda: tmp_path)
    monkeypatch.setattr(
        voice_model_download.settings,
        "voice_model_download_base",
        "https://fake.local",
        raising=False,
    )
    return payloads, files


def _install_urlopen(
    monkeypatch: pytest.MonkeyPatch,
    payloads: dict[str, bytes],
    block_event: threading.Event | None = None,
) -> list[str]:
    requested: list[str] = []

    def fake_urlopen(url: str, timeout: int = 0) -> _FakeResponse:  # noqa: ARG001
        requested.append(url)
        name = url.rsplit("/", 1)[-1]
        return _FakeResponse(payloads[name], block_event)

    monkeypatch.setattr(voice_model_download.urllib.request, "urlopen", fake_urlopen)
    return requested


def _make_client(role: str) -> TestClient:
    app = FastAPI()
    app.include_router(voice_router.router, prefix=_PREFIX)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id="user-1", role=role
    )
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_runtime() -> None:
    voice_model_download._runtime.snapshot = voice_model_download.DownloadSnapshot()
    voice_model_download._runtime.cancel_event = threading.Event()
    yield
    # 等待可能仍在跑的下载线程退出，避免跨用例串扰
    thread = voice_model_download._runtime.thread
    if thread is not None and thread.is_alive():
        voice_model_download._runtime.cancel_event.set()
        thread.join(timeout=5)
    voice_model_download._runtime.snapshot = voice_model_download.DownloadSnapshot()
    voice_model_download._runtime.cancel_event = threading.Event()


def test_download_requires_admin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _install_fake_files(monkeypatch, tmp_path)
    client = _make_client(role="editor")
    response = client.post(f"{_PREFIX}/voice/model/download")
    assert response.status_code == 403
    assert client.post(f"{_PREFIX}/voice/model/download/cancel").status_code == 403


def test_download_happy_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payloads, files = _install_fake_files(monkeypatch, tmp_path)
    requested = _install_urlopen(monkeypatch, payloads)

    client = _make_client(role="admin")
    response = client.post(f"{_PREFIX}/voice/model/download")
    assert response.status_code == 202
    assert response.json()["started"] is True

    thread = voice_model_download._runtime.thread
    assert thread is not None
    thread.join(timeout=5)
    snapshot = voice_model_download.snapshot()
    assert snapshot.state == "done"
    assert snapshot.received_bytes == sum(size for _, size in files.values())
    for name, payload in payloads.items():
        assert (tmp_path / name).read_bytes() == payload
        assert not (tmp_path / f"{name}.part").exists()
    # 固定仓库路径 + 服务端配置的下载源（不接受客户端 URL）
    assert all(url.startswith("https://fake.local/") for url in requested)


def test_download_double_start_is_409(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payloads, _ = _install_fake_files(monkeypatch, tmp_path)
    block_event = threading.Event()
    _install_urlopen(monkeypatch, payloads, block_event)
    try:
        client = _make_client(role="admin")
        assert client.post(f"{_PREFIX}/voice/model/download").status_code == 202
        second = client.post(f"{_PREFIX}/voice/model/download")
        assert second.status_code == 409
        assert "进行中" in second.json()["detail"]
    finally:
        block_event.set()


def test_download_cancel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payloads, _ = _install_fake_files(monkeypatch, tmp_path)
    block_event = threading.Event()
    _install_urlopen(monkeypatch, payloads, block_event)
    try:
        client = _make_client(role="admin")
        assert client.post(f"{_PREFIX}/voice/model/download").status_code == 202
        assert client.post(f"{_PREFIX}/voice/model/download/cancel").json()["cancelled"] is True
        thread = voice_model_download._runtime.thread
        assert thread is not None
        thread.join(timeout=5)
        assert voice_model_download.snapshot().state == "cancelled"
        assert not list(tmp_path.glob("*.part"))
    finally:
        block_event.set()


def test_download_sha_mismatch_fails_with_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payloads, _ = _install_fake_files(monkeypatch, tmp_path, corrupt=True)
    _install_urlopen(monkeypatch, payloads)

    client = _make_client(role="admin")
    assert client.post(f"{_PREFIX}/voice/model/download").status_code == 202
    thread = voice_model_download._runtime.thread
    assert thread is not None
    thread.join(timeout=5)
    snapshot = voice_model_download.snapshot()
    assert snapshot.state == "failed"
    assert "sha256" in (snapshot.error or "")
    # 校验失败的半成品不落正式文件名
    assert not (tmp_path / "model.int8.onnx").exists()


def test_status_exposes_download_progress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_fake_files(monkeypatch, tmp_path)
    client = _make_client(role="admin")
    body = client.get(f"{_PREFIX}/voice/status").json()
    download = body["download"]
    assert download["state"] == "idle"
    assert download["total_bytes"] > 0
    assert body["model_present"] is False
