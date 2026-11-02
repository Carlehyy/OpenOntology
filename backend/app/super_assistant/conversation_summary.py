"""会话纪要（每会话一份结构化纪要 + 用户级生成模型配置；独立子路由）。

输入：会话至今全部 complete 的用户消息与助手最终回复（工具调用过程住在
steps/ToolRun，不进入纪要），按【用户消息 时间】/【超级助手 时间】拼成
Markdown 交给 LLM 压缩为五维微卡片；输出按模板解析为结构化字段持久化，
每会话仅保留最新一份（markdown 原文同时保留作展示兜底与审计）。

生成模型默认取会话当前使用的 model_config_id；用户可在前端纪要弹窗配置
全局覆盖（super_assistant_summary_settings，每用户一行，对所有会话生效），
两种来源都经 select_llm_model_config 解析（不可用时按平台规则回退）。

触发时机由前端决定（发送后约 1s / 完全响应结束后约 1s 调 generate 端点），
本模块不感知触发；LLM 调用走 llm_gateway.chat（同步、写 model_call_logs，
与 assistant_evaluation 同 seam）。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth.models import User
from app.deps import get_current_user, get_db
from app.model_configs.llm_gateway import chat as llm_chat
from app.model_configs.models import ModelConfig
from app.model_configs.selector import llm_call_kwargs, select_llm_model_config
from app.super_assistant.models import (
    SuperAssistantConversation,
    SuperAssistantConversationSummary,
    SuperAssistantMessage,
    SuperAssistantSummarySetting,
)
from app.super_assistant.schemas import (
    ConversationSummaryOut,
    SummarySettingsOut,
    SummarySettingsUpdate,
)

router = APIRouter()

# 超长会话防御：纪要输入从最新内容截断（保留尾部），避免超出模型上下文
_MAX_TRANSCRIPT_CHARS = 80_000

# 纪要提示词：指令 + 输出模板作为 system，会话正文以 user 消息注入
_SUMMARY_SYSTEM_PROMPT = """# Role
你是一个极简主义的技术摘要提取器。请将输入的 Markdown 过程记录压缩为一张“3秒可读完”的五维微卡片。

# Strict Constraints (极简硬约束)
1. **总字数必须控制在 250 字以内**！
2. **禁止啰嗦**：剔除具体的长串 UUID/ID、具体人名清单、重复的工具调用次数。只保留核心业务名词和关键错误码。
3. **单行法则**：5 个维度中，每个维度严禁展开多级子列表，必须浓缩在 1~2 行以内（多个点用 ① ② ③ 同行串联）。
4. **内部先理顺，输出只给干货**：忽略试错草稿，直接按以下模板输出。

# Output Template
### 📋 [15字以内概括主题]
`状态：[已完成 / 进行中 / 阻塞中]` ｜ `标签：#标签1 #标签2`

- **🎯 核心目标**：[一句话说明做这件事的目的，不超过35字]
- **⚡ 关键动作**：[用 ① ② ③ 串联核心步骤，每步不超过15字]
- **🚧 踩坑决策**：[提取最关键的 1-2 个报错/阻碍 → 以及采取了什么应对决策，不超过60字]
- **🏆 最终产出**：[一句话总结达成的结论或产物，不超过45字]
- **📌 后续待办**：[用 ① ② 列出遗留问题，无则填“无”，不超过35字]

