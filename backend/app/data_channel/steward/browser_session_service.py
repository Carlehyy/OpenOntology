"""Conversation-scoped browser runtime and live-control services."""
from __future__ import annotations

import logging

from fastapi import HTTPException, Response
from sqlalchemy.orm import Session

from app.data_channel.steward import browser_sources, workspace
from app.data_channel.steward.browser_runtime import (
    BrowserRuntimeError,
    browser_manager,
)
from app.data_channel.steward.contracts import (
    BindBrowserSourceBody,
    BrowserClickBody,
    BrowserLiveControlBody,
    BrowserLiveInputBody,
    BrowserLiveLeaseBody,
    BrowserTypeBody,
    BrowserUrlBody,
)
from app.data_channel.steward.query_service import (
    _conv_out,
    _ok,
    _require_conversation,
)
from app.data_channel.steward.service import StewardError


logger = logging.getLogger(__name__)


def _browser_error(exc: Exception) -> HTTPException:
    if isinstance(
        exc,
        (BrowserRuntimeError, StewardError, workspace.WorkspaceError),
    ):
        return HTTPException(422, str(exc))
    logger.exception("会话浏览器操作失败")
    return HTTPException(500, "会话浏览器操作失败")


def bind_browser_source(
    conversation_id: str,
    body: BindBrowserSourceBody,
    db: Session,
    current_user,
    *,
    require_conversation_fn=_require_conversation,
    conv_out_fn=_conv_out,
):
    conversation = require_conversation_fn(
        db, conversation_id, current_user
    )
    source_id = body.sourceId
    if source_id and source_id != browser_sources.MANAGED_SOURCE_ID:
        try:
            browser_sources.require_source(
                db,
                source_id,
                getattr(current_user, "id", None),
            )
        except StewardError as exc:
            raise HTTPException(422, str(exc)) from exc
    browser_manager.close(conversation_id)
    conversation.browser_source_id = (
        None
        if source_id in {None, browser_sources.MANAGED_SOURCE_ID}
        else source_id
    )
    db.commit()
    return _ok(conv_out_fn(conversation))


def start_browser(
    conversation_id: str,
    body: BrowserUrlBody,
    db: Session,
    current_user,
    *,
    require_conversation_fn=_require_conversation,
    browser_error_fn=_browser_error,
    session_workspace=None,
):
    conversation = require_conversation_fn(
        db, conversation_id, current_user
    )
    try:
        owner_id = (
            conversation.user_id or getattr(current_user, "id", None)
        )
        target = browser_sources.resolve_target(
            db,
            conversation.browser_source_id,
            owner_id,
            admin=getattr(current_user, "role", "") == "admin",
        )
        return _ok(browser_manager.start(
            conversation_id,
            body.url,
            user_id=owner_id,
            actor="user",
            browser_target=target,
            session_workspace=session_workspace,
        ))
    except Exception as exc:  # noqa: BLE001
        raise browser_error_fn(exc)


def navigate_browser(
    conversation_id: str,
    body: BrowserUrlBody,
    db: Session,
    current_user,
    *,
    require_conversation_fn=_require_conversation,
    browser_error_fn=_browser_error,
):
    require_conversation_fn(db, conversation_id, current_user)
    try:
        return _ok(
            browser_manager.navigate(
                conversation_id, body.url, actor="user"
            )
        )
    except Exception as exc:  # noqa: BLE001
        raise browser_error_fn(exc)


def browser_state(
    conversation_id: str,
    db: Session,
    current_user,
    *,
    require_conversation_fn=_require_conversation,
    browser_error_fn=_browser_error,
):
    require_conversation_fn(db, conversation_id, current_user)
    try:
        return _ok(
            browser_manager.state(conversation_id, actor="user")
        )
    except Exception as exc:  # noqa: BLE001
        raise browser_error_fn(exc)


def browser_session(
    conversation_id: str,
    db: Session,
    current_user,
    *,
    require_conversation_fn=_require_conversation,
    browser_error_fn=_browser_error,
):
    require_conversation_fn(db, conversation_id, current_user)
    try:
        return _ok(browser_manager.session_info(conversation_id))
    except Exception as exc:  # noqa: BLE001
        raise browser_error_fn(exc)


def browser_click(
    conversation_id: str,
    body: BrowserClickBody,
    db: Session,
    current_user,
    *,
    require_conversation_fn=_require_conversation,
    browser_error_fn=_browser_error,
):
    require_conversation_fn(db, conversation_id, current_user)
    try:
        return _ok(
            browser_manager.click_text(
                conversation_id, body.text, actor="user"
            )
        )
    except Exception as exc:  # noqa: BLE001
        raise browser_error_fn(exc)


