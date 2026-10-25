"""Complete-source FK/alternate-key inference with bounded SQL column stages.

Only one source/target column pair is expanded at a time; no N×columns Python
maps or durable EAV projection. Candidate statistics and joins cover every row.
"""
from __future__ import annotations

import hashlib
import logging
import re
from types import SimpleNamespace
import uuid

from sqlalchemy import Column, Integer, MetaData, String, Table, Text, func, select, delete

from app.ontologies.mappings.errors import MappingSourceError
from app.ontologies.mappings.source_staging import MAPPING_BATCH_SIZE, managed_stage, stage_connection
from app.ontologies.mappings.sql_link_materialization import RelationStage

logger = logging.getLogger(__name__)


class ColumnStage:
    """Transaction-local one-column normalization, indexed for exact joins."""
    def __init__(self, db, source, column, *, split=False):
        self.db = db
        self.table = Table("mapping_fk_" + uuid.uuid4().hex, MetaData(),
            Column("ordinal", Integer, primary_key=True),
            Column("entity_id", String, nullable=False),
            Column("raw", Text), Column("norm", Text),
            Column("raw_hash", String(64), index=True), Column("norm_hash", String(64), index=True),
            Column("prefix", String), Column("numeric", Integer, nullable=False),
            prefixes=["TEMPORARY"])
        self.table.create(stage_connection(db))
        ordinal = 0
        pending = []
        self.nonempty_count = 0
        self.long_count = 0
        self.numeric_count = 0
        try:
            for page in source_pages(db, source, [source.field(column).label("value")]):
                for row in page:
                    raw = row.value
                    if raw in (None, ""):
                        continue
                    self.nonempty_count += 1
                    self.long_count += len(raw) >= 3
                    numeric = re.fullmatch(r"[\d.\-+%,\s]+", raw) is not None
                    self.numeric_count += numeric
                    for value in (re.split(r"[,，、;；|]", raw) if split else [raw]):
                        value = value.strip()
                        if not value:
                            continue
                        norm = re.sub(r"[\s\-_]", "", value).upper()
                        if split and len(norm) < 3:
                            continue
                        ordinal += 1
                        prefix_match = re.fullmatch(r"([A-Za-z]+)[-_]?\d+", value)
                        pending.append({"ordinal": ordinal, "entity_id": row.entity_id,
                            "raw": value, "norm": norm,
                            "raw_hash": hashlib.sha256(value.encode()).hexdigest(),
                            "norm_hash": hashlib.sha256(norm.encode()).hexdigest(),
                            "prefix": prefix_match.group(1).upper() if prefix_match else None,
                            "numeric": int(numeric)})
                        if len(pending) == MAPPING_BATCH_SIZE:
                            db.execute(self.table.insert(), pending)
                            pending = []
            if pending:
                db.execute(self.table.insert(), pending)
            self.distinct_count = db.execute(select(func.count(func.distinct(self.table.c.raw)))).scalar_one()
        except BaseException:
            if db.is_active:
                try:
                    self.close()
                except Exception:
                    logger.warning("FK column cleanup deferred to rollback", exc_info=True)
            raise

    def endpoint(self, field):
        ranked = select(self.table.c.entity_id, self.table.c[field].label("join_value"),
            self.table.c[field + "_hash"].label("join_hash"),
            func.row_number().over(partition_by=self.table.c[field],
                                   order_by=self.table.c.ordinal.desc()).label("rank")).subquery()
        return select(ranked.c.entity_id, ranked.c.join_value, ranked.c.join_hash).where(ranked.c.rank == 1).subquery()

    def close(self):
        self.table.drop(self.db.connection(), checkfirst=True)


def source_pages(db, source, fields):
    after = 0
    while True:
        page = db.execute(select(source.table.c.ordinal, source.table.c.entity_id, *fields).where(
            source.table.c.ordinal > after).order_by(source.table.c.ordinal).limit(MAPPING_BATCH_SIZE)).all()
        if not page:
            return
        after = page[-1].ordinal
        yield page


