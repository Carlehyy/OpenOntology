from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.models import User
from app.shared.database import Base
from app.super_assistant import mcp_server_service
from app.super_assistant.models import SuperAssistantMcpServer
from app.super_assistant.kernel.models import CapabilityRevision
from app.super_assistant.mcp_client import decrypt_headers
from app.super_assistant.schemas import McpServerCreate, McpServerUpdate
from app.super_assistant.weknora import mcp_service as weknora_mcp_service
from app.super_assistant.weknora import service as weknora_service
from app.super_assistant.weknora.policy import WEKNORA_ALLOWED_TOOLS

# 捕获真实注册函数：conftest 的 autouse fixture 只把调用入口替换为
# 测试桩，本文件要验证真实注册/幂等/revision 语义。
_REAL_ENSURE = weknora_mcp_service.ensure_server


@pytest.mark.asyncio
async def test_mcp_server_lifecycle_preserves_owner_and_builtin_boundaries(
    tmp_path,
    monkeypatch,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'mcp-service.db'}")
    Base.metadata.create_all(
        bind=engine,
        tables=[User.__table__, SuperAssistantMcpServer.__table__, CapabilityRevision.__table__],
    )
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    with Session() as db:
        db.add_all(
            [
                User(
                    id="owner-1",
                    username="owner-one",
                    email="owner-one@example.com",
                    password_hash="unused",
                    role="editor",
                ),
                User(
                    id="owner-2",
                    username="owner-two",
                    email="owner-two@example.com",
                    password_hash="unused",
                    role="editor",
                ),
            ]
        )
        db.commit()

        custom = mcp_server_service.create_mcp_server(
            db,
            "owner-1",
            McpServerCreate(
                name="owner_tools",
                display_name="OWNER 工具集",
                description="owner 专用 MCP",
                transport="stdio",
                command="npx",
                args=["-y", "@example/mcp-server"],
                env={"API_KEY": "secret"},
            ),
        )
        assert custom.display_name == "OWNER 工具集"
        assert custom.description == "owner 专用 MCP"
        legacy = mcp_server_service.create_mcp_server(
            db,
            "owner-1",
            McpServerCreate(
                name="legacy_tools",
                transport="stdio",
                command="npx",
                args=["-y", "@example/legacy"],
            ),
        )
        assert legacy.display_name == ""
        assert legacy.description == ""
        assert (
            McpServerCreate(
                name="blank_display",
                display_name="   ",
                transport="stdio",
                command="npx",
            ).display_name
            == ""
        )
        renamed = mcp_server_service.update_mcp_server(
            db,
            "owner-1",
            custom.id,
            McpServerUpdate(display_name="OWNER 工具集 v2", description="更新后的说明"),
            include_builtins=False,
        )
        assert renamed.display_name == "OWNER 工具集 v2"
        assert renamed.description == "更新后的说明"
        assert renamed.tool_manifest == []
        builtin = SuperAssistantMcpServer(
            owner_id="owner-1",
            name="platform_minio",
            builtin_key="minio",
            transport="streamable_http",
            url="builtin://minio",
            header_names=[],
            args=[],
            env_names=[],
            tool_manifest=[],
        )
        foreign = SuperAssistantMcpServer(
            owner_id="owner-2",
            name="foreign_tools",
            builtin_key=None,
            transport="stdio",
            url="",
            command="node",
            header_names=[],
            args=[],
            env_names=[],
            tool_manifest=[],
        )
        db.add_all([builtin, foreign])
        db.commit()
        db.refresh(builtin)
        db.refresh(foreign)

        community_ids = {
            item.id
            for item in mcp_server_service.list_mcp_servers(
                db,
                "owner-1",
                include_builtins=False,
            )
        }
        assistant_ids = {
            item.id
            for item in mcp_server_service.list_mcp_servers(
                db,
                "owner-1",
                include_builtins=True,
            )
        }
        assert community_ids == {custom.id, legacy.id}
        assert assistant_ids == {custom.id, legacy.id, builtin.id}
        assert foreign.id not in assistant_ids

        with pytest.raises(
            mcp_server_service.McpServerNotFoundError,
            match="MCP Server 不存在",
        ):
            mcp_server_service.update_mcp_server(
                db,
                "owner-1",
                builtin.id,
                McpServerUpdate(enabled=False),
                include_builtins=False,
            )
        with pytest.raises(
            mcp_server_service.McpServerNotFoundError,
            match="MCP Server 不存在",
        ):
            mcp_server_service.remove_mcp_server(
                db,
                "owner-1",
                foreign.id,
                include_builtins=True,
            )

        toggled_builtin = mcp_server_service.update_mcp_server(
            db,
            "owner-1",
            builtin.id,
            McpServerUpdate(enabled=False, require_confirmation=False),
            include_builtins=True,
        )
        assert toggled_builtin.enabled is False
        with pytest.raises(
            mcp_server_service.McpServerValidationError,
            match="平台内置 MCP 仅允许修改",
        ):
            mcp_server_service.update_mcp_server(
                db,
                "owner-1",
                builtin.id,
                McpServerUpdate(url="https://example.com/mcp"),
                include_builtins=True,
            )

        observed: dict = {}

        async def fake_discover_tools(**connection):
            observed.update(connection)
            return [{"name": "search", "description": "Search"}]

        monkeypatch.setattr(
            mcp_server_service,
            "discover_tools",
            fake_discover_tools,
        )
        tested = await mcp_server_service.test_mcp_server(
            db,
            "owner-1",
            custom.id,
            include_builtins=False,
        )
        assert tested.ok is True
        assert tested.message == "连接成功，发现 1 个工具"
        assert observed["command"] == "npx"
        assert observed["args"] == ["-y", "@example/mcp-server"]
        assert observed["env"] == {"API_KEY": "secret"}

        removed_id = custom.id
        mcp_server_service.remove_mcp_server(
            db,
            "owner-1",
            removed_id,
            include_builtins=False,
        )
        assert db.get(SuperAssistantMcpServer, removed_id) is None
        capability = db.get(CapabilityRevision, ("mcp__owner_tools__search", 1))
        assert capability is not None and capability.enabled is False

    engine.dispose()


