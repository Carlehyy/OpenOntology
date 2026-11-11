"""Hindsight 记忆专用 OpenAI 兼容端点（机器鉴权网关）。

Hindsight 独立进程的全部 LLM 调用经本端点回平台模型网关：模型固定别名
``oo-memory-default``（compose 部署值；Hindsight 侧不按 bank 覆写 llm_model，
其 config PATCH 对静态字段返回 400，动态 owner 别名不可行）。网关解析唯一
「enabled 且已选型」的 SuperAssistantMemorySetting（无 → 503 配置缺失、
多条 → 503 歧义，均不任意选、不回退），owner 必须存在；复用 llm_gateway
的调用记录、脱敏错误与 usage；不接受请求侧的 model_config_id/base_url/api_key。
单用户产品已确认；平台鉴权与 bank 隔离由 Hindsight 侧保留。

挂载（main.py）：``include_router(memory_llm.router,
prefix="/api/v2/super-assistant")``。无会话鉴权，仅共享 Bearer token
（``settings.hindsight_service_token``，空值 = 端点禁用 503）；调用方仅
部署内网 Hindsight 服务。日志与 ModelCallLog 均不含请求正文与密钥。

请求面对齐 Hindsight 固定源码 openai_compatible_llm.call 的非流式面；
未知字段 422（extra=forbid）、不支持组合 400，绝不默默丢字段。解析失败
一律显式报错，不回退默认模型；api_base 指回本端点即拒绝（防模型回环）。
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
import uuid
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.auth.models import User
from app.deps import get_db
from app.model_configs.connectivity_service import safe_test_error
from app.model_configs.llm_gateway import (
    LLMError,
    _safe_error_message,
    chat as gateway_chat,
)
from app.model_configs.models import ModelConfig
from app.model_configs.selector import llm_call_kwargs
from app.shared.config import settings
from app.super_assistant.memory_models import SuperAssistantMemorySetting

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/memory/v1")

# 固定模型别名（compose 部署值）；唯一 enabled+已选型 MemorySetting 决定模型。
MEMORY_MODEL_ALIAS = "oo-memory-default"

# 初期仅支持 OpenAI 兼容协议 provider（anthropic 分支无结构化透传）。
ALLOWED_PROVIDERS = ("openai", "compatible")

# 并发闸门：探针与正式调用共用，超限立即快速失败不排队。
_MAX_CONCURRENT_CALLS = 4
_CALL_SEMAPHORE = threading.BoundedSemaphore(_MAX_CONCURRENT_CALLS)

# Hindsight 自带重试预算；网关层单次尝试，避免「Hindsight × 网关」重试相乘。
_GATEWAY_RETRY_MAX_ATTEMPTS = 1
_CALL_TIMEOUT_SECONDS = 120

# 请求大小闸门：messages 正文 + response_format（schema 可能很大）合计。
_MAX_MESSAGES = 256
_MAX_TOTAL_CONTENT_CHARS = 400_000

# 能力探针预算：样例答案只有 1 个 token，但推理模型把思维链计入
# max_tokens（glm-5.3 实测探针 reasoning ~112 token、总用量 119），64 会让
# 样例以 finish_reason=length 截断、被误判为不支持。512 给思维链留足余量
# 同时保持小样例；实测探针 <6s 完成，30s 超时无需放大。
_PROBE_MAX_TOKENS = 512
_PROBE_TIMEOUT_SECONDS = 30

_LOOP_BACK_PATH_MARK = "/internal/memory/"


def _error_response(status_code: int, message: str, err_type: str,
                    code: str | None = None, param: str | None = None) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={
        "error": {"message": message, "type": err_type, "param": param, "code": code},
    })


class _ExplicitBadRequest(Exception):
    """请求语义不支持（stream=true 等）——转 400，与 schema 422 区分。"""


class _ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["system", "user", "assistant"]
    content: str


class _ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field(min_length=1, max_length=128)
    messages: list[_ChatMessage] = Field(min_length=1, max_length=_MAX_MESSAGES)
    response_format: dict[str, Any] | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    seed: int | None = None
    max_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    max_completion_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    stream: bool | None = False


def _validate_response_format(value: dict[str, Any]) -> dict[str, Any]:
    """只放行 Hindsight 实际发送的两种结构化输出形态，其余显式拒绝。"""
    rf_type = value.get("type")
    if rf_type == "json_object":
        if set(value) - {"type"}:
            raise _ExplicitBadRequest(
                "response_format json_object 仅接受 type 字段，多余字段不被支持")
        return value
    if rf_type == "json_schema":
        inner = value.get("json_schema")
        if not isinstance(inner, dict) or not isinstance(inner.get("schema"), dict):
            raise _ExplicitBadRequest(
                "response_format json_schema 需要 json_schema.schema 对象")
        return value
    raise _ExplicitBadRequest(
        f"不支持的 response_format.type={rf_type!r}：仅支持 json_schema / json_object")


def _check_service_token(authorization: str | None) -> JSONResponse | None:
    """共享 token 校验；返回错误响应或 None（通过）。

    空配置 = 服务禁用；空 Bearer 一律拒绝，避免空 token 与空配置相等。
    """
    expected = str(settings.hindsight_service_token or "")
    if not expected:
        return _error_response(
            503, "memory llm 服务未启用（hindsight_service_token 未配置）",
            "server_error", "service_disabled")
    scheme, _, token = (authorization or "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or not secrets.compare_digest(
            token, expected):
        return _error_response(401, "无效的 memory 服务凭证", "unauthorized", "invalid_token")
    return None


class _ModelResolutionError(Exception):
    """alias 解析失败；携带 OpenAI 风格错误部件。"""

    def __init__(self, status_code: int, message: str, err_type: str, code: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.err_type = err_type
        self.code = code


def _resolve_call_kwargs(db: Session, alias: str) -> tuple[str, dict[str, Any]]:
    """把固定 alias 解析为唯一启用记忆设置对应的平台模型调用参数；fail-closed。"""
    def fail(status_code: int, message: str, err_type: str, code: str):
        return _ModelResolutionError(status_code, message, err_type, code)

    if alias != MEMORY_MODEL_ALIAS:
        raise fail(404, f"未知模型 {alias!r}：memory 网关仅接受固定别名 "
                        f"{MEMORY_MODEL_ALIAS!r}",
                   "invalid_request_error", "model_not_found")

    candidates = (
        db.query(SuperAssistantMemorySetting)
        .filter(SuperAssistantMemorySetting.enabled.is_(True),
                SuperAssistantMemorySetting.model_config_id.isnot(None))
        .all()
    )
    eligible = [s for s in candidates if s.model_config_id]
    if not eligible:
        raise fail(503, "没有任何已启用且已选型的记忆设置（MemorySetting），"
                        "请先在平台完成记忆模型配置",
                   "server_error", "memory_model_unconfigured")
    if len(eligible) > 1:
        raise fail(503, f"存在 {len(eligible)} 条已启用且已选型的记忆设置，"
                        "无法唯一确定记忆模型（单用户产品应仅一条）",
                   "server_error", "memory_model_ambiguous")
    setting = eligible[0]

    owner_id = setting.owner_id
    if db.get(User, owner_id) is None:
        raise fail(503, f"记忆设置的 owner 不存在（owner_id={owner_id}），配置完整性异常",
                   "server_error", "memory_owner_missing")

    config = db.get(ModelConfig, setting.model_config_id)
    if config is None or not config.enabled or (config.config_type or "llm") != "llm":
        raise fail(404, f"记忆设置指向的模型配置不存在或已停用（{setting.model_config_id}）",
                   "invalid_request_error", "model_not_found")
    if str(config.provider or "").lower() not in ALLOWED_PROVIDERS:
        raise fail(400, f"记忆模型仅支持 openai/compatible provider，当前为 {config.provider!r}",
                   "invalid_request_error", "provider_unsupported")

    try:
        call_kwargs = llm_call_kwargs(config)
    except ValueError as exc:  # 密钥解密失败等：显式报错，不回退
        raise fail(500, str(exc), "server_error", "model_config_error") from exc
    if not call_kwargs:
        raise fail(500, "记忆模型配置缺少模型名", "server_error", "model_config_error")

    api_base = str(call_kwargs.get("api_base") or "")
    if api_base and _LOOP_BACK_PATH_MARK in (urlsplit(api_base).path or ""):
        raise fail(400, "记忆模型的 api_base 指回本 memory 端点，禁止模型回环",
                   "invalid_request_error", "model_loop_forbidden")

    call_kwargs["timeout_seconds"] = _CALL_TIMEOUT_SECONDS
    call_kwargs["retry_max_attempts"] = _GATEWAY_RETRY_MAX_ATTEMPTS
    return owner_id, call_kwargs


def _usage_counts(result: dict[str, Any]) -> tuple[int, int] | None:
    """提取上游真实 usage；缺失/非法返回 None（调用方显式报错，不伪造零）。"""
    usage = result.get("usage") or {}
    prompt = usage.get("inputTokens")
    completion = usage.get("outputTokens")
    if isinstance(prompt, bool) or isinstance(completion, bool):
        return None
    if isinstance(prompt, int) and isinstance(completion, int) \
            and prompt >= 0 and completion >= 0:
        return prompt, completion
    return None


def _openai_chat_response(alias: str, result: dict[str, Any],
                          prompt_tokens: int, completion_tokens: int) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": result.get("content")}
    tool_calls = result.get("tool_calls") or []
    if tool_calls:
        message["tool_calls"] = [{
            "id": tc.get("id") or f"call_{index}",
            "type": "function",
            "function": {
                "name": tc.get("name"),
                "arguments": json.dumps(tc.get("arguments") or {}, ensure_ascii=False),
            },
        } for index, tc in enumerate(tool_calls)]
    # finish_reason 原样透传（截断=length 不伪装 stop）；上游缺失按协议缺省 stop。
    return {
        "id": f"chatcmpl-mem-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": alias,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": result.get("finish_reason") or "stop",
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _request_size_chars(request: _ChatCompletionRequest) -> int:
    total = sum(len(m.content) for m in request.messages)
    if request.response_format is not None:
        total += len(json.dumps(request.response_format, ensure_ascii=False))
    return total


@router.post("/chat/completions")
def memory_chat_completions(
    request: _ChatCompletionRequest,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> JSONResponse:
    gate = _check_service_token(authorization)
    if gate is not None:
        return gate

    try:
        if request.stream:
            raise _ExplicitBadRequest("memory 端点仅支持非流式调用（stream=false）")
        if request.max_tokens is not None and request.max_completion_tokens is not None:
            raise _ExplicitBadRequest(
                "max_tokens 与 max_completion_tokens 只能提供其中一个")
        response_format = (
            _validate_response_format(request.response_format)
            if request.response_format is not None else None)
        total_chars = _request_size_chars(request)
        if total_chars > _MAX_TOTAL_CONTENT_CHARS:
            raise _ExplicitBadRequest(
                f"请求过大（{total_chars} 字符 > {_MAX_TOTAL_CONTENT_CHARS}），请拆分输入")
    except _ExplicitBadRequest as exc:
        return _error_response(400, str(exc), "invalid_request_error", "unsupported_request")

    try:
        owner_id, call_kwargs = _resolve_call_kwargs(db, request.model)
    except _ModelResolutionError as exc:
        logger.warning("memory llm alias 解析失败 model=%s code=%s",
                       request.model, exc.code)
        return _error_response(exc.status_code, exc.message, exc.err_type, exc.code)

    if not _CALL_SEMAPHORE.acquire(blocking=False):
        return _error_response(
            429, "memory llm 并发已满，请稍后重试", "rate_limit_error", "concurrency_limit")
    started = time.monotonic()
    try:
        result = gateway_chat(
            call_kwargs,
            [m.model_dump() for m in request.messages],
            [],
            response_format=response_format,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            max_completion_tokens=request.max_completion_tokens,
            seed=request.seed,
            top_p=request.top_p,
            return_finish_reason=True,
        )
    except LLMError as exc:
        logger.warning("memory llm 上游失败 owner=%s latency_ms=%d",
                       owner_id, int((time.monotonic() - started) * 1000))
        return _error_response(
            502, _safe_error_message(str(exc)) or "上游 LLM 调用失败",
            "upstream_error", "llm_error")
    except Exception as exc:  # noqa: BLE001 — 网关端点统一收口，消息脱敏后返回
        logger.warning("memory llm 调用异常 owner=%s error=%s",
                       owner_id, type(exc).__name__)
        return _error_response(
            502, _safe_error_message(str(exc)) or "上游 LLM 调用失败",
            "upstream_error", "llm_error")
    finally:
        _CALL_SEMAPHORE.release()

    usage = _usage_counts(result)
    if usage is None:
        # 选型探针已要求 usage；运行期缺失属上游异常，显式报错不伪造成零消耗。
        logger.warning("memory llm 上游未返回 usage owner=%s", owner_id)
        return _error_response(
            502, "上游模型未返回可用 usage，拒绝以零消耗伪账返回",
            "upstream_error", "usage_missing")

    prompt_tokens, completion_tokens = usage
    logger.info(
        "memory llm call owner=%s model=%s finish=%s in=%d out=%d latency_ms=%d",
        owner_id, request.model, result.get("finish_reason"),
        prompt_tokens, completion_tokens,
        int((time.monotonic() - started) * 1000))
    return JSONResponse(content=_openai_chat_response(
        request.model, result, prompt_tokens, completion_tokens))


# ---------------------------------------------------------------------------
# 模型能力探针（memory_router 保存模型选择时调用；用户态鉴权由调用方负责）
# ---------------------------------------------------------------------------

_PROBE_SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}

_STRUCTURED_UNSUPPORTED_HINTS = ("response_format", "json_schema", "json_object")


def validate_memory_model(db: Session, model_config_id: str) -> dict[str, Any]:
    """校验给定模型配置可作为记忆模型：静态检查 + 一次小样例结构化探测。

    返回 ``{"ok", "code", "message"}``，成功附 ``"usage"`` 证据。不回退、
    不落 last_test_*；错误消息脱敏。探针与正式调用共用并发闸门与网关
    重试上限（单次尝试），样例必须完整返回精确 ``{"ok": true}`` 且不被
    截断，否则不算能力通过。
    """
    def result(ok: bool, code: str, message: str, **extra: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": ok, "code": code, "message": message}
        payload.update(extra)
        return payload

    config = db.get(ModelConfig, model_config_id) if model_config_id else None
    if config is None:
        return result(False, "MODEL_NOT_FOUND", "模型配置不存在")
    if not config.enabled or (config.config_type or "llm") != "llm":
        return result(False, "MODEL_NOT_FOUND", "模型配置已停用或类型不是 llm")
    if str(config.provider or "").lower() not in ALLOWED_PROVIDERS:
        return result(
            False, "PROVIDER_UNSUPPORTED",
            f"记忆模型仅支持 openai/compatible provider，当前为 {config.provider!r}")

    api_base = str(config.api_base or "")
    if api_base and _LOOP_BACK_PATH_MARK in (urlsplit(api_base).path or ""):
        return result(
            False, "MODEL_LOOP_FORBIDDEN",
            "模型 api_base 指回本 memory 端点，会造成模型回环，禁止选择")

    try:
        call_kwargs = llm_call_kwargs(config)
    except ValueError as exc:
        return result(False, "KEY_DECRYPT_FAILED", str(exc))
    if not call_kwargs:
        return result(False, "MODEL_NAME_MISSING", "模型配置缺少模型名")
    call_kwargs["timeout_seconds"] = _PROBE_TIMEOUT_SECONDS
    call_kwargs["retry_max_attempts"] = _GATEWAY_RETRY_MAX_ATTEMPTS

    if not _CALL_SEMAPHORE.acquire(blocking=False):
        return result(False, "CONCURRENCY_LIMIT", "memory llm 并发已满，请稍后重试")
    try:
        try:
            probe = gateway_chat(
                call_kwargs,
                [{"role": "user", "content": 'Return exactly this JSON object: {"ok": true}'}],
                [],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "memory_model_probe",
                        "strict": True,
                        "schema": _PROBE_SCHEMA,
                    },
                },
                temperature=0.0,
                max_tokens=_PROBE_MAX_TOKENS,
                return_finish_reason=True,
            )
        except Exception as exc:  # noqa: BLE001 — 探针统一收口，映射为可读错误
            message = str(exc)
            if any(hint in message.lower() for hint in _STRUCTURED_UNSUPPORTED_HINTS):
                return result(
                    False, "STRUCTURED_OUTPUT_UNSUPPORTED",
                    "模型拒绝了 json_schema 结构化输出，无法作为记忆模型")
            code, readable = safe_test_error(exc)
            return result(False, code, readable)
    finally:
        _CALL_SEMAPHORE.release()

    content = probe.get("content")
    if not content or not isinstance(content, str):
        return result(False, "EMPTY_CONTENT", "模型未返回任何内容")
    if probe.get("finish_reason") == "length":
        return result(
            False, "STRUCTURED_OUTPUT_TRUNCATED",
            "探测样例被截断（finish_reason=length），无法验证结构化输出能力")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return result(
            False, "STRUCTURED_OUTPUT_UNSUPPORTED",
            "模型在 json_schema 约束下未返回合法 JSON，无法作为记忆模型")
    if not isinstance(parsed, dict) or parsed.get("ok") is not True \
            or set(parsed) != {"ok"}:
        return result(
            False, "STRUCTURED_OUTPUT_INVALID",
            '模型未按 json_schema 约束返回精确的 {"ok": true}')

    usage = _usage_counts(probe)
    if usage is None:
        return result(
            False, "USAGE_MISSING",
            "模型未返回可用的 token 用量（usage），无法核算记忆调用成本")

    return result(
        True, "OK", "结构化 JSON 输出与用量返回验证通过",
        usage={
            "prompt_tokens": usage[0],
            "completion_tokens": usage[1],
            "total_tokens": usage[0] + usage[1],
        })
