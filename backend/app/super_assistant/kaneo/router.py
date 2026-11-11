"""Kaneo 前端跳转入口。

Kaneo 的原生 Web 负责项目、任务与工作区配置；平台只暴露经过 origin
校验的公开 Web 地址（nginx HTTPS 入口），不读取项目/任务数据，也不持有
Kaneo 账号凭据（管理员账号只由部署 bootstrap 服务初始化）。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.models import User
from app.deps import get_current_user
from app.shared.config import settings
from app.super_assistant.web_entry import origin


router = APIRouter(tags=["super-assistant"])


class KaneoIntegrationOut(BaseModel):
    web_url: str


@router.get("/kaneo", response_model=KaneoIntegrationOut)
def read_kaneo_integration(
    current_user: User = Depends(get_current_user),
) -> KaneoIntegrationOut:
    # 复用 WeKnora 的 origin 归一化校验：两个集成的入口 URL 语义一致，
    # 共享实现避免出现两套平行的 URL 判定规则。
    try:
        web_url = origin(settings.kaneo_web_url)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="项目任务服务尚未就绪") from exc
    return KaneoIntegrationOut(web_url=web_url)
