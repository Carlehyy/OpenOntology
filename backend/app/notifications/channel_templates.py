"""消息通知 — 渠道模板注册表（去技术化核心）

每个模板声明：用户需要填写的字段（label/hint/是否敏感）与
"字段 → apprise URL" 的拼装函数。界面只呈现字段与人话指引，
永不暴露 URL 语法；投递层仍统一走 apprise URL（加密落库不变）。

URL 语法均已在 apprise 2.0.1 实测 add() 通过。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit, quote


@dataclass(frozen=True)
class ChannelField:
    key: str
    label: str
    hint: str = ""
    required: bool = True
    secret: bool = False  # 敏感：回显时脱敏
    placeholder: str = ""


@dataclass(frozen=True)
class ChannelTemplate:
    id: str
    name: str
    description: str
    fields: list[ChannelField] = field(default_factory=list)
    build: Callable[[dict[str, str]], str] | None = None  # custom 无 build


def _dingtalk(params: dict[str, str]) -> str:
    token = params["token"].strip()
    secret = (params.get("secret") or "").strip()
    return f"dingtalk://{secret}@{token}/" if secret else f"dingtalk://{token}/"


def _feishu(params: dict[str, str]) -> str:
    return f"feishu://{params['token'].strip()}/"


def _wecom(params: dict[str, str]) -> str:
    return f"wecombot://{params['key'].strip()}"


def _tgram(params: dict[str, str]) -> str:
    return f"tgram://{params['botToken'].strip()}/{params['chatId'].strip()}"


def _webhook(params: dict[str, str]) -> str:
    """用户粘贴完整 http(s) URL → 转换为 apprise json(s):// 语法。"""
    raw = params["url"].strip()
    parts = urlsplit(raw)
    scheme = "jsons" if parts.scheme == "https" else "json"
    netloc = parts.netloc
    path = parts.path or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"{scheme}://{netloc}{path}{query}"


def _email(params: dict[str, str], smtp: dict[str, Any]) -> str:
    """收件人 + 平台 SMTP 设置 → mailtos://（凭据来自 SMTP 配置，用户零感知）。"""
    recipients = ",".join(
        addr.strip() for addr in re.split(r"[,;\s]+", params["recipients"]) if addr.strip()
    )
    user = quote(str(smtp["username"] or ""), safe="")
    password = quote(str(smtp["password"] or ""), safe="")
    host = smtp["host"]
    port = int(smtp.get("port") or 465)
    sender = quote(str(smtp.get("sender") or smtp["username"] or ""), safe="")
    return (
        f"mailtos://{user}:{password}@{host}:{port}/"
        f"?to={quote(recipients, safe=',@.')}&from={sender}"
    )


DINGTALK = ChannelTemplate(
    id="dingtalk",
    name="钉钉机器人",
    description="向钉钉群推送 Markdown 消息",
    fields=[
        ChannelField(
            key="token", label="access_token", secret=True,
            hint="钉钉群 → 设置 → 机器人 → 添加「自定义」→ 复制 Webhook，取 URL 中 access_token= 后面的串",
            placeholder="如 6fb1fa7c2b...",
        ),
        ChannelField(
            key="secret", label="加签密钥（可选）", required=False, secret=True,
            hint="安全设置选「加签」时复制 SEC 开头的密钥；未开启加签请留空",
            placeholder="SEC...",
        ),
    ],
    build=_dingtalk,
)

FEISHU = ChannelTemplate(
    id="feishu",
    name="飞书机器人",
    description="向飞书群推送消息",
    fields=[
        ChannelField(
            key="token", label="Webhook 地址", secret=True,
            hint="飞书群 → 设置 → 群机器人 → 添加「自定义机器人」→ 复制 Webhook 地址（整段粘贴，自动解析 token）",
            placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/…",
        ),
    ],
    build=lambda p: _feishu({"token": _feishu_token(p["token"])}),
)

WECOM = ChannelTemplate(
    id="wecom",
    name="企业微信机器人",
    description="向企业微信群推送 Markdown 消息",
    fields=[
        ChannelField(
            key="key", label="机器人 Webhook 地址", secret=True,
            hint="企业微信群 → 右键 → 添加群机器人 → 复制 Webhook 地址（整段粘贴，自动解析 key）",
            placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…",
        ),
    ],
    build=lambda p: _wecom({"key": _wecom_key(p["key"])}),
)

TELEGRAM = ChannelTemplate(
    id="tgram",
    name="Telegram",
    description="向 Telegram 用户/群推送消息",
    fields=[
        ChannelField(
            key="botToken", label="Bot Token", secret=True,
            hint="在 Telegram 中找 @BotFather → /newbot → 复制 Bot Token",
            placeholder="如 123456789:AAH...",
        ),
        ChannelField(
            key="chatId", label="Chat ID",
            hint="接收消息的会话 ID：与 @userinfobot 对话获取你的数字 ID；群组为负数 ID",
            placeholder="如 987654321",
        ),
    ],
    build=_tgram,
)

EMAIL = ChannelTemplate(
    id="email",
    name="邮件",
    description="通过平台发件邮箱发送邮件（无需任何凭据，填收件人即可）",
    fields=[
        ChannelField(
            key="recipients", label="收件邮箱", hint="多个邮箱用逗号分隔",
            placeholder="如 ops@example.com, dev@example.com",
        ),
    ],
    build=lambda p: p.get("__url__", ""),  # 邮件需 SMTP 设置，由 service 侧特化构建
)

WEBHOOK = ChannelTemplate(
    id="webhook",
    name="通用 Webhook",
    description="向自建系统/内网接收端 POST JSON 消息",
    fields=[
        ChannelField(
            key="url", label="接收地址", hint="完整 http(s) URL，平台将以 POST JSON 推送",
            placeholder="https://your-host/hook",
        ),
    ],
    build=_webhook,
)

CUSTOM = ChannelTemplate(
    id="custom",
    name="自定义 apprise（高级）",
    description="直接填写 apprise URL 语法，适配 apprise 支持的任意渠道",
    fields=[
        ChannelField(
            key="url", label="apprise URL", secret=True,
            hint="完整 apprise URL（如 mailto://、discord://、msteams:// 等），参考 apprise 官方文档",
            placeholder="json://host/path",
        ),
    ],
    build=None,
)

TEMPLATES: dict[str, ChannelTemplate] = {
    t.id: t for t in (DINGTALK, FEISHU, WECOM, TELEGRAM, EMAIL, WEBHOOK, CUSTOM)
}


def _feishu_token(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("http"):
        match = re.search(r"/([0-9a-fA-F-]{8,})/?$", raw)
        if match:
            return match.group(1)
    return raw


def _wecom_key(raw: str) -> str:
    raw = raw.strip()
    match = re.search(r"[?&]key=([^&\s]+)", raw)
    return match.group(1) if match else raw


def mask_field(value: str) -> str:
    """敏感字段脱敏回显：保留前 4 后 4，过短全遮。"""
    value = (value or "").strip()
    if len(value) <= 8:
        return "…"
    return f"{value[:4]}…{value[-4:]}"


def template_display(template_id: str | None, params: dict[str, str]) -> str:
    """渠道卡片副行：按模板回显关键字段（已脱敏）。"""
    template = TEMPLATES.get(template_id or "")
    if not template:
        return ""
    parts = []
    for f in template.fields:
        value = (params.get(f.key) or "").strip()
        if not value:
            continue
        shown = mask_field(value) if f.secret else (value if len(value) <= 48 else f"{value[:45]}…")
        parts.append(shown)
    return " · ".join(parts)
