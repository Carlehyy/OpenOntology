"""Logical field-config projection for registered integrated assets.

The physical source schema (``columns``/``primary_key`` and its fingerprint)
is the asset's authoritative identity: drift detection, mutation tokens and
sealed version checksums always operate on physical truth.  An optional
one-to-one ``IntegratedAssetFieldConfig`` row projects that truth into logical
names for presentation and API addressing only.  Aliases and column subsets
never change physical storage, physical fingerprints or the lake manifest's
physical schema; a separate logical fingerprint identifies the projection
itself.
"""
from __future__ import annotations

import re
from typing import Any

from app.data_channel.integrated.contracts import digest

# Logical names are the addressing namespace of the public API (query fields,
# mutation keys/values, sealed version columns), so they stay portable ASCII
# snake_case identifiers well inside common SQL identifier limits.
FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
FIELD_NAME_MAX = 63
DISPLAY_NAME_MAX = 200

_ENTRY_KEYS = {"source_key", "name", "display_name"}


class FieldConfigError(ValueError):
    """A field config does not match the physical schema it must project."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def normalize_field_config(raw: Any, schema: dict) -> list[dict] | None:
    """Validate a caller-supplied config against a physical schema.

    ``raw`` is ``None`` for unconfigured assets (original full-schema
    identity).  A present config must be a nonempty list of entries whose
    ``source_key`` references an existing physical column exactly once, whose
    logical ``name`` is a unique lowercase snake_case alias, and which covers
    every physical primary-key column.  Only ``source_key``, ``name`` and
    ``display_name`` are accepted; type/nullability/PK membership are always
    physical truth and cannot be configured.
    """
    if raw is None:
        return None
    if isinstance(raw, dict) or not isinstance(raw, (list, tuple)) or not raw:
        raise FieldConfigError("FIELD_CONFIG_EMPTY",
                               "field_config must be a nonempty list when present")
    columns = schema.get("columns") if isinstance(schema, dict) else None
    if not isinstance(columns, list) or not columns:
        raise FieldConfigError("FIELD_CONFIG_INVALID",
                               "field_config requires a physical source schema")
    physical = [column.get("name") for column in columns
                if isinstance(column, dict) and isinstance(column.get("name"), str)]
    if len(physical) != len(columns) or len(set(physical)) != len(physical):
        raise FieldConfigError("FIELD_CONFIG_INVALID",
                               "field_config requires a valid physical source schema")
    primary_key = schema.get("primary_key")
    if not isinstance(primary_key, list) or not all(isinstance(k, str) for k in primary_key):
        raise FieldConfigError("FIELD_CONFIG_INVALID",
                               "field_config requires a valid physical primary key")
    known = set(physical)
    normalized: list[dict] = []
    aliases: set[str] = set()
    sources: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise FieldConfigError("FIELD_CONFIG_INVALID",
                                   "Each field_config entry must be an object")
        if entry.keys() - _ENTRY_KEYS:
            raise FieldConfigError("FIELD_CONFIG_INVALID",
                                   "field_config entries accept only source_key, name and display_name")
        source_key, name = entry.get("source_key"), entry.get("name")
        if not isinstance(source_key, str) or not source_key or "\x00" in source_key:
            raise FieldConfigError("FIELD_CONFIG_INVALID",
                                   "source_key must be a nonempty string")
        if source_key not in known:
            raise FieldConfigError("FIELD_CONFIG_UNKNOWN_SOURCE",
                                   f"source_key {source_key!r} is not a physical source column")
        if source_key in sources:
            raise FieldConfigError("FIELD_CONFIG_INVALID",
                                   "Each source_key may appear at most once")
        if (not isinstance(name, str) or len(name) > FIELD_NAME_MAX
                or not FIELD_NAME_RE.fullmatch(name)):
            raise FieldConfigError("FIELD_CONFIG_INVALID",
                                   "Logical field names must be unique lowercase snake_case identifiers")
        if name in aliases:
            raise FieldConfigError("FIELD_CONFIG_INVALID",
                                   "Logical field names must be unique")
        display = entry.get("display_name")
        if display is not None:
            if not isinstance(display, str):
                raise FieldConfigError("FIELD_CONFIG_INVALID",
                                       "display_name must be a string when present")
            display = display.strip()
            if not display:
                display = None
            elif len(display) > DISPLAY_NAME_MAX:
                raise FieldConfigError("FIELD_CONFIG_INVALID",
                                       f"display_name must be at most {DISPLAY_NAME_MAX} characters")
        normalized.append({"source_key": source_key, "name": name, "display_name": display})
        aliases.add(name)
        sources.add(source_key)
    missing = [key for key in primary_key if key not in sources]
    if missing:
        raise FieldConfigError("FIELD_CONFIG_PK_REQUIRED",
                               "field_config must include every physical primary-key column")
    return normalized


def logical_view(schema: dict, config: list[dict]) -> dict:
    """Project a physical schema through a normalized field config.

    Column order follows the physical schema (stable across refreshes); each
    returned column spec is the untouched physical spec with the logical name
    substituted and ``source_key``/``display_name`` attached.  The primary key
    is the logical alias of every physical primary-key column, in order.
    """
    by_source = {entry["source_key"]: entry for entry in config}
    columns = []
    for column in schema["columns"]:
        entry = by_source.get(column["name"])
        if entry is None:
            continue
        spec = dict(column)
        spec["name"] = entry["name"]
        spec["source_key"] = entry["source_key"]
        if entry["display_name"] is not None:
            spec["display_name"] = entry["display_name"]
        columns.append(spec)
    alias_of = {entry["source_key"]: entry["name"] for entry in config}
    return {"columns": columns,
            "primary_key": [alias_of[key] for key in schema["primary_key"]]}


def logical_fingerprint(view: dict) -> str:
    """Fingerprint of the logical view; never mixed into physical fingerprints."""
    return digest({"columns": view["columns"], "primary_key": view["primary_key"]})


def logical_to_physical(config: list[dict]) -> dict[str, str]:
    """Map logical names to physical source column names."""
    return {entry["name"]: entry["source_key"] for entry in config}


def projection_of(schema: dict | None, raw_config: Any) -> dict | None:
    """Validated projection of a physical schema plus a stored config, or None.

    Returns ``{"field_config", "columns", "primary_key", "logical_to_physical",
    "fingerprint"}`` for a configured asset and ``None`` when no config row
    exists (original full-schema identity).  A structurally invalid persisted
    config fails closed instead of being silently ignored.
    """
    if raw_config is None or not isinstance(schema, dict):
        return None
    config = normalize_field_config(raw_config, schema)
    view = logical_view(schema, config)
    return {"field_config": config, "columns": view["columns"],
            "primary_key": view["primary_key"],
            "logical_to_physical": logical_to_physical(config),
            "fingerprint": logical_fingerprint(view)}


def projected_column_map(source_schema: list[dict], column_map: dict[str, str],
                         config: list[dict]) -> dict[str, str]:
    """Physical-order logical -> physical-storage column map for a manifest.

    ``column_map`` maps a physical source column to its storage column; the
    projection keeps the physical schema order and substitutes logical names.
    """
    by_source = {entry["source_key"]: entry["name"] for entry in config}
    projected = {}
    for column in source_schema:
        name = column["name"]
        if name in by_source and name in column_map:
            projected[by_source[name]] = column_map[name]
    return projected
