"""用户变量查询密钥（PAT 式）：跟用户、分类别（env/privacy）、多把并存。

覆盖：
- 管理端点：创建（六档有效期、明文仅此一次、前缀可辨类别）、列表（不回
  明文、类别过滤）、吊销（软删、重复吊销 404）、需登录鉴权、按用户隔离、
  每类别有效密钥配额；
- 公开查询端点（/api/public/env-vars、/api/public/privacy-vars）：有效
  密钥返回该用户该类别全部变量明文（隐私变量未上报时 value=null）、
  类别错配 403、无效/缺失密钥 401、吊销/过期/停用用户 401、跨用户隔离、
  last_used_at 更新、落库仅存 sha256 哈希。
"""

from datetime import datetime, timedelta, timezone

from app.auth.crypto import encrypt_value, hash_query_key
from app.auth.models import UserEnvVar, UserPrivacyVar, UserQueryKey


def _login_headers(client, username: str, password: str) -> dict:
    r = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['data']['access_token']}"}


def _create_key(client, headers, category="env", name="n8n", validity="365d"):
    r = client.post(
        "/api/v1/auth/query-keys",
        json={"category": category, "name": name, "validity": validity},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]


def _put_env_vars(client, headers, items):
    r = client.put("/api/v1/auth/env-vars", json={"items": items}, headers=headers)
    assert r.status_code == 200, r.text


# ---- 管理端点 ----

def test_create_key_returns_plaintext_once(client, admin_user, db):
    headers = _login_headers(client, "admin", "admin123")
    data = _create_key(client, headers, category="env", name="n8n 流水线")

    assert data["key"].startswith("obk_env_")
    assert len(data["key"]) > len("obk_env_")
    assert data["name"] == "n8n 流水线"
    assert data["category"] == "env"
    assert data["key_prefix"] == data["key"][:16]
    assert data["expires_at"] is not None
    assert data["revoked_at"] is None

    # 明文仅此一次：列表不再回显；落库只存 sha256 哈希，查表可命中。
    listed = client.get("/api/v1/auth/query-keys", headers=headers).json()["data"]
    assert len(listed) == 1
    assert "key" not in listed[0]
    row = db.query(UserQueryKey).filter(UserQueryKey.id == data["id"]).one()
    assert row.key_hash == hash_query_key(data["key"])


def test_create_privacy_key_prefix_and_permanent(client, admin_user, db):
    headers = _login_headers(client, "admin", "admin123")
    data = _create_key(client, headers, category="privacy", validity="permanent")
    assert data["key"].startswith("obk_priv_")
    assert data["expires_at"] is None

    # 1d 档：过期时间约在 now+1d（容忍存储往返误差；SQLite 回读 naive 补 UTC）。
    short = _create_key(client, headers, category="env", validity="1d")
    expires = datetime.fromisoformat(short["expires_at"].replace("Z", "+00:00"))
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    delta = expires - datetime.now(timezone.utc)
    assert timedelta(hours=23) < delta < timedelta(hours=25)


def test_list_category_filter(client, admin_user):
    headers = _login_headers(client, "admin", "admin123")
    _create_key(client, headers, category="env")
    _create_key(client, headers, category="privacy")

    all_rows = client.get("/api/v1/auth/query-keys", headers=headers).json()["data"]
    assert {r["category"] for r in all_rows} == {"env", "privacy"}
    env_rows = client.get("/api/v1/auth/query-keys?category=env", headers=headers).json()["data"]
    assert {r["category"] for r in env_rows} == {"env"}


def test_revoke_key(client, admin_user):
    headers = _login_headers(client, "admin", "admin123")
    data = _create_key(client, headers)

    r = client.delete(f"/api/v1/auth/query-keys/{data['id']}", headers=headers)
    assert r.status_code == 200
    listed = client.get("/api/v1/auth/query-keys", headers=headers).json()["data"]
    assert listed[0]["revoked_at"] is not None
    # 重复吊销与未知 id 一律 404。
    assert client.delete(f"/api/v1/auth/query-keys/{data['id']}", headers=headers).status_code == 404
    assert client.delete("/api/v1/auth/query-keys/nope", headers=headers).status_code == 404


