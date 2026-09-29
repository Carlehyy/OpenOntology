"""个人变量引用解析（隐私变量 + 环境变量）。

接口配置的 header 值、body 内容、URL 里可写 ``{{privacy:VAR_KEY}}`` 或
``{{env:VAR_KEY}}`` 占位符，执行时由执行器调用 :func:`resolve_personal_refs`
把占位符替换为当前调用者（数据所有者本人）的个人变量明文值。

权限模型与 :mod:`app.auth.router.get_privacy_var_value` 完全对齐：只解析
当前 JWT 用户的变量，跨用户或对不存在的 key 一律报错，绝不静默发空。
解析后的明文只用于出站 HTTP 请求，不写进调用历史快照（快照整体打码）。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from app.auth.crypto import decrypt_value
from app.auth.models import UserEnvVar, UserPrivacyVar
from app.database import SessionLocal

_LOG = logging.getLogger(__name__)

# 占位符语法：{{privacy:VAR_KEY}} / {{env:VAR_KEY}}，VAR_KEY 与两类个人变量
# 的 key 同字符集（创建端校验见 auth schemas，此处只做结构约束）。
PRIVACY_REF_RE = re.compile(r"\{\{privacy:([A-Za-z0-9_.\-]+)\}\}")
ENV_REF_RE = re.compile(r"\{\{env:([A-Za-z0-9_.\-]+)\}\}")
PERSONAL_REF_RE = re.compile(r"\{\{(privacy|env):([A-Za-z0-9_.\-]+)\}\}")
_REDACTED = "***"


def resolve_personal_refs(value: Any, user) -> Any:
    """把 ``value`` 中的 ``{{privacy:KEY}}`` / ``{{env:KEY}}`` 替换为当前用户的变量明文。

    - 仅处理 str；非 str（bytes / dict / list）原样返回。
    - 隐私/环境变量各一次查询加载全部命中的 key，避免逐 key 打开 DB 会话。
    - 无 actor（公开代理 / n8n 内部代理）：占位符原样返回，fail-closed。
    - 缺失的 key 抛 ``ValueError``，绝不静默发空值（避免凭据缺失时仍发出站）。
    """
    if not isinstance(value, str):
        return value
    privacy_keys = set(PRIVACY_REF_RE.findall(value))
    env_keys = set(ENV_REF_RE.findall(value))
    if not privacy_keys and not env_keys:
        return value
    if user is None:
        return value

    # admin 与隐私变量同理：接口私有可见前提下 admin 调用的就是自己的接口，
    # 占位符就是 admin 自己的 key，正常解析。
    plaintext_map = {
        **_load_privacy_plaintext(privacy_keys, user),
        **_load_env_plaintext(env_keys, user),
    }
    missing = sorted(
        ref
        for kind, keys in (("privacy", privacy_keys), ("env", env_keys))
        for ref in (f"{kind}:{key}" for key in keys)
        if ref not in plaintext_map
    )
    if missing:
        raise ValueError("个人变量未配置：" + ", ".join(missing))

    def _sub(match: re.Match) -> str:
        return plaintext_map[f"{match.group(1)}:{match.group(2)}"]

    return PERSONAL_REF_RE.sub(_sub, value)


def _load_privacy_plaintext(keys: set[str], user) -> dict[str, str]:
    """批量解密当前用户命中的隐私变量明文，键带 ``privacy:`` 前缀。"""
    out: dict[str, str] = {}
    if not keys:
        return out
    db = SessionLocal()
    try:
        rows = (
            db.query(UserPrivacyVar)
            .filter(
                UserPrivacyVar.user_id == user.id,
                UserPrivacyVar.key.in_(keys),
            )
            .all()
        )
        for row in rows:
            if not row.value_encrypted:
                continue
            try:
                out[f"privacy:{row.key}"] = decrypt_value(row.value_encrypted)
            except Exception:  # noqa: BLE001
                # 解密失败按"无值"处理，由缺失校验统一报错；落 warning
                # 便于区分「没配置」与「配置了但解不开」（不打印密文/密钥材料）
                _LOG.warning(
                    "隐私变量解密失败，按未配置处理：user=%s key=%s",
                    getattr(user, "id", None),
                    row.key,
                )
                continue
    finally:
        db.close()
    return out


def _load_env_plaintext(keys: set[str], user) -> dict[str, str]:
    """批量解密当前用户命中的环境变量明文，键带 ``env:`` 前缀。"""
    out: dict[str, str] = {}
    if not keys:
        return out
    db = SessionLocal()
    try:
        rows = (
            db.query(UserEnvVar)
            .filter(
                UserEnvVar.user_id == user.id,
                UserEnvVar.key.in_(keys),
            )
            .all()
        )
        for row in rows:
            if not row.value_encrypted:
                continue
            try:
                out[f"env:{row.key}"] = decrypt_value(row.value_encrypted)
            except Exception:  # noqa: BLE001
                _LOG.warning(
                    "环境变量解密失败，按未配置处理：user=%s key=%s",
                    getattr(user, "id", None),
                    row.key,
                )
                continue
    finally:
        db.close()
    return out


def redact_personal_refs(value: Any) -> Any:
    """把 ``value`` 中的个人变量占位符替换为 ``***``，用于审计快照。

    快照里保留占位符会让审计日志本身泄露"这个接口用了哪个变量名"，
    因此统一替换为 ``***``。
    """
    if isinstance(value, str):
        return PERSONAL_REF_RE.sub(_REDACTED, value)
    return value


def interface_has_personal_refs(iface) -> bool:
    """接口的可解析字段（URL / Header 值 / Body）里是否含个人变量占位符。

    接受 SQLite 行 dict（``_row_to_dict``）或 ``InterfaceIn`` 草稿。只检查
    执行器真正会解析的三处；query 参数值不在解析范围内，也不检查。
    公开代理与 n8n 内部代理链路没有用户身份、永不解析这些占位符，
    发布 / 编排前用它拒绝必然失败的配置。
    """
    if isinstance(iface, dict):
        url = iface.get("url") or ""
        headers = iface.get("headers") or []
        body = iface.get("body_content") or ""
    else:
        url = getattr(iface, "url", "") or ""
        headers = getattr(iface, "headers", None) or []
        body = getattr(iface, "body_content", "") or ""
    if PERSONAL_REF_RE.search(url):
        return True
    for item in headers:
        if isinstance(item, dict):
            value = item.get("value", "") or ""
        else:
            value = getattr(item, "value", "") or ""
        if PERSONAL_REF_RE.search(value):
            return True
    return bool(PERSONAL_REF_RE.search(body))
