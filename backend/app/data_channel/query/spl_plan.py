"""Multi-stage SPL text planning over the existing single-pipeline core.

A staged text plan is lowered onto the current typed primitives: every named
stage becomes a ``DerivedSourceSpec`` (same-physical-source pushdown is
re-enforced by core at resolve time), and the main pipeline may federate
across stages and leaf bindings. Nothing here executes, authorizes, or
imports the ontology domain: leaf resolution for semantic sources stays
behind the composition root (remote_query / ontology_adapter wiring).

Rejections are explicit and structural only: unknown (unbound) sources,
forward/self references, duplicate names, dead stages, and whole-plan
complexity budgets. Per-pipeline operator/source caps stay in
``parser.validate_pipeline`` and are checked per stage plus the main
pipeline — the whole-plan budgets below never reset per stage.
"""
from __future__ import annotations

import re
from typing import Any

from .contracts import Binding, DerivedSourceSpec, QueryError, TypedQueryRequest, stable_hash
from .parser import (
    Command, Pipeline, SplPlan, params_in, pipeline_to_dict, validate_pipeline,
)

MAX_PLAN_STAGES = 8
# Whole logical plan inherits the existing single-pipeline budget (Goal
# baseline: 64 commands / 16 sources): the SUM of operators across every
# stage plus the main pipeline must fit it — staging never relaxes it.
MAX_PLAN_COMMANDS = 64
# Leaf bindings plus stages must fit the request source surface; core caps
# derived-node counts again at resolve time (MAX_DERIVED_NODES).
MAX_PLAN_SOURCES = 16

_LEAF_KINDS = frozenset({"integrated", "dataset"})


def _sources_of(pipeline: Pipeline) -> list[str]:
    return [name for name, _ in pipeline.sources]


def _internalize(pipeline: Pipeline, name_map: dict[str, str]) -> Pipeline:
    """Rewrite source NAMES to internal names; user-facing aliases stay.

    Only the pipeline's leading source name and JOIN's right source name are
    names — every other reference in commands uses aliases, which map 1:1.
    """
    commands = []
    for command in pipeline.commands:
        if command.op == "join":
            commands.append(Command("join", (name_map[command.args[0]], *command.args[1:])))
        else:
            commands.append(command)
    return Pipeline(name_map[pipeline.source], pipeline.alias, tuple(commands))


def validate_plan_graph(plan: SplPlan, bindings: dict[str, Binding]) -> dict[str, list[str]]:
    """Structural validation of a staged plan against its leaf bindings.

    Returns the leaf names in first-use order (main pipeline last), so the
    caller can name internal aliases deterministically. Raises QueryError on
    any rejected shape; never touches db/user/providers.
    """
    if not isinstance(plan, SplPlan):
        raise QueryError(422, "invalid_pipeline", "A SplPlan object is required")
    if not bindings:
        raise QueryError(422, "invalid_query", "At least one leaf binding is required")
    stage_names = plan.stage_names
    if len(stage_names) > MAX_PLAN_STAGES:
        raise QueryError(422, "query_too_complex",
                         f"More than {MAX_PLAN_STAGES} stages are not supported")
    if len(bindings) + len(stage_names) > MAX_PLAN_SOURCES:
        raise QueryError(422, "query_too_complex",
                         f"Leaf bindings plus stages exceed {MAX_PLAN_SOURCES} sources")
    for name in bindings:
        if len(name) > 63 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise QueryError(422, "invalid_identifier",
                             "Binding keys must be ASCII identifiers of at most 63 characters")
    collisions = sorted(set(bindings) & set(stage_names))
    if collisions:
        raise QueryError(422, "duplicate_source",
                         f"Binding keys must not collide with stage names: {', '.join(collisions)}")

    used_at: dict[str, list[int]] = {}
    for index, stage in enumerate(plan.stages):
        validate_pipeline(stage.pipeline)
        # Every stage must end in an explicit projection: without fields/stats
        # the core's default pass-through names derived outputs after the
        # internal alias ("x.cid"), which outer stages could never reference
        # stably. Declared outputs are the stage contract.
        if not stage.pipeline.commands or stage.pipeline.commands[-1].op not in {"fields", "stats"}:
            raise QueryError(422, "stage_requires_projection",
                             f"Stage「{stage.name}」must end with a fields/stats projection")
        for source in _sources_of(stage.pipeline):
            if source == stage.name:
                raise QueryError(422, "stage_cycle",
                                 f"Stage「{stage.name}」references itself")
            if source in bindings:
                used_at.setdefault(source, []).append(index)
            elif source in stage_names:
                position = stage_names.index(source)
                if position >= index:
                    raise QueryError(
                        422, "forward_stage_reference",
                        f"Stage「{stage.name}」references「{source}」, which is defined later; "
                        "stages may only use earlier stages or leaf sources")
                used_at.setdefault(source, []).append(index)
            else:
                raise QueryError(422, "unknown_source",
                                 f"Stage「{stage.name}」references unknown source「{source}」")
    for index, source in enumerate(_sources_of(plan.pipeline)):
        if source in bindings:
            used_at.setdefault(source, []).append(len(plan.stages))
        elif source in stage_names:
            used_at.setdefault(source, []).append(len(plan.stages))
        else:
            raise QueryError(422, "unknown_source",
                             f"Main pipeline references unknown source「{source}」")
    dead = [name for name in stage_names if not used_at.get(name)]
    if dead:
        raise QueryError(422, "dead_stage",
                         f"Stage(s) defined but never used: {', '.join(dead)}")
    total_commands = sum(len(p.commands) for p in plan.pipelines)
    if total_commands > MAX_PLAN_COMMANDS:
        raise QueryError(422, "query_too_complex",
                         f"Whole-plan operators ({total_commands}) exceed {MAX_PLAN_COMMANDS}")
    # Unreferenced leaf bindings stay inert (no provider resolution, no auth
    # cost) — same semantics as single-pipeline text requests.
    return {name: used_at[name] for name in sorted(used_at)}


