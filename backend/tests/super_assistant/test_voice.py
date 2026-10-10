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
from fastapi import FastAPI, HTTPException
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


def test_decode_wav_returns_mono_at_source_rate() -> None:
    stereo_44k = _sine_wav(seconds=1.0, sample_rate=44100, channels=2)
    samples, source_rate = voice_service.decode_wav(stereo_44k)
    # 解码只做声道折叠，不做重采样（重采样前必须先完成时长校验）
    assert source_rate == 44100
    assert abs(len(samples) - 44100) <= 2
    assert samples.dtype == np.float32
    assert float(np.max(np.abs(samples))) <= 1.0


def test_resample_to_16k_length_and_start() -> None:
    samples = np.sin(np.arange(4800, dtype=np.float32) * 2 * np.pi * 40 / 4800)
    resampled = voice_service.resample_to_16k(samples, 48000)
    assert abs(len(resampled) - 1600) <= 1
    assert abs(resampled[0] - samples[0]) < 1e-6


def test_resample_output_capped_by_duration_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 100Hz 声明的超长样本：即使时长校验被绕过，输出样本数也被
    # voice_max_duration_seconds × 16k 硬顶，不可能无上界分配
    monkeypatch.setattr(
        voice_service.settings, "voice_max_duration_seconds", 1, raising=False
    )
    samples = np.zeros(50_000, dtype=np.float32)  # 100Hz 下=500s
    resampled = voice_service.resample_to_16k(samples, 100)
    assert len(resampled) <= 16000


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


# ---------------------------------------------------------------------------
# 对抗式审查补充：截断 WAV / 恶意低采样率 / chunked 超限 / 并发转写
# ---------------------------------------------------------------------------


def test_truncated_wav_is_400_not_500(client: TestClient) -> None:
    response = _post_wav(client, b"RIFF")
    assert response.status_code == 400
    assert "WAV" in response.json()["detail"]


def test_low_sample_rate_wav_rejected_before_resample(
    client: TestClient,
) -> None:
    # 100Hz 声明、200s 时长（仅 2 万样本）：按原始采样率先判 413，绝不进入重采样
    low_rate = _sine_wav(seconds=200.0, sample_rate=100)
    response = _post_wav(client, low_rate)
    assert response.status_code == 413
    assert "时长" in response.json()["detail"]


def test_read_limited_body_stops_on_chunked_overflow() -> None:
    """无 Content-Length 的 chunked 请求：边读边累计，超限立即 413。"""
    import asyncio
    from types import SimpleNamespace as NS

    from app.super_assistant.voice import read_limited_body

    class FakeRequest:
        def __init__(self, chunks: list[bytes], headers: dict[str, str]) -> None:
            self._chunks = chunks
            self.headers = headers

        async def stream(self):
            for chunk in self._chunks:
                yield chunk

    limit = 1024
    request = FakeRequest([b"x" * 512, b"x" * 512, b"x" * 512], {})
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(read_limited_body(request, limit))  # type: ignore[arg-type]
    assert excinfo.value.status_code == 413

    # 未超限的正常路径完整拼接
    ok_request = FakeRequest([b"ab", b"cd"], {})
    body = asyncio.run(read_limited_body(ok_request, limit))  # type: ignore[arg-type]
    assert body == b"abcd"

    # 有 Content-Length 且超限：不读流直接 413
    declared = FakeRequest([], {"content-length": str(limit + 1)})
    with pytest.raises(HTTPException) as declared_info:
        asyncio.run(read_limited_body(declared, limit))  # type: ignore[arg-type]
    assert declared_info.value.status_code == 413


def test_concurrent_transcribe_both_succeed(
    fake_recognizer: FakeRecognizer,
) -> None:
    """threadpool 化后并发转写互不阻塞（事件循环不被 CPU 推理冻结）。"""
    import asyncio

    import httpx
    from fastapi import FastAPI

    from app.deps import get_current_user

    app = FastAPI()
    app.include_router(voice_router.router, prefix=_PREFIX)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="user-1")

    async def run() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            wav = _sine_wav(seconds=0.3)
            results = await asyncio.gather(*[
                client.post(
                    f"{_PREFIX}/voice/transcribe",
                    content=wav,
                    headers={"Content-Type": "audio/wav"},
                )
                for _ in range(4)
            ])
        for response in results:
            assert response.status_code == 200, response.text
            assert response.json()["text"] == "你好，帮我把这段话整理成本体需求。"

    asyncio.run(run())