@pytest.mark.asyncio
async def test_mcp_manifest_revisions_freeze_and_failed_probe_keeps_last_good(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'mcp-revisions.db'}")
    Base.metadata.create_all(bind=engine, tables=[User.__table__, SuperAssistantMcpServer.__table__, CapabilityRevision.__table__])
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        owner = User(id="owner-rev", username="owner-rev", email="owner-rev@example.com", password_hash="unused", role="editor")
        db.add(owner); db.commit()
        server = mcp_server_service.create_mcp_server(db, owner.id, McpServerCreate(name="rev_tools", transport="stdio", command="node"))
        mode = {"value": 1}
        async def discover(**_):
            if mode["value"] == 3:
                raise RuntimeError("offline")
            return [{"name": "search" if mode["value"] == 1 else "lookup"}]
        monkeypatch.setattr(mcp_server_service, "discover_tools", discover)
        first = await mcp_server_service.test_mcp_server(db, owner.id, server.id, include_builtins=False)
        assert first.ok and server.manifest_revision == 1
        mode["value"] = 2
        second = await mcp_server_service.test_mcp_server(db, owner.id, server.id, include_builtins=False)
        assert second.ok and server.manifest_revision == 2
        mode["value"] = 3
        failed = await mcp_server_service.test_mcp_server(db, owner.id, server.id, include_builtins=False)
        assert not failed.ok and server.manifest_revision == 2 and server.tool_manifest[0]["name"] == "lookup"
        caps = db.query(CapabilityRevision).filter(CapabilityRevision.key == "mcp__rev_tools__lookup").all()
        assert len(caps) == 1 and caps[0].revision == 2 and caps[0].enabled
    engine.dispose()