def test_active_keys_per_category_cap(client, admin_user):
    headers = _login_headers(client, "admin", "admin123")
    for i in range(20):
        assert client.post(
            "/api/v1/auth/query-keys",
            json={"category": "env", "name": f"k{i}", "validity": "365d"},
            headers=headers,
        ).status_code == 201
    r = client.post(
        "/api/v1/auth/query-keys",
        json={"category": "env", "name": "over", "validity": "365d"},
        headers=headers,
    )
    assert r.status_code == 400
    # privacy 类别不受 env 配额影响。
    assert client.post(
        "/api/v1/auth/query-keys",
        json={"category": "privacy", "name": "p", "validity": "365d"},
        headers=headers,
    ).status_code == 201
    # 吊销一把后配额释放。
    listed = client.get("/api/v1/auth/query-keys?category=env", headers=headers).json()["data"]
    client.delete(f"/api/v1/auth/query-keys/{listed[0]['id']}", headers=headers)
    assert client.post(
        "/api/v1/auth/query-keys",
        json={"category": "env", "name": "again", "validity": "365d"},
        headers=headers,
    ).status_code == 201


def test_management_requires_auth(client):
    assert client.get("/api/v1/auth/query-keys").status_code == 403
    assert client.post("/api/v1/auth/query-keys", json={"category": "env"}).status_code == 403
    assert client.delete("/api/v1/auth/query-keys/x").status_code == 403


def test_management_scoped_per_user(client, admin_user, editor_user):
    admin_h = _login_headers(client, "admin", "admin123")
    editor_h = _login_headers(client, "editor", "editor123")
    admin_key = _create_key(client, admin_h, category="env", name="admin 的")

    editor_rows = client.get("/api/v1/auth/query-keys", headers=editor_h).json()["data"]
    assert editor_rows == []
    # editor 不能吊销 admin 的密钥（跨用户按不存在处理）。
    assert client.delete(
        f"/api/v1/auth/query-keys/{admin_key['id']}", headers=editor_h
    ).status_code == 404


# ---- 公开查询端点 ----

def test_public_env_vars_with_key(client, admin_user, db):
    headers = _login_headers(client, "admin", "admin123")
    _put_env_vars(client, headers, [
        {"key": "API_BASE", "value": "https://example.com"},
        {"key": "TIMEOUT", "value": "30"},
    ])
    key = _create_key(client, headers, category="env")

    r = client.get("/api/public/env-vars", headers={"Authorization": f"Bearer {key['key']}"})
    assert r.status_code == 200, r.text
    # 无信封契约：流水线直接消费 items。
    assert set(r.json().keys()) == {"items"}
    assert r.json()["items"] == [
        {"key": "API_BASE", "value": "https://example.com"},
        {"key": "TIMEOUT", "value": "30"},
    ]

    # 命中后 last_used_at 更新。
    row = db.query(UserQueryKey).filter(UserQueryKey.id == key["id"]).one()
    assert row.last_used_at is not None


def test_public_privacy_vars_null_when_unreported(client, admin_user, db):
    headers = _login_headers(client, "admin", "admin123")
    r = client.post("/api/v1/auth/privacy-vars", json={"key": "REPORTED"}, headers=headers)
    assert r.status_code == 201
    r = client.post("/api/v1/auth/privacy-vars", json={"key": "PENDING"}, headers=headers)
    assert r.status_code == 201
    # 直接落一条已上报值（绕开 RSA 上报链路，公开端点只关心读路径）。
    row = db.query(UserPrivacyVar).filter(UserPrivacyVar.key == "REPORTED").one()
    row.value_encrypted = encrypt_value("cookie-value")
    row.last_reported_at = datetime.now(timezone.utc)
    db.commit()

    key = _create_key(client, headers, category="privacy")
    r = client.get("/api/public/privacy-vars", headers={"Authorization": f"Bearer {key['key']}"})
    assert r.status_code == 200, r.text
    items = {item["key"]: item for item in r.json()["items"]}
    assert items["REPORTED"]["value"] == "cookie-value"
    assert items["REPORTED"]["last_reported_at"] is not None
    assert items["PENDING"]["value"] is None


def test_public_category_mismatch_rejected(client, admin_user):
    headers = _login_headers(client, "admin", "admin123")
    env_key = _create_key(client, headers, category="env")
    privacy_key = _create_key(client, headers, category="privacy")

    r = client.get("/api/public/privacy-vars", headers={"Authorization": f"Bearer {env_key['key']}"})
    assert r.status_code == 403
    r = client.get("/api/public/env-vars", headers={"Authorization": f"Bearer {privacy_key['key']}"})
    assert r.status_code == 403


