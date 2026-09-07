from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class UserOut(BaseModel):
    id: str
    username: str
    email: str
    role: str
    is_active: bool
    created_at: datetime
    menu_permissions: list[str] = Field(default_factory=list)

    model_config = {"from_attributes": True}

class LoginRequest(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"

class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


class ProfileUpdate(BaseModel):
    """自助资料更新（MYW-56）：只开放 email。

    username 是账号唯一标识，刻意不在此模型中——请求即使携带也会被忽略，
    从协议层面保证用户名不可自改。
    """

    email: EmailStr


class UserEnvVarItem(BaseModel):
    # key：非空、≤128 字符，仅允许字母/数字/下划线/连字符/点；value：字符串
    #（可空），长度上限 4096 用于防止无界写入。pattern 锚定全串
    #（pydantic v2 的 pattern 是 search 语义，不锚定会放过含非法字符的 key）。
    key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.\-]+$")
    value: str = Field(default="", max_length=4096)


class UserEnvVarsReplace(BaseModel):
    items: list[UserEnvVarItem] = Field(default_factory=list, max_length=50)


# ---- 隐私变量（RSA 公钥加密上报 + 平台私钥解密 + Fernet 落库） ----

class PrivacyVarCreate(BaseModel):
    # 与 UserEnvVarItem 同一 key 规范，保持前端校验一致。
    key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.\-]+$")


class PrivacyVarOut(BaseModel):
    """列表/详情对外结构：刻意不回显 value（隐私变量值由本地脚本上报，
    平台侧仅存储，不对前端明文展示，避免浏览器/日志侧泄露）。"""
    id: str
    key: str
    has_value: bool
    last_reported_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class PrivacyReportItem(BaseModel):
    """上报单条密文。支持两种加密形式（脚本侧择一）：

    1. 混合加密（推荐，无长度限制）：aes_key_ciphertext + value_ciphertext
       + nonce 三字段同时非空。
    2. 纯 RSA（向后兼容，仅短值可用）：只填 ciphertext 字段。
    """
    key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.\-]+$")
    # 纯 RSA 模式：base64(RSA-OAEP-SHA256 密文)。混合模式留空。
    ciphertext: str = Field(default="", max_length=8192)
    # 混合模式三件套（同时非空时走混合解密）。
    aes_key_ciphertext: str = Field(default="", max_length=8192)
    value_ciphertext: str = Field(default="", max_length=32768)
    nonce: str = Field(default="", max_length=64)


class PrivacyReport(BaseModel):
    items: list[PrivacyReportItem] = Field(default_factory=list, max_length=50)


class ReportTokenOut(BaseModel):
    """上报 token 明文只此一次返回（创建/重置时）。前端展示后由用户复制。"""
    report_token: str
