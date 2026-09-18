"""
数据管家受限工具集 — LLM 与 n8n 交互的全部动词

职权收敛到流水线编排和当前会话文件两类写权限：
  1. 新建流水线：create_pipeline 只收名称+描述，在 n8n 建 Webhook→输出 骨架并
     登记为未发布流水线（等价于流水线列表「新建 n8n 流水线」），不激活。
  2. 辅助编排：update_workflow 只能改「未发布 且 未启用」的流水线定义。

会话文件工具可以创建、编辑和删除当前会话内的文档，但所有路径解析仍由会话工作区
完成，不能访问其他会话或主机目录。用户明确要求时，execute_pipeline 可以触发一次
受控执行预览，结束后恢复原启停状态且不写资产湖。其余工具全是只读支撑，只服务编排质量：查看/
列表、节点目录与深挖(describe_node)、表达式&模式参考(n8n_reference)、静态体检
(check_workflow)、探测数据源(probe_url)、只读执行诊断(inspect_runs)、凭据缺口
检查(check_credentials)。治理边界（与本体 agent 同一哲学：agent 干活，人签字）：
  - 创建的 workflow 一律不激活；激活只发生在用户于流水线编辑向导「发布」时
    ——发布是生命周期的唯一入口，工具集里没有这个动词。
  - 已发布流水线永久封版；需要变更时新建流水线。未发布但在 n8n 侧启用的草稿须先停用。
  - 管家可按用户明确指令触发一次执行预览，但不能借此发布、永久启停或写入资产湖。
  - 纳管、归档/删除都不再是管家职权（归档在流水线列表操作）。
  - 凭据(credentials)无法经 API 创建 — 工具只能引用用户在 n8n 界面配好的凭据。
"""
from __future__ import annotations

import json
import logging
import mimetypes
import re
import uuid
from contextlib import ExitStack
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit, urlunsplit

import httpx

from sqlalchemy.orm import Session

from app.config import settings
from app.api_hub import agent_service as api_hub_agent
from app.api_hub import config as api_hub_config, db as api_hub_db, executor as api_hub_executor
from app.data_channel.file_assets.service import (
    authenticated_download_url,
    platform_public_api_origin,
    public_share_url,
)
from app.settings.workflows.n8n_client import N8nApiError, N8nClient
from app.data_channel.steward import browser_sources, file_tools, service, workspace
from app.data_channel.steward.browser_runtime import (
    BrowserRuntimeError, browser_manager, validate_target_url,
)
from app.data_channel.steward.models import N8nPipeline, StewardConversation, STATUS_ARCHIVED
from app.data_channel.steward.node_catalog import CATEGORIES, describe_node, find_nodes
from app.data_channel.steward.references import reference
from app.data_channel.steward.service import StewardError

logger = logging.getLogger(__name__)

_EXEC_SAMPLE_ROWS = 5   # inspect_runs 默认展示的末节点样例行数
_EXEC_MAX_SAMPLE_ROWS = 20
_EXEC_MAX_SAMPLE_COLUMNS = 12
_EXEC_CELL_CHARS = 240
_SENSITIVE_OUTPUT_KEY = re.compile(
    r"(?:pass(?:word)?|secret|token|authorization|cookie|api[-_]?key|credential|private[-_]?key)",
    re.IGNORECASE,
)
_SHARE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{40,200}$")