def _relation_name(entity_class):
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", entity_class).upper()
    return "HAS_" + (re.sub(r"[^A-Z0-9_]", "", name) or "REF")


def _alternate_name(column, pk_columns):
    normalized = str(column).strip().lower().replace("_", " ")
    return (column not in pk_columns
        and not set(normalized.split()) & {"status", "state", "type", "category", "level", "rating", "stage"}
        and not any(token in str(column) for token in {"状态", "类型", "类别", "分类", "等级", "评级", "阶段", "标志", "是否"}))


def _matched_query(source, target):
    # Exact match takes precedence over normalization; both lookup relations use
    # deterministic last-row-wins endpoint ownership, matching legacy inference.
    exact = target.endpoint("raw")
    normalized = target.endpoint("norm")
    return select(source.table.c.ordinal, source.table.c.entity_id.label("src"),
        func.coalesce(exact.c.entity_id, normalized.c.entity_id).label("tgt")).select_from(
            source.table.outerjoin(exact,
                (source.table.c.raw_hash == exact.c.join_hash) & (source.table.c.raw == exact.c.join_value)).outerjoin(
                normalized, (source.table.c.norm_hash == normalized.c.join_hash)
                & (source.table.c.norm == normalized.c.join_value)))


def _publish(service, ontology_id, src_m, src_meta, tgt_m, tgt_meta,
             source, target, column, target_key, relation_type, *, via=None, confidence):
    from app.ontologies.mappings.models import OntologyLinkMapping
    with managed_stage(RelationStage(service._db)) as relation:
        joined = _matched_query(source, target).subquery()
        after = 0
        while True:
            page = service._db.execute(select(joined).where(joined.c.ordinal > after).order_by(
                joined.c.ordinal).limit(MAPPING_BATCH_SIZE)).mappings().all()
            if not page:
                break
            after = page[-1]["ordinal"]
            props = {"fk_column": column, "source": "fk_inference"}
            if via:
                props["via"] = via
            if via == "alternate_key":
                props["alt_column"] = target_key
            relation.append([{"id": service._stable_relation_id(ontology_id, row["src"], row["tgt"],
                relation_type, "fk_inference"), "source_entity": row["src"], "target_entity": row["tgt"],
                "properties": props} for row in page if row["tgt"] is not None])
        if service._db.execute(select(relation.table.c.id).limit(1)).first() is None:
            return None
        inferred = service._upsert_inferred_link_mapping(ontology_id=ontology_id,
            src_dataset_id=src_m.curated_dataset_id, tgt_dataset_id=tgt_m.curated_dataset_id,
            relation_type=relation_type, src_key=column, tgt_key=target_key, model=OntologyLinkMapping)
        count, cardinality = relation.publish(ontology_id,
            SimpleNamespace(id=inferred.id if inferred else "", relation_type=relation_type),
            confidence=confidence, reconcile=False)
        service._db.flush()
        return {"src": src_meta["entity_class"], "tgt": tgt_meta["entity_class"],
            "rel_type": relation_type, "fk_col": column, "count": count, "cardinality": cardinality,
            **({"via": via} if via else {}),
            **({"alt_col": target_key} if via == "alternate_key" else {}),
            "link_mapping_id": inferred.id if inferred else None,
            "link_mapping_status": inferred.status if inferred else None}


