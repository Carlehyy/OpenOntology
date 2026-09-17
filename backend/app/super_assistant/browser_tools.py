"""超级助手的浏览器协作内置工具：与数据管家共用 BrowserManager 运行时。

10 个 browser_* 工具的 schema 与 steward toolkit 同名同构（描述去掉管家语境），
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


def _dispatch(
    db: Session,
    *,
    conversation,
    name: str,
    arguments: dict[str, Any],
) -> Any:
    cid = conversation.id
    if name == "browser_open":
        target = browser_sources.resolve_target(
            db, conversation.browser_source_id, conversation.owner_id,
        )
        return browser_manager.start(
            cid, str(arguments.get("url") or ""), user_id=conversation.owner_id,
            actor="agent", browser_target=target,
            session_workspace=files_workspace.session_workspace())
    if name == "browser_state":
        return browser_manager.state(cid, actor="agent")
    if name == "browser_navigate":
        return browser_manager.navigate(cid, str(arguments.get("url") or ""), actor="agent")
    if name == "browser_click_text":
        return browser_manager.click_text(cid, str(arguments.get("text") or ""), actor="agent")
    if name == "browser_click_element":
        return browser_manager.click_element(cid, int(arguments.get("element_index") or 0), actor="agent")
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
