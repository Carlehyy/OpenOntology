"""Same-physical-source derived relations, compiled but never fetched here.

A derived spec may join several tables and filter them, provided every named
source resolves to the same physical connection. The compiled statement is
wrapped as a subquery Relation sharing the parent source scope, so executing
any statement over it (group statement, cardinality check, direct stream)
runs on the parent scope in one source round trip with its own filters.
"""
from __future__ import annotations

from typing import Any

from .contracts import DerivedSourceSpec, QueryError, Relation, stable_hash
from .compiler import compile_query
from .parser import pipeline_to_dict, validate_pipeline

MAX_DERIVED_NODES = 96
MAX_DERIVED_PATH_DEPTH = 8


def derived_relation(alias: str, parents: dict[str, Relation], spec: DerivedSourceSpec,
                     *, bind_prefix: str) -> Relation:
    """Compile a derived spec over name-keyed parent relations.

    ``parents`` maps source NAMES (leaf bindings and earlier derived) to
    relations; the spec's pipeline may use its own aliases, which are mapped
    per compilation. All named sources must share one physical connection.
    """
    validate_pipeline(spec.pipeline)
    if spec.pipeline.source != spec.source:
        raise QueryError(422, "invalid_derived", "Derived pipeline must start from its declared source")
    names = {name for name, _ in spec.pipeline.sources}
    missing = names - set(parents)
    if missing:
        raise QueryError(422, "unknown_derived_source",
                         f"Derived source(s) not resolved yet: {', '.join(sorted(missing))}")
    connections = {parents[name].connection_key for name in names}
    if len(connections) != 1:
        raise QueryError(422, "cross_source_derived_rejected",
                         "A derived relation may only combine sources on one physical connection")
    primary = parents[spec.source]
    dialect = primary.state.get("dialect", "postgresql")
    compile_relations = {alias_name: parents[name] for name, alias_name in spec.pipeline.sources}
    compiled = compile_query(spec.pipeline, compile_relations,
                             dict(spec.params), dialect=dialect, bind_prefix=bind_prefix)
    table = compiled.statement.subquery(alias)
    state = {"alias": alias, "kind": "derived", "derivedFrom": sorted(names),
             "consistency": primary.state.get("consistency"),
             "completeness": primary.state.get("completeness", "complete"),
             "definitionHash": stable_hash(pipeline_to_dict(spec.pipeline))}
    for key in ("asOf", "snapshotId", "maxPageSize", "dialect"):
        if key in primary.state:
            state[key] = primary.state[key]
    def recheck():
        for name in sorted(names):
            if parents[name].recheck is not None:
                parents[name].recheck()

    return Relation(
        alias=alias, table=table, columns={c.name: c for c in compiled.columns}, state=state,
        auth_fingerprint=stable_hash(sorted((n, parents[n].auth_fingerprint) for n in names)),
        schema_fingerprint=stable_hash([sorted((n, parents[n].schema_fingerprint) for n in names),
                                        pipeline_to_dict(spec.pipeline), spec.params]),
        connection_key=primary.connection_key, batches=primary.batches, recheck=recheck,
        platform=primary.platform, source_group=primary.source_group,
        bind_params=dict(compiled.params) if compiled.params else None)


def resolve_derived(base: dict[str, Relation], derived: dict[str, DerivedSourceSpec]) -> dict[str, Relation]:
    """Resolve derived specs over name-keyed base relations in dependency order."""
    if not derived:
        return dict(base)
    resolved: dict[str, Relation] = dict(base)
    depth: dict[str, int] = {name: 0 for name in base}
    remaining = dict(derived)
    while remaining:
        progress = False
        for alias, spec in list(remaining.items()):
            if not isinstance(spec, DerivedSourceSpec):
                raise QueryError(422, "invalid_derived", "Derived specs must be DerivedSourceSpec objects")
            if spec.alias != alias:
                raise QueryError(422, "invalid_derived",
                                 "Derived spec alias must match its registration key")
            names = {name for name, _ in validate_pipeline(spec.pipeline).sources}
            if names <= set(resolved) and alias not in resolved and alias not in base:
                resolved[alias] = derived_relation(
                    alias, resolved, spec, bind_prefix=f"dv{len(resolved)}")
                depth[alias] = 1 + max(depth.get(name, 0) for name in names)
                if depth[alias] > MAX_DERIVED_PATH_DEPTH:
                    raise QueryError(422, "query_too_complex",
                                     f"Derived nesting exceeds path depth {MAX_DERIVED_PATH_DEPTH}")
                del remaining[alias]
                progress = True
        if not progress:
            raise QueryError(422, "unknown_derived_source",
                             "Derived graph has a cycle or references an unknown source")
    if len(derived) > MAX_DERIVED_NODES:
        raise QueryError(422, "query_too_complex", f"More than {MAX_DERIVED_NODES} derived relations")
    if len(base) + len(derived) > MAX_DERIVED_NODES:
        raise QueryError(422, "query_too_complex", "Too many total pipeline sources")
    return resolved
