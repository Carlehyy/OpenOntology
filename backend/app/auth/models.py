import uuid
from datetime import datetime, timezone
from sqlalchemy import JSON, String, Boolean, DateTime, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base

class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="viewer")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 会话吊销代数：JWT 的 ver claim 与此值不一致即失效。改密（自助或管理
    # 员重置）时 +1，使全部已签发 token 立即作废。默认 0，且校验侧对缺失
    # ver claim 的存量 token 按 0 处理，升级不强制全员重新登录。
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 隐私变量上报 token（用户级，Fernet 密文）。为空表示该用户尚未启用
    # 隐私变量上报；创建首个隐私变量或显式重置时生成。nullable 以兼容存量用户。
    report_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class RoleMenuPermission(Base):
    """Navigable product areas granted to a non-admin role."""

    __tablename__ = "role_menu_permissions"

    role: Mapped[str] = mapped_column(String(20), primary_key=True)
    menu_keys: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    updated_by: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class UserEnvVar(Base):
    """用户私有环境变量（MYW-56）。

    key 明文存储用于列表展示与 (user_id, key) 唯一约束；value 为 Fernet
    密文（与 MCP 服务器配置同一加密设施）。接口代理的 URL/Header/Body 里
    可以 ``{{env:KEY}}`` 占位符引用，UI 调用链路以本人身份解析（见
    app.api_hub.personal_ref）；无用户身份的链路（公开代理 / n8n）不解析。
    """

    __tablename__ = "user_env_vars"
    __table_args__ = (
        UniqueConstraint("user_id", "key", name="uq_user_env_vars_user_key"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    value_encrypted: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class UserPrivacyKeypair(Base):
    """用户隐私变量 RSA 密钥对（每用户一行）。

    公钥 PEM 明文存储——下发给用户的上报脚本只用公钥加密，泄露公钥无
    风险。私钥 PEM 经平台 Fernet 再加密后落库（DB 拖库时私钥仍不可读，
    还需要平台 Fernet 密钥才能解出私钥）。创建首个隐私变量时按需生成。
    """

    __tablename__ = "user_privacy_keypairs"

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    public_key_pem: Mapped[str] = mapped_column(Text, nullable=False)
    private_key_pem_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class UserQueryKey(Base):
    """用户变量查询密钥（PAT 式：跟用户不跟变量、按类别隔离、多把并存）。

    一把某类别（env/privacy）有效密钥即可读取该用户该类别下全部变量，
    供 n8n 等外部流水线经公开只读端点（app.auth.public_query）无人值守
    调用。明文仅在创建时返回一次；落库只存 sha256 key_hash（查表校验）
    与可见前缀 key_prefix（列表识别用，不足以还原密钥）。expires_at 为
    NULL 表示永久。吊销为软删除（revoked_at），保留行供用户回看。
    """

    __tablename__ = "user_query_keys"
    __table_args__ = (
        UniqueConstraint("key_hash", name="uq_user_query_keys_key_hash"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    key_prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class UserPrivacyVar(Base):
    """用户隐私变量。

    与 UserEnvVar 的差异：value 由用户本地脚本用该用户的 RSA 公钥加密后
    上报，平台用对应私钥解密后再以 Fernet 包一层落库（双层加密）。key
    明文用于列表展示与 (user_id, key) 唯一约束。本期仅存储与维护，不注入
    任何执行链路（与 UserEnvVar 同一克制边界）。
    """

    __tablename__ = "user_privacy_vars"
    __table_args__ = (
        UniqueConstraint("user_id", "key", name="uq_user_privacy_vars_user_key"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    # 最终落库值：RSA 解密后的明文再经 Fernet 加密（双层保险）。
    value_encrypted: Mapped[str] = mapped_column(Text, nullable=False, default="")
    last_reported_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
