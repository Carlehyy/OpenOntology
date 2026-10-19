from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.models import User
from app.shared.database import Base
from app.super_assistant import mcp_server_service
from app.super_assistant.models import SuperAssistantMcpServer
from app.super_assistant.kernel.models import CapabilityRevision
from app.super_assistant.schemas import McpServerCreate, McpServerUpdate


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
