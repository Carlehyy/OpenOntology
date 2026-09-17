"""Browser capacity, source configuration, and companion distribution."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.data_channel.steward import browser_sources
from app.data_channel.steward.browser_runtime import (
    browser_manager,
    probe_browser_cdp,
)
from app.data_channel.steward.contracts import (
    CreateBrowserSourceBody,
    UpdateBrowserSourceBody,
)
from app.data_channel.steward.models import (
    BROWSER_SOURCE_REMOTE_CDP,
    StewardBrowserSource,
    StewardConversation,
)
from app.data_channel.steward.query_service import _ok
from app.data_channel.steward.service import StewardError


logger = logging.getLogger(__name__)


def browser_status():
    try:
        capacity = browser_manager.capacity_status()
    except Exception:  # noqa: BLE001
        capacity = {
            "activeSessions": 0,
            "liveSessions": 0,
            "maxSessions": max(
                1, int(settings.steward_browser_max_sessions)
            ),
            "maxSessionsPerUser": max(
                1, int(settings.steward_browser_max_sessions_per_user)
            ),
            "idleTimeoutSeconds": max(
                30, int(settings.steward_browser_idle_timeout_seconds)
            ),
        }
    return _ok({**probe_browser_cdp(), **capacity})


def list_browser_sources(db: Session, current_user):
    user_id = getattr(current_user, "id", None)
    rows = (
        db.query(StewardBrowserSource)
        .filter(StewardBrowserSource.user_id == user_id)
        .order_by(StewardBrowserSource.created_at.asc())
        .all()
    )
    return _ok([
        browser_sources.managed_out(),
        *[browser_sources.source_out(row) for row in rows],
    ])


def create_browser_source(
    body: CreateBrowserSourceBody,
    db: Session,
    current_user,
):
    if (
        body.sourceType == BROWSER_SOURCE_REMOTE_CDP
        and getattr(current_user, "role", "") != "admin"
    ):
        raise HTTPException(
            403,
            (
                "远程 CDP 是服务器级高权限入口，只允许管理员配置；"
                "普通用户请使用本机浏览器助手"
            ),
        )
    try:
        source, token = browser_sources.create_source(
            db,
            getattr(current_user, "id", None),
            name=body.name,
            source_type=body.sourceType,
            endpoint_url=body.endpointUrl,
            headers=body.headers,
        )
    except StewardError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _ok({
        **browser_sources.source_out(source),
        "pairingToken": token,
    })


def update_browser_source(
    source_id: str,
    body: UpdateBrowserSourceBody,
    db: Session,
    current_user,
):
    try:
        source = browser_sources.require_source(
            db,
            source_id,
            getattr(current_user, "id", None),
            admin=getattr(current_user, "role", "") == "admin",
        )
        if body.name is not None:
            source.name = body.name.strip()[:120] or source.name
        if body.enabled is not None:
            source.enabled = body.enabled
        if body.endpointUrl is not None:
            if source.source_type != BROWSER_SOURCE_REMOTE_CDP:
                raise StewardError(
                    "本机浏览器助手没有可编辑的远程地址"
                )
            from app.shared.encryption import encrypt

            source.endpoint_url_encrypted = encrypt(
                browser_sources.validate_remote_endpoint(body.endpointUrl)
            )
        if body.headers is not None:
            if source.source_type != BROWSER_SOURCE_REMOTE_CDP:
                raise StewardError("本机浏览器助手没有远程请求头")
            from app.shared.encryption import encrypt

            source.headers_encrypted = encrypt(json.dumps(
                browser_sources.normalize_headers(body.headers),
                ensure_ascii=False,
            ))
        db.commit()
        db.refresh(source)
        return _ok(browser_sources.source_out(source))
    except StewardError as exc:
        raise HTTPException(422, str(exc)) from exc


def rotate_browser_source_token(
    source_id: str,
    db: Session,
    current_user,
):
    try:
        source = browser_sources.require_source(
            db,
            source_id,
            getattr(current_user, "id", None),
        )
        return _ok({
            "sourceId": source.id,
            "pairingToken": browser_sources.rotate_companion_token(
                db, source
            ),
        })
    except StewardError as exc:
        raise HTTPException(422, str(exc)) from exc


def delete_browser_source(
    source_id: str,
    db: Session,
    current_user,
):
    try:
        source = browser_sources.require_source(
            db,
            source_id,
            getattr(current_user, "id", None),
        )
    except StewardError as exc:
        raise HTTPException(404, str(exc)) from exc
    conversations = db.query(StewardConversation).filter(
        StewardConversation.browser_source_id == source.id
    ).all()
    for conversation in conversations:
        try:
            browser_manager.close(conversation.id)
        except Exception:  # noqa: BLE001
            logger.warning(
                "关闭已删除来源的浏览器会话失败: %s",
                conversation.id,
                exc_info=True,
            )
        conversation.browser_source_id = None
    # 超助等其它域复用同一运行时的会话也按来源一并释放；库侧由 FK SET NULL 兜底
    browser_manager.close_by_source(f"{source.source_type}:{source.id}")
    db.delete(source)
    db.commit()


def test_browser_source(
    source_id: str,
    db: Session,
    current_user,
    *,
    browser_error_fn,
):
    try:
        target = browser_sources.resolve_target(
            db,
            (
                None
                if source_id == browser_sources.MANAGED_SOURCE_ID
                else source_id
            ),
            getattr(current_user, "id", None),
            admin=getattr(current_user, "role", "") == "admin",
        )
        return _ok(browser_manager.test_target(target))
    except Exception as exc:  # noqa: BLE001
        raise browser_error_fn(exc)


def download_browser_companion():
    path = Path(__file__).with_name("companion_client.mjs")
    return FileResponse(
        path,
        filename="openontology-browser-companion.mjs",
        media_type="text/javascript",
    )