TOOL_DEFS: list[dict] = [
    {
        "name": "steward_overview",
        "description": "查看数据管家全景：n8n 连接健康状况、受管流水线的状态统计。开场或用户问'现在有哪些流水线/什么状态'时先用它。",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "list_pipelines",
        "description": "列出受管的 n8n 数据流水线记录（含发布状态：未发布/已发布）。要编排哪条流水线时先用它拿到 record_id。",
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "按名称过滤（可选）"},
            },
        },
    },
    {
        "name": "get_workflow",
        "description": "查看一条受管流水线的完整 n8n workflow JSON（nodes/connections）与治理状态。修改任何工作流之前必须先看当前定义。",
        "parameters": {
            "type": "object",
            "properties": {
                "record_id": {"type": "string", "description": "数据管家记录 id（list_pipelines 返回的 id）"},
            },
            "required": ["record_id"],
        },
    },
    {
        "name": "create_pipeline",
        "description": (
            "新建一条 n8n 数据流水线：只需名称与描述。"
            "后台自动在 n8n 建好 Webhook→输出 的骨架工作流并登记为未发布流水线"
            "（等价于用户在流水线列表点「新建流水线 → n8n → 填名称/描述」）——不激活、不调度。"
            "创建后用 get_workflow 看骨架，再用 update_workflow 逐步补全取数与整形节点。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "流水线名称（同时作为 n8n 工作流名）"},
                "description": {"type": "string", "description": "这条数据流水线的用途说明（可选）"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "update_workflow",
        "description": "编排/修改受管流水线的 workflow 定义（整体替换 nodes/connections，先 get_workflow 取当前值改后回传）。只能修改「未发布 且 未启用」的流水线；已发布版本永久封版，变更应新建流水线；草稿若在 n8n 侧启用则先停用。",
        "parameters": {
            "type": "object",
            "properties": {
                "record_id": {"type": "string"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "nodes": {"type": "array", "items": {"type": "object"}},
                "connections": {"type": "object"},
                "settings": {"type": "object"},
            },
            "required": ["record_id"],
        },
    },
    {
        "name": "check_workflow",
        "description": "只读体检一条受管流水线是否符合平台约定：触发器、连接完整性、Webhook 约定、凭据引用。编排完/让用户去发布前自检一遍，修复 error 级问题（不改动工作流、不激活）。",
        "parameters": {
            "type": "object",
            "properties": {"record_id": {"type": "string"}},
            "required": ["record_id"],
        },
    },
    {
        "name": "execute_pipeline",
        "description": (
            "触发一条受管 n8n 流水线的新执行，并把本次末节点真实输出作为在线表格返回。"
            "仅在用户明确说‘执行/运行/重新跑/触发’时使用；不要用 inspect_runs 冒充新执行。"
            "未发布流水线会在锁内临时激活并在结束后恢复，已发布流水线会先校验发布 revision；"
            "两者都不会发布、永久启停或把产物写入数据资产湖。草稿执行权限独立于发布凭证，"
            "不能因为 n8n 未返回 activeVersionId 等发布元数据而拒绝执行。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "record_id": {"type": "string"},
                "payload": {"type": "object", "description": "可选：POST 给受管 Webhook 的 JSON 载荷"},
                "sample_limit": {"type": "integer", "description": "在线表格展示前几行，默认 5，最多 20"},
                "columns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "只展示用户点名的字段，最多 12 个",
                },
            },
            "required": ["record_id"],
        },
    },
    {
        "name": "inspect_runs",
        "description": "只读诊断一条受管流水线已有的最近 n8n 执行：状态、报错、各节点产出行数，并把末节点真实样例作为在线表格返回。用户说「看上次输出 / 展示已有结果 / 为什么失败」时使用；它不会触发新执行。用户明确要求重新执行时改用 execute_pipeline。",
        "parameters": {
            "type": "object",
            "properties": {
                "record_id": {"type": "string"},
                "limit": {"type": "integer", "description": "取最近几次执行（默认 5）"},
                "sample_limit": {"type": "integer", "description": "在线表格展示末节点前几行，默认 5，最多 20"},
                "columns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "只展示用户点名的字段，最多 12 个；不传则按输出字段顺序展示前 12 个",
                },
            },
            "required": ["record_id"],
        },
    },
    {
        "name": "check_credentials",
        "description": "只读检查一条受管流水线的凭据缺口：列出各节点引用的凭据，比对实例已配置的凭据（只看名字/类型，不碰密文），标出缺哪些。管家不能建凭据，只能明确告诉用户去 n8n 界面配好哪些、以及可复用哪个已有凭据。",
        "parameters": {
            "type": "object",
            "properties": {"record_id": {"type": "string"}},
            "required": ["record_id"],
        },
    },
    {
        "name": "list_node_types",
        "description": f"查平台维护的 n8n 常用节点目录（type/typeVersion/关键参数模板）。拼节点前不确定类型或版本时先查这里。category 可选：{', '.join(CATEGORIES)}。",
        "parameters": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": list(CATEGORIES)},
                "keyword": {"type": "string"},
            },
        },
    },
    {
        "name": "describe_node",
        "description": "查一个 n8n 节点的完整编排知识：typeVersion + 参数说明 + 可直接抄的 worked example + 常见坑。配 HTTP 认证 / Set 赋值 / Code / 数据库 这类节点前先查，别凭记忆拼参数。",
        "parameters": {
            "type": "object",
            "properties": {
                "node_type": {"type": "string", "description": "节点类型：全名 n8n-nodes-base.httpRequest、短名 httpRequest、或关键字 http 都行"},
            },
            "required": ["node_type"],
        },
    },
    {
        "name": "n8n_reference",
        "description": "查编排参考：expressions（{{ }} 表达式语法与坑）/ code（Code 节点写法与返回契约）/ files（平台 FileRef 与附件网关）/ patterns（可复用骨架）。",
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "enum": ["expressions", "code", "files", "patterns"]},
            },
            "required": ["topic"],
        },
    },
    {
        "name": "probe_url",
        "description": "探测一个数据源 URL（唯一的对外探测手段）：发送 GET 请求，返回状态码、Content-Type；JSON 响应给出结构摘要与样例，HTML/文本给出标题与截断正文。用户给了网址/API 就先探测再设计流水线，不要凭空假设数据形态。只支持 http/https GET，响应会截断。",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "http/https 地址"},
                "headers": {
                    "type": "object",
                    "description": "可选请求头（如 Accept、Authorization——需用户在对话里提供的凭证）",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "list_session_files",
        "description": "列出当前会话隔离空间中的上传文件和网页下载文件。文件只属于本会话，不能访问其他目录。",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "read_session_file",
        "description": "读取当前会话某个文件的已解析文本（Word/PPT/Excel/PDF/Markdown 等）。先 list_session_files 获取 artifact_id。",
        "parameters": {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string"},
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "从第几个字符开始读取，默认 0；长文件按返回的 next_offset 继续分页",
                },
                "max_chars": {"type": "integer", "description": "最多返回字符数，默认 30000"},
            },
            "required": ["artifact_id"],
        },
    },
    {
        "name": "create_session_file",
        "description": "在当前会话隔离空间创建文件。支持 docx/pptx/xlsx/pdf/md/txt/csv；不能传路径，也不能写入其他会话。用户要求生成 Word、报告、表格、演示文稿或 Markdown 时直接使用。",
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "带受支持扩展名的文件名，如 报告.docx"},
                "content": {"type": "string", "description": "文件正文；Excel/CSV 可作为逗号或制表符分隔文本"},
                "title": {"type": "string", "description": "文档或演示标题（可选）"},
                "rows": {"type": "array", "items": {}, "description": "Excel/CSV 可选数据，对象数组或二维数组"},
            },
            "required": ["filename", "content"],
        },
    },
    {
        "name": "edit_session_file",
        "description": "编辑当前会话中的文件并保存为新版本（原文件保留，避免误覆盖）。支持替换或追加正文；只能使用 list_session_files 返回的 artifact_id。",
        "parameters": {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string"},
                "content": {"type": "string"},
                "mode": {"type": "string", "enum": ["replace", "append"], "description": "默认 replace"},
                "output_filename": {"type": "string", "description": "可选的新版本文件名"},
                "title": {"type": "string"},
                "rows": {"type": "array", "items": {}},
            },
            "required": ["artifact_id", "content"],
        },
    },
    {
        "name": "delete_session_file",
        "description": "删除当前会话内的一个文件。删除不可撤销，只有用户明确要求删除时才能使用；只能接收当前会话 artifact_id。",
        "parameters": {
            "type": "object",
            "properties": {"artifact_id": {"type": "string"}},
            "required": ["artifact_id"],
        },
    },
    {
        "name": "browser_open",
        "description": "在当前会话的独立浏览器中打开合法 http/https 网址。浏览器登录态只属于本会话；用户可在实时浏览器中旁观且不会阻止你操作，需要账号密码时应让用户点击“暂停管家，我来处理”后手动登录，再点“继续交给数据管家”。",
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
    {
        "name": "register_proxy_interface",
        "description": "把已捕获 API 注册到平台接口代理，并返回供 n8n 使用的代理 URL。可选择复制该请求的认证头；认证值不会返回给模型。分页参数会被拆成可覆盖 query。",
        "parameters": {
            "type": "object",
            "properties": {
                "capture_id": {"type": "string"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "include_auth": {"type": "boolean", "description": "复制浏览器捕获的 Authorization/Cookie 等认证头"},
            },
            "required": ["capture_id", "name"],
        },
    },
    {
        "name": "list_proxy_interfaces",
        "description": "列出当前用户有权管理的接口代理接口。托管、编辑、调用或编排接口前先用它定位 interface_id。",
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "按名称、说明、URL、方法或分组过滤"},
                "group": {"type": "string", "description": "可选分组名"},
            },
        },
    },
    {
        "name": "get_proxy_interface",
        "description": "读取一个接口的完整托管配置、动态参数契约和 revision。敏感参数值始终隐藏；编辑或编排前必须先读取。",
        "parameters": {
            "type": "object",
            "properties": {"interface_id": {"type": "integer"}},
            "required": ["interface_id"],
        },
    },
    {
        "name": "create_proxy_interface",
        "description": "新建一个内部接口草稿。不会自动开放 MCP 或发布 HTTP 地址；敏感 Header 必须在接口管理界面配置，不能交给模型。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "url": {"type": "string", "description": "HTTP/HTTPS 绝对 URL；Path 参数用 {name} 占位"},
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]},
                "group": {"type": "string"},
                "description": {"type": "string"},
                "query_params": {"type": "object", "additionalProperties": {}},
                "headers": {"type": "object", "additionalProperties": {}},
                "body_type": {"type": "string", "enum": ["none", "json", "form", "multipart", "raw"]},
                "body_content": {"type": "string"},
                "parameters": {
                    "type": "array",
                    "description": "动态参数契约：name/location(path|query|header|body)/value_type/required/default/description/dynamic/sensitive",
                    "items": {"type": "object"},
                },
            },
            "required": ["name", "url"],
        },
    },
    {
        "name": "update_proxy_interface",
        "description": "按字段修改接口并产生新 revision。必须传 get_proxy_interface 刚读取的 expected_revision；未提及的字段和隐藏密钥保持不变。",
        "parameters": {
            "type": "object",
            "properties": {
                "interface_id": {"type": "integer"},
                "expected_revision": {"type": "integer"},
                "changes": {
                    "type": "object",
                    "description": "可改 name/url/method/group/description/query_params/headers/body_type/body_content/parameters；删除参数用 remove_query_params/remove_headers",
                },
            },
            "required": ["interface_id", "expected_revision", "changes"],
        },
    },
    {
        "name": "call_proxy_interface",
        "description": "直接试调一个托管接口。支持动态 path/query/header/body 和当前会话 multipart 文件；POST/PUT/PATCH/DELETE 必须先向用户确认副作用并传 confirm_side_effect=true。",
        "parameters": {
            "type": "object",
            "properties": {
                "interface_id": {"type": "integer"},
                "path": {"type": "object", "additionalProperties": {}},
                "query": {"type": "object", "additionalProperties": {}},
                "headers": {"type": "object", "additionalProperties": {}},
                "body": {},
                "files": {
                    "type": "array",
                    "description": "multipart 文件：field + 当前会话 artifact_id",
                    "items": {"type": "object", "properties": {"field": {"type": "string"}, "artifact_id": {"type": "string"}}},
                },
                "confirm_side_effect": {"type": "boolean"},
            },
            "required": ["interface_id"],
        },
    },
    {
        "name": "orchestrate_proxy_interface",
        "description": "把接口代理中的接口作为受管 HTTP Request 节点插入一条未发布且未启用的 n8n 流水线。节点固定接口 revision，只引用 n8n Header Auth 凭据，不写入密钥。先 get_workflow 和 get_proxy_interface，再确认参数映射。",
        "parameters": {
            "type": "object",
            "properties": {
                "record_id": {"type": "string"},
                "interface_id": {"type": "integer"},
                "after_node_name": {"type": "string", "description": "插入到哪个现有节点之后；默认 Webhook"},
                "node_name": {"type": "string"},
                "credential_name": {"type": "string", "description": "n8n Header Auth 凭据名；默认平台配置 API Hub Internal Proxy"},
                "path_bindings": {"type": "object", "additionalProperties": {}},
                "query_bindings": {"type": "object", "additionalProperties": {}},
                "header_bindings": {"type": "object", "additionalProperties": {}},
                "body_binding": {},
            },
            "required": ["record_id", "interface_id"],
        },
    },
]

