"""Transaction-local lineage and complete cross-page Formal contract checks."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import logging
import uuid

from sqlalchemy import Column, Integer, MetaData, String, Table, Text, func, select

from app.ontologies.mappings.source_staging import MAPPING_BATCH_SIZE, keyset_pages, stage_connection

logger = logging.getLogger(__name__)


class ProjectionStage:
    """Keep row-sized identity/validation state in SQL, never Python maps."""
    def __init__(self, db):
        self.db = db
        metadata = MetaData()
        self.table = Table("formal_lineage_" + uuid.uuid4().hex, metadata,
            Column("entity_id", String, primary_key=True),
            Column("instance_id", String, nullable=False, index=True),
            Column("entity_class", String, nullable=False),
            Column("object_type_id", String, nullable=False), prefixes=["TEMPORARY"])
        self.table.create(stage_connection(db))

    def append(self, values):
        if len(values) > MAPPING_BATCH_SIZE:
            raise ValueError("Formal lineage batches may contain at most 500 rows")
        if values:
            self.db.execute(self.table.insert(), values)

    def close(self):
        self.table.drop(self.db.connection(), checkfirst=True)


class InstanceLookupStage:
    """Index existing instance lineage/legacy PKs once per object type."""
    def __init__(self, db, ontology_id, object_type_id, pk_field):
        from app.ontologies.formal_modeling.models import ObjectInstance
        self.db = db
        self.table = Table("formal_lookup_" + uuid.uuid4().hex, MetaData(),
            Column("id", String, primary_key=True),
            Column("external_id", String, index=True),
            Column("pk_value", Text), Column("pk_hash", String(64), index=True), prefixes=["TEMPORARY"])
        try:
            self.table.create(stage_connection(db))
            query = db.query(ObjectInstance).filter(
                ObjectInstance.ontology_id == ontology_id,
                ObjectInstance.object_type_id == object_type_id)
            for page in keyset_pages(query, ObjectInstance.id):
                values = []
                for row in page:
                    value = (self.pk_key((row.properties or {}).get(pk_field))
                             if pk_field and row.source == "pipeline" else None)
                    values.append({"id": row.id, "external_id": row.external_id, "pk_value": value,
                                   "pk_hash": hashlib.sha256(value.encode()).hexdigest() if value is not None else None})
                db.execute(self.table.insert(), values)
        except BaseException:
            if db.is_active:
                try:
                    self.close()
                except Exception:
                    logger.warning("Instance lookup cleanup deferred to rollback", exc_info=True)
            raise

    @staticmethod
    def pk_key(value):
        from decimal import Decimal
        from app.ontologies.formal_modeling.validation import _canonical
        # Preserve historical Python equality for numeric PKs (1 == 1.0), but
        # keep strings distinct; canonical composite identities are strings.
        if isinstance(value, (int, float, bool)):
            return "numeric:" + str(Decimal(str(int(value) if isinstance(value, bool) else value)).normalize())
        return _canonical(value)

    def resolve(self, rows):
        """Return bounded external and legacy-PK candidates with ambiguity counts."""
        table = self.table
        external_ids = [row[0] for row in rows]
        pk_keys = [self.pk_key(row[1]) for row in rows]
        external = dict(self.db.execute(select(table.c.external_id, func.min(table.c.id)).where(
            table.c.external_id.in_(external_ids)).group_by(table.c.external_id).limit(MAPPING_BATCH_SIZE)).all())
        legacy = {key: (instance_id, count) for key, instance_id, count in self.db.execute(select(
            table.c.pk_value, func.min(table.c.id), func.count()).where(
            table.c.pk_hash.in_([hashlib.sha256(value.encode()).hexdigest() for value in pk_keys]),
            table.c.pk_value.in_(pk_keys)).group_by(table.c.pk_value).limit(MAPPING_BATCH_SIZE)).all()}
        return external, legacy

    def close(self):
        self.table.drop(self.db.connection(), checkfirst=True)


def _legacy_property_key(value):
    """Honor Python dict equality used by historical link adoption."""
    from decimal import Decimal
    if isinstance(value, dict):
        return ("dict", sorted((key, _legacy_property_key(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return ("list", [_legacy_property_key(item) for item in value])
    if isinstance(value, (int, float, bool, Decimal)):
        return ("number", str(Decimal(str(int(value) if isinstance(value, bool) else value)).normalize()))
    return (type(value).__name__, value)


class LinkLookupStage:
    """Index canonical/Relation/legacy identities once; resolve ≤500 SQL joins."""
    def __init__(self, db, ontology_id):
        from app.ontologies.formal_modeling.models import LinkInstance
        self.db = db
        token = uuid.uuid4().hex
        self.table = Table("formal_link_lookup_" + token, MetaData(),
            Column("id", String, primary_key=True), Column("lineage_key", String(64), index=True),
            Column("legacy_key", String(64), index=True),
            Column("lineage_value", Text), Column("legacy_value", Text), prefixes=["TEMPORARY"])
        self.requests = Table("formal_link_requests_" + token, MetaData(),
            Column("ordinal", Integer, primary_key=True), Column("canonical_id", String),
            Column("lineage_key", String(64)), Column("legacy_key", String(64)),
            Column("lineage_value", Text), Column("legacy_value", Text), prefixes=["TEMPORARY"])
        try:
            self.table.create(stage_connection(db))
            self.requests.create(stage_connection(db))
            for page in keyset_pages(db.query(LinkInstance).filter(
                    LinkInstance.ontology_id == ontology_id), LinkInstance.id):
                self.remember(page)
        except BaseException:
            if db.is_active:
                try:
                    self.close()
                except Exception:
                    logger.warning("Link lookup cleanup deferred to rollback", exc_info=True)
            raise

    @staticmethod
    def payload(*values):
        import json
        return json.dumps(values, sort_keys=True, ensure_ascii=False, default=str)

    @classmethod
    def key(cls, *values):
        return hashlib.sha256(cls.payload(*values).encode()).hexdigest()

    def resolve(self, rows):
        """Each row: (canonical id, link type, source, target, relation id, props)."""
        if len(rows) > MAPPING_BATCH_SIZE:
            raise ValueError("Link lookup pages may contain at most 500 rows")
        from sqlalchemy import delete
        self.db.execute(delete(self.requests))
        if not rows:
            return []
        self.db.execute(self.requests.insert(), [{"ordinal": index, "canonical_id": row[0],
            "lineage_key": self.key(*row[1:5]),
            "legacy_key": self.key(*row[1:4], _legacy_property_key(row[5])),
            "lineage_value": self.payload(*row[1:5]),
            "legacy_value": self.payload(*row[1:4], _legacy_property_key(row[5]))}
            for index, row in enumerate(rows)])
        result = []
        for key in ("canonical_id", "lineage_key", "legacy_key"):
            target_key = self.table.c.id if key == "canonical_id" else self.table.c[key]
            condition = self.requests.c[key] == target_key
            if key != "canonical_id":
                value = key.replace("_key", "_value")
                condition &= self.requests.c[value] == self.table.c[value]
            query = select(self.requests.c.ordinal, func.min(self.table.c.id), func.count()).select_from(
                self.requests.join(self.table, condition)).group_by(self.requests.c.ordinal)
            result.append({index: (item_id, count) for index, item_id, count in self.db.execute(query)})
        return result

    def remember(self, links):
        """Reflect current-page mutations before resolving later-page duplicates."""
        from sqlalchemy import delete
        links = list({row.id: row for row in links}.values())
        if not links:
            return
        if len(links) > MAPPING_BATCH_SIZE:
            raise ValueError("Link lookup updates may contain at most 500 rows")
        self.db.execute(delete(self.table).where(self.table.c.id.in_([row.id for row in links])))
        self.db.execute(self.table.insert(), [{"id": row.id,
            "lineage_key": self.key(row.link_type_id, row.source_object_id,
                row.target_object_id, row.source_relation_id) if row.source_relation_id else None,
            "legacy_key": self.key(row.link_type_id, row.source_object_id,
                row.target_object_id, _legacy_property_key(row.properties or {}))
                if row.source_relation_id is None else None,
            "lineage_value": self.payload(row.link_type_id, row.source_object_id,
                row.target_object_id, row.source_relation_id),
            "legacy_value": self.payload(row.link_type_id, row.source_object_id,
                row.target_object_id, _legacy_property_key(row.properties or {}))} for row in links])

    def close(self):
        self.requests.drop(self.db.connection(), checkfirst=True)
        self.table.drop(self.db.connection(), checkfirst=True)


@contextmanager
def projection_stage(db):
    stage = ProjectionStage(db)
    try:
        yield stage
    except BaseException:
        # PostgreSQL failed transactions cannot execute DROP; rollback belongs
        # to the caller. Never replace the original projection error with cleanup.
        if db.is_active:
            try:
                stage.close()
            except Exception:
                logger.warning("Formal stage cleanup deferred to rollback", exc_info=True)
        raise
    else:
        stage.close()


def _raise_contract(errors):
    if errors:
        preview = "; ".join(error.get("message", "") for error in errors[:5])
        raise ValueError(f"正规本体投影违反运行契约: {preview}")


def validate_projection(db, ontology_id, ontology_release_id=None):
    """Validate every row plus global PK/edge/cardinality state in SQL stages.

    Existing validators remain authoritative for values, required/computed fields,
    endpoint references and types. Temporary canonical keys extend their in-page
    checks across pages, preserving Python JSON type-sensitive PK comparison.
    """
    from app.ontologies.formal_modeling.models import (
        ObjectInstance, ObjectType, LinkInstance, LinkType,
    )
    from app.ontologies.formal_modeling.validation import (
        _canonical, validate_instance_contract, validate_link_instance_contract,
    )

    object_types = [row for page in keyset_pages(db.query(ObjectType).filter(
        ObjectType.ontology_id == ontology_id), ObjectType.id) for row in page]
    link_types = [row for page in keyset_pages(db.query(LinkType).filter(
        LinkType.ontology_id == ontology_id), LinkType.id) for row in page]
    primary_keys = {}
    for owner in object_types:
        primary_keys[owner.id] = next((prop["name"] for prop in owner.properties or []
            if prop.get("id") == owner.primary_key or prop.get("name") == owner.primary_key), None)
    token = uuid.uuid4().hex
    metadata = MetaData()
    keys = Table("formal_keys_" + token, metadata,
        Column("id", String, primary_key=True), Column("owner", String, nullable=False),
        Column("value", Text, nullable=False), prefixes=["TEMPORARY"])
    edges = Table("formal_edges_" + token, metadata,
        Column("id", String, primary_key=True), Column("owner", String, nullable=False),
        Column("source", String, nullable=False), Column("target", String, nullable=False),
        Column("value", Text, nullable=False), prefixes=["TEMPORARY"])
    try:
        keys.create(stage_connection(db))
        edges.create(stage_connection(db))
        query = db.query(ObjectInstance).filter(ObjectInstance.ontology_id == ontology_id)
        links = db.query(LinkInstance).filter(LinkInstance.ontology_id == ontology_id)
        if ontology_release_id is not None:
            query = query.filter(ObjectInstance.ontology_release_id == ontology_release_id)
            links = links.filter(LinkInstance.ontology_release_id == ontology_release_id)
        for page in keyset_pages(query, ObjectInstance.id):
            _raise_contract(validate_instance_contract(object_types, page))
            values = [{"id": row.id, "owner": row.object_type_id,
                       "value": _canonical((row.properties or {}).get(primary_keys[row.object_type_id]))}
                      for row in page if primary_keys.get(row.object_type_id)]
            if values:
                db.execute(keys.insert(), values)
        duplicate = db.execute(select(keys.c.owner).group_by(
            keys.c.owner, keys.c.value).having(func.count() > 1).limit(1)).first()
        if duplicate:
            raise ValueError("正规本体投影违反运行契约: 同一对象类型下主键值重复（duplicate_primary_key）")
        for page in keyset_pages(links, LinkInstance.id):
            ids = {row.source_object_id for row in page} | {row.target_object_id for row in page}
            endpoints = []
            ordered_ids = sorted(ids)
            for offset in range(0, len(ordered_ids), MAPPING_BATCH_SIZE):
                endpoints.extend(query.filter(ObjectInstance.id.in_(
                    ordered_ids[offset:offset + MAPPING_BATCH_SIZE])).limit(MAPPING_BATCH_SIZE).all())
            _raise_contract(validate_link_instance_contract(link_types, endpoints, page))
            db.execute(edges.insert(), [{"id": row.id, "owner": row.link_type_id,
                "source": row.source_object_id, "target": row.target_object_id,
                "value": _canonical(row.properties or {})} for row in page])
        duplicate = db.execute(select(edges.c.owner).group_by(
            edges.c.owner, edges.c.source, edges.c.target, edges.c.value
        ).having(func.count() > 1).limit(1)).first()
        if duplicate:
            raise ValueError("正规本体投影违反运行契约: 相同类型、端点和属性的链接重复（duplicate_link）")
        for owner in link_types:
            for constrained, opposite, kinds in (
                (edges.c.source, edges.c.target, {"one-to-one", "many-to-one"}),
                (edges.c.target, edges.c.source, {"one-to-one", "one-to-many"}),
            ):
                if owner.cardinality not in kinds:
                    continue
                violation = db.execute(select(constrained).where(edges.c.owner == owner.id).group_by(
                    constrained).having(func.count(func.distinct(opposite)) > 1).limit(1)).first()
                if violation:
                    raise ValueError(f"正规本体投影违反运行契约: 关系「{owner.name}」基数冲突（cardinality_violation）")
    except BaseException:
        if db.is_active:
            for table in (edges, keys):
                try:
                    table.drop(db.connection(), checkfirst=True)
                except Exception:
                    logger.warning("Formal validation cleanup deferred to rollback", exc_info=True)
        raise
    else:
        edges.drop(db.connection(), checkfirst=True)
        keys.drop(db.connection(), checkfirst=True)
