"""Isolated resource/SQL contracts, not real PostgreSQL or scale acceptance."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import DBAPIError

from app.ontologies.agent_query.service import UnifiedQueryService
from app.ontologies.agent_runtime.boundary import ToolError
from app.ontologies.agent_runtime.read_queries import temp_limit_kb
from app.ontologies.agent_runtime.toolkit import ToolRunner


class BudgetSession:
    """Record only budget statements; deliberately not a database emulator."""

    def __init__(self, superuser="off", limit="32MB"):
        self.superuser, self.limit = superuser, limit
        self.statements = []
        self.events = []

    @contextmanager
    def begin_nested(self):
        self.events.append("savepoint")
        try:
            yield
        except BaseException:
            self.events.append("rollback_savepoint")
            raise
        else:
            self.events.append("release_savepoint")

    def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(one=lambda: (self.superuser, self.limit))

    def commit(self):
        raise AssertionError("resource setup must never commit caller's transaction")


@pytest.mark.parametrize("display,kb", [
    ("-1", None), ("", None), ("unlimited", None), ("NaN", None),
    ("0", 0), ("100B", 1), ("32768", 32768), ("32768kB", 32768),
    ("32MB", 32768), ("1GB", 1048576),
])
def test_temp_budget_display_units(display, kb):
    assert temp_limit_kb(display) == kb


@pytest.mark.parametrize("limit", ["-1", "1GB", "bad", ""])
def test_ordinary_role_without_effective_temp_budget_rejected(limit):
    session = BudgetSession(limit=limit)
    runner = ToolRunner(session, None)
    with pytest.raises(ToolError, match="query_budget_unavailable"):
        with runner._sql_read_budget():
            raise AssertionError("must reject before executing data query")
    assert session.events == ["savepoint", "rollback_savepoint"]


@pytest.mark.parametrize("limit", ["0", "1MB", "32768kB"])
def test_ordinary_role_keeps_equal_or_stricter_temp_budget(limit):
    session = BudgetSession(limit=limit)
    with ToolRunner(session, None)._sql_read_budget():
        session.events.append("data_query")
    assert session.events == ["savepoint", "data_query", "release_savepoint"]
    configs = [s.compile().params for s in session.statements if "set_config" in str(s)]
    assert any("statement_timeout" in p.values() for p in configs)
    assert any("lock_timeout" in p.values() for p in configs)
    assert any("work_mem" in p.values() for p in configs)
    assert not any("temp_file_limit" in p.values() for p in configs)
    # The legacy structured entry must also accept a no-temp-files role.
    service = object.__new__(UnifiedQueryService)
    service._verify_temp_file_budget(limit)


def test_superuser_receives_transaction_local_temp_budget():
    session = BudgetSession(superuser="on", limit="-1")
    with ToolRunner(session, None)._sql_read_budget():
        pass
    settings = [s.compile().params for s in session.statements]
    assert any("temp_file_limit" in p.values() and True in p.values() for p in settings)


@pytest.mark.parametrize("limit", ["0", "1MB"])
def test_superuser_does_not_loosen_stricter_temp_budget(limit):
    session = BudgetSession(superuser="on", limit=limit)
    with ToolRunner(session, None)._sql_read_budget():
        pass
    configs = [s.compile().params for s in session.statements if "set_config" in str(s)]
    assert not any("temp_file_limit" in p.values() for p in configs)


def test_resource_failure_rolls_back_only_savepoint_before_tool_error():
    session = BudgetSession()
    with pytest.raises(ToolError, match="query_execution_rejected"):
        with ToolRunner(session, None)._sql_read_budget():
            raise DBAPIError("SELECT", {}, RuntimeError("statement timeout"))
    assert session.events == ["savepoint", "rollback_savepoint"]
    # A subsequent query remains available for the orchestrator's rejection audit.
    session.execute(None)