API_HUB_TOOL_NAMES = {
    "register_proxy_interface",
    "list_proxy_interfaces",
    "get_proxy_interface",
    "create_proxy_interface",
    "update_proxy_interface",
    "call_proxy_interface",
    "orchestrate_proxy_interface",
}

_PROBE_BODY_CAP = 200_000       # 读取响应体的字节上限
_PROBE_TEXT_SAMPLE = 2500       # HTML/文本回给 LLM 的样本长度
_PROBE_JSON_SAMPLE_ROWS = 3     # JSON 数组样例行数


def _json_shape(value: Any, depth: int = 0) -> Any:
    """把 JSON 压缩成"结构骨架"——保留键名与类型，砍掉海量数据。"""
    if depth >= 4:
        return "…"
    if isinstance(value, dict):
        return {k: _json_shape(v, depth + 1) for k, v in list(value.items())[:24]}
    if isinstance(value, list):
        head = _json_shape(value[0], depth + 1) if value else None
        return {"__type": f"array[{len(value)}]", "item": head}
    if isinstance(value, str):
        return value if len(value) <= 80 else value[:80] + "…"
    return value


def _normalize_nodes(nodes: list[dict]) -> list[dict]:
    """补全 LLM 常省略的字段：id / position / typeVersion / parameters。"""
    if not isinstance(nodes, list) or not nodes:
        raise StewardError("nodes 必须是非空数组。")
    normalized = []
    seen_names: set[str] = set()
    for i, raw in enumerate(nodes):
        if not isinstance(raw, dict):
            raise StewardError(f"nodes[{i}] 不是对象。")
        node = dict(raw)
        name = str(node.get("name") or "").strip()
        ntype = str(node.get("type") or "").strip()
        if not name or not ntype:
            raise StewardError(f"nodes[{i}] 缺少 name 或 type。")
        if name in seen_names:
            raise StewardError(f"节点 name 重复：「{name}」。n8n 要求节点名唯一（connections 以 name 为键）。")
        seen_names.add(name)
        node.setdefault("id", str(uuid.uuid4()))
        node.setdefault("typeVersion", 1)
        node.setdefault("parameters", {})
        if not node.get("position"):
            node["position"] = [260 * i, 300]
        normalized.append(node)
    return normalized


def _validate_connections(nodes: list[dict], connections: dict) -> None:
    if not isinstance(connections, dict):
        raise StewardError("connections 必须是对象（键为源节点 name）。")
    names = {n["name"] for n in nodes}
    for src, outs in connections.items():
        if src not in names:
            raise StewardError(f"connections 引用了不存在的源节点「{src}」。现有节点：{sorted(names)}")
        for branch in (outs or {}).values():
            for lane in branch or []:
                for target in lane or []:
                    tgt = (target or {}).get("node")
                    if tgt not in names:
                        raise StewardError(f"connections 引用了不存在的目标节点「{tgt}」。现有节点：{sorted(names)}")


