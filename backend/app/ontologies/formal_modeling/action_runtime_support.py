"""Compatibility facade for formal Action runtime helpers.

The implementation is separated into cohesive value-evaluation, write-contract,
and durable execution-record modules.  Re-exporting the exact function objects
keeps historical import and monkeypatch paths available to callers.
"""
from __future__ import annotations

import logging

from app.ontologies.formal_modeling.action_execution_errors import (
    RuleExecutionError,
)
from app.ontologies.formal_modeling.action_execution_records import (
    _current_release_error,
    _fail_log,
    _failed_effects,
    _idempotency_key,
    _idempotency_owner,
    _idempotent_replay,
    _is_executing_sentinel_approval,
    _log_to_dict,
    _match_state_id,
    _normalize_target_snapshot,
    _now,
    _rule_identity,
    _same_idempotent_request,
    _sentinel_id_from_execution_lineage,
)
from app.ontologies.formal_modeling.action_runtime_contracts import (
    _contract_messages,
    _runtime_instance_query,
    _runtime_link_query,
    _validate_link_candidate,
    _validate_link_write,
    _validate_object_candidate,
    _validate_object_write,
)
from app.ontologies.formal_modeling.action_runtime_values import (
    _evaluate_context_derived_projection,
    _execute_action_function,
    _preview_find,
    _preview_instance_values,
    _preview_iter,
    _preview_link_values,
    _preview_reader,
    _preview_values,
    _render_template,
    _resolve_recipient,
    _resolve_value,
    _validate,
)


logger = logging.getLogger(
    "app.ontologies.formal_modeling.action_engine")
