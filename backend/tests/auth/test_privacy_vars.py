"""用户隐私变量：RSA 公钥加密上报 + 平台私钥解密 + Fernet 双层落库。

覆盖：
- 创建/列表/删除、上限与重复 key 约束、需登录鉴权；
- 首次创建返回 report_token（仅此一次）、后续创建不回显；
- 下载脚本：内容含公钥/token/变量名、可执行 import；
- 上报端点：独立 token 鉴权（不走 JWT）、RSA 加密→平台解密正确、
  双层落库（DB 里是 Fernet 密文）、token 重置后旧 token 失效、
  跨用户 token 不可互用、上报未知 key 拒绝、上报未创建变量拒绝。
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.auth.crypto import (
    decrypt_private_key,
    decrypt_value,
    encrypt_value,
    generate_rsa_keypair,
    hash_query_key,
    rsa_encrypt,
)
from app.auth.models import User, UserPrivacyKeypair, UserPrivacyVar


def _login_headers(client, username: str, password: str) -> dict:
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['data']['access_token']}"}


def _create_var_get_token(client, headers, key="MY_COOKIE"):
    """创建一个隐私变量并拿到首次返回的 report_token。"""
    r = client.post("/api/v1/auth/privacy-vars", json={"key": key}, headers=headers)
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["key"] == key
    assert data["has_value"] is False
    assert "report_token" in data and data["report_token"]
    return data["report_token"]


# ---- 列表 / 创建 / 删除 ----

def test_create_and_list_privacy_var(client, admin_user):
    headers = _login_headers(client, "admin", "admin123")
    token = _create_var_get_token(client, headers, "MY_COOKIE")

    r = client.get("/api/v1/auth/privacy-vars", headers=headers)
    assert r.status_code == 200
    rows = r.json()["data"]
    assert len(rows) == 1
    assert rows[0]["key"] == "MY_COOKIE"
    assert rows[0]["has_value"] is False
    assert rows[0]["last_reported_at"] is None
    # 列表项不回显 value（即便 has_value 也只有布尔标记）
    assert "value" not in rows[0]


def test_second_create_does_not_return_token(client, admin_user):
    headers = _login_headers(client, "admin", "admin123")
    _create_var_get_token(client, headers, "FIRST")
    r = client.post("/api/v1/auth/privacy-vars", json={"key": "SECOND"}, headers=headers)
    assert r.status_code == 201
    # 后续创建不应再回显 report_token（避免无意暴露）
    assert "report_token" not in r.json()["data"]


def test_create_rejects_duplicate_key(client, admin_user):
    headers = _login_headers(client, "admin", "admin123")
    _create_var_get_token(client, headers, "DUP")
    r = client.post("/api/v1/auth/privacy-vars", json={"key": "DUP"}, headers=headers)
    assert r.status_code == 409


def test_create_rejects_invalid_key(client, admin_user):
    headers = _login_headers(client, "admin", "admin123")
    for bad in ("has space", "", "中文名", "a" * 129, "with/slash"):
        r = client.post("/api/v1/auth/privacy-vars", json={"key": bad}, headers=headers)
        assert r.status_code == 422, bad


def test_create_rejects_over_limit(client, admin_user):
    headers = _login_headers(client, "admin", "admin123")
    for i in range(50):
        r = client.post("/api/v1/auth/privacy-vars", json={"key": f"K{i}"}, headers=headers)
        assert r.status_code == 201, i
    r = client.post("/api/v1/auth/privacy-vars", json={"key": "OVER"}, headers=headers)
    assert r.status_code == 400


def test_delete_privacy_var(client, admin_user):
    headers = _login_headers(client, "admin", "admin123")
    _create_var_get_token(client, headers, "TO_DELETE")
    r = client.delete("/api/v1/auth/privacy-vars/TO_DELETE", headers=headers)
    assert r.status_code == 200
    r = client.get("/api/v1/auth/privacy-vars", headers=headers)
    assert all(row["key"] != "TO_DELETE" for row in r.json()["data"])


def test_delete_unknown_returns_404(client, admin_user):
    headers = _login_headers(client, "admin", "admin123")
    r = client.delete("/api/v1/auth/privacy-vars/NOPE", headers=headers)
    assert r.status_code == 404


def test_privacy_vars_scoped_per_user(client, admin_user, editor_user):
    admin_h = _login_headers(client, "admin", "admin123")
    editor_h = _login_headers(client, "editor", "editor123")
    _create_var_get_token(client, admin_h, "ADMIN_ONLY")
    _create_var_get_token(client, editor_h, "EDITOR_ONLY")
    admin_rows = client.get("/api/v1/auth/privacy-vars", headers=admin_h).json()["data"]
    editor_rows = client.get("/api/v1/auth/privacy-vars", headers=editor_h).json()["data"]
    assert {r["key"] for r in admin_rows} == {"ADMIN_ONLY"}
    assert {r["key"] for r in editor_rows} == {"EDITOR_ONLY"}


def test_endpoints_require_auth(client):
    assert client.get("/api/v1/auth/privacy-vars").status_code == 403
    assert client.post("/api/v1/auth/privacy-vars", json={"key": "X"}).status_code == 403
    assert client.delete("/api/v1/auth/privacy-vars/X").status_code == 403
    assert client.post("/api/v1/auth/privacy-vars/report-token/reset").status_code == 403


# ---- 上报 token 重置 ----

def test_reset_report_token(client, admin_user):
    headers = _login_headers(client, "admin", "admin123")
    old = _create_var_get_token(client, headers, "K")
    r = client.post("/api/v1/auth/privacy-vars/report-token/reset", headers=headers)
    assert r.status_code == 200
    new = r.json()["data"]["report_token"]
    assert new and new != old


# ---- 下载脚本 ----

def test_download_script_contains_required_parts(client, admin_user):
    headers = _login_headers(client, "admin", "admin123")
    token = _create_var_get_token(client, headers, "MY_COOKIE")
    r = client.get("/api/v1/auth/privacy-vars/script", headers=headers)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/x-python")
    assert 'filename="privacy_reporter.py"' in r.headers["content-disposition"]
    body = r.text
    # 内嵌了 token、公钥、变量名
    assert token in body
    assert "BEGIN PUBLIC KEY" in body
    assert "MY_COOKIE" in body
    # 采集函数按序号命名（变量名只进 VAR_KEYS 与注释，不充当标识符）
    assert "def collect_0():" in body
    # 生成的脚本是合法 Python（可用 compile 静态校验）
    compile(body, "privacy_reporter.py", "exec")


def test_download_script_works_without_any_var(client, admin_user):
    headers = _login_headers(client, "admin", "admin123")
    # 没有任何隐私变量也应能下载（模板里变量清单为空）
    r = client.get("/api/v1/auth/privacy-vars/script", headers=headers)
    assert r.status_code == 200
    assert "BEGIN PUBLIC KEY" in r.text


def test_download_script_compiles_with_dash_and_dot_keys(client, admin_user):
    """变量名含连字符/点（如 my-cookie、a.b）时脚本仍可运行。

    回归历史缺陷：模板曾把变量名直接拼进 `def collect_<key>()`，这类名字
    会生成无法启动的 Python 脚本；现在函数名只用序号，变量名只进 VAR_KEYS。
    """
    headers = _login_headers(client, "admin", "admin123")
    for key in ("my-cookie", "a.b", "0day_token"):
        r = client.post("/api/v1/auth/privacy-vars", json={"key": key}, headers=headers)
        assert r.status_code == 201, key
    r = client.get("/api/v1/auth/privacy-vars/script", headers=headers)
    assert r.status_code == 200
    body = r.text
    # 原变量名完整保留在脚本里（VAR_KEYS 清单 + 采集函数注释）
    for key in ("my-cookie", "a.b", "0day_token"):
        assert key in body, key
    # 函数名不包含变量名片段（只有序号），且整脚本能通过编译
    assert "collect_my-cookie" not in body
    assert "collect_a.b" not in body
    compile(body, "privacy_reporter.py", "exec")


# ---- 上报端点（独立 token 鉴权） ----

def _report(client, token, items):
    return client.post(
        "/api/v1/auth/privacy-vars/report",
        json={"items": items},
        headers={"X-Report-Token": token},
    )


def test_report_roundtrip_decrypts_and_double_encrypts_at_rest(
    client, admin_user, db: Session,
):
    headers = _login_headers(client, "admin", "admin123")
    token = _create_var_get_token(client, headers, "MY_COOKIE")

    # 用脚本同样的方式：取公钥 → RSA-OAEP 加密 → base64 上报
    kp = db.query(UserPrivacyKeypair).filter(
        UserPrivacyKeypair.user_id == admin_user.id
    ).first()
    public_pem = kp.public_key_pem
    secret = "session-cookie-value-非常机密"
    ct = rsa_encrypt(public_pem, secret)

    r = _report(client, token, [{"key": "MY_COOKIE", "ciphertext": ct}])
    assert r.status_code == 200, r.text
    row = next(x for x in r.json()["data"] if x["key"] == "MY_COOKIE")
    assert row["has_value"] is True
    assert row["last_reported_at"] is not None

    # 落库是双层密文：DB 里的 value_encrypted 经 Fernet 解密后应等于明文
    db_row = db.query(UserPrivacyVar).filter(
        UserPrivacyVar.user_id == admin_user.id,
        UserPrivacyVar.key == "MY_COOKIE",
    ).first()
    assert db_row.value_encrypted
    assert secret not in db_row.value_encrypted  # 不是明文
    assert decrypt_value(db_row.value_encrypted) == secret


def test_report_requires_token(client, admin_user):
    headers = _login_headers(client, "admin", "admin123")
    _create_var_get_token(client, headers, "K")
    # 缺 header → 403
    r = client.post("/api/v1/auth/privacy-vars/report", json={"items": []})
    assert r.status_code == 403
    # 错误 token → 403
    r = _report(client, "wrong-token", [])
    assert r.status_code == 403


def test_report_token_hash_persisted_on_create_and_reset(client, admin_user, db: Session):
    """token 落库为「密文 + sha256 哈希」双列；哈希列是鉴权依据。"""
    headers = _login_headers(client, "admin", "admin123")
    token = _create_var_get_token(client, headers, "K")
    row = db.query(User).filter(User.id == admin_user.id).first()
    assert row.report_token_encrypted  # 密文列保留（供下载脚本解出明文）
    assert token not in row.report_token_encrypted
    assert row.report_token_hash == hash_query_key(token)

    # 重置后哈希列同步更新为新 token
    new = client.post(
        "/api/v1/auth/privacy-vars/report-token/reset", headers=headers
    ).json()["data"]["report_token"]
    db.refresh(row)
    assert row.report_token_hash == hash_query_key(new)
    assert row.report_token_hash != hash_query_key(token)


def test_report_rejects_disabled_user(client, admin_user, db: Session):
    """停用账号的 token 不能再上报（与查询密钥公开端点同一口径，403 防枚举）。"""
    headers = _login_headers(client, "admin", "admin123")
    token = _create_var_get_token(client, headers, "K")
    admin_user.is_active = False
    db.commit()

    r = _report(client, token, [])
    assert r.status_code == 403
    assert r.json()["detail"] == "Invalid report token"

    admin_user.is_active = True
    db.commit()


def test_report_rejects_unknown_var_key(client, admin_user, db: Session):
    headers = _login_headers(client, "admin", "admin123")
    token = _create_var_get_token(client, headers, "KNOWN")
    # 用真实公钥加密一个合法密文，走"密文合法但 key 未知"路径（端点先
    # 解密成功再查 key，未知 key 返回 404）。
    kp = db.query(UserPrivacyKeypair).filter(
        UserPrivacyKeypair.user_id == admin_user.id
    ).first()
    ct = rsa_encrypt(kp.public_key_pem, "v")
    r = _report(client, token, [{"key": "UNKNOWN", "ciphertext": ct}])
    assert r.status_code == 404


def test_report_old_token_invalid_after_reset(client, admin_user):
    headers = _login_headers(client, "admin", "admin123")
    old = _create_var_get_token(client, headers, "K")
    new = client.post(
        "/api/v1/auth/privacy-vars/report-token/reset", headers=headers
    ).json()["data"]["report_token"]
    assert new != old
    # 旧 token 失效
    r = _report(client, old, [])
    assert r.status_code == 403
    # 新 token 可用（空上报也返回 200）
    r = _report(client, new, [])
    assert r.status_code == 200


def test_report_token_cannot_cross_users(client, admin_user, editor_user, db: Session):
    admin_h = _login_headers(client, "admin", "admin123")
    editor_h = _login_headers(client, "editor", "editor123")
    admin_token = _create_var_get_token(client, admin_h, "ADMIN_VAR")
    editor_token = _create_var_get_token(client, editor_h, "EDITOR_VAR")

    # admin 的 token 上报 editor 的变量应被拒（key 不属于该用户）
    kp_admin = db.query(UserPrivacyKeypair).filter(
        UserPrivacyKeypair.user_id == admin_user.id
    ).first()
    ct = rsa_encrypt(kp_admin.public_key_pem, "x")
    r = _report(client, admin_token, [{"key": "EDITOR_VAR", "ciphertext": ct}])
    assert r.status_code == 404


def test_report_rejects_invalid_ciphertext(client, admin_user):
    headers = _login_headers(client, "admin", "admin123")
    token = _create_var_get_token(client, headers, "K")
    r = _report(client, token, [{"key": "K", "ciphertext": "not-valid-base64-or-ciphertext"}])
    assert r.status_code == 400


def test_report_rejects_duplicate_keys_in_payload(client, admin_user, db: Session):
    headers = _login_headers(client, "admin", "admin123")
    token = _create_var_get_token(client, headers, "K")
    kp = db.query(UserPrivacyKeypair).filter(
        UserPrivacyKeypair.user_id == admin_user.id
    ).first()
    ct = rsa_encrypt(kp.public_key_pem, "v")
    r = _report(client, token, [
        {"key": "K", "ciphertext": ct},
        {"key": "K", "ciphertext": ct},
    ])
    assert r.status_code == 400


# ---- 私钥双层加密落库验证 ----

def test_private_key_is_fernet_encrypted_at_rest(client, admin_user, db: Session):
    headers = _login_headers(client, "admin", "admin123")
    _create_var_get_token(client, headers, "K")
    kp = db.query(UserPrivacyKeypair).filter(
        UserPrivacyKeypair.user_id == admin_user.id
    ).first()
    # 私钥落库是 Fernet 密文，不是裸 PEM
    assert "BEGIN PRIVATE KEY" not in kp.private_key_pem_encrypted
    private_pem = decrypt_private_key(kp.private_key_pem_encrypted)
    assert "BEGIN PRIVATE KEY" in private_pem


# ---- 混合加密（RSA + AES-GCM）：长明文无长度限制 ----

def test_report_with_hybrid_encryption_long_cookie(client, admin_user, db: Session):
    """长 Cookie（超 RSA 单块上限 190 字节）走混合加密，平台解密 + 双层落库。"""
    from app.auth.crypto import hybrid_encrypt
    headers = _login_headers(client, "admin", "admin123")
    token = _create_var_get_token(client, headers, "LONG_COOKIE")
    kp = db.query(UserPrivacyKeypair).filter(
        UserPrivacyKeypair.user_id == admin_user.id
    ).first()
    # 构造一个超长的、含大量特殊符号的真实 Cookie 样本
    long_cookie = (
        "env_token=pro; login_uid=8C-AC-46-C2-7B-01-EC; "
        "hwsso_login=V006F5m9oyiU_ar0PDWsIo8iBxViHW6XFMkkJwZ2E9ZOotZrW6GZxPeY"
        "ZLMHauDf2LbfNyKn7MEIHyPc91eXevnJsc0SRhcEiufAUdvwpqWIg5P0A27KDYp7uEJq"
        "lJOwbafhhqOqLdCDbpvyRnT_aqp_bmXATzulVQgvGDhL9J_aIloMq6OeJ8AxR9zexycj"
        "hWqEp_bX56r_byxNs1tnx6P6fcetvTWJTTctDkXqYtDkSH0Mh_alegPGyikUGuctTkhIb"
        "4_bVo6_btL6CvTS8VOkr30cI0TvNb2AZ3P5av8DqmDzygSmkgn03J5WzXh_auGSSPTmH"
        "JitHdZdc_aftZvxzze4PBp8ti4jg_c_c; "
        "X-Auth-Token=89e8edef7ed7dba24109ecf0546b6c9a320e5b1c3231892d; "
        "IAM-Csrf-Token=56018f7b2ecdb13cc58ea43f4bcc1dbc2bc5532bbbdf4d8f;"
    )
    assert len(long_cookie) > 200  # 确实超 RSA 单块上限
    encrypted = hybrid_encrypt(kp.public_key_pem, long_cookie)
    r = _report(client, token, [{"key": "LONG_COOKIE", **encrypted}])
    assert r.status_code == 200, r.text
    row = next(x for x in r.json()["data"] if x["key"] == "LONG_COOKIE")
    assert row["has_value"] is True
    # 落库双层加密：DB 里是 Fernet 密文，解密后等于原始长 Cookie
    db_row = db.query(UserPrivacyVar).filter(
        UserPrivacyVar.user_id == admin_user.id,
        UserPrivacyVar.key == "LONG_COOKIE",
    ).first()
    assert long_cookie not in db_row.value_encrypted
    assert decrypt_value(db_row.value_encrypted) == long_cookie


def test_report_backward_compat_pure_rsa_still_works(client, admin_user, db: Session):
    """向后兼容：旧脚本只填 ciphertext（纯 RSA），仍能解密。"""
    headers = _login_headers(client, "admin", "admin123")
    token = _create_var_get_token(client, headers, "SHORT")
    kp = db.query(UserPrivacyKeypair).filter(
        UserPrivacyKeypair.user_id == admin_user.id
    ).first()
    short_value = "short-cookie"  # 短值，纯 RSA 可加密
    ct = rsa_encrypt(kp.public_key_pem, short_value)
    r = _report(client, token, [{"key": "SHORT", "ciphertext": ct}])
    assert r.status_code == 200
    db_row = db.query(UserPrivacyVar).filter(
        UserPrivacyVar.user_id == admin_user.id,
        UserPrivacyVar.key == "SHORT",
    ).first()
    assert decrypt_value(db_row.value_encrypted) == short_value


# ---- 查看明文值（当前用户 JWT 取回自己的明文） ----

def test_get_value_returns_plaintext_for_reported_var(client, admin_user, db: Session):
    headers = _login_headers(client, "admin", "admin123")
    token = _create_var_get_token(client, headers, "MY_COOKIE")
    secret = "session-cookie-value-非常机密"
    kp = db.query(UserPrivacyKeypair).filter(
        UserPrivacyKeypair.user_id == admin_user.id
    ).first()
    ct = rsa_encrypt(kp.public_key_pem, secret)
    assert _report(client, token, [{"key": "MY_COOKIE", "ciphertext": ct}]).status_code == 200

    r = client.get("/api/v1/auth/privacy-vars/MY_COOKIE/value", headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["key"] == "MY_COOKIE"
    assert data["value"] == secret  # 明文回显，不脱敏
    assert data["last_reported_at"] is not None


def test_get_value_returns_plaintext_for_long_hybrid_value(client, admin_user, db: Session):
    """长明文（混合加密上报）也能正确解密回显。"""
    from app.auth.crypto import hybrid_encrypt
    headers = _login_headers(client, "admin", "admin123")
    token = _create_var_get_token(client, headers, "LONG_COOKIE")
    kp = db.query(UserPrivacyKeypair).filter(
        UserPrivacyKeypair.user_id == admin_user.id
    ).first()
    long_cookie = "x" * 500
    encrypted = hybrid_encrypt(kp.public_key_pem, long_cookie)
    assert _report(client, token, [{"key": "LONG_COOKIE", **encrypted}]).status_code == 200

    r = client.get("/api/v1/auth/privacy-vars/LONG_COOKIE/value", headers=headers)
    assert r.status_code == 200
    assert r.json()["data"]["value"] == long_cookie


def test_get_value_404_when_not_reported(client, admin_user):
    """变量已创建但尚未上报（value_encrypted 为空）→ 404，不返回空明文。"""
    headers = _login_headers(client, "admin", "admin123")
    _create_var_get_token(client, headers, "EMPTY")
    r = client.get("/api/v1/auth/privacy-vars/EMPTY/value", headers=headers)
    assert r.status_code == 404


def test_get_value_404_when_key_unknown(client, admin_user):
    headers = _login_headers(client, "admin", "admin123")
    r = client.get("/api/v1/auth/privacy-vars/NOPE/value", headers=headers)
    assert r.status_code == 404


def test_get_value_cannot_cross_users(client, admin_user, editor_user, db: Session):
    """A 拿不到 B 的变量明文；跨用户对不存在的 key 一律 404，不泄漏存在性。"""
    admin_h = _login_headers(client, "admin", "admin123")
    editor_h = _login_headers(client, "editor", "editor123")
    editor_token = _create_var_get_token(client, editor_h, "EDITOR_SECRET")
    kp = db.query(UserPrivacyKeypair).filter(
        UserPrivacyKeypair.user_id == editor_user.id
    ).first()
    secret = "editor-only-value"
    ct = rsa_encrypt(kp.public_key_pem, secret)
    assert _report(client, editor_token, [{"key": "EDITOR_SECRET", "ciphertext": ct}]).status_code == 200

    # admin 用自己的 JWT 去取 editor 的变量 → 404（对 admin 而言该 key 不存在）
    r = client.get("/api/v1/auth/privacy-vars/EDITOR_SECRET/value", headers=admin_h)
    assert r.status_code == 404


def test_get_value_requires_auth(client):
    assert client.get("/api/v1/auth/privacy-vars/K/value").status_code == 403


