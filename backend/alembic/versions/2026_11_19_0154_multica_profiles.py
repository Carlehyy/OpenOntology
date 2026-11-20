"""super assistant multica multi-profile support

multica 外部集成多 profile 化：

- 新表 ``super_assistant_multica_profiles``（每用户多套命名绑定，每
  owner 至多一个 is_active 与一个 platform_default，部分唯一索引）与
  ``super_assistant_multica_profile_revisions``（不可变配置版本，每
  profile 至多一个未 retired 的 current revision，部分唯一索引）；
- 旧表 ``super_assistant_multica_configs`` 追加可空投影列
  profile_id/profile_revision_id/source_kind，降级为 effective legacy
  projection 供旧 runtime 继续读取；
- 存量旧行回填为 custom profile + revision 1 + 指针投影；disabled 或
  字段不齐（unusable）的行同样保留数据，但 is_active 回填为 false。
- ``super_assistant_execution_calls`` 追加可空
  multica_profile_revision_id。存量 Kernel 调用不回填来源绑定：迁移无法
  从冻结能力摘要证明某次调用当时绑定的 revision，宁可为空（旧调用保持
  历史 active-config 语义），由 Kernel 侧 fail-closed 处理。

迁移保持 0089/0093/0095 同一防御口径：users 或旧表缺失时跳过对应
DDL/回填（迁移测试场景只手工建被测表并 stamp 到中间版本），真实库与
全量 upgrade 正常执行；全部步骤幂等可逆。

降级口径（可表示性校验，全部在任何 DDL 之前完成，脚本不删除、不导出
数据）：旧模型只能表达"每 owner 一行 legacy 配置"。仅当每 owner 至多
一个 custom profile（未软删、非 platform_default/builtin）、该 profile
至多一个 revision（未 retired/revoked）、且该 owner 存在 legacy 行可
承接投影时才允许降级，并把 revision 的 base_url/workspace/token/测试
记录投影回旧行（enabled = revision.enabled AND profile.is_active）。
多 profile、多 revision、软删、platform_default、孤儿 revision、指针
错位，或存在来源绑定无法解析 / 状态未 closed / 结局 outcome_unknown
的执行调用，一律拒绝并提示先人工导出清理；拒绝时库结构零改动。

Revision ID: 0154_multica_profiles
Revises: 0153_weknora_operations_merge
Create Date: 2026-11-19
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect as sa_inspect


revision = "0154_multica_profiles"
down_revision = "0153_weknora_operations_merge"
branch_labels = None
depends_on = None

_PROFILES_TABLE = "super_assistant_multica_profiles"
_REVISIONS_TABLE = "super_assistant_multica_profile_revisions"
_LEGACY_TABLE = "super_assistant_multica_configs"
_EXECUTION_CALLS_TABLE = "super_assistant_execution_calls"
_EXECUTION_SOURCE_COLUMN = "multica_profile_revision_id"

_PROJECTION_COLUMNS = (
    sa.Column("profile_id", sa.String(length=64), nullable=True),
    sa.Column("profile_revision_id", sa.String(length=64), nullable=True),
    sa.Column("source_kind", sa.String(length=20), nullable=True),
)

# 与 models.py SuperAssistantMulticaProfile/Revision 的显式索引同名同谓词。
_PROFILE_INDEXES = (
    (
        "ix_sa_multica_profiles_owner_id",
        ["owner_id"],
        False,
        None,
        None,
    ),
    (
        "ix_sa_multica_profiles_owner_kind",
        ["owner_id", "kind"],
        False,
        None,
        None,
    ),
    (
        "uq_sa_multica_profile_owner_active",
        ["owner_id"],
        True,
        sa.text("is_active = 1"),
        sa.text("is_active IS TRUE"),
    ),
    (
        "uq_sa_multica_profile_owner_default",
        ["owner_id"],
        True,
        sa.text("kind = 'platform_default'"),
        sa.text("kind = 'platform_default'"),
    ),
)

_REVISION_INDEXES = (
    (
        # 名称与 ORM 一致：SuperAssistantMulticaProfileRevision.profile_id 的
        # index=True 生成 ix_<table>_<column>。0003 的 create_all 在全新链上
        # 已按 ORM 建出同名索引，这里用任何其它名字都会在 profile_id 上建出
        # 重复索引（写入放大且偏离模型/迁移 parity）。
        "ix_super_assistant_multica_profile_revisions_profile_id",
        ["profile_id"],
        False,
        None,
        None,
    ),
    (
        "ix_sa_multica_profile_revisions_profile_no",
        ["profile_id", "revision_no"],
        False,
        None,
        None,
    ),
    (
        "uq_sa_multica_profile_current_revision",
        ["profile_id"],
        True,
        sa.text("retired_at IS NULL"),
        sa.text("retired_at IS NULL"),
    ),
)

_CLOSED_STATUS = "closed"
_UNKNOWN_OUTCOME = "outcome_unknown"


def _table_names(bind) -> set[str]:
    return set(sa_inspect(bind).get_table_names())


def _columns(bind, table: str) -> set[str]:
    return {column["name"] for column in sa_inspect(bind).get_columns(table)}


def _index_names(bind, table: str) -> set[str]:
    return {index["name"] for index in sa_inspect(bind).get_indexes(table)}


def _create_indexes(table: str, spec: tuple) -> None:
    """Create declared indexes by name (op.create_table builds no indexes)."""
    existing = _index_names(op.get_bind(), table)
    for name, columns, unique, sqlite_where, postgresql_where in spec:
        if name in existing:
            continue
        op.create_index(
            name, table, columns,
            unique=unique,
            sqlite_where=sqlite_where,
            postgresql_where=postgresql_where,
        )


def upgrade() -> None:
    bind = op.get_bind()
    tables = _table_names(bind)

    # 与 0093 同一防御口径：合成测试库可能没有 users 表，跳过全部 DDL。
    if "users" not in tables:
        return

    if _EXECUTION_CALLS_TABLE in tables:
        execution_columns = _columns(bind, _EXECUTION_CALLS_TABLE)
        if _EXECUTION_SOURCE_COLUMN not in execution_columns:
            op.add_column(
                _EXECUTION_CALLS_TABLE,
                sa.Column(_EXECUTION_SOURCE_COLUMN, sa.String(length=255), nullable=True),
            )
        # 存量调用不回填来源绑定：迁移无法证明调用当时的 revision 归属
        # （冻结能力摘要不在迁移可达范围内），猜测绑定会伪造审计来源。
        # 保持 NULL，旧调用沿用历史 active-config 语义，由 Kernel 侧
        # fail-closed 兜底。

    if _PROFILES_TABLE not in tables:
        op.create_table(
            _PROFILES_TABLE,
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("owner_id", sa.String(), nullable=False),
            sa.Column("name", sa.String(length=100), nullable=False),
            sa.Column("kind", sa.String(length=20), nullable=False),
            sa.Column("builtin_key", sa.String(length=50), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("deleted_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
    _create_indexes(_PROFILES_TABLE, _PROFILE_INDEXES)

    if _REVISIONS_TABLE not in tables:
        op.create_table(
            _REVISIONS_TABLE,
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("profile_id", sa.String(), nullable=False),
            sa.Column("revision_no", sa.Integer(), nullable=False),
            sa.Column("base_url", sa.String(length=500), nullable=False),
            sa.Column("workspace_id", sa.String(length=100), nullable=False),
            sa.Column("workspace_name", sa.String(length=200), nullable=False),
            sa.Column("token_encrypted", sa.Text(), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("last_test_status", sa.String(length=20), nullable=True),
            sa.Column("last_test_message", sa.String(length=500), nullable=True),
            sa.Column("last_tested_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("retired_at", sa.DateTime(), nullable=True),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(
                ["profile_id"], ["super_assistant_multica_profiles.id"], ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "profile_id", "revision_no", name="uq_sa_multica_profile_revision_no",
            ),
        )
    _create_indexes(_REVISIONS_TABLE, _REVISION_INDEXES)

    if _LEGACY_TABLE not in tables:
        return

    legacy_columns = _columns(bind, _LEGACY_TABLE)
    for column in _PROJECTION_COLUMNS:
        if column.name not in legacy_columns:
            op.add_column(_LEGACY_TABLE, column)

    # 存量旧行 → custom profile + revision 1 + 指针投影（幂等：已回填
    # （profile_id 非空）或空桩行跳过）。凭据密文原样迁移（同一加密
    # 密钥），旧行本体继续作为生效投影保留。disabled 行同样保留数据，
    # 但 unusable（未启用或字段不齐）时 is_active 回填 false，与服务层
    # _revision_usable 的生效判定对齐。
    legacy = sa.table(
        _LEGACY_TABLE,
        sa.column("owner_id", sa.String),
        sa.column("base_url", sa.String),
        sa.column("workspace_id", sa.String),
        sa.column("workspace_name", sa.String),
        sa.column("token_encrypted", sa.Text),
        sa.column("enabled", sa.Boolean),
        sa.column("last_test_status", sa.String),
        sa.column("last_test_message", sa.String),
        sa.column("last_tested_at", sa.DateTime),
        sa.column("profile_id", sa.String),
        sa.column("profile_revision_id", sa.String),
        sa.column("source_kind", sa.String),
    )
    profiles = sa.table(
        _PROFILES_TABLE,
        sa.column("id", sa.String),
        sa.column("owner_id", sa.String),
        sa.column("name", sa.String),
        sa.column("kind", sa.String),
        sa.column("builtin_key", sa.String),
        sa.column("is_active", sa.Boolean),
        sa.column("deleted_at", sa.DateTime),
        sa.column("created_at", sa.DateTime),
        sa.column("updated_at", sa.DateTime),
    )
    revisions = sa.table(
        _REVISIONS_TABLE,
        sa.column("id", sa.String),
        sa.column("profile_id", sa.String),
        sa.column("revision_no", sa.Integer),
        sa.column("base_url", sa.String),
        sa.column("workspace_id", sa.String),
        sa.column("workspace_name", sa.String),
        sa.column("token_encrypted", sa.Text),
        sa.column("enabled", sa.Boolean),
        sa.column("last_test_status", sa.String),
        sa.column("last_test_message", sa.String),
        sa.column("last_tested_at", sa.DateTime),
        sa.column("created_at", sa.DateTime),
        sa.column("retired_at", sa.DateTime),
        sa.column("revoked_at", sa.DateTime),
    )

    rows = bind.execute(
        sa.select(
            legacy.c.owner_id, legacy.c.base_url, legacy.c.workspace_id,
            legacy.c.workspace_name, legacy.c.token_encrypted, legacy.c.enabled,
            legacy.c.last_test_status, legacy.c.last_test_message,
            legacy.c.last_tested_at, legacy.c.profile_id,
        )
    ).all()
    now = datetime.now(timezone.utc)
    for row in rows:
        base_url = (row.base_url or "").strip()
        workspace_id = (row.workspace_id or "").strip()
        has_data = bool(base_url or workspace_id or row.token_encrypted)
        if row.profile_id or not has_data:
            continue
        usable = bool(
            row.enabled and base_url and workspace_id and row.token_encrypted
        )
        profile_id = str(uuid.uuid4())
        revision_id = str(uuid.uuid4())
        bind.execute(profiles.insert().values(
            id=profile_id, owner_id=row.owner_id, name="我的 multica",
            kind="custom", builtin_key=None, is_active=usable, deleted_at=None,
            created_at=now, updated_at=now,
        ))
        bind.execute(revisions.insert().values(
            id=revision_id, profile_id=profile_id, revision_no=1,
            base_url=row.base_url or "", workspace_id=row.workspace_id or "",
            workspace_name=row.workspace_name or "",
            token_encrypted=row.token_encrypted,
            enabled=bool(row.enabled),
            last_test_status=row.last_test_status,
            last_test_message=row.last_test_message,
            last_tested_at=row.last_tested_at,
            created_at=now, retired_at=None, revoked_at=None,
        ))
        bind.execute(
            legacy.update().where(legacy.c.owner_id == row.owner_id).values(
                profile_id=profile_id, profile_revision_id=revision_id,
                source_kind="custom",
            )
        )


def _reject(reason: str) -> None:
    raise RuntimeError(
        f"cannot downgrade 0154_multica_profiles: {reason}; "
        "export/remove the affected profiles and resolve their calls "
        "manually first (this migration never deletes or exports data)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    tables = _table_names(bind)

    # 与 upgrade 对称：合成测试库没有 users 表时本迁移未建任何对象。
    if "users" not in tables:
        return

    # ---- 可表示性校验：全部在任何 DDL 之前完成，拒绝时结构零改动。 ----
    profiles_exist = _PROFILES_TABLE in tables
    revisions_exist = _REVISIONS_TABLE in tables
    legacy_exists = _LEGACY_TABLE in tables

    legacy = sa.table(
        _LEGACY_TABLE,
        sa.column("owner_id", sa.String),
        sa.column("base_url", sa.String),
        sa.column("workspace_id", sa.String),
        sa.column("workspace_name", sa.String),
        sa.column("token_encrypted", sa.Text),
        sa.column("enabled", sa.Boolean),
        sa.column("last_test_status", sa.String),
        sa.column("last_test_message", sa.String),
        sa.column("last_tested_at", sa.DateTime),
        sa.column("updated_at", sa.DateTime),
        sa.column("profile_id", sa.String),
        sa.column("profile_revision_id", sa.String),
        sa.column("source_kind", sa.String),
    )
    profiles = sa.table(
        _PROFILES_TABLE,
        sa.column("id", sa.String),
        sa.column("owner_id", sa.String),
        sa.column("name", sa.String),
        sa.column("kind", sa.String),
        sa.column("builtin_key", sa.String),
        sa.column("is_active", sa.Boolean),
        sa.column("deleted_at", sa.DateTime),
    )
    revisions = sa.table(
        _REVISIONS_TABLE,
        sa.column("id", sa.String),
        sa.column("profile_id", sa.String),
        sa.column("revision_no", sa.Integer),
        sa.column("base_url", sa.String),
        sa.column("workspace_id", sa.String),
        sa.column("workspace_name", sa.String),
        sa.column("token_encrypted", sa.Text),
        sa.column("enabled", sa.Boolean),
        sa.column("last_test_status", sa.String),
        sa.column("last_test_message", sa.String),
        sa.column("last_tested_at", sa.DateTime),
        sa.column("retired_at", sa.DateTime),
        sa.column("revoked_at", sa.DateTime),
    )

    profile_rows: list = []
    revisions_by_profile: dict[str, list] = {}
    if profiles_exist:
        profile_rows = bind.execute(sa.select(
            profiles.c.id, profiles.c.owner_id, profiles.c.name, profiles.c.kind,
            profiles.c.builtin_key, profiles.c.is_active, profiles.c.deleted_at,
        )).all()
    if revisions_exist and not profiles_exist:
        orphan_count = bind.execute(
            sa.select(sa.func.count()).select_from(revisions)
        ).scalar_one()
        if orphan_count:
            _reject(
                f"{_REVISIONS_TABLE} holds {orphan_count} revision(s) without a "
                f"{_PROFILES_TABLE} table (orphan revisions would be lost)"
            )
    if revisions_exist and profile_rows:
        profile_ids = {row.id for row in profile_rows}
        for row in bind.execute(sa.select(
            revisions.c.id, revisions.c.profile_id, revisions.c.revision_no,
            revisions.c.base_url, revisions.c.workspace_id,
            revisions.c.workspace_name, revisions.c.token_encrypted,
            revisions.c.enabled, revisions.c.last_test_status,
            revisions.c.last_test_message, revisions.c.last_tested_at,
            revisions.c.retired_at, revisions.c.revoked_at,
        )).all():
            revisions_by_profile.setdefault(row.profile_id, []).append(row)
        orphans = [
            profile_id for profile_id in revisions_by_profile
            if profile_id not in profile_ids
        ]
        if orphans:
            _reject(
                f"{len(orphans)} profile(s) have revisions but no profile row "
                "(orphan revisions would be lost)"
            )

    profiles_by_owner: dict[str, list] = {}
    for row in profile_rows:
        profiles_by_owner.setdefault(row.owner_id, []).append(row)

    legacy_by_owner: dict[str, object] = {}
    if legacy_exists:
        legacy_columns = _columns(bind, _LEGACY_TABLE)
        legacy_cols = [legacy.c.owner_id]
        if "profile_id" in legacy_columns:
            legacy_cols.append(legacy.c.profile_id)
        if "profile_revision_id" in legacy_columns:
            legacy_cols.append(legacy.c.profile_revision_id)
        for row in bind.execute(sa.select(*legacy_cols)).all():
            legacy_by_owner[row.owner_id] = row

    surviving: dict[str, tuple] = {}
    for owner_id, owner_profiles in profiles_by_owner.items():
        if len(owner_profiles) > 1:
            _reject(f"owner {owner_id} has {len(owner_profiles)} profiles; the legacy schema holds exactly one")
        profile = owner_profiles[0]
        if profile.deleted_at is not None:
            _reject(f"profile {profile.id} of owner {owner_id} is soft-deleted but still carries data")
        if profile.kind != "custom" or profile.builtin_key is not None:
            _reject(
                f"profile {profile.id} of owner {owner_id} has kind="
                f"{profile.kind!r}/builtin_key={profile.builtin_key!r}; "
                "platform-default bindings have no legacy representation"
            )
        owner_revisions = revisions_by_profile.get(profile.id, [])
        if len(owner_revisions) > 1:
            _reject(
                f"profile {profile.id} of owner {owner_id} has "
                f"{len(owner_revisions)} revisions; the legacy schema keeps no history"
            )
        revision = owner_revisions[0] if owner_revisions else None
        if revision is not None and (revision.retired_at is not None or revision.revoked_at is not None):
            _reject(
                f"revision {revision.id} of profile {profile.id} is retired/revoked; "
                "its lifecycle metadata has no legacy representation"
            )
        if legacy_exists:
            legacy_row = legacy_by_owner.get(owner_id)
            if legacy_row is None:
                _reject(
                    f"profile {profile.id} of owner {owner_id} has no "
                    f"{_LEGACY_TABLE} row to project onto"
                )
            if revision is not None:
                if getattr(legacy_row, "profile_id", None) not in (None, profile.id):
                    _reject(
                        f"legacy row of owner {owner_id} points at profile "
                        f"{legacy_row.profile_id}, not the surviving {profile.id}"
                    )
                if getattr(legacy_row, "profile_revision_id", None) not in (None, revision.id):
                    _reject(
                        f"legacy row of owner {owner_id} points at revision "
                        f"{legacy_row.profile_revision_id}, not the surviving {revision.id}"
                    )
        surviving[owner_id] = (profile, revision)

    # ---- 执行调用来源守卫：绑定无法解析 / 未 closed / 结局未知的调用，
    # 降级会丢失其解析来源，必须在 DDL 前拒绝。未绑定调用保持历史
    # active-config 语义，不受影响。 ----
    if _EXECUTION_CALLS_TABLE in tables:
        execution_columns = _columns(bind, _EXECUTION_CALLS_TABLE)
        if _EXECUTION_SOURCE_COLUMN in execution_columns:
            calls = sa.table(
                _EXECUTION_CALLS_TABLE,
                sa.column("id", sa.String),
                sa.column(_EXECUTION_SOURCE_COLUMN, sa.String),
                sa.column("status", sa.String),
                sa.column("outcome", sa.String),
            )
            source_column = calls.c[_EXECUTION_SOURCE_COLUMN]
            surviving_revision_ids = {
                revision.id for _, revision in surviving.values()
                if revision is not None
            }
            bound_calls = bind.execute(sa.select(
                calls.c.id,
                source_column,
                calls.c.status,
                calls.c.outcome,
            ).where(source_column.is_not(None))).all()
            for call in bound_calls:
                bound_revision = getattr(call, _EXECUTION_SOURCE_COLUMN)
                unresolvable = bound_revision not in surviving_revision_ids
                unresolved_state = (
                    call.status != _CLOSED_STATUS
                    or call.outcome == _UNKNOWN_OUTCOME
                )
                if unresolvable or unresolved_state:
                    detail = (
                        "its source revision no longer resolves"
                        if unresolvable
                        else f"its status/outcome is unresolved (status={call.status!r}, "
                            f"outcome={call.outcome!r})"
                    )
                    _reject(
                        f"execution call {call.id} is bound to multica revision "
                        f"{bound_revision}, but {detail}"
                    )

    # ---- 投影：revision 的来源/字段/凭据/测试记录写回旧-row，
    # enabled 语义 = revision.enabled AND profile.is_active。 ----
    if legacy_exists and surviving:
        legacy_columns = _columns(bind, _LEGACY_TABLE)
        projection_fields = (
            "base_url", "workspace_id", "workspace_name", "token_encrypted",
            "last_test_status", "last_test_message", "last_tested_at",
        )
        now = datetime.now(timezone.utc)
        for owner_id, (profile, revision) in surviving.items():
            if revision is None:
                continue
            values = {
                field: getattr(revision, field)
                for field in projection_fields
                if field in legacy_columns
            }
            if "enabled" in legacy_columns:
                values["enabled"] = bool(revision.enabled and profile.is_active)
            if "updated_at" in legacy_columns:
                values["updated_at"] = now
            if values:
                bind.execute(
                    legacy.update().where(legacy.c.owner_id == owner_id).values(**values)
                )

    # ---- DDL：先撤指针列，再按 FK 依赖顺序撤表。 ----
    if _EXECUTION_CALLS_TABLE in tables:
        execution_columns = _columns(bind, _EXECUTION_CALLS_TABLE)
        if _EXECUTION_SOURCE_COLUMN in execution_columns:
            op.drop_column(_EXECUTION_CALLS_TABLE, _EXECUTION_SOURCE_COLUMN)

    if legacy_exists:
        legacy_columns = _columns(bind, _LEGACY_TABLE)
        for name in ("profile_revision_id", "profile_id", "source_kind"):
            if name in legacy_columns:
                op.drop_column(_LEGACY_TABLE, name)

    if revisions_exist:
        op.drop_table(_REVISIONS_TABLE)
    if profiles_exist:
        op.drop_table(_PROFILES_TABLE)