def leaf_name_map(plan: SplPlan, bindings: dict[str, Binding]) -> dict[str, str]:
    """Public leaf name -> internal binding alias (deterministic first use).

    The service layer resolves providers per binding and must key relations by
    the internal names the lowered request references; this mapping is that
    contract, computed by the same first-use order as ``build_spl_request``.
    """
    validate_plan_graph(plan, bindings)
    name_map: dict[str, str] = {}
    counter = 0

    def internal(public: str) -> str:
        nonlocal counter
        if public not in name_map:
            counter += 1
            name_map[public] = f"s{counter}"
        return name_map[public]

    for stage in plan.stages:
        for source in _sources_of(stage.pipeline):
            internal(source)
    for source in _sources_of(plan.pipeline):
        internal(source)
    return {public: internal_name for public, internal_name in name_map.items()
            if public in bindings}


def build_spl_request(plan: SplPlan, bindings: dict[str, Binding], *,
                      params: dict[str, Any] | None = None, page_size: int = 100,
                      result_storage: str | None = None,
                      session_kind: str | None = None, session_id: str | None = None,
                      origin: dict[str, Any] | None = None) -> TypedQueryRequest:
    """Lower a staged plan into the existing typed request for leaf bindings.

    Pure core lowering for integrated/dataset leaves: no db, no user, no
    semantic authorization, no engine rewrite. Each stage becomes one
    DerivedSourceSpec over internal aliases; the text aliases inside stage
    pipelines are preserved so user-facing refs and fingerprints stay stable.
    ``result_storage`` passes through untouched when given (the ontology
    adapter path must run memory; plain text requests keep their own value) —
    this helper never hardcodes a storage mode for its callers.
    """
    if not isinstance(plan, SplPlan):
        raise QueryError(422, "invalid_pipeline", "A SplPlan object is required")
    for name, binding in bindings.items():
        if not isinstance(binding, Binding) or binding.kind not in _LEAF_KINDS:
            raise QueryError(422, "semantic_binding_rejected",
                             f"Binding「{name}」must be an integrated/dataset leaf; "
                             "ontology sources go through the semantic query entry")
    validate_plan_graph(plan, bindings)

    # Deterministic internal naming in first-use order: stages in text order,
    # then the main pipeline's leaves.
    name_map: dict[str, str] = {}
    counter = 0

    def internal(public: str) -> str:
        nonlocal counter
        if public not in name_map:
            counter += 1
            name_map[public] = f"s{counter}"
        return name_map[public]

    for stage in plan.stages:
        for source in _sources_of(stage.pipeline):
            internal(source)
    for source in _sources_of(plan.pipeline):
        internal(source)

    bindings_out: dict[str, Binding] = {}
    derived_out: dict[str, DerivedSourceSpec] = {}
    plan_params = params or {}
    for stage in plan.stages:
        stage_pipeline = _internalize(stage.pipeline, name_map)
        validate_pipeline(stage_pipeline)
        # Stage parameters are scoped to the names the stage itself references:
        # core compiles a derived relation from spec.params, so the whole-plan
        # dict must be narrowed per stage, never handed over wholesale.
        stage_params = {name: value for name, value in plan_params.items()
                        if name in params_in(stage_pipeline.commands)}
        derived_out[name_map[stage.name]] = DerivedSourceSpec(
            alias=name_map[stage.name], source=stage_pipeline.source,
            pipeline=stage_pipeline, params=stage_params)
    for name in name_map:
        if name in bindings:
            bindings_out[name_map[name]] = bindings[name]
    main = _internalize(plan.pipeline, name_map)
    validate_pipeline(main)
    # The request-level params dict is main-pipeline scoped: core's top-level
    # compile rejects parameters it cannot see in the main pipeline, and the
    # stage copies live on their DerivedSourceSpec.params above. A name used
    # by both stays in both scopes, which both compiles accept.
    stage_param_names: set[str] = set()
    for stage in plan.stages:
        stage_param_names.update(params_in(stage.pipeline.commands))
    main_params = {name: value for name, value in plan_params.items()
                   if name not in stage_param_names or name in params_in(main.commands)}

    fingerprint_payload = {
        "stages": [{"name": stage.name, "pipeline": pipeline_to_dict(stage.pipeline)}
                   for stage in plan.stages],
        "pipeline": pipeline_to_dict(plan.pipeline),
        "bindings": sorted((public, name_map[public]) for public in name_map if public in bindings),
        "params": params or {},
    }
    request_kwargs: dict[str, Any] = dict(
        pipeline=main, bindings=bindings_out, derived=derived_out,
        join_constraints={}, params=main_params, page_size=page_size,
        origin=origin, request_fingerprint=stable_hash(fingerprint_payload),
        session_kind=session_kind, session_id=session_id)
    if result_storage is not None:
        request_kwargs["result_storage"] = result_storage
    return TypedQueryRequest(**request_kwargs)


def plan_fingerprint(plan: SplPlan, params: dict[str, Any] | None = None) -> str:
    """Canonical identity of a staged plan (stages + main + params)."""
    return stable_hash({
        "stages": [{"name": stage.name, "pipeline": pipeline_to_dict(stage.pipeline)}
                   for stage in plan.stages],
        "pipeline": pipeline_to_dict(plan.pipeline),
        "params": params or {},
    })
