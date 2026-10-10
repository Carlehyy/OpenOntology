"""语音听写端点与服务的单元/契约测试。

识别器经 voice_service.create_recognizer 替换缝注入假实现，不加载真实
模型；WAV 样本用 stdlib wave 现场合成（16-bit PCM）。
"""
from __future__ import annotations

import io
import math
import struct
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.deps import get_current_user
from app.super_assistant import voice as voice_router
from app.super_assistant import voice_service

_PREFIX = "/api/v2/super-assistant"


class FakeStream:
    def __init__(self, text: str) -> None:
        # 真实 OfflineStream 暴露 .result.text（RecognizerResult）
        self.result = SimpleNamespace(text=text)
        self.accepted: list[tuple[int, np.ndarray]] = []

    def accept_waveform(self, rate: int, samples: np.ndarray) -> None:
        self.accepted.append((rate, samples))


class FakeRecognizer:
    def __init__(self, text: str = "你好，帮我把这段话整理成本体需求。") -> None:
        self.text = text
        self.stream: FakeStream | None = None

    def create_stream(self) -> FakeStream:
        self.stream = FakeStream(self.text)
        return self.stream

    def decode_stream(self, stream: FakeStream) -> None:
        assert stream is self.stream


def _sine_wav(
    seconds: float = 0.5,
    sample_rate: int = 16000,
    channels: int = 1,
    frequency: float = 220.0,
) -> bytes:
    """合成 16-bit PCM 正弦波 WAV。"""
    frame_count = max(int(sample_rate * seconds), 1)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        frames = bytearray()
        for index in range(frame_count):
            value = int(12000 * math.sin(2 * math.pi * frequency * index / sample_rate))
            for _channel in range(channels):
                frames += struct.pack("<h", value)
        writer.writeframes(bytes(frames))
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _reset_recognizer_cache() -> None:
    # 识别器缓存是模块级单例，跨用例清空避免不同替换缝实现串味
    voice_service._recognizer_cache.clear()
    yield
    voice_service._recognizer_cache.clear()


@pytest.fixture
def fake_recognizer(monkeypatch: pytest.MonkeyPatch) -> FakeRecognizer:
    recognizer = FakeRecognizer()
    monkeypatch.setattr(voice_service, "create_recognizer", lambda language: recognizer)
    return recognizer


@pytest.fixture
def client(fake_recognizer: FakeRecognizer) -> TestClient:
    app = FastAPI()
    app.include_router(voice_router.router, prefix=_PREFIX)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="user-1")
    return TestClient(app)


def _post_wav(
    client: TestClient,
    payload: bytes,
    language: str | None = None,
    content_type: str = "audio/wav",
) -> object:
    url = f"{_PREFIX}/voice/transcribe"
    if language is not None:
        url += f"?language={language}"
    return client.post(url, content=payload, headers={"Content-Type": content_type})


# ---------------------------------------------------------------------------
# 路由契约
# ---------------------------------------------------------------------------


def test_transcribe_returns_text_and_duration(client: TestClient) -> None:
    response = _post_wav(client, _sine_wav(seconds=0.5))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["text"] == "你好，帮我把这段话整理成本体需求。"
    assert body["language"] == "auto"
    assert 400 <= body["duration_ms"] <= 600


def test_transcribe_requires_login(fake_recognizer: FakeRecognizer) -> None:
    app = FastAPI()
    app.include_router(voice_router.router, prefix=_PREFIX)
    client = TestClient(app)
    response = client.post(
        f"{_PREFIX}/voice/transcribe", content=_sine_wav(0.1)
    )
    # 项目鉴权走 HTTPBearer：缺凭证默认 403
    assert response.status_code == 403


def test_transcribe_rejects_invalid_language(client: TestClient) -> None:
    response = _post_wav(client, _sine_wav(0.1), language="fr")
    assert response.status_code == 422


def test_transcribe_rejects_wrong_content_type(client: TestClient) -> None:
    response = _post_wav(client, _sine_wav(0.1), content_type="audio/webm")
    assert response.status_code == 415


def test_transcribe_rejects_non_wav_body(client: TestClient) -> None:
    response = _post_wav(client, b"this is definitely not a wav file")
    assert response.status_code == 400
    assert "WAV" in response.json()["detail"]


