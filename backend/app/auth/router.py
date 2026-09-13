from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Header, Response
from sqlalchemy.orm import Session
from app.deps import get_db, get_current_user
from app.config import settings
from app.auth.schemas import (
    LoginRequest,
    PasswordChangeRequest,
    PrivacyReport,
    PrivacyVarCreate,
    ProfileUpdate,
    TokenResponse,
    UserEnvVarsReplace,
    UserOut,
)
from app.auth.service import authenticate_user, create_access_token, hash_password, verify_password
from app.auth.models import User, UserEnvVar, UserPrivacyKeypair, UserPrivacyVar
from app.auth.permissions import get_role_menu_keys
from app.auth.crypto import (
    decrypt_private_key,
    decrypt_value,
    encrypt_value,
    generate_report_token,
    generate_rsa_keypair,
    hybrid_decrypt,
    rsa_decrypt,
)

router = APIRouter()

@router.post("/login")
def login(body: LoginRequest, db: Session = Depends(get_db)):
    user = authenticate_user(db, body.username, body.password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = create_access_token(
        {"sub": user.id, "role": user.role, "ver": user.token_version})
    return {"data": {"access_token": token, "token_type": "bearer"}, "message": "ok"}

@router.get("/profile")
def profile(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    data = UserOut.model_validate(current_user).model_dump()
    data["menu_permissions"] = get_role_menu_keys(db, current_user.role)
    return {"data": data, "message": "ok"}

@router.put("/password")
def change_password(body: PasswordChangeRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if not verify_password(body.current_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="Current password incorrect")
    current_user.password_hash = hash_password(body.new_password)
    # 改密即吊销全部已签发 token（token_version 会话吊销）
    current_user.token_version = (current_user.token_version or 0) + 1
    db.commit()
    return {"message": "Password updated"}

# 个人资料自助更新（MYW-56）：用户名是账号唯一标识，不允许自改；这里只
# 开放邮箱。响应结构与 GET /profile 一致，前端可直接用返回值刷新登录态。

@router.put("/profile")
def update_profile(
    body: ProfileUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    duplicate = db.query(User).filter(User.email == body.email, User.id != current_user.id).first()
    if duplicate:
        raise HTTPException(status_code=409, detail="Email already exists")
    current_user.email = body.email
    db.commit()
    db.refresh(current_user)
    data = UserOut.model_validate(current_user).model_dump()
    data["menu_permissions"] = get_role_menu_keys(db, current_user.role)
    return {"data": data, "message": "ok"}


# 用户私有环境变量（MYW-56）：仅本人可见可改，value 加密落库。PUT 为全量
# 保存语义——请求中的列表即该用户的完整变量集（条数上限等约束在 schema 层）。

def _env_var_out(row: UserEnvVar) -> dict:
    return {"key": row.key, "value": decrypt_value(row.value_encrypted)}


@router.get("/env-vars")
def list_env_vars(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    rows = (
        db.query(UserEnvVar)
        .filter(UserEnvVar.user_id == current_user.id)
        .order_by(UserEnvVar.key)
        .all()
    )
    return {"data": [_env_var_out(row) for row in rows], "message": "ok"}

@router.put("/env-vars")
def replace_env_vars(
    body: UserEnvVarsReplace,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    seen: set[str] = set()
    for item in body.items:
        if item.key in seen:
            raise HTTPException(status_code=400, detail=f"Duplicate env var key: {item.key}")
        seen.add(item.key)

    db.query(UserEnvVar).filter(UserEnvVar.user_id == current_user.id).delete(synchronize_session=False)
    for item in body.items:
        db.add(UserEnvVar(
            user_id=current_user.id,
            key=item.key,
            value_encrypted=encrypt_value(item.value),
        ))
    db.commit()

    rows = (
        db.query(UserEnvVar)
        .filter(UserEnvVar.user_id == current_user.id)
        .order_by(UserEnvVar.key)
        .all()
    )
    return {"data": [_env_var_out(row) for row in rows], "message": "ok"}


# --------------------------------------------------------------------------
# 用户隐私变量：本地脚本用公钥 RSA 加密上报，平台私钥解密后再 Fernet 落库。
# report 端点是唯一绕开 get_current_user 的端点，走独立上报 token 鉴权。
# --------------------------------------------------------------------------

PRIVACY_VAR_MAX_ITEMS = 50
# RSA-2048 + OAEP-SHA256 单块明文上限约 190 字节，足够覆盖典型 Cookie；
# 超长值由脚本侧分段或缩短，平台不做拼接（保持链路简单）。
_REPORT_TOKEN_HEADER = "X-Report-Token"


def _ensure_keypair(db: Session, user: User) -> UserPrivacyKeypair:
    """按需为用户生成 RSA 密钥对（已存在则原样返回）。"""
    existing = db.query(UserPrivacyKeypair).filter(
        UserPrivacyKeypair.user_id == user.id
    ).first()
    if existing:
        return existing
    public_pem, private_pem = generate_rsa_keypair()
    kp = UserPrivacyKeypair(
        user_id=user.id,
        public_key_pem=public_pem,
        private_key_pem_encrypted=encrypt_value(private_pem),
    )
    db.add(kp)
    db.flush()
    return kp


def _ensure_report_token(db: Session, user: User) -> str:
    """按需为用户生成上报 token（已存在则不重置，返回空串表示未生成）。

    明文只在"首次创建"时返回；已存在 token 的后续调用返回空串，避免无意
    暴露。重置走 reset 端点。
    """
    if user.report_token_encrypted:
        return ""
    token = generate_report_token()
    user.report_token_encrypted = encrypt_value(token)
    return token


def _privacy_var_out(row: UserPrivacyVar) -> dict:
    return {
        "id": row.id,
        "key": row.key,
        "has_value": bool(row.value_encrypted),
        "last_reported_at": row.last_reported_at,
        "created_at": row.created_at,
    }


@router.get("/privacy-vars")
def list_privacy_vars(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    rows = (
        db.query(UserPrivacyVar)
        .filter(UserPrivacyVar.user_id == current_user.id)
        .order_by(UserPrivacyVar.key)
        .all()
    )
    return {"data": [_privacy_var_out(row) for row in rows], "message": "ok"}


@router.post("/privacy-vars", status_code=201)
def create_privacy_var(
    body: PrivacyVarCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    count = db.query(UserPrivacyVar).filter(
        UserPrivacyVar.user_id == current_user.id
    ).count()
    if count >= PRIVACY_VAR_MAX_ITEMS:
        raise HTTPException(
            status_code=400,
            detail=f"Privacy vars limit reached ({PRIVACY_VAR_MAX_ITEMS})",
        )
    existing = db.query(UserPrivacyVar).filter(
        UserPrivacyVar.user_id == current_user.id,
        UserPrivacyVar.key == body.key,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Privacy var key already exists: {body.key}")

    # 首个隐私变量：按需生成密钥对与上报 token（token 仅此一次返回）。
    _ensure_keypair(db, current_user)
    token_plain = _ensure_report_token(db, current_user)

    var = UserPrivacyVar(
        user_id=current_user.id,
        key=body.key,
        value_encrypted="",
    )
    db.add(var)
    db.commit()
    db.refresh(var)

    data = _privacy_var_out(var)
    if token_plain:
        data["report_token"] = token_plain
    return {"data": data, "message": "ok"}


@router.get("/privacy-vars/{key}/value")
def get_privacy_var_value(
    key: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 仅当前用户可取回自己的明文值；跨用户对不存在的 key 一律返回 404，不泄漏存在性。
    row = db.query(UserPrivacyVar).filter(
        UserPrivacyVar.user_id == current_user.id,
        UserPrivacyVar.key == key,
    ).first()
    if not row or not row.value_encrypted:
        raise HTTPException(status_code=404, detail="Privacy var not found")
    return {
        "data": {
            "key": row.key,
            "value": decrypt_value(row.value_encrypted),
            "last_reported_at": row.last_reported_at,
        },
        "message": "ok",
    }


@router.delete("/privacy-vars/{key}")
def delete_privacy_var(
    key: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = db.query(UserPrivacyVar).filter(
        UserPrivacyVar.user_id == current_user.id,
        UserPrivacyVar.key == key,
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Privacy var not found")
    db.delete(row)
    db.commit()
    return {"message": "deleted"}


@router.post("/privacy-vars/report-token/reset")
def reset_report_token(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """重置上报 token（旧 token 立即失效）。明文仅此一次返回。"""
    token = generate_report_token()
    current_user.report_token_encrypted = encrypt_value(token)
    db.commit()
    return {"data": {"report_token": token}, "message": "ok"}


@router.get("/privacy-vars/script")
def download_reporter_script(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """生成并下发 Python 上报脚本模板（含公钥、上报 token、当前变量名清单）。

    下载交互依赖浏览器副作用，按 AGENTS.md §5 副作用验收标准：前端 E2E
    必须断言下载文件内容，不能只断言"提示出现"。
    """
    _ensure_keypair(db, current_user)
    if not current_user.report_token_encrypted:
        token = generate_report_token()
        current_user.report_token_encrypted = encrypt_value(token)
        db.commit()
    else:
        token = decrypt_value(current_user.report_token_encrypted)

    kp = db.query(UserPrivacyKeypair).filter(
        UserPrivacyKeypair.user_id == current_user.id
    ).first()
    vars_rows = (
        db.query(UserPrivacyVar)
        .filter(UserPrivacyVar.user_id == current_user.id)
        .order_by(UserPrivacyVar.key)
        .all()
    )
    var_keys = [r.key for r in vars_rows]

    # 平台后端无法可靠获知自己对外暴露的公网地址（穿过 Nginx/域名/端口
    # 转发后，request.base_url 是容器内部地址）。用配置里已有的"公开 API
    # 地址"字段作默认值；为空时模板里 BASE_URL 留空，用户需自行填写。
    script = _build_reporter_script(
        base_url=getattr(settings, "pipeline_file_public_api_base_url", "") or "",
        report_token=token,
        public_key_pem=kp.public_key_pem,
        var_keys=var_keys,
    )
    return Response(
        content=script,
        media_type="text/x-python",
        headers={"Content-Disposition": 'attachment; filename="privacy_reporter.py"'},
    )


def _build_reporter_script(
    *, base_url: str, report_token: str, public_key_pem: str, var_keys: list[str]
) -> str:
    """生成 Python 上报脚本模板。用户填入 collect_<key>() 采集逻辑后即可运行。"""
    keys_block = ", ".join(repr(k) for k in var_keys) if var_keys else ""
    if var_keys:
        collect_funcs = "\n\n".join(
            f"def collect_{k}():\n"
            f"    # TODO: 在此填入采集 {k} 的本地逻辑（如读取本地 Cookie/凭据）\n"
            f"    # 返回字符串值；若当前无值返回 None 则本次跳过该变量。\n"
            f"    return None"
            for k in var_keys
        )
    else:
        collect_funcs = "def collect_PLACEHOLDER():\n    return None"
    return _REPORTER_TEMPLATE.format(
        BASE_URL=repr(base_url),
        REPORT_TOKEN=repr(report_token),
        PUBLIC_KEY_PEM=public_key_pem,
        VAR_KEYS=keys_block,
        COLLECT_FUNCS=collect_funcs,
    )


_REPORTER_TEMPLATE = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenOntology 隐私变量上报脚本（由平台自动生成）。

用法：
  1. 本脚本已内嵌：平台地址 BASE_URL、上报 token REPORT_TOKEN、
     公钥 PUBLIC_KEY_PEM、当前变量名清单 VAR_KEYS。
  2. 在 collect_<key>() 函数里填入本地采集逻辑，返回字符串值。
  3. 运行：python privacy_reporter.py
  4. 脚本会按 INTERVAL 间隔周期性把每个变量的值用公钥加密后上报平台。
     平台用对应私钥解密后落库；本脚本不含任何私钥，泄露只有公钥+token。
     token 泄露可在平台「个人资料 → 隐私变量」重置使旧 token 失效。

依赖：cryptography（pip install cryptography）。
"""
import base64
import time
import sys

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
except ImportError:
    sys.stderr.write("缺少 cryptography 依赖，请先安装：pip install cryptography\n")
    sys.exit(1)

BASE_URL = {BASE_URL}
REPORT_TOKEN = {REPORT_TOKEN}
PUBLIC_KEY_PEM = {PUBLIC_KEY_PEM!r}
VAR_KEYS = [{VAR_KEYS}]

# 上报间隔（秒），用户可按需调整。默认 300s = 5 分钟。
INTERVAL = 300

# 采集函数：平台依据当前已创建的变量名生成占位实现，用户自行填写。
{COLLECT_FUNCS}


def _encrypt(public_key_pem: str, plaintext: str) -> dict:
    """混合加密：RSA 加密随机 AES 密钥，AES-GCM 加密明文。无长度限制。"""
    import os
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    aes_key = os.urandom(32)
    nonce = os.urandom(12)
    value_ct = AESGCM(aes_key).encrypt(nonce, plaintext.encode(), None)
    pub = serialization.load_pem_public_key(public_key_pem.encode())
    aes_key_ct = pub.encrypt(
        aes_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return {{
        "aes_key_ciphertext": base64.b64encode(aes_key_ct).decode(),
        "value_ciphertext": base64.b64encode(value_ct).decode(),
        "nonce": base64.b64encode(nonce).decode(),
    }}


def _report_once():
    import urllib.request
    import json
    items = []
    for key in VAR_KEYS:
        fn = globals().get(f"collect_{{key}}")
        if fn is None:
            continue
        try:
            value = fn()
        except Exception as exc:  # 采集失败不影响其他变量上报
            sys.stderr.write(f"collect_{{key}} 失败: {{exc}}\n")
            continue
        if value is None:
            continue
        if not isinstance(value, str):
            value = str(value)
        item = {{"key": key}}
        item.update(_encrypt(PUBLIC_KEY_PEM, value))
        items.append(item)
    if not items:
        sys.stderr.write("本次无可上报值\n")
        return
    payload = json.dumps({{"items": items}}).encode()
    req = urllib.request.Request(
        f"{{BASE_URL}}/api/v1/auth/privacy-vars/report",
        data=payload,
        headers={{
            "Content-Type": "application/json",
            "X-Report-Token": REPORT_TOKEN,
        }},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            sys.stdout.write(f"上报成功: {{resp.status}}\n")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        sys.stderr.write(f"上报失败 HTTP {{exc.code}}: {{body}}\n")
    except Exception as exc:
        sys.stderr.write(f"上报异常: {{exc}}\n")


def main():
    if not BASE_URL or not REPORT_TOKEN or not PUBLIC_KEY_PEM:
        sys.stderr.write("脚本配置不完整（BASE_URL/REPORT_TOKEN/PUBLIC_KEY_PEM 缺失）\n")
        sys.exit(1)
    sys.stdout.write("隐私变量上报脚本已启动，按 Ctrl+C 退出\n")
    while True:
        _report_once()
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
'''


@router.post("/privacy-vars/report")
def report_privacy_vars(
    body: PrivacyReport,
    x_report_token: str = Header(default="", alias=_REPORT_TOKEN_HEADER),
    db: Session = Depends(get_db),
):
    """上报端点：独立 token 鉴权，不走 get_current_user。

    鉴权链路：X-Report-Token 明文 → 与 users.report_token_encrypted 解密
    后比对 → 命中用户的私钥解密上报密文 → 明文再 Fernet 加密落库。
    """
    if not x_report_token:
        raise HTTPException(status_code=403, detail="Missing report token")
    users = db.query(User).filter(User.report_token_encrypted.isnot(None)).all()
    user = None
    for u in users:
        if decrypt_value(u.report_token_encrypted) == x_report_token:
            user = u
            break
    if not user:
        raise HTTPException(status_code=403, detail="Invalid report token")

    kp = db.query(UserPrivacyKeypair).filter(
        UserPrivacyKeypair.user_id == user.id
    ).first()
    if not kp:
        raise HTTPException(status_code=400, detail="No keypair; create a privacy var first")

    private_pem = decrypt_private_key(kp.private_key_pem_encrypted)

    seen: set[str] = set()
    for item in body.items:
        if item.key in seen:
            raise HTTPException(status_code=400, detail=f"Duplicate privacy var key: {item.key}")
        seen.add(item.key)

    now = datetime.now(timezone.utc)
    for item in body.items:
        try:
            # 优先混合加密（无长度限制）；三件套齐全时走 hybrid_decrypt。
            # 否则回退纯 RSA（向后兼容旧脚本，仅短值可用）。
            if item.aes_key_ciphertext and item.value_ciphertext and item.nonce:
                plaintext = hybrid_decrypt(private_pem, {
                    "aes_key_ciphertext": item.aes_key_ciphertext,
                    "value_ciphertext": item.value_ciphertext,
                    "nonce": item.nonce,
                })
            else:
                plaintext = rsa_decrypt(private_pem, item.ciphertext)
        except Exception:
            raise HTTPException(status_code=400, detail=f"Failed to decrypt value for key: {item.key}")
        row = db.query(UserPrivacyVar).filter(
            UserPrivacyVar.user_id == user.id,
            UserPrivacyVar.key == item.key,
        ).first()
        if not row:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown privacy var key: {item.key}",
            )
        # 双层保险：RSA 解密后的明文再 Fernet 加密落库。
        row.value_encrypted = encrypt_value(plaintext)
        row.last_reported_at = now
    db.commit()

    rows = (
        db.query(UserPrivacyVar)
        .filter(UserPrivacyVar.user_id == user.id)
        .order_by(UserPrivacyVar.key)
        .all()
    )
    return {"data": [_privacy_var_out(row) for row in rows], "message": "ok"}
