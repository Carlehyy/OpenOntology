"""Database-backed validation of a complete, bounded Formal projection.

The canonical validator still owns definitions and per-row semantics. Only its
cross-row indexes are moved to a transaction-local SQL staging relation: Python
canonical JSON keys are retained exactly (including bool/int and int/float).
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from types import SimpleNamespace
import uuid

from sqlalchemy import Column, Index, MetaData, String, Table, func, select

from app.ontologies.formal_modeling import validation
from app.ontologies.formal_modeling.models import LinkInstance, ObjectInstance
from app.ontologies.versions.batching import query_batches


class ValidationErrors(list):
    """List-compatible diagnostic samples with exact counts for complete scans."""

    def __init__(self, errors=(), *, sample_limit=100):
        if isinstance(sample_limit, bool) or not isinstance(sample_limit, int) or sample_limit < 1:
            raise ValueError("Validation error sample_limit must be a positive integer")
        super().__init__()
        self.sample_limit = sample_limit
        self.total_count = 0
        self.code_counts = Counter()
        self.extend(errors)

    def append(self, error):
        self.total_count += 1
        self.code_counts[str(error.get("code") or "unknown")] += 1
        if len(self) < self.sample_limit:
            super().append(error)

    def extend(self, errors):
        if isinstance(errors, ValidationErrors):
            # Merging only its iterable samples would silently lose exact counts.
            self.total_count += errors.total_count
            self.code_counts.update(errors.code_counts)
            remaining = self.sample_limit - len(self)
            if remaining > 0:
                super().extend(errors[:remaining])
        else:
            for error in errors:
                self.append(error)

    def extend_counted(self, code, count, samples):
        """Merge an exact SQL count without fetching every invalid row."""
        if not count:
            return
        self.total_count += count
        self.code_counts[code] += count
        for error in samples:
            if len(self) == self.sample_limit:
                break
            super().append(error)

    def __iadd__(self, errors):
        self.extend(errors)
        return self


@contextmanager
def _validation_keys(db):
    name = f"release_validation_{uuid.uuid4().hex}"
    table = Table(
        name, MetaData(), Column("kind", String, primary_key=True),
        Column("id", String, primary_key=True), Column("type_id", String),
        Column("key", String), Column("source_id", String),
        Column("target_id", String), prefixes=["TEMPORARY"],
    )
    # Canonical JSON can exceed PostgreSQL's btree entry limit; keep it in
    # the staging heap and let SQL aggregate/sort it, never index its payload.
    Index(f"ix_{name}_type", table.c.kind, table.c.type_id)
    Index(f"ix_{name}_source", table.c.kind, table.c.type_id, table.c.source_id)
    Index(f"ix_{name}_target", table.c.kind, table.c.type_id, table.c.target_id)
    from app.ontologies.mappings.source_staging import managed_stage, stage_connection
    # Share the transaction-local cleanup guard; keep the original validation
    # failure when an aborted SQL transaction also prevents DROP TABLE.
    connection = stage_connection(db)
    with managed_stage(SimpleNamespace(db=db, close=lambda: table.drop(connection, checkfirst=True))):
        table.create(connection)
        yield table


def validate_persisted_model(
    db, ontology_id, object_types, link_types, actions, functions,
    *, model_validator=validation.validate_model, object_rows=None, link_rows=None,
):
    """Validate all live rows without an all-object Python dictionary/set."""
    errors = ValidationErrors(model_validator(object_types, link_types, actions, functions, [], []))
    from app.ontologies.versions.models import OntologyTrialObject, OntologyTrialLink

    objects = object_rows if object_rows is not None else db.query(ObjectInstance).filter(
        ObjectInstance.ontology_id == ontology_id,
    )
    links = link_rows if link_rows is not None else db.query(LinkInstance).filter(
        LinkInstance.ontology_id == ontology_id,
    )
    trial = object_rows is not None
    object_key = OntologyTrialObject.object_id if trial else ObjectInstance.id
    link_key = OntologyTrialLink.link_id if trial else LinkInstance.id

    def object_model(item):
        return SimpleNamespace(
            id=item.object_id, object_type_id=item.object_type_id,
            properties=item.properties, computed=item.computed,
        ) if trial else item

    def link_model(item):
        return SimpleNamespace(
            id=item.link_id, link_type_id=item.link_type_id,
            source_object_id=item.source_object_id, target_object_id=item.target_object_id,
            properties=item.properties,
        ) if trial else item
    object_by_type = {str(item.id): item for item in object_types}
    link_by_type = {str(item.id): item for item in link_types}
    with _validation_keys(db) as keys:
        for raw_batch in query_batches(objects, object_key):
            batch = [object_model(item) for item in raw_batch]
            # Duplicate PK errors are handled globally below, not page-locally.
            errors.extend(error for error in validation.validate_instance_contract(
                object_types, batch,
            ) if error["code"] != "duplicate_primary_key")
            rows = []
            for item in batch:
                object_type = object_by_type.get(str(item.object_type_id))
                if object_type is None or not object_type.primary_key:
                    continue
                definition = next((prop for prop in validation._property_defs(object_type).values()
                                   if prop.get("id") == object_type.primary_key
                                   or prop.get("name") == object_type.primary_key), None)
                if definition is None or not isinstance(item.properties, dict):
                    continue
                value = item.properties.get(str(definition["name"]))
                if not validation._is_empty(value):
                    rows.append({"id": item.id, "kind": "object", "type_id": item.object_type_id,
                                 "key": validation._canonical(value), "source_id": None,
                                 "target_id": None})
            if rows:
                db.execute(keys.insert(), rows)

        for raw_batch in query_batches(links, link_key):
            batch = [link_model(item) for item in raw_batch]
            endpoint_ids = {str(item.source_object_id) for item in batch} | {
                str(item.target_object_id) for item in batch}
            endpoints = [object_model(item) for item in objects.filter(
                object_key.in_(endpoint_ids),
            ).all()]
            errors.extend(error for error in validation.validate_link_instance_contract(
                link_types, endpoints, batch,
            ) if error["code"] not in {"duplicate_link", "cardinality_violation"})
            endpoint_types = {str(item.id): str(item.object_type_id) for item in endpoints}
            rows = []
            for item in batch:
                link_type = link_by_type.get(str(item.link_type_id))
                if link_type is None or (
                    endpoint_types.get(str(item.source_object_id)) != link_type.source_object_type_id
                    or endpoint_types.get(str(item.target_object_id)) != link_type.target_object_type_id
                ):
                    continue
                rows.append({"id": item.id, "kind": "link", "type_id": item.link_type_id,
                             "key": validation._canonical(item.properties or {}),
                             "source_id": item.source_object_id, "target_id": item.target_object_id})
            if rows:
                db.execute(keys.insert(), rows)

        # SQL computes exact cross-page groups. Only invalid rows are returned;
        # a valid ten-million-row projection has no per-row Python index.
        for kind, group_columns, code in (
            ("object", (keys.c.type_id, keys.c.key), "duplicate_primary_key"),
            ("link", (keys.c.type_id, keys.c.source_id, keys.c.target_id, keys.c.key), "duplicate_link"),
        ):
            groups = select(
                *group_columns, func.count().label("group_count"),
            ).where(keys.c.kind == kind).group_by(*group_columns).having(func.count() > 1).subquery()
            condition = [keys.c[column.name] == groups.c[column.name] for column in group_columns]
            query = select(keys.c.id, groups.c.group_count).join(groups, *condition[:1])
            for extra in condition[1:]:
                query = query.where(extra)
            query = query.where(keys.c.kind == kind)
            count = db.execute(select(func.count()).select_from(query.subquery())).scalar_one()
            limit = errors.sample_limit - len(errors)
            samples = db.execute(query.order_by(keys.c.id).limit(limit)).all() if limit else []
            errors.extend_counted(code, count, (validation._err(
                code, "objectInstance" if kind == "object" else "linkInstance",
                f"同一对象类型下主键值重复（{row.group_count} 个实例）" if kind == "object"
                else f"相同类型、端点和属性的链接重复（{row.group_count} 条）", item_id=row.id,
            ) for row in samples))

        for link_type in link_types:
            for column, opposite, direction, cardinalities in (
                (keys.c.source_id, keys.c.target_id, "源", {"one-to-one", "many-to-one"}),
                (keys.c.target_id, keys.c.source_id, "目标", {"one-to-one", "one-to-many"}),
            ):
                if link_type.cardinality not in cardinalities:
                    continue
                violating = select(column).where(
                    keys.c.kind == "link", keys.c.type_id == link_type.id,
                ).group_by(column).having(func.count(func.distinct(opposite)) > 1)
                query = select(keys.c.id).where(
                    keys.c.kind == "link", keys.c.type_id == link_type.id,
                    column.in_(violating),
                ).order_by(keys.c.id)
                count = db.execute(select(func.count()).select_from(query.order_by(None).subquery())).scalar_one()
                limit = errors.sample_limit - len(errors)
                samples = db.execute(query.limit(limit)).all() if limit else []
                errors.extend_counted("cardinality_violation", count, (validation._err(
                    "cardinality_violation", "linkInstance",
                    f"关系「{validation._label(link_type)}」要求每个{direction}实例最多关联一个{'目标' if direction == '源' else '源'}实例",
                    validation._label(link_type), row.id,
                ) for row in samples))
    return errors
