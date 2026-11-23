"""persist Multica browser workbench origins

Display-only column: it belongs to the profile row, never to an immutable
revision or an execution-source digest. Custom profiles are backfilled once
from the origin of their current API base_url, which is the entry point the
previous frontend derived; rows whose base_url has no valid http(s) origin
stay NULL instead of aborting the migration.

Downgrade drops the column. A workbench address that an operator entered by
hand (anything other than origin(base_url)) cannot be derived again and is
lost; see docs/operations/rollback.md.

Revision ID: 0155_multica_profile_web_url
Revises: 0154_multica_profiles
Create Date: 2026-11-23
"""
from __future__ import annotations

from urllib.parse import urlsplit

import sqlalchemy as sa
from alembic import op
from pydantic import AnyHttpUrl
from sqlalchemy import inspect as sa_inspect

revision = "0155_multica_profile_web_url"
down_revision = "0154_multica_profiles"
branch_labels = None
depends_on = None

_TABLE = "super_assistant_multica_profiles"
_COLUMN = "web_url"


def _tables(bind) -> set[str]:
    return set(sa_inspect(bind).get_table_names())


def _columns(bind) -> set[str]:
    return {column["name"] for column in sa_inspect(bind).get_columns(_TABLE)}


def _origin(raw: str | None) -> str | None:
    value = (raw or "").strip()
    if any(char in "\\" or char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username is not None:
            return None
        url = AnyHttpUrl(f"{parsed.scheme}://{parsed.netloc}")
    except ValueError:
        return None
    origin = str(url).removesuffix("/")
    return origin if url.port != 0 and len(origin) <= 500 else None


def upgrade() -> None:
    bind = op.get_bind()
    if _TABLE not in _tables(bind):
        return
    if _COLUMN not in _columns(bind):
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(length=500), nullable=True))

    # Existing custom profiles used base_url as their only available entry
    # point. Preserve that behavior once, without introducing a runtime fallback
    # that would again confuse API and browser origins.
    profiles = sa.table(
        _TABLE,
        sa.column("id", sa.String),
        sa.column("kind", sa.String),
        sa.column("web_url", sa.String),
    )
    revisions = sa.table(
        "super_assistant_multica_profile_revisions",
        sa.column("profile_id", sa.String),
        sa.column("base_url", sa.String),
        sa.column("retired_at", sa.DateTime),
    )
    rows = bind.execute(
        sa.select(profiles.c.id, revisions.c.base_url)
        .select_from(profiles.join(revisions, revisions.c.profile_id == profiles.c.id))
        .where(
            profiles.c.kind == "custom",
            profiles.c.web_url.is_(None),
            revisions.c.retired_at.is_(None),
        )
    ).all()
    for profile_id, base_url in rows:
        web_url = _origin(base_url)
        if web_url:
            bind.execute(
                profiles.update().where(profiles.c.id == profile_id).values(web_url=web_url)
            )


def downgrade() -> None:
    bind = op.get_bind()
    if _TABLE in _tables(bind) and _COLUMN in _columns(bind):
        op.drop_column(_TABLE, _COLUMN)
