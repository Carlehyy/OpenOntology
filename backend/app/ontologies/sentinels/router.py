"""
哨兵引擎 API

挂载于 /api/v1/ontologies/{ontology_id}/sentinels
  - 哨兵 CRUD / 启停
  - 手动触发(全量评估)
  - 触发日志 / 通知 可观测查询
"""
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import (
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)
from sqlalchemy.orm import Session, sessionmaker

from app.deps import get_db, get_current_user
from app.schemas.ontology_formal import CamelModel
from app.models.sentinel import (
    Notification,
    Sentinel,
    SentinelFiring,
    SentinelMatchState,
)
from app.models.ontology import OntologyProject
from app.ontologies.release_context import current_release_context
from app.ontologies.sentinels.engine import run_manual
from app.ontologies.access import ontology_access_guard
from app.ontologies.sentinels import (
    definition_workflow as _definition_workflow,
)
from app.ontologies.sentinels import (
    operational_workflow as _operational_workflow,
)
from app.ontologies.sentinels import query_service as _query_service
from app.ontologies.sentinels.dynamic_service import (
    ORIGIN_BUILTIN,
    _sentinel_write_fence,
)
from app.ontologies.sentinels.project_guard import (
    _project,
    _require_draft,
)
from app.ontologies.sentinels.query_service import (
    _dict,
    _released_dict,
)
from app.shared.time_utils import utc_iso


router = APIRouter(dependencies=[Depends(ontology_access_guard)])


class SentinelIn(CamelModel):
    name: str
    display_name: str
    description: Optional[str] = None
    bindings: list = Field(default_factory=list)
    links: list = Field(default_factory=list)
    condition: Optional[str] = None
    condition_rows: list = Field(default_factory=list)
    condition_logic: str = "and"
    primary_alias: Optional[str] = None
    action_ids: list = Field(default_factory=list)
    action_parameters: dict = Field(default_factory=dict)
    on_change: bool = True
    on_schedule: bool = False
    scan_interval_seconds: int = 300
    trigger_mode: str = "on_enter"
    pattern: Optional[dict] = None
    muted: bool = False
    enabled: bool = True
    # 本体 release 是唯一上线边界；客户端不能把未校验定义直接标成 published。
    status: str = "draft"


class SentinelUpdate(CamelModel):
    name: Optional[str] = None
    display_name: Optional[str] = None
    description: Optional[str] = None
    bindings: Optional[list] = None
    links: Optional[list] = None
    condition: Optional[str] = None
    condition_rows: Optional[list] = None
    condition_logic: Optional[str] = None
    primary_alias: Optional[str] = None
    action_ids: Optional[list] = None
    action_parameters: Optional[dict] = None
    on_change: Optional[bool] = None
    on_schedule: Optional[bool] = None
    scan_interval_seconds: Optional[int] = None
    trigger_mode: Optional[str] = None
    pattern: Optional[dict] = None
    muted: Optional[bool] = None
    enabled: Optional[bool] = None


class SentinelOperationalUpdate(CamelModel):
    enabled: Optional[StrictBool] = None
    muted: Optional[StrictBool] = None
    expected_release_id: StrictStr
    expected_generation: StrictInt = Field(ge=0)

    @field_validator("expected_release_id")
    @classmethod
    def normalize_release_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("expectedReleaseId 不能为空")
        return normalized

    @model_validator(mode="after")
    def require_state_field(self):
        if self.enabled is None and self.muted is None:
            raise ValueError("enabled 与 muted 至少提供一个")
        return self


@router.get("/")
def list_sentinels(
    ontology_id: str,
    release_id: Optional[str] = None,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    return _query_service.list_sentinels(
        ontology_id,
        release_id,
        db,
        current_release_context_fn=current_release_context,
        released_dict_fn=_released_dict,
    )


@router.post("/", status_code=201)
def create_sentinel(
    ontology_id: str,
    body: SentinelIn,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    return _definition_workflow.create_sentinel(
        ontology_id,
        body,
        db,
        require_draft_fn=_require_draft,
        dict_fn=_dict,
    )


@router.post("/run")
def run(
    ontology_id: str,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """手动触发：全量评估本体所有启用哨兵。"""
    return _operational_workflow.run(
        ontology_id,
        db,
        run_manual_fn=run_manual,
    )


@router.get("/firings")
def list_firings(
    ontology_id: str,
    sentinel_id: Optional[str] = None,
    limit: int = 50,
    release_id: Optional[str] = None,
    include_history: bool = False,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    return _query_service.list_firings(
        ontology_id,
        sentinel_id,
        limit,
        release_id,
        include_history,
        db,
        current_release_context_fn=current_release_context,
    )


@router.get("/notifications")
def list_notifications(
    ontology_id: str,
    limit: int = 50,
    release_id: Optional[str] = None,
    include_history: bool = False,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    return _query_service.list_notifications(
        ontology_id,
        limit,
        release_id,
        include_history,
        db,
        current_release_context_fn=current_release_context,
    )


@router.get("/cdc-status")
def get_cdc_status(
    ontology_id: str,
    release_id: Optional[str] = None,
    include_history: bool = False,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """Authenticated operational view of durable Sentinel CDC and dead letters."""
    return _operational_workflow.get_cdc_status(
        ontology_id,
        release_id,
        include_history,
        db,
        project_fn=_project,
        sessionmaker_fn=sessionmaker,
    )


@router.patch("/{sentinel_id}/operational-state")
def update_operational_state(
    ontology_id: str,
    sentinel_id: str,
    body: SentinelOperationalUpdate,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    """CAS-update the mutable overlay of one exact published built-in."""
    return _operational_workflow.update_operational_state(
        ontology_id,
        sentinel_id,
        body,
        db,
        sentinel_write_fence_fn=_sentinel_write_fence,
        project_fn=_project,
        current_release_context_fn=current_release_context,
        released_dict_fn=_released_dict,
    )


@router.get("/{sentinel_id}")
def get_sentinel(
    ontology_id: str,
    sentinel_id: str,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    return _query_service.get_sentinel(
        ontology_id,
        sentinel_id,
        db,
        dict_fn=_dict,
    )


@router.put("/{sentinel_id}")
def update_sentinel(
    ontology_id: str,
    sentinel_id: str,
    body: SentinelUpdate,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    return _definition_workflow.update_sentinel(
        ontology_id,
        sentinel_id,
        body,
        db,
        sentinel_write_fence_fn=_sentinel_write_fence,
        project_fn=_project,
        dict_fn=_dict,
    )


@router.delete("/{sentinel_id}", status_code=204)
def delete_sentinel(
    ontology_id: str,
    sentinel_id: str,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    return _definition_workflow.delete_sentinel(
        ontology_id,
        sentinel_id,
        db,
        sentinel_write_fence_fn=_sentinel_write_fence,
        require_draft_fn=_require_draft,
    )


@router.post("/{sentinel_id}/toggle")
def toggle_sentinel(
    ontology_id: str,
    sentinel_id: str,
    db: Session = Depends(get_db),
    _=Depends(get_current_user),
):
    return _operational_workflow.toggle_sentinel(
        ontology_id,
        sentinel_id,
        db,
        sentinel_write_fence_fn=_sentinel_write_fence,
        project_fn=_project,
    )
