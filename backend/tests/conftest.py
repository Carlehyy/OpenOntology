import atexit
import base64
import os
import tempfile
from pathlib import Path

# Production/development startup requires the real external stack. Tests opt
# into the only supported SQLite profile before importing application modules.
os.environ["ENVIRONMENT"] = "test"
# 测试环境不依赖本机 Redis：任务池缓存整体关闭，行为与直查路径一致；
# 缓存命中/失效/降级语义由 test_pipeline_task_cache.py 用假客户端验证。
os.environ["PIPELINE_TASK_CACHE_ENABLED"] = "false"
# 本体详情页缓存同理：默认关闭，缓存语义由 test_ontology_detail_cache.py
# 用假客户端按需开启验证。
os.environ["ONTOLOGY_DETAIL_CACHE_ENABLED"] = "false"
# 本体网络缓存同理：默认关闭，缓存语义由 test_network_cache.py
# 用假客户端按需开启验证。
os.environ["ONTOLOGY_NETWORK_CACHE_ENABLED"] = "false"
# 世界模型缓存同理：默认关闭，缓存语义由 test_world_model_cache.py
# 用假客户端按需开启验证。
os.environ["WORLD_MODEL_CACHE_ENABLED"] = "false"
# 本体版本树缓存同理：默认关闭，缓存语义由 test_version_tree_cache.py
# 用假客户端按需开启验证。
os.environ["ONTOLOGY_VERSION_TREE_CACHE_ENABLED"] = "false"
# 本体列表缓存同理：默认关闭，缓存语义由 test_ontology_list_cache.py
# 用假客户端按需开启验证。
os.environ["ONTOLOGY_LIST_CACHE_ENABLED"] = "false"
# 平台概览统计缓存同理：默认关闭，缓存语义由 test_platform_stats_cache.py
# 用假客户端按需开启验证。
os.environ["PLATFORM_STATS_CACHE_ENABLED"] = "false"
# 版本事件 drain 默认内联执行（历史语义）；异步派发路径由
# test_version_automation_events.py 用 patch 单独验证。
os.environ["DATASET_EVENT_DISPATCH_MODE"] = "sync"

# 应用的 SessionLocal/engine 在 import 时按 DATABASE_URL 绑定。默认的固定路径
# /tmp/ontoprompt.db 会被 xdist 多 worker 共享，并发 lifespan seeding 的
# create_all 会互相踩表（table users already exists）；这里给每个进程一个独立
# 文件。它刻意与下方 fixture 引擎的库分离——seed 的管理员账号不能与测试
# fixture 自建账号同库，否则用户名唯一约束冲突。
_app_db_fd, _app_db_path = tempfile.mkstemp(prefix="ontoprompt_app_", suffix=".db")
os.close(_app_db_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_app_db_path}"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.main import app
from app.database import Base
from app.deps import get_db
from app.services.auth_service import hash_password
from app.models.user import User
import uuid

# 测试环境使用最低 bcrypt 轮次：生产默认 12 轮时每次 hash+verify ≈ 0.37s，
# 认证 fixture 与 TestClient 启动 seeding 在近两千个用例中重复支付该成本。
# 轮次存储在哈希串中，低轮次上下文仍可验证既有哈希，不影响生产策略。
from passlib.context import CryptContext
import app.auth.service as _auth_service

_auth_service.pwd_context = CryptContext(
    schemes=["bcrypt_sha256", "bcrypt"],
    deprecated="auto",
    bcrypt_sha256__rounds=4,
    bcrypt__rounds=4,
)

# 每次 pytest 运行使用独立的临时 SQLite, 避免并发运行互相锁库
_db_fd, _db_path = tempfile.mkstemp(prefix="ontoprompt_test_", suffix=".db")
os.close(_db_fd)

TEST_DB = f"sqlite:///{_db_path}"
engine = create_engine(TEST_DB, connect_args={"check_same_thread": False})
TestSession = sessionmaker(bind=engine)


class QueryCounter:
    """统计测试会话引擎上实际执行的 SQL 条数（上下文管理器）。

    用于 N+1 回归与缓存命中断言：缓存命中路径只剩认证等固定开销。
    """

    def __init__(self, session):
        self._session = session
        self.count = 0

    def _incr(self, *args, **kwargs):
        self.count += 1

    def __enter__(self):
        from sqlalchemy import event

        event.listen(self._session.get_bind(), "before_cursor_execute", self._incr)
        return self

    def __exit__(self, *exc):
        from sqlalchemy import event

        event.remove(self._session.get_bind(), "before_cursor_execute", self._incr)


@atexit.register
def _cleanup_test_db():
    engine.dispose()
    for path in (_db_path, _app_db_path):
        try:
            os.unlink(path)
        except OSError:
            pass

@pytest.fixture(autouse=True)
def setup_db():
    # Use the same complete registry as Alembic and background workers. A
    # route's incidental imports must not decide whether an FK target exists.
    from app.model_registry import import_all_models

    import_all_models()
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)

@pytest.fixture
def db():
    session = TestSession()
    try:
        yield session
    finally:
        session.close()

@pytest.fixture
def client(db):
    def override_get_db():
        yield db
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()

@pytest.fixture
def admin_user(db):
    user = User(id=str(uuid.uuid4()), username="admin", email="admin@test.com",
                password_hash=hash_password("admin123"), role="admin")
    db.add(user); db.commit(); db.refresh(user)
    return user

@pytest.fixture
def editor_user(db):
    user = User(id=str(uuid.uuid4()), username="editor", email="editor@test.com",
                password_hash=hash_password("editor123"), role="editor")
    db.add(user); db.commit(); db.refresh(user)
    return user

@pytest.fixture
def admin_token(client, admin_user):
    r = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
    return r.json()["data"]["access_token"]

@pytest.fixture
def auth_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}

@pytest.fixture
def ontology(client, auth_headers, db):
    r = client.post("/api/v1/ontologies", json={"name": "测试本体", "domain": "供应链"}, headers=auth_headers)
    return r.json()["data"]


@pytest.fixture
def legacy_xls_bytes():
    """真实 OLE/BIFF8 工作簿：整数、单元格内换行及第二工作表。"""
    fixture = (
        Path(__file__).resolve().parent
        / "v2" / "fixtures" / "legacy_integer_multiline.xls.b64"
    )
    encoded = fixture.read_text(encoding="ascii").strip()
    return base64.b64decode(encoded, validate=True)
