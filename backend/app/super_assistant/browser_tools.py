"""超级助手的浏览器协作内置工具：与数据管家共用 BrowserManager 运行时。

browser_* 工具的 schema 与 steward toolkit 同名同构（描述去掉管家语境），
执行经 execute_browser_tool 统一分派：归属校验复用会话 404 语义，浏览器产物
（登录态/捕获/下载）落超助会话工作区。
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.data_channel.steward import browser_sources
from app.data_channel.steward.browser_runtime import (
    BrowserRuntimeError,
    browser_manager,
)
from app.data_channel.steward.service import StewardError
from app.data_channel.steward.workspace import WorkspaceError
from app.super_assistant import conversation_service, files_workspace

BROWSER_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "browser_open",
        "description": "在当前会话的独立浏览器中打开合法 http/https 网址。浏览器登录态只属于本会话；用户可在实时浏览器中旁观且不会阻止你操作，需要账号密码时请用户在实时浏览器画面中手动登录，绝不向用户索要密码。",
        "parameters": {
            "type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"],
        },
    },
    {
        "name": "browser_state",
        "description": "读取当前会话浏览器的 URL、标题、可见正文和交互元素。不会读取密码、Cookie 或本地存储。",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "browser_navigate",
        "description": "让当前会话浏览器跳转到另一个合法 http/https 网址。",
        "parameters": {
            "type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"],
        },
    },
    {
        "name": "browser_click_text",
        "description": "在当前页面按可见文字进行真实浏览器点击。点击前先 browser_state；若点击触发原生下载，结果的 downloadedFiles 会列出已保存到会话的文件。",
        "parameters": {
            "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
        },
    },
    {
        "name": "browser_click_element",
        "description": "按 browser_state 返回的元素 index 进行真实浏览器点击，适合无文字的图标、图片和下载控件；若触发原生下载，文件会自动保存到当前会话。",
        "parameters": {
            "type": "object",
            "properties": {"element_index": {"type": "integer", "minimum": 0}},
            "required": ["element_index"],
        },
    },
    {
        "name": "browser_page_resources",
        "description": "列出当前页面可保存的图片、音视频和链接资源，返回稳定的元素 index、标签、文字和 resourceUrl；下载页面图片前优先使用。",
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "按资源 URL、alt 或可见文字过滤"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
        },
    },
    {
        "name": "browser_save_resource",
        "description": "把 browser_page_resources 返回的图片、媒体或链接资源直接保存到当前会话；支持 http/https、data: 和 blob:，只能使用该工具返回的元素 index。",
        "parameters": {
            "type": "object",
            "properties": {
                "element_index": {"type": "integer", "minimum": 0},
                "filename": {"type": "string", "description": "可选的保存文件名"},
            },
            "required": ["element_index"],
        },
    },
    {
        "name": "browser_scroll",
        "description": "滚动当前会话浏览器的页面。往底部翻用 position=bottom，往下翻一屏用 page_down，回到顶部用 top。不要假装用 JS 或键盘滚动。",
        "parameters": {
            "type": "object",
            "properties": {
                "position": {
                    "type": "string",
                    "enum": ["top", "bottom", "page_down", "page_up"],
                    "description": "bottom=滚到页底，page_down=向下滚一屏，top=回顶部，page_up=向上一屏",
                },
            },
            "required": ["position"],
        },
    },
    {
        "name": "browser_type",
        "description": "向普通输入框填写非敏感文本；密码框会被系统拒绝，账号密码必须由用户在实时画面中手动输入。",
        "parameters": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector"},
                "text": {"type": "string"},
                "press_enter": {"type": "boolean"},
            },
            "required": ["selector", "text"],
        },
    },
    {
        "name": "browser_network_requests",
        "description": "查看当前会话浏览器捕获到的 XHR/fetch/API 和文件请求，返回请求 id、响应结构、样例和分页线索；认证头会脱敏。查页面数据来源时必须使用本工具。",
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "按 URL 或响应内容过滤"},
                "limit": {"type": "integer", "description": "默认 50，最大 100"},
            },
        },
    },
    {
        "name": "download_captured_file",
        "description": "将已捕获的 GET 文件请求在同一浏览器登录态下重放，并保存到当前会话隔离空间。只能使用 browser_network_requests 返回的 capture_id。",
        "parameters": {
            "type": "object", "properties": {"capture_id": {"type": "string"}}, "required": ["capture_id"],
        },
    },
]

BROWSER_TOOL_NAMES = frozenset(schema["name"] for schema in BROWSER_TOOL_SCHEMAS)

# YIYA-36 P2-5：超级助手浏览器通道不得访问平台自身管理面。受保护来源
# 恒非空（不依赖可为空配置）：回环任意端口 + 平台 compose 内部服务名
# 任意端口 + 平台公开地址派生（三个 base_url 设置项均有非空默认值，
# 生产由部署覆盖）+ ASSISTANT_BROWSER_PROTECTED_ORIGINS 额外别名。
_LOOPBACK_HOSTNAMES = frozenset({
    "localhost", "127.0.0.1", "::1", "0.0.0.0",
})
_INTERNAL_PLATFORM_HOSTNAMES = frozenset({
    "backend", "frontend", "browser", "minio", "neo4j", "nats", "redis",
    "db", "python_kernel_gateway", "pipeline_executor", "task_worker",
})
# 作用于已存在浏览器会话的工具：执行前必须能核实当前页面来源（含读取类
# 工具——页面内容同样属管理面），覆盖会话先于规则建立、重定向落地等
# open/navigate 之外的路径。
_PAGE_ACTING_TOOLS = frozenset({
    "browser_state",
    "browser_click_text",
    "browser_click_element",
    "browser_scroll",
    "browser_type",
    "browser_page_resources",
    "browser_save_resource",
    "browser_network_requests",
    "download_captured_file",
})
_NO_SESSION_MARKER = "尚未启动浏览器"


def _protected_origin_denied(url: str) -> bool:
    from urllib.parse import urlparse

    from app.config import settings

    target = urlparse(str(url or ""))
    host = (target.hostname or "").rstrip(".").lower()
    if not host:
        return False
    if host in _LOOPBACK_HOSTNAMES or host in _INTERNAL_PLATFORM_HOSTNAMES:
        return True
    raw_values = (
        settings.super_assistant_public_api_base_url,
        settings.pipeline_file_public_app_base_url,
        settings.pipeline_file_public_api_base_url,
        settings.assistant_browser_protected_origins,
    )
    for raw in raw_values:
        for origin in str(raw or "").split(","):
            origin = origin.strip()
            if not origin:
                continue
            protected = urlparse(origin if "//" in origin else f"https://{origin}")
            protected_host = (protected.hostname or "").rstrip(".").lower()
            if protected_host and host == protected_host:
                return True
    return False


def _protected_origin_denial() -> dict[str, str]:
    return {
        "error": "capability_boundary_unenforced",
        "message": "超级助手浏览器通道不得访问平台自身管理来源；"
                   "用户直连管理请使用普通页面登录",
    }


def _boundary_unavailable() -> dict[str, str]:
    return {
        "error": "capability_boundary_unverifiable",
        "message": "无法核实当前页面来源（浏览器状态暂不可读），"
                   "已阻止本次超级助手浏览器操作",
    }


def _current_page_verdict(cid: str) -> str:
    """当前页面来源裁决：protected / ok / no_session / unavailable。

    unavailable 时调用方必须阻断（fail-closed，不得旁路副作用工具）；
    no_session 交由工具自身的错误路径，不做猜测。
    """
    try:
        state = browser_manager.state(cid, actor="agent")
    except BrowserRuntimeError as exc:
        return "no_session" if _NO_SESSION_MARKER in str(exc) else "unavailable"
    except Exception:  # noqa: BLE001 — 预检基础设施失败一律不可核实
        return "unavailable"
    url = str((state or {}).get("url") or "")
    if not url:
        return "unavailable"
    return "protected" if _protected_origin_denied(url) else "ok"


def _capture_origin_denied(cid: str, capture_id: str) -> bool:
    rows = browser_manager.list_captures(
        cid, limit=100, session_workspace=files_workspace.session_workspace())
    for row in rows or []:
        if str(row.get("id") or "") == str(capture_id or ""):
            return _protected_origin_denied(str(row.get("url") or ""))
    return False




def _dispatch(
    db: Session,
    *,
    conversation,
    name: str,
    arguments: dict[str, Any],
) -> Any:
    # 裁决 4（v2 安全回退）：assistant 浏览器通道 fail-closed — 所有工具
    # （含只读观测/网络捕获）在访问 browser source/session/storage 之前
    # 拒绝；正常用户浏览器与共享 steward 功能不经此路径，不受影响。
    from app.config import settings

    if not getattr(settings, "assistant_browser_channel_enabled", False):
        return {
            "error": "capability_boundary_unenforced",
            "message": "超级助手浏览器通道暂未开放：等待隔离上下文/凭据/"
                       "网络策略证据与独立验证后由部署显式启用"
                       "（assistant_browser_channel_enabled）",
        }
    cid = conversation.id
    if name in ("browser_open", "browser_navigate") and _protected_origin_denied(
            str(arguments.get("url") or "")):
        return _protected_origin_denial()
    if name in _PAGE_ACTING_TOOLS:
        verdict = _current_page_verdict(cid)
        if verdict == "protected":
            return _protected_origin_denial()
        if verdict == "unavailable":
            return _boundary_unavailable()
    if name == "download_captured_file" and _capture_origin_denied(
            cid, str(arguments.get("capture_id") or "")):
        return _protected_origin_denial()
    if name == "browser_open":
        target = browser_sources.resolve_target(
            db, conversation.browser_source_id, conversation.owner_id,
        )
        result = browser_manager.start(
            cid, str(arguments.get("url") or ""), user_id=conversation.owner_id,
            actor="agent", browser_target=target,
            session_workspace=files_workspace.session_workspace())
        # 导航后立即核对最终落地 URL：重定向到受保护来源时不回显页面内容
        if _protected_origin_denied(str((result or {}).get("url") or "")):
            return _protected_origin_denial()
        return result
    if name == "browser_state":
        return browser_manager.state(cid, actor="agent")
    if name == "browser_navigate":
        result = browser_manager.navigate(cid, str(arguments.get("url") or ""), actor="agent")
        # 同 browser_open：落地 URL 命中受保护来源时不回显页面内容
        if _protected_origin_denied(str((result or {}).get("url") or "")):
            return _protected_origin_denial()
        return result
    if name == "browser_click_text":
        return browser_manager.click_text(cid, str(arguments.get("text") or ""), actor="agent")
    if name == "browser_click_element":
        return browser_manager.click_element(cid, int(arguments.get("element_index") or 0), actor="agent")
    if name == "browser_scroll":
        return browser_manager.scroll(
            cid, str(arguments.get("position") or "page_down"), actor="agent")
    if name == "browser_page_resources":
        rows = browser_manager.page_resources(
            cid, arguments.get("keyword"), int(arguments.get("limit") or 50), actor="agent")
        return {"resources": rows, "count": len(rows),
                "hint": "选择目标资源的 element_index 交给 browser_save_resource。"}
    if name == "browser_save_resource":
        row = browser_manager.save_page_resource(
            cid, int(arguments.get("element_index") or 0), arguments.get("filename"), actor="agent")
        return {"file": row, "notice": "页面资源已保存到当前会话，可在会话文件面板查看并打包。"}
    if name == "browser_type":
        return browser_manager.type_text(
            cid, str(arguments.get("selector") or ""), str(arguments.get("text") or ""),
            bool(arguments.get("press_enter")), actor="agent")
    if name == "browser_network_requests":
        rows = browser_manager.list_captures(
            cid, arguments.get("keyword"), int(arguments.get("limit") or 50),
            session_workspace=files_workspace.session_workspace())
        return {"requests": rows, "count": len(rows),
                "hint": "优先比较用户操作前后的新增请求；pagination 字段给出页码/offset/cursor 线索。"}
    if name == "download_captured_file":
        row = browser_manager.download(cid, str(arguments.get("capture_id") or ""), actor="agent")
        return {"file": row, "notice": "文件已保存到当前会话，可在会话文件面板查看并随会话一键打包。"}
    return {"error": f"未知工具 {name}"}


def execute_browser_tool(
    db: Session,
    *,
    owner_id: str,
    conversation_id: str,
    name: str,
    arguments: dict[str, Any],
) -> str:
    try:
        conversation = conversation_service._conversation(db, owner_id, conversation_id)
        result = _dispatch(db, conversation=conversation, name=name, arguments=arguments or {})
    except HTTPException as exc:
        result = {"error": str(exc.detail)}
    except (BrowserRuntimeError, StewardError, WorkspaceError) as exc:
        result = {"error": str(exc)}
    return json.dumps(result, ensure_ascii=False, default=str)
