"""Transaction-local SQL keys for bounded mapping joins and reconciliation."""
from __future__ import annotations

from contextlib import contextmanager
import logging
import uuid

from sqlalchemy import Column, Integer, MetaData, String, Table, Text, select


MAPPING_BATCH_SIZE = 500
logger = logging.getLogger(__name__)


def keyset_pages(query, key, *, batch_size=MAPPING_BATCH_SIZE):
    """Read and flush one bounded ORM page without retaining earlier pages."""
    last = None
    while True:
        page_query = query if last is None else query.filter(key > last)
        page = page_query.order_by(key).limit(batch_size).all()
        if not page:
            return
        last = getattr(page[-1], key.key)
        yield page
        query.session.flush()


def join_text(value):
    """Match the mapping contract's exact, trimmed scalar lookup values."""
    return None if value is None else str(value).strip()


def stage_connection(db):
    """Keep TEMP CREATE and DROP in the caller's physical SQL transaction."""
    connection = db.connection()
    # sqlite3 legacy mode does not BEGIN for DDL: a later rollback would
    # resurrect a staged DROP while its earlier CREATE survived that rollback.
    if (connection.dialect.name == "sqlite"
            and not connection.connection.driver_connection.in_transaction):
        connection.exec_driver_sql("BEGIN")
    return connection


class MappingStage:
    def __init__(self, db, columns):
        self.db = db
        self.columns = list(dict.fromkeys(columns))
        self.column_map = {name: f"c{index}" for index, name in enumerate(self.columns)}
        self.table = Table(
            "mapping_stage_" + uuid.uuid4().hex, MetaData(),
            Column("ordinal", Integer, primary_key=True),
            Column("entity_id", String, nullable=False, index=True),
            *(Column(physical, Text) for physical in self.column_map.values()),
            prefixes=["TEMPORARY"],
        )
        self.table.create(stage_connection(db))
        self.count = 0

    def append(self, rows, entity_ids):
        if len(rows) > MAPPING_BATCH_SIZE:
            raise ValueError("Mapping staging pages may contain at most 500 rows")
        values = []
        for row, entity_id in zip(rows, entity_ids, strict=True):
            self.count += 1
            values.append({
                "ordinal": self.count, "entity_id": entity_id,
                **{physical: join_text(row.get(logical))
                   for logical, physical in self.column_map.items()},
            })
        if values:
            self.db.execute(self.table.insert(), values)

    def field(self, name):
        if name not in self.column_map:
            raise ValueError(f"Mapping join column {name!r} is absent from source schema")
        return self.table.c[self.column_map[name]]

    @property
    def entity_ids(self):
        return select(self.table.c.entity_id)

    def close(self):
        self.table.drop(self.db.connection(), checkfirst=True)


@contextmanager
def managed_stage(stage):
    """Drop transaction-local state without replacing the original exception."""
    try:
        yield stage
    except BaseException:
        if stage.db.is_active:
            try:
                stage.close()
            except Exception:
                logger.warning("Mapping stage cleanup deferred to rollback", exc_info=True)
        raise
    else:
        stage.close()


@contextmanager
def mapping_stage(db, columns):
    with managed_stage(MappingStage(db, columns)) as stage:
        yield stage
