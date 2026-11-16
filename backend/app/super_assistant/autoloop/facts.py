"""Keep connector credentials out of persisted external facts."""
from __future__ import annotations

import re

_SENSITIVE = re.compile(r"token|secret|password|authorization|cookie|api.?key|credential|^pat$", re.I)
_ISSUE_FIELDS = frozenset({"id", "identifier", "workspace_id", "workspaceId", "status", "priority",
    "created_at", "updated_at", "url", "web_url", "html_url"})
_COMMENT_FIELDS = frozenset({"id", "issue_id", "author_id", "author_type", "source_task_id", "created_at"})
_EXECUTION_FIELDS = frozenset({"task_id", "status", "delivered_at", "trigger_type", "agent_id",
    "supplement_task_id", "supplement_status", "supplement_delivered_at", "trigger_outcomes", "supplements"})


def redact(value, secret):
    """Preserve evidence content while excluding credential fields and PAT echoes."""
    if isinstance(value, dict):
        return {str(key): redact(item, secret) for key, item in value.items()
                if not _SENSITIVE.search(str(key)) and not (secret and secret in str(key))}
    if isinstance(value, list):
        return [redact(item, secret) for item in value]
    if isinstance(value, str) and secret:
        return value.replace(secret, "[redacted]")
    return value


def issue(value, secret):
    return redact({key: item for key, item in value.items() if key in _ISSUE_FIELDS}, secret)


def comment(value, secret):
    return redact({key: item for key, item in value.items() if key in _COMMENT_FIELDS}, secret)


def execution(value, secret):
    if isinstance(value, dict):
        return {key: execution(item, secret) for key, item in value.items() if key in _EXECUTION_FIELDS}
    if isinstance(value, list):
        return [execution(item, secret) for item in value]
    return redact(value, secret)
