"""Authoritative SQL-to-Neo4j rebuild with bounded, keyset-paged writes."""
from __future__ import annotations

import hashlib
import logging
import re
from collections import defaultdict

from sqlalchemy import and_, case, exists, func, or_, select, union_all
from sqlalchemy.orm import Session

from app.ontologies.mappings.neo4j_projection_contract import (
    neo4j_entity_properties, neo4j_projection_properties,
)


logger = logging.getLogger(__name__)
# A write batch, not a truncation limit: every authoritative row is projected.
PROJECTION_BATCH_SIZE = 500


def _relationship_type(value: str | None) -> str:
    """Return a deterministic, injection-safe Cypher relationship type."""
    raw = str(value or "RELATED").strip() or "RELATED"
    normalized = re.sub(r"[^A-Za-z0-9_]", "_", raw).strip("_").upper()
    if not normalized:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12].upper()
        normalized = f"REL_{digest}"
    if normalized[0].isdigit():
        normalized = "REL_" + normalized
    return normalized


def _projection_key(ontology_id: str, stable_id: str) -> str:
    """Neo4j MERGE key; public IDs remain ontology-local stable IDs."""
    return f"{ontology_id}::{stable_id}"


def projection_node_id(
    formal_instance_id: object,
    external_id: object | None,
    legacy_entity_ids: set[str],
) -> str:
    """Resolve the public node ID shared by rebuild and repair reporting."""
    normalized_external_id = (
        str(external_id) if external_id is not None else None
    )
    if (
        normalized_external_id is not None
        and normalized_external_id in legacy_entity_ids
    ):
        return normalized_external_id
    return str(formal_instance_id)


def _columns(table, prefix):
    return [column.label(prefix + column.key) for column in table.c]


def _part(row, prefix):
    return {key[len(prefix):]: value for key, value in row.items()
            if key.startswith(prefix)}


def _batches(db, statement, key):
    """Page scalar SQL rows without retaining ORM objects or an open cursor."""
    after = None
    while True:
        page = statement
        if after is not None:
            page = page.where(key > after)
        rows = db.execute(
            page.order_by(key).limit(PROJECTION_BATCH_SIZE)
        ).mappings().all()
        if not rows:
            return
        yield rows
        after = rows[-1][key]


def _legacy_node(entity, ontology_id):
    stable_id = str(entity["id"])
    business = neo4j_entity_properties(
        entity["properties"], entity_id=stable_id,
        ontology_id=ontology_id, updated_at=entity["updated_at"],
        encode_values=False,
    )
    return {
        **business,
        "id": stable_id,
        "projection_key": _projection_key(ontology_id, stable_id),
        "source_entity_id": stable_id,
        "ontology_id": ontology_id,
        "name_cn": entity["name_cn"] or "",
        "name": entity["name_cn"] or "",
        "name_en": entity["name_en"] or "",
        "type": entity["type"] or "Object",
        "description": entity["description"] or "",
        "confidence": entity["confidence"],
        "version": entity["version"] or "v0.1",
    }


def _formal_node(instance, object_type, stable_id, ontology_id, existing):
    business = {**dict(instance["properties"] or {}),
                **dict(instance["computed"] or {})}
    envelope = {
        "id": stable_id,
        "projection_key": _projection_key(ontology_id, stable_id),
        "formal_instance_id": str(instance["id"]),
        "object_type_id": str(instance["object_type_id"]),
        "ontology_id": ontology_id,
        "type": (object_type["name"] if object_type["id"] is not None
                 else existing.get("type") or "Object"),
        "type_display_name": (object_type["display_name"]
                              if object_type["id"] is not None else ""),
        "name": (business.get("name") or business.get("name_cn")
                 or existing.get("name") or stable_id),
        "name_cn": (business.get("name_cn") or business.get("name")
                    or existing.get("name_cn") or stable_id),
        "updated_at": instance["updated_at"],
    }
    return {**existing, **neo4j_projection_properties(business, envelope)}