def test_public_invalid_or_missing_key(client, admin_user):
    _login_headers(client, "admin", "admin123")
    assert client.get("/api/public/env-vars").status_code == 401
    assert client.get(
        "/api/public/env-vars", headers={"Authorization": "Bearer obk_env_nope"}
    ).status_code == 401
    # 无效与缺失统一响应体，防枚举。
    r1 = client.get("/api/public/env-vars")
    r2 = client.get("/api/public/env-vars", headers={"Authorization": "Bearer obk_env_nope"})
    assert r1.json() == r2.json()


def test_public_revoked_key_rejected(client, admin_user):
    headers = _login_headers(client, "admin", "admin123")
    key = _create_key(client, headers, category="env")
    assert client.delete(f"/api/v1/auth/query-keys/{key['id']}", headers=headers).status_code == 200
    r = client.get("/api/public/env-vars", headers={"Authorization": f"Bearer {key['key']}"})
    assert r.status_code == 401


def test_public_expired_key_rejected(client, admin_user, db):
    headers = _login_headers(client, "admin", "admin123")
    key = _create_key(client, headers, category="env", validity="1d")
    row = db.query(UserQueryKey).filter(UserQueryKey.id == key["id"]).one()
    row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()
    r = client.get("/api/public/env-vars", headers={"Authorization": f"Bearer {key['key']}"})
    assert r.status_code == 401


def test_public_deactivated_user_rejected(client, admin_user, db):
    headers = _login_headers(client, "admin", "admin123")
    key = _create_key(client, headers, category="env")
    admin_user.is_active = False
    db.commit()
    r = client.get("/api/public/env-vars", headers={"Authorization": f"Bearer {key['key']}"})
    assert r.status_code == 401


def test_password_change_revokes_query_keys(client, admin_user):
    """改密是账号失守后的止损动作：token_version 吊销 JWT 的同时，
    全部查询密钥（含永久档）一并吊销，公开端点立即 401。"""
    headers = _login_headers(client, "admin", "admin123")
    key = _create_key(client, headers, category="env", validity="permanent")

    r = client.put(
        "/api/v1/auth/password",
        json={"current_password": "admin123", "new_password": "admin456"},
        headers=headers,
    )
    assert r.status_code == 200
    # 旧 JWT 因 token_version 失效，公开端点因密钥已吊销而 401。
    r = client.get("/api/public/env-vars", headers={"Authorization": f"Bearer {key['key']}"})
    assert r.status_code == 401

    new_headers = _login_headers(client, "admin", "admin456")
    listed = client.get("/api/v1/auth/query-keys", headers=new_headers).json()["data"]
    assert len(listed) == 1
    assert listed[0]["revoked_at"] is not None


def test_public_decrypt_failure_degrades_to_null(client, admin_user, db):
    """坏行（Fernet 密钥轮换/数据损坏）不打断整批返回：单条 value=null。"""
    headers = _login_headers(client, "admin", "admin123")
    _put_env_vars(client, headers, [
        {"key": "GOOD", "value": "good-value"},
        {"key": "BAD", "value": "bad-value"},
    ])
    row = db.query(UserEnvVar).filter(UserEnvVar.key == "BAD").one()
    row.value_encrypted = "not-a-valid-fernet-token"
    db.commit()

    key = _create_key(client, headers, category="env")
    r = client.get("/api/public/env-vars", headers={"Authorization": f"Bearer {key['key']}"})
    assert r.status_code == 200, r.text
    items = {item["key"]: item["value"] for item in r.json()["items"]}
    assert items["GOOD"] == "good-value"
    assert items["BAD"] is None


def test_public_scoped_per_user(client, admin_user, editor_user):
    admin_h = _login_headers(client, "admin", "admin123")
    editor_h = _login_headers(client, "editor", "editor123")
    _put_env_vars(client, admin_h, [{"key": "ADMIN_ONLY", "value": "a"}])
    _put_env_vars(client, editor_h, [{"key": "EDITOR_ONLY", "value": "e"}])
    admin_key = _create_key(client, admin_h, category="env")

    r = client.get("/api/public/env-vars", headers={"Authorization": f"Bearer {admin_key['key']}"})
    assert r.status_code == 200
    assert [item["key"] for item in r.json()["items"]] == ["ADMIN_ONLY"]
