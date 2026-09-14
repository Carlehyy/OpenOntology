"""Auth 域私有的加密薄封装。

架构边界（tests/architecture/test_foundation_dependency_direction.py）要求
auth 不得依赖 app.settings / app.shared 兼容门面；与该域持有 passlib/jose
封装的方式一致，这里直接使用 cryptography 库并从 canonical 的 app.config
settings 取密钥材料。

密钥派生逻辑与 app.shared.encryption 保持一致：优先显式 ENCRYPTION_KEY，
否则由 SECRET_KEY 派生。两处实现因此产出同一把 Fernet 密钥，密文互通。

本模块提供两层加密能力：
- 对称层（Fernet）：用于用户私有环境变量值、隐私变量上报 token、以及
  隐私变量 RSA 私钥的"再包一层"落库（双层保险：DB 拖库时私钥仍不可读）。
- 非对称层（RSA-OAEP）：用于隐私变量上报链路。用户本地脚本只持有公钥
  （只能加密），平台持有私钥（唯一能解密）。私钥本身再用 Fernet 包一层
  后落库，DB 泄露且平台 Fernet 密钥未泄露时，上报历史与未来数据仍安全。
"""

import base64
import hashlib
import secrets

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


from app.config import settings


def _get_fernet() -> Fernet:
    key = settings.encryption_key
    if not key:
        raw = hashlib.sha256(settings.secret_key.encode()).digest()
        key = base64.urlsafe_b64encode(raw).decode()
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_value(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    return _get_fernet().decrypt(ciphertext.encode()).decode()


# ---- 隐私变量上报 token ----

def generate_report_token() -> str:
    """生成一个上报 token（足够长的随机 URL-safe 字符串）。

    落库前用 encrypt_value（Fernet）包一层；明文只在创建/重置时返回一次。
    """
    return secrets.token_urlsafe(32)


# ---- 变量查询密钥（公开查询端点凭据） ----

def generate_query_key(category: str) -> str:
    """生成查询密钥明文：类别前缀 + 高熵随机段（token_urlsafe(32)=256 位熵）。

    前缀让密钥肉眼可辨类别（obk_env_* / obk_priv_*），便于用户在 n8n 等
    外部系统中配置时自查用错类别。校验走 sha256 查表（hash_query_key），
    不落明文、不做全表解密比对。
    """
    prefix = "obk_env_" if category == "env" else "obk_priv_"
    return prefix + secrets.token_urlsafe(32)


def hash_query_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


# ---- RSA 非对称层（隐私变量上报） ----

# RSA-OAEP 的明文上限取决于密钥尺寸与哈希。2048 位密钥 + SHA-256 时
# 单次加密上限为 190 字节，足够覆盖绝大多数 Cookie/小型凭据值；超长值
# 由调用方分段或自行缩短，本期不在平台侧做分段拼接（保持链路简单）。
_RSA_KEY_SIZE = 2048


def generate_rsa_keypair() -> tuple[str, str]:
    """生成一对 RSA 密钥，返回 (public_pem, private_pem) 明文字符串。

    private_pem 在落库前由调用方再调用 encrypt_value 包一层 Fernet。
    """
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=_RSA_KEY_SIZE,
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return public_pem, private_pem


def _load_public_key(public_pem: str):
    return serialization.load_pem_public_key(public_pem.encode())


def _load_private_key(private_pem: str):
    return serialization.load_pem_private_key(private_pem.encode(), password=None)


def rsa_encrypt(public_pem: str, plaintext: str) -> str:
    """用公钥加密，返回 base64 编码的密文（适合 JSON 传输）。"""
    if not plaintext:
        return ""
    pub = _load_public_key(public_pem)
    ciphertext = pub.encrypt(
        plaintext.encode(),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return base64.b64encode(ciphertext).decode()


def rsa_decrypt(private_pem: str, ciphertext_b64: str) -> str:
    """用私钥解密 base64 编码的密文。空串原样返回。"""
    if not ciphertext_b64:
        return ""
    priv = _load_private_key(private_pem)
    ciphertext = base64.b64decode(ciphertext_b64)
    plaintext = priv.decrypt(
        ciphertext,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return plaintext.decode()


def decrypt_private_key(private_key_pem_encrypted: str) -> str:
    """从 Fernet 密文还原出 RSA 私钥 PEM 明文（仅供平台侧解密上报值用）。"""
    return decrypt_value(private_key_pem_encrypted)


# ---- 混合加密（RSA + AES-GCM）：无明文长度限制 ----
#
# 纯 RSA-OAEP 单块明文有上限（RSA-2048 + SHA256 约 190 字节），而典型
# Cookie 常达数百字符。混合加密用 RSA 只加密一把随机 AES 密钥，明文本身
# 用 AES-GCM 加密，从而无长度限制，且脚本侧仍只持有公钥（无私钥）。
#
# 上报契约（JSON 字段）：
#   aes_key_ciphertext: base64(RSA-OAEP-SHA256 加密的 AES-256 密钥，32 字节)
#   value_ciphertext:   base64(AES-GCM 密文)
#   nonce:              base64(AES-GCM 12 字节 nonce)
# 老的纯 RSA ciphertext 字段仍向后兼容（短值可单块加密直接走 ciphertext）。

import os as _os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def hybrid_encrypt(public_pem: str, plaintext: str) -> dict:
    """混合加密：返回 {aes_key_ciphertext, value_ciphertext, nonce}（均 base64）。

    供上报脚本调用：生成随机 AES-256 密钥，用平台公钥 RSA 加密该密钥，
    用 AES-GCM 加密明文。脚本侧无任何私钥，泄露只有公钥+一次性 AES 密钥。
    """
    if not plaintext:
        return {"aes_key_ciphertext": "", "value_ciphertext": "", "nonce": ""}
    aes_key = _os.urandom(32)
    nonce = _os.urandom(12)
    aesgcm = AESGCM(aes_key)
    value_ct = aesgcm.encrypt(nonce, plaintext.encode(), None)
    pub = _load_public_key(public_pem)
    aes_key_ct = pub.encrypt(
        aes_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return {
        "aes_key_ciphertext": base64.b64encode(aes_key_ct).decode(),
        "value_ciphertext": base64.b64encode(value_ct).decode(),
        "nonce": base64.b64encode(nonce).decode(),
    }


def hybrid_decrypt(private_pem: str, payload: dict) -> str:
    """混合解密：从 {aes_key_ciphertext, value_ciphertext, nonce} 还原明文。

    平台侧用私钥 RSA 解出 AES 密钥，再用 AES-GCM 解密明文。
    payload 任一字段为空时原样返回空串（兼容空值上报）。
    """
    aes_key_ct = payload.get("aes_key_ciphertext", "")
    value_ct = payload.get("value_ciphertext", "")
    nonce = payload.get("nonce", "")
    if not aes_key_ct or not value_ct or not nonce:
        return ""
    priv = _load_private_key(private_pem)
    aes_key = priv.decrypt(
        base64.b64decode(aes_key_ct),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    aesgcm = AESGCM(aes_key)
    plaintext = aesgcm.decrypt(base64.b64decode(nonce), base64.b64decode(value_ct), None)
    return plaintext.decode()
