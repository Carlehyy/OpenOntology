"""Public DTOs and leaf ports. No ontology/source implementation imports."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Literal

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, StrictInt


class QueryError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra

    def detail(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.extra}


def _camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.title() for part in tail)


class StrictDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", alias_generator=_camel,
                              populate_by_name=True)


class Binding(StrictDTO):
    kind: Literal["integrated", "dataset", "ontology"]
    asset_id: str | None = Field(default=None, min_length=1, max_length=128)
    dataset_id: str | None = Field(default=None, min_length=1, max_length=128)
    version_id: str | None = Field(default=None, min_length=1, max_length=128)
    ontology_id: str | None = Field(default=None, min_length=1, max_length=128)
    release_id: str | None = Field(default=None, min_length=1, max_length=128)
    object_type_id: str | None = Field(default=None, min_length=1, max_length=200)


class TextQueryRequest(StrictDTO):
    language: Literal["spl"]
    query: str = Field(min_length=1, max_length=32768)
    bindings: dict[str, Binding] = Field(min_length=1, max_length=16)
    params: dict[str, Any] = Field(default_factory=dict, max_length=128)
    page_size: StrictInt = Field(default=100, ge=1, le=1000)


class TextQueryCursorRequest(StrictDTO):
    cursor: str = Field(min_length=16, max_length=8192)


@dataclass(frozen=True)
class QueryLimits:
    statement_timeout_ms: int = 300000
    lock_timeout_ms: int = 5000
    work_mem_mib: int = 32
    temp_file_limit_mib: int = 16384
    result_bytes_limit: int = 1073741824
    staging_bytes_limit: int = 17179869184
    snapshot_ttl_seconds: int = 300
    max_active_per_user: int = 10
    batch_size: int = 2000
    max_page_size: int = 1000

    def __post_init__(self):
        for name, value in vars(self).items():
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    type: str
    nullable: bool = True
    precision: int | None = None
    scale: int | None = None
    timezone: bool = False
    source_type: str | None = None
    collation: str | None = None
    unsigned: bool = False

    def sql_type(self) -> sa.types.TypeEngine:
        types = {"string": sa.Text, "integer": sa.BigInteger,
                 "float": sa.Float, "boolean": sa.Boolean,
                 "date": sa.Date, "json": sa.JSON, "binary": sa.LargeBinary}
        if self.type == "integer" and self.unsigned:
            return sa.Numeric(20, 0)
        if self.type == "decimal":
            return sa.Numeric(self.precision, self.scale)
        if self.type == "timestamp":
            return sa.DateTime(timezone=self.timezone)
        if self.type == "time":
            return sa.Time(timezone=self.timezone)
        if self.type not in types:
            raise QueryError(422, "unsupported_type", f"Unsupported type {self.type!r}")
        return types[self.type]()

    @property
    def encoding(self) -> str:
        return {"decimal": "decimal_string", "integer": "integer_string",
                "date": "iso8601", "timestamp": "iso8601", "time": "iso8601",
                "binary": "base64", "json": "json_text"}.get(self.type, "json")

    def output(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type, "nullable": self.nullable,
                "encoding": self.encoding}


@dataclass
class Relation:
    """A permission-checked, pinned read relation supplied by a leaf adapter.

    ``batches`` executes only compiler-generated SQLAlchemy Selects. A provider
    must raise on failed/incomplete EOF, and reauthorize on every cursor request.
    External adapters must fetch incrementally and bound buffered encoded bytes,
    not just row counts; the built-in source adapter keeps one driver row in flight.
    Driver allocation for a single source cell is outside the application bound.
    Platform relations may use the query transaction directly (no staging copy).
    """
    alias: str
    table: Any
    columns: dict[str, ColumnSpec]
    state: dict[str, Any]
    auth_fingerprint: str
    schema_fingerprint: str
    connection_key: str
    batches: Callable[[sa.Select, dict[str, Any], int], Any] | None = None
    recheck: Callable[[], None] | None = None
    platform: bool = False
    expressions: dict[str, Any] = field(default_factory=dict)
    close: Callable[[], None] | None = None
    source_group: Any = None


# The composition root installs adapters; this leaf package never imports the
# ontology domain, whose permission/release rules remain owned by that domain.
Provider = Callable[[Any, Any, str, Binding, Any], Relation]
_PROVIDERS: dict[str, Provider] = {}


def register_provider(kind: str, provider: Provider) -> None:
    if kind not in {"integrated", "dataset", "ontology"}:
        raise ValueError("unknown query provider kind")
    _PROVIDERS[kind] = provider


def provider_for(kind: str) -> Provider:
    try:
        return _PROVIDERS[kind]
    except KeyError as exc:
        raise QueryError(503, "query_provider_unavailable",
                         f"Read adapter for {kind!r} is not installed") from exc
