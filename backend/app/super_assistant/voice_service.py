"""超级助手语音听写：录音 WAV 上传后的本地 SenseVoice 离线转写。

识别完全在进程内完成（sherpa-onnx + SenseVoice Small int8，模型经
scripts/download_voice_model.py 显式下载并做 sha256 校验），音频不出
服务器。模型未就绪时 fail-closed：transcribe 返回 503 并附指引，绝不
后台静默下载。

资源纪律：识别器常驻内存约 0.9GB、每路并发解码再增数百 MB，且转写
与全平台同步端点共享线程池——必须经 inflight 闸门限流（默认 2 路），
超限立即 429，不做排队。
"""
from __future__ import annotations

import io
import re
import threading
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.shared.config import settings

# SenseVoice 原生语种；auto 为模型自动判别（中英混合场景推荐）。
SENSEVOICE_LANGUAGES = ("auto", "zh", "en", "ja", "ko", "yue")

TARGET_SAMPLE_RATE = 16000

_MODEL_REPO = "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
_MODEL_FILE = "model.int8.onnx"
_TOKENS_FILE = "tokens.txt"
# 与 scripts/download_voice_model.py 的 sha256 钉死值配套的字节数；
# 就绪判定同时校验尺寸，磁盘满/误截断的坏文件不谎报 ready
_MODEL_FILE_SIZE = 239_233_841
_TOKENS_FILE_SIZE = 315_894

# SenseVoice 输出可能携带 <|zh|><|NEUTRAL|> 等语种/情感/事件标记，
# sherpa-onnx 新版已剥离，这里兜底再清一次（幂等）。
_TAG_PATTERN = re.compile(r"<\|[^|]*\|>")


class VoiceServiceError(Exception):
    """语音域错误基类：路由层负责映射为对应 HTTP 状态码。"""


class WavFormatError(VoiceServiceError):
    """WAV 无法解码（非 WAV、非 PCM16、零样本、样本区畸形等）。"""


class AudioTooLongError(VoiceServiceError):
    """音频时长超过上限。"""


class BodyTooLargeError(VoiceServiceError):
    """请求体超过上限。"""


class UnsupportedLanguageError(VoiceServiceError):
    """language 不在 SenseVoice 支持列表。"""


class ModelNotReadyError(VoiceServiceError):
    """模型文件缺失或损坏：需要（重新）执行下载脚本。"""


class TranscriptionBusyError(VoiceServiceError):
    """并发转写超过闸门上限：立即拒绝，不排队。"""


class TranscriptionFailedError(VoiceServiceError):
    """识别器解码阶段失败。"""


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    duration_ms: int
    language: str


@dataclass(frozen=True)
class VoiceStatus:
    ready: bool
    model_present: bool
    model_file: str
    languages: tuple[str, ...]
    sample_rate: int
    max_duration_seconds: int
    max_body_bytes: int


def model_dir() -> Path:
    """模型目录：VOICE_MODEL_DIR 显式优先，空值回退仓库内 voice-models/。"""
    configured = settings.voice_model_dir.strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "voice-models"


def max_body_bytes() -> int:
    return settings.voice_max_body_mb * 1024 * 1024


def model_present() -> bool:
    model_path = model_dir() / _MODEL_FILE
    tokens_path = model_dir() / _TOKENS_FILE
    if not model_path.is_file() or not tokens_path.is_file():
        return False
    return (
        model_path.stat().st_size == _MODEL_FILE_SIZE
        and tokens_path.stat().st_size == _TOKENS_FILE_SIZE
    )


# ---------------------------------------------------------------------------
# WAV 解码：仅接受 PCM WAV（前端 recorder 统一产出 16-bit PCM），负责
# 声道折叠；时长校验在重采样之前按原始采样率完成——恶意低采样率 WAV
# 若先重采样会触发无上界内存分配（如 100Hz 声明可使 8MB 包体膨胀出数
# 亿样本），必须先拒后转。
# ---------------------------------------------------------------------------


def decode_wav(payload: bytes) -> tuple[np.ndarray, int]:
    """解码 PCM WAV 为 (原始采样率下的 float32 单声道, 采样率)。"""
    try:
        with wave.open(io.BytesIO(payload), "rb") as reader:
            channels = reader.getnchannels()
            sample_width = reader.getsampwidth()
            sample_rate = reader.getframerate()
            frames = reader.readframes(reader.getnframes())
    except (wave.Error, EOFError) as error:
        # CPython wave 对截断文件抛 EOFError 而非 wave.Error
        raise WavFormatError(f"无法解析 WAV 音频：{error}") from error

    if sample_width != 2:
        raise WavFormatError(f"仅支持 16-bit PCM WAV，收到 {sample_width * 8}-bit")
    if sample_rate <= 0:
        raise WavFormatError("WAV 采样率非法")
    if not frames:
        raise WavFormatError("WAV 不含音频样本")

    try:
        samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
    except ValueError as error:
        # data 区字节数与声道/位宽声明不匹配（畸形/截断包体）
        raise WavFormatError(f"WAV 样本区与头部声明不一致：{error}") from error

    if not np.isfinite(samples).all():
        raise WavFormatError("WAV 含非法采样值")
    return samples.astype(np.float32, copy=False), sample_rate


