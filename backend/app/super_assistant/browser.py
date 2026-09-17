"""超级助手实时浏览器端点（/api/v2/super-assistant 前缀的独立子路由）。

会话级端点薄壳委托 steward 的 browser_session_service（归属校验注入超助的
_conversation：404 语义、无 admin 旁路，与本域一致）；浏览器来源管理与
companion 脚本直接委托 browser_source_service。响应沿用 steward 的
{"data": ...} 包络——与超助常规裸 dict 不同，这是有意决策：前端浏览器
协作面板对两个域共用一套解包。浏览器产物（登录态/捕获/下载）落超助会话
工作区，随会话删除一并清理。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.auth.models import User
from app.data_channel.steward import browser_sources
from app.data_channel.steward import (
    browser_session_service as _browser_session_service,
)
from app.data_channel.steward import (
    browser_source_service as _browser_source_service,
)
from app.data_channel.steward.browser_runtime import browser_manager
from app.data_channel.steward.browser_session_service import _browser_error
from app.data_channel.steward.contracts import (
    BindBrowserSourceBody,
    BrowserLiveControlBody,
    BrowserLiveInputBody,
    BrowserLiveLeaseBody,
    BrowserUrlBody,
    CreateBrowserSourceBody,
    UpdateBrowserSourceBody,
)
from app.data_channel.steward.query_service import _ok
from app.data_channel.steward.service import StewardError
from app.deps import get_current_user, get_db
from app.super_assistant import conversation_service, files_workspace

router = APIRouter()


def _require_conversation(db: Session, conversation_id: str, current_user: User):
    return conversation_service._conversation(db, current_user.id, conversation_id)


def bind_browser_source(
    conversation_id: str,
    body: BindBrowserSourceBody,
    db: Session,
    current_user: User,
):
    conversation = _require_conversation(db, conversation_id, current_user)
    source_id = body.sourceId
    if source_id and source_id != browser_sources.MANAGED_SOURCE_ID:
        try:
            browser_sources.require_source(db, source_id, current_user.id)
        except StewardError as exc:
            raise HTTPException(422, str(exc)) from exc
    browser_manager.close(conversation_id)
    conversation.browser_source_id = (
        None if source_id in {None, browser_sources.MANAGED_SOURCE_ID} else source_id
    )
    db.commit()
    return _ok({
        "conversationId": conversation.id,
        "browserSourceId": conversation.browser_source_id or browser_sources.MANAGED_SOURCE_ID,
    })


def start_browser(
    conversation_id: str,
    body: BrowserUrlBody,
    db: Session,
    current_user: User,
):
    conversation = _require_conversation(db, conversation_id, current_user)
    try:
        target = browser_sources.resolve_target(
            db, conversation.browser_source_id, conversation.owner_id,
        )
        return _ok(browser_manager.start(
            conversation_id,
            body.url,
            user_id=conversation.owner_id,
            actor="user",
            browser_target=target,
            session_workspace=files_workspace.session_workspace(),
        ))
    except Exception as exc:  # noqa: BLE001
        raise _browser_error(exc)


# ── 浏览器来源（用户级，与会话无关）────────────────────────────────

@router.get("/browser/sources")
def list_browser_sources(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _browser_source_service.list_browser_sources(db, current_user)


@router.post("/browser/sources", status_code=201)
def create_browser_source(
    body: CreateBrowserSourceBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _browser_source_service.create_browser_source(body, db, current_user)


@router.patch("/browser/sources/{source_id}")
def update_browser_source(
    source_id: str,
    body: UpdateBrowserSourceBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _browser_source_service.update_browser_source(
        source_id, body, db, current_user,
    )


@router.delete("/browser/sources/{source_id}", status_code=204)
def delete_browser_source(
    source_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _browser_source_service.delete_browser_source(
        source_id, db, current_user,
    )


@router.post("/browser/sources/{source_id}/rotate-token")
def rotate_browser_source_token(
    source_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _browser_source_service.rotate_browser_source_token(
        source_id, db, current_user,
    )


@router.post("/browser/sources/{source_id}/test")
def test_browser_source(
    source_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _browser_source_service.test_browser_source(
        source_id, db, current_user, browser_error_fn=_browser_error,
    )


@router.get("/browser/companion/script")
def download_browser_companion(_=Depends(get_current_user)):
    return _browser_source_service.download_browser_companion()


# ── 会话级浏览器操作 ────────────────────────────────────────────────

@router.put("/conversations/{conversation_id}/browser/source")
def bind_conversation_browser_source(
    conversation_id: str,
    body: BindBrowserSourceBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return bind_browser_source(conversation_id, body, db, current_user)


@router.post("/conversations/{conversation_id}/browser/start")
def start_conversation_browser(
    conversation_id: str,
    body: BrowserUrlBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return start_browser(conversation_id, body, db, current_user)


@router.post("/conversations/{conversation_id}/browser/navigate")
def navigate_browser(
    conversation_id: str,
    body: BrowserUrlBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _browser_session_service.navigate_browser(
        conversation_id,
        body,
        db,
        current_user,
        require_conversation_fn=_require_conversation,
        browser_error_fn=_browser_error,
    )


@router.get("/conversations/{conversation_id}/browser/session")
def browser_session(
    conversation_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _browser_session_service.browser_session(
        conversation_id,
        db,
        current_user,
        require_conversation_fn=_require_conversation,
        browser_error_fn=_browser_error,
    )


@router.get("/conversations/{conversation_id}/browser/captures")
def browser_captures(
    conversation_id: str,
    keyword: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _browser_session_service.browser_captures(
        conversation_id,
        keyword,
        limit,
        db,
        current_user,
        require_conversation_fn=_require_conversation,
        session_workspace=files_workspace.session_workspace(),
    )


@router.post(
    "/conversations/{conversation_id}/browser/captures/"
    "{capture_id}/download"
)
def browser_capture_download(
    conversation_id: str,
    capture_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _browser_session_service.browser_capture_download(
        conversation_id,
        capture_id,
        db,
        current_user,
        require_conversation_fn=_require_conversation,
        browser_error_fn=_browser_error,
    )


@router.post("/conversations/{conversation_id}/browser/ticket")
def browser_live_ticket(
    conversation_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _browser_session_service.browser_live_ticket(
        conversation_id,
        db,
        current_user,
        require_conversation_fn=_require_conversation,
    )


@router.post("/conversations/{conversation_id}/browser/live-http")
def attach_browser_live_http(
    conversation_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _browser_session_service.attach_browser_live_http(
        conversation_id,
        db,
        current_user,
        require_conversation_fn=_require_conversation,
        browser_error_fn=_browser_error,
    )


@router.post(
    "/conversations/{conversation_id}/browser/live-http/frame"
)
def browser_live_http_frame(
    conversation_id: str,
    body: BrowserLiveLeaseBody,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _browser_session_service.browser_live_http_frame(
        conversation_id,
        body,
        response,
        db,
        current_user,
        require_conversation_fn=_require_conversation,
        browser_error_fn=_browser_error,
    )


@router.post(
    "/conversations/{conversation_id}/browser/live-http/input"
)
def browser_live_http_input(
    conversation_id: str,
    body: BrowserLiveInputBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _browser_session_service.browser_live_http_input(
        conversation_id,
        body,
        db,
        current_user,
        require_conversation_fn=_require_conversation,
        browser_error_fn=_browser_error,
    )


@router.post(
    "/conversations/{conversation_id}/browser/live-http/control"
)
def browser_live_http_control(
    conversation_id: str,
    body: BrowserLiveControlBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _browser_session_service.browser_live_http_control(
        conversation_id,
        body,
        db,
        current_user,
        require_conversation_fn=_require_conversation,
        browser_error_fn=_browser_error,
    )


@router.post(
    "/conversations/{conversation_id}/browser/live-http/release"
)
def release_browser_live_http(
    conversation_id: str,
    body: BrowserLiveLeaseBody,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return _browser_session_service.release_browser_live_http(
        conversation_id,
        body,
        db,
        current_user,
        require_conversation_fn=_require_conversation,
        browser_error_fn=_browser_error,
    )