def _contains_file_ref(value: Any) -> bool:
    if isinstance(value, dict):
        return value.get("$type") == "file_ref" or any(
            _contains_file_ref(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_file_ref(item) for item in value)
    return False


def _safe_share_url(value: Any) -> str | None:
    """Keep only this platform's canonical anonymous-download URL."""
    if not isinstance(value, str) or not value:
        return None
    try:
        candidate = urlsplit(value)
        expected = urlsplit(platform_public_api_origin())
    except (TypeError, ValueError):
        return None
    if (
        candidate.scheme != expected.scheme
        or candidate.netloc != expected.netloc
        or candidate.username is not None
        or candidate.password is not None
        or candidate.query
        or candidate.fragment
    ):
        return None
    parts = candidate.path.split("/")
    if (
        len(parts) != 7
        or parts[:4] != ["", "api", "public", "file-assets"]
        or parts[5:] != ["download", ""]
    ):
        # Canonical paths have no trailing slash, yielding six path segments.
        if (
            len(parts) != 6
            or parts[:4] != ["", "api", "public", "file-assets"]
            or parts[5] != "download"
        ):
            return None
    token = unquote(parts[4])
    if not _SHARE_TOKEN.fullmatch(token):
        return None
    return public_share_url(token)


def _safe_output_value(value: Any, key: str = "", depth: int = 0) -> Any:
    """Return a bounded, JSON-safe preview value without leaking credential-shaped fields."""
    if _SENSITIVE_OUTPUT_KEY.search(key):
        return "[已隐藏]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= _EXEC_CELL_CHARS else value[:_EXEC_CELL_CHARS] + "…"
    if isinstance(value, dict) and value.get("$type") == "file_ref":
        asset_id = str(value.get("id") or "")
        name = str(value.get("name") or "附件")[:240]
        if asset_id:
            try:
                size = max(0, int(value.get("size") or 0))
            except (TypeError, ValueError):
                size = 0
            # The route is reconstructed instead of trusting n8n output.  The
            # runner already scope-validates fresh executions; this also keeps
            # inspect_runs previews from turning an arbitrary URL into a link.
            safe_ref = {
                "$type": "file_ref",
                "id": asset_id,
                "name": name,
                "size": size,
                "content_type": str(value.get("content_type") or "application/octet-stream")[:200],
                "sha256": str(value.get("sha256") or "")[:64],
                "download_url": f"/api/v2/file-assets/{asset_id}/download",
                "authenticated_url": authenticated_download_url(asset_id),
                "share_url": _safe_share_url(value.get("share_url")),
            }
            return safe_ref
    if depth >= 3:
        return "[嵌套内容已折叠]"
    if isinstance(value, dict):
        safe = {str(k): _safe_output_value(v, str(k), depth + 1)
                for k, v in list(value.items())[:24]}
    elif isinstance(value, list):
        safe = [_safe_output_value(item, "", depth + 1) for item in value[:20]]
    else:
        text = str(value)
        return text if len(text) <= _EXEC_CELL_CHARS else text[:_EXEC_CELL_CHARS] + "…"
    # Keep bounded containers structured so nested file_ref values remain
    # downloadable in the UI. Oversized arbitrary JSON still falls back to a
    # truncated string, preserving the existing cell-size budget.
    text = json.dumps(safe, ensure_ascii=False, default=str)
    return (safe if _contains_file_ref(safe) or len(text) <= _EXEC_CELL_CHARS
            else text[:_EXEC_CELL_CHARS] + "…")


def _execution_table_preview(items: list, sample_limit: int | None = None,
                             columns: list[str] | None = None) -> dict:
    """Build the small structured table exposed to the browser and the LLM."""
    if columns is not None and not isinstance(columns, list):
        raise StewardError("columns 必须是字段名数组。")
    try:
        requested_limit = _EXEC_SAMPLE_ROWS if sample_limit is None else int(sample_limit)
    except (TypeError, ValueError):
        raise StewardError(f"sample_limit 必须是 1 到 {_EXEC_MAX_SAMPLE_ROWS} 的整数。")
    if requested_limit < 1:
        raise StewardError(f"sample_limit 必须是 1 到 {_EXEC_MAX_SAMPLE_ROWS} 的整数。")
    row_limit = min(requested_limit, _EXEC_MAX_SAMPLE_ROWS)

    raw_rows: list[dict[str, Any]] = []
    for item in items:
        value = item.get("json") if isinstance(item, dict) and isinstance(item.get("json"), dict) else item
        raw_rows.append(value if isinstance(value, dict) else {"value": value})

    available: list[str] = []
    for row in raw_rows:
        for key in row:
            name = str(key)
            if name not in available:
                available.append(name)

    requested = []
    for column in columns or []:
        name = str(column).strip()
        if name and name not in requested:
            requested.append(name)
    if len(requested) > _EXEC_MAX_SAMPLE_COLUMNS:
        requested = requested[:_EXEC_MAX_SAMPLE_COLUMNS]

    selected = requested or available[:_EXEC_MAX_SAMPLE_COLUMNS]
    missing = [column for column in selected if column not in available]
    selected = [column for column in selected if column in available]
    safe_rows = [
        {column: _safe_output_value(row.get(column), column) for column in selected}
        for row in raw_rows[:row_limit]
    ]
    redacted = [column for column in selected if _SENSITIVE_OUTPUT_KEY.search(column)]
    return {
        "title": "最近一次执行输出",
        "columns": selected,
        "rows": safe_rows,
        "totalRows": len(raw_rows),
        "shownRows": len(safe_rows),
        "totalColumns": len(available),
        "omittedColumns": max(0, len(available) - len(selected)),
        "missingColumns": missing,
        "redactedColumns": redacted,
        "truncated": len(raw_rows) > len(safe_rows) or len(available) > len(selected),
    }


def _execution_brief(e: dict) -> dict:
    return {
        "id": e.get("id"),
        "status": e.get("status"),
        "startedAt": e.get("startedAt"),
        "stoppedAt": e.get("stoppedAt"),
        "mode": e.get("mode"),
    }


_N8N_EXPRESSION = re.compile(r"^=\{\{\s*(.*?)\s*\}\}$", re.DOTALL)
_N8N_EXPRESSION_BLOCKED = re.compile(
    r"(?:\bprocess\b|\brequire\s*\(|\bconstructor\b|\bglobalThis\b|\$env\b|[;`])"
)


def _validate_n8n_binding(value: Any) -> None:
    """Reject dangerous expressions while keeping n8n's JSON-field syntax.

    A single expression that constructs the complete object looks attractive
    but fails with ``invalid syntax`` on supported real n8n versions.  The
    compiler therefore places each validated expression in its own body field.
    """
    if isinstance(value, str):
        matched = _N8N_EXPRESSION.fullmatch(value.strip())
        if matched:
            expression = matched.group(1).strip()
            if (
                not expression
                or len(expression) > 1000
                or _N8N_EXPRESSION_BLOCKED.search(expression)
            ):
                raise StewardError("n8n 参数表达式包含不允许的内容。")
        elif value.lstrip().startswith("={{"):
            raise StewardError("n8n 参数表达式格式无效，必须完整写成 ={{ ... }}。")
    if isinstance(value, dict):
        for item in value.values():
            _validate_n8n_binding(item)
    elif isinstance(value, list):
        for item in value:
            _validate_n8n_binding(item)


def _proxy_body_parameters(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten the proxy envelope into n8n's expression-safe key/value mode.

    The real n8n version used by the platform evaluates expressions reliably in
    individual body-parameter values, but not inside JSON text; whole-object
    expressions also fail parsing.  The internal proxy inflates these dotted
    keys back into path/query/header/body structures.
    """
    parameters: list[dict[str, Any]] = []

    def append(name: str, value: Any) -> None:
        _validate_n8n_binding(value)
        if isinstance(value, dict):
            for child_name, child in value.items():
                append(f"{name}.{child_name}", child)
            return
        if isinstance(value, list):
            # Literal arrays are safe in key/value mode; dynamic arrays should
            # bind the whole body through one expression instead.
            if any(isinstance(item, (dict, list)) for item in value):
                raise StewardError("n8n 代理参数暂不支持嵌套数组，请先在 Code/Set 节点整理。")
        parameters.append({"name": name, "value": value})

    append("interface_revision", payload["interface_revision"])
    for section in ("path", "query", "headers"):
        for name, value in (payload.get(section) or {}).items():
            append(f"{section}.{name}", value)
    if "body" in payload:
        append("body", payload["body"])
    return parameters


class ToolRunner:
    """一次对话回合的工具执行器 — 记录触达的流水线，供前端刷新受管流水线面板。"""

    def __init__(self, db: Session, user_id: str | None, conversation_id: str | None,
                 *, api_hub_allowed: bool = False):
        self.db = db
        self.user_id = user_id
        self.conversation_id = conversation_id
        self.api_hub_allowed = bool(api_hub_allowed)
        self.touched_pipeline_ids: list[str] = []
        self._client: N8nClient | None = None

    @property
    def client(self) -> N8nClient:
        if self._client is None:
            self._client = service.get_n8n_client(self.db)
        return self._client

    def _touch(self, record_id: str) -> None:
        if record_id not in self.touched_pipeline_ids:
            self.touched_pipeline_ids.append(record_id)

    def _require_api_hub(self) -> None:
        if not self.api_hub_allowed:
            raise StewardError("当前用户没有「接口代理 → 接口管理」权限，数据管家不能托管或调用接口。")

    def run(self, name: str, args: dict) -> dict:
        handler = getattr(self, f"tool_{name}", None)
        if handler is None:
            return {"error": f"未知工具: {name}"}
        try:
            return handler(**(args or {}))
        except StewardError as e:
            return {"error": str(e)}
        except N8nApiError as e:
            return {"error": f"n8n API 错误 (HTTP {e.status_code}): {e.message}"}
        except (workspace.WorkspaceError, BrowserRuntimeError) as e:
            return {"error": str(e)}
        except api_hub_agent.AgentInterfaceError as e:
            return {"error": str(e)}
        except TypeError as e:
            return {"error": f"参数不合法: {e}"}

    # ── 只读 ──────────────────────────────────────────────────────

    def tool_steward_overview(self) -> dict:
        status = service.n8n_config_status(self.db)
        overview: dict[str, Any] = {"n8n": status}
        if status["configured"] and status["enabled"]:
            try:
                self.client.test_connection()
                overview["n8n"]["reachable"] = True
            except Exception as e:  # noqa: BLE001
                overview["n8n"]["reachable"] = False
                overview["n8n"]["error"] = str(e)[:200]
        records = (self.db.query(N8nPipeline)
                   .filter(N8nPipeline.status != STATUS_ARCHIVED)
                   .order_by(N8nPipeline.updated_at.desc()).all())
        by_status: dict[str, int] = {}
        recent = []
        for r in records:
            pub = service.shadow_status(self.db, r)
            by_status[pub] = by_status.get(pub, 0) + 1
            if len(recent) < 10:
                recent.append({"id": r.id, "name": r.name, "pipelineStatus": pub})
        overview["pipelines"] = {
            "total": len(records),
            "byStatus": by_status,   # published=已发布可调度 / draft=未发布
            "recent": recent,
        }
        return overview

    def tool_list_pipelines(self, keyword: str | None = None) -> dict:
        # 只列受管流水线；不再枚举 n8n 上未纳管的工作流（纳管已不在管家职权内）
        records = (self.db.query(N8nPipeline)
                   .filter(N8nPipeline.status != STATUS_ARCHIVED)
                   .order_by(N8nPipeline.updated_at.desc()).limit(50).all())
        if keyword:
            kw = keyword.lower()
            records = [r for r in records if kw in (r.name or "").lower()]
        return {"managed": [service.record_out(self.db, r) for r in records]}

    def tool_get_workflow(self, record_id: str) -> dict:
        rec = service.require_record(self.db, record_id)
        workflow = self.client.get_workflow(rec.n8n_workflow_id)
        # 草稿快照随远端更新；已发布快照是不可变发布证据，任何只读查看都不能覆盖。
        _snapshot, changed = service.refresh_draft_snapshot(self.db, rec, workflow)
        if changed:
            self.db.commit()
        return {
            "record": service.record_out(self.db, rec, active=bool(workflow.get("active"))),
            "workflow": {
                "name": workflow.get("name"),
                "nodes": workflow.get("nodes"),
                "connections": workflow.get("connections"),
                "settings": workflow.get("settings"),
            },
        }

    def tool_list_node_types(self, category: str | None = None, keyword: str | None = None) -> dict:
        nodes = find_nodes(category=category, keyword=keyword)
        return {"nodes": nodes, "hint": "不在目录里的 n8n 节点也可以使用，但请优先用目录内节点以保证类型/版本正确。"}

    def tool_describe_node(self, node_type: str) -> dict:
        return describe_node(node_type)

    def tool_n8n_reference(self, topic: str) -> dict:
        return reference(topic)

    def tool_probe_url(self, url: str, headers: dict | None = None) -> dict:
        url = validate_target_url(url)

        req_headers = {"User-Agent": "OntoPrompt-DataSteward/1.0", "Accept": "*/*"}
        for k, v in (headers or {}).items():
            if isinstance(k, str) and isinstance(v, str) and k.lower() != "host":
                req_headers[k] = v
        try:
            with httpx.Client(timeout=15, follow_redirects=False) as client:
                current = url
                for _ in range(6):
                    resp = client.get(current, headers=req_headers)
                    if not resp.is_redirect:
                        break
                    location = resp.headers.get("location")
                    if not location:
                        break
                    current = validate_target_url(urljoin(current, location))
                else:
                    raise StewardError("探测失败：重定向次数过多")
        except httpx.HTTPError as e:
            raise StewardError(f"探测失败：{e}") from e

        body = resp.content[:_PROBE_BODY_CAP]
        content_type = resp.headers.get("content-type", "")
        out: dict[str, Any] = {
            "status": resp.status_code,
            "finalUrl": str(resp.url),
            "contentType": content_type,
            "bytes": len(resp.content),
            "truncated": len(resp.content) > _PROBE_BODY_CAP,
        }

        text = body.decode(resp.encoding or "utf-8", errors="replace")
        parsed = None
        if "json" in content_type or text[:1] in ("{", "["):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
        if parsed is not None:
            out["kind"] = "json"
            out["shape"] = _json_shape(parsed)
            rows = parsed if isinstance(parsed, list) else None
            if rows is None and isinstance(parsed, dict):
                # 常见包裹形态：取第一个数组字段作为"行"
                rows = next((v for v in parsed.values() if isinstance(v, list)), None)
            if isinstance(rows, list) and rows:
                out["sampleRows"] = rows[:_PROBE_JSON_SAMPLE_ROWS]
            out["hint"] = "JSON 数据源：HTTP Request 节点直接拉取，再按 shape 用 Set/Code 整形成行。"
        else:
            out["kind"] = "html" if "html" in content_type or "<html" in text[:500].lower() else "text"
            m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
            if m:
                out["title"] = m.group(1).strip()[:200]
            # 粗剥 script/style 后取正文样本，给 LLM 判断页面形态
            stripped = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.I | re.S)
            stripped = re.sub(r"<[^>]+>", " ", stripped)
            stripped = re.sub(r"\s+", " ", stripped).strip()
            out["textSample"] = stripped[:_PROBE_TEXT_SAMPLE]
            if out["kind"] == "html":
                out["hint"] = ("HTML 页面：优先找它的 JSON API（浏览器开发者工具/常见 /api 路径），"
                               "实在没有再用 HTML 节点(extractHtmlContent) 按 CSS 选择器解析。")
        return out

    # ── 会话文件与同会话浏览器 ─────────────────────────────────────

    def _conversation(self) -> str:
        if not self.conversation_id:
            raise StewardError("该操作需要先建立数据管家会话")
        return self.conversation_id

    def tool_list_session_files(self) -> dict:
        rows = workspace.list_files(self._conversation())
        return {"files": rows, "count": len(rows),
                "note": "所有路径均限制在当前会话；浏览器登录态和内部捕获日志不会出现在文件清单或打包结果中。"}

    def tool_read_session_file(
        self,
        artifact_id: str,
        max_chars: int | None = None,
        offset: int | None = None,
    ) -> dict:
        cid = self._conversation()
        row, _ = workspace.require_file(cid, artifact_id)
        start = max(0, int(offset or 0))
        limit = max(1, min(int(max_chars or 30_000), 30_000))
        text = workspace.extracted_text(
            cid, artifact_id, limit, offset=start)
        total = int(row.get("extractedChars") or 0)
        next_offset = start + len(text)
        truncated = next_offset < total
        return {"file": row, "content": text,
                "offset": start,
                "next_offset": next_offset if truncated else None,
                "truncated": truncated,
                "note": None if text else "该文件没有可用的文本解析结果；仍可作为原文件下载。"}

    def tool_create_session_file(self, filename: str, content: str,
                                 title: str | None = None, rows: list | None = None) -> dict:
        row = file_tools.create(self._conversation(), filename, content, title=title, rows=rows)
        return {"file": row, "notice": "文件已创建在当前会话隔离空间，可在会话文件面板查看、下载或打包。"}

    def tool_edit_session_file(self, artifact_id: str, content: str,
                               mode: str | None = None, output_filename: str | None = None,
                               title: str | None = None, rows: list | None = None) -> dict:
        row = file_tools.edit(self._conversation(), artifact_id, content, mode=mode or "replace",
                              output_filename=output_filename, title=title, rows=rows)
        return {"file": row, "notice": "已另存为当前会话中的新版本，原文件未被覆盖。"}

    def tool_delete_session_file(self, artifact_id: str) -> dict:
        cid = self._conversation()
        row, _ = workspace.require_file(cid, artifact_id)
        workspace.delete_file(cid, artifact_id)
        return {"deleted": True, "artifactId": artifact_id, "filename": row["filename"]}

    def tool_browser_open(self, url: str) -> dict:
        conv = self.db.query(StewardConversation).filter(
            StewardConversation.id == self._conversation()).first()
        target = browser_sources.resolve_target(
            self.db, conv.browser_source_id if conv else None, self.user_id)
        return browser_manager.start(
            self._conversation(), url, user_id=self.user_id, actor="agent", browser_target=target)

    def tool_browser_state(self) -> dict:
        return browser_manager.state(self._conversation(), actor="agent")

    def tool_browser_navigate(self, url: str) -> dict:
        return browser_manager.navigate(self._conversation(), url, actor="agent")

    def tool_browser_click_text(self, text: str) -> dict:
        return browser_manager.click_text(self._conversation(), text, actor="agent")

    def tool_browser_click_element(self, element_index: int) -> dict:
        return browser_manager.click_element(
            self._conversation(), element_index, actor="agent")

    def tool_browser_scroll(self, position: str) -> dict:
        return browser_manager.scroll(
            self._conversation(), position, actor="agent")

    def tool_browser_page_resources(self, keyword: str | None = None,
                                    limit: int | None = None) -> dict:
        rows = browser_manager.page_resources(
            self._conversation(), keyword, limit or 50, actor="agent")
        return {"resources": rows, "count": len(rows),
                "hint": "选择目标资源的 element_index 交给 browser_save_resource。"}

    def tool_browser_save_resource(self, element_index: int,
                                   filename: str | None = None) -> dict:
        row = browser_manager.save_page_resource(
            self._conversation(), element_index, filename, actor="agent")
        return {"file": row, "notice": "页面资源已保存到当前会话，可在会话文件面板查看并打包。"}

    def tool_browser_type(self, selector: str, text: str, press_enter: bool | None = None) -> dict:
        return browser_manager.type_text(
            self._conversation(), selector, text, bool(press_enter), actor="agent")

    def tool_browser_network_requests(self, keyword: str | None = None, limit: int | None = None) -> dict:
        rows = browser_manager.list_captures(self._conversation(), keyword, limit or 50)
        return {"requests": rows, "count": len(rows),
                "hint": "优先比较用户操作前后的新增请求；pagination 字段给出页码/offset/cursor 线索。"}

    def tool_download_captured_file(self, capture_id: str) -> dict:
        row = browser_manager.download(self._conversation(), capture_id, actor="agent")
        return {"file": row, "notice": "文件已保存到当前会话，可在会话文件面板查看并随会话一键打包。"}

    # ── 接口代理托管与调用 ────────────────────────────────────────

    def tool_list_proxy_interfaces(self, keyword: str | None = None,
                                   group: str | None = None) -> dict:
        self._require_api_hub()
        return api_hub_agent.list_interfaces_for_agent(keyword=keyword, group=group)

    def tool_get_proxy_interface(self, interface_id: int) -> dict:
        self._require_api_hub()
        return api_hub_agent.get_interface_for_agent(int(interface_id))

    def tool_create_proxy_interface(
        self, name: str, url: str, method: str = "GET",
        group: str | None = None, description: str | None = None,
        query_params: dict | list | None = None,
        headers: dict | list | None = None,
        body_type: str | None = None, body_content: str | None = None,
        parameters: list[dict] | None = None,
    ) -> dict:
        self._require_api_hub()
        return api_hub_agent.create_interface_for_agent(
            actor_user_id=self.user_id,
            name=name,
            url=url,
            method=method,
            group=group or "",
            description=description or "",
            query_params=query_params,
            headers=headers,
            body_type=body_type or "none",
            body_content=body_content or "",
            parameters=parameters,
        )

    def tool_update_proxy_interface(
        self, interface_id: int, expected_revision: int, changes: dict,
    ) -> dict:
        self._require_api_hub()
        if not isinstance(changes, dict) or not changes:
            raise StewardError("changes 必须包含至少一个要修改的字段。")
        return api_hub_agent.update_interface_for_agent(
            iid=int(interface_id),
            actor_user_id=self.user_id,
            expected_revision=int(expected_revision),
            changes=changes,
        )

    def tool_call_proxy_interface(
        self, interface_id: int,
        path: dict | list | None = None,
        query: dict | list | None = None,
        headers: dict | list | None = None,
        body: Any = None,
        files: list[dict] | None = None,
        confirm_side_effect: bool | None = None,
    ) -> dict:
        self._require_api_hub()
        interface = api_hub_agent.load_interface(int(interface_id))
        if interface["method"] not in {"GET", "HEAD", "OPTIONS"} and not confirm_side_effect:
            raise StewardError(
                f"接口使用 {interface['method']}，可能产生业务写入。请先向用户展示目标和参数，"
                "获得明确确认后再传 confirm_side_effect=true 调用。"
            )

        with ExitStack() as stack:
            runtime_files: list[api_hub_executor.RequestFile] | None = None
            if files is not None:
                runtime_files = []
                configured = {
                    str(item.get("key")): item
                    for item in interface.get("file_fields") or []
                    if item.get("key")
                }
                counts: dict[str, int] = {}
                for item in files:
                    if not isinstance(item, dict):
                        raise StewardError("files 必须是 field/artifact_id 对象数组。")
                    field = str(item.get("field") or "").strip()
                    artifact_id = str(item.get("artifact_id") or "").strip()
                    definition = configured.get(field)
                    if definition is None:
                        raise StewardError(f"接口未配置 multipart 文件字段：{field or '(空)'}")
                    counts[field] = counts.get(field, 0) + 1
                    if counts[field] > 1 and not definition.get("multiple"):
                        raise StewardError(f"文件字段 {field} 不允许多文件。")
                    artifact, file_path = workspace.require_file(self._conversation(), artifact_id)
                    stream = stack.enter_context(open(file_path, "rb"))
                    runtime_files.append(api_hub_executor.RequestFile(
                        field_name=field,
                        filename=artifact.get("filename") or file_path.name,
                        stream=stream,
                        content_type=artifact.get("mimeType") or artifact.get("mime_type")
                        or mimetypes.guess_type(file_path.name)[0] or "application/octet-stream",
                        size=file_path.stat().st_size,
                    ))
            return api_hub_agent.call_interface_for_agent(
                iid=int(interface_id),
                path=path,
                query=query,
                headers=headers,
                body=body,
                files=runtime_files,
            )

    def tool_orchestrate_proxy_interface(
        self, record_id: str, interface_id: int,
        after_node_name: str | None = None, node_name: str | None = None,
        credential_name: str | None = None,
        path_bindings: dict | list | None = None,
        query_bindings: dict | list | None = None,
        header_bindings: dict | list | None = None,
        body_binding: Any = None,
    ) -> dict:
        self._require_api_hub()
        rec = service.require_record(self.db, record_id)
        service.require_orchestrable(self.db, rec, self.client)
        interface = api_hub_agent.load_interface(int(interface_id))

        # Validate every dynamic key before compiling it into workflow JSON.
        path_pairs = api_hub_agent.validate_runtime_pairs(interface, "path", path_bindings)
        query_pairs = api_hub_agent.validate_runtime_pairs(interface, "query", query_bindings)
        header_pairs = api_hub_agent.validate_runtime_pairs(interface, "header", header_bindings)
        body_fields = api_hub_agent.validate_runtime_body(
            interface, body_binding, allow_n8n_expression=True,
        ) if body_binding is not None else set()
        binding_maps = {
            "path": {item["key"]: item["value"] for item in path_pairs},
            "query": {item["key"]: item["value"] for item in query_pairs},
            "header": {item["key"]: item["value"] for item in header_pairs},
            "body": {name: True for name in body_fields},
        }
        configured_defaults = {
            "query": {str(item.get("key")) for item in interface.get("query_params") or [] if item.get("key")},
            "header": {str(item.get("key")) for item in interface.get("headers") or [] if item.get("key")},
            "path": set(),
            "body": set(),
        }
        if interface.get("body_content") and body_binding is None:
            # A saved request body is the governed default. Runtime bindings
            # only have to cover required fields that are not already saved.
            configured_defaults["body"] = {
                str(item.get("name"))
                for item in interface.get("parameter_schema") or []
                if item.get("location") == "body" and item.get("name")
            }
        missing_required = []
        for parameter in interface.get("parameter_schema") or []:
            location = str(parameter.get("location") or "")
            name = str(parameter.get("name") or "")
            if (
                parameter.get("required")
                and parameter.get("dynamic", True)
                and name not in binding_maps.get(location, {})
                and name not in configured_defaults.get(location, set())
            ):
                missing_required.append(f"{location}.{name}")
        if missing_required:
            raise StewardError("缺少必填的 n8n 动态参数绑定：" + ", ".join(missing_required))
        if body_binding is not None and (interface.get("body_type") or "none") == "none":
            raise StewardError("该接口未配置请求 Body，不能添加 body_binding。")

        desired_credential = (credential_name or settings.steward_proxy_credential_name).strip()
        try:
            available_credentials = self.client.list_credentials(limit=250)
        except Exception as exc:  # noqa: BLE001
            raise StewardError(f"无法读取 n8n 凭据元信息：{exc}") from exc
        credential = next(
            (item for item in available_credentials
             if str(item.get("name") or "") == desired_credential),
            None,
        )
        if credential is None:
            raise StewardError(
                f"n8n 中没有名为「{desired_credential}」的 Header Auth 凭据。"
                "请先在 n8n 安全配置 Authorization: Bearer <API_HUB_INTERNAL_PROXY_TOKEN>，"
                "数据管家不会把令牌写进工作流。"
            )

        workflow = self.client.get_workflow(rec.n8n_workflow_id)
        nodes = json.loads(json.dumps(workflow.get("nodes") or []))
        connections = json.loads(json.dumps(workflow.get("connections") or {}))
        names = {str(node.get("name") or "") for node in nodes}
        webhook = next(
            (node for node in nodes if node.get("type") == "n8n-nodes-base.webhook"),
            None,
        )
        after_name = (after_node_name or (webhook or {}).get("name") or "").strip()
        after_node = next((node for node in nodes if node.get("name") == after_name), None)
        if after_node is None:
            raise StewardError(f"工作流中不存在插入起点节点：{after_name or '(空)'}")
        requested_name = (node_name or f"接口代理 · {interface['name']}").strip()
        if not requested_name:
            raise StewardError("node_name 不能为空。")
        if requested_name in names:
            raise StewardError(f"工作流中已存在同名节点：{requested_name}")
        interface_marker = f"api-hub-interface:{interface['id']}@{interface['config_revision']}"
        if any(interface_marker in str(node.get("notes") or "") for node in nodes):
            raise StewardError("该接口 revision 已经编排进当前工作流，不能重复插入。")

        payload: dict[str, Any] = {
            "interface_revision": int(interface.get("config_revision") or 1),
        }
        if binding_maps["path"]:
            payload["path"] = binding_maps["path"]
        if binding_maps["query"]:
            payload["query"] = binding_maps["query"]
        if binding_maps["header"]:
            payload["headers"] = binding_maps["header"]
        if body_binding is not None:
            payload["body"] = body_binding

        position = after_node.get("position") or [0, 0]
        proxy_node = {
            "id": str(uuid.uuid4()),
            "name": requested_name,
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [int(position[0]) + 280, int(position[1])],
            "parameters": {
                "method": "POST",
                "url": f"{settings.steward_internal_proxy_base_url.rstrip('/')}/{interface['id']}/invoke",
                "authentication": "genericCredentialType",
                "genericAuthType": "httpHeaderAuth",
                "sendBody": True,
                "contentType": "json",
                "specifyBody": "keypair",
                "bodyParameters": {"parameters": _proxy_body_parameters(payload)},
                "options": {},
            },
            "credentials": {
                "httpHeaderAuth": {
                    "id": str(credential.get("id") or ""),
                    "name": desired_credential,
                }
            },
            "notesInFlow": True,
            "notes": (
                f"{interface_marker}\n由数据管家编排；接口配置变化后需重新绑定并重新发布流水线。"
            ),
        }
        nodes.append(proxy_node)
        previous = ((connections.get(after_name) or {}).get("main") or [[]])
        first_output = previous[0] if previous else []
        connections[after_name] = {
            "main": [[{"node": requested_name, "type": "main", "index": 0}]]
        }
        connections[requested_name] = {"main": [first_output]}

        updated = self.tool_update_workflow(
            record_id=record_id,
            nodes=nodes,
            connections=connections,
            settings=workflow.get("settings") or {},
        )
        if updated.get("error"):
            return updated
        return {
            **updated,
            "interfaceBinding": {
                "interfaceId": interface["id"],
                "interfaceName": interface["name"],
                "configRevision": interface.get("config_revision") or 1,
                "nodeName": requested_name,
                "afterNodeName": after_name,
                "credentialName": desired_credential,
                "dynamicParameters": {
                    key: sorted(value.keys()) for key, value in binding_maps.items() if value
                },
            },
            "notice": "接口代理节点已插入未发布工作流；请继续体检和试运行，最后由用户在编辑向导发布。",
        }

    def tool_register_proxy_interface(self, capture_id: str, name: str,
                                      description: str | None = None,
                                      include_auth: bool | None = None) -> dict:
        self._require_api_hub()
        capture = workspace.require_capture(self._conversation(), capture_id)
        split = urlsplit(capture["url"])
        base_url = urlunsplit((split.scheme, split.netloc, split.path, "", ""))
        query_params = [{"key": k, "value": v} for k, v in parse_qsl(split.query, keep_blank_values=True)]

        keep_headers = {"accept", "content-type", "referer", "origin"}
        sensitive = {"authorization", "cookie", "proxy-authorization", "x-api-key"}
        request_headers = capture.get("requestHeaders") or {}
        headers = []
        for key, value in request_headers.items():
            lowered = key.lower()
            if lowered in keep_headers or lowered.startswith("x-") or (include_auth and lowered in sensitive):
                if lowered not in {"host", "content-length", "accept-encoding"}:
                    headers.append({"key": key, "value": value})

        body_content = capture.get("requestBody") or ""
        content_type = str(request_headers.get("content-type") or request_headers.get("Content-Type") or "").lower()
        body_type = "none"
        if body_content:
            body_type = "json" if "json" in content_type else ("form" if "form" in content_type else "raw")

        parameter_schema = [
            {
                "name": item["key"],
                "location": "query",
                "value_type": "string",
                "required": False,
                "default": item["value"],
                "description": "由浏览器捕获的查询参数，可在试调或 n8n 中动态覆盖",
                "sensitive": bool(_SENSITIVE_OUTPUT_KEY.search(item["key"])),
                "dynamic": not bool(_SENSITIVE_OUTPUT_KEY.search(item["key"])),
            }
            for item in query_params
        ]

        now = datetime.now(timezone.utc).isoformat()
        with api_hub_db.get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO interfaces(name, description, group_name, method, url, query_params, headers, "
                "body_type, body_content, mcp_enabled, open_enabled, parameter_schema, "
                "created_by, updated_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    name.strip()[:200] or "浏览器发现接口",
                    (description or f"数据管家会话 {self._conversation()[:8]} 从页面网络请求发现").strip(),
                    "数据管家发现", str(capture.get("method") or "GET").upper(), base_url,
                    json.dumps(query_params, ensure_ascii=False), json.dumps(headers, ensure_ascii=False),
                    body_type, body_content, 0, 0,
                    json.dumps(parameter_schema, ensure_ascii=False),
                    str(self.user_id or ""), str(self.user_id or ""), now, now,
                ),
            )
            interface_id = int(cur.lastrowid)
        proxy_url = (
            f"{settings.steward_internal_proxy_base_url.rstrip('/')}/"
            f"{interface_id}/invoke"
        )
        return {
            "interface": {
                "id": interface_id, "name": name.strip(), "method": capture.get("method"),
                "targetUrl": base_url, "queryParams": [item["key"] for item in query_params],
                "authCopied": bool(include_auth and any(h["key"].lower() in sensitive for h in headers)),
            },
            "proxyUrl": proxy_url,
            "n8n": {
                "method": "POST",
                "body": {
                    "interface_revision": 1,
                    "query": {item["key"]: f"={{ $json.{item['key']} }}" for item in query_params},
                },
                "credential": (
                    f"Header Auth「{settings.steward_proxy_credential_name}」: "
                    "Authorization = Bearer <API_HUB_INTERNAL_PROXY_TOKEN>"
                ),
            },
            "notice": "接口已登记为内部草稿，未自动加入开放 MCP 清单。",
            "warning": None if api_hub_config.INTERNAL_PROXY_TOKEN else (
                "接口已登记，但 API_HUB_INTERNAL_PROXY_TOKEN 尚未配置；配置后 n8n 才能调用代理。"),
        }

    # ── 写入（治理规则内嵌） ──────────────────────────────────────

    def tool_create_pipeline(self, name: str, description: str | None = None) -> dict:
        # 与流水线列表「新建 n8n 流水线」完全同源：建 Webhook→输出 骨架、未激活、
        # 登记为未发布流水线。任意节点的搭建交给随后的 update_workflow。
        rec = service.bootstrap_blank_workflow(
            self.db, name, description or "", user_id=self.user_id)
        rec.conversation_id = self.conversation_id
        self.db.commit()
        self._touch(rec.id)
        return {
            "record": service.record_out(self.db, rec, active=False),
            "notice": ("已新建为未发布流水线（含 Webhook→输出 骨架，n8n 侧未激活），流水线列表已可见。"
                       "接下来用 get_workflow 看骨架、update_workflow 补全取数与整形节点；"
                       "编排、体检通过后，提醒用户到流水线列表的编辑向导中「发布并启用」。"),
        }

    def tool_update_workflow(self, record_id: str, name: str | None = None,
                             description: str | None = None,
                             nodes: list | None = None, connections: dict | None = None,
                             settings: dict | None = None) -> dict:
        rec = service.require_record(self.db, record_id)
        payload: dict[str, Any] = {}
        if name is not None and name.strip():
            payload["name"] = name.strip()
        if nodes is not None:
            payload["nodes"] = _normalize_nodes(nodes)
        if settings is not None:
            payload["settings"] = settings
        if connections is not None:
            base_nodes = payload.get("nodes")
            if base_nodes is None:
                base_nodes = (rec.workflow_snapshot or {}).get("nodes") or []
            _validate_connections(base_nodes, connections)
            payload["connections"] = connections
        elif payload.get("nodes") is not None:
            # 只换 nodes 不换 connections：校验旧连接仍指向存在的节点
            old_conns = (rec.workflow_snapshot or {}).get("connections") or {}
            _validate_connections(payload["nodes"], old_conns)

        if payload:
            # 编排守卫：写 n8n 之前拦——只放行「未发布 且 未启用」的流水线
            service.require_orchestrable(self.db, rec, self.client)
            updated = self.client.update_workflow(rec.n8n_workflow_id, payload)
            rec.workflow_snapshot = N8nClient.sanitize_workflow(updated)
            # 任何编排写入都会使此前“试跑输出 + 字段定义”发布凭证失效。
            # 即便操作者把节点改回肉眼相同，n8n revision 也已经变化，必须重跑。
            service.invalidate_validation_attestation(rec)
            if payload.get("name"):
                rec.name = payload["name"]
        if description is not None:
            rec.description = description.strip()
        # 名称/定义同步到影子行（不触碰发布状态）
        service.ensure_shadow_pipeline(self.db, rec)
        self.db.commit()
        self._touch(rec.id)
        return {"record": service.record_out(self.db, rec)}

    def tool_check_workflow(self, record_id: str) -> dict:
        rec = service.require_record(self.db, record_id)
        workflow = self.client.get_workflow(rec.n8n_workflow_id)
        _snapshot, changed = service.refresh_draft_snapshot(self.db, rec, workflow)
        if changed:
            self.db.commit()

        issues: list[dict] = []
        nodes = workflow.get("nodes") or []
        connections = workflow.get("connections") or {}

        summary = service.summarize_workflow(workflow)
        try:
            _validate_connections([{"name": n.get("name")} for n in nodes], connections)
        except StewardError as e:
            issues.append({"level": "error", "message": str(e)})
        managed_contract = None
        try:
            managed_contract = service.validate_managed_workflow_contract(workflow)
        except StewardError as e:
            issues.append({"level": "error", "message": str(e)})

        for n in nodes:
            if n.get("credentials"):
                cred_names = ", ".join(str((v or {}).get("name") or k) for k, v in n["credentials"].items())
                issues.append({"level": "info",
                               "message": f"节点「{n.get('name')}」引用凭据 [{cred_names}]——请确认已在 n8n 界面配置好，API 无法代建凭据。"})

        ok = not any(i["level"] == "error" for i in issues)
        self._touch(rec.id)
        return {"ok": ok, "issues": issues, "summary": summary,
                "managedContract": managed_contract}

    # ── 受控执行预览（真实触发，不发布、不永久启停、不写资产湖） ──────────

    def tool_execute_pipeline(self, record_id: str, payload: dict | None = None,
                              sample_limit: int | None = None,
                              columns: list[str] | None = None) -> dict:
        from app.data_channel.steward.runner import collect_n8n_rows, collect_test_rows

        if payload is not None and not isinstance(payload, dict):
            raise StewardError("payload 必须是 JSON 对象。")
        rec = service.require_record(self.db, record_id)
        pl = service.shadow_pipeline(self.db, rec)
        if pl is not None and (pl.status or "") == "published":
            rows, exec_meta = collect_n8n_rows(self.db, pl, payload=payload)
            lifecycle = "published"
        else:
            rows, exec_meta = collect_test_rows(
                self.db, rec, payload=payload, require_publish_evidence=False)
            lifecycle = "draft"

        preview = _execution_table_preview(rows, sample_limit, columns)
        preview["title"] = "本次执行输出"
        preview["node"] = exec_meta.get("last_node")
        preview["executionId"] = str(exec_meta.get("execution_id") or "")
        self._touch(rec.id)
        return {
            "pipeline": rec.name,
            "pipelineStatus": lifecycle,
            "rows": len(rows),
            "execution": exec_meta,
            "preview": preview,
            "note": "本次执行仅用于查看输出，未写入数据资产湖，流水线启停状态已保持或恢复。",
        }

    # ── 只读诊断（读取已有执行，无副作用） ────────────────────────────

    def tool_inspect_runs(self, record_id: str, limit: int | None = None,
                          sample_limit: int | None = None,
                          columns: list[str] | None = None) -> dict:
        rec = service.require_record(self.db, record_id)
        execs = self.client.list_executions(workflow_id=rec.n8n_workflow_id, limit=limit or 5)
        out: dict[str, Any] = {"pipeline": rec.name,
                               "executions": [_execution_brief(e) for e in execs]}
        if not execs:
            out["note"] = ("这条流水线还没有执行记录。若用户希望立即运行，"
                           "请使用 execute_pipeline 触发一次受控执行预览。")
            return out
        # 展开最近一次的细节：报错 / 各节点行数 / 末节点样例
        latest = self.client.get_execution(str(execs[0].get("id")), include_data=True)
        result_data = (latest.get("data") or {}).get("resultData") or {}
        detail: dict[str, Any] = {"lastNode": result_data.get("lastNodeExecuted")}
        err = result_data.get("error") or {}
        if err:
            node = err.get("node")
            detail["error"] = {
                "message": str(err.get("message", ""))[:400],
                "node": node.get("name") if isinstance(node, dict) else node,
                "description": str(err.get("description", ""))[:400],
            }
        run_data = result_data.get("runData") or {}
        counts: dict[str, Any] = {}
        for node_name, runs in run_data.items():
            try:
                items = ((runs[-1].get("data") or {}).get("main") or [[]])[0] or []
                counts[node_name] = len(items)
            except Exception:  # noqa: BLE001
                counts[node_name] = None
        detail["nodeItemCounts"] = counts
        last = detail.get("lastNode")
        if last and last in run_data:
            items = None
            try:
                items = ((run_data[last][-1].get("data") or {}).get("main") or [[]])[0] or []
            except Exception:  # noqa: BLE001
                logger.exception("failed to read last-node execution output")
            if items is not None:
                preview = _execution_table_preview(items, sample_limit, columns)
                preview["node"] = last
                preview["executionId"] = str(execs[0].get("id") or "")
                detail["lastNodeSample"] = preview["rows"]
                out["preview"] = preview
        out["latest"] = detail
        return out

    def tool_check_credentials(self, record_id: str) -> dict:
        rec = service.require_record(self.db, record_id)
        workflow = self.client.get_workflow(rec.n8n_workflow_id)
        _snapshot, changed = service.refresh_draft_snapshot(self.db, rec, workflow)
        if changed:
            self.db.commit()

        referenced = []
        for node in workflow.get("nodes") or []:
            for cred_type, ref in (node.get("credentials") or {}).items():
                ref = ref or {}
                referenced.append({"node": node.get("name"), "type": cred_type,
                                   "name": ref.get("name"), "id": ref.get("id")})

        out: dict[str, Any] = {"pipeline": rec.name, "referenced": referenced}
        try:
            available = [{"id": c.get("id"), "name": c.get("name"), "type": c.get("type")}
                         for c in self.client.list_credentials(limit=200)]
        except Exception as e:  # noqa: BLE001 — 部分 n8n 版本不支持列凭据，降级
            out["availableError"] = str(e)[:200]
            out["note"] = ("无法列出实例凭据（可能该 n8n 版本不支持）。"
                           "请让用户确认这些被引用的凭据已在 n8n 界面配置好。")
            return out

        avail_ids = {c["id"] for c in available if c.get("id")}
        avail_pairs = {(c["type"], c["name"]) for c in available}
        missing = [r for r in referenced
                   if not (r.get("id") in avail_ids or (r["type"], r.get("name")) in avail_pairs)]
        out["available"] = available
        out["missing"] = missing
        if not referenced:
            out["note"] = "这条流水线没有任何节点引用凭据。"
        elif missing:
            out["note"] = "有被引用的凭据在实例上找不到，请让用户去 n8n 界面配好（API 不能代建凭据）。"
        else:
            out["note"] = "所有被引用的凭据在实例上都已配置。"
        return out
