"""文件夹同步脚本的端到端测试：subprocess 运行渲染后的真实脚本，
打到测试内启动的真实 FastAPI 应用（uvicorn 线程 + sqlite 临时库），
覆盖首传建树 / 幂等二跑 / 替换更新 / 镜像删除与空目录清理 / 忽略规则 /
dry-run 零变更 / 在途满自动等待 / 令牌失效快速失败。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.models import RoleMenuPermission, User
from app.deps import get_db
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


def _user(user_id: str, username: str) -> User:
    return User(
        id=user_id, username=username, email=f"{username}@example.com",
        password_hash="unused", role="editor",
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "super_assistant_palace_workspace_root", str(tmp_path / "palace"))
    monkeypatch.setattr(palace_service, "dispatch_super_assistant_palace_extract", lambda owner_id, file_id: None)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'script-e2e.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(bind=engine, tables=_TABLES)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with Session() as db:
        db.add(_user("user-1", "owner"))
        db.commit()
        token = palace_sync.generate_sync_token(db, "user-1")

    def override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(palace_sync.sync_router, prefix=_PREFIX)
    app.dependency_overrides[get_db] = override_db

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn 测试服务未能启动"

    counter = iter(range(10000))

    def write_script(*, token_value: str | None = None, max_in_flight: int = 20) -> Path:
        path = tmp_path / f"palace_sync_{next(counter)}.py"
        path.write_text(
            palace_sync.render_sync_script(
                base_url=f"http://127.0.0.1:{port}",
                token=token_value or token,
                allowed_extensions=["md", "txt", "pdf", "docx"],
                max_upload_mb=5,
                max_in_flight=max_in_flight,
            ),
            encoding="utf-8",
        )
        return path

    def run(script_path: Path, folder: Path, *extra: str):
        return subprocess.run(
            [sys.executable, str(script_path), "--folder", str(folder), "--yes", *extra],
            capture_output=True, text=True, encoding="utf-8", timeout=180,
            env={
                **os.environ,
                "PALACE_SYNC_POLL_SECONDS": "1",
                "PALACE_SYNC_HOURLY_WAIT_SECONDS": "2",
            },
        )

    try:
        yield SimpleNamespace(session=Session, write_script=write_script, run=run, root=tmp_path)
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_first_sync_idempotent_replace_and_mirror_delete(env):
    folder = env.root / "docs"
    (folder / "笔记").mkdir(parents=True)
    (folder / "知识库.md").write_text("# 知识 v1\n", encoding="utf-8")
    (folder / "笔记" / "会议.txt").write_text("会议纪要", encoding="utf-8")
    script = env.write_script()

    first = env.run(script, folder)
    assert first.returncode == 0, first.stdout + first.stderr
    with env.session() as db:
        rows = db.query(SuperAssistantPalaceFile).all()
        assert {(row.folder_path, row.filename) for row in rows} == {
            ("synced/docs", "知识库.md"),
            ("synced/docs/笔记", "会议.txt"),
        }
        sha_before = {row.filename: row.sha256 for row in rows}

    second = env.run(script, folder)
    assert second.returncode == 0
    assert "计划: 上传 0，更新 0，删除 0" in second.stdout

    (folder / "知识库.md").write_text("# 知识 v2\n", encoding="utf-8")
    third = env.run(script, folder)
    assert third.returncode == 0
    assert "计划: 上传 0，更新 1，删除 0" in third.stdout
    with env.session() as db:
        row = db.query(SuperAssistantPalaceFile).filter_by(filename="知识库.md").one()
        assert row.sha256 != sha_before["知识库.md"]

    # 镜像删除（用户已确认的语义）：本地删除文件与目录 → 平台侧同步删除并清理空目录
    (folder / "笔记" / "会议.txt").unlink()
    (folder / "笔记").rmdir()
    fourth = env.run(script, folder)
    assert fourth.returncode == 0
    assert "计划: 上传 0，更新 0，删除 1" in fourth.stdout
    with env.session() as db:
        assert db.query(SuperAssistantPalaceFile).filter_by(filename="会议.txt").count() == 0
        assert db.query(SuperAssistantPalaceFolder).filter_by(path="synced/docs/笔记").count() == 0
        assert db.query(SuperAssistantPalaceFolder).filter_by(path="synced/docs").count() == 1


def test_skip_rules_exclude_unwanted_files(env):
    folder = env.root / "mixed"
    (folder / ".git").mkdir(parents=True)
    (folder / ".git" / "config.md").write_text("x", encoding="utf-8")
    (folder / "node_modules").mkdir()
    (folder / "node_modules" / "lib.md").write_text("x", encoding="utf-8")
    (folder / "evil.exe").write_bytes(b"MZ")
    (folder / "~$lock.docx").write_bytes(b"x")
    (folder / "notes.md.tmp").write_text("x", encoding="utf-8")
    (folder / "Thumbs.db").write_bytes(b"x")
    (folder / "big.md").write_text("y" * (6 * 1024 * 1024), encoding="utf-8")  # 超 5MB 上限
    (folder / "ok.md").write_text("keep", encoding="utf-8")

    result = env.run(env.write_script(), folder)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "evil.exe（类型不在白名单（.exe））" in result.stdout
    assert "notes.md.tmp（类型不在白名单（.tmp））" in result.stdout
    assert "Thumbs.db（系统文件）" in result.stdout
    assert "~$lock.docx（Office 临时锁文件）" in result.stdout
    assert "big.md（超过单文件 5MB 上限）" in result.stdout
    assert "忽略目录" in result.stdout and ".git（忽略目录）" in result.stdout
    assert "node_modules（忽略目录）" in result.stdout
    with env.session() as db:
        assert {row.filename for row in db.query(SuperAssistantPalaceFile).all()} == {"ok.md"}


def test_dry_run_plans_without_mutation(env):
    folder = env.root / "plan"
    folder.mkdir()
    (folder / "a.md").write_text("a", encoding="utf-8")
    result = env.run(env.write_script(), folder, "--dry-run")
    assert result.returncode == 0
    assert "计划: 上传 1，更新 0，删除 0" in result.stdout
    assert "[将上传] a.md" in result.stdout
    assert "dry-run 结束，未做任何改动" in result.stdout
    with env.session() as db:
        assert db.query(SuperAssistantPalaceFile).count() == 0


def test_invalid_token_fails_fast(env):
    folder = env.root / "unauthorized"
    folder.mkdir()
    (folder / "a.md").write_text("a", encoding="utf-8")
    result = env.run(env.write_script(token_value="pal_sync_wrong"), folder)
    assert result.returncode == 1
    assert "令牌无效或已被重置" in result.stdout


def test_waits_when_in_flight_threshold_reached(env):
    folder = env.root / "paced"
    folder.mkdir()
    (folder / "a.md").write_text("a", encoding="utf-8")
    (folder / "b.md").write_text("b", encoding="utf-8")
    # 阈值 = max(1, 2-4) = 1：首个文件上传后必须等待在途释放才能继续
    script = env.write_script(max_in_flight=2)

    stop = threading.Event()

    def drain():  # 模拟后台抽取完成：pending → built
        while not stop.is_set():
            with env.session() as db:
                for row in db.query(SuperAssistantPalaceFile).filter_by(status="pending").all():
                    row.status = "built"
                db.commit()
            time.sleep(0.2)

    worker = threading.Thread(target=drain, daemon=True)
    worker.start()
    try:
        result = env.run(script, folder)
    finally:
        stop.set()
        worker.join(timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[等待]" in result.stdout
    with env.session() as db:
        assert db.query(SuperAssistantPalaceFile).count() == 2


def test_aborts_with_clear_message_on_file_count_cap(env, monkeypatch):
    monkeypatch.setattr(settings, "super_assistant_palace_max_files_per_user", 1)
    folder = env.root / "capped"
    folder.mkdir()
    (folder / "a.md").write_text("a", encoding="utf-8")
    (folder / "b.md").write_text("b", encoding="utf-8")
    result = env.run(env.write_script(), folder)
    assert result.returncode == 1
    assert "已达上限" in result.stdout
