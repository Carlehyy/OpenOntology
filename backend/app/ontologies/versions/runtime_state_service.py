"""Runtime-state conflict detection and deterministic release readiness gates."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import desc, func, tuple_
from sqlalchemy.orm import Session

from app.config import settings
from app.models.ontology_formal import (
    LinkInstance as FoLinkInstance,
    ObjectInstance as FoObjectInstance,
    PropertyFact,
)
from app.models.ontology_version import (
    OntologyTrialLink,
    OntologyTrialObject,
    OntologyTrialRun,
    OntologyVersion,
)
from app.models.sentinel import Sentinel
from app.data_channel.datasets.models import Dataset, DatasetVersion
from app.ontologies.versions.snapshot_contract import (
    complete_snapshot,
    snapshot_hash,
)
from app.ontologies.versions.evolution_service import (
    split_mapping_coverage_errors,
    validate_builtin_sentinel_contract,
    validate_manual_mapping_trial_contract,
    validate_release_mapping_contract,
)
from app.ontologies.versions.gate_contract import gate_error as _gate_error


def _dynamic_sentinel_id_conflict_errors(
        db: Session, ontology_id: str, sentinels: Any) -> list[dict]:
    """Protect the global Sentinel PK without mixing the two management schemas."""
    if not isinstance(sentinels, list):
        return []
    builtin_by_id = {
        str(item.get("id")).strip(): item
        for item in sentinels
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and str(item.get("id")).strip()
    }
    if not builtin_by_id:
        return []
    conflicts = {
        str(item[0])
        for item in db.query(Sentinel.id).filter(
            Sentinel.ontology_id == ontology_id,
            Sentinel.origin == "assistant_dynamic",
            Sentinel.id.in_(set(builtin_by_id)),
        ).all()
    }
    return [
        _gate_error(
            "sentinel_id_conflicts_dynamic",
            "sentinel",
            (
                f"建模内置哨兵 ID「{sentinel_id}」已被本体助手动态哨兵占用；"
                "两类哨兵必须使用不同 ID"
            ),
            item_id=sentinel_id,
            name=str(
                builtin_by_id[sentinel_id].get("displayName")
                or builtin_by_id[sentinel_id].get("name")
                or sentinel_id
            ),
            field="id",
        )
        for sentinel_id in sorted(conflicts)
    ]


def _verify_trial_dataset_pins(db: Session, run: OntologyTrialRun) -> list[dict]:
    errors = []
    for pin in run.dataset_versions or []:
        dataset = db.query(Dataset).filter(
            Dataset.id == pin.get("datasetId")).first()
        version = db.query(DatasetVersion).filter(
            DatasetVersion.id == pin.get("versionId"),
            DatasetVersion.dataset_id == pin.get("datasetId"),
        ).first()
        if dataset is None or version is None:
            errors.append(_gate_error(
                "trial_dataset_version_missing", "dataset",
                "试跑固定的数据版本已不存在",
                item_id=str(pin.get("datasetId") or "")))
            continue
        if dataset.latest_version_id != version.id:
            errors.append(_gate_error(
                "trial_dataset_version_stale", "dataset",
                f"数据集「{dataset.name}」在试跑后已产生新版本，请从该试跑版本创建新草稿后重新试跑",
                item_id=dataset.id))
        if version.checksum != pin.get("checksum"):
            errors.append(_gate_error(
                "trial_dataset_checksum_changed", "dataset",
                f"数据集「{dataset.name}」固定版本校验和变化，拒绝发布",
                item_id=dataset.id))
    return errors


_RUNTIME_STATE_CONFLICT_LIMIT = 50
_RUNTIME_FACT_QUERY_CHUNK = 300
_RUNTIME_FACT_QUERY_POSTGRES_CHUNK = 5000
_RUNTIME_STATE_MASK = "••••••（已隐藏）"
_RUNTIME_STATE_SENSITIVE_FIELD = re.compile(
    r"(?:password|passwd|pwd|secret|token|api[\s_-]?key|authorization|"
    r"credential|cookie|session|private[\s_-]?key|client[\s_-]?secret|"
    r"signature)",
    re.IGNORECASE,
)
_RUNTIME_STATE_JWT = re.compile(
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_RUNTIME_STATE_ACCESS_TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|"
    r"Bearer\s+[A-Za-z0-9._~+/=-]{12,})\b",
    re.IGNORECASE,
)
_RUNTIME_STATE_INLINE_SECRET = re.compile(
    r"(\b(?:password|passwd|pwd|secret|token|api[\s_-]?key|"
    r"authorization|credential)\b\s*[:=]\s*)"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;，；]+)",
    re.IGNORECASE,
)


def _empty_runtime_state_conflicts() -> dict:
    return {
        "totalCount": 0,
        "propertyConflictCount": 0,
        "objectConflictCount": 0,
        "linkConflictCount": 0,
        "itemLimit": _RUNTIME_STATE_CONFLICT_LIMIT,
        "truncated": False,
        "items": [],
    }


def _redact_runtime_state_value(
    value: Any, field_name: str = "", depth: int = 0,
) -> Any:
    """Bound and redact conflict values before they leave the backend."""
    if _RUNTIME_STATE_SENSITIVE_FIELD.search(str(field_name or "")):
        return _RUNTIME_STATE_MASK
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        redacted = _RUNTIME_STATE_JWT.sub("[令牌已隐藏]", value)
        redacted = _RUNTIME_STATE_ACCESS_TOKEN.sub("[凭据已隐藏]", redacted)
        redacted = _RUNTIME_STATE_INLINE_SECRET.sub(
            r"\1[凭据已隐藏]", redacted)
        return (
            redacted
            if len(redacted) <= 500
            else f"{redacted[:500]}…（已截断）"
        )
    if depth >= 4:
        return "[内容已折叠]"
    if isinstance(value, list):
        visible = [
            _redact_runtime_state_value(item, "", depth + 1)
            for item in value[:20]
        ]
        if len(value) > 20:
            visible.append(f"其余 {len(value) - 20} 项已折叠")
        return visible
    if isinstance(value, dict):
        entries = list(value.items())
        result = {
            str(key): _redact_runtime_state_value(
                item, str(key), depth + 1)
            for key, item in entries[:40]
        }
        if len(entries) > 40:
            result["…"] = f"其余 {len(entries) - 40} 个字段已折叠"
        return result
    return str(value)[:500]


def _is_lake_projection_fact_source(source: str | None) -> bool:
    """Return whether a property fact is a normal lake/release projection write.

    Publication is allowed to replace data-lake snapshots.  Every other source
    is runtime business state (actions, users/manual edits, collectors/imports,
    and future writers) and therefore fails closed instead of being guessed
    into an overwrite/retain policy.
    """
    value = str(source or "").strip().lower()
    return (
        value in {"pipeline", "pipeline-reconcile"}
        or value.startswith("pipeline://")
        or value.startswith("ontology-release://")
        or value.startswith("mapping://")
        or value.startswith("link-mapping://")
    )


def _safe_runtime_fact_source(source: str | None) -> str:
    """Keep useful provenance without leaking a user identifier in impact UI."""
    value = str(source or "unknown")
    if value.lower().startswith("user://"):
        return "user://[redacted]"
    return str(_redact_runtime_state_value(value))


def _runtime_fact_chunks(
    items: list[Any], chunk_size: int = _RUNTIME_FACT_QUERY_CHUNK,
) -> list[list[Any]]:
    return [
        items[index:index + chunk_size]
        for index in range(0, len(items), chunk_size)
    ]


def _runtime_fact_query_chunk_size(db: Session) -> int:
    # PostgreSQL safely supports a much larger bind budget than SQLite.  Keep
    # SQLite conservative for tests/embedded deployments while avoiding
    # thousands of round trips for a large production lake.
    dialect = str(getattr(db.get_bind().dialect, "name", "")).lower()
    return (
        _RUNTIME_FACT_QUERY_POSTGRES_CHUNK
        if dialect == "postgresql"
        else _RUNTIME_FACT_QUERY_CHUNK
    )


def _runtime_coordinate_facts(
    db: Session, *, ontology_id: str, release_ids: list[str],
    kind: str, coordinates: list[tuple[str, str]],
) -> list[PropertyFact]:
    """Return one canonical latest Fact per release/coordinate.

    The SQL window bounds materialization to
    ``len(release_ids) * len(coordinates)`` instead of loading the append-only
    history for every differing value into Python.
    """
    facts: list[PropertyFact] = []
    for chunk in _runtime_fact_chunks(
        coordinates, _runtime_fact_query_chunk_size(db),
    ):
        ranked = db.query(
            PropertyFact.id.label("fact_id"),
            func.row_number().over(
                partition_by=(
                    PropertyFact.ontology_release_id,
                    PropertyFact.instance_id,
                    PropertyFact.property_name,
                ),
                order_by=(
                    PropertyFact.recorded_at.desc(),
                    PropertyFact.seq.desc(),
                    PropertyFact.id.desc(),
                ),
            ).label("runtime_rank"),
        ).filter(
            PropertyFact.ontology_id == ontology_id,
            PropertyFact.ontology_release_id.in_(release_ids),
            PropertyFact.kind == kind,
            tuple_(
                PropertyFact.instance_id,
                PropertyFact.property_name,
            ).in_(chunk),
        ).subquery()
        facts.extend(db.query(PropertyFact).join(
            ranked, PropertyFact.id == ranked.c.fact_id,
        ).filter(ranked.c.runtime_rank == 1).all())
    return facts


def _runtime_existence_facts(
    db: Session, *, ontology_id: str, release_ids: list[str],
    kind: str, instance_ids: list[str],
) -> list[PropertyFact]:
    """Return one canonical latest existence Fact per release/instance."""
    facts: list[PropertyFact] = []
    for chunk in _runtime_fact_chunks(
        instance_ids, _runtime_fact_query_chunk_size(db),
    ):
        ranked = db.query(
            PropertyFact.id.label("fact_id"),
            func.row_number().over(
                partition_by=(
                    PropertyFact.ontology_release_id,
                    PropertyFact.instance_id,
                    PropertyFact.property_name,
                ),
                order_by=(
                    PropertyFact.recorded_at.desc(),
                    PropertyFact.seq.desc(),
                    PropertyFact.id.desc(),
                ),
            ).label("runtime_rank"),
        ).filter(
            PropertyFact.ontology_id == ontology_id,
            PropertyFact.ontology_release_id.in_(release_ids),
            PropertyFact.kind == kind,
            PropertyFact.property_name == "exists",
            PropertyFact.instance_id.in_(chunk),
        ).subquery()
        facts.extend(db.query(PropertyFact).join(
            ranked, PropertyFact.id == ranked.c.fact_id,
        ).filter(ranked.c.runtime_rank == 1).all())
    return facts


def _runtime_latest_by_scope(
    facts: list[PropertyFact], *, current_release_id: str,
    ancestor_release_ids: list[str],
) -> tuple[dict[tuple[str, str], PropertyFact],
           dict[tuple[str, str], PropertyFact]]:
    current: dict[tuple[str, str], PropertyFact] = {}
    ancestor: dict[tuple[str, str], PropertyFact] = {}
    ancestor_rank = {
        release_id: rank
        for rank, release_id in enumerate(ancestor_release_ids)
    }
    selected_rank: dict[tuple[str, str], int] = {}
    for fact in facts:
        key = (str(fact.instance_id), str(fact.property_name))
        release_id = str(fact.ontology_release_id or "")
        if release_id == str(current_release_id):
            current.setdefault(key, fact)
            continue
        rank = ancestor_rank.get(release_id)
        if rank is None:
            continue
        previous_rank = selected_rank.get(key)
        if previous_rank is None or rank < previous_rank:
            ancestor[key] = fact
            selected_rank[key] = rank
    return current, ancestor


def _release_ancestor_context(
    db: Session, ontology_id: str, current_release_id: str,
) -> tuple[list[str], bool, bool]:
    rows = db.query(
        OntologyVersion.id,
        OntologyVersion.parent_version_id,
        OntologyVersion.node_kind,
        OntologyVersion.promoted_from_id,
    ).filter(OntologyVersion.ontology_id == ontology_id).all()
    releases = {
        str(row.id): row for row in rows
        if (row.node_kind or "release") == "release"
    }
    result: list[str] = []
    seen = {str(current_release_id)}
    current = releases.get(str(current_release_id))
    explicit_trial_activation = bool(
        current is not None and current.promoted_from_id)
    cursor = str(current.parent_version_id or "") if current else ""
    reset_boundary_reached = False
    while cursor and cursor not in seen:
        seen.add(cursor)
        row = releases.get(cursor)
        if row is None:
            break
        result.append(cursor)
        if row.promoted_from_id:
            # Rollback/legacy activations inherit only as far as the nearest
            # complete promotion baseline, never through it.
            reset_boundary_reached = True
            break
        cursor = str(row.parent_version_id or "")
    return result, explicit_trial_activation, reset_boundary_reached


def _runtime_state_conflicts_batch(
    db: Session, *, ontology_id: str, current_release_id: str,
    trial_objects: list[OntologyTrialObject],
    trial_links: list[OntologyTrialLink],
    current_objects: list[FoObjectInstance],
    current_link_rows: list[FoLinkInstance],
    ancestor_context: tuple[list[str], bool, bool],
) -> dict:
    """Find trial values that would erase newer non-lake runtime facts.

    Current-release facts take precedence.  A normal trial promotion is an
    explicit new baseline, while a rollback/legacy activation inherits the
    nearest matching ancestor provenance.  Ordering matches the canonical Fact
    reader (recorded_at, per-property seq, id), including same-millisecond
    writes.
    """
    candidates = {str(item.object_id): item for item in trial_objects}
    (
        ancestor_release_ids,
        explicit_trial_activation,
        reset_boundary_reached,
    ) = ancestor_context
    current_object_by_id = {
        str(item.id): item for item in current_objects
    }

    differing: dict[
        tuple[str, str],
        tuple[Any, bool, Any, bool, str | None, str | None],
    ] = {}
    for current_object in current_objects:
        candidate = candidates.get(str(current_object.id))
        current_props = dict(current_object.properties or {})
        candidate_props = dict(candidate.properties or {}) if candidate else {}
        candidate_object_present = candidate is not None
        property_names = (
            current_props.keys() | candidate_props.keys()
            if candidate_object_present
            else current_props.keys()
        )
        for property_name in property_names:
            current_present = property_name in current_props
            current_value = (
                current_props.get(property_name)
                if current_present
                else None
            )
            candidate_present = (
                candidate_object_present and property_name in candidate_props
            )
            candidate_value = (
                candidate_props.get(property_name) if candidate_present else None
            )
            if (
                current_present != candidate_present
                or (
                    current_present
                    and candidate_present
                    and current_value != candidate_value
                )
            ):
                differing[(str(current_object.id), str(property_name))] = (
                    current_value,
                    current_present,
                    candidate_value,
                    candidate_present,
                    str(current_object.object_type_id or "") or None,
                    str(current_object.source or "") or None,
                )
    release_scope = [str(current_release_id), *ancestor_release_ids]
    facts = (
        _runtime_coordinate_facts(
            db,
            ontology_id=ontology_id,
            release_ids=release_scope,
            kind="property",
            coordinates=sorted(differing),
        )
        if differing
        else []
    )
    latest, ancestor_latest = _runtime_latest_by_scope(
        facts,
        current_release_id=current_release_id,
        ancestor_release_ids=ancestor_release_ids,
    )

    conflicts: list[dict] = []

    def property_fact_matches_projection(
        fact: PropertyFact | None, current_value: Any, current_present: bool,
    ) -> bool:
        if fact is None:
            return False
        payload = fact.value or {}
        if current_present:
            return (
                payload.get("present") is not False
                and payload.get("v") == current_value
            )
        # Legacy {"v": None} facts prove explicit null, not removal: the old
        # writer did not emit removal facts at all.  Ambiguous history must
        # fail closed rather than laundering an unattributed deletion into a
        # lake-authoritative state.
        return payload.get("present") is False

    for key in sorted(differing):
        fact = latest.get(key)
        (
            current_value,
            current_present,
            candidate_value,
            candidate_present,
            object_type_id,
            object_source,
        ) = (
            differing[key]
        )
        fact_matches_projection = property_fact_matches_projection(
            fact, current_value, current_present,
        )
        if (
            fact_matches_projection
            and _is_lake_projection_fact_source(fact.source)
        ):
            continue
        ancestor_fact = ancestor_latest.get(key)
        ancestor_matches_projection = property_fact_matches_projection(
            ancestor_fact, current_value, current_present,
        )
        if (
            not current_present
            and candidate_present
            and fact is None
            and ancestor_fact is None
        ):
            # A draft adding a genuinely new property is normal schema/data
            # evolution.  Absence of both current and historical provenance is
            # not runtime drift and must not block publication.
            continue
        if (
            fact is None
            and str(object_source or "").lower() == "pipeline"
            and (
                (
                    explicit_trial_activation
                    and ancestor_matches_projection
                )
                or (
                    ancestor_matches_projection
                    and _is_lake_projection_fact_source(
                        ancestor_fact.source)
                )
                or (
                    reset_boundary_reached
                    and ancestor_fact is None
                )
            )
        ):
            # A no-op activation rebinds the materialized lake object to the
            # new release but intentionally appends no duplicate Fact.  The
            # release-owned pipeline projection is the activation baseline.
            continue
        reported_fact = fact if fact_matches_projection else (
            ancestor_fact
            if (
                not explicit_trial_activation
                and ancestor_matches_projection
            )
            else None
        )
        conflicts.append({
            "resourceKind": "objectProperty",
            "objectId": key[0],
            "objectTypeId": object_type_id,
            "property": key[1],
            "current": _redact_runtime_state_value(
                current_value, key[1]),
            "currentPresent": current_present,
            "candidate": _redact_runtime_state_value(
                candidate_value, key[1]),
            "candidatePresent": candidate_present,
            "candidateObjectPresent": key[0] in candidates,
            "source": _safe_runtime_fact_source(
                reported_fact.source if reported_fact is not None else None),
            "factId": (
                reported_fact.id if reported_fact is not None else None),
        })

    # Object existence is a first-class temporal chain.  This covers both
    # runtime-created objects which a candidate would delete and tombstoned
    # objects which a candidate would revive, including zero-property objects.
    object_ids = sorted(set(current_object_by_id) | set(candidates))
    object_facts = _runtime_existence_facts(
        db,
        ontology_id=ontology_id,
        release_ids=release_scope,
        kind="object",
        instance_ids=object_ids,
    ) if object_ids else []
    current_object_facts, ancestor_object_facts = _runtime_latest_by_scope(
        object_facts,
        current_release_id=current_release_id,
        ancestor_release_ids=ancestor_release_ids,
    )
    current_object_facts_by_id = {
        key[0]: fact for key, fact in current_object_facts.items()
    }
    ancestor_object_facts_by_id = {
        key[0]: fact for key, fact in ancestor_object_facts.items()
    }

    for object_id in sorted(set(current_object_by_id) - set(candidates)):
        current_object = current_object_by_id[object_id]
        fact = current_object_facts_by_id.get(object_id)
        fact_matches_projection = (
            fact is not None
            and (fact.value or {}).get("v") is True
        )
        ancestor_fact = ancestor_object_facts_by_id.get(object_id)
        ancestor_matches_projection = (
            ancestor_fact is not None
            and (ancestor_fact.value or {}).get("v") is True
        )
        if (
            fact_matches_projection
            and _is_lake_projection_fact_source(fact.source)
        ):
            continue
        if fact is None and (
            (
                explicit_trial_activation
                and ancestor_matches_projection
                and str(current_object.source or "").lower() == "pipeline"
            )
            or (
                ancestor_matches_projection
                and _is_lake_projection_fact_source(ancestor_fact.source)
            )
            or (
                reset_boundary_reached
                and ancestor_fact is None
                and str(current_object.source or "").lower() == "pipeline"
            )
        ):
            continue
        reported_fact = fact if fact_matches_projection else (
            ancestor_fact
            if (
                not explicit_trial_activation
                and ancestor_matches_projection
            )
            else None
        )
        conflicts.append({
            "resourceKind": "object",
            "objectId": object_id,
            "objectTypeId": (
                str(current_object.object_type_id or "") or None),
            "current": _redact_runtime_state_value({
                "exists": True,
                "objectTypeId": str(current_object.object_type_id),
                "properties": dict(current_object.properties or {}),
            }),
            "candidate": {"exists": False},
            "source": _safe_runtime_fact_source(
                reported_fact.source if reported_fact is not None else None),
            "factId": (
                reported_fact.id if reported_fact is not None else None),
        })

    for object_id in sorted(set(candidates) - set(current_object_by_id)):
        candidate = candidates[object_id]
        fact = current_object_facts_by_id.get(object_id)
        fact_matches_projection = (
            fact is not None
            and (fact.value or {}).get("v") is False
        )
        ancestor_fact = ancestor_object_facts_by_id.get(object_id)
        ancestor_matches_projection = (
            ancestor_fact is not None
            and (ancestor_fact.value or {}).get("v") is False
        )
        if (
            fact_matches_projection
            and _is_lake_projection_fact_source(fact.source)
        ):
            continue
        if fact is None and ancestor_fact is None:
            # No existence history means a genuine first release addition.
            continue
        if (
            fact is None
            and ancestor_matches_projection
            and (
                explicit_trial_activation
                or _is_lake_projection_fact_source(ancestor_fact.source)
            )
        ):
            continue
        reported_fact = fact if fact_matches_projection else (
            ancestor_fact
            if (
                not explicit_trial_activation
                and ancestor_matches_projection
            )
            else None
        )
        conflicts.append({
            "resourceKind": "object",
            "objectId": object_id,
            "objectTypeId": str(candidate.object_type_id or "") or None,
            "current": {"exists": False},
            "candidate": _redact_runtime_state_value({
                "exists": True,
                "objectTypeId": str(candidate.object_type_id),
                "properties": dict(candidate.properties or {}),
            }),
            "source": _safe_runtime_fact_source(
                reported_fact.source if reported_fact is not None else None),
            "factId": (
                reported_fact.id if reported_fact is not None else None),
        })

    current_links = {
        str(item.id): item for item in current_link_rows
    }
    candidate_links = {
        str(item.link_id): item for item in trial_links
    }
    link_ids = sorted(current_links.keys() | candidate_links.keys())
    link_facts = _runtime_existence_facts(
        db,
        ontology_id=ontology_id,
        release_ids=release_scope,
        kind="link",
        instance_ids=link_ids,
    ) if link_ids else []
    scoped_link_facts, scoped_ancestor_link_facts = (
        _runtime_latest_by_scope(
            link_facts,
            current_release_id=current_release_id,
            ancestor_release_ids=ancestor_release_ids,
        )
    )
    latest_link_facts = {
        key[0]: fact for key, fact in scoped_link_facts.items()
    }
    ancestor_link_facts = {
        key[0]: fact for key, fact in scoped_ancestor_link_facts.items()
    }

    def link_state(item: Any | None) -> dict:
        if item is None:
            return {"exists": False}
        return {
            "exists": True,
            "linkTypeId": str(item.link_type_id),
            "sourceObjectId": str(item.source_object_id),
            "targetObjectId": str(item.target_object_id),
            "properties": dict(item.properties or {}),
        }

    for link_id in link_ids:
        current_item = current_links.get(link_id)
        candidate_item = candidate_links.get(link_id)
        current_state = link_state(current_item)
        candidate_state = link_state(candidate_item)
        if current_state == candidate_state:
            continue
        fact = latest_link_facts.get(link_id)
        current_exists = current_item is not None
        fact_matches_projection = (
            fact is not None
            and isinstance((fact.value or {}).get("v"), bool)
            and (fact.value or {}).get("v") == current_exists
        )
        # A candidate-only ID with no Fact is a genuine first release of that
        # relationship, not unattributed runtime drift.  A current row without
        # provenance remains fail-closed; a candidate-only ID with a current-
        # release tombstone is handled below and can therefore block revival.
        ancestor_fact = ancestor_link_facts.get(link_id)
        ancestor_matches_projection = (
            ancestor_fact is not None
            and isinstance((ancestor_fact.value or {}).get("v"), bool)
            and (ancestor_fact.value or {}).get("v") == current_exists
        )
        if fact is None and current_item is None:
            if ancestor_fact is None:
                continue
            if (ancestor_fact.value or {}).get("v") is False:
                if (
                    explicit_trial_activation
                    or _is_lake_projection_fact_source(
                        ancestor_fact.source)
                ):
                    continue
                fact = ancestor_fact
                fact_matches_projection = True
        if (
            fact is None
            and current_item is not None
            and (
                (
                    explicit_trial_activation
                    and ancestor_matches_projection
                )
                or (
                    bool(current_item.source_relation_id)
                    and
                    ancestor_matches_projection
                    and _is_lake_projection_fact_source(
                        ancestor_fact.source)
                )
                or (
                    bool(current_item.source_relation_id)
                    and
                    reset_boundary_reached
                    and ancestor_fact is None
                )
            )
        ):
            # Same no-op activation baseline as objects: promoted lake links
            # are explicitly adopted by a normal release even when a legacy
            # edge has no Relation id.  Rollback inheritance remains stricter:
            # it needs immutable Relation lineage before an implicit baseline
            # can be trusted.
            continue
        if (
            fact is None
            and not explicit_trial_activation
            and ancestor_matches_projection
            and not _is_lake_projection_fact_source(
                ancestor_fact.source)
        ):
            fact = ancestor_fact
            fact_matches_projection = True
        if (
            fact_matches_projection
            and _is_lake_projection_fact_source(fact.source)
        ):
            continue
        conflicts.append({
            "resourceKind": "link",
            "linkId": link_id,
            "linkTypeId": str(
                getattr(current_item, "link_type_id", None)
                or getattr(candidate_item, "link_type_id", None)
                or (
                    fact.object_type_id
                    if fact_matches_projection else None
                )
                or ""
            ),
            "current": _redact_runtime_state_value(current_state),
            "candidate": _redact_runtime_state_value(candidate_state),
            "source": _safe_runtime_fact_source(
                fact.source if fact_matches_projection else None),
            "factId": fact.id if fact_matches_projection else None,
        })

    conflicts.sort(key=lambda item: (
        str(item.get("resourceKind") or ""),
        str(item.get("objectId") or item.get("linkId") or ""),
        str(item.get("property") or ""),
    ))
    total = len(conflicts)
    property_count = sum(
        item["resourceKind"] == "objectProperty" for item in conflicts)
    object_count = sum(
        item["resourceKind"] == "object" for item in conflicts)
    link_count = total - property_count - object_count
    return {
        "totalCount": total,
        "propertyConflictCount": property_count,
        "objectConflictCount": object_count,
        "linkConflictCount": link_count,
        "itemLimit": _RUNTIME_STATE_CONFLICT_LIMIT,
        "truncated": total > _RUNTIME_STATE_CONFLICT_LIMIT,
        "items": conflicts[:_RUNTIME_STATE_CONFLICT_LIMIT],
    }


def _runtime_state_conflicts(
    db: Session, *, ontology_id: str, current_release_id: str,
    trial_objects, trial_links,
) -> dict:
    """Compare every candidate in bounded SQL-aligned batches.

    Query inputs stay queries throughout the release path. Existing callers may
    also supply bounded in-memory candidates; they use the same comparison and
    provenance rules. Counts are exact even when only the first 50 are exposed.
    """
    from app.ontologies.versions.batching import RELEASE_BATCH_SIZE
    from sqlalchemy.orm import Query

    result = _empty_runtime_state_conflicts()
    ancestor_context = _release_ancestor_context(
        db, ontology_id, current_release_id,
    )

    def key(item):
        return (
            str(item.get("resourceKind") or ""),
            str(item.get("objectId") or item.get("linkId") or ""),
            str(item.get("property") or ""),
        )

    def merge(batch):
        for field in (
            "totalCount", "propertyConflictCount", "objectConflictCount",
            "linkConflictCount",
        ):
            result[field] += batch[field]
        result["items"] = sorted(
            result["items"] + batch["items"], key=key,
        )[:_RUNTIME_STATE_CONFLICT_LIMIT]

    def compare(candidates, current_rows, live_model):
        merge(_runtime_state_conflicts_batch(
            db, ontology_id=ontology_id,
            current_release_id=current_release_id,
            trial_objects=candidates if live_model is FoObjectInstance else [],
            trial_links=candidates if live_model is FoLinkInstance else [],
            current_objects=current_rows if live_model is FoObjectInstance else [],
            current_link_rows=current_rows if live_model is FoLinkInstance else [],
            ancestor_context=ancestor_context,
        ))

    for candidate_rows, candidate_key, live_model in (
        (trial_objects, OntologyTrialObject.object_id, FoObjectInstance),
        (trial_links, OntologyTrialLink.link_id, FoLinkInstance),
    ):
        live = db.query(live_model).filter(
            live_model.ontology_id == ontology_id,
            live_model.ontology_release_id == current_release_id,
        )
        is_query = isinstance(candidate_rows, Query)
        if is_query:
            # Merge only two indexed key pages. A global UNION/DISTINCT inside
            # each page would repeatedly sort millions of candidate keys.
            after = None
            while True:
                live_ids = live.with_entities(live_model.id)
                trial_ids = candidate_rows.with_entities(candidate_key)
                if after is not None:
                    live_ids = live_ids.filter(live_model.id > after)
                    trial_ids = trial_ids.filter(candidate_key > after)
                live_page = live_ids.order_by(live_model.id).limit(RELEASE_BATCH_SIZE).all()
                trial_page = trial_ids.order_by(candidate_key).limit(RELEASE_BATCH_SIZE).all()
                batch_ids = sorted({row[0] for row in live_page + trial_page})[:RELEASE_BATCH_SIZE]
                if not batch_ids:
                    break
                after = batch_ids[-1]
                candidates = candidate_rows.filter(
                    candidate_key.in_(batch_ids),
                ).all()
                current_rows = live.filter(live_model.id.in_(batch_ids)).all()
                compare(candidates, current_rows, live_model)
        else:
            candidate_by_id = {
                str(getattr(item, candidate_key.key)): item
                for item in candidate_rows
            }
            after = None
            pending = set(candidate_by_id)
            while True:
                current_query = live
                if after is not None:
                    current_query = current_query.filter(live_model.id > after)
                current_rows = current_query.order_by(live_model.id).limit(
                    RELEASE_BATCH_SIZE,
                ).all()
                if not current_rows:
                    break
                after = current_rows[-1].id
                batch_ids = [str(item.id) for item in current_rows]
                candidates = [candidate_by_id[i] for i in batch_ids if i in candidate_by_id]
                pending.difference_update(batch_ids)
                compare(candidates, current_rows, live_model)
            ordered = sorted(pending)
            for offset in range(0, len(ordered), RELEASE_BATCH_SIZE):
                candidates = [candidate_by_id[i] for i in ordered[offset:offset + RELEASE_BATCH_SIZE]]
                compare(candidates, [], live_model)
    result["truncated"] = result["totalCount"] > _RUNTIME_STATE_CONFLICT_LIMIT
    return result


def _release_readiness(
        db: Session, *, draft: OntologyVersion,
        current: OntologyVersion, report: dict,
        release_mapping_validator=validate_release_mapping_contract) -> dict:
    """Return a read-only, structured preview of every deterministic publish gate.

    The impact dialog consumes this before the user confirms publication.  It
    deliberately never mutates the trial record: the authoritative promote
    endpoint repeats the same fail-closed checks under row locks.
    """
    snap = complete_snapshot(draft.snapshot_formal)
    errors: list[dict] = []

    if draft.lifecycle_status != "trial_ready":
        errors.append(_gate_error(
            "trial_ready_required", "version",
            "只有已通过并冻结的试跑态版本可以转为发布态",
            item_id=draft.id, name=draft.version_number))
    if draft.base_release_id != current.id:
        errors.append(_gate_error(
            "draft_base_outdated", "version",
            "当前发布版已变化，需要先基于最新发布版合并本分支改动",
            item_id=draft.id, name=draft.version_number))

    # Revalidate mappings even for legacy passed trials. Older deployments may
    # have allowed partial mappings, while current publication is fail-closed.
    errors.extend(validate_builtin_sentinel_contract(snap["sentinels"]))
    errors.extend(_dynamic_sentinel_id_conflict_errors(
        db, draft.ontology_id, snap["sentinels"],
    ))
    # 映射覆盖类问题（类型未映射/属性未全覆盖）只进 warnings：发布前检查
    # 弹窗按风险提示渲染，不再置 ready=false；绑定完整性问题仍阻断。
    mapping_errors, mapping_coverage_warnings = split_mapping_coverage_errors(
        release_mapping_validator(snap))
    errors.extend(mapping_errors)

    run = db.query(OntologyTrialRun).filter(
        OntologyTrialRun.ontology_id == draft.ontology_id,
        OntologyTrialRun.version_id == draft.id,
        OntologyTrialRun.status == "passed",
    ).order_by(desc(OntologyTrialRun.created_at)).first()
    exact_trial = False
    runtime_conflicts = _empty_runtime_state_conflicts()
    if run is None:
        errors.append(_gate_error(
            "passed_trial_required", "trialRun",
            "发布前必须先完成一次通过的隔离试跑",
            item_id=draft.id, name=draft.version_number))
    else:
        current_hash = snapshot_hash(snap)
        exact_trial = (
            run.revision == (draft.revision or 0)
            and run.snapshot_hash == draft.snapshot_hash
            and run.snapshot_hash == current_hash
        )
        if not exact_trial:
            errors.append(_gate_error(
                "trial_snapshot_stale", "trialRun",
                "试跑记录与当前快照不一致，需要创建新草稿并重新试跑",
                item_id=run.id, name=draft.version_number))
        else:
            errors.extend(_verify_trial_dataset_pins(db, run))
            if settings.environment == "production":
                errors.extend(validate_manual_mapping_trial_contract(
                    db, snap, run.dataset_versions,
                ))
            expected = (run.result_json or {}).get("counts") or {}
            object_count = db.query(OntologyTrialObject).filter(
                OntologyTrialObject.trial_run_id == run.id).count()
            link_count = db.query(OntologyTrialLink).filter(
                OntologyTrialLink.trial_run_id == run.id).count()
            if (object_count != int(expected.get("objects") or 0)
                    or link_count != int(expected.get("links") or 0)):
                errors.append(_gate_error(
                    "trial_materialization_incomplete", "trialRun",
                    "试跑隔离投影不完整，需要创建新草稿后重新试跑",
                    item_id=run.id, name=draft.version_number))
            if run.impact_hash != report.get("impactHash"):
                errors.append(_gate_error(
                    "trial_impact_stale", "trialRun",
                    "试跑影响范围与当前发布基线不一致，需要重新试跑",
                    item_id=run.id, name=draft.version_number))
            runtime_conflicts = _runtime_state_conflicts(
                db,
                ontology_id=draft.ontology_id,
                current_release_id=current.id,
                trial_objects=db.query(OntologyTrialObject).filter(
                    OntologyTrialObject.trial_run_id == run.id,
                ),
                trial_links=db.query(OntologyTrialLink).filter(
                    OntologyTrialLink.trial_run_id == run.id,
                ),
            )
            if runtime_conflicts["totalCount"]:
                issue = _gate_error(
                    "runtime_state_conflict", "runtimeState",
                    "试跑候选会覆盖当前发布版中的非数据湖运行态事实，"
                    "系统不会自动选择保留或覆盖",
                    item_id=run.id,
                    name=draft.version_number,
                    field="runtimeStateConflicts",
                )
                issue["conflictCount"] = runtime_conflicts["totalCount"]
                errors.append(issue)

    ready = len(errors) == 0
    base_outdated = draft.base_release_id != current.id
    return {
        "ready": ready,
        "blockingCount": len(errors),
        "errors": errors,
        "warnings": mapping_coverage_warnings,
        "trialRunId": run.id if run else None,
        "runtimeStateConflicts": runtime_conflicts,
        "repairStrategy": (
            None
            if ready or runtime_conflicts["totalCount"]
            else "rebase" if base_outdated else "create_draft"
        ),
        "repairSourceVersionId": current.id if base_outdated else draft.id,
    }
