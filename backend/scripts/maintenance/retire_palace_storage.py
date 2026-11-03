#!/usr/bin/env python3
"""Retire the retired Super Assistant memory-palace storage.

This command is intended to run **after** the ``0141_retire_super_assistant_palace``
migration. It removes the old feature's dedicated external state without ever
touching shared platform storage:

- uploads: only ``<uploads_dir>/super-assistant-palace`` by default. A custom
  legacy root must be supplied explicitly with ``--palace-root`` and
  ``--allow-custom-root``; the command validates real path content, symlinks and
  ancestor/root boundaries before deleting anything.
- Neo4j: only ``:PalaceEntity`` nodes **without** the ``:OntologyEntity`` label,
  plus indexes whose names start with ``palace_entity_``. The graph is never
  cleared wholesale.
- Redis: only keys matching the retired cache namespace ``ob:palace:*`` in the
  same db-1 cache used by the old ``palace_cache`` module.
- NATS: only the exact retired subjects/durables in ``PIPELINE_TASKS``. Messages
  are purged with exact-subject filters, and only the four retired subjects are
  removed from the stream config; every other subject and consumer is preserved.

Usage::

    uv run python scripts/maintenance/retire_palace_storage.py
    uv run python scripts/maintenance/retire_palace_storage.py --execute

Dry-run is the default. ``--execute`` runs a read-only preflight across every
source first; if any source is ``unsafe``, ``error``, ``unavailable`` or
``not_configured``, nothing is deleted anywhere. Only a clean preflight proceeds
to per-source re-validation and mutation. The command is idempotent and prints
only redacted counts/statuses.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    # Direct script execution: make the backend package root importable.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


PALACE_DIR_NAME = "super-assistant-palace"
PALACE_NODE_LABEL = "PalaceEntity"
SHARED_NODE_LABEL = "OntologyEntity"
PALACE_INDEX_PREFIX = "palace_entity_"
REDIS_PATTERN = "ob:palace:*"
NATS_STREAM = "PIPELINE_TASKS"
NATS_SUBJECTS = (
    "super_assistant.palace.extract",
    "super_assistant.palace.consolidate",
    "super_assistant.palace.ontology-rebuild",
    "ontology.documents.published",
)
NATS_DURABLES = (
    "super-assistant-palace-extract",
    "super-assistant-palace-consolidate",
    "super-assistant-palace-ontology-rebuild",
    "ontology-documents-published",
)
_INDEX_NAME_RE = re.compile(rf"^{PALACE_INDEX_PREFIX}[A-Za-z0-9_]+$")
_REDIS_BATCH = 500


class SafetyError(ValueError):
    """The requested path/source did not prove it is the retired palace state."""


class QueryShapeError(RuntimeError):
    """A maintenance query returned an unexpected or unverifiable shape."""


@dataclass(frozen=True)
class WorkspaceStats:
    exists: bool
    files: int = 0
    dirs: int = 0
    bytes: int = 0


def _resolved(value: str | Path) -> Path:
    return Path(value).expanduser().resolve(strict=False)


def _is_ancestor(candidate: Path, child: Path) -> bool:
    return candidate == child or candidate in child.parents


def resolve_palace_root(
    uploads_dir: str | Path,
    *,
    custom_root: str | Path | None = None,
    allow_custom_root: bool = False,
) -> Path:
    """Return the only upload directory this command may delete.

    The default target is the retired fixed directory. A custom root is accepted
    only with an explicit acknowledgement and after proving it is not the shared
    uploads root, one of its ancestors, or an unrelated workspace inside it.
    """
    uploads = _resolved(uploads_dir)
    if uploads == Path(uploads.anchor):
        raise SafetyError("uploads root must not be a filesystem root")

    if custom_root is None:
        target = uploads / PALACE_DIR_NAME
    else:
        if not allow_custom_root:
            raise SafetyError("custom palace root requires explicit acknowledgement")
        raw = Path(custom_root).expanduser()
        if not raw.is_absolute():
            raise SafetyError("custom palace root must be absolute")
        if raw.is_symlink():
            raise SafetyError("custom palace root must not be a symlink")
        # Compare the lexical path with its real path before using resolve():
        # resolve() would otherwise silently erase a symlinked parent and make a
        # redirected custom root look canonical.
        lexical = os.path.abspath(os.fspath(raw))
        real = os.path.realpath(os.fspath(raw))
        if lexical != real:
            raise SafetyError("custom palace root must not traverse symlinks")
        target = Path(real)
        if target == Path(target.anchor):
            raise SafetyError("custom palace root must not be a filesystem root")
        if target == uploads:
            raise SafetyError("refusing to delete the shared uploads root")
        if _is_ancestor(target, uploads):
            raise SafetyError("custom palace root must not be an uploads ancestor")
        if target.is_relative_to(uploads) and target.name != PALACE_DIR_NAME:
            # Shared workspaces under uploads_dir use the same UUID/manifest
            # layout as the old palace root; only the exact retired name is
            # provably palace-specific.
            raise SafetyError("custom palace root inside uploads must use the retired name")

    if target.is_symlink():
        raise SafetyError("palace root must not be a symlink")
    if target == uploads or target == Path(target.anchor):
        raise SafetyError("refusing to delete shared or filesystem root")
    return target


def _validate_manifest(manifest: Path) -> None:
    if manifest.is_symlink() or not manifest.is_file():
        raise SafetyError("workspace entry is missing its manifest")
    try:
        parsed = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SafetyError("workspace manifest is unreadable") from exc
    if not isinstance(parsed, list):
        raise SafetyError("workspace manifest has an unexpected shape")


def inspect_workspace(target: Path) -> WorkspaceStats:
    """Validate the old SessionWorkspace layout and return redacted counts."""
    if target.is_symlink():
        raise SafetyError("palace root is a symlink")
    if not target.exists():
        return WorkspaceStats(exists=False)
    if not target.is_dir():
        raise SafetyError("palace root is not a regular directory")

    for entry in target.iterdir():
        if entry.is_symlink() or not entry.is_dir():
            raise SafetyError("palace root contains an unexpected entry")
        try:
            uuid.UUID(entry.name)
        except (ValueError, TypeError, AttributeError) as exc:
            raise SafetyError("palace root contains a non-workspace directory") from exc
        meta = entry / ".meta"
        if meta.is_symlink() or not meta.is_dir():
            raise SafetyError("workspace entry has an unexpected metadata directory")
        _validate_manifest(meta / "manifest.json")

    files = dirs = total_bytes = 0
    for root, dirnames, filenames in os.walk(target, followlinks=False):
        for name in dirnames:
            if (Path(root) / name).is_symlink():
                raise SafetyError("palace root contains a symlink")
        for name in filenames:
            path = Path(root) / name
            if path.is_symlink():
                raise SafetyError("palace root contains a symlink")
            try:
                stat = path.stat(follow_symlinks=False)
            except OSError as exc:
                raise SafetyError("palace root file is unreadable") from exc
            files += 1
            total_bytes += int(stat.st_size)
        dirs += len(dirnames)
    return WorkspaceStats(
        exists=True, files=files, dirs=dirs, bytes=total_bytes,
    )


def retire_uploads(
    uploads_dir: str | Path,
    *,
    custom_root: str | Path | None = None,
    allow_custom_root: bool = False,
    execute: bool = False,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "ok", "exists": False, "files": 0, "dirs": 0,
        "bytes": 0, "deleted": False,
    }
    try:
        target = resolve_palace_root(
            uploads_dir,
            custom_root=custom_root,
            allow_custom_root=allow_custom_root,
        )
        stats = inspect_workspace(target)
    except SafetyError:
        return {**result, "status": "unsafe"}
    except (OSError, RuntimeError):
        return {**result, "status": "error"}

    if not stats.exists:
        return {**result, "status": "missing"}
    result.update({
        "exists": True,
        "files": stats.files,
        "dirs": stats.dirs,
        "bytes": stats.bytes,
    })
    if execute:
        try:
            shutil.rmtree(target)
        except OSError:
            return {**result, "status": "error"}
        result["deleted"] = True
    return result


def _require_count(rows: Any, key: str) -> int:
    """Reject empty/partial/untyped count results instead of treating them as 0."""
    if not isinstance(rows, list) or len(rows) != 1:
        raise QueryShapeError("count query did not return exactly one row")
    row = rows[0]
    if not isinstance(row, dict):
        raise QueryShapeError("count row is not a mapping")
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise QueryShapeError("count value is missing or not a non-negative integer")
    return value


def _index_names(graph: Any) -> list[str]:
    rows = graph.run_cypher(
        f'SHOW INDEXES YIELD name WHERE name STARTS WITH "{PALACE_INDEX_PREFIX}" '
        "RETURN name ORDER BY name"
    )
    if not isinstance(rows, list):
        raise QueryShapeError("index query did not return a list")
    names = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise QueryShapeError("index row has an unexpected shape")
        name = row["name"]
        if not _INDEX_NAME_RE.fullmatch(name):
            raise QueryShapeError("index name is outside the retired namespace")
        names.append(name)
    return sorted(set(names))


def _retirable_node_count(graph: Any) -> int:
    return _require_count(graph.run_cypher(
        f"MATCH (n:{PALACE_NODE_LABEL}) "
        f"WHERE NOT n:{SHARED_NODE_LABEL} RETURN count(n) AS count"
    ), "count")


def retire_neo4j(graph: Any, *, execute: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "ok",
        "nodes_to_delete": 0,
        "shared_label_nodes_skipped": 0,
        "indexes_to_drop": 0,
        "deleted_nodes": 0,
        "dropped_indexes": 0,
    }
    if graph is None or not getattr(graph, "available", False):
        return {**result, "status": "unavailable"}
    try:
        retirable = _retirable_node_count(graph)
        shared = _require_count(graph.run_cypher(
            f"MATCH (n:{PALACE_NODE_LABEL}:{SHARED_NODE_LABEL}) "
            "RETURN count(n) AS count"
        ), "count")
        indexes = _index_names(graph)
    except Exception:  # noqa: BLE001 — maintenance command reports sanitized status
        return {**result, "status": "error"}

    result.update({
        "nodes_to_delete": retirable,
        "shared_label_nodes_skipped": shared,
        "indexes_to_drop": len(indexes),
    })
    if not execute:
        return result

    try:
        if retirable:
            deleted = _require_count(graph.run_cypher(
                f"MATCH (n:{PALACE_NODE_LABEL}) "
                f"WHERE NOT n:{SHARED_NODE_LABEL} "
                "WITH collect(n) AS nodes "
                "FOREACH (n IN nodes | DETACH DELETE n) "
                "RETURN size(nodes) AS deleted"
            ), "deleted")
            result["deleted_nodes"] = deleted
            # Verify the deletion actually converged; a swallowed/empty result
            # must not turn into a false-success report.
            if _retirable_node_count(graph) != 0:
                raise QueryShapeError("palace nodes remain after delete")
        for name in indexes:
            if not _INDEX_NAME_RE.fullmatch(name):
                raise QueryShapeError("unexpected index name")
            graph.run_cypher(f"DROP INDEX `{name}` IF EXISTS")
        # DROP INDEX returns no rows; verify by re-reading the index namespace.
        remaining = set(_index_names(graph))
        if remaining & set(indexes):
            raise QueryShapeError("palace indexes remain after drop")
        result["dropped_indexes"] = len(indexes)
    except Exception:  # noqa: BLE001 — report counts collected so far
        return {**result, "status": "error"}
    return result


class StrictNeo4jGraph:
    """Strict driver/session adapter: errors and empty results are never masked."""

    def __init__(self, driver: Any):
        self._driver = driver
        self.available = True

    def run_cypher(self, query: str, params: dict | None = None) -> list[dict]:
        with self._driver.session() as session:
            result = session.run(query, **(params or {}))
            rows = [dict(record) for record in result]
            # Consume the summary so server-side failures are not hidden by an
            # empty iteration.
            result.consume()
            return rows

    def close(self) -> None:
        self._driver.close()


def _redis_cache_url(redis_url: str | None) -> str:
    """Mirror the retired cache's db-1 URL selection exactly."""
    url = str(redis_url or "")
    if not url:
        return ""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    path_sep = rest.rfind("/")
    if path_sep > 0 and rest[path_sep + 1:].isdigit():
        return f"{scheme}://{rest[:path_sep]}/1"
    return f"{url}/1"


