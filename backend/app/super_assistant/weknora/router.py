"""WeKnora 前端跳转入口。

WeKnora 的原生 Web 负责模型、知识库与 MCP endpoint 配置；平台只暴露经过
origin 校验的公开 Web 地址，不读取本地 state、知识库或 MCP 清单。
"""
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.models import User
from app.deps import get_current_user
from app.shared.config import settings


router = APIRouter(tags=["super-assistant"])


class WeKnoraIntegrationOut(BaseModel):
    web_url: str


def origin(value: str) -> str:
    """Return a normalized absolute HTTP(S) origin or raise ``ValueError``."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("WEKNORA_INVALID_URL") from exc
    if (
        parsed.scheme not in {"http", "https"} or not parsed.hostname
        or parsed.username is not None or parsed.password is not None
        or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
        or any(char.isspace() for char in value)
    ):
        raise ValueError("WEKNORA_INVALID_URL")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    suffix = f":{port}" if port is not None else ""
    return f"{parsed.scheme}://{host}{suffix}"


@router.get("/weknora", response_model=WeKnoraIntegrationOut)
def read_weknora_integration(
    current_user: User = Depends(get_current_user),
) -> WeKnoraIntegrationOut:
    try:
        web_url = origin(settings.weknora_web_url)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="知识图谱服务尚未就绪") from exc
    return WeKnoraIntegrationOut(web_url=web_url)