---"""

# 五维卡片字段：LLM 输出模板中的中文维度名 → structured 存储键
_BULLET_FIELDS = {
    "核心目标": "goal",
    "关键动作": "actions",
    "踩坑决策": "pitfalls",
    "最终产出": "outcome",
    "后续待办": "todos",
}
_TITLE_RE = re.compile(r"^#{2,4}\s*📋?\s*[\[【「]?\s*(.+?)\s*[\]」】]?\s*$")
_STATUS_RE = re.compile(r"状态[：:]\s*[`'\"]?([^`'\"｜|\n]+?)\s*[`'\"]?\s*(?:[｜|]|$)")
_TAGS_RE = re.compile(r"标签[：:]\s*[`'\"]?([^`'\"\n]+?)\s*[`'\"]?\s*(?:[｜|]|$)")
_BULLET_RE = re.compile(
    r"^\s*[-*]\s*\*\*[^*]*?(核心目标|关键动作|踩坑决策|最终产出|后续待办)[^*]*\*\*\s*[：:]\s*(.*)$"
)


def _owned_conversation(
    db: Session,
    owner_id: str,
    conversation_id: str,
) -> SuperAssistantConversation:
    item = db.query(SuperAssistantConversation).filter(
        SuperAssistantConversation.id == conversation_id,
        SuperAssistantConversation.owner_id == owner_id,
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="会话不存在")
    return item


def _fmt_minute(value: datetime | None) -> str:
    if value is None:
        return "时间未知"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


def build_transcript(db: Session, conversation: SuperAssistantConversation) -> tuple[str, int]:
    """把会话内 complete 的用户消息与助手最终回复拼成纪要输入 MD。

    只取消息 content（工具调用过程在 steps，不进入纪要）；助手消息的
    时间用 completed_at（回复落定时刻），无则回退 created_at。
    """
    rows = db.query(SuperAssistantMessage).filter(
        SuperAssistantMessage.conversation_id == conversation.id,
        SuperAssistantMessage.role.in_(("user", "assistant")),
        SuperAssistantMessage.status == "complete",
    ).order_by(
        SuperAssistantMessage.created_at.asc(),
        SuperAssistantMessage.id.asc(),
    ).all()
    lines: list[str] = []
    included = 0
    for row in rows:
        content = (row.content or "").strip()
        if not content:
            continue
        included += 1
        label = "用户消息" if row.role == "user" else "超级助手"
        timestamp = row.completed_at if row.role == "assistant" else row.created_at
        lines.append(f"【{label} {_fmt_minute(timestamp)}】{content}")
    transcript = "\n".join(lines)
    if len(transcript) > _MAX_TRANSCRIPT_CHARS:
        transcript = "（较早的对话内容已省略）\n" + transcript[-_MAX_TRANSCRIPT_CHARS:]
    return transcript, included


def parse_summary_card(raw: str) -> dict:
    """把 LLM 输出的五维微卡片解析为结构化字段（宽容解析，不全则留空）。"""
    structured: dict = {
        "title": "",
        "status": "",
        "tags": [],
        "goal": "",
        "actions": "",
        "pitfalls": "",
        "outcome": "",
        "todos": "",
    }
    lines = (raw or "").splitlines()
    for line in lines[:8]:
        if not structured["title"]:
            matched = _TITLE_RE.match(line.strip())
            if matched and matched.group(1).strip():
                structured["title"] = matched.group(1).strip()
        if not structured["status"]:
            matched = _STATUS_RE.search(line)
            if matched:
                structured["status"] = matched.group(1).strip()
        if not structured["tags"]:
            matched = _TAGS_RE.search(line)
            if matched:
                structured["tags"] = [
                    tag for tag in (
                        piece.strip().lstrip("#") for piece in matched.group(1).split("#")
                    ) if tag
                ]
    current_field: str | None = None
    buffer: list[str] = []
    for line in lines:
        stripped = line.strip()
        matched = _BULLET_RE.match(line)
        if matched:
            if current_field:
                structured[current_field] = " ".join(piece for piece in buffer if piece)
            current_field = _BULLET_FIELDS[matched.group(1)]
            buffer = [matched.group(2).strip()]
            continue
        if current_field is None:
            continue
        # 值的续行（模板允许 1~2 行）：空行、下一个要点/标题/反引号行即收口
        if not stripped or stripped.startswith(("#", "- ", "* ", "`")):
            structured[current_field] = " ".join(piece for piece in buffer if piece)
            current_field = None
            buffer = []
        else:
            buffer.append(stripped)
    if current_field:
        structured[current_field] = " ".join(piece for piece in buffer if piece)
    return structured


def _resolve_model_config(
    db: Session,
    owner_id: str,
    conversation: SuperAssistantConversation,
) -> ModelConfig | None:
    setting = db.get(SuperAssistantSummarySetting, owner_id)
    requested = (
        setting.model_config_id
        if setting and setting.model_config_id
        else conversation.model_config_id
    )
    return select_llm_model_config(
        db=db,
        model_id=requested,
        purpose_tags=("super_assistant",),
        allow_vlm=False,
    )


def get_summary(
    db: Session,
    owner_id: str,
    conversation_id: str,
) -> SuperAssistantConversationSummary | None:
    _owned_conversation(db, owner_id, conversation_id)
    return db.get(SuperAssistantConversationSummary, conversation_id)


def generate_summary(
    db: Session,
    owner_id: str,
    conversation_id: str,
) -> SuperAssistantConversationSummary:
    conversation = _owned_conversation(db, owner_id, conversation_id)
    transcript, included = build_transcript(db, conversation)
    if included == 0:
        raise HTTPException(status_code=400, detail="暂无可总结的对话内容")
    model_config = _resolve_model_config(db, owner_id, conversation)
    if model_config is None:
        raise HTTPException(status_code=400, detail="没有可用的模型配置")
    call_kwargs = llm_call_kwargs(model_config)
    if not call_kwargs:
        raise HTTPException(status_code=400, detail="所选模型配置缺少可用的模型名")
    messages = [
        {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": f'# Input Text\n"""\n{transcript}\n"""'},
    ]
    try:
        result = llm_chat(call_kwargs, messages, [])
    except Exception as exc:  # noqa: BLE001 — LLM 网关异常统一转 502 交给前端静默处理
        raise HTTPException(status_code=502, detail=f"会话纪要生成失败：{exc}") from exc
    raw = str((result or {}).get("content") or "").strip()
    if not raw:
        raise HTTPException(status_code=502, detail="会话纪要生成失败：模型返回空内容")
    summary = db.get(SuperAssistantConversationSummary, conversation_id)
    if summary is None:
        summary = SuperAssistantConversationSummary(conversation_id=conversation_id)
        db.add(summary)
    summary.structured = parse_summary_card(raw)
    summary.markdown = raw
    summary.model_config_id = model_config.id
    summary.message_count = included
    summary.generated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(summary)
    return summary


def get_summary_settings(db: Session, owner_id: str) -> SuperAssistantSummarySetting | None:
    return db.get(SuperAssistantSummarySetting, owner_id)


def update_summary_settings(
    db: Session,
    owner_id: str,
    body: SummarySettingsUpdate,
) -> SuperAssistantSummarySetting:
    if body.model_config_id:
        model = db.query(ModelConfig).filter(
            ModelConfig.id == body.model_config_id,
            ModelConfig.config_type == "llm",
            ModelConfig.enabled.is_(True),
        ).first()
        if not model:
            raise HTTPException(status_code=400, detail="所选模型不存在或未启用")
    setting = db.get(SuperAssistantSummarySetting, owner_id)
    if setting is None:
        setting = SuperAssistantSummarySetting(
            owner_id=owner_id,
            model_config_id=body.model_config_id,
        )
        db.add(setting)
    else:
        setting.model_config_id = body.model_config_id
    db.commit()
    db.refresh(setting)
    return setting


@router.get("/conversations/{conversation_id}/summary", response_model=ConversationSummaryOut | None)
def read_conversation_summary(
    conversation_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuperAssistantConversationSummary | None:
    return get_summary(db, current_user.id, conversation_id)


@router.post(
    "/conversations/{conversation_id}/summary/generate",
    response_model=ConversationSummaryOut,
)
def create_conversation_summary(
    conversation_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuperAssistantConversationSummary:
    return generate_summary(db, current_user.id, conversation_id)


@router.get("/summary/settings", response_model=SummarySettingsOut)
def read_summary_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SummarySettingsOut:
    # 未配置（无行）返回全空字段的默认对象而非裸 null：共享 axios 拦截器
    # 对 null 响应体做空安全处理前的既有契约是对象响应（widget-config 同例）
    setting = get_summary_settings(db, current_user.id)
    if setting is None:
        return SummarySettingsOut(model_config_id=None, updated_at=None)
    return setting


@router.put("/summary/settings", response_model=SummarySettingsOut)
def update_summary_settings_handler(
    body: SummarySettingsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuperAssistantSummarySetting:
    return update_summary_settings(db, current_user.id, body)