@pytest.mark.asyncio
async def test_constrained_registration_is_admin_gated_and_builtin_is_platform_owned(tmp_path):
    """YIYA-36 P2-5：constrained 是信任声明——owner 不能自授，admin 才能登记。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'mcp-constrained.db'}")
    Base.metadata.create_all(
        bind=engine,
        tables=[User.__table__, SuperAssistantMcpServer.__table__, CapabilityRevision.__table__],
    )
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    with Session() as db:
        db.add_all([
            User(id="owner-c", username="owner-c", email="owner-c@example.com",
                 password_hash="unused", role="editor"),
            User(id="admin-c", username="admin-c", email="admin-c@example.com",
                 password_hash="unused", role="admin"),
        ])
        db.commit()
        external = mcp_server_service.create_mcp_server(
            db, "owner-c",
            McpServerCreate(name="ext_tools", transport="streamable_http",
                            url="https://127.0.0.1:9/sse"),
        )
        # 存量内置行（迁移前的旧行，列值保持 False）：受约束由构造派生
        legacy_builtin = SuperAssistantMcpServer(
            owner_id="owner-c", name="platform_minio", builtin_key="minio",
            transport="streamable_http", url="builtin://minio",
            header_names=[], args=[], env_names=[], tool_manifest=[],
            constrained=False,
        )
        dev = SuperAssistantMcpServer(
            owner_id="owner-c", name="my_dev", builtin_key=None,
            dev_project_id="dev-1", transport="developed", url="developed://dev-1",
            header_names=[], args=[], env_names=[], tool_manifest=[],
        )
        db.add_all([legacy_builtin, dev])
        db.commit()
        db.refresh(legacy_builtin)
        db.refresh(dev)

        # 1) 普通_owner PATCH constrained=true：拒绝且不落库
        with pytest.raises(mcp_server_service.McpServerValidationError,
                           match="仅平台管理员"):
            mcp_server_service.update_mcp_server(
                db, "owner-c", external.id, McpServerUpdate(constrained=True),
                include_builtins=False)
        db.rollback()
        assert external.constrained is False

        # 2) admin 登记外部 server：放行（跨用户唯一允许字段）
        registered = mcp_server_service.update_mcp_server(
            db, "admin-c", external.id, McpServerUpdate(constrained=True),
            include_builtins=False)
        assert registered.constrained is True
        with pytest.raises(mcp_server_service.McpServerValidationError,
                           match="只允许登记或撤销"):
            mcp_server_service.update_mcp_server(
                db, "admin-c", external.id, McpServerUpdate(enabled=False),
                include_builtins=False)
        db.rollback()

        # 3) 登记与连接变更不能同批提交（admin 在自己的 server 上验证）
        external2 = mcp_server_service.create_mcp_server(
            db, "admin-c",
            McpServerCreate(name="ext_tools2", transport="streamable_http",
                            url="https://127.0.0.1:9/sse2"),
        )
        with pytest.raises(mcp_server_service.McpServerValidationError,
                           match="不能同批提交"):
            mcp_server_service.update_mcp_server(
                db, "admin-c", external2.id,
                McpServerUpdate(constrained=True, url="https://127.0.0.1:9/rotated"),
                include_builtins=False)
        db.rollback()

        # 4) 内置 MCP 的受约束状态由平台维护：admin 也不可改
        with pytest.raises(mcp_server_service.McpServerValidationError,
                           match="由平台构造性保证"):
            mcp_server_service.update_mcp_server(
                db, "admin-c", legacy_builtin.id, McpServerUpdate(constrained=False),
                include_builtins=True)
        db.rollback()

        # 5) 执行期判定（runtime.mcp_server_constrained）：内置（含旧行）恒
        #    受约束；自研/外部默认 fail-closed，外部经 admin 登记后放行
        #    （不能只看 dev_project_id 布尔）
        from app.super_assistant import runtime as sa_runtime

        assert sa_runtime.mcp_server_constrained(legacy_builtin) is True
        assert sa_runtime.mcp_server_constrained(dev) is False
        assert sa_runtime.mcp_server_constrained(external2) is False
        assert sa_runtime.mcp_server_constrained(registered) is True

        # 6) admin 可对自研 server 登记/撤销（连接仍由开发页管理）
        dev_registered = mcp_server_service.update_mcp_server(
            db, "admin-c", dev.id, McpServerUpdate(constrained=True),
            include_builtins=True)
        assert dev_registered.constrained is True
        assert sa_runtime.mcp_server_constrained(dev) is True

        # 7) 登记后连接/凭据变更：信任随旧 endpoint 失效（不得沿用到新端点）
        rotated = mcp_server_service.update_mcp_server(
            db, "owner-c", registered.id,
            McpServerUpdate(url="https://127.0.0.1:9/sse"),
            include_builtins=False)
        assert rotated.constrained is False
        assert sa_runtime.mcp_server_constrained(rotated) is False

    engine.dispose()


def _weknora_tools(*names: str) -> list[dict]:
    return [
        {
            "name": name,
            "description": f"{name} description",
            "input_schema": {"type": "object", "properties": {}},
        }
        for name in names
    ]


def _weknora_integration(tools: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(
        base_url="http://weknora:8080",
        mcp_url="http://weknora:8080/mcp/endpoint-1",
        mcp_token="mcp_" + "a" * 24,
        knowledge_base_id="kb-1",
        knowledge_base_name="平台知识库",
        tools=tools,
    )


def _weknora_session(tmp_path, name: str):
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(
        bind=engine,
        tables=[
            User.__table__,
            SuperAssistantMcpServer.__table__,
            CapabilityRevision.__table__,
        ],
    )
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as db:
        db.add(User(
            id="owner-w", username="owner-w", email="owner-w@example.com",
            password_hash="unused", role="editor",
        ))
        db.commit()
    return engine, Session


def test_weknora_default_registration_is_idempotent_owner_scoped_and_read_only(
    tmp_path, monkeypatch,
):
    engine, Session = _weknora_session(tmp_path, "weknora-ensure.db")
    tools = _weknora_tools(*WEKNORA_ALLOWED_TOOLS)
    integration = _weknora_integration(tools)
    monkeypatch.setattr(weknora_service, "get_integration", lambda: integration)

    def _no_network():
        raise AssertionError("default ensure must not call discover_tools")

    monkeypatch.setattr(weknora_service, "discover_tools", _no_network)

    with Session() as db:
        first = _REAL_ENSURE(db, "owner-w")
        assert first is not None
        assert first.owner_id == "owner-w"
        assert first.name == "platform_weknora"
        assert first.display_name == "知识图谱(WeKnora)"
        assert first.builtin_key == "weknora"
        assert first.transport == "streamable_http"
        assert first.url == integration.mcp_url
        assert first.constrained is True
        assert first.require_confirmation is False
        assert first.enabled is True
        assert [
            tool["name"] for tool in first.tool_manifest
        ] == list(WEKNORA_ALLOWED_TOOLS)
        assert decrypt_headers(first.headers_encrypted) == {
            "Authorization": f"Bearer {integration.mcp_token}",
        }
        capabilities = db.query(CapabilityRevision).all()
        assert {row.side_effect_class for row in capabilities} == {"read_only"}
        assert {row.source for row in capabilities} == {"mcp"}
        assert all(row.enabled for row in capabilities)
        assert {row.revision for row in capabilities} == {1}

        ciphertext = first.headers_encrypted
        second = _REAL_ENSURE(db, "owner-w")
        assert second is not None
        assert second.id == first.id
        assert second.manifest_revision == 1
        # 幂等：token 未变化时不重新加密，manifest hash/revision 不漂移。
        assert second.headers_encrypted == ciphertext
        assert db.query(CapabilityRevision).count() == len(capabilities)

        # 用户停用是用户设置：ensure 不复活，也不改 revision。
        second.enabled = False
        db.commit()
        after_disable = _REAL_ENSURE(db, "owner-w")
        assert after_disable is not None
        assert after_disable.enabled is False
        assert after_disable.manifest_revision == 1

        # 工具清单变化 -> 升 revision，旧快照撤销，新快照继承用户停用状态。
        integration.tools = _weknora_tools(
            "list_knowledge_bases", "search_knowledge", "read_document",
        )
        changed = _REAL_ENSURE(db, "owner-w")
        assert changed is not None
        assert changed.manifest_revision == 2
        assert changed.enabled is False
        old_rows = db.query(CapabilityRevision).filter(
            CapabilityRevision.revision == 1).all()
        assert old_rows and all(row.enabled is False for row in old_rows)
        new_rows = db.query(CapabilityRevision).filter(
            CapabilityRevision.revision == 2).all()
        assert new_rows and all(row.enabled is False for row in new_rows)

        # 用户重新启用后再次变化：新 revision 可用。
        changed.enabled = True
        db.commit()
        integration.tools = _weknora_tools(
            "list_knowledge_bases", "search_knowledge", "grep_chunks",
            "read_document", "list_documents",
        )
        third = _REAL_ENSURE(db, "owner-w")
        assert third is not None
        assert third.manifest_revision == 3
        assert third.enabled is True
        active = db.query(CapabilityRevision).filter(
            CapabilityRevision.revision == 3).all()
        assert active and all(row.enabled for row in active)

        # 删除后重新默认接入：历史快照仍在，不能被 revision 冲突卡死。
        db.delete(third)
        db.commit()
        integration.tools = _weknora_tools(
            "list_knowledge_bases", "search_knowledge", "read_document",
            "list_documents", "wiki_search",
        )
        recreated = _REAL_ENSURE(db, "owner-w")
        assert recreated is not None
        assert recreated.manifest_revision > 3
        selected = db.query(CapabilityRevision).filter(
            CapabilityRevision.revision == recreated.manifest_revision,
        ).all()
        assert selected and all(row.enabled for row in selected)

    engine.dispose()


def test_weknora_ensure_fails_closed_without_verified_integration(tmp_path, monkeypatch):
    engine, Session = _weknora_session(tmp_path, "weknora-unverified.db")

    def _raise():
        raise weknora_service.WeKnoraError("WEKNORA_NOT_INITIALIZED")

    monkeypatch.setattr(weknora_service, "get_integration", _raise)
    with Session() as db:
        with pytest.raises(weknora_service.WeKnoraError, match="WEKNORA_NOT_INITIALIZED"):
            _REAL_ENSURE(db, "owner-w")
        assert db.query(SuperAssistantMcpServer).count() == 0
        assert db.query(CapabilityRevision).count() == 0
    engine.dispose()


@pytest.mark.asyncio
async def test_weknora_manual_test_rejects_unverified_or_changed_manifest(
    tmp_path, monkeypatch,
):
    engine, Session = _weknora_session(tmp_path, "weknora-test.db")
    integration = _weknora_integration(
        _weknora_tools(*WEKNORA_ALLOWED_TOOLS),
    )
    monkeypatch.setattr(weknora_service, "get_integration", lambda: integration)

    with Session() as db:
        installed = _REAL_ENSURE(db, "owner-w")
        assert installed is not None
        baseline = [tool["name"] for tool in installed.tool_manifest]

        async def fake_discover_with_writes():
            return _weknora_tools(
                *WEKNORA_ALLOWED_TOOLS,
                "add_document", "delete_document",
            )

        monkeypatch.setattr(
            weknora_service, "discover_tools", fake_discover_with_writes,
        )
        rejected = await mcp_server_service.test_mcp_server(
            db, "owner-w", installed.id, include_builtins=True,
        )
        # 远端出现白名单外工具必须 fail-closed，不能静默筛掉后注册。
        assert rejected.ok is False
        assert rejected.message == "WEKNORA_UNEXPECTED_TOOL"
        assert installed.manifest_revision == 1
        assert [tool["name"] for tool in installed.tool_manifest] == baseline

        async def fake_discover_exact():
            return _weknora_tools(*WEKNORA_ALLOWED_TOOLS)

        monkeypatch.setattr(weknora_service, "discover_tools", fake_discover_exact)
        matched = await mcp_server_service.test_mcp_server(
            db, "owner-w", installed.id, include_builtins=True,
        )
        assert matched.ok is True
        assert [tool["name"] for tool in matched.tools] == baseline
        assert installed.manifest_revision == 1

        async def fake_discover_subset():
            return _weknora_tools(
                "list_knowledge_bases", "search_knowledge",
                "read_document",
            )

        monkeypatch.setattr(weknora_service, "discover_tools", fake_discover_subset)
        stale = await mcp_server_service.test_mcp_server(
            db, "owner-w", installed.id, include_builtins=True,
        )
        # 远端与已冻结 state 不一致：不能手工临时允许，要求 bootstrap 更新 state。
        assert stale.ok is False
        assert stale.message == "WEKNORA_STATE_OUT_OF_DATE"
        assert installed.manifest_revision == 1
        assert [tool["name"] for tool in installed.tool_manifest] == baseline

        async def fake_discover_changed_description():
            tools = _weknora_tools(*WEKNORA_ALLOWED_TOOLS)
            tools[0]["description"] = "changed by remote"
            return tools

        monkeypatch.setattr(
            weknora_service, "discover_tools", fake_discover_changed_description,
        )
        changed = await mcp_server_service.test_mcp_server(
            db, "owner-w", installed.id, include_builtins=True,
        )
        assert changed.ok is False
        assert changed.message == "WEKNORA_STATE_OUT_OF_DATE"
        assert installed.manifest_revision == 1

        async def fake_write_only():
            return _weknora_tools("add_document", "delete_document")

        monkeypatch.setattr(weknora_service, "discover_tools", fake_write_only)
        write_only = await mcp_server_service.test_mcp_server(
            db, "owner-w", installed.id, include_builtins=True,
        )
        assert write_only.ok is False
        assert write_only.message == "WEKNORA_UNEXPECTED_TOOL"
        assert all(
            row.side_effect_class == "read_only"
            for row in db.query(CapabilityRevision).all()
        )
    engine.dispose()


@pytest.mark.asyncio
async def test_weknora_manual_test_does_not_persist_raw_remote_or_db_errors(
    tmp_path, monkeypatch, caplog,
):
    engine, Session = _weknora_session(tmp_path, "weknora-safe-errors.db")
    integration = _weknora_integration(
        _weknora_tools(*WEKNORA_ALLOWED_TOOLS),
    )
    monkeypatch.setattr(weknora_service, "get_integration", lambda: integration)

    with Session() as db:
        installed = _REAL_ENSURE(db, "owner-w")
        assert installed is not None

        async def fake_discover_leaky():
            raise RuntimeError(
                "postgresql://user:secret@db/weknora token=mcp_secret"
            )

        monkeypatch.setattr(weknora_service, "discover_tools", fake_discover_leaky)
        failed = await mcp_server_service.test_mcp_server(
            db, "owner-w", installed.id, include_builtins=True,
        )
        assert failed.ok is False
        assert failed.message == "WEKNORA_TEST_FAILED"
        assert "secret" not in (failed.message or "")
        assert installed.last_test_message == "WEKNORA_TEST_FAILED"
        assert "secret" not in caplog.text
        assert installed.manifest_revision == 1
    engine.dispose()


def test_weknora_ensure_does_not_log_exception_details(tmp_path, monkeypatch, caplog):
    engine, Session = _weknora_session(tmp_path, "weknora-leak.db")

    def _boom(db, owner_id):
        raise RuntimeError("postgresql://user:secret@db/weknora")

    monkeypatch.setattr(weknora_mcp_service, "_ensure_server", _boom)
    with Session() as db:
        with pytest.raises(
            weknora_service.WeKnoraError, match="WEKNORA_REGISTRATION_FAILED",
        ):
            _REAL_ENSURE(db, "owner-w")
    assert "secret" not in caplog.text
    assert "postgresql" not in caplog.text
    engine.dispose()


def test_weknora_ensure_name_conflict_fails_closed(tmp_path, monkeypatch):
    engine, Session = _weknora_session(tmp_path, "weknora-conflict.db")
    integration = _weknora_integration(
        _weknora_tools(*WEKNORA_ALLOWED_TOOLS),
    )
    monkeypatch.setattr(weknora_service, "get_integration", lambda: integration)
    with Session() as db:
        db.add(SuperAssistantMcpServer(
            owner_id="owner-w", name="platform_weknora", builtin_key=None,
            transport="streamable_http", url="https://example.com/mcp",
            header_names=[], args=[], env_names=[], tool_manifest=[],
        ))
        db.commit()
        with pytest.raises(
            weknora_service.WeKnoraError, match="WEKNORA_SERVER_NAME_CONFLICT",
        ):
            _REAL_ENSURE(db, "owner-w")
        assert db.query(SuperAssistantMcpServer).count() == 1
    engine.dispose()


def test_install_for_platform_admin_requires_exactly_one_active_admin(
    tmp_path, monkeypatch,
):
    engine, Session = _weknora_session(tmp_path, "weknora-bootstrap.db")
    integration = _weknora_integration(
        _weknora_tools(*WEKNORA_ALLOWED_TOOLS),
    )
    monkeypatch.setattr(weknora_service, "get_integration", lambda: integration)

    import app.shared.database as shared_database

    monkeypatch.setattr(shared_database, "SessionLocal", Session)
    with pytest.raises(
        weknora_service.WeKnoraError, match="WEKNORA_PLATFORM_ADMIN_REQUIRED",
    ):
        weknora_mcp_service.install_for_platform_admin()

    with Session() as db:
        db.add(User(
            id="admin-w", username="admin-w", email="admin-w@example.com",
            password_hash="unused", role="admin", is_active=True,
        ))
        db.commit()

    monkeypatch.setattr(weknora_mcp_service, "ensure_server", _REAL_ENSURE)
    server_id = weknora_mcp_service.install_for_platform_admin()
    with Session() as db:
        installed = db.get(SuperAssistantMcpServer, server_id)
        assert installed is not None
        assert installed.owner_id == "admin-w"
        assert installed.builtin_key == "weknora"
        assert installed.constrained is True
        caps = db.query(CapabilityRevision).all()
        assert caps and all(row.side_effect_class == "read_only" for row in caps)
    engine.dispose()


@pytest.mark.parametrize("denied_tool", ["ask", "add_document", "delete_document"])
def test_weknora_ensure_rejects_non_allowlisted_tools(
    tmp_path, monkeypatch, denied_tool,
):
    engine, Session = _weknora_session(tmp_path, f"weknora-denied-{denied_tool}.db")
    integration = _weknora_integration(
        _weknora_tools(*WEKNORA_ALLOWED_TOOLS, denied_tool),
    )
    monkeypatch.setattr(weknora_service, "get_integration", lambda: integration)
    with Session() as db:
        with pytest.raises(
            weknora_service.WeKnoraError, match="WEKNORA_UNEXPECTED_TOOL",
        ):
            _REAL_ENSURE(db, "owner-w")
        assert db.query(SuperAssistantMcpServer).count() == 0
        assert db.query(CapabilityRevision).count() == 0
    engine.dispose()
