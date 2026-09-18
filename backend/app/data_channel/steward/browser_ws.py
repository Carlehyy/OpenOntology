"""Ticket-authenticated live browser WebSocket (kept outside JWT dependencies)."""
from __future__ import annotations

import asyncio
import hmac
import json
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.config import settings
from app.data_channel.steward.browser_runtime import browser_manager
from app.data_channel.steward.browser_sources import token_hash
from app.data_channel.steward.companion import companion_hub
from app.data_channel.steward.models import BROWSER_SOURCE_COMPANION, StewardBrowserSource
from app.database import SessionLocal

router = APIRouter()


def _secure_websocket(websocket: WebSocket) -> bool:
    forwarded = (websocket.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    return websocket.url.scheme == "wss" or forwarded == "https"


@router.websocket("/browser/companion/connect")
async def browser_companion(websocket: WebSocket):
    """Authenticate a local companion and expose its CDP on server loopback only."""
    if settings.environment == "production" and not _secure_websocket(websocket):
        await websocket.close(code=4403, reason="browser companion requires HTTPS/WSS")
        return
    await websocket.accept()
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        auth = json.loads(raw)
    except Exception:
        await websocket.close(code=4401, reason="companion authentication required")
        return
    source_id = str(auth.get("sourceId") or "")
    supplied_hash = token_hash(str(auth.get("token") or ""))
    db = SessionLocal()
    try:
        source = db.query(StewardBrowserSource).filter(
            StewardBrowserSource.id == source_id,
            StewardBrowserSource.source_type == BROWSER_SOURCE_COMPANION,
            StewardBrowserSource.enabled.is_(True),
        ).first()
        if not source or not hmac.compare_digest(source.device_token_hash or "", supplied_hash):
            await websocket.close(code=4401, reason="invalid companion credentials")
            return
        source.last_seen_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()
    connection = await companion_hub.register(source_id, websocket)
    try:
        await websocket.send_json({"type": "ready", "sourceId": source_id})
        await connection.run()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        await companion_hub.unregister(source_id, connection)


@router.websocket("/conversations/{conversation_id}/browser/live")
async def browser_live(websocket: WebSocket, conversation_id: str, ticket: str = ""):
    valid, _user_id = browser_manager.redeem_ticket(ticket, conversation_id)
    if not valid:
        await websocket.close(code=4401, reason="invalid or expired browser ticket")
        return
    await websocket.accept()
    client_id = secrets.token_urlsafe(18)
    try:
        await browser_manager.attach_live(conversation_id)
    except Exception:
        await websocket.close(code=1011, reason="browser handoff unavailable")
        return
    stopped = asyncio.Event()
    send_lock = asyncio.Lock()

    async def send_json(payload: dict) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    async def send_frames() -> None:
        min_interval = max(33, int(settings.steward_browser_frame_interval_ms)) / 1000
        last_version = -1
        sent_at = 0.0
        while not stopped.is_set():
            try:
                frame, version = await browser_manager.next_live_frame(
                    conversation_id, client_id=client_id,
                    after_version=last_version, timeout=1.0)
                if version == last_version:
                    # Idle pages do not produce JPEG frames; still push
                    # collaboration so transient/held expiry is visible.
                    await send_json({
                        "type": "collaboration",
                        "collaboration": frame.get("collaboration"),
                    })
                    continue
                last_version = version
                now = asyncio.get_running_loop().time()
                wait = min_interval - (now - sent_at)
                if wait > 0:
                    await asyncio.sleep(wait)
                await send_json({"type": "frame", **frame})
                sent_at = asyncio.get_running_loop().time()
            except Exception as exc:  # noqa: BLE001
                await send_json({"type": "error", "message": str(exc)})
                await asyncio.sleep(1)

    async def receive_input() -> None:
        while not stopped.is_set():
            message = await websocket.receive_json()
            if message.get("type") == "ping":
                await send_json({"type": "pong"})
                continue
            try:
                if message.get("type") == "control":
                    status = await browser_manager.set_live_control(
                        conversation_id, client_id, str(message.get("action") or ""))
                else:
                    status = await browser_manager.input(
                        conversation_id, message, client_id=client_id)
                await send_json({
                    "type": "collaboration",
                    "collaboration": status,
                })
            except Exception as exc:  # noqa: BLE001 — 单个坏按键不应断开实时会话
                await send_json({"type": "error", "message": str(exc)})

    sender = asyncio.create_task(send_frames())
    receiver = asyncio.create_task(receive_input())
    try:
        done, _ = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        stopped.set()
        sender.cancel()
        receiver.cancel()
        await asyncio.gather(sender, receiver, return_exceptions=True)
        try:
            await browser_manager.detach_live(conversation_id, client_id)
        except Exception:
            pass
