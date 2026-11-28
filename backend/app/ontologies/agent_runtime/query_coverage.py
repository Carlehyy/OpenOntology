"""Read-only data coverage of the authorized workspace or pinned release.

Coverage describes the available query path, not synchronization completeness.
A local fact is evidence even without a mapping; a definition alone is not.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

from sqlalchemy import cast, func, literal, select, union_all
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import aliased

from app.config import settings
from app.ontologies.agent_query.errors import QueryError
from app.ontologies.agent_query.relation_keys import published_relation_keys
from app.ontologies.agent_runtime.property_visibility import remote_binding_states
from app.ontologies.formal_modeling.models import LinkInstance, ObjectInstance
from app.ontologies.mappings.formal_projection_contract import projection_property_mappings
from app.ontologies.mappings.models import OntologyLinkMapping, OntologyMapping
from app.ontologies.mappings.remote_binding import (
    BINDING_MODE_LOCAL, RemoteBindingError, mapping_binding_mode, snapshot_link_binding_mode,
)

OBJECT_UNMAPPED = "object_data_unmapped"
RELATION_UNMAPPED = "relation_data_unmapped"
LOCAL_REMOTE_FENCE = "remote_ontology_semantic_query_required"
MAX_GAPS = 8


@dataclass(frozen=True)
class DataAccess:
    mode: str
    reason: str | None = None

    def payload(self) -> dict:
        data = {"dataMode": self.mode, "queryable": self.reason is None}
        if self.reason:
            data["unqueryableReason"] = self.reason
        return data


def coverage_gap(code: str, *, object_type=None, link_type=None,
                 property_name: str | None = None) -> dict:
    """Only authorized definition metadata belongs in a public coverage gap."""
    if link_type is not None:
        return {"code": code, "kind": "linkType", "linkTypeId": str(link_type.id),
                "label": str(link_type.display_name or link_type.name)[:160]}
    if object_type is not None:
        gap = {"code": code, "kind": "objectType", "objectTypeId": str(object_type.id),
               "label": str(object_type.display_name or object_type.name)[:160]}
        if property_name is not None:
            gap.update(kind="property", property=property_name[:160])
        return gap
    return {"code": code, "kind": "source"}


def partial_coverage(gaps: list[dict]) -> dict:
    if not gaps:
        return {}
    coverage = {"gaps": gaps[:MAX_GAPS]}
    if len(gaps) > MAX_GAPS:
        coverage["gapsOmitted"] = len(gaps) - MAX_GAPS
    return {"partial": True, "completeness": "partial",
            "completenessScope": "query_execution", "dataCoverage": coverage}


def coverage_metadata(result: dict, keep: int = MAX_GAPS) -> dict:
    """Keep coverage across model previews, tool display and ledger compaction."""
    metadata = {key: result[key] for key in (
        "partial", "completeness", "completenessScope", "dataCoverage") if key in result}
    coverage = metadata.get("dataCoverage")
    if isinstance(coverage, dict) and isinstance(coverage.get("gaps"), list):
        gaps = coverage["gaps"]
        fields = ("code", "kind", "label", "objectTypeId", "linkTypeId", "property")
        bounded = [{key: gap[key][:160] for key in fields if isinstance(gap.get(key), str)}
                   for gap in gaps[:keep] if isinstance(gap, dict)]
        omitted = int(coverage.get("gapsOmitted") or 0) + len(gaps) - len(bounded)
        metadata["dataCoverage"] = {"gaps": bounded}
        if omitted:
            metadata["dataCoverage"]["gapsOmitted"] = omitted
    return metadata


def _existing_ids(db, probes):
    # Batched index probes stop at the first fact for each type. DISTINCT/count
    # over business rows would make prompt construction depend on dataset size.
    result = set()
    for start in range(0, len(probes), 100):
        batch = probes[start:start + 100]
        result.update(db.scalars(union_all(*batch) if len(batch) > 1 else batch[0]))
    return result


class QueryCoverage:
    def __init__(self, scope, remote_ids: set[str]):
        self.scope = scope
        self.remote_ids = set(scope.object_types) & remote_ids
        self._property_facts: dict[str, dict[str, str | None]] = {}

    @cached_property
    def property_states(self) -> dict:
        return remote_binding_states(self.scope, self.remote_ids)

    def object(self, ot) -> DataAccess:
        if ot.id in self.remote_ids:
            reason = self.property_states[ot.id]["reason"]
            if not self.scope.release_id and not reason:
                reason = "query_objects_release_required"
            return DataAccess("remote", reason)
        if ot.id in self._local_datasets or ot.id in self._object_facts:
            return DataAccess("materialized")
        return DataAccess("unmapped", OBJECT_UNMAPPED)

    @cached_property
    def _local_mappings(self) -> dict[str, list[tuple[str, dict]]]:
        scope = self.scope
        by_name = {}
        for ot in scope.object_types.values():
            for name in (ot.name, ot.display_name):
                if name:
                    by_name.setdefault(name, set()).add(ot.id)
        if scope.release is not None:
            rows = ((entry.get("targetObjectTypeId"), entry.get("entityClass"),
                     entry.get("curatedDatasetId"), entry.get("fieldMapping"))
                    for entry in scope.release.snapshot.get("mappings") or []
                    if isinstance(entry, dict) and entry.get("status") == "applied"
                    and snapshot_link_binding_mode(entry) == BINDING_MODE_LOCAL)
        else:
            mappings = scope.db.query(OntologyMapping).filter_by(
                ontology_id=scope.ontology.id, status="applied").all()
            rows = ((m.target_object_type_id, m.entity_class, m.curated_dataset_id, m.field_mapping)
                    for m in mappings if mapping_binding_mode(m.field_mapping) == BINDING_MODE_LOCAL)
        by_type = {}
        for target, name, dataset_id, fields in rows:
            matches = by_name.get(name, ())
            type_id = target or (next(iter(matches)) if len(matches) == 1 else None)
            if dataset_id and type_id in scope.object_types and type_id not in self.remote_ids:
                by_type.setdefault(type_id, []).append((str(dataset_id), fields or {}))
        return by_type

    @cached_property
    def _local_datasets(self) -> dict[str, set[str]]:
        return {type_id: {dataset for dataset, _ in mappings}
                for type_id, mappings in self._local_mappings.items()}

    def properties(self, ot, names) -> dict[str, DataAccess]:
        """Check only requested, authorized properties, never during prompt assembly."""
        names = list(dict.fromkeys(names))
        if not names:
            return {}
        access = self.object(ot)
        if access.reason:
            return dict.fromkeys(names, access)
        if access.mode == "remote":
            mapped = self.property_states[ot.id]["queryable"]
            return {name: access if name in mapped else DataAccess("remote", "remote_property_unmapped")
                    for name in names}

        definitions = {item["name"]: item for item in (ot.properties or [])
                       if isinstance(item, dict) and item.get("name")}
        mapped = {item["property"] for _, fields in self._local_mappings.get(ot.id, [])
                  for item in projection_property_mappings(fields)}
        # Computed values live in a different projection; an input column does
        # not establish coverage for a computed property with the same name.
        missing = {name: bool(definitions[name].get("source") == "computed"
                              or definitions[name].get("computed")) for name in names}
        missing = {name: computed for name, computed in missing.items()
                   if computed or name not in mapped}
        facts = self._local_property_facts(ot.id, missing)
        return {name: (DataAccess("materialized", facts[name]) if name in missing else access)
                for name in names}

    def _local_property_facts(self, type_id, missing: dict[str, bool]) -> dict[str, str | None]:
        cache = self._property_facts.setdefault(type_id, {})
        pending = {name: computed for name, computed in missing.items() if name not in cache}
        if not pending:
            return cache
        db = self.scope.db
        pg = db.get_bind().dialect.name == "postgresql"
        probes = []
        for name, computed in pending.items():
            column = ObjectInstance.computed if computed else ObjectInstance.properties
            if pg:
                present = cast(column, JSONB).has_key(name)
            else:
                entry = func.json_each(column).table_valued("key")
                present = select(literal(1)).where(entry.c.key == name).correlate(ObjectInstance).exists()
            probes.append(select(literal(name)).where(select(ObjectInstance.id).where(
                ObjectInstance.object_type_id == type_id, *self._in_scope(ObjectInstance),
                present).exists()))
        try:
            if pg:
                # JSON-key probes can scan a type when no fact is present. End
                # the read-only savepoint to restore the caller's timeout.
                with db.begin_nested() as probe:
                    db.execute(select(func.set_config(
                        "statement_timeout", str(settings.agent_query_statement_timeout_ms), True)))
                    found = _existing_ids(db, probes)
                    probe.rollback()
            else:
                found = _existing_ids(db, probes)
        except DBAPIError:
            return {**cache, **dict.fromkeys(pending, "query_coverage_unavailable")}
        cache.update({name: None if name in found else "property_data_unmapped" for name in pending})
        return cache

    def _in_scope(self, model):
        predicates = [model.ontology_id == self.scope.ontology.id]
        if self.scope.release_id is not None:
            predicates.append(model.ontology_release_id == self.scope.release_id)
        return predicates

    @cached_property
    def _object_facts(self) -> set[str]:
        candidates = self.scope.object_types.keys() - self.remote_ids - self._local_datasets.keys()
        return _existing_ids(self.scope.db, [
            select(literal(type_id)).where(select(ObjectInstance.id).where(
                ObjectInstance.object_type_id == type_id, *self._in_scope(ObjectInstance),
            ).exists()) for type_id in sorted(candidates)
        ])

    def link(self, link_type, *, local: bool = False) -> DataAccess:
        endpoints = {link_type.source_object_type_id, link_type.target_object_type_id}
        if endpoints & self.remote_ids:
            if local or self.scope.release is None:
                return DataAccess("remote", LOCAL_REMOTE_FENCE)
            if endpoints - self.remote_ids:
                return DataAccess("remote", "remote_relation_unsupported")
            return self._remote_link(link_type)
        if link_type.id in self._mapped_links or link_type.id in self._link_facts:
            return DataAccess("materialized")
        return DataAccess("unmapped", RELATION_UNMAPPED)

    @cached_property
    def _mapped_links(self) -> set[str]:
        scope = self.scope
        if scope.release is not None:
            rows = ((entry.get("linkTypeId"), entry.get("relationType"),
                     entry.get("srcDatasetId"), entry.get("tgtDatasetId"),
                     entry.get("srcKey"), entry.get("tgtKey"))
                    for entry in scope.release.snapshot.get("linkMappings") or []
                    if isinstance(entry, dict) and entry.get("status") in {"active", "inferred"}
                    and snapshot_link_binding_mode(entry) == BINDING_MODE_LOCAL)
        else:
            mappings = scope.db.query(OntologyLinkMapping).filter(
                OntologyLinkMapping.ontology_id == scope.ontology.id,
                OntologyLinkMapping.status.in_(("active", "inferred"))).all()
            rows = ((m.link_type_id, m.relation_type, m.src_dataset_id,
                     m.tgt_dataset_id, m.src_key, m.tgt_key) for m in mappings
                    if mapping_binding_mode(m.field_mapping) == BINDING_MODE_LOCAL)
        by_name = {link.name: link for link in scope.link_types.values()}
        result = set()
        for link_id, name, src, tgt, src_key, tgt_key in rows:
            link = scope.link_types.get(link_id) if link_id else by_name.get(name)
            if (link is not None and src_key and tgt_key
                    and src in self._local_datasets.get(link.source_object_type_id, ())
                    and tgt in self._local_datasets.get(link.target_object_type_id, ())):
                result.add(link.id)
        return result

    @cached_property
    def _link_facts(self) -> set[str]:
        source, target = aliased(ObjectInstance), aliased(ObjectInstance)
        probes = []
        for link in sorted(self.scope.link_types.values(), key=lambda item: item.id):
            if (link.id in self._mapped_links or link.source_object_type_id in self.remote_ids
                    or link.target_object_type_id in self.remote_ids):
                continue
            facts = (select(LinkInstance.id)
                     .join(source, source.id == LinkInstance.source_object_id)
                     .join(target, target.id == LinkInstance.target_object_id)
                     .where(LinkInstance.link_type_id == link.id, *self._in_scope(LinkInstance),
                            source.object_type_id == link.source_object_type_id, *self._in_scope(source),
                            target.object_type_id == link.target_object_type_id, *self._in_scope(target)))
            probes.append(select(literal(link.id)).where(facts.exists()))
        return _existing_ids(self.scope.db, probes)

    def _remote_link(self, link_type) -> DataAccess:
        scope = self.scope
        source_id, target_id = link_type.source_object_type_id, link_type.target_object_type_id
        try:
            left, right, _ = published_relation_keys(scope.release.snapshot, link_type)
        except (QueryError, RemoteBindingError) as exc:
            return DataAccess("remote", exc.code)
        for type_id, prop in ((source_id, left), (target_id, right)):
            ot = scope.object_types[type_id]
            if not any(isinstance(item, dict) and item.get("name") == prop
                       for item in (ot.properties or [])):
                return DataAccess("remote", "remote_relation_unmapped")
            allowed = scope.allowed_property_names(type_id)
            if allowed is not None and prop not in allowed:
                return DataAccess("remote", "property_forbidden")
        return DataAccess("remote")
