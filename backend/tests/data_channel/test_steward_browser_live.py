"""Live browser capture: CDP screencast + cheap human navigation."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.data_channel.steward.browser_runtime import BrowserManager, BrowserRuntimeError
from app.shared.config import settings


class FakeCdp:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.handler = None

    def on(self, event: str, handler) -> None:
        assert event == "Page.screencastFrame"
        self.handler = handler

    async def send(self, method: str, params: dict | None = None) -> dict:
        self.sent.append((method, params or {}))
        return {}

    async def detach(self) -> None:
        self.sent.append(("detach", {}))


class FakeContext:
    def __init__(self, cdp: FakeCdp) -> None:
        self.cdp = cdp

    async def new_cdp_session(self, _page) -> FakeCdp:
        return self.cdp


def _manager() -> BrowserManager:
    manager = BrowserManager.__new__(BrowserManager)
    manager._sessions = {}
    manager._live_clients = {}
    manager._live_leases = {}
    manager._user_controls = {}
    return manager


def _session(page, conversation_id: str = "c1"):
    return SimpleNamespace(
        conversation_id=conversation_id,
        page=page,
        operation_lock=asyncio.Lock(),
        screencast_lock=asyncio.Lock(),
        screencast_active=False,
        screencast_failed=False,
        cdp_session=None,
        latest_frame=None,
        frame_version=0,
        frame_condition=asyncio.Condition(),
        touch=lambda: None,
        save_state_if_due=AsyncMock(),
        save_state=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_screencast_acks_and_serves_frames_without_screenshot(monkeypatch):
    monkeypatch.setattr(settings, "steward_browser_frame_interval_ms", 0)
    cdp = FakeCdp()
    page = SimpleNamespace(
        url="https://example.com/live",
        is_closed=lambda: False,
        context=FakeContext(cdp),
        screenshot=AsyncMock(side_effect=AssertionError("live path must not screenshot")),
    )
    session = _session(page)
    manager = _manager()
    manager._sessions = {"c1": session}
    manager._live_clients = {"c1": 1}

    await manager._ensure_screencast(session)
    assert ("Page.enable", {}) in cdp.sent
    assert ("Page.startScreencast", {
        "format": "jpeg", "quality": 55, "maxWidth": 1365, "maxHeight": 768, "everyNthFrame": 1,
    }) in cdp.sent
    assert cdp.handler is not None

    await manager._on_screencast_frame(session, cdp, {"data": "abc123", "sessionId": 7})
    assert ("Page.screencastFrameAck", {"sessionId": 7}) in cdp.sent
    assert session.latest_frame["data"] == "abc123"
    assert session.frame_version == 1

    frame, version = await manager._next_live_frame(
        "c1", after_version=-1, timeout=0)
    assert frame["data"] == "abc123"
    assert frame["url"] == "https://example.com/live"
    assert version == 1
    page.screenshot.assert_not_called()


@pytest.mark.asyncio
async def test_live_frame_falls_back_to_screenshot_when_screencast_unavailable():
    page = SimpleNamespace(
        url="https://example.com/fallback",
        is_closed=lambda: False,
        context=SimpleNamespace(
            new_cdp_session=AsyncMock(side_effect=RuntimeError("no cdp")),
        ),
        screenshot=AsyncMock(return_value=b"\xff\xd8\xff"),
    )
    session = _session(page)
    manager = _manager()
    manager._sessions = {"c1": session}

    frame, version = await manager._next_live_frame("c1", after_version=-1, timeout=0)
    assert version == 1
    assert frame["url"] == "https://example.com/fallback"
    assert frame["data"]
    page.screenshot.assert_awaited()
    assert session.screencast_active is False
    assert session.screencast_failed is True

    await manager._next_live_frame("c1", after_version=-1, timeout=0)
    assert page.context.new_cdp_session.await_count == 1
    assert page.screenshot.await_count == 2


@pytest.mark.asyncio
async def test_user_navigate_commits_without_scraping_the_document(monkeypatch):
    monkeypatch.setattr(
        "app.data_channel.steward.browser_runtime._safe_url", lambda url: url)

    scraped = {"locator": 0, "evaluate": 0}

    class FakePage:
        url = "https://example.com/from"
        wait_until = None

        def is_closed(self) -> bool:
            return False

        async def title(self) -> str:
            return "Next"

        async def goto(self, url, wait_until=None, timeout=None):
            self.url = url
            self.wait_until = wait_until
            return SimpleNamespace(status=200)

        def locator(self, _selector):
            scraped["locator"] += 1
            raise AssertionError("user navigate must not scrape")

        async def evaluate(self, *_args, **_kwargs):
            scraped["evaluate"] += 1
            raise AssertionError("user navigate must not scrape")

    page = FakePage()
    session = _session(page)
    manager = _manager()
    manager._sessions = {"c1": session}

    result = await manager._navigate("c1", "https://example.com/next", actor="user")
    assert page.wait_until == "commit"
    assert result["url"] == "https://example.com/next"
    assert result["title"] == "Next"
    assert result["text"] == ""
    assert result["elements"] == []
    assert scraped == {"locator": 0, "evaluate": 0}


@pytest.mark.asyncio
async def test_agent_navigate_still_scrapes_after_domcontentloaded(monkeypatch):
    monkeypatch.setattr(
        "app.data_channel.steward.browser_runtime._safe_url", lambda url: url)

    class FakeLocator:
        async def inner_text(self, timeout=None):
            return "body text"

    class FakePage:
        url = "https://example.com/from"
        wait_until = None

        def is_closed(self) -> bool:
            return False

        async def title(self) -> str:
            return "Agent"

        async def goto(self, url, wait_until=None, timeout=None):
            self.url = url
            self.wait_until = wait_until
            return SimpleNamespace(status=200)

        def locator(self, _selector):
            return FakeLocator()

        async def evaluate(self, *_args, **_kwargs):
            return [{
                "index": 0, "tag": "a", "type": "", "text": "link",
                "downloadable": False, "x": 0, "y": 0, "width": 10, "height": 10,
                "visible": True,
            }]

    page = FakePage()
    session = _session(page)
    manager = _manager()
    manager._sessions = {"c1": session}

    result = await manager._navigate("c1", "https://example.com/next", actor="agent")
    assert page.wait_until == "domcontentloaded"
    assert result["text"] == "body text"
    assert result["elements"]


@pytest.mark.asyncio
async def test_user_navigate_rejects_http_error_without_waiting_for_load(monkeypatch):
    monkeypatch.setattr(
        "app.data_channel.steward.browser_runtime._safe_url", lambda url: url)

    class FakePage:
        url = "https://example.com/from"
        wait_until = None

        def is_closed(self) -> bool:
            return False

        async def goto(self, url, wait_until=None, timeout=None):
            self.wait_until = wait_until
            return SimpleNamespace(status=567)

    page = FakePage()
    session = _session(page)
    manager = _manager()
    manager._sessions = {"c1": session}

    with pytest.raises(BrowserRuntimeError, match="567"):
        await manager._navigate("c1", "https://example.com/blocked", actor="user")
    assert page.wait_until == "commit"


@pytest.mark.asyncio
async def test_hover_move_does_not_claim_user_control():
    mouse = SimpleNamespace(
        move=AsyncMock(),
        down=AsyncMock(),
        up=AsyncMock(),
    )
    page = SimpleNamespace(is_closed=lambda: False, mouse=mouse, url="https://example.com")
    session = _session(page)
    manager = _manager()
    manager._sessions = {"c1": session}

    status = await manager._input(
        "c1", {"type": "mouse", "action": "move", "x": 8, "y": 12})
    assert status["controller"] == "agent"
    assert manager._user_controls == {}
    mouse.move.assert_awaited_once()

    await manager._input(
        "c1", {"type": "mouse", "action": "down", "x": 8, "y": 12, "button": "left"})
    assert manager._control_status("c1")["controller"] == "user"
    mouse.down.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_screencast_start_detaches_cdp(monkeypatch):
    monkeypatch.setattr(settings, "steward_browser_frame_interval_ms", 0)
    cdp = FakeCdp()

    class BoomContext:
        async def new_cdp_session(self, _page):
            return cdp

    async def boom_send(method, params=None):
        cdp.sent.append((method, params or {}))
        if method == "Page.startScreencast":
            raise RuntimeError("no screencast")
        return {}

    cdp.send = boom_send
    page = SimpleNamespace(
        url="https://example.com/x",
        is_closed=lambda: False,
        context=BoomContext(),
        screenshot=AsyncMock(return_value=b"\xff\xd8\xff"),
    )
    session = _session(page)
    manager = _manager()
    manager._sessions = {"c1": session}

    await manager._ensure_screencast(session)
    assert session.screencast_failed is True
    assert session.cdp_session is None
    assert ("Page.stopScreencast", {}) in cdp.sent
    assert ("detach", {}) in cdp.sent


@pytest.mark.asyncio
async def test_expired_http_lease_stops_screencast():
    cdp = FakeCdp()
    session = _session(SimpleNamespace(is_closed=lambda: False, url="https://example.com"))
    session.screencast_active = True
    session.cdp_session = cdp
    manager = _manager()
    manager._sessions = {"c1": session}
    manager._live_clients = {}
    manager._live_leases = {"c1": {"lease": time.monotonic() - 1}}
    submitted: list[asyncio.Task] = []
    manager._loop = asyncio.get_running_loop()
    manager._submit = lambda coro: submitted.append(asyncio.get_running_loop().create_task(coro))

    assert manager._is_live("c1") is False
    assert submitted
    await submitted[0]
    assert session.screencast_active is False
    assert session.cdp_session is None
