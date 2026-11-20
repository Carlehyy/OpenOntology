"""multica 多 profile 服务层：CRUD、生效回落、平台默认只读与凭据治理。

语义基线（与 multica_service 实现对齐）：
- 平台默认在无生效 custom 时自动激活（active 与 default ID 一致）；
- 编辑/token 轮换/软删除只 retire 旧 revision，旧凭据保持可解析，
  直到显式写入 revoked_at 才拒绝；
- 激活走真实校验链（fetch_me + list_workspaces + 工作区归属），失败 409
  且不影响既有激活绑定；
- 网络只在测试里 mock（multica_client 函数级），服务层不被 patch。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.auth.models import User
from app.shared.database import Base
from app.super_assistant import multica_client, multica_service
from app.super_assistant.models import (
    SuperAssistantMulticaConfig,
    SuperAssistantMulticaProfile,
    SuperAssistantMulticaProfileRevision,
)
from app.super_assistant.schemas import (
    MulticaConfigUpdate,
    MulticaProfileCreate,
    MulticaProfileUpdate,
)

DEFAULTS = {
    "base_url": "http://127.0.0.1:9090",
    "web_url": "http://127.0.0.1:3000",
    "workspace_id": "ws-default",
    "workspace_name": "平台工作区",
    "token": "platform-token",
}
VISIBLE_WORKSPACES = ("ws-1", "ws-b", "ws-default", "ws-9")


@pytest.fixture()
def multica_network(monkeypatch):
    """mock 网络层并记录调用：激活/测试连接的真实校验链可观测。"""
    calls = {"fetch_me": [], "workspaces": []}

    def _fetch_me(base_url, token):
        calls["fetch_me"].append((base_url, token))
        return {"id": "fixture-user", "name": "fixture-user"}

    def _list_workspaces(base_url, token):
        calls["workspaces"].append((base_url, token))
        return [
            {"id": identity, "name": identity, "slug": identity}
            for identity in VISIBLE_WORKSPACES
        ]

    monkeypatch.setattr(multica_client, "fetch_me", _fetch_me)
    monkeypatch.setattr(multica_client, "list_workspaces", _list_workspaces)
    return calls


@pytest.fixture()
def session(tmp_path, multica_network):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'multica-profiles.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=[
        User.__table__, SuperAssistantMulticaConfig.__table__,
        SuperAssistantMulticaProfile.__table__,
        SuperAssistantMulticaProfileRevision.__table__,
    ])
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with TestingSession() as db:
        db.add(User(
            id="user-1", username="owner", email="owner@example.com",
            password_hash="unused", role="editor",
        ))
        db.add(User(
            id="user-2", username="intruder", email="intruder@example.com",
            password_hash="unused", role="editor",
        ))
        db.commit()
        yield db


def _create_custom(session, **overrides):
    body = MulticaProfileCreate(
        name=overrides.get("name", "团队实例"),
        base_url=overrides.get("base_url", "http://127.0.0.1:8080"),
        workspace_id=overrides.get("workspace_id", "ws-1"),
        workspace_name=overrides.get("workspace_name", "My Workspace"),
        token=overrides.get("token", "mul-token"),
        enabled=overrides.get("enabled", True),
    )
    return multica_service.create_profile(session, "user-1", body)


def _platform_defaults(monkeypatch, **overrides):
    source = {**DEFAULTS, **overrides}
    monkeypatch.setattr(
        multica_service, "_platform_default_settings", lambda: source,
    )
    return source


def _no_platform_defaults(monkeypatch):
    blanks = {key: "" for key in DEFAULTS}
    monkeypatch.setattr(
        multica_service, "_platform_default_settings", lambda: blanks,
    )
    return blanks


def _current_revision(session, profile_id):
    return multica_service._current_revision(session, profile_id)


# ---------------------------------------------------------------------------
# CRUD 与凭据脱敏
# ---------------------------------------------------------------------------

def test_create_profile_is_inactive_revision_one_and_never_echoes_token(session):
    view = _create_custom(session)
    assert view.kind == "custom" and view.is_active is False
    assert view.revision == 1 and view.token_set is True
    assert view.enabled is True and view.base_url == "http://127.0.0.1:8080"
    payload = view.model_dump()
    assert "token" not in payload and "token_encrypted" not in payload


def test_create_profile_requires_token_to_enable_and_validates_url(session):
    with pytest.raises(multica_service.MulticaServiceError, match="API Token"):
        _create_custom(session, token=None, enabled=True)
    with pytest.raises(multica_client.MulticaClientError):
        _create_custom(session, base_url="ftp://multica.local")


def test_update_profile_appends_immutable_revision_and_keeps_blank_token(session):
    profile = _create_custom(session)
    updated = multica_service.update_profile(session, "user-1", profile.id, MulticaProfileUpdate(
        workspace_id="ws-2",
    ))
    assert updated.revision == 2 and updated.workspace_id == "ws-2"
    # token 留空保留已存凭据
    revision = _current_revision(session, profile.id)
    assert multica_service.decrypt_token(revision) == "mul-token"
    old = session.get(SuperAssistantMulticaProfileRevision, session.scalar(
        select(SuperAssistantMulticaProfileRevision.id).where(
            SuperAssistantMulticaProfileRevision.profile_id == profile.id,
            SuperAssistantMulticaProfileRevision.revision_no == 1,
        )))
    assert old.retired_at is not None and old.revoked_at is None
    # 编辑后旧 revision 仍按创建时快照解析（历史任务来源不变）
    resolved = multica_service.config_for_revision(session, "user-1", old.id)
    assert resolved.workspace_id == "ws-1"
    assert multica_service.decrypt_token(resolved) == "mul-token"


def test_token_rotation_keeps_old_revision_resolvable_until_explicit_revocation(session):
    profile = _create_custom(session)
    multica_service.activate_profile(session, "user-1", profile.id)
    first = _current_revision(session, profile.id)
    multica_service.update_profile(session, "user-1", profile.id, MulticaProfileUpdate(
        token="rotated-token",
    ))
    second = _current_revision(session, profile.id)
    assert second.revision_no == 2
    assert multica_service.decrypt_token(second) == "rotated-token"
    # 轮换只 retire 不自动 revoke：旧凭据继续可解析
    assert first.retired_at is not None and first.revoked_at is None
    resolved = multica_service.config_for_revision(session, "user-1", first.id)
    assert resolved.id == first.id
    assert multica_service.decrypt_token(resolved) == "mul-token"
    # 显式安全撤销后才拒绝历史解析
    first.revoked_at = datetime.now(timezone.utc)
    session.flush()
    with pytest.raises(multica_service.MulticaProfileConflictError, match="撤销"):
        multica_service.config_for_revision(session, "user-1", first.id)
    resolved = multica_service.config_for_revision(session, "user-1", second.id)
    assert resolved.id == second.id
    # 非凭据变更不 revoke：workspace 改名只 retire
    multica_service.update_profile(session, "user-1", profile.id, MulticaProfileUpdate(
        workspace_name="新名称",
    ))
    assert session.get(SuperAssistantMulticaProfileRevision, second.id).revoked_at is None


def test_soft_deleted_profile_revision_keeps_resolving(session):
    profile = _create_custom(session)
    revision = _current_revision(session, profile.id)
    multica_service.delete_profile(session, "user-1", profile.id)
    resolved = multica_service.config_for_revision(session, "user-1", revision.id)
    assert resolved.id == revision.id
    assert multica_service.decrypt_token(resolved) == "mul-token"
    source = multica_service.config_for_source(
        session, "user-1", base_url="http://127.0.0.1:8080", workspace_id="ws-1",
    )
    assert source.id == revision.id


def test_owner_isolation_hides_profiles_and_revisions(session):
    profile = _create_custom(session)
    multica_service.activate_profile(session, "user-1", profile.id)
    revision = _current_revision(session, profile.id)
    actions = (
        lambda: multica_service.update_profile(
            session, "user-2", profile.id, MulticaProfileUpdate(name="抢注")),
        lambda: multica_service.delete_profile(session, "user-2", profile.id),
        lambda: multica_service.activate_profile(session, "user-2", profile.id),
        lambda: multica_service.test_profile(session, "user-2", profile.id),
        lambda: multica_service.list_profile_workspaces(session, "user-2", profile.id),
        lambda: multica_service.config_for_revision(session, "user-2", revision.id),
    )
    for action in actions:
        with pytest.raises(multica_service.MulticaProfileNotFoundError):
            action()
    assert multica_service.list_profiles(session, "user-2").profiles == []
    assert multica_service.active_config(session, "user-2") is None
    with pytest.raises(multica_service.MulticaProfileNotFoundError):
        multica_service.config_for_source(
            session, "user-2", base_url="http://127.0.0.1:8080", workspace_id="ws-1")
    # user-1 的绑定不受探测影响
    assert multica_service.active_config(session, "user-1").profile_id == profile.id


# ---------------------------------------------------------------------------
# 激活：真实校验链、切换与回落
# ---------------------------------------------------------------------------

def test_activate_switch_updates_projection_without_revoking_old_profile(session):
    first = _create_custom(session, name="A", token="token-a")
    second = _create_custom(session, name="B", token="token-b", workspace_id="ws-b")
    multica_service.activate_profile(session, "user-1", first.id)
    active = multica_service.active_config(session, "user-1")
    assert active.profile_id == first.id and active.workspace_id == "ws-1"

    switched = multica_service.activate_profile(session, "user-1", second.id)
    assert switched.is_active is True
    # 切换不 revoke/retire 老 profile 的 revision
    old_revision = session.scalar(select(SuperAssistantMulticaProfileRevision).where(
        SuperAssistantMulticaProfileRevision.profile_id == first.id))
    assert old_revision.retired_at is None and old_revision.revoked_at is None
    active = multica_service.active_config(session, "user-1")
    assert active.profile_id == second.id
    assert multica_service.decrypt_token(active) == "token-b"
    # 每 owner 仅一个 is_active（部分唯一索引兜底）
    actives = session.scalars(select(SuperAssistantMulticaProfile).where(
        SuperAssistantMulticaProfile.is_active.is_(True))).all()
    assert [item.id for item in actives] == [second.id]


def test_activate_incomplete_profile_conflicts(session):
    profile = _create_custom(session, token=None, enabled=False)
    with pytest.raises(multica_service.MulticaProfileConflictError, match="补全"):
        multica_service.activate_profile(session, "user-1", profile.id)


def test_activate_validates_workspace_membership_and_keeps_previous_active(session, multica_network):
    keeper = _create_custom(session, name="A", token="token-a")
    multica_service.activate_profile(session, "user-1", keeper.id)
    hidden = _create_custom(session, name="X", workspace_id="ws-x", token="token-x")
    with pytest.raises(multica_service.MulticaProfileConflictError, match="无权访问"):
        multica_service.activate_profile(session, "user-1", hidden.id)
    # 409 后旧激活绑定保持不变，未生效 profile 不会被激活
    assert multica_service.active_config(session, "user-1").profile_id == keeper.id
    assert session.get(SuperAssistantMulticaProfile, hidden.id).is_active is False
    # 激活确实执行了 fetch_me + list_workspaces 校验（携带该 revision 凭据）
    assert ("http://127.0.0.1:8080", "token-x") in multica_network["fetch_me"]
    assert ("http://127.0.0.1:8080", "token-x") in multica_network["workspaces"]


def test_activate_connection_failure_conflicts_and_keeps_previous_active(session, monkeypatch):
    keeper = _create_custom(session)
    multica_service.activate_profile(session, "user-1", keeper.id)
    other = _create_custom(session, name="B", workspace_id="ws-b", token="token-b")

    def _unreachable(base_url, token):
        raise multica_client.MulticaClientError("multica 请求失败：HTTP 503")

    monkeypatch.setattr(multica_client, "fetch_me", _unreachable)
    with pytest.raises(multica_service.MulticaProfileConflictError, match="连接验证失败"):
        multica_service.activate_profile(session, "user-1", other.id)
    assert multica_service.active_config(session, "user-1").profile_id == keeper.id


def test_activate_success_stamps_validation_result_on_revision(session):
    profile = _create_custom(session)
    result = multica_service.activate_profile(session, "user-1", profile.id)
    assert result.is_active is True
    assert result.last_test_status == "success"
    assert result.last_test_message == "连接和工作区验证成功"
    assert result.last_tested_at is not None


def test_disabling_active_custom_falls_back_to_platform_default(session, monkeypatch):
    _platform_defaults(monkeypatch)
    profile = _create_custom(session)
    multica_service.activate_profile(session, "user-1", profile.id)
    assert multica_service.active_config(session, "user-1").profile_id == profile.id

    updated = multica_service.update_profile(
        session, "user-1", profile.id, MulticaProfileUpdate(enabled=False))
    assert updated.enabled is False and updated.is_active is False
    active = multica_service.active_config(session, "user-1")
    assert active.profile_id != profile.id
    assert active.source_kind == "platform_default"
    assert active.base_url == DEFAULTS["base_url"]
    listing = multica_service.list_profiles(session, "user-1")
    assert listing.active_profile_id == listing.default_profile_id


def test_delete_active_custom_falls_back_to_platform_default(session, monkeypatch):
    _platform_defaults(monkeypatch)
    profile = _create_custom(session)
    multica_service.activate_profile(session, "user-1", profile.id)
    assert multica_service.active_config(session, "user-1").profile_id == profile.id

    multica_service.delete_profile(session, "user-1", profile.id)
    listing = multica_service.list_profiles(session, "user-1")
    assert [item.kind for item in listing.profiles] == ["platform_default"]
    assert listing.active_profile_id == listing.default_profile_id
    active = multica_service.active_config(session, "user-1")
    assert active is not None and active.source_kind == "platform_default"
    assert active.base_url == DEFAULTS["base_url"]
    assert multica_service.decrypt_token(active) == DEFAULTS["token"]
    # 软删除：行保留、不再可见
    row = session.get(SuperAssistantMulticaProfile, profile.id)
    assert row.deleted_at is not None and row.is_active is False


def test_delete_without_platform_default_disables_projection(session):
    profile = _create_custom(session)
    multica_service.activate_profile(session, "user-1", profile.id)
    assert multica_service.active_config(session, "user-1") is not None
    multica_service.delete_profile(session, "user-1", profile.id)
    assert multica_service.active_config(session, "user-1") is None
    config = multica_service.get_config(session, "user-1")
    assert config.enabled is False and config.profile_id is None


# ---------------------------------------------------------------------------
# 平台默认：派生、只读、设置变更与生命周期
# ---------------------------------------------------------------------------

def test_platform_default_derived_from_settings_and_immutable(session, monkeypatch):
    _platform_defaults(monkeypatch)
    listing = multica_service.list_profiles(session, "user-1")
    assert listing.default_profile_id is not None
    # 无生效 custom 时平台默认被自动选为激活（两者 ID 一致）
    assert listing.active_profile_id == listing.default_profile_id
    default = listing.profiles[0]
    assert default.kind == "platform_default" and default.builtin_key == "default"
    assert default.is_active is True
    assert listing.effective.enabled is True
    assert listing.effective.base_url == DEFAULTS["base_url"]
    assert multica_service.decrypt_token(
        multica_service.active_config(session, "user-1")) == DEFAULTS["token"]

    with pytest.raises(multica_service.MulticaProfileConflictError, match="不可修改"):
        multica_service.update_profile(session, "user-1", default.id, MulticaProfileUpdate(
            name="改个名",
        ))
    with pytest.raises(multica_service.MulticaProfileConflictError, match="不可删除"):
        multica_service.delete_profile(session, "user-1", default.id)


def test_platform_default_refresh_appends_revision_on_settings_change(session, monkeypatch):
    source = _platform_defaults(monkeypatch)
    listing = multica_service.list_profiles(session, "user-1")
    first = _current_revision(session, listing.default_profile_id)
    assert first.revision_no == 1
    # 相同设置不需要写事务，也不追加 revision。
    assert multica_service.sync_deployment_default(session, "user-1") is False
    assert _current_revision(session, listing.default_profile_id).id == first.id
    # token 轮换：追加 revision 2；旧 revision 仅 retire，凭据仍可解析
    source["token"] = "platform-token-2"
    assert multica_service.sync_deployment_default(session, "user-1") is True
    second = _current_revision(session, listing.default_profile_id)
    assert second.revision_no == 2
    assert first.retired_at is not None and first.revoked_at is None
    assert multica_service.decrypt_token(second) == "platform-token-2"
    resolved = multica_service.config_for_revision(session, "user-1", first.id)
    assert multica_service.decrypt_token(resolved) == DEFAULTS["token"]


def test_platform_default_settings_removal_stops_default_keeps_history_readd_singleton(
    session, monkeypatch,
):
    source = _platform_defaults(monkeypatch)
    listing = multica_service.list_profiles(session, "user-1")
    default_id = listing.default_profile_id
    assert listing.active_profile_id == default_id
    first = _current_revision(session, default_id)

    # 部署设置清空：默认停用且不可见，生效投影关闭
    source.update({key: "" for key in DEFAULTS})
    stopped = multica_service.list_profiles(session, "user-1")
    assert stopped.profiles == [] and stopped.active_profile_id is None
    assert stopped.effective.enabled is False
    assert multica_service.active_config(session, "user-1") is None
    row = session.get(SuperAssistantMulticaProfile, default_id)
    assert row.deleted_at is not None and row.is_active is False

    # 历史保留：revision 行仍在且按旧凭据可解析
    resolved = multica_service.config_for_revision(session, "user-1", first.id)
    assert multica_service.decrypt_token(resolved) == DEFAULTS["token"]

    # 重新配置：复活同一 singleton 行，不产生重复平台默认，配置不变不追加 revision
    source.update(DEFAULTS)
    rebound = multica_service.list_profiles(session, "user-1")
    default_rows = session.scalars(select(SuperAssistantMulticaProfile).where(
        SuperAssistantMulticaProfile.kind == "platform_default")).all()
    assert [item.id for item in default_rows] == [default_id]
    assert rebound.default_profile_id == default_id
    assert rebound.active_profile_id == default_id
    assert _current_revision(session, default_id).id == first.id


def test_no_platform_default_when_settings_incomplete(session, monkeypatch):
    _no_platform_defaults(monkeypatch)
    listing = multica_service.list_profiles(session, "user-1")
    assert listing.default_profile_id is None and listing.profiles == []


def test_platform_default_settings_dict_has_five_keys_including_web_url(monkeypatch):
    from app.shared.config import settings

    monkeypatch.setattr(settings, "multica_default_base_url", "  https://multica.example.com  ")
    monkeypatch.setattr(settings, "multica_default_web_url", " https://multica.example.com/web ")
    monkeypatch.setattr(settings, "multica_default_workspace_id", " ws-uuid ")
    monkeypatch.setattr(settings, "multica_default_workspace_name", " 平台工作区 ")
    monkeypatch.setattr(settings, "multica_default_pat", " platform-pat ")
    source = multica_service._platform_default_settings()
    assert set(source) == {"base_url", "web_url", "workspace_id", "workspace_name", "token"}
    assert source["web_url"] == "https://multica.example.com/web"
    assert all(value == value.strip() for value in source.values())


def test_unknown_profile_operations_return_not_found(session):
    with pytest.raises(multica_service.MulticaProfileNotFoundError):
        multica_service.update_profile(session, "user-1", "missing", MulticaProfileUpdate(name="x"))
    with pytest.raises(multica_service.MulticaProfileNotFoundError):
        multica_service.delete_profile(session, "user-1", "missing")
    with pytest.raises(multica_service.MulticaProfileNotFoundError):
        multica_service.config_for_revision(session, "user-1", "missing")


# ---------------------------------------------------------------------------
# legacy PUT / GET 在多 profile 下的兼容
# ---------------------------------------------------------------------------

def test_legacy_put_creates_and_activates_custom_over_platform_default(session, monkeypatch):
    _platform_defaults(monkeypatch)
    config = multica_service.save_config(session, "user-1", MulticaConfigUpdate(
        base_url="http://127.0.0.1:8080", token="mul-secret",
        workspace_id="ws-1", workspace_name="My Workspace", enabled=True,
    ))
    assert config.source_kind == "custom" and config.enabled is True
    # 平台默认未被修改：revision 仍为 1
    listing = multica_service.list_profiles(session, "user-1")
    default_revision = _current_revision(session, listing.default_profile_id)
    assert default_revision.revision_no == 1
    assert listing.active_profile_id != listing.default_profile_id
    active = multica_service.active_config(session, "user-1")
    assert multica_service.decrypt_token(active) == "mul-secret"


def test_legacy_put_updates_active_custom_without_token_overwrite(session):
    profile = _create_custom(session)
    multica_service.activate_profile(session, "user-1", profile.id)
    multica_service.save_config(session, "user-1", MulticaConfigUpdate(
        base_url="http://127.0.0.1:8080", workspace_id="ws-9", enabled=True,
    ))
    revision = _current_revision(session, profile.id)
    assert revision.revision_no == 2 and revision.workspace_id == "ws-9"
    assert multica_service.decrypt_token(revision) == "mul-token"


def test_legacy_put_identical_payload_without_token_appends_no_revision(session):
    saved = multica_service.save_config(session, "user-1", MulticaConfigUpdate(
        base_url="http://127.0.0.1:8080", token="mul-secret",
        workspace_id="ws-1", workspace_name="My Workspace", enabled=True,
    ))
    again = multica_service.save_config(session, "user-1", MulticaConfigUpdate(
        base_url="http://127.0.0.1:8080", workspace_id="ws-1",
        workspace_name="My Workspace", enabled=True,
    ))
    assert again.profile_id == saved.profile_id
    revision = _current_revision(session, saved.profile_id)
    assert revision.revision_no == 1
    assert multica_service.decrypt_token(revision) == "mul-secret"
    assert multica_service.active_config(session, "user-1").profile_id == saved.profile_id


def test_legacy_get_returns_effective_platform_default_view(session, monkeypatch):
    _platform_defaults(monkeypatch)
    view = multica_service.read_config(session, "user-1")
    assert view.configured is True and view.enabled is True
    assert view.base_url == DEFAULTS["base_url"]
    assert view.workspace_name == DEFAULTS["workspace_name"]
    assert "token" not in view.model_dump()


def test_read_config_unconfigured_returns_complete_disabled_view(session, monkeypatch):
    _no_platform_defaults(monkeypatch)
    view = multica_service.read_config(session, "user-1")
    assert view.configured is False and view.enabled is False
    assert view.base_url == "" and view.workspace_id == ""
    assert view.workspace_name == "" and view.token_set is False
    assert view.commands == []
    assert view.last_test_status is None


# ---------------------------------------------------------------------------
# 事务边界与来源一致性
# ---------------------------------------------------------------------------

def test_default_sync_is_explicit_and_rolls_back_with_caller(session, monkeypatch):
    _platform_defaults(monkeypatch)
    session.add(User(
        id="stranger", username="stranger", email="stranger@example.com",
        password_hash="unused", role="viewer",
    ))
    session.flush()
    assert multica_service.get_config(session, "user-1") is None
    assert multica_service.active_config(session, "user-1") is None
    assert session.scalars(select(SuperAssistantMulticaProfile)).all() == []

    assert multica_service.sync_deployment_default(session, "user-1") is True
    assert multica_service.active_config(session, "user-1").profile_revision_id
    session.rollback()
    with sessionmaker(bind=session.get_bind())() as fresh:
        assert fresh.scalars(select(SuperAssistantMulticaProfile)).all() == []
        assert fresh.scalars(select(SuperAssistantMulticaProfileRevision)).all() == []
        assert fresh.scalars(select(SuperAssistantMulticaConfig)).all() == []
        assert fresh.get(User, "stranger") is None


@pytest.mark.parametrize("configured", [False, True])
def test_stable_config_reads_skip_owner_lock(session, monkeypatch, configured):
    if configured:
        _platform_defaults(monkeypatch)
        multica_service.read_config(session, "user-1")
    else:
        _no_platform_defaults(monkeypatch)

    def unexpected_lock(*_args):
        pytest.fail("stable configuration must not lock the user row")

    monkeypatch.setattr(multica_service, "_lock_owner", unexpected_lock)
    assert multica_service.sync_deployment_default(session, "user-1") is False
    assert (multica_service.active_config(session, "user-1") is not None) is configured
    assert multica_service.read_config(session, "user-1").enabled is configured


def test_conflicting_default_change_leaves_custom_and_historical_default_usable(session, monkeypatch):
    deployment = _platform_defaults(monkeypatch)
    original = multica_service.list_profiles(session, "user-1")
    old_default = multica_service.active_config(session, "user-1").profile_revision_id
    custom = _create_custom(session)
    multica_service.activate_profile(session, "user-1", custom.id)

    deployment["base_url"] = "http://127.0.0.1:9999"
    listing = multica_service.list_profiles(session, "user-1")
    assert listing.default_profile_id is None
    assert listing.active_profile_id == custom.id
    assert listing.effective.base_url == custom.base_url
    assert multica_service.active_config(session, "user-1").profile_id == custom.id
    old_source = multica_service.config_for_revision(session, "user-1", old_default)
    assert old_source.base_url == DEFAULTS["base_url"]
    assert multica_service.decrypt_token(old_source) == DEFAULTS["token"]

    deployment.update(DEFAULTS)
    restored = multica_service.list_profiles(session, "user-1")
    assert restored.default_profile_id == original.default_profile_id
    assert restored.active_profile_id == custom.id


def test_new_owner_custom_remains_usable_when_default_workspace_conflicts(session, monkeypatch):
    custom = _create_custom(session)
    multica_service.activate_profile(session, "user-1", custom.id)
    _platform_defaults(monkeypatch, workspace_id=custom.workspace_id)
    result = multica_service.list_profiles(session, "user-1")
    assert result.default_profile_id is None
    assert result.active_profile_id == custom.id
    assert multica_service.read_config(session, "user-1").enabled
    # The deployment default still becomes available for a different owner.
    assert multica_service.list_profiles(session, "user-2").default_profile_id is not None


@pytest.mark.parametrize("operation", ["activate_profile", "test_profile"])
def test_remote_validation_does_not_write_back_to_a_concurrently_changed_profile(
    session, monkeypatch, operation,
):
    custom = _create_custom(session)
    first = _current_revision(session, custom.id)
    new_session = sessionmaker(bind=session.get_bind(), autoflush=False)

    def rotate_while_waiting(base_url, token):
        assert token == "mul-token"
        assert not session.in_transaction()
        with new_session() as other:
            multica_service.update_profile(other, "user-1", custom.id, MulticaProfileUpdate(token="rotated"))
        return {"name": "fixture-user"}

    monkeypatch.setattr(multica_client, "fetch_me", rotate_while_waiting)
    invoke = getattr(multica_service, operation)
    if operation == "activate_profile":
        with pytest.raises(multica_service.MulticaProfileConflictError, match="已变更"):
            invoke(session, "user-1", custom.id)
        session.rollback()
    else:
        invoke(session, "user-1", custom.id)

    current = _current_revision(session, custom.id)
    assert current.id != first.id and current.revision_no == 2
    assert multica_service.decrypt_token(current) == "rotated"
    assert current.last_test_status is None
    assert multica_service.active_config(session, "user-1") is None


def test_revoked_default_is_not_reenabled_by_workspace_rename(session, monkeypatch):
    deployment = _platform_defaults(monkeypatch)
    current = multica_service.active_config(session, "user-1")
    assert current is None
    multica_service.read_config(session, "user-1")
    first_id = multica_service.active_config(session, "user-1").profile_revision_id
    session.get(SuperAssistantMulticaProfileRevision, first_id).revoked_at = datetime.now(timezone.utc)
    session.commit()

    deployment["workspace_name"] = "renamed"
    assert multica_service.read_config(session, "user-1").enabled is False
    assert multica_service.active_config(session, "user-1") is None
    deployment["token"] = "rotated"
    assert multica_service.read_config(session, "user-1").enabled is True
    assert multica_service.decrypt_token(multica_service.active_config(session, "user-1")) == "rotated"


def test_workspace_source_collision_enforced_across_soft_deleted_history(session):
    doomed = _create_custom(session, name="旧绑定", workspace_id="ws-collide")
    multica_service.delete_profile(session, "user-1", doomed.id)
    # 软删除后的历史仍参与“工作区 ID ↔ 服务地址”一致性检查
    with pytest.raises(multica_service.MulticaProfileConflictError, match="已绑定其他"):
        _create_custom(session, name="换址重绑", workspace_id="ws-collide",
                       base_url="http://127.0.0.1:9999")
    # 同地址复用同一工作区不受影响
    reborn = _create_custom(session, name="同址复用", workspace_id="ws-collide")
    assert reborn.workspace_id == "ws-collide"


def test_draft_test_with_different_url_never_uses_stored_token(session, multica_network):
    profile = _create_custom(session)
    revision = _current_revision(session, profile.id)
    # 草稿换了服务地址且未填 token：直接拒绝，不回放已存凭据、不发起请求
    refused = multica_service.test_profile(
        session, "user-1", profile.id, base_url="http://127.0.0.1:9999", token=None)
    assert refused.ok is False and "API Token" in refused.message
    assert multica_network["fetch_me"] == []

    # 显式草稿 token + 不同地址：只携带草稿凭据请求，且不落任何测试状态
    drafted = multica_service.test_profile(
        session, "user-1", profile.id, base_url="http://127.0.0.1:9999", token="draft-token")
    assert drafted.ok is True
    assert ("http://127.0.0.1:9999", "draft-token") in multica_network["fetch_me"]
    fresh = session.get(SuperAssistantMulticaProfileRevision, revision.id)
    assert fresh.last_test_status is None and fresh.last_tested_at is None

    # 相同地址留空 token：回退已存凭据执行并记录测试状态
    saved = multica_service.test_profile(session, "user-1", profile.id, token=None)
    assert saved.ok is True
    stored_calls = [call for call in multica_network["fetch_me"] if call[1] == "mul-token"]
    assert stored_calls == [("http://127.0.0.1:8080", "mul-token")]
    assert _current_revision(session, profile.id).last_test_status == "success"