def _write_relations(neo, rows, ontology_id):
    # Group only this bounded batch; never collect all rows by relationship type.
    grouped = defaultdict(list)
    for source, target, semantic_type, properties in rows:
        grouped[_relationship_type(semantic_type)].append({
            "src_key": _projection_key(ontology_id, source),
            "tgt_key": _projection_key(ontology_id, target),
            "props": properties,
        })
    for relationship_type, batch in grouped.items():
        written = neo.batch_upsert_relations(
            "OntologyEntity", "OntologyEntity", relationship_type,
            batch, key_field="projection_key",
        )
        if written != len(batch):
            raise RuntimeError(
                f"Neo4j relationship projection incomplete: "
                f"expected={len(batch)} actual={written} ontology={ontology_id}")


def rebuild_neo4j_projection(db: Session, ontology_id: str) -> bool:
    """Project every SQL row using bounded memory under the caller's fence.

    SQL joins resolve the frozen legacy namespace and validate collisions before
    destructive graph work. No instance/endpoint/relationship ID set grows with
    ontology size. Stable IDs and reserved business-property encoding are shared
    with repair and incremental projection.
    """
    from app.ontologies.entities.models import Entity
    from app.ontologies.relations.models import Relation
    from app.ontologies.formal_modeling.models import (
        LinkInstance, LinkType, ObjectInstance, ObjectType,
    )
    from app.ontologies.graph.neo4j_service import Neo4jService

    entity = Entity.__table__
    instance = ObjectInstance.__table__
    object_type = ObjectType.__table__
    relation = Relation.__table__
    link = LinkInstance.__table__
    link_type = LinkType.__table__
    mirror = and_(entity.c.id == instance.c.external_id,
                  entity.c.ontology_id == instance.c.ontology_id)
    stable = select(
        instance.c.id.label("formal_id"),
        instance.c.external_id,
        case((entity.c.id.is_not(None), entity.c.id),
             else_=instance.c.id).label("stable_id"),
    ).select_from(instance.outerjoin(entity, mirror)).where(
        instance.c.ontology_id == ontology_id).subquery("stable_nodes")
    neo = None
    try:
        neo = Neo4jService()
        if not neo.available:
            return False
        # Detect ownership collisions in SQL, including ones spanning batches.
        duplicate = select(stable.c.stable_id).group_by(
            stable.c.stable_id).having(func.count() > 1)
        collision = select(stable.c.formal_id).select_from(stable.join(
            entity, and_(entity.c.id == stable.c.stable_id,
                         entity.c.ontology_id == ontology_id),
        )).where(or_(stable.c.external_id.is_(None),
                     stable.c.external_id != stable.c.stable_id))
        link_collision = select(link.c.id).select_from(link.join(
            relation, and_(relation.c.id == link.c.id,
                           relation.c.ontology_id == ontology_id),
        )).where(link.c.ontology_id == ontology_id).where(or_(
            link.c.source_relation_id.is_(None),
            link.c.source_relation_id != link.c.id,
        ))
        nodes = union_all(
            select(entity.c.id.label("id")).where(
                entity.c.ontology_id == ontology_id),
            select(stable.c.stable_id.label("id")),
        ).subquery("node_ids")
        represented = exists(select(link.c.id).where(
            link.c.ontology_id == ontology_id,
            link.c.source_relation_id == relation.c.id,
        ))
        legacy_relations = select(*_columns(relation, "r_")).where(
            relation.c.ontology_id == ontology_id, ~represented,
        )
        missing_legacy = select(relation.c.id).where(
            relation.c.ontology_id == ontology_id, ~represented,
            or_(
                ~exists(select(nodes.c.id).where(
                    nodes.c.id == relation.c.source_entity)),
                ~exists(select(nodes.c.id).where(
                    nodes.c.id == relation.c.target_entity)),
            ),
        )
        source = stable.alias("source_node")
        target = stable.alias("target_node")
        link_endpoints = link.outerjoin(
            source, source.c.formal_id == link.c.source_object_id,
        ).outerjoin(target, target.c.formal_id == link.c.target_object_id)
        missing_formal = select(link.c.id).select_from(link_endpoints).where(
            link.c.ontology_id == ontology_id,
            or_(source.c.formal_id.is_(None), target.c.formal_id.is_(None)),
        )
        for check in (duplicate, collision, link_collision,
                      missing_legacy, missing_formal):
            if db.execute(check.limit(1)).first() is not None:
                raise RuntimeError(
                    f"Projection identity collision or missing endpoint: {ontology_id}")

        neo.delete_by_ontology(ontology_id)

        legacy_nodes = select(
            *_columns(entity, "e_"), *_columns(instance, "f_"),
            *_columns(object_type, "t_"), entity.c.id,
        ).select_from(entity.outerjoin(instance, mirror).outerjoin(
            object_type, and_(object_type.c.id == instance.c.object_type_id,
                              object_type.c.ontology_id == ontology_id),
        )).where(entity.c.ontology_id == ontology_id)
        formal_nodes = select(
            *_columns(instance, "f_"), *_columns(object_type, "t_"),
            instance.c.id,
        ).select_from(instance.outerjoin(object_type, and_(
            object_type.c.id == instance.c.object_type_id,
            object_type.c.ontology_id == ontology_id,
        ))).where(
            instance.c.ontology_id == ontology_id,
            ~exists(select(entity.c.id).where(mirror)),
        )
        for statement, key, legacy in (
            (legacy_nodes, entity.c.id, True),
            (formal_nodes, instance.c.id, False),
        ):
            for batch in _batches(db, statement, key):
                rows = []
                for row in batch:
                    formal = _part(row, "f_")
                    node = _legacy_node(_part(row, "e_"), ontology_id) if legacy else {}
                    stable_id = str(row["id"])
                    if formal["id"] is not None:
                        node = _formal_node(formal, _part(row, "t_"),
                                            stable_id, ontology_id, node)
                    rows.append(node)
                written = neo.batch_upsert_entities(
                    "OntologyEntity", rows, key_field="projection_key")
                if written != len(rows):
                    raise RuntimeError(
                        f"Neo4j node projection incomplete: "
                        f"expected={len(rows)} actual={written} ontology={ontology_id}")

        for batch in _batches(db, legacy_relations.add_columns(relation.c.id),
                              relation.c.id):
            rows = []
            for row in batch:
                item = _part(row, "r_")
                semantic_type = str(item["type"] or "RELATED")
                rows.append((str(item["source_entity"]), str(item["target_entity"]),
                             semantic_type, neo4j_projection_properties(
                                 item["properties"], {
                                     "id": str(item["id"]), "ontology_id": ontology_id,
                                     "confidence": item["confidence"],
                                     "semantic_type": semantic_type,
                                 })))
            _write_relations(neo, rows, ontology_id)

        formal_links = select(
            *_columns(link, "l_"), source.c.stable_id.label("source_id"),
            target.c.stable_id.label("target_id"),
            link_type.c.name.label("semantic_type"), link.c.id,
        ).select_from(link_endpoints.outerjoin(link_type, and_(
            link_type.c.id == link.c.link_type_id,
            link_type.c.ontology_id == ontology_id,
        ))).where(link.c.ontology_id == ontology_id)
        for batch in _batches(db, formal_links, link.c.id):
            rows = []
            for row in batch:
                item = _part(row, "l_")
                semantic_type = row["semantic_type"] or "RELATED"
                rows.append((row["source_id"], row["target_id"], semantic_type,
                             neo4j_projection_properties(item["properties"], {
                                 "id": str(item["id"]),
                                 "formal_link_instance_id": str(item["id"]),
                                 "link_type_id": str(item["link_type_id"]),
                                 "ontology_id": ontology_id,
                                 "source_relation_id": item["source_relation_id"],
                                 "semantic_type": semantic_type,
                             })))
            _write_relations(neo, rows, ontology_id)
        return True
    except Exception as exc:
        logger.warning("Neo4j projection rebuild failed: %s", exc)
        return False
    finally:
        if neo is not None:
            try:
                neo.close()
            except Exception:
                pass
