from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.models import User
from app.deps import get_current_user
from app.shared.config import settings
from app.super_assistant.weknora.service import WeKnoraError, get_integration, origin


router = APIRouter(tags=["super-assistant"])


class WeKnoraIntegrationOut(BaseModel):
    web_url: str
    knowledge_base_name: str


@router.get("/weknora", response_model=WeKnoraIntegrationOut)
def read_weknora_integration(
    current_user: User = Depends(get_current_user),
) -> WeKnoraIntegrationOut:
    try:
        integration = get_integration()
        return WeKnoraIntegrationOut(
            web_url=origin(settings.weknora_web_url),
            knowledge_base_name=integration.knowledge_base_name,
        )
    except WeKnoraError as exc:
        raise HTTPException(status_code=503, detail="知识图谱服务尚未就绪") from exc
