"""记忆宫殿「文件夹同步」：令牌生命周期、鉴权矩阵、脚本下发与薄封装 CRUD。

fixture 与断言风格沿用 test_memory_palace.py：sqlite 临时库 + 依赖覆盖；
抽取派发替换为 no-op，HTTP 用例不触发真实抽取。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.models import RoleMenuPermission, User
from app.deps import get_current_user, get_db
from app.shared.config import settings
from app.shared.database import Base
from app.super_assistant import palace_service, palace_sync
from app.super_assistant.models import (
    SuperAssistantPalaceBuild,
    SuperAssistantPalaceFile,
    SuperAssistantPalaceFolder,
    SuperAssistantPalaceSyncToken,
)

_TABLES = [
    User.__table__,
    RoleMenuPermission.__table__,
    SuperAssistantPalaceFile.__table__,
    SuperAssistantPalaceBuild.__table__,
    SuperAssistantPalaceFolder.__table__,
    SuperAssistantPalaceSyncToken.__table__,
]

_PREFIX = "/api/v2/super-assistant"


def _user(user_id: str, username: str, role: str = "editor") -> User:
    return User(
        id=user_id, username=username, email=f"{username}@example.com",
        password_hash="unused", role=role,
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "super_assistant_palace_workspace_root", str(tmp_path / "palace"))
    monkeypatch.setattr(palace_service, "dispatch_super_assistant_palace_extract", lambda owner_id, file_id: None)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'palace-sync.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(bind=engine, tables=_TABLES)
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with TestingSession() as db:
        db.add(_user("user-1", "owner"))
        db.add(_user("user-2", "other"))
        db.add(_user("user-3", "limited", role="custom"))
        db.commit()

    def override_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    def make_client(user: User, token: str | None = None):
        app = FastAPI()
        app.include_router(palace_sync.management_router, prefix=_PREFIX)
        app.include_router(palace_sync.sync_router, prefix=_PREFIX)
        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = lambda: user
        client = TestClient(app)
        if token:
            client.headers.update({"X-Palace-Sync-Token": token})
        return client

    def mint(user_id: str) -> str:
        with TestingSession() as db:
            return palace_sync.generate_sync_token(db, user_id)

    return SimpleNamespace(
        client=make_client(_user("user-1", "owner")),
        make_client=make_client,
        mint=mint,
        session=TestingSession,
    )


def test_token_reset_invalidates_old_token(env):
    created = env.client.post(f"{_PREFIX}/palace/sync/token")
    assert created.status_code == 200
    token = created.json()["token"]
    assert token.startswith("pal_sync_")

    listed = env.make_client(_user("user-1", "owner"), token=token).get(f"{_PREFIX}/palace/sync/files")
    assert listed.status_code == 200
    assert listed.json() == []

    reset = env.client.post(f"{_PREFIX}/palace/sync/token")
    assert reset.status_code == 200
    new_token = reset.json()["token"]
    assert new_token != token

    # 旧令牌立即失效，新令牌可用
    assert env.make_client(_user("user-1", "owner"), token=token).get(
        f"{_PREFIX}/palace/sync/files").status_code == 401
    assert env.make_client(_user("user-1", "owner"), token=new_token).get(
        f"{_PREFIX}/palace/sync/files").status_code == 200


def test_sync_routes_require_sync_token_not_browser_jwt(env):
    # 无令牌：即便路由侧 get_current_user 可解析（浏览器 JWT 场景），sync 路由也不放行
    plain = env.make_client(_user("user-1", "owner"))
    response = plain.get(f"{_PREFIX}/palace/sync/files")
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid sync token"

    forged = env.make_client(_user("user-1", "owner"), token="pal_sync_forged")
    assert forged.get(f"{_PREFIX}/palace/sync/files").status_code == 401

    # 每次成功调用刷新 last_used_at
    token = env.mint("user-1")
    ok = env.make_client(_user("user-1", "owner"), token=token).get(f"{_PREFIX}/palace/sync/files")
    assert ok.status_code == 200
    with env.session() as db:
        row = db.get(SuperAssistantPalaceSyncToken, "user-1")
        assert row is not None and row.last_used_at is not None


def test_sync_upload_replace_delete_roundtrip_with_filename_override(env):
    token = env.mint("user-1")
    client = env.make_client(_user("user-1", "owner"), token=token)

    rejected = client.post(
        f"{_PREFIX}/palace/sync/files",
        files={"file": ("payload", b"MZ", "application/octet-stream")},
        data={"filename": "evil.exe", "folder_path": "synced/docs"},
    )
    assert rejected.status_code == 400  # 白名单复用 palace_service

    created = client.post(
        f"{_PREFIX}/palace/sync/files",
        files={"file": ("payload", "# 知识\n张三 任职 ACME\n".encode(), "text/markdown")},
        data={"filename": "知识库.md", "folder_path": "synced/docs"},
    )
    assert created.status_code == 201, created.text
    row = created.json()
    # multipart 占位文件名被 UTF-8 form 字段覆盖；目录自动 mkdir-p
    assert row["filename"] == "知识库.md"
    assert row["path"] == "synced/docs"
    assert row["sha256"]
    file_id = row["id"]

    listed = client.get(f"{_PREFIX}/palace/sync/files")
    assert [item["filename"] for item in listed.json()] == ["知识库.md"]
    folders = client.get(f"{_PREFIX}/palace/sync/folders")
    assert {"synced", "synced/docs"} <= {item["path"] for item in folders.json()}

    replaced = client.post(
        f"{_PREFIX}/palace/sync/files/{file_id}/replace",
        files={"file": ("payload", "# 知识 v2\n".encode(), "text/markdown")},
        data={"filename": "知识库.md"},
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["sha256"] != row["sha256"]

    assert client.delete(f"{_PREFIX}/palace/sync/files/{file_id}").status_code == 204
    assert client.get(f"{_PREFIX}/palace/sync/files").json() == []


def test_sync_files_scoped_to_token_owner(env):
    owner_token = env.mint("user-1")
    owner_client = env.make_client(_user("user-1", "owner"), token=owner_token)
    artifact = owner_client.post(
        f"{_PREFIX}/palace/sync/files",
        files={"file": ("payload", "私有知识".encode(), "text/markdown")},
        data={"filename": "secret.md", "folder_path": "synced"},
    ).json()

    other_token = env.mint("user-2")
    other_client = env.make_client(_user("user-2", "other"), token=other_token)
    assert other_client.get(f"{_PREFIX}/palace/sync/files").json() == []
    assert other_client.delete(f"{_PREFIX}/palace/sync/files/{artifact['id']}").status_code == 404


def test_custom_role_without_menu_denied(env):
    token = env.mint("user-3")  # custom 角色默认仅 overview，无 super_assistant
    client = env.make_client(_user("user-3", "limited", role="custom"), token=token)
    response = client.get(f"{_PREFIX}/palace/sync/files")
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "MENU_ACCESS_DENIED"


def test_script_download_embeds_token_and_compiles(env):
    first = env.client.get(f"{_PREFIX}/palace/sync/script")
    assert first.status_code == 200
    assert first.headers["content-type"].startswith("text/x-python")
    assert 'filename="palace_sync.py"' in first.headers["content-disposition"]
    body = first.text
    assert "pal_sync_" in body
    assert settings.pipeline_file_public_api_base_url.rstrip("/") in body
    compile(body, "palace_sync.py", "exec")

    # 重置后再次下载：脚本内嵌的是新令牌（重复下载本身不轮换）
    reset = env.client.post(f"{_PREFIX}/palace/sync/token").json()["token"]
    second = env.client.get(f"{_PREFIX}/palace/sync/script").text
    assert reset in second
    assert reset not in body


def test_filename_override_rejects_overlong(env):
    token = env.mint("user-1")
    client = env.make_client(_user("user-1", "owner"), token=token)
    response = client.post(
        f"{_PREFIX}/palace/sync/files",
        files={"file": ("payload", b"x", "text/markdown")},
        data={"filename": "a" * 256 + ".md"},
    )
    assert response.status_code == 400


def test_quota_defaults_raised_for_folder_sync():
    # 文件夹同步整夹首传上调三项默认值；在途与并发维持 LLM 成本护栏
    assert settings.super_assistant_palace_max_files_per_user == 2000
    assert settings.super_assistant_palace_max_total_mb == 10240
    assert settings.super_assistant_palace_max_builds_per_hour == 300
    assert settings.super_assistant_palace_max_in_flight == 20