def _decode_key(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def retire_redis(client: Any, *, execute: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "ok", "keys": 0, "deleted": 0,
    }
    if client is None:
        return {**result, "status": "unavailable"}
    try:
        client.ping()
    except Exception:  # noqa: BLE001 — service absence is reported as unavailable
        return {**result, "status": "unavailable"}

    keys: list[str] = []
    try:
        cursor: Any = 0
        while True:
            cursor, batch = client.scan(
                cursor=cursor, match=REDIS_PATTERN, count=_REDIS_BATCH,
            )
            keys.extend(
                key for key in (_decode_key(item) for item in batch or [])
                if key.startswith("ob:palace:")
            )
            if int(cursor or 0) == 0:
                break
    except Exception:  # noqa: BLE001
        return {**result, "status": "error"}

    keys = sorted(set(keys))
    result["keys"] = len(keys)
    if not execute or not keys:
        return result
    try:
        for index in range(0, len(keys), _REDIS_BATCH):
            batch = keys[index:index + _REDIS_BATCH]
            result["deleted"] += int(client.delete(*batch) or 0)
    except Exception:  # noqa: BLE001
        return {**result, "status": "error"}
    return result


def _consumer_name(info: Any) -> str:
    name = getattr(info, "name", None)
    if not name:
        config = getattr(info, "config", None)
        name = getattr(config, "durable_name", None)
    return str(name or "")


async def retire_nats(js: Any, *, execute: bool = False) -> dict[str, Any]:
    """Inspect then mutate only the exact retired JetStream subjects/durables."""
    result: dict[str, Any] = {
        "status": "ok",
        "stream_subjects": 0,
        "stream_consumers": 0,
        "old_subjects_present": 0,
        "old_consumers_present": 0,
        "deleted_consumers": 0,
        "purged_subjects": 0,
        "removed_subjects": 0,
    }
    if js is None:
        return {**result, "status": "not_configured"}

    from nats.js.errors import NotFoundError

    try:
        info = await js.stream_info(NATS_STREAM)
    except NotFoundError:
        return {**result, "status": "stream_missing"}
    except Exception:  # noqa: BLE001
        return {**result, "status": "error"}

    try:
        subjects = [str(item) for item in (info.config.subjects or [])]
        consumers = await js.consumers_info(NATS_STREAM)
        names = {_consumer_name(item) for item in consumers}
    except Exception:  # noqa: BLE001
        return {**result, "status": "error"}

    old_subjects = sorted(set(subjects) & set(NATS_SUBJECTS))
    old_consumers = sorted(names & set(NATS_DURABLES))
    result.update({
        "stream_subjects": len(subjects),
        "stream_consumers": len(consumers),
        "old_subjects_present": len(old_subjects),
        "old_consumers_present": len(old_consumers),
    })
    if not execute:
        return result

    try:
        for name in old_consumers:
            try:
                deleted = await js.delete_consumer(NATS_STREAM, name)
            except NotFoundError:  # already removed by a concurrent retry
                continue
            if not deleted:
                raise QueryShapeError("consumer delete was not confirmed")
            result["deleted_consumers"] += 1
        for subject in old_subjects:
            # Exact subject filter: never a wildcard or a general stream purge.
            try:
                purged = await js.purge_stream(NATS_STREAM, subject=subject)
            except NotFoundError:  # subject already removed by a concurrent retry
                continue
            if not purged:
                raise QueryShapeError("subject purge was not confirmed")
            result["purged_subjects"] += 1
        if old_consumers:
            remaining = {
                _consumer_name(item)
                for item in await js.consumers_info(NATS_STREAM)
            }
            if remaining & set(old_consumers):
                raise QueryShapeError("retired consumers remain after delete")
        if old_subjects:
            kept = [subject for subject in subjects if subject not in old_subjects]
            if kept != subjects:
                info.config.subjects = kept
                await js.update_stream(info.config)
            refreshed = await js.stream_info(NATS_STREAM)
            remaining_subjects = {
                str(subject) for subject in (refreshed.config.subjects or [])
            }
            if remaining_subjects & set(old_subjects):
                raise QueryShapeError("retired subjects remain after update")
            result["removed_subjects"] = len(subjects) - len(kept)
    except Exception:  # noqa: BLE001
        return {**result, "status": "error"}
    return result


_BLOCKING_STATUSES = frozenset({"unsafe", "error", "unavailable", "not_configured"})


def _errors_for(sources: dict[str, dict[str, Any]]) -> list[str]:
    return sorted(
        f"{name}:{value['status']}"
        for name, value in sources.items()
        if value["status"] in _BLOCKING_STATUSES
    )


async def inspect_all(
    *,
    uploads_dir: str | Path,
    custom_root: str | Path | None = None,
    allow_custom_root: bool = False,
    graph: Any = None,
    redis_client: Any = None,
    nats_js: Any = None,
) -> dict[str, dict[str, Any]]:
    """Read-only inspection of every source, before any mutation is possible."""
    return {
        "uploads": retire_uploads(
            uploads_dir,
            custom_root=custom_root,
            allow_custom_root=allow_custom_root,
            execute=False,
        ),
        "neo4j": retire_neo4j(graph, execute=False),
        "redis": retire_redis(redis_client, execute=False),
        "nats": await retire_nats(nats_js, execute=False),
    }


async def _execute_all(
    *,
    uploads_dir: str | Path,
    custom_root: str | Path | None,
    allow_custom_root: bool,
    graph: Any,
    redis_client: Any,
    nats_js: Any,
) -> dict[str, dict[str, Any]]:
    """Re-validate and execute each source, stopping before later sources fail."""
    execution: dict[str, dict[str, Any]] = {}

    uploads = retire_uploads(
        uploads_dir,
        custom_root=custom_root,
        allow_custom_root=allow_custom_root,
        execute=True,
    )
    execution["uploads"] = uploads
    if uploads["status"] in _BLOCKING_STATUSES:
        return execution

    neo4j = retire_neo4j(graph, execute=True)
    execution["neo4j"] = neo4j
    if neo4j["status"] in _BLOCKING_STATUSES:
        return execution

    redis_result = retire_redis(redis_client, execute=True)
    execution["redis"] = redis_result
    if redis_result["status"] in _BLOCKING_STATUSES:
        return execution

    execution["nats"] = await retire_nats(nats_js, execute=True)
    return execution


async def retire_all(
    *,
    execute: bool,
    uploads_dir: str | Path,
    custom_root: str | Path | None = None,
    allow_custom_root: bool = False,
    graph: Any = None,
    redis_client: Any = None,
    nats_js: Any = None,
) -> dict[str, Any]:
    """Inspect every source first; mutate only when the whole preflight is clean."""
    inspection = await inspect_all(
        uploads_dir=uploads_dir,
        custom_root=custom_root,
        allow_custom_root=allow_custom_root,
        graph=graph,
        redis_client=redis_client,
        nats_js=nats_js,
    )
    errors = _errors_for(inspection)
    if not execute:
        return {
            "mode": "dry-run",
            "inspection": inspection,
            "execution": None,
            "aborted": False,
            "partial": False,
            "errors": errors,
        }
    if errors:
        return {
            "mode": "execute",
            "inspection": inspection,
            "execution": None,
            "aborted": True,
            "partial": False,
            "errors": errors,
        }

    execution = await _execute_all(
        uploads_dir=uploads_dir,
        custom_root=custom_root,
        allow_custom_root=allow_custom_root,
        graph=graph,
        redis_client=redis_client,
        nats_js=nats_js,
    )
    execution_errors = _errors_for(execution)
    return {
        "mode": "execute",
        "inspection": inspection,
        "execution": execution,
        "aborted": False,
        "partial": bool(execution_errors),
        "errors": execution_errors,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Retire Super Assistant memory-palace external storage. "
            "Dry-run unless --execute is supplied."
        ),
    )
    parser.add_argument(
        "--execute", action="store_true",
        help=(
            "Apply deletions only after every source passes the read-only "
            "preflight. Omit for a read-only report."
        ),
    )
    parser.add_argument(
        "--palace-root", default=None,
        help=(
            "Explicit old custom palace workspace root. Requires "
            "--allow-custom-root and passes path/content/symlink safety checks."
        ),
    )
    parser.add_argument(
        "--allow-custom-root", action="store_true",
        help="Acknowledge that --palace-root is the retired palace root.",
    )
    return parser


