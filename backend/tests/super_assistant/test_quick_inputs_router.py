"""输入框快捷内容 CRUD 与优先级重排（/api/v2/super-assistant/quick-inputs）。

行为分层验证：
- 列表/创建：空列表起步、字段往返、新建追加末尾（sort_order 递增）、
  (sort_order, created_at) 稳定排序；
- 更新/删除：部分字段更新、他人 id 一律 404（不泄露存在性）、删除后列表收口；
- 重排：全量顺序归一化为 0..n-1、未知/他人/重复 id 静默忽略不打挂重排；
- 校验：空白标题/内容与超长字段 422。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.models import User
from app.deps import get_current_user, get_db
from app.shared.database import Base
from app.super_assistant.models import SuperAssistantQuickInput
from app.super_assistant.quick_inputs import router as quick_inputs_router

_TABLES = [User.__table__, SuperAssistantQuickInput.__table__]


def _make_client(tmp_path, *, current_id: str = "user-1", db_name: str = "quick-inputs.db", seed: bool = True) -> TestClient:
    engine = create_engine(
        f"sqlite:///{tmp_path / db_name}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=_TABLES)
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(quick_inputs_router, prefix="/api/v2/super-assistant")
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: User(
        id=current_id, username="owner", email="owner@example.com",
        password_hash="unused", role="editor",
    )
    if seed:
        with TestingSession() as db:
            for uid in ("user-1", "user-2"):
                db.add(User(
                    id=uid, username=uid, email=f"{uid}@example.com",
                    password_hash="unused", role="editor",
                ))
            # 他人的一条数据：验证 owner 隔离（不可见、不可改、重排不波及）
            db.add(SuperAssistantQuickInput(
                id="qi-other", owner_id="user-2", title="他人的",
                content="他人内容", sort_order=1,
            ))
            db.commit()
    return TestClient(app)


def _titles(client: TestClient) -> list[str]:
    resp = client.get("/api/v2/super-assistant/quick-inputs")
    assert resp.status_code == 200, resp.text
    return [item["title"] for item in resp.json()]


def _create(client: TestClient, title: str, content: str = "内容"):
    resp = client.post("/api/v2/super-assistant/quick-inputs", json={"title": title, "content": content})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_list_starts_empty_and_hides_other_owners(tmp_path):
    assert _titles(_make_client(tmp_path)) == []


def test_create_appends_to_end_with_roundtrip_fields(tmp_path):
    client = _make_client(tmp_path)
    first = _create(client, "自我介绍", "我是产品经理，负责 OpenOntology 平台。")
    second = _create(client, "追问模板", "请展开讲讲第 2 点，给出具体例子。")

    assert first["title"] == "自我介绍"
    assert first["content"] == "我是产品经理，负责 OpenOntology 平台。"
    assert first["sort_order"] == 1
    assert second["sort_order"] == 2
    # 列表按展示优先级返回：新建在后
    assert _titles(client) == ["自我介绍", "追问模板"]


def test_update_changes_fields_partially(tmp_path):
    client = _make_client(tmp_path)
    entry = _create(client, "标题A", "内容A")
    resp = client.patch(
        f"/api/v2/super-assistant/quick-inputs/{entry['id']}",
        json={"title": "标题B"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "标题B"
    assert resp.json()["content"] == "内容A"  # 未提交字段不动
    resp = client.patch(
        f"/api/v2/super-assistant/quick-inputs/{entry['id']}",
        json={"content": "内容B"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] == "内容B"


def test_delete_removes_entry(tmp_path):
    client = _make_client(tmp_path)
    entry = _create(client, "待删", "内容")
    resp = client.delete(f"/api/v2/super-assistant/quick-inputs/{entry['id']}")
    assert resp.status_code == 204
    assert _titles(client) == []
    # 重复删除：已不存在 → 404
    assert client.delete(f"/api/v2/super-assistant/quick-inputs/{entry['id']}").status_code == 404


def test_foreign_ids_are_404_and_invisible(tmp_path):
    client = _make_client(tmp_path)
    assert client.get("/api/v2/super-assistant/quick-inputs").json() == []
    assert client.patch(
        "/api/v2/super-assistant/quick-inputs/qi-other", json={"title": "越权"}
    ).status_code == 404
    assert client.delete("/api/v2/super-assistant/quick-inputs/qi-other").status_code == 404


def test_reorder_normalizes_full_list(tmp_path):
    client = _make_client(tmp_path)
    a = _create(client, "A")
    b = _create(client, "B")
    c = _create(client, "C")
    assert _titles(client) == ["A", "B", "C"]

    # 拖拽把 C 提到最前：全量顺序 [C, A, B]
    resp = client.put("/api/v2/super-assistant/quick-inputs/reorder", json={"ids": [c["id"], a["id"], b["id"]]})
    assert resp.status_code == 200, resp.text
    assert [item["title"] for item in resp.json()] == ["C", "A", "B"]
    # sort_order 归一化为 0..n-1，刷新后顺序持久
    assert [item["sort_order"] for item in resp.json()] == [0, 1, 2]
    assert _titles(client) == ["C", "A", "B"]


def test_reorder_ignores_unknown_duplicate_and_foreign_ids(tmp_path):
    client = _make_client(tmp_path)
    a = _create(client, "A")
    b = _create(client, "B")

    # 未知 id / 重复 id / 他人 id：静默忽略，不 4xx 不 5xx，其余按给定顺序落位
    resp = client.put("/api/v2/super-assistant/quick-inputs/reorder", json={
        "ids": ["no-such-id", b["id"], "qi-other", b["id"], a["id"]],
    })
    assert resp.status_code == 200, resp.text
    assert _titles(client) == ["B", "A"]
    # 他人条目不受波及：user-2 视角复用同一库断言（他人 id 被忽略，sort_order 原样）
    other = _make_client(tmp_path, current_id="user-2", seed=False)
    other_entries = other.get("/api/v2/super-assistant/quick-inputs").json()
    assert [item["title"] for item in other_entries] == ["他人的"]
    assert other_entries[0]["sort_order"] == 1


def test_validation_rejects_blank_and_overlong_fields(tmp_path):
    client = _make_client(tmp_path)
    for body in (
        {"title": "   ", "content": "内容"},          # 空白标题（strip 后为空）
        {"title": "标题", "content": ""},             # 空内容
        {"title": "x" * 101, "content": "内容"},      # 标题超长
        {"title": "标题", "content": "x" * 2001},     # 内容超长
    ):
        resp = client.post("/api/v2/super-assistant/quick-inputs", json=body)
        assert resp.status_code == 422, body
    assert _titles(client) == []
    # 更新通道同口径
    entry = _create(client, "合法", "内容")
    assert client.patch(
        f"/api/v2/super-assistant/quick-inputs/{entry['id']}", json={"title": " "}
    ).status_code == 422
