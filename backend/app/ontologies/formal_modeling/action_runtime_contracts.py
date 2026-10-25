"""Runtime object/link write-contract enforcement for formal Actions."""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.models.ontology_formal import (
    LinkInstance,
    LinkType,
    ObjectInstance,
    ObjectType,
)
from app.ontologies.formal_modeling.action_execution_errors import (
    RuleExecutionError,
)
from app.ontologies.formal_modeling.action_runtime_values import (
    _preview_instance_values,
    _preview_link_values,
    _preview_reader,
    _preview_values,
)
from app.ontologies.formal_modeling.validation import (
    validate_instance_contract,
    validate_link_instance_contract,
)


def _runtime_instance_query(
        db: Session, ontology_id: str, ontology_release_id: str | None):
    query = db.query(ObjectInstance).filter(
        ObjectInstance.ontology_id == ontology_id)
    if ontology_release_id is not None:
        query = query.filter(
            ObjectInstance.ontology_release_id == ontology_release_id)
    return query


def _runtime_link_query(
        db: Session, ontology_id: str, ontology_release_id: str | None):
    query = db.query(LinkInstance).filter(
        LinkInstance.ontology_id == ontology_id)
    if ontology_release_id is not None:
        query = query.filter(
            LinkInstance.ontology_release_id == ontology_release_id)
    return query


def _contract_messages(errors: list[dict]) -> str:
    messages = [
        str(item.get("message") or item.get("code") or item)
        if isinstance(item, dict) else str(item)
        for item in errors
    ]
    return "；".join(messages[:8]) + (
        f"；另有 {len(messages) - 8} 项" if len(messages) > 8 else "")


def _validate_object_write(
        db: Session, ontology_id: str, ontology_release_id: str | None,
        instance_id: str, rule_name: str,
        *, definition_context: Optional[dict] = None) -> None:
    """Validate the exact post-write object projection before side effects commit."""
    db.flush()
    object_types = (
        _preview_values(definition_context, "object_types")
        if definition_context is not None else
        db.query(ObjectType).filter(
            ObjectType.ontology_id == ontology_id).all()
    )
    instances = _runtime_instance_query(
        db, ontology_id, ontology_release_id).all()
    errors = validate_instance_contract(
        object_types, instances, validate_ids={instance_id})
    if errors:
        raise RuleExecutionError(
            rule_name, f"对象实例契约校验失败: {_contract_messages(errors)}")


def _validate_object_candidate(
        db: Session, ontology_id: str, ontology_release_id: str | None,
        candidate, rule_name: str,
        *, extra_candidates: Optional[list] = None,
        preview_context: Optional[dict] = None) -> None:
    """Dry-run equivalent of ``_validate_object_write``."""
    reader = _preview_reader(preview_context)
    if reader is not None:
        errors = reader.validate_object_candidate(candidate, extra_candidates or [])
        if errors:
            raise RuleExecutionError(
                rule_name, f"对象实例契约校验失败: {_contract_messages(errors)}")
        return
    if (
        preview_context is not None
        and preview_context.get("isolated", False)
    ):
        object_types = _preview_values(preview_context, "object_types")
        instances = _preview_instance_values(preview_context)
    else:
        object_types = (
            _preview_values(preview_context, "object_types")
            if preview_context is not None else
            db.query(ObjectType).filter(
                ObjectType.ontology_id == ontology_id).all()
        )
        instances = _runtime_instance_query(
            db, ontology_id, ontology_release_id).all()
    merged = [
        candidate if item.id == candidate.id else item
        for item in instances
    ]
    known_ids = {item.id for item in merged}
    for item in (extra_candidates or []):
        if item.id != candidate.id and item.id not in known_ids:
            merged.append(item)
            known_ids.add(item.id)
    if not any(item.id == candidate.id for item in instances):
        merged.append(candidate)
    errors = validate_instance_contract(
        object_types, merged, validate_ids={candidate.id})
    if errors:
        raise RuleExecutionError(
            rule_name, f"对象实例契约校验失败: {_contract_messages(errors)}")


def _validate_link_write(
        db: Session, ontology_id: str, ontology_release_id: str | None,
        link_id: str, rule_name: str,
        *, definition_context: Optional[dict] = None) -> None:
    """Validate endpoints, duplicate edges and cardinality before commit."""
    db.flush()
    link_types = (
        _preview_values(definition_context, "link_types")
        if definition_context is not None else
        db.query(LinkType).filter(
            LinkType.ontology_id == ontology_id).all()
    )
    instances = _runtime_instance_query(
        db, ontology_id, ontology_release_id).all()
    links = _runtime_link_query(
        db, ontology_id, ontology_release_id).all()
    errors = validate_link_instance_contract(
        link_types, instances, links, validate_ids={link_id})
    if errors:
        raise RuleExecutionError(
            rule_name, f"链接实例契约校验失败: {_contract_messages(errors)}")


def _validate_link_candidate(
        db: Session, ontology_id: str, ontology_release_id: str | None,
        candidate, rule_name: str,
        *, extra_instances: Optional[list] = None,
        existing_candidates: Optional[list] = None,
        excluded_link_ids: Optional[set[str]] = None,
        preview_context: Optional[dict] = None) -> None:
    """Dry-run equivalent of ``_validate_link_write``."""
    reader = _preview_reader(preview_context)
    if reader is not None:
        errors = reader.validate_link_candidate(
            candidate, extra_instances or [], existing_candidates or [],
            excluded_link_ids or set())
        if errors:
            raise RuleExecutionError(
                rule_name, f"链接实例契约校验失败: {_contract_messages(errors)}")
        return
    if (
        preview_context is not None
        and preview_context.get("isolated", False)
    ):
        link_types = _preview_values(preview_context, "link_types")
        instances = _preview_instance_values(preview_context)
        base_links = _preview_link_values(preview_context)
    else:
        link_types = (
            _preview_values(preview_context, "link_types")
            if preview_context is not None else
            db.query(LinkType).filter(
                LinkType.ontology_id == ontology_id).all()
        )
        instances = _runtime_instance_query(
            db, ontology_id, ontology_release_id).all()
        base_links = _runtime_link_query(
            db, ontology_id, ontology_release_id).all()
    known_instance_ids = {item.id for item in instances}
    for item in (extra_instances or []):
        if item.id not in known_instance_ids:
            instances.append(item)
            known_instance_ids.add(item.id)
    excluded = excluded_link_ids or set()
    links = [
        item for item in base_links
        if str(item.id) not in excluded
    ]
    links.extend(existing_candidates or [])
    links.append(candidate)
    errors = validate_link_instance_contract(
        link_types, instances, links, validate_ids={candidate.id})
    if errors:
        raise RuleExecutionError(
            rule_name, f"链接实例契约校验失败: {_contract_messages(errors)}")
