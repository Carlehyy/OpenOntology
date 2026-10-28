"""Query budget construction from shared settings (leaf module).

Kept out of the router so composition adapters can import limits without a
reverse dependency on the HTTP layer.
"""
from __future__ import annotations

from dataclasses import fields

from app.shared.config import settings

from .contracts import QueryLimits


def configured_limits() -> QueryLimits:
    return QueryLimits(**{f.name: getattr(settings, "unified_query_" + f.name)
                          for f in fields(QueryLimits)})
