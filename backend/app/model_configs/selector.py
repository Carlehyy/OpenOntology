"""Model config selection helpers for LLM/VLM call sites."""
from __future__ import annotations

from typing import Iterable

from cryptography.fernet import InvalidToken


VLM_TAGS = {"VLM提取", "vlm", "vision", "视觉", "多模态", "multimodal"}
VLM_TOKENS = ("omni", "vlm", "vision", "multimodal")


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def usage_tags(model_config) -> list[str]:
    options = getattr(model_config, "options", None) or {}
    return [str(tag) for tag in _as_list(options.get("usage_tags"))]


def is_vlm_config(model_config) -> bool:
    options = getattr(model_config, "options", None) or {}
    tags = set(usage_tags(model_config))
    if tags & VLM_TAGS:
        return True
    modalities = " ".join(str(x).lower() for x in _as_list(options.get("modalities")))
    model_names = " ".join(str(x).lower() for x in _as_list(getattr(model_config, "models", None)))
    provider = str(getattr(model_config, "provider", "") or "").lower()
    return (
        "vision" in modalities
        or "image" in modalities
        or any(token in model_names for token in VLM_TOKENS)
        or any(token in provider for token in ("omni", "vlm"))
    )


def select_llm_model_config(
    db=None,
    model_id: str | None = None,
    purpose_tags: Iterable[str] = (),
    allow_vlm: bool = False,
):
    """Select a configured LLM.

    Text LLM callers should keep allow_vlm=False so a VLM such as mimo-omni
    does not override a text model such as DeepSeek just because it was updated
    later. VLM callers pass allow_vlm=True and purpose_tags=("VLM提取",).
    """
    from app.database import SessionLocal
    from app.model_configs.models import ModelConfig

    owns_db = db is None
    db = db or SessionLocal()
    try:
        query = db.query(ModelConfig).filter(ModelConfig.config_type == "llm", ModelConfig.enabled.is_(True))
        if model_id:
            selected = query.filter(ModelConfig.id == model_id).first()
            if selected:
                return selected

        configs = query.order_by(ModelConfig.is_default.desc(), ModelConfig.updated_at.desc()).all()
        if not configs:
            return None

        requested_tags = [str(tag) for tag in purpose_tags if tag]
        for tag in requested_tags:
            for item in configs:
                if tag in usage_tags(item):
                    return item

        if allow_vlm:
            vlm_candidates = [item for item in configs if is_vlm_config(item)]
            candidates = vlm_candidates or configs
        else:
            candidates = [item for item in configs if not is_vlm_config(item)]
        return candidates[0] if candidates else configs[0]
    finally:
        if owns_db:
            db.close()


def llm_call_kwargs(model_config) -> dict | None:
    if not model_config:
        return None
    from app.services import encryption_service

    models = _as_list(getattr(model_config, "models", None))
    model_name = str(models[0]) if models else ""
    if not model_name:
        return None
    api_key = ""
    encrypted = getattr(model_config, "api_key_encrypted", None)
    if encrypted:
        try:
            api_key = encryption_service.decrypt(encrypted)
        except InvalidToken as exc:
            name = getattr(model_config, "name", "") or "当前模型"
            raise ValueError(
                f"模型配置「{name}」的 API Key 无法解密，可能是本地加密密钥变更导致。"
                "请到「模型配置」重新填写并保存 API Key。"
            ) from exc

    options = getattr(model_config, "options", None) or {}
    kwargs = {
        "model_config_id": getattr(model_config, "id", None),
        "provider": getattr(model_config, "provider", None),
        "api_key": api_key,
        "api_base": getattr(model_config, "api_base", None),
        "model": model_name,
    }
    max_output = _positive_int(options.get("max_output_tokens"))
    if max_output:
        kwargs["max_output_tokens"] = max_output
    max_context = _positive_int(options.get("max_context_tokens"))
    if max_context:
        kwargs["max_context_tokens"] = max_context
    assistant_timeout = _positive_int(options.get("ontology_assistant_timeout_seconds"))
    if assistant_timeout:
        kwargs["ontology_assistant_timeout_seconds"] = min(assistant_timeout, 600)
    return kwargs


def _positive_int(value) -> int | None:
    """将 options 中的 token 上限解析为正整数，非法值返回 None。"""
    if value is None or value == "":
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None
