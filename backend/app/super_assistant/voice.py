"""超级助手语音听写端点（独立子路由）：与 search/multica 同前缀同守卫。

浏览器录音（16-bit PCM WAV 原始字节）上传后由 voice_service 在本地
完成 SenseVoice 离线转写；音频不出服务器，无任何云端依赖。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

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

_WAV_CONTENT_TYPES = {"audio/wav", "audio/x-wav", "audio/wave", "application/octet-stream"}
_LANGUAGE_PATTERN = "^(auto|zh|en|ja|ko|yue)$"


@router.post("/voice/transcribe", response_model=VoiceTranscriptionOut)
async def voice_transcribe(
    request: Request,
    language: str = Query(default="auto", pattern=_LANGUAGE_PATTERN),
    current_user: User = Depends(get_current_user),
) -> VoiceTranscriptionOut:
    # Content-Length 预检：超限直接 413，不读入大包体。
    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            declared_bytes = int(declared_length)
        except ValueError:
            raise HTTPException(status_code=400, detail="Content-Length 非法") from None
        if declared_bytes > voice_service.max_body_bytes():
            raise HTTPException(
                status_code=413,
                detail=f"音频超过大小上限 {voice_service.max_body_bytes() // (1024 * 1024)}MB",
            )

    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type and content_type not in _WAV_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Content-Type 需为 audio/wav，收到 {content_type!r}",
        )

    payload = await request.body()
    try:
        result = voice_service.transcribe(payload, language=language)
    except (BodyTooLargeError, AudioTooLongError) as error:
        raise HTTPException(status_code=413, detail=str(error)) from error
    except (WavFormatError, UnsupportedLanguageError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ModelNotReadyError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except TranscriptionFailedError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
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
        model_dir=state.model_dir,
        model_file=state.model_file,
        languages=list(state.languages),
        sample_rate=state.sample_rate,
        max_duration_seconds=state.max_duration_seconds,
        max_body_bytes=state.max_body_bytes,
    )
