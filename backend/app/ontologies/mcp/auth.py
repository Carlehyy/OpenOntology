"""本体 MCP 端点的请求级认证与失败节流。

凭据取自 ``X-OpenOntology-Username``/``X-OpenOntology-Password``：原样逐
字节使用（不 percent-decode），控制字符/超长拒绝，每请求复用
``authenticate_user`` + agent 菜单校验。凭据与请求头值绝不进入日志或
异常文本：错误只含静态 code/message。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

USERNAME_HEADER = "x-openontology-username"
PASSWORD_HEADER = "x-openontology-password"

# 格式边界：密码上限对齐 auth LoginRequest；用户名对齐 User.username 语义。
MAX_USERNAME_LENGTH = 64
MAX_PASSWORD_LENGTH = 128

# 认证并发上界：立即拒绝而不是排队，防止 bcrypt 风暴占满线程池。
MAX_CONCURRENT_AUTH = 8

# 失败节流（进程内、无外部依赖、无新配置项）。
MAX_AUTH_FAILURES = 5
AUTH_FAILURE_WINDOW_SECONDS = 60.0
AUTH_LOCKOUT_SECONDS = 60.0
# 节流表容量上限：随机用户名刷失败也最多占这么多条记录。
MAX_FAILURE_RECORDS = 10_000

AGENT_MENU_KEY = "agent"


class McpAuthError(Exception):
    """认证层错误：静态 code/message，可安全透出给客户端。"""

    def __init__(self, status: int, code: str, message: str, *, retry_after: int = 0):
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


@dataclass
class AuthenticatedPrincipal:
    """跨线程传递的主体标识：只携带标量，不携带 ORM 对象。"""

    user_id: str
    username: str
    role: str


@dataclass
class _FailureRecord:
    failures: int = 0
    first_failure_at: float = field(default_factory=time.monotonic)
    locked_until: float = 0.0

    def expired(self, now: float) -> bool:
        # 锁定结束后窗口继续保留到自然过期，保证锁定语义完整。
        return (
            self.locked_until <= now
            and now - self.first_failure_at > AUTH_FAILURE_WINDOW_SECONDS
        )


class _FailureThrottle:
    """(客户端地址, 用户名) 维度的滑动窗口失败节流，进程内实现。"""

    def __init__(self) -> None:
        self._records: dict[str, _FailureRecord] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(client: str, username: str) -> str:
        return f"{client}|{username}"

    def check(self, client: str, username: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            record = self._records.get(self._key(client, username))
            if record is not None and record.locked_until > now:
                raise McpAuthError(
                    429,
                    "auth_throttled",
                    "认证失败次数过多，请稍后重试",
                    retry_after=max(1, int(record.locked_until - now) + 1),
                )

    def record_failure(self, client: str, username: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            key = self._key(client, username)
            record = self._records.get(key)
            if record is None:
                if len(self._records) >= MAX_FAILURE_RECORDS:
                    # 容量保护：丢弃最早过期的记录后再插入。
                    self._prune(now, force=True)
                record = _FailureRecord()
                self._records[key] = record
            if now - record.first_failure_at > AUTH_FAILURE_WINDOW_SECONDS:
                record.failures = 0
                record.first_failure_at = now
            record.failures += 1
            if record.failures >= MAX_AUTH_FAILURES:
                record.locked_until = now + AUTH_LOCKOUT_SECONDS

    def record_success(self, client: str, username: str) -> None:
        with self._lock:
            self._records.pop(self._key(client, username), None)

    def _prune(self, now: float, *, force: bool = False) -> None:
        expired = [key for key, record in self._records.items() if record.expired(now)]
        for key in expired:
            del self._records[key]
        if force and len(self._records) >= MAX_FAILURE_RECORDS:
            # 全部活跃时按最早失败时间逐出：宁可放过一次节流也不无界增长。
            oldest = min(
                self._records.items(), key=lambda item: item[1].first_failure_at
            )
            del self._records[oldest[0]]

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


_throttle = _FailureThrottle()
# 有界认证并发：容量外立即拒绝，不排队。
_auth_slots = threading.BoundedSemaphore(MAX_CONCURRENT_AUTH)


def reset_auth_state() -> None:
    """测试辅助：仅清空节流表（不动信号量，避免与并发任务竞态）。"""
    _throttle.clear()


def _reject_control_chars(value: str) -> bool:
    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)


def extract_credentials(headers) -> tuple[str, str]:
    """提取并格式校验，返回 (username, password) 原值；违规走静态错误。"""
    username = headers.get(USERNAME_HEADER)
    password = headers.get(PASSWORD_HEADER)
    if username is None or password is None or not username or not password:
        raise McpAuthError(401, "missing_credentials", "缺少认证请求头")
    if len(username) > MAX_USERNAME_LENGTH or len(password) > MAX_PASSWORD_LENGTH:
        raise McpAuthError(400, "invalid_credentials_format", "认证请求头格式无效")
    if _reject_control_chars(username) or _reject_control_chars(password):
        raise McpAuthError(400, "invalid_credentials_format", "认证请求头格式无效")
    return username, password


def authenticate(db: Session, username: str, password: str, *, client: str) -> AuthenticatedPrincipal:
    """认证核心（须在 worker 线程调用）：节流 → bcrypt/DB → agent 菜单。"""
    _throttle.check(client, username)
    if not _auth_slots.acquire(blocking=False):
        raise McpAuthError(
            429, "auth_busy", "认证并发已达上限，请稍后重试", retry_after=1
        )
    try:
        from app.auth.service import authenticate_user

        user = authenticate_user(db, username, password)
        if user is None:
            _throttle.record_failure(client, username)
            raise McpAuthError(401, "invalid_credentials", "用户名或密码错误")

        from app.auth.permissions import user_has_menu_access

        if not user_has_menu_access(db, user, AGENT_MENU_KEY):
            _throttle.record_success(client, username)
            raise McpAuthError(403, "menu_forbidden", "无权访问本体助手")

        _throttle.record_success(client, username)
        return AuthenticatedPrincipal(
            user_id=str(user.id), username=str(user.username), role=str(user.role)
        )
    finally:
        _auth_slots.release()