def resample_to_16k(samples: np.ndarray, source_rate: int) -> np.ndarray:
    """线性重采样到 16kHz（与前端 recorder 的纯 DSP 同一算法）。"""
    if source_rate == TARGET_SAMPLE_RATE or samples.size == 0:
        return samples
    count = int(round(samples.size * TARGET_SAMPLE_RATE / source_rate))
    # 双保险：输出样本数绝不超过时长上限对应的 16k 样本量
    count = min(max(count, 1), settings.voice_max_duration_seconds * TARGET_SAMPLE_RATE)
    positions = np.arange(count, dtype=np.float32) * source_rate / TARGET_SAMPLE_RATE
    left = np.floor(positions).astype(np.int64)
    right = np.minimum(left + 1, samples.size - 1)
    weight = positions - left
    return (samples[left] * (1.0 - weight) + samples[right] * weight).astype(np.float32, copy=False)


# ---------------------------------------------------------------------------
# 识别器：按 language 惰性构建并缓存最近一个实例（常驻内存约 0.9GB，
# 不做多语种并存）。create_recognizer 是测试替换缝。
# ---------------------------------------------------------------------------

_recognizer_lock = threading.Lock()
_recognizer_cache: dict[str, object] = {}


def create_recognizer(language: str) -> object:
    """构建 SenseVoice 离线识别器；模型缺失/损坏时 fail-closed。"""
    import sherpa_onnx

    directory = model_dir()
    model_path = directory / _MODEL_FILE
    tokens_path = directory / _TOKENS_FILE
    if not model_path.is_file() or not tokens_path.is_file():
        raise ModelNotReadyError(
            "语音模型未就绪：请先在服务器执行 "
            "python scripts/download_voice_model.py 下载（sha256 校验通过后生效）"
        )
    try:
        return sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model_path),
            tokens=str(tokens_path),
            language=language,
            use_itn=True,
        )
    except VoiceServiceError:
        raise
    except Exception as error:
        # 文件存在但解析失败（磁盘满写入/误截断/版本不符）：
        # 归为未就绪而非 500，给用户重新下载的指引
        raise ModelNotReadyError(
            "语音模型文件校验失败：请重新执行 python scripts/download_voice_model.py 下载"
        ) from error


def _get_recognizer(language: str) -> object:
    # 快路径：dict 读在 GIL 下安全，命中时不必碰锁——
    # 否则换语种触发的模型加载（秒级、持锁）会拖住所有缓存命中者
    cached = _recognizer_cache.get(language)
    if cached is not None:
        return cached
    with _recognizer_lock:
        cached = _recognizer_cache.get(language)
        if cached is not None:
            return cached
        recognizer = create_recognizer(language)
        # 仅保留最近一个语种的识别器，切换语种时旧实例自然释放。
        _recognizer_cache.clear()
        _recognizer_cache[language] = recognizer
        return recognizer


# ---------------------------------------------------------------------------
# 并发闸：实时读 settings（测试可 monkeypatch 上限），超限立即拒绝。
# 识别器常驻 ~0.9GB、每路解码再 +数百 MB，且转写与全平台同步端点共享
# 线程池——40 路并发会 OOM 并饿死其它接口，默认 2 路。
# ---------------------------------------------------------------------------

_inflight_lock = threading.Lock()
_inflight_count = 0


class _InflightSlot:
    """非阻塞占用一个转写名额；acquire 失败立即抛 Busy。"""

    def __enter__(self) -> "_InflightSlot":
        global _inflight_count
        with _inflight_lock:
            if _inflight_count >= settings.voice_max_concurrent:
                raise TranscriptionBusyError(
                    f"语音识别繁忙（并发上限 {settings.voice_max_concurrent}），请稍后重试"
                )
            _inflight_count += 1
        return self

    def __exit__(self, *exc: object) -> None:
        global _inflight_count
        with _inflight_lock:
            _inflight_count -= 1


def transcribe(payload: bytes, language: str = "auto") -> TranscriptionResult:
    """把一段 16-bit PCM WAV 转写为文本。"""
    language = (language or "auto").strip().lower()
    if language not in SENSEVOICE_LANGUAGES:
        raise UnsupportedLanguageError(
            f"不支持的语言 {language!r}，可选：{', '.join(SENSEVOICE_LANGUAGES)}"
        )
    if len(payload) > max_body_bytes():
        raise BodyTooLargeError(
            f"音频超过大小上限 {settings.voice_max_body_mb}MB"
        )

    with _InflightSlot():
        samples, source_rate = decode_wav(payload)
        # 时长校验必须在重采样之前：按原始采样率计算零额外分配
        duration_seconds = samples.size / source_rate
        if duration_seconds > settings.voice_max_duration_seconds:
            raise AudioTooLongError(
                f"音频时长 {duration_seconds:.0f}s 超过上限 "
                f"{settings.voice_max_duration_seconds}s"
            )

        if source_rate != TARGET_SAMPLE_RATE:
            samples = resample_to_16k(samples, source_rate)

        try:
            recognizer = _get_recognizer(language)
            stream = recognizer.create_stream()
            stream.accept_waveform(TARGET_SAMPLE_RATE, samples)
            recognizer.decode_stream(stream)
            raw_text = str(stream.result.text or "")
        except VoiceServiceError:
            raise
        except Exception as error:  # sherpa-onnx 原生异常统一收口
            # 原文只进异常链（服务端日志），不透传客户端（可能含模型路径）
            raise TranscriptionFailedError("语音识别失败，请重试") from error

    text = _TAG_PATTERN.sub("", raw_text).strip()
    return TranscriptionResult(
        text=text,
        duration_ms=int(round(duration_seconds * 1000)),
        language=language,
    )


def status() -> VoiceStatus:
    present = model_present()
    return VoiceStatus(
        ready=present,
        model_present=present,
        model_file=_MODEL_FILE,
        languages=SENSEVOICE_LANGUAGES,
        sample_rate=TARGET_SAMPLE_RATE,
        max_duration_seconds=settings.voice_max_duration_seconds,
        max_body_bytes=max_body_bytes(),
    )
