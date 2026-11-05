"""Driver-failure logging stays diagnostic without leaking query material.

The unified query layer and the integrated native lane both log only the
driver exception class, numeric driver code, SQLSTATE and stage. Raw driver
text, SQL statements, parameters and credentials never reach the log line.
Status/code contracts are unchanged; only observability was added.
"""
from __future__ import annotations

import logging

import pymysql
import pytest
from sqlalchemy.exc import DBAPIError

from app.data_channel.integrated.native import _log_driver_error
from app.data_channel.query.service import _query_dbapi_error

_SECRET_SQL = "SELECT credential FROM private_table WHERE token = 'leak-me'"
_SECRET_PARAM = "param-leak"


def _dbapi_error(orig):
    return DBAPIError(_SECRET_SQL, {"param": _SECRET_PARAM}, orig)


def _assert_redacted(message: str) -> None:
    assert _SECRET_SQL not in message
    assert _SECRET_PARAM not in message
    assert "leak-me" not in message
    assert "private_table" not in message


def test_query_core_driver_error_log_is_redacted(caplog):
    error = _dbapi_error(pymysql.err.OperationalError(
        1055, "Expression #1 of ORDER BY clause is not in GROUP BY clause (leak-me)"))
    with caplog.at_level(logging.WARNING, logger="app.data_channel.query.service"):
        mapped = _query_dbapi_error(error, stage="memory_execution")
    assert (mapped.status, mapped.code) == (503, "query_execution_failed")
    records = [record for record in caplog.records
               if record.name == "app.data_channel.query.service"]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "stage=memory_execution" in message
    assert "driver=pymysql.OperationalError" in message
    assert "code=1055" in message
    assert "sqlstate=None" in message
    _assert_redacted(message)
    assert "Expression #1" not in message


def test_native_driver_error_log_is_redacted(caplog):
    error = _dbapi_error(pymysql.err.OperationalError(
        1055, "Expression #1 of ORDER BY clause is not in GROUP BY clause (leak-me)"))
    with caplog.at_level(logging.WARNING, logger="app.data_channel.integrated.native"):
        _log_driver_error("source_read_scope", error)
    records = [record for record in caplog.records
               if record.name == "app.data_channel.integrated.native"]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "stage=source_read_scope" in message
    assert "driver=pymysql.OperationalError" in message
    assert "code=1055" in message
    _assert_redacted(message)
    assert "Expression #1" not in message


def test_driver_sqlstate_is_logged_without_raw_text(caplog):
    class _PgError(Exception):
        pgcode = "42P01"

    error = _dbapi_error(_PgError('relation "private_table" does not exist'))
    with caplog.at_level(logging.WARNING, logger="app.data_channel.query.service"):
        _query_dbapi_error(error, stage="persisted_execution")
    message = next(record.getMessage() for record in caplog.records
                   if record.name == "app.data_channel.query.service")
    assert "stage=persisted_execution" in message
    assert "sqlstate=42P01" in message
    assert "_PgError" in message
    _assert_redacted(message)


def test_statement_timeout_mapping_is_unchanged(caplog):
    error = _dbapi_error(pymysql.err.OperationalError(3024, "execution deadline (leak-me)"))
    with caplog.at_level(logging.WARNING, logger="app.data_channel.query.service"):
        mapped = _query_dbapi_error(error, stage="memory_execution")
    assert (mapped.status, mapped.code) == (422, "query_budget_exceeded")
    assert mapped.extra["budget"] == "statement_timeout_ms"
    message = next(record.getMessage() for record in caplog.records
                   if record.name == "app.data_channel.query.service")
    assert "code=3024" in message
    _assert_redacted(message)
