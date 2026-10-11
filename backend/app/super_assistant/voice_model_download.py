"""语音模型在线下载（cc-haha 式：管理端显式触发 + 进度可视 + 可取消）。

与 scripts/download_voice_model.py 共用同一组 sha256/字节数钉死值；在线
路径在后台守护线程执行，状态快照供 /voice/status 轮询。绝不静默下载：
只有管理员显式调用 start 才会发起。下载源 base 由服务端环境变量固定
（VOICE_MODEL_DOWNLOAD_BASE，国内可配 hf-mirror），不接受客户端传 URL。
"""
from __future__ import annotations

import hashlib
import threading
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from app.shared.config import settings
from app.super_assistant.voice_service import model_dir

# 与 scripts/download_voice_model.py 的钉死值保持一致（哈希取自
# HuggingFace LFS 元数据，2026-10-11 核验）
_MODEL_REPO = "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
FILES: dict[str, tuple[str, int]] = {
    "model.int8.onnx": (
        "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51",
        239_233_841,
    ),
    "tokens.txt": (
        "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc",
        315_894,
    ),
}

_STATE_IDLE = "idle"
_STATE_RUNNING = "running"
_STATE_DONE = "done"
_STATE_FAILED = "failed"
_STATE_CANCELLED = "cancelled"


@dataclass
class DownloadSnapshot:
    state: str = _STATE_IDLE
    received_bytes: int = 0
    total_bytes: int = sum(size for _, size in FILES.values())
    current_file: str | None = None
    error: str | None = None


@dataclass
class _DownloadRuntime:
    lock: threading.Lock = field(default_factory=threading.Lock)
    snapshot: DownloadSnapshot = field(default_factory=DownloadSnapshot)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


_runtime = _DownloadRuntime()


def snapshot() -> DownloadSnapshot:
    with _runtime.lock:
        return DownloadSnapshot(**_runtime.snapshot.__dict__)


def start() -> bool:
    """启动后台下载；已在进行中返回 False（不重复起线程）。"""
    with _runtime.lock:
        if _runtime.snapshot.state == _STATE_RUNNING:
            return False
        _runtime.snapshot = DownloadSnapshot(state=_STATE_RUNNING)
        _runtime.cancel_event = threading.Event()
        _runtime.thread = threading.Thread(
            target=_run, name="voice-model-download", daemon=True
        )
        _runtime.thread.start()
        return True


def cancel() -> bool:
    """请求取消；正在运行时返回 True。"""
    with _runtime.lock:
        running = _runtime.snapshot.state == _STATE_RUNNING
    if running:
        _runtime.cancel_event.set()
    return running


def _progress(current_file: str, received: int) -> None:
    with _runtime.lock:
        _runtime.snapshot.current_file = current_file
        _runtime.snapshot.received_bytes = received


def _finish(state: str, error: str | None = None) -> None:
    with _runtime.lock:
        _runtime.snapshot.state = state
        _runtime.snapshot.error = error
        _runtime.snapshot.current_file = None


def _download_one(url: str, dest: Path, expected_sha256: str, expected_size: int,
                  received_before: int) -> None:
    digest = hashlib.sha256()
    downloaded = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    part_file = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=60) as response, part_file.open("wb") as out:
        while True:
            if _runtime.cancel_event.is_set():
                part_file.unlink(missing_ok=True)
                _finish(_STATE_CANCELLED)
                raise _Cancelled()
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            downloaded += len(chunk)
            digest.update(chunk)
            out.write(chunk)
            _progress(dest.name, received_before + downloaded)
    if downloaded != expected_size:
        part_file.unlink(missing_ok=True)
        raise ValueError(f"{dest.name} 大小不符：期望 {expected_size}，实收 {downloaded}")
    actual = digest.hexdigest()
    if actual != expected_sha256:
        part_file.unlink(missing_ok=True)
        raise ValueError(f"{dest.name} sha256 校验失败：{actual}")
    part_file.replace(dest)


class _Cancelled(Exception):
    pass


def _run() -> None:
    base = settings.voice_model_download_base.rstrip("/")
    received = 0
    try:
        for name, (expected_sha256, expected_size) in FILES.items():
            # 已校验通过的文件跳过（幂等重试不重复拉 228MB）
            dest = model_dir() / name
            if dest.is_file() and dest.stat().st_size == expected_size:
                received += expected_size
                _progress(name, received)
                continue
            _download_one(
                f"{base}/{_MODEL_REPO}/resolve/main/{name}",
                dest,
                expected_sha256,
                expected_size,
                received,
            )
            received += expected_size
        _finish(_STATE_DONE)
    except _Cancelled:
        return
    except Exception as error:  # 网络中断/磁盘满/校验失败统一落到快照
        _finish(_STATE_FAILED, str(error)[:200])
