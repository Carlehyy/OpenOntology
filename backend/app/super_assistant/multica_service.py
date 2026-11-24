"""multica 外部集成：配置、命令目录、/multica: 斜杠解析与工具执行。

设计约定（与需求对齐）：
- 每用户多套 custom profile + 部署派生的只读 platform_default。
  配置变化追加不可变 revision；切换、编辑、软删除均保留旧任务的来源。
- 现有 super_assistant_multica_configs 与 HTTP 接口保留为生效投影。
  新旧调用方统一解析 profile；读路径不提交调用方事务。
- 未配置/未启用时工具不进目录，斜杠命令得到确定性引导回复，绝不静默
  失败。查询工具（list_agents/list_tasks）在命令无参数尾串时由 runtime
  直接执行（不经过 LLM 决策）；create_task 及带尾串的查询走强制 LLM 轮，
  参数从尾串提取，写操作由 runtime 审批门确认。
- PAT 经 shared encryption 加密存储、永不回显；服务地址复用 MCP 同一
  SSRF 校验。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.shared.encryption import decrypt, encrypt
from app.super_assistant import multica_client, multica_sso
from app.super_assistant.models import (
    SuperAssistantMulticaConfig,
    SuperAssistantMulticaProfile,
    SuperAssistantMulticaProfileRevision,
)
from app.super_assistant.schemas import (
    MulticaCommandOut,
    MulticaConfigOut,
    MulticaConfigUpdate,
    MulticaProfileCreate,
    MulticaProfileOut,
    MulticaProfileUpdate,
    MulticaProfilesOut,
    MulticaTestOut,
    MulticaWorkspaceOut,
    MulticaWorkspacesOut,
)


class MulticaServiceError(Exception):
    """配置层错误（由路由层翻译为 HTTP 状态码）。"""


class MulticaProfileNotFoundError(MulticaServiceError):
    """profile/revision 不存在或不属于当前用户（HTTP 404）。"""


class MulticaProfileConflictError(MulticaServiceError):
    """profile 状态冲突：平台默认只读、激活配置不完整等（HTTP 409）。"""


# 命令目录：前端提示与后端解析的单一事实源
MULTICA_COMMANDS: list[dict[str, Any]] = [
    {
        "command": "list_agents",
        "aliases": ["agents"],
        "tool": "multica_list_agents",
        "title": "查看智能体",
        "description": "列出 multica 工作台的全部智能体及其运行时绑定状态。",
        "usage": "/multica:list_agents",
        "write": False,
    },
    {
        "command": "list_tasks",
        "aliases": ["tasks"],
        "tool": "multica_list_tasks",
        "title": "查看任务清单",
        "description": "查看当前工作台的任务清单，可按状态或负责人过滤。",
        "usage": "/multica:list_tasks [过滤条件]",
        "write": False,
    },
    {
        "command": "create_task",
        "aliases": ["create"],
        "tool": "multica_create_task",
        "title": "下发任务",
        "description": "在 multica 创建任务并指派给指定智能体（写操作，需用户确认）。",
        "usage": "/multica:create_task 任务描述…",
        "write": True,
    },
]

_COMMAND_INDEX: dict[str, dict[str, Any]] = {
    str(alias).lower(): item
    for item in MULTICA_COMMANDS
    for alias in (item["command"], *item["aliases"])
}

_MULTICA_TOOL_NAMES = frozenset(str(item["tool"]) for item in MULTICA_COMMANDS)
_READ_TOOLS = frozenset(
    str(item["tool"]) for item in MULTICA_COMMANDS if not item["write"]
)

_SLASH_PATTERN = re.compile(r"^/multica[:：]\s*(.*)$", re.IGNORECASE | re.DOTALL)
_UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_MAX_LIST_LIMIT = 50
_DEFAULT_LIST_LIMIT = 10

# Multica's issue and task statuses are intentionally normalized here rather
# than leaking provider-specific values into kernel.v1.
_MULTICA_SUCCESS_STATES = frozenset({"completed", "complete", "done", "closed", "resolved", "success", "succeeded", "finished"})
_MULTICA_FAILURE_STATES = frozenset({"failed", "failure", "error", "errored", "blocked"})
_MULTICA_CANCELLED_STATES = frozenset({"cancelled", "canceled", "aborted", "stopped"})


@dataclass(frozen=True)
class MulticaSlashCommand:
    """一次 /multica: 输入的解析结果。

    state：
    - ok          命中已知命令（command/tool_name 有效，tail 为自然语言尾串）
    - unknown     前缀匹配但命令名未命中（raw_command 保留用于提示）
    - unconfigured 尚未配置 multica（命令一律不可用）
    """

    state: str
    command: dict[str, Any] | None = None
    tool_name: str = ""
    raw_command: str = ""
    tail: str = ""


def parse_slash_command(message: str, *, configured: bool) -> MulticaSlashCommand | None:
    """识别 ``/multica:命令 尾串``（兼容全角冒号）；非该前缀返回 None。"""
    match = _SLASH_PATTERN.match((message or "").strip())
    if match is None:
        return None
    if not configured:
        return MulticaSlashCommand(state="unconfigured")
    body = match.group(1).strip()
    parts = body.split(None, 1)
    raw_command = (parts[0] if parts else "").strip().lower()
    tail = parts[1].strip() if len(parts) > 1 else ""
    if not raw_command:
        return MulticaSlashCommand(state="unknown")
    item = _COMMAND_INDEX.get(raw_command)
    if item is None:
        return MulticaSlashCommand(state="unknown", raw_command=raw_command)
    return MulticaSlashCommand(
        state="ok", command=item, tool_name=str(item["tool"]), tail=tail,
    )


def guidance_text(slash: MulticaSlashCommand) -> str:
    """未配置/未知命令的确定性引导文案（不经过 LLM）。"""
    if slash.state == "unconfigured":
        return (
            "multica 集成尚未配置，/multica: 命令当前不可用。"
            "请在左侧「外部集成」中填写 multica 服务地址与 API Token 并启用后重试。"
        )
    lines = [f"未知的 multica 命令“{slash.raw_command}”。可用命令："]
    lines.extend(
        f"- {item['usage']}：{item['title']}。{item['description']}"
        for item in MULTICA_COMMANDS
    )
    return "\n".join(lines)


def forced_directive_text(slash: MulticaSlashCommand) -> str:
    """强制 LLM 轮的系统指令：只允许调用命令指定的工具，参数从尾串提取。"""
    usage = str((slash.command or {}).get("usage") or "")
    return (
        f"\n\n[平台指令] 用户通过强制命令 {usage.split(' ', 1)[0]} 指定调用工具 "
        f"{slash.tool_name}：请立即调用该工具完成上述请求，参数从命令后的描述中提取"
        "（创建任务时 title 必填、assignee 用智能体名称精确匹配）；本轮不要调用其它工具，"
        "也不要重复确认（平台会向用户展示审批）。"
    )


# ---------------------------------------------------------------------------
# Named profiles, immutable source revisions and the existing config projection
# ---------------------------------------------------------------------------

PROFILE_KIND_CUSTOM = "custom"
PROFILE_KIND_PLATFORM_DEFAULT = "platform_default"
PLATFORM_DEFAULT_BUILTIN_KEY = "default"
LEGACY_PROFILE_NAME = "我的 multica"


def _lock_owner(db: Session, owner_id: str) -> None:
    from app.auth.models import User

    # Serialize profile writers without blocking foreign-key references to the user.
    if db.scalar(select(User.id).where(User.id == owner_id).with_for_update(key_share=True)) is None:
        raise MulticaProfileNotFoundError("multica 配置所属用户不存在")


def _platform_default_settings() -> dict[str, str]:
    from app.shared.config import settings

    return {
        "base_url": settings.multica_default_base_url.strip(),
        "web_url": settings.multica_default_web_url.strip(),
        "workspace_id": settings.multica_default_workspace_id.strip(),
        "workspace_name": settings.multica_default_workspace_name.strip(),
        "token": settings.multica_default_pat.strip(),
    }


def _get_profile(db: Session, owner_id: str, profile_id: str) -> SuperAssistantMulticaProfile | None:
    return db.scalar(select(SuperAssistantMulticaProfile).where(
        SuperAssistantMulticaProfile.id == profile_id,
        SuperAssistantMulticaProfile.owner_id == owner_id,
        SuperAssistantMulticaProfile.deleted_at.is_(None),
    ).execution_options(populate_existing=True))


def _require_profile(db: Session, owner_id: str, profile_id: str) -> SuperAssistantMulticaProfile:
    profile = _get_profile(db, owner_id, profile_id)
    if profile is None:
        raise MulticaProfileNotFoundError("multica profile 不存在")
    return profile


def _current_revision(db: Session, profile_id: str) -> SuperAssistantMulticaProfileRevision | None:
    return db.scalar(select(SuperAssistantMulticaProfileRevision).where(
        SuperAssistantMulticaProfileRevision.profile_id == profile_id,
        SuperAssistantMulticaProfileRevision.retired_at.is_(None),
    ).order_by(SuperAssistantMulticaProfileRevision.revision_no.desc()).execution_options(populate_existing=True))


def _active_custom_profile(db: Session, owner_id: str) -> SuperAssistantMulticaProfile | None:
    return db.scalar(select(SuperAssistantMulticaProfile).where(
        SuperAssistantMulticaProfile.owner_id == owner_id,
        SuperAssistantMulticaProfile.kind == PROFILE_KIND_CUSTOM,
        SuperAssistantMulticaProfile.deleted_at.is_(None),
        SuperAssistantMulticaProfile.is_active.is_(True),
    ).execution_options(populate_existing=True))


def _platform_default_profile(db: Session, owner_id: str) -> SuperAssistantMulticaProfile | None:
    return db.scalar(select(SuperAssistantMulticaProfile).where(
        SuperAssistantMulticaProfile.owner_id == owner_id,
        SuperAssistantMulticaProfile.kind == PROFILE_KIND_PLATFORM_DEFAULT,
    ).execution_options(populate_existing=True))


def _revision_complete(revision: SuperAssistantMulticaProfileRevision) -> bool:
    return bool(revision.base_url.strip() and revision.workspace_id.strip() and revision.token_encrypted)


def _revision_usable(revision: SuperAssistantMulticaProfileRevision) -> bool:
    return bool(revision.enabled and revision.revoked_at is None and _revision_complete(revision))


def _token_matches(stored_encrypted: str | None, plaintext: str | None) -> bool:
    if not stored_encrypted or not plaintext:
        return not stored_encrypted and not plaintext
    try:
        return decrypt(stored_encrypted) == plaintext
    except Exception:
        return False


def _workspace_source_conflicts(db: Session, owner_id: str, base_url: str, workspace_id: str) -> bool:
    # AutoLoop's durable issue keys use the workspace ID. Reusing it across
    # endpoints would merge distinct issue histories for the same owner.
    return db.scalar(select(SuperAssistantMulticaProfileRevision.id).join(
        SuperAssistantMulticaProfile,
        SuperAssistantMulticaProfile.id == SuperAssistantMulticaProfileRevision.profile_id,
    ).where(
        SuperAssistantMulticaProfile.owner_id == owner_id,
        SuperAssistantMulticaProfileRevision.workspace_id == workspace_id,
        SuperAssistantMulticaProfileRevision.base_url != base_url,
    ).limit(1)) is not None


def _check_workspace_source(db: Session, owner_id: str, base_url: str, workspace_id: str) -> None:
    if _workspace_source_conflicts(db, owner_id, base_url, workspace_id):
        raise MulticaProfileConflictError("此工作区 ID 已绑定其他 multica 服务地址；请使用具有独立 ID 的工作区")


def _append_revision(
    db: Session,
    profile: SuperAssistantMulticaProfile,
    *,
    base_url: str,
    workspace_id: str,
    workspace_name: str,
    token_encrypted: str | None,
    enabled: bool,
    previous: SuperAssistantMulticaProfileRevision | None = None,
) -> SuperAssistantMulticaProfileRevision:
    _check_workspace_source(db, profile.owner_id, base_url, workspace_id)
    now = datetime.now(timezone.utc)
    if previous is not None:
        previous.retired_at = previous.retired_at or now
        db.flush()
    revision = SuperAssistantMulticaProfileRevision(
        profile_id=profile.id,
        revision_no=previous.revision_no + 1 if previous is not None else 1,
        base_url=base_url,
        workspace_id=workspace_id,
        workspace_name=workspace_name,
        token_encrypted=token_encrypted,
        enabled=enabled,
        revoked_at=previous.revoked_at if previous is not None and previous.token_encrypted == token_encrypted else None,
        created_at=now,
    )
    db.add(revision)
    profile.updated_at = now
    db.flush()
    return revision


def _default_source(db: Session, owner_id: str) -> dict[str, str] | None:
    source = _platform_default_settings()
    if not all(source.values()):
        return None
    # Readiness and the HTTP client enforce the network policy. Configuration
    # synchronization only needs the persisted address, with no DNS under a lock.
    try:
        base_url = multica_client.clean_base_url(source["base_url"])
    except multica_client.MulticaClientError:
        return None
    if _workspace_source_conflicts(db, owner_id, base_url, source["workspace_id"]):
        # A deployment default is independent of any healthy custom binding.
        # Keep the historical revision so pending work still has its source.
        return None
    return {**source, "base_url": base_url}


def _matches_default(revision: SuperAssistantMulticaProfileRevision | None,
                     source: dict[str, str]) -> bool:
    return bool(
        revision is not None and revision.enabled
        and revision.base_url == source["base_url"]
        and revision.workspace_id == source["workspace_id"]
        and revision.workspace_name == source["workspace_name"]
        and _token_matches(revision.token_encrypted, source["token"])
    )


def _sync_platform_default(db: Session, owner_id: str) -> SuperAssistantMulticaProfile | None:
    """Apply deployment settings while the owner write lock is held."""
    profile = _platform_default_profile(db, owner_id)
    source = _default_source(db, owner_id)
    if source is None:
        if profile is not None and profile.deleted_at is None:
            profile.is_active = False
            profile.deleted_at = datetime.now(timezone.utc)
            db.flush()
        return None
    if profile is None:
        profile = SuperAssistantMulticaProfile(
            owner_id=owner_id, name="平台默认 multica", kind=PROFILE_KIND_PLATFORM_DEFAULT,
            builtin_key=PLATFORM_DEFAULT_BUILTIN_KEY, is_active=False,
        )
        db.add(profile)
        db.flush()
    profile.deleted_at = None
    revision = _current_revision(db, profile.id)
    if not _matches_default(revision, source):
        token_encrypted = (
            revision.token_encrypted if revision is not None and _token_matches(revision.token_encrypted, source["token"])
            else encrypt(source["token"])
        )
        _append_revision(
            db, profile, base_url=source["base_url"], workspace_id=source["workspace_id"],
            workspace_name=source["workspace_name"], token_encrypted=token_encrypted,
            enabled=True, previous=revision,
        )
    return profile


def sync_deployment_default(db: Session, owner_id: str) -> bool:
    """Synchronize deployment drift. The caller ends the transaction before any I/O."""
    profile = _platform_default_profile(db, owner_id)
    source = _default_source(db, owner_id)
    if source is None:
        if profile is None or profile.deleted_at is not None:
            return False
    elif profile is not None and profile.deleted_at is None:
        revision = _current_revision(db, profile.id)
        if revision is not None and _matches_default(revision, source):
            if revision.revoked_at is None or not profile.is_active:
                return False
    _lock_owner(db, owner_id)
    _sync_projection(db, owner_id)
    return True


def _set_active_profile(db: Session, owner_id: str, profile: SuperAssistantMulticaProfile) -> None:
    for other in db.scalars(select(SuperAssistantMulticaProfile).where(
        SuperAssistantMulticaProfile.owner_id == owner_id,
        SuperAssistantMulticaProfile.is_active.is_(True),
    ).execution_options(populate_existing=True)):
        if other.id != profile.id:
            other.is_active = False
    db.flush()
    profile.is_active = True
    db.flush()


def _resolve_effective(
    db: Session, owner_id: str, platform: SuperAssistantMulticaProfile | None,
    projection: SuperAssistantMulticaConfig | None,
) -> tuple[SuperAssistantMulticaProfile | None, SuperAssistantMulticaProfileRevision | None]:
    custom = _active_custom_profile(db, owner_id)
    revision = _current_revision(db, custom.id) if custom is not None else None
    if revision is not None and _revision_usable(revision):
        return custom, revision
    if custom is not None:
        custom.is_active = False
        db.flush()
    if platform is not None:
        platform_revision = _current_revision(db, platform.id)
        if platform_revision is not None and _revision_usable(platform_revision):
            if not platform.is_active:
                _set_active_profile(db, owner_id, platform)
            return platform, platform_revision
        platform.is_active = False
    if custom is not None:
        return custom, revision
    # A disabled custom configuration remains editable through the existing API.
    if projection is not None and projection.source_kind == PROFILE_KIND_CUSTOM and projection.profile_id:
        saved = _get_profile(db, owner_id, projection.profile_id)
        if saved is not None:
            return saved, _current_revision(db, saved.id)
    return None, None


def _sync_projection(db: Session, owner_id: str) -> SuperAssistantMulticaConfig | None:
    """Update the legacy effective binding inside the owner write transaction."""
    db.flush()
    config = get_config(db, owner_id)
    platform = _sync_platform_default(db, owner_id)
    profile, revision = _resolve_effective(db, owner_id, platform, config)
    if profile is None or revision is None:
        if config is not None:
            config.enabled = False
            config.profile_id = config.profile_revision_id = config.source_kind = None
            config.base_url = config.workspace_id = config.workspace_name = ""
            config.token_encrypted = None
            config.last_test_status = config.last_test_message = config.last_tested_at = None
    else:
        if config is None:
            config = SuperAssistantMulticaConfig(owner_id=owner_id)
            db.add(config)
        for key in ("base_url", "workspace_id", "workspace_name", "token_encrypted",
                    "last_test_status", "last_test_message", "last_tested_at"):
            setattr(config, key, getattr(revision, key))
        config.enabled = bool(profile.is_active and _revision_usable(revision))
        config.profile_id, config.profile_revision_id, config.source_kind = profile.id, revision.id, profile.kind
    db.flush()
    return config


def get_config(db: Session, owner_id: str) -> SuperAssistantMulticaConfig | None:
    """Read the persisted effective binding without synchronizing deployment settings."""
    return db.get(SuperAssistantMulticaConfig, owner_id, populate_existing=True)


def read_config(db: Session, owner_id: str) -> MulticaConfigOut:
    sync_deployment_default(db, owner_id)
    result = config_view(get_config(db, owner_id))
    db.commit()
    return result


def active_config(db: Session, owner_id: str) -> SuperAssistantMulticaConfig | None:
    projection = SuperAssistantMulticaConfig
    revision = SuperAssistantMulticaProfileRevision
    profile = SuperAssistantMulticaProfile
    # Read the source and its authorization in one statement. The legacy row
    # alone cannot re-enable a revoked, retired or foreign revision.
    config = db.scalar(select(projection).join(revision, revision.id == projection.profile_revision_id)
        .join(profile, profile.id == revision.profile_id).where(
            projection.owner_id == owner_id, projection.enabled.is_(True),
            profile.owner_id == owner_id, profile.id == projection.profile_id,
            profile.is_active.is_(True), profile.deleted_at.is_(None),
            revision.enabled.is_(True), revision.revoked_at.is_(None), revision.retired_at.is_(None),
        ).execution_options(populate_existing=True))
    if config is not None and config.base_url.strip() and config.workspace_id.strip() and config.token_encrypted:
        return config
    return None


def decrypt_token(config: SuperAssistantMulticaConfig | SuperAssistantMulticaProfileRevision) -> str:
    try:
        token = decrypt(config.token_encrypted or "").strip()
    except Exception as exc:
        raise MulticaServiceError("multica API Token 无法解密，请重新保存配置") from exc
    if not token:
        raise MulticaServiceError("multica API Token 缺失，请重新保存配置")
    return token


def save_config(db: Session, owner_id: str, body: MulticaConfigUpdate) -> SuperAssistantMulticaConfig:
    """Keep the existing PUT contract while storing immutable custom revisions."""
    base_url = multica_client.normalize_base_url(body.base_url)
    _lock_owner(db, owner_id)
    config = get_config(db, owner_id)
    profile = _get_profile(db, owner_id, config.profile_id) if config is not None and config.profile_id else None
    if profile is not None and profile.kind != PROFILE_KIND_CUSTOM:
        profile = None
    revision = _current_revision(db, profile.id) if profile is not None else None
    token_encrypted = revision.token_encrypted if revision is not None else None
    if revision is not None and revision.base_url != base_url and token_encrypted and not body.token:
        raise MulticaServiceError("更换 multica 服务地址时请重新填写 API Token")
    if body.token and not _token_matches(token_encrypted, body.token):
        token_encrypted = encrypt(body.token)
    if body.enabled and not token_encrypted:
        raise MulticaServiceError("启用 multica 集成前请先填写 API Token")
    _check_workspace_source(db, owner_id, base_url, body.workspace_id)
    if profile is None:
        profile = SuperAssistantMulticaProfile(owner_id=owner_id, name=LEGACY_PROFILE_NAME,
            kind=PROFILE_KIND_CUSTOM, is_active=False)
        db.add(profile)
        db.flush()
    workspace_name = body.workspace_name or (revision.workspace_name if revision is not None else "")
    if revision is None or any((
        revision.base_url != base_url, revision.workspace_id != body.workspace_id,
        revision.workspace_name != workspace_name, revision.enabled != body.enabled,
        revision.token_encrypted != token_encrypted,
    )):
        revision = _append_revision(db, profile, base_url=base_url, workspace_id=body.workspace_id,
            workspace_name=workspace_name, token_encrypted=token_encrypted, enabled=body.enabled, previous=revision)
    _set_active_profile(db, owner_id, profile)
    config = _sync_projection(db, owner_id)
    assert config is not None
    db.commit()
    db.refresh(config)
    return config


def profile_view(profile: SuperAssistantMulticaProfile,
                 revision: SuperAssistantMulticaProfileRevision | None = None) -> MulticaProfileOut:
    return MulticaProfileOut(
        id=profile.id, name=profile.name, kind=profile.kind, builtin_key=profile.builtin_key,
        web_url=(_platform_default_settings()["web_url"] or None)
            if profile.kind == PROFILE_KIND_PLATFORM_DEFAULT else profile.web_url,
        sso_entry=(profile.kind == PROFILE_KIND_PLATFORM_DEFAULT
            and multica_sso.sso_entry_enabled()),
        is_active=profile.is_active, enabled=bool(revision is not None and _revision_usable(revision)),
        base_url=revision.base_url if revision is not None else "",
        workspace_id=revision.workspace_id if revision is not None else "",
        workspace_name=revision.workspace_name if revision is not None else "",
        token_set=bool(revision is not None and revision.token_encrypted),
        revision=revision.revision_no if revision is not None else 0,
        last_test_status=revision.last_test_status if revision is not None else None,
        last_test_message=revision.last_test_message if revision is not None else None,
        last_tested_at=revision.last_tested_at if revision is not None else None,
        created_at=profile.created_at, updated_at=profile.updated_at,
    )


def list_profiles(db: Session, owner_id: str) -> MulticaProfilesOut:
    sync_deployment_default(db, owner_id)
    config = get_config(db, owner_id)
    rows = list(db.scalars(select(SuperAssistantMulticaProfile).where(
        SuperAssistantMulticaProfile.owner_id == owner_id,
        SuperAssistantMulticaProfile.deleted_at.is_(None),
    ).order_by(SuperAssistantMulticaProfile.kind.desc(), SuperAssistantMulticaProfile.created_at,
               SuperAssistantMulticaProfile.id)))
    result = MulticaProfilesOut(
        profiles=[profile_view(item, _current_revision(db, item.id)) for item in rows],
        active_profile_id=next((item.id for item in rows if item.is_active), None),
        default_profile_id=next((item.id for item in rows if item.kind == PROFILE_KIND_PLATFORM_DEFAULT), None),
        effective=config_view(config),
    )
    db.commit()
    return result


def create_profile(db: Session, owner_id: str, body: MulticaProfileCreate) -> MulticaProfileOut:
    base_url = multica_client.normalize_base_url(body.base_url)
    web_url = multica_client.clean_web_url(body.web_url)
    if body.enabled and not body.token:
        raise MulticaServiceError("启用 multica 集成前请先填写 API Token")
    _lock_owner(db, owner_id)
    _check_workspace_source(db, owner_id, base_url, body.workspace_id)
    profile = SuperAssistantMulticaProfile(owner_id=owner_id, name=body.name,
        kind=PROFILE_KIND_CUSTOM, web_url=web_url, is_active=False)
    db.add(profile)
    db.flush()
    revision = _append_revision(db, profile, base_url=base_url, workspace_id=body.workspace_id,
        workspace_name=body.workspace_name or "", token_encrypted=encrypt(body.token) if body.token else None,
        enabled=body.enabled)
    _sync_projection(db, owner_id)
    result = profile_view(profile, revision)
    db.commit()
    return result


def update_profile(db: Session, owner_id: str, profile_id: str, body: MulticaProfileUpdate) -> MulticaProfileOut:
    # DNS validation belongs outside the owner write transaction.
    new_base_url = multica_client.normalize_base_url(body.base_url) if body.base_url is not None else None
    new_web_url = multica_client.clean_web_url(body.web_url) if body.web_url is not None else None
    _lock_owner(db, owner_id)
    profile = _require_profile(db, owner_id, profile_id)
    if profile.kind == PROFILE_KIND_PLATFORM_DEFAULT:
        raise MulticaProfileConflictError("平台默认 profile 由部署配置派生，不可修改")
    revision = _current_revision(db, profile.id)
    if revision is None:
        raise MulticaProfileConflictError("profile 缺少当前 revision")
    base_url = new_base_url if new_base_url is not None else revision.base_url
    workspace_id = body.workspace_id if body.workspace_id is not None else revision.workspace_id
    workspace_name = body.workspace_name if body.workspace_name is not None else revision.workspace_name
    enabled = body.enabled if body.enabled is not None else revision.enabled
    token_encrypted = revision.token_encrypted
    if revision.base_url != base_url and token_encrypted and not body.token:
        raise MulticaServiceError("更换 multica 服务地址时请重新填写 API Token")
    if body.token and not _token_matches(token_encrypted, body.token):
        token_encrypted = encrypt(body.token)
    if enabled and not token_encrypted:
        raise MulticaServiceError("启用 multica 集成前请先填写 API Token")
    _check_workspace_source(db, owner_id, base_url, workspace_id)
    if body.name is not None:
        profile.name = body.name
    if "web_url" in body.model_fields_set:
        profile.web_url = new_web_url
    if any((revision.base_url != base_url, revision.workspace_id != workspace_id,
            revision.workspace_name != workspace_name, revision.enabled != enabled,
            revision.token_encrypted != token_encrypted)):
        revision = _append_revision(db, profile, base_url=base_url, workspace_id=workspace_id,
            workspace_name=workspace_name, token_encrypted=token_encrypted, enabled=enabled, previous=revision)
    _sync_projection(db, owner_id)
    result = profile_view(profile, revision)
    db.commit()
    return result


def activate_profile(db: Session, owner_id: str, profile_id: str) -> MulticaProfileOut:
    sync_deployment_default(db, owner_id)
    profile = _require_profile(db, owner_id, profile_id)
    revision = _current_revision(db, profile.id)
    if revision is None or not _revision_usable(revision):
        raise MulticaProfileConflictError("激活前请启用并补全服务地址、工作区与 API Token")
    revision_id, base_url, workspace_id = revision.id, revision.base_url, revision.workspace_id
    try:
        token = decrypt_token(revision)
        db.commit()
        multica_client.fetch_me(base_url, token)
        workspaces = multica_client.list_workspaces(base_url, token)
    except (multica_client.MulticaClientError, MulticaServiceError) as exc:
        raise MulticaProfileConflictError("multica 连接验证失败，请测试连接后重试") from exc
    if not any(str(item.get("id") or "") == workspace_id for item in workspaces):
        raise MulticaProfileConflictError("API Token 无权访问所选工作区")

    # Activate only the credential and workspace that passed validation.
    _lock_owner(db, owner_id)
    _sync_platform_default(db, owner_id)
    db.flush()
    profile = _get_profile(db, owner_id, profile_id)
    revision = _current_revision(db, profile.id) if profile is not None else None
    if revision is None or revision.id != revision_id or not _revision_usable(revision):
        raise MulticaProfileConflictError("multica 配置在连接验证期间已变更，请重新激活")
    revision.last_test_status = "success"
    revision.last_test_message = "连接和工作区验证成功"
    revision.last_tested_at = datetime.now(timezone.utc)
    _set_active_profile(db, owner_id, profile)
    _sync_projection(db, owner_id)
    result = profile_view(profile, revision)
    db.commit()
    return result


def delete_profile(db: Session, owner_id: str, profile_id: str) -> None:
    _lock_owner(db, owner_id)
    profile = _require_profile(db, owner_id, profile_id)
    if profile.kind == PROFILE_KIND_PLATFORM_DEFAULT:
        raise MulticaProfileConflictError("平台默认 profile 由部署配置派生，不可删除")
    profile.is_active = False
    profile.deleted_at = datetime.now(timezone.utc)
    db.flush()
    _sync_projection(db, owner_id)
    db.commit()


def config_for_revision(db: Session, owner_id: str, revision_id: str) -> SuperAssistantMulticaProfileRevision:
    revision = db.scalar(select(SuperAssistantMulticaProfileRevision).join(
        SuperAssistantMulticaProfile,
        SuperAssistantMulticaProfile.id == SuperAssistantMulticaProfileRevision.profile_id,
    ).where(SuperAssistantMulticaProfileRevision.id == revision_id,
            SuperAssistantMulticaProfile.owner_id == owner_id).execution_options(populate_existing=True))
    if revision is None:
        raise MulticaProfileNotFoundError("multica profile revision 不存在")
    if revision.revoked_at is not None:
        raise MulticaProfileConflictError("该 revision 凭据已撤销，历史任务需要人工处理")
    if not _revision_usable(revision):
        raise MulticaProfileConflictError("multica profile revision 未启用或配置不完整")
    return revision


def config_for_source(db: Session, owner_id: str, *, base_url: str,
                      workspace_id: str) -> SuperAssistantMulticaProfileRevision:
    rows = list(db.scalars(select(SuperAssistantMulticaProfileRevision).join(
        SuperAssistantMulticaProfile,
        SuperAssistantMulticaProfile.id == SuperAssistantMulticaProfileRevision.profile_id,
    ).where(SuperAssistantMulticaProfile.owner_id == owner_id,
            SuperAssistantMulticaProfileRevision.base_url == base_url,
            SuperAssistantMulticaProfileRevision.workspace_id == workspace_id,
            SuperAssistantMulticaProfileRevision.enabled.is_(True),
    ).order_by(SuperAssistantMulticaProfileRevision.created_at.desc(),
               SuperAssistantMulticaProfileRevision.revision_no.desc(),
               SuperAssistantMulticaProfileRevision.id).execution_options(populate_existing=True)))
    rows = [item for item in rows if _revision_complete(item)]
    if not rows:
        raise MulticaProfileNotFoundError("multica 任务来源不存在或不可用")
    # A pair cannot tell a revoked binding from another credential for that
    # same endpoint and workspace. Only an explicit revision can do so.
    if any(item.revoked_at is not None for item in rows):
        raise MulticaProfileConflictError("multica 任务来源包含已撤销的凭据，需要人工处理")
    token = decrypt_token(rows[0])
    if any(not _token_matches(item.token_encrypted, token) for item in rows[1:]):
        raise MulticaProfileConflictError("multica 任务来源凭据不唯一，需要人工处理")
    return rows[0]


def _execute_connection_test(
    db: Session, owner_id: str, *, revision: SuperAssistantMulticaProfileRevision | None,
    base_url: str | None, token: str | None,
) -> MulticaTestOut:
    saved_base_url = revision.base_url if revision is not None else ""
    saved_token = revision.token_encrypted if revision is not None and revision.revoked_at is None else None
    profile_id = revision.profile_id if revision is not None else None
    revision_id = revision.id if revision is not None else None
    workspace_id = revision.workspace_id if revision is not None else ""
    effective_base = (base_url or saved_base_url).strip()
    effective_token = (token or "").strip()
    db.commit()
    if not effective_base:
        return MulticaTestOut(ok=False, message="请先填写服务地址与 API Token")
    effective_base = multica_client.normalize_base_url(effective_base)
    # Never send a saved token to a different draft endpoint.
    if not effective_token and saved_token and effective_base == saved_base_url:
        try:
            effective_token = decrypt(saved_token).strip()
        except Exception:
            pass
    if not effective_token:
        return MulticaTestOut(ok=False, message="请先填写服务地址与 API Token")
    saved_source = effective_base == saved_base_url and _token_matches(saved_token, effective_token)

    def record(ok: bool, message: str, workspaces: list[dict[str, Any]]) -> None:
        if not saved_source or profile_id is None:
            return
        _lock_owner(db, owner_id)
        profile = _get_profile(db, owner_id, profile_id)
        current = _current_revision(db, profile.id) if profile is not None else None
        if current is not None and current.id == revision_id and current.revoked_at is None:
            matched = next((str(item.get("name") or "") for item in workspaces
                            if str(item.get("id") or "") == workspace_id), "")
            if matched and matched != current.workspace_name and profile.kind == PROFILE_KIND_CUSTOM:
                current = _append_revision(db, profile, base_url=current.base_url,
                    workspace_id=workspace_id, workspace_name=matched,
                    token_encrypted=current.token_encrypted, enabled=current.enabled, previous=current)
            current.last_test_status = "success" if ok else "error"
            current.last_test_message = message[:500]
            current.last_tested_at = datetime.now(timezone.utc)
            _sync_projection(db, owner_id)
        db.commit()

    try:
        me = multica_client.fetch_me(effective_base, effective_token)
        workspaces = multica_client.list_workspaces(effective_base, effective_token)
    except multica_client.MulticaClientError as exc:
        record(False, str(exc), [])
        return MulticaTestOut(ok=False, message=str(exc))
    account_name = str(me.get("name") or me.get("email") or "")
    message = f"连接成功：{account_name}，可见 {len(workspaces)} 个工作区"
    if saved_source and not any(str(item.get("id") or "") == workspace_id for item in workspaces):
        message = "连接成功，但 API Token 无权访问已保存工作区"
        record(False, message, workspaces)
        return MulticaTestOut(ok=False, message=message, workspaces=workspaces)
    record(True, message, workspaces)
    return MulticaTestOut(ok=True, message=message, account_name=account_name or None,
        workspaces=[MulticaWorkspaceOut(id=str(item.get("id") or ""), name=str(item.get("name") or ""),
            slug=str(item.get("slug") or "")) for item in workspaces])


def test_profile(db: Session, owner_id: str, profile_id: str, *,
                 base_url: str | None = None, token: str | None = None) -> MulticaTestOut:
    sync_deployment_default(db, owner_id)
    profile = _require_profile(db, owner_id, profile_id)
    return _execute_connection_test(db, owner_id, revision=_current_revision(db, profile.id),
        base_url=base_url, token=token)


def _list_revision_workspaces(db: Session, revision: SuperAssistantMulticaProfileRevision | None) -> MulticaWorkspacesOut:
    if revision is None or revision.revoked_at is not None or not (revision.base_url.strip() and revision.token_encrypted):
        raise MulticaServiceError("multica 尚未配置完整，请先保存服务地址与 API Token")
    base_url, token = revision.base_url, decrypt_token(revision)
    db.commit()
    return MulticaWorkspacesOut(workspaces=[MulticaWorkspaceOut(
        id=str(item.get("id") or ""), name=str(item.get("name") or ""), slug=str(item.get("slug") or ""),
    ) for item in multica_client.list_workspaces(base_url, token)])


def list_profile_workspaces(db: Session, owner_id: str, profile_id: str) -> MulticaWorkspacesOut:
    sync_deployment_default(db, owner_id)
    profile = _require_profile(db, owner_id, profile_id)
    return _list_revision_workspaces(db, _current_revision(db, profile.id))


def config_view(config: SuperAssistantMulticaConfig | None) -> MulticaConfigOut:
    if config is None:
        return MulticaConfigOut(configured=False, enabled=False, base_url="", workspace_id="",
            token_set=False, commands=[], last_test_status=None,
            last_test_message=None, last_tested_at=None)
    enabled = bool(config.enabled and config.base_url.strip() and config.workspace_id.strip() and config.token_encrypted)
    return MulticaConfigOut(configured=True, enabled=enabled, base_url=config.base_url,
        workspace_id=config.workspace_id, workspace_name=config.workspace_name,
        token_set=bool(config.token_encrypted),
        commands=[MulticaCommandOut(command=str(item["command"]), title=str(item["title"]),
            description=str(item["description"]), usage=str(item["usage"]), write=bool(item["write"]))
            for item in MULTICA_COMMANDS] if enabled else [],
        last_test_status=config.last_test_status, last_test_message=config.last_test_message,
        last_tested_at=config.last_tested_at)


def _configured_revision(db: Session, owner_id: str) -> SuperAssistantMulticaProfileRevision | None:
    projection = SuperAssistantMulticaConfig
    revision = SuperAssistantMulticaProfileRevision
    profile = SuperAssistantMulticaProfile
    return db.scalar(select(revision).join(projection, projection.profile_revision_id == revision.id)
        .join(profile, profile.id == revision.profile_id).where(
            projection.owner_id == owner_id, profile.owner_id == owner_id, profile.id == projection.profile_id,
            profile.deleted_at.is_(None), revision.retired_at.is_(None), revision.revoked_at.is_(None),
        ).execution_options(populate_existing=True))


def test_connection(db: Session, owner_id: str, *,
                    base_url: str | None = None, token: str | None = None) -> MulticaTestOut:
    sync_deployment_default(db, owner_id)
    return _execute_connection_test(db, owner_id, revision=_configured_revision(db, owner_id),
        base_url=base_url, token=token)


def list_config_workspaces(db: Session, owner_id: str) -> MulticaWorkspacesOut:
    sync_deployment_default(db, owner_id)
    return _list_revision_workspaces(db, _configured_revision(db, owner_id))


def tool_schemas() -> list[dict[str, Any]]:
    """multica 工具的 LLM function-calling schema（仅在配置生效时进入目录）。"""
    return [
        {
            "name": "multica_list_agents",
            "description": "列出 multica 工作台的全部智能体（名称、描述、运行时绑定状态）。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
        {
            "name": "multica_list_tasks",
            "description": "查看 multica 工作台的任务清单，可按状态或负责人过滤。",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "状态过滤（如 open/in_progress/done），留空返回全部"},
                    "assignee": {"type": "string", "description": "负责人名称过滤，留空返回全部"},
                    "limit": {"type": "integer", "description": f"返回条数上限，默认 {_DEFAULT_LIST_LIMIT}，最大 {_MAX_LIST_LIMIT}"},
                },
                "additionalProperties": False,
            },
        },
        {
            "name": "multica_create_task",
            "description": (
                "在 multica 创建任务并指派给指定智能体（写操作，执行前需用户确认）。"
                "assignee 尽量使用 multica_list_agents 返回的智能体名称。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "任务标题（必填，一句话概括）"},
                    "assignee": {"type": "string", "description": "指派对象：智能体名称或 ID，留空则不指派"},
                    "description": {"type": "string", "description": "任务详细描述（可选）"},
                    "priority": {"type": "string", "enum": ["urgent", "high", "medium", "low", "none"], "description": "优先级；省略时使用 multica 默认值"},
                    "allow_duplicate": {"type": "boolean", "description": "同名活跃任务默认被拒绝（409）；确需重复创建时置 true"},
                },
                "required": ["title"],
                "additionalProperties": False,
            },
        },
    ]


def _brief(value: Any, limit: int = 160) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else f"{text[:limit]}…"


def _trim_agent(agent: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": agent.get("id"),
        "name": agent.get("name"),
        "description": _brief(agent.get("description")),
        "runtime_bound": bool(agent.get("runtime_bound")),
    }


def _trim_issue(issue: dict[str, Any]) -> dict[str, Any]:
    assignee = issue.get("assignee") or issue.get("assignee_name")
    if isinstance(assignee, dict):
        assignee = assignee.get("name")
    return {
        "identifier": issue.get("identifier") or issue.get("key"),
        "number": issue.get("number"),
        "title": issue.get("title"),
        "status": issue.get("status"),
        "priority": issue.get("priority"),
        "assignee": assignee,
        "updated_at": issue.get("updated_at"),
    }


def _external_ref(
    issue: dict[str, Any],
    task: dict[str, Any] | None = None,
    *,
    config: Any | None = None,
    revision_id: str | None = None,
) -> str | None:
    """Encode provider identity and the immutable source when available.

    The two legacy identity keys remain stable.  New Kernel calls additionally
    carry the profile revision, endpoint and workspace so a later A→B switch
    cannot redirect reconciliation to the active B profile.
    """
    issue_ref = issue.get("identifier") or issue.get("key") or issue.get("id")
    if not issue_ref:
        return None
    task = task or {}
    task_ref = task.get("id") or task.get("task_id")
    value: dict[str, Any] = {
        "issue_ref": str(issue_ref),
        "task_id": str(task_ref) if task_ref else None,
    }
    if issue.get("id"):
        value["issue_id"] = str(issue["id"])
    source_revision_id = revision_id or getattr(config, "profile_revision_id", None)
    if source_revision_id:
        value["profile_revision_id"] = str(source_revision_id)
    base_url = str(getattr(config, "base_url", "") or "").strip()
    workspace_id = str(getattr(config, "workspace_id", "") or "").strip()
    if source_revision_id and base_url:
        value["base_url"] = base_url
    if source_revision_id and workspace_id:
        value["workspace_id"] = workspace_id
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _parse_external_ref(
    remote_task_ref: str,
) -> tuple[str, str | None, dict[str, str]]:
    try:
        value = json.loads(remote_task_ref or "")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MulticaServiceError("multica remote_task_ref 无法解析") from exc
    if not isinstance(value, dict) or not value.get("issue_ref"):
        raise MulticaServiceError("multica remote_task_ref 缺少 issue_ref")
    task_id = value.get("task_id")
    metadata = {
        key: str(value[key]).strip()
        for key in ("profile_revision_id", "base_url", "workspace_id", "issue_id")
        if value.get(key)
    }
    return str(value["issue_ref"]), str(task_id) if task_id else None, metadata


def _verify_external_ref_source(
    metadata: dict[str, str],
    config: Any,
    revision_id: str | None,
) -> None:
    """Reject a provider reference whose recorded source does not match."""
    recorded_revision = metadata.get("profile_revision_id")
    if recorded_revision and revision_id and recorded_revision != str(revision_id):
        raise MulticaServiceError("multica remote reference belongs to a different profile revision")
    expected_base = str(getattr(config, "base_url", "") or "")
    recorded_base = metadata.get("base_url")
    if recorded_base and recorded_base != expected_base:
        raise MulticaServiceError("multica remote reference belongs to a different service endpoint")
    expected_workspace = str(getattr(config, "workspace_id", "") or "")
    recorded_workspace = metadata.get("workspace_id")
    if recorded_workspace and recorded_workspace != expected_workspace:
        raise MulticaServiceError("multica remote reference belongs to a different workspace")


def _resolve_external_config(
    db: Session,
    owner_id: str,
    *,
    revision_id: str | None = None,
    metadata: dict[str, str] | None = None,
    require_source: bool = False,
) -> tuple[Any, str | None]:
    """Resolve a Multica source without falling back across a bound revision."""
    metadata = metadata or {}
    recorded_revision = metadata.get("profile_revision_id")
    if revision_id and recorded_revision and recorded_revision != str(revision_id):
        raise MulticaServiceError("multica remote reference belongs to a different profile revision")
    resolved_revision_id = str(revision_id or recorded_revision or "") or None
    if resolved_revision_id:
        config = config_for_revision(db, owner_id, resolved_revision_id)
    elif metadata.get("base_url") and metadata.get("workspace_id"):
        config = config_for_source(
            db, owner_id, base_url=metadata["base_url"], workspace_id=metadata["workspace_id"],
        )
        resolved_revision_id = config.id
    else:
        if require_source:
            raise MulticaServiceError("multica 旧任务缺少可验证的来源，需要人工处理")
        config = active_config(db, owner_id)
        if config is None:
            raise MulticaServiceError("multica 未配置或未启用")
        resolved_revision_id = config.profile_revision_id
    _verify_external_ref_source(metadata, config, resolved_revision_id)
    return config, str(resolved_revision_id) if resolved_revision_id else None


def _provider_state(value: Any) -> str:
    state = str(value or "").strip().lower().replace("-", "_")
    if state in _MULTICA_SUCCESS_STATES:
        return "completed"
    if state in _MULTICA_FAILURE_STATES:
        return "failed"
    if state in _MULTICA_CANCELLED_STATES:
        return "cancelled"
    return "running"


def _external_task_issue(config, token: str, issue_ref: str, metadata: dict[str, str]) -> dict[str, Any]:
    expected_id = metadata.get("issue_id")
    issue = multica_client.get_issue(
        config.base_url, token, config.workspace_id, expected_id or issue_ref,
    )
    if not issue.get("id") or issue.get("workspace_id") != config.workspace_id:
        raise MulticaServiceError("multica issue 工作区身份无法验证")
    if expected_id:
        matches = str(issue["id"]) == expected_id
    else:
        matches = issue_ref in {str(issue.get(key) or "") for key in ("id", "identifier", "key")}
    if not matches:
        raise MulticaServiceError("multica issue 身份与原任务不一致")
    return issue


def _verify_task_identity(task: dict[str, Any], issue: dict[str, Any], task_id: str) -> None:
    if (str(task.get("id") or "") != task_id
            or task.get("issue_id") != issue["id"]
            or task.get("workspace_id") != issue["workspace_id"]
            or not task.get("status")):
        raise MulticaServiceError("multica 执行身份与原任务不一致")


def _external_task_run(config, token: str, issue: dict[str, Any], task_id: str | None) -> dict[str, Any]:
    runs = multica_client.list_task_runs(config.base_url, token, config.workspace_id, str(issue["id"]))
    if task_id:
        matches = [run for run in runs if str(run["id"]) == task_id]
        if len(matches) != 1:
            raise MulticaServiceError("multica 原执行记录无法确认，需要人工处理")
        task = matches[0]
    else:
        # The issue-create API does not return a run ID. Bind only an
        # unambiguous history; an active rerun cannot stand in for the original.
        if len(runs) > 1:
            raise MulticaServiceError("multica 执行来源不唯一，需要人工处理")
        if not runs:
            return {}
        task = runs[0]
    _verify_task_identity(task, issue, str(task["id"]))
    return task


def _external_observation(config, revision_id: str | None, issue: dict[str, Any],
                          task: dict[str, Any], token: str) -> dict[str, Any]:
    provider_status = str(task.get("status") or issue.get("status") or "unknown")
    content = task.get("result") or task.get("content") or task.get("error")
    if not task:
        content = issue.get("result") or issue.get("description")
    text = str(content or "")
    if token:
        text = text.replace(token, "[redacted]")
    remote_ref = _external_ref(issue, task, config=config, revision_id=revision_id)
    # Polls are observations, not upstream event IDs. Equal observations replay
    # safely while a later status/result gets a distinct reconciliation key.
    fingerprint = hashlib.sha256(json.dumps(
        [remote_ref, provider_status, text], ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()
    return {
        "status": _provider_state(provider_status),
        "content": text[:20000],
        "remote_task_ref": remote_ref,
        "provider_status": provider_status,
        "provider_event_id": f"multica:{fingerprint}",
    }


def query_external_task(
    db: Session,
    owner_id: str,
    remote_task_ref: str,
    *,
    revision_id: str | None = None,
) -> dict[str, Any]:
    """Observe the original run through its immutable source and issue identity."""
    issue_ref, task_id, metadata = _parse_external_ref(remote_task_ref)
    config, resolved_revision_id = _resolve_external_config(
        db, owner_id, revision_id=revision_id, metadata=metadata, require_source=True,
    )
    token = decrypt_token(config)
    issue = _external_task_issue(config, token, issue_ref, metadata)
    task = _external_task_run(config, token, issue, task_id)
    return _external_observation(config, resolved_revision_id, issue, task, token)


def cancel_external_task(
    db: Session,
    owner_id: str,
    remote_task_ref: str,
    *,
    revision_id: str | None = None,
) -> dict[str, Any]:
    """Cancel only the proven original run; completed runs never target a rerun."""
    issue_ref, task_id, metadata = _parse_external_ref(remote_task_ref)
    config, resolved_revision_id = _resolve_external_config(
        db, owner_id, revision_id=revision_id, metadata=metadata, require_source=True,
    )
    token = decrypt_token(config)
    issue = _external_task_issue(config, token, issue_ref, metadata)
    task = _external_task_run(config, token, issue, task_id)
    if not task:
        if _provider_state(issue.get("status")) == "cancelled":
            return _external_observation(config, resolved_revision_id, issue, task, token)
        raise MulticaServiceError("multica 未返回可确认的原执行记录")
    if _provider_state(task["status"]) != "running":
        return _external_observation(config, resolved_revision_id, issue, task, token)
    response = multica_client.cancel_task(
        config.base_url, token, config.workspace_id, str(issue["id"]), str(task["id"]),
    )
    try:
        _verify_task_identity(response, issue, str(task["id"]))
    except (MulticaServiceError, TypeError, AttributeError) as exc:
        raise multica_client.MulticaRequestError(
            "multica 取消响应未确认原执行身份", outcome="unknown",
        ) from exc
    return _external_observation(config, resolved_revision_id, issue, response, token)


def execute_tool(
    db: Session,
    owner_id: str,
    name: str,
    arguments: dict[str, Any],
    *,
    revision_id: str | None = None,
) -> str:
    """Execute a Multica tool against an explicit immutable revision when bound."""
    if name not in _MULTICA_TOOL_NAMES:
        raise MulticaServiceError(f"未知 multica 工具 {name}")
    config, resolved_revision_id = _resolve_external_config(
        db, owner_id, revision_id=revision_id,
    )
    token = decrypt_token(config)
    base_url, workspace_id = config.base_url, config.workspace_id

    if name == "multica_list_agents":
        agents = multica_client.list_agents(base_url, token, workspace_id)
        return json.dumps(
            {"count": len(agents), "agents": [_trim_agent(agent) for agent in agents]},
            ensure_ascii=False,
        )

    if name == "multica_list_tasks":
        try:
            limit = int(arguments.get("limit") or _DEFAULT_LIST_LIMIT)
        except (TypeError, ValueError):
            limit = _DEFAULT_LIST_LIMIT
        limit = max(1, min(limit, _MAX_LIST_LIMIT))
        issues = multica_client.list_issues(
            base_url,
            token,
            workspace_id,
            status=str(arguments.get("status") or "").strip() or None,
            assignee=str(arguments.get("assignee") or "").strip() or None,
            limit=limit,
        )
        return json.dumps(
            {"count": len(issues), "issues": [_trim_issue(issue) for issue in issues]},
            ensure_ascii=False,
        )

    # multica_create_task
    title = str(arguments.get("title") or "").strip()
    if not title:
        raise MulticaServiceError("创建任务失败：title 不能为空")
    assignee = str(arguments.get("assignee") or "").strip() or None
    description = str(arguments.get("description") or "").strip() or None
    assignee_id: str | None = None
    resolved_name: str | None = None
    if assignee:
        # 服务端 create 只认 assignee_type=agent + assignee_id（真实环境实测：
        # 名称字段会被静默忽略）；名称到 ID 的解析与 multica CLI 同语义
        if _UUID_PATTERN.match(assignee):
            assignee_id = assignee
        else:
            agents = multica_client.list_agents(base_url, token, workspace_id)
            assignee_id, resolved_name = multica_client.match_agent(agents, assignee)
    create_options = {}
    if "priority" in arguments:
        priority = arguments["priority"]
        if priority not in ("urgent", "high", "medium", "low", "none"):
            raise MulticaServiceError("创建任务失败：priority 无效")
        create_options["priority"] = priority
    issue = multica_client.create_issue(
        base_url,
        token,
        workspace_id,
        title=title,
        description=description,
        assignee_id=assignee_id,
        allow_duplicate=bool(arguments.get("allow_duplicate")),
        **create_options,
    )
    issue_ref = _external_ref(
        issue, config=config, revision_id=resolved_revision_id,
    )
    return json.dumps(
        {
            "created": True,
            "issue": _trim_issue(issue),
            # Kernel callers retain this opaque identity and reconcile through
            # query_external_task; legacy callers can ignore the additive key.
            "remote_task_ref": issue_ref,
            "assignee": resolved_name or assignee,
            "note": "任务已创建" + (
                f"并指派给 {resolved_name or assignee}，被指派的智能体会自动开始执行"
                if assignee_id else ""
            ),
        },
        ensure_ascii=False,
    )
