"""超级助手语音听写端点（独立子路由）：与 search/multica 同前缀同守卫。

浏览器录音（16-bit PCM WAV 原始字节）上传后由 voice_service 在本地
完成 SenseVoice 离线转写；音频不出服务器，无任何云端依赖。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool

from app.auth.models import User
from app.deps import get_current_user
from app.super_assistant import voice_service
from app.super_assistant.schemas import VoiceStatusOut, VoiceTranscriptionOut
from app.super_assistant.voice_service import (
    AudioTooLongError,
    BodyTooLargeError,
    ModelNotReadyError,
    TranscriptionFailedError,
    UnsupportedLanguageError,
    WavFormatError,
)

router = APIRouter()

logger = logging.getLogger("app.super_assistant.voice")

_WAV_CONTENT_TYPES = {"audio/wav", "audio/x-wav", "audio/wave", "application/octet-stream"}
_LANGUAGE_PATTERN = "^(auto|zh|en|ja|ko|yue)$"


async def read_limited_body(request: Request, limit: int) -> bytes:
    """读取请求体并硬性限制字节数。

    Content-Length 只作快速预检：chunked 请求没有该头，必须边读边累计，
    超限立即中止，避免任意大 body 全量进内存。
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_bytes = int(declared)
        except ValueError:
            raise HTTPException(status_code=400, detail="Content-Length 非法") from None
        if declared_bytes > limit:
            raise HTTPException(
                status_code=413,
                detail=f"音频超过大小上限 {limit // (1024 * 1024)}MB",
            )

    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > limit:
            raise HTTPException(
                status_code=413,
                detail=f"音频超过大小上限 {limit // (1024 * 1024)}MB",
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/voice/transcribe", response_model=VoiceTranscriptionOut)
async def voice_transcribe(
    request: Request,
    language: str = Query(default="auto", pattern=_LANGUAGE_PATTERN),
    current_user: User = Depends(get_current_user),
) -> VoiceTranscriptionOut:
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type and content_type not in _WAV_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Content-Type 需为 audio/wav，收到 {content_type!r}",
        )

    payload = await read_limited_body(request, voice_service.max_body_bytes())
    try:
        # 转写含 CPU 密集解码与推理（秒级），必须让出事件循环，
        # 否则会冻结同进程内全部聊天 SSE 流
        result = await run_in_threadpool(voice_service.transcribe, payload, language=language)
    except (BodyTooLargeError, AudioTooLongError) as error:
        raise HTTPException(status_code=413, detail=str(error)) from error
    except (WavFormatError, UnsupportedLanguageError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ModelNotReadyError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except TranscriptionFailedError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    # 排障观测：只记长度与时长，不落转写内容（语音属用户隐私）
    logger.info(
        "transcribed user=%s language=%s duration_ms=%s body_bytes=%s text_chars=%s",
        current_user.id,
        result.language,
        result.duration_ms,
        len(payload),
        len(result.text),
    )
    return VoiceTranscriptionOut(
        text=result.text,
        duration_ms=result.duration_ms,
        language=result.language,
    )


@router.get("/voice/status", response_model=VoiceStatusOut)
def voice_status(current_user: User = Depends(get_current_user)) -> VoiceStatusOut:
    state = voice_service.status()
    return VoiceStatusOut(
        ready=state.ready,
        model_present=state.model_present,
        model_file=state.model_file,
        languages=list(state.languages),
        sample_rate=state.sample_rate,
        max_duration_seconds=state.max_duration_seconds,
        max_body_bytes=state.max_body_bytes,
    )