def browser_type(
    conversation_id: str,
    body: BrowserTypeBody,
    db: Session,
    current_user,
    *,
    require_conversation_fn=_require_conversation,
    browser_error_fn=_browser_error,
):
    require_conversation_fn(db, conversation_id, current_user)
    try:
        return _ok(browser_manager.type_text(
            conversation_id,
            body.selector,
            body.text,
            body.pressEnter,
            actor="user",
        ))
    except Exception as exc:  # noqa: BLE001
        raise browser_error_fn(exc)


def browser_captures(
    conversation_id: str,
    keyword: str | None,
    limit: int,
    db: Session,
    current_user,
    *,
    require_conversation_fn=_require_conversation,
    session_workspace=None,
):
    require_conversation_fn(db, conversation_id, current_user)
    return _ok(
        browser_manager.list_captures(
            conversation_id, keyword, limit,
            session_workspace=session_workspace,
        )
    )


def browser_capture_download(
    conversation_id: str,
    capture_id: str,
    db: Session,
    current_user,
    *,
    require_conversation_fn=_require_conversation,
    browser_error_fn=_browser_error,
):
    require_conversation_fn(db, conversation_id, current_user)
    try:
        return _ok(browser_manager.download(
            conversation_id,
            capture_id,
            actor="user",
        ))
    except Exception as exc:  # noqa: BLE001
        raise browser_error_fn(exc)


def browser_live_ticket(
    conversation_id: str,
    db: Session,
    current_user,
    *,
    require_conversation_fn=_require_conversation,
):
    require_conversation_fn(db, conversation_id, current_user)
    ticket = browser_manager.issue_ticket(
        conversation_id,
        getattr(current_user, "id", None),
    )
    return _ok({"ticket": ticket, "expiresIn": 60})


def attach_browser_live_http(
    conversation_id: str,
    db: Session,
    current_user,
    *,
    require_conversation_fn=_require_conversation,
    browser_error_fn=_browser_error,
):
    require_conversation_fn(db, conversation_id, current_user)
    try:
        return _ok(browser_manager.attach_http_live(conversation_id))
    except Exception as exc:  # noqa: BLE001
        raise browser_error_fn(exc)


def browser_live_http_frame(
    conversation_id: str,
    body: BrowserLiveLeaseBody,
    response: Response,
    db: Session,
    current_user,
    *,
    require_conversation_fn=_require_conversation,
    browser_error_fn=_browser_error,
):
    require_conversation_fn(db, conversation_id, current_user)
    try:
        response.headers["Cache-Control"] = "no-store"
        return _ok(browser_manager.http_live_screenshot(
            conversation_id, body.leaseId
        ))
    except Exception as exc:  # noqa: BLE001
        raise browser_error_fn(exc)


def browser_live_http_input(
    conversation_id: str,
    body: BrowserLiveInputBody,
    db: Session,
    current_user,
    *,
    require_conversation_fn=_require_conversation,
    browser_error_fn=_browser_error,
):
    require_conversation_fn(db, conversation_id, current_user)
    try:
        collaboration = browser_manager.http_live_input(
            conversation_id,
            body.leaseId,
            body.message,
        )
        return _ok({
            "accepted": True,
            "collaboration": collaboration,
        })
    except Exception as exc:  # noqa: BLE001
        raise browser_error_fn(exc)


def browser_live_http_control(
    conversation_id: str,
    body: BrowserLiveControlBody,
    db: Session,
    current_user,
    *,
    require_conversation_fn=_require_conversation,
    browser_error_fn=_browser_error,
):
    require_conversation_fn(db, conversation_id, current_user)
    try:
        collaboration = browser_manager.http_live_control(
            conversation_id,
            body.leaseId,
            body.action,
        )
        return _ok({"collaboration": collaboration})
    except Exception as exc:  # noqa: BLE001
        raise browser_error_fn(exc)


def release_browser_live_http(
    conversation_id: str,
    body: BrowserLiveLeaseBody,
    db: Session,
    current_user,
    *,
    require_conversation_fn=_require_conversation,
    browser_error_fn=_browser_error,
):
    require_conversation_fn(db, conversation_id, current_user)
    try:
        browser_manager.release_http_live(
            conversation_id, body.leaseId
        )
        return _ok({"released": True})
    except Exception as exc:  # noqa: BLE001
        raise browser_error_fn(exc)
