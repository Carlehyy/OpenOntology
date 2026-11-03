"""WeKnora 内置 MCP 的默认注册、revision 稳定性与手工重检。

职责边界：
- 通用 ``mcp_server_service`` 只做目录/测试入口接线；
- 本模块拥有 WeKnora 的固定只读白名单、owner-scoped 幂等注册与
  CapabilityRevision 冻结；
- 不反向 import ``mcp_server_service``，也不在默认注册路径联网。
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import re

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.super_assistant.kernel.capability_service import revoke_capability_revisions
from app.super_assistant.kernel.connectors import TrustLevel
from app.super_assistant.kernel.models import CapabilityRevision
from app.super_assistant.mcp_client import (
    decrypt_headers,
    encrypt_headers,
    namespaced_tool_name,
)
from app.super_assistant.mcp_manifest import (
    freeze_manifest,
    manifest_hash,
    manifest_keys,
)
from app.super_assistant.models import SuperAssistantMcpServer
from app.super_assistant.schemas import McpTestOut
from app.super_assistant.weknora.policy import (
    WEKNORA_ALLOWED_TOOL_SET,
    WEKNORA_ALLOWED_TOOLS,
    WEKNORA_BUILTIN_KEY,
    WEKNORA_DESCRIPTION,
    WEKNORA_DISPLAY_NAME,
    WEKNORA_SERVER_NAME,
)
from app.super_assistant.weknora.service import WeKnoraError

logger = logging.getLogger(__name__)

__all__ = [
    "ensure_server",
    "install_for_platform_admin",
    "test_server",
    "WEKNORA_ALLOWED_TOOLS",
    "WEKNORA_BUILTIN_KEY",
]

_SAFE_ERROR_CODE = re.compile(r"WEKNORA_[A-Z0-9_]{1,64}")


def _safe_error_code(exc: BaseException) -> str:
    """Only normalized WEKNORA_* codes may reach user-visible fields/logs."""
    message = str(exc).strip()
    return message if _SAFE_ERROR_CODE.fullmatch(message) else "WEKNORA_TEST_FAILED"


def _record_test_failure(
    db: Session,
    item: SuperAssistantMcpServer,
    message: str,
) -> None:
    """Persist the failure marker without letting DB errors surface raw."""
    try:
        item.last_test_status = "error"
        item.last_test_message = message
        db.commit()
    except Exception:  # noqa: BLE001 - 响应已收敛为安全 code，不泄露驱动异常
        db.rollback()
        logger.error("WeKnora 手工重检失败状态落库失败: WEKNORA_TEST_FAILED")


def _weknora_service():
    from app.super_assistant.weknora import service

    return service


def _manifest(tools) -> list[dict]:
    """Normalize the verified tool list, rejecting anything outside the allowlist.

    Unknown/duplicate tools are never silently dropped: the bootstrap probe
    compares the live manifest with the frozen state, and a stale/foreign
    manifest must fail closed instead of being partially registered.
    """
    if not isinstance(tools, (list, tuple)):
        raise WeKnoraError("WEKNORA_INVALID_TOOLS")
    by_name: dict[str, dict] = {}
    for tool in tools:
        if not isinstance(tool, dict):
            raise WeKnoraError("WEKNORA_INVALID_TOOLS")
        name = str(tool.get("name") or "").strip()
        if name not in WEKNORA_ALLOWED_TOOL_SET:
            raise WeKnoraError("WEKNORA_UNEXPECTED_TOOL")
        if name in by_name:
            raise WeKnoraError("WEKNORA_UNEXPECTED_TOOL")
        schema = tool.get("input_schema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise WeKnoraError("WEKNORA_INVALID_TOOL_SCHEMA")
        by_name[name] = {
            "name": name,
            "description": str(tool.get("description") or ""),
            "input_schema": schema,
        }
    return [by_name[name] for name in WEKNORA_ALLOWED_TOOLS if name in by_name]


def _headers(integration) -> dict[str, str]:
    token = str(getattr(integration, "mcp_token", "") or "")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _revision_available(
    db: Session,
    server: SuperAssistantMcpServer,
    tools: list[dict],
    digest: str,
    revision: int,
) -> bool:
    """A revision is reusable only if no conflicting frozen snapshot exists.

    Deleting the server keeps its CapabilityRevision rows for audit; a later
    re-registration with a changed manifest must therefore pick a fresh
    revision instead of colliding with the historical snapshot.
    """
    keys = [
        namespaced_tool_name(server.name, str(tool.get("name") or ""))
        for tool in tools
        if isinstance(tool, dict) and tool.get("name")
    ]
    if not keys:
        return True
    rows = db.query(CapabilityRevision).filter(
        CapabilityRevision.key.in_(keys),
        CapabilityRevision.revision == int(revision),
    ).all()
    if not rows:
        return True
    return all(
        row.manifest_hash == digest
        and row.side_effect_class == "read_only"
        and row.source == "mcp"
        and row.trust_level == TrustLevel.USER_UNTRUSTED.value
        for row in rows
    )


def _select_revision(
    db: Session,
    server: SuperAssistantMcpServer,
    tools: list[dict],
    digest: str,
    desired: int,
) -> int:
    revision = max(1, int(desired))
    while not _revision_available(db, server, tools, digest, revision):
        revision += 1
    return revision


async def test_server(db: Session, item: SuperAssistantMcpServer) -> McpTestOut:
    """Re-pin a WeKnora builtin only against the verified integration snapshot.

    Live discovery must match the frozen state exactly; otherwise a manual
    test would pin a temporary manifest that the next default ensure would
    overwrite.  State changes require a bootstrap verify/update first.
    """
    item.last_tested_at = datetime.now(timezone.utc)
    try:
        integration = _weknora_service().get_integration()
        verified = _manifest(getattr(integration, "tools", None))
        discovered = _manifest(await _weknora_service().discover_tools())
        if discovered != verified:
            raise WeKnoraError("WEKNORA_STATE_OUT_OF_DATE")
        tools = verified
        digest = manifest_hash(item, tools)
        revision = int(item.manifest_revision or 1)
        if item.manifest_hash and item.manifest_hash != digest:
            # 清单/URL/token 变化才升 revision；旧快照撤销，新快照继承
            # 用户当前的启停状态。
            revoke_capability_revisions(
                db, manifest_keys(item), revision=revision,
            )
            revision += 1
        revision = _select_revision(db, item, tools, digest, revision)
        freeze_manifest(
            db, item, tools, revision, enabled=bool(item.enabled),
            side_effect_class="read_only",
        )
        item.manifest_revision = revision
        item.manifest_hash = digest
        item.tool_manifest = tools
        item.constrained = True
        item.last_test_status = "success"
        item.last_test_message = f"连接成功，发现 {len(tools)} 个工具"
        db.commit()
        return McpTestOut(ok=True, message=item.last_test_message, tools=tools)
    except WeKnoraError as exc:
        # 归一化 code 可回显；保留上一次已验证清单与授权位。
        db.rollback()
        message = _safe_error_code(exc)[:500]
        _record_test_failure(db, item, message)
        return McpTestOut(ok=False, message=message, tools=[])
    except Exception:
        # 远端/DB 异常原文可能含连接串、token 或 SQL 参数，禁止落库/回显。
        db.rollback()
        logger.error("WeKnora 手工重检失败: WEKNORA_TEST_FAILED")
        _record_test_failure(db, item, "WEKNORA_TEST_FAILED")
        return McpTestOut(ok=False, message="WEKNORA_TEST_FAILED", tools=[])


def ensure_server(
    db: Session,
    owner_id: str,
) -> SuperAssistantMcpServer:
    """Idempotently register the platform WeKnora read-only MCP server.

    Uses the bootstrap-verified integration snapshot only, so a normal
    request never performs a discovery call.  Fail-closed: an unverified
    integration, a conflicting server name or a registration failure raises
    ``WeKnoraError`` (HTTP 503 at the adapter) instead of silently running
    without the platform knowledge tools.
    """
    try:
        return _ensure_server(db, owner_id)
    except WeKnoraError:
        db.rollback()
        raise
    except Exception:  # noqa: BLE001 - 未知注册故障也必须 fail-closed
        db.rollback()
        logger.error("WeKnora 内置 MCP 注册失败: WEKNORA_REGISTRATION_FAILED")
        raise WeKnoraError("WEKNORA_REGISTRATION_FAILED") from None


def install_for_platform_admin() -> str:
    """Bootstrap-time registration for the single platform admin.

    Runs after the WeKnora probe and before workers start.  A missing or
    ambiguous active admin, a conflicting server name or any DB failure is a
    safe startup error (``WeKnoraError``), never a deferred first-chat failure.
    """
    from app.auth.models import User
    from app.shared.database import SessionLocal

    db = SessionLocal()
    try:
        admins = db.query(User).filter(
            User.role == "admin",
            User.is_active.is_(True),
        ).all()
        if len(admins) != 1:
            raise WeKnoraError("WEKNORA_PLATFORM_ADMIN_REQUIRED")
        return ensure_server(db, admins[0].id).id
    except WeKnoraError:
        db.rollback()
        raise
    except Exception:  # noqa: BLE001 - 启动期注册失败必须安全终止
        db.rollback()
        logger.error("WeKnora 平台管理员内置 MCP 注册失败: WEKNORA_REGISTRATION_FAILED")
        raise WeKnoraError("WEKNORA_REGISTRATION_FAILED") from None
    finally:
        db.close()


def _ensure_server(
    db: Session,
    owner_id: str,
) -> SuperAssistantMcpServer:
    integration = _weknora_service().get_integration()

    url = str(getattr(integration, "mcp_url", "") or "").strip()
    if not url:
        raise WeKnoraError("WEKNORA_INVALID_STATE")
    tools = _manifest(getattr(integration, "tools", None))
    if not tools:
        raise WeKnoraError("WEKNORA_REQUIRED_TOOLS_MISSING")
    headers = _headers(integration)

    item = db.query(SuperAssistantMcpServer).filter(
        SuperAssistantMcpServer.owner_id == owner_id,
        SuperAssistantMcpServer.builtin_key == WEKNORA_BUILTIN_KEY,
    ).first()
    if item is None:
        name_taken = db.query(SuperAssistantMcpServer).filter(
            SuperAssistantMcpServer.owner_id == owner_id,
            SuperAssistantMcpServer.name == WEKNORA_SERVER_NAME,
        ).first()
        if name_taken is not None:
            raise WeKnoraError("WEKNORA_SERVER_NAME_CONFLICT")
        item = SuperAssistantMcpServer(
            owner_id=owner_id,
            name=WEKNORA_SERVER_NAME,
            display_name=WEKNORA_DISPLAY_NAME,
            description=WEKNORA_DESCRIPTION,
            builtin_key=WEKNORA_BUILTIN_KEY,
            constrained=True,
            transport="streamable_http",
            url=url,
            headers_encrypted=encrypt_headers(headers)[0],
            header_names=sorted(headers, key=str.lower),
            command=None,
            args=[],
            env_encrypted=None,
            env_names=[],
            enabled=True,
            require_confirmation=False,
        )
        try:
            db.add(item)
            db.flush()
        except IntegrityError:
            # 并发首注册：只有确实出现同 owner/builtin 行时才沿用。
            db.rollback()
            existing = db.query(SuperAssistantMcpServer).filter(
                SuperAssistantMcpServer.owner_id == owner_id,
                SuperAssistantMcpServer.builtin_key == WEKNORA_BUILTIN_KEY,
            ).first()
            if existing is None:
                raise WeKnoraError("WEKNORA_REGISTRATION_FAILED") from None
            return existing
        digest = manifest_hash(item, tools)
        revision = _select_revision(db, item, tools, digest, 1)
        freeze_manifest(
            db, item, tools, revision, enabled=True,
            side_effect_class="read_only",
        )
        item.manifest_revision = revision
        item.manifest_hash = digest
        item.tool_manifest = tools
        item.last_test_status = "success"
        item.last_test_message = f"平台内置连接成功，发现 {len(tools)} 个工具"
        item.last_tested_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(item)
        return item

    try:
        current_headers = decrypt_headers(item.headers_encrypted)
    except Exception:  # noqa: BLE001 - 旧密文不可读时按受信任配置重写
        current_headers = None
    connection_changed = item.url != url or current_headers != headers
    if item.url != url:
        item.url = url
    if current_headers != headers:
        # token 未变化时不重新加密：密文是 manifest 哈希输入，重加密会造成
        # revision 无意义漂移。
        item.headers_encrypted, item.header_names = encrypt_headers(headers)
    metadata_changed = (
        item.display_name != WEKNORA_DISPLAY_NAME
        or item.description != WEKNORA_DESCRIPTION
        or not item.constrained
    )
    digest = manifest_hash(item, tools)
    if (
        not connection_changed
        and item.tool_manifest == tools
        and item.manifest_hash == digest
    ):
        if metadata_changed:
            item.display_name = WEKNORA_DISPLAY_NAME
            item.description = WEKNORA_DESCRIPTION
            item.constrained = True
            db.commit()
            db.refresh(item)
        return item

    old_revision = int(item.manifest_revision or 1)
    revoke_capability_revisions(
        db, manifest_keys(item), revision=old_revision,
    )
    revision = _select_revision(db, item, tools, digest, old_revision + 1)
    freeze_manifest(
        db, item, tools, revision, enabled=bool(item.enabled),
        side_effect_class="read_only",
    )
    item.manifest_revision = revision
    item.manifest_hash = digest
    item.tool_manifest = tools
    item.constrained = True
    item.display_name = WEKNORA_DISPLAY_NAME
    item.description = WEKNORA_DESCRIPTION
    item.last_test_status = "success"
    item.last_test_message = f"平台内置连接成功，发现 {len(tools)} 个工具"
    item.last_tested_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(item)
    return item