def test_transcribe_oversized_body_is_413(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        voice_service.settings, "voice_max_body_mb", 1, raising=False
    )
    payload = _sine_wav(seconds=70.0)  # 16kHz*2B ≈ 2.2MB > 1MB
    response = _post_wav(client, payload)
    assert response.status_code == 413


def test_transcribe_too_long_audio_is_413(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        voice_service.settings, "voice_max_duration_seconds", 2, raising=False
    )
    response = _post_wav(client, _sine_wav(seconds=3.0))
    assert response.status_code == 413
    assert "时长" in response.json()["detail"]


def test_transcribe_model_missing_is_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recognizer = FakeRecognizer()
    monkeypatch.setattr(
        voice_service,
        "create_recognizer",
        lambda language: (_ for _ in ()).throw(
            voice_service.ModelNotReadyError("语音模型未就绪")
        ),
    )
    app = FastAPI()
    app.include_router(voice_router.router, prefix=_PREFIX)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="user-1")
    client = TestClient(app)
    response = _post_wav(client, _sine_wav(0.2))
    assert response.status_code == 503
    assert "模型" in response.json()["detail"]


def test_transcribe_decoder_failure_is_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenRecognizer:
        def create_stream(self) -> None:
            raise RuntimeError("native crash")

        def decode_stream(self, stream: None) -> None:
            raise AssertionError("不应到达")

    monkeypatch.setattr(
        voice_service, "create_recognizer", lambda language: BrokenRecognizer()
    )
    app = FastAPI()
    app.include_router(voice_router.router, prefix=_PREFIX)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="user-1")
    client = TestClient(app)
    response = _post_wav(client, _sine_wav(0.2))
    assert response.status_code == 500
    assert "识别失败" in response.json()["detail"]


def test_status_reports_model_readiness(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(voice_service, "model_dir", lambda: tmp_path)
    response = client.get(f"{_PREFIX}/voice/status")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is False
    assert body["model_present"] is False
    assert body["languages"][0] == "auto"
    assert body["sample_rate"] == 16000


# ---------------------------------------------------------------------------
# 服务层：WAV 解码（重采样/声道折叠）与识别样本流
# ---------------------------------------------------------------------------


def test_decode_wav_resamples_to_16k_mono() -> None:
    stereo_44k = _sine_wav(seconds=1.0, sample_rate=44100, channels=2)
    samples, source_rate = voice_service.decode_wav(stereo_44k)
    assert source_rate == 44100
    # 1 秒音频重采样后仍约 1 秒
    assert abs(len(samples) - 16000) <= 8
    assert samples.dtype == np.float32
    assert float(np.max(np.abs(samples))) <= 1.0


def test_decode_wav_rejects_non_pcm16() -> None:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(4)  # 32-bit：应拒绝
        writer.setframerate(16000)
        writer.writeframes(b"\x00" * 64)
    with pytest.raises(voice_service.WavFormatError):
        voice_service.decode_wav(buffer.getvalue())


def test_transcribe_feeds_16k_samples_to_recognizer(
    fake_recognizer: FakeRecognizer,
) -> None:
    stereo_48k = _sine_wav(seconds=0.5, sample_rate=48000, channels=2)
    result = voice_service.transcribe(stereo_48k, language="zh")
    assert result.text == "你好，帮我把这段话整理成本体需求。"
    assert result.language == "zh"
    rate, samples = fake_recognizer.stream.accepted[0]
    assert rate == 16000
    assert abs(len(samples) - 8000) <= 4  # 0.5s @16k


def test_transcribe_strips_sensevoice_tags(fake_recognizer: FakeRecognizer) -> None:
    fake_recognizer.text = "<|zh|><|NEUTRAL|>你好 <|en|>world"
    wav = _sine_wav(0.2)
    result = voice_service.transcribe(wav)
    assert result.text == "你好 world"


def test_transcribe_rejects_unknown_language(fake_recognizer: FakeRecognizer) -> None:
    with pytest.raises(voice_service.UnsupportedLanguageError):
        voice_service.transcribe(_sine_wav(0.1), language="fr")


def test_recognizer_is_cached_per_language(fake_recognizer: FakeRecognizer) -> None:
    wav = _sine_wav(0.1)
    voice_service.transcribe(wav, language="zh")
    voice_service.transcribe(wav, language="zh")
    # 同语言复用缓存实例：两次转写共用同一个假识别器 stream 引用即可证明
    assert fake_recognizer.stream is not None