def infer_relations(service, ontology_id, mappings, mapping_meta):
    from app.ontologies.relations.models import Relation
    db = service._db
    eligible = [mapping for mapping in mappings if mapping.id in mapping_meta]
    # Validate completeness before deleting anything: never silently infer only
    # from a preview if the complete source stage is unavailable.
    for mapping in eligible:
        meta = mapping_meta[mapping.id]
        if "stage" not in meta or meta["stage"].count != meta.get("row_count"):
            raise MappingSourceError("Complete mapping stage required for FK inference; previews cannot execute joins")
    table = Relation.__table__
    while True:
        ids = list(db.execute(select(table.c.id).where(table.c.ontology_id == ontology_id,
            table.c.properties["source"].as_string() == "fk_inference").order_by(
                table.c.id).limit(MAPPING_BATCH_SIZE)).scalars())
        if not ids:
            break
        db.execute(delete(table).where(table.c.id.in_(ids)))
    results = []
    for src_m in eligible:
        src_meta = mapping_meta[src_m.id]
        for tgt_m in eligible:
            if src_m.id == tgt_m.id:
                continue
            tgt_meta = mapping_meta[tgt_m.id]
            target_pk_columns = service._pk_columns(tgt_meta.get("pk_col"))
            named = dict(service._detect_fk_columns(src_meta["columns"], tgt_meta["entity_class"], tgt_m.entity_class))
            linked = set()
            # A scalar FK cannot encode a composite target key. Such links must
            # use declared multi-column edge/direct contracts, not guessed IDs.
            if len(target_pk_columns) == 1:
                with managed_stage(ColumnStage(db, tgt_meta["stage"], target_pk_columns[0])) as target:
                    target_norm = target.endpoint("norm")
                    for column in src_meta["columns"]:
                        with managed_stage(ColumnStage(db, src_meta["stage"], column)) as source:
                            relation_type = named.get(column)
                            if not relation_type and source.nonempty_count >= 2:
                                matched = db.execute(select(func.count()).select_from(source.table.join(
                                    target_norm, (source.table.c.norm_hash == target_norm.c.join_hash)
                                    & (source.table.c.norm == target_norm.c.join_value)))).scalar_one()
                                if matched >= 2 and matched / source.nonempty_count >= 0.5:
                                    relation_type = _relation_name(tgt_meta["entity_class"])
                                else:
                                    prefix = db.execute(select(source.table.c.prefix, func.count()).where(
                                        source.table.c.prefix.is_not(None)).group_by(source.table.c.prefix).order_by(
                                            func.count().desc(), source.table.c.prefix).limit(1)).first()
                                    if prefix and prefix[1] >= 2:
                                        relation_type = "HAS_" + prefix[0]
                            if relation_type:
                                result = _publish(service, ontology_id, src_m, src_meta, tgt_m, tgt_meta,
                                    source, target, column, target_pk_columns[0], relation_type, confidence=0.85)
                                if result:
                                    linked.add(column)
                                    results.append(result)
                            elif column == target_pk_columns[0] and source.nonempty_count:
                                result = _publish(service, ontology_id, src_m, src_meta, tgt_m, tgt_meta,
                                    source, target, column, target_pk_columns[0],
                                    _relation_name(tgt_meta["entity_class"]), via="same_name_fk", confidence=0.85)
                                if result:
                                    linked.add(column)
                                    results.append(result)
            # Alternate-key qualification and token matching also inspect every
            # row, retaining no all-source name maps or entity-pair sets.
            for alt_column in tgt_meta["columns"]:
                if not _alternate_name(alt_column, target_pk_columns):
                    continue
                with managed_stage(ColumnStage(db, tgt_meta["stage"], alt_column)) as target:
                    n = target.nonempty_count
                    if (n < 2 or target.distinct_count < n * 0.9
                            or target.numeric_count > n * 0.8 or target.long_count < n * 0.8):
                        continue
                    for column in src_meta["columns"]:
                        if column in linked or column in service._pk_columns(src_meta.get("pk_col")):
                            continue
                        with managed_stage(ColumnStage(db, src_meta["stage"], column, split=True)) as source:
                            if source.distinct_count <= 1:
                                continue
                            result = _publish(service, ontology_id, src_m, src_meta, tgt_m, tgt_meta,
                                source, target, column, alt_column, _relation_name(tgt_meta["entity_class"]),
                                via="alternate_key", confidence=0.75)
                            if result:
                                results.append(result)
    return results