async def _run_cli(args: argparse.Namespace) -> dict[str, Any]:
    from app.config import settings

    graph = None
    redis_client = None
    nc = None
    js = None
    nats_error = False
    driver = None
    try:
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
        driver.verify_connectivity()
        graph = StrictNeo4jGraph(driver)
    except Exception:  # noqa: BLE001
        graph = None
        if driver is not None:
            try:
                driver.close()
            except Exception:  # noqa: BLE001
                pass
    try:
        from redis import Redis

        redis_url = _redis_cache_url(settings.redis_url)
        redis_client = (
            Redis.from_url(
                redis_url,
                socket_timeout=0.5,
                socket_connect_timeout=0.5,
                decode_responses=True,
            )
            if redis_url else None
        )
    except Exception:  # noqa: BLE001
        redis_client = None
    if str(getattr(settings, "nats_url", "") or "").strip():
        try:
            import nats

            nc = await nats.connect(
                str(settings.nats_url).strip(), connect_timeout=3,
            )
            js = nc.jetstream()
        except Exception:  # noqa: BLE001
            nats_error = True

    try:
        result = await retire_all(
            execute=bool(args.execute),
            uploads_dir=settings.uploads_dir,
            custom_root=args.palace_root,
            allow_custom_root=bool(args.allow_custom_root),
            graph=graph,
            redis_client=redis_client,
            nats_js=js,
        )
    finally:
        if nc is not None:
            try:
                await nc.close()
            except Exception:  # noqa: BLE001
                pass
        if graph is not None:
            try:
                graph.close()
            except Exception:  # noqa: BLE001
                pass
        if redis_client is not None:
            try:
                redis_client.close()
            except Exception:  # noqa: BLE001
                pass

    if nats_error:
        result["nats"] = {**result["nats"], "status": "error"}
        result["errors"] = sorted(
            {error for error in result["errors"] if not error.startswith("nats:")}
            | {"nats:error"}
        )
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = asyncio.run(_run_cli(args))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
