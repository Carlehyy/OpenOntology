"""Frozen/live remote mapping visibility shared by discovery and query planning."""
from __future__ import annotations


def remote_binding_states(scope, remote_ids) -> dict[str, dict]:
    """Return mapped business properties and a precise unavailable reason."""
    remote_ids = {str(type_id) for type_id in remote_ids}
    states: dict[str, dict] = {}
    if not remote_ids:
        return states
    if scope.release is not None:
        from app.ontologies.mappings.remote_binding import (
            RemoteBindingError, remote_bindings_by_type,
        )
        try:
            grouped = remote_bindings_by_type(scope.release.snapshot)
        except RemoteBindingError as exc:
            return {type_id: {"queryable": set(), "reason": exc.code}
                    for type_id in remote_ids}
        for type_id in remote_ids:
            matches = grouped.get(type_id) or []
            if len(matches) == 1:
                states[type_id] = {
                    "queryable": {
                        str(prop) for prop in matches[0].get("fieldMap", {}).values()
                        if prop and str(prop) != "__primary_key__"
                    },
                    "reason": None,
                }
            else:
                states[type_id] = {
                    "queryable": set(),
                    "reason": ("remote_binding_ambiguous" if len(matches) > 1
                               else "remote_binding_unresolved"),
                }
        return states

    from app.ontologies.mappings.models import OntologyMapping
    from app.ontologies.mappings.remote_binding import (
        BINDING_MODE_REMOTE, RemoteBindingError, mapping_binding_mode,
    )
    by_identity: dict[str, list[set[str]]] = {}
    try:
        rows = scope.db.query(OntologyMapping).filter(
            OntologyMapping.ontology_id == scope.ontology.id).all()
    except Exception:  # noqa: BLE001 - discovery fails closed on bad mapping
        return {type_id: {"queryable": set(), "reason": "remote_binding_unresolved"}
                for type_id in remote_ids}
    for mapping in rows:
        try:
            if mapping_binding_mode(mapping.field_mapping) != BINDING_MODE_REMOTE:
                continue
        except RemoteBindingError as exc:
            for type_id in remote_ids:
                states.setdefault(type_id, {"queryable": set(),
                                            "reason": exc.code})
            continue
        target = str(mapping.target_object_type_id or "")
        if target not in remote_ids:
            entity_class = str(mapping.entity_class or "")
            target = next((ot.id for ot in scope.object_types.values()
                           if entity_class in (ot.name, ot.display_name)), "")
        if target not in remote_ids:
            continue
        props = {str(value) for key, value in (mapping.field_mapping or {}).items()
                 if not str(key).startswith("__") and value
                 and str(value) != "__primary_key__"}
        by_identity.setdefault(target, []).append(props)
    for type_id in remote_ids:
        matches = by_identity.get(type_id) or []
        if len(matches) == 1:
            states[type_id] = {"queryable": matches[0], "reason": None}
        else:
            states[type_id] = {
                "queryable": set(),
                "reason": ("remote_binding_ambiguous" if len(matches) > 1
                           else "remote_binding_unresolved"),
            }
    return states
