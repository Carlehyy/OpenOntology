"""SQL-backed explicit LinkMapping joins and exact, bounded deletion diffs."""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import logging
import uuid

from sqlalchemy import Column, Integer, JSON, MetaData, String, Table, Text, func, select, delete, tuple_

from app.ontologies.mappings.source_staging import MAPPING_BATCH_SIZE, join_text, managed_stage, stage_connection

logger = logging.getLogger(__name__)


def _upsert(db, table, rows):
    if not rows:
        return
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise ValueError("Bounded mapping SQL writes require PostgreSQL or SQLite")
    # PostgreSQL ON CONFLICT cannot update one target twice in one INSERT.
    # Legacy inference/direct joins collapse duplicate pairs deterministically.
    rows = list({row["id"]: row for row in rows}.values())
    stmt = insert(table).values(rows)
    db.execute(stmt.on_conflict_do_update(
        index_elements=[table.c.id],
        set_={key: getattr(stmt.excluded, key) for key in rows[0] if key != "id"}))


class RelationStage:
    def __init__(self, db):
        self.db = db
        self.table = Table(
            "mapping_edges_" + uuid.uuid4().hex, MetaData(),
            Column("id", String, primary_key=True),
            Column("source_entity", String, nullable=False, index=True),
            Column("target_entity", String, nullable=False, index=True),
            Column("properties", JSON, nullable=False), prefixes=["TEMPORARY"])
        self.table.create(stage_connection(db))

    def append(self, values):
        if len(values) > MAPPING_BATCH_SIZE:
            raise ValueError("Relation staging pages may contain at most 500 rows")
        _upsert(self.db, self.table, values)

    def cardinality(self):
        table = self.table
        source_many = self.db.execute(select(table.c.source_entity).group_by(
            table.c.source_entity).having(func.count(func.distinct(table.c.target_entity)) > 1).limit(1)).first() is not None
        target_many = self.db.execute(select(table.c.target_entity).group_by(
            table.c.target_entity).having(func.count(func.distinct(table.c.source_entity)) > 1).limit(1)).first() is not None
        if not self.db.execute(select(table.c.id).limit(1)).first():
            return "unknown"
        if source_many and target_many:
            return "many_to_many"
        if source_many:
            return "one_to_many"
        if target_many:
            return "many_to_one"
        return "one_to_one"

    def publish(self, ontology_id, link, *, confidence=1.0, reconcile=True):
        from app.ontologies.relations.models import Relation
        table = Relation.__table__
        cardinality = self.cardinality()
        count = 0
        after = None
        while True:
            stmt = select(self.table).order_by(self.table.c.id).limit(MAPPING_BATCH_SIZE)
            if after is not None:
                stmt = stmt.where(self.table.c.id > after)
            page = self.db.execute(stmt).mappings().all()
            if not page:
                break
            after = page[-1]["id"]
            _upsert(self.db, table, [{
                "id": row["id"], "ontology_id": ontology_id,
                "source_entity": row["source_entity"], "target_entity": row["target_entity"],
                "type": link.relation_type, "confidence": confidence,
                "properties": {**row["properties"], "cardinality": cardinality},
            } for row in page])
            count += len(page)
        # Keep SQL deletion result bounded too; no giant stale-id IN predicate.
        while reconcile:
            stale_ids = list(self.db.execute(select(table.c.id).where(
                table.c.ontology_id == ontology_id,
                table.c.properties["__link_mapping_id__"].as_string() == link.id,
                ~table.c.id.in_(select(self.table.c.id)),
            ).order_by(table.c.id).limit(MAPPING_BATCH_SIZE)).scalars())
            if not stale_ids:
                break
            self.db.execute(delete(table).where(table.c.id.in_(stale_ids)))
        return count, cardinality

    def close(self):
        self.table.drop(self.db.connection(), checkfirst=True)


def _join_pages(db, query, cursor_columns):
    """Keyset the joined output too: one input key may have millions of matches."""
    after = None
    while True:
        stmt = query.order_by(*cursor_columns).limit(MAPPING_BATCH_SIZE)
        if after is not None:
            stmt = stmt.where(tuple_(*cursor_columns) > tuple_(*after))
        page = db.execute(stmt).mappings().all()
        if not page:
            return
        after = tuple(page[-1][column.key] for column in cursor_columns)
        yield page


class EndpointStage:
    """Keep every distinct legal endpoint; a nonunique join key fans out."""
    def __init__(self, stage, key):
        self.db = stage.db
        self.table = Table("mapping_endpoint_" + uuid.uuid4().hex, MetaData(),
            Column("ordinal", Integer, primary_key=True), Column("join_value", Text, nullable=False),
            Column("join_hash", String(64), index=True),
            Column("entity_id", String, nullable=False), prefixes=["TEMPORARY"])
        try:
            self.table.create(stage_connection(self.db))
            source = stage.table
            distinct_rows = select(func.min(source.c.ordinal).label("ordinal")).where(
                stage.field(key).is_not(None), stage.field(key) != "",
            ).group_by(stage.field(key), source.c.entity_id).subquery()
            query = select(source.c.ordinal, stage.field(key).label("value"), source.c.entity_id).join(
                distinct_rows, distinct_rows.c.ordinal == source.c.ordinal)
            after = 0
            while True:
                page = self.db.execute(query.where(source.c.ordinal > after).order_by(
                    source.c.ordinal).limit(MAPPING_BATCH_SIZE)).all()
                if not page:
                    break
                after = page[-1].ordinal
                self.db.execute(self.table.insert(), [{"ordinal": row.ordinal, "join_value": row.value,
                    "join_hash": hashlib.sha256(row.value.encode()).hexdigest(), "entity_id": row.entity_id} for row in page])
        except BaseException:
            if self.db.is_active:
                try:
                    self.close()
                except Exception:
                    logger.warning("Endpoint cleanup deferred to rollback", exc_info=True)
            raise

    def close(self):
        self.table.drop(self.db.connection(), checkfirst=True)


class EdgeRequestStage:
    """At most one source batch; fanout stays in the database join."""
    def __init__(self, db):
        self.db = db
        self.table = Table("mapping_edge_requests_" + uuid.uuid4().hex, MetaData(),
            Column("ordinal", Integer, primary_key=True),
            Column("source_key", Text), Column("source_hash", String(64)),
            Column("target_key", Text), Column("target_hash", String(64)),
            Column("edge_key", Text), Column("properties", JSON), prefixes=["TEMPORARY"])
        self.table.create(stage_connection(db))

    def replace(self, rows):
        if len(rows) > MAPPING_BATCH_SIZE:
            raise ValueError("Edge request pages may contain at most 500 rows")
        self.db.execute(delete(self.table))
        if rows:
            self.db.execute(self.table.insert(), rows)

    def close(self):
        self.table.drop(self.db.connection(), checkfirst=True)


def process_edge_link(service, ontology_id, link, src_meta, tgt_meta):
    from types import SimpleNamespace
    from app.data_channel.connections.type_normalization import normalize_cell
    from app.ontologies.mappings.source_batches import prepare_mapping_source
    source = prepare_mapping_source(service._db, SimpleNamespace(
        id=link.id, curated_dataset_id=link.edge_dataset_id), batch_size=MAPPING_BATCH_SIZE)
    pk_columns = source.metadata.get("primary_key_columns") or []
    pk_column = ",".join(pk_columns)
    if not pk_column:
        raise ValueError("Edge dataset has no canonical primary key")
    field_map = {key: value for key, value in dict(link.field_mapping or {}).items()
                 if not str(key).startswith("__")}
    from app.ontologies.mappings.raw_json_guard import structured_json_columns, reject_raw_json_columns
    structured_columns = structured_json_columns(service._db, link, edge=True)
    with ExitStack() as resources:
        src_endpoint = resources.enter_context(managed_stage(EndpointStage(src_meta["stage"], src_meta["pk_col"])))
        tgt_endpoint = resources.enter_context(managed_stage(EndpointStage(tgt_meta["stage"], tgt_meta["pk_col"])))
        stage = resources.enter_context(managed_stage(RelationStage(service._db)))
        requests = resources.enter_context(managed_stage(EdgeRequestStage(service._db)))
        request = requests.table
        src = src_endpoint.table
        tgt = tgt_endpoint.table
        cursors = [request.c.ordinal.label("request_ordinal"), src.c.ordinal.label("source_ordinal"),
                   tgt.c.ordinal.label("target_ordinal")]
        query = select(*cursors, src.c.entity_id.label("src"), tgt.c.entity_id.label("tgt"),
                       request.c.edge_key, request.c.properties).select_from(
            request.join(src, (request.c.source_hash == src.c.join_hash) &
                         (request.c.source_key == src.c.join_value)).join(tgt,
                (request.c.target_hash == tgt.c.join_hash) & (request.c.target_key == tgt.c.join_value)))
        for batch in source.batches:
            reject_raw_json_columns(batch, structured_columns)
            if any(not service._has_complete_pk(row, pk_column) for row in batch):
                raise ValueError("Edge dataset row is missing a canonical primary-key component")
            values = []
            for ordinal, raw_row in enumerate(batch):
                source_key = join_text(raw_row.get(link.src_key))
                target_key = join_text(raw_row.get(link.tgt_key))
                if source_key in (None, "") or target_key in (None, ""):
                    continue
                row = normalize_cell(raw_row)
                values.append({"ordinal": ordinal, "source_key": source_key, "target_key": target_key,
                    "source_hash": hashlib.sha256(source_key.encode()).hexdigest(),
                    "target_hash": hashlib.sha256(target_key.encode()).hexdigest(),
                    "edge_key": service._row_identity_value(raw_row, pk_column),
                    "properties": {prop: row[col] for prop, col in field_map.items() if col in row}})
            requests.replace(values)
            for page in _join_pages(service._db, query, cursors):
                stage.append([{
                    "id": service._stable_relation_id(ontology_id, row["src"], row["tgt"],
                        link.relation_type, f"link_mapping:{link.id}:{row['edge_key']}"),
                    "source_entity": row["src"], "target_entity": row["tgt"],
                    "properties": {"mapping_type": "link_mapping", "__edge_key__": row["edge_key"],
                        "__link_mapping_id__": link.id, **row["properties"]},
                } for row in page])
        count, cardinality = stage.publish(ontology_id, link)
        service._record_link_mapping_versions(link, src_meta, tgt_meta, edge_version_id=source.version.id)
        return {"src": src_meta["entity_class"], "tgt": tgt_meta["entity_class"],
            "rel_type": link.relation_type, "src_key": link.src_key, "tgt_key": link.tgt_key,
            "link_mapping_id": link.id, "edge_dataset_id": link.edge_dataset_id,
            "edge_properties": list(field_map), "count": count, "cardinality": cardinality,
            "warning": None if count else "连接表未匹配到任何两端实体"}


def process_direct_link(service, ontology_id, link, src_meta, tgt_meta):
    with ExitStack() as resources:
        source = resources.enter_context(managed_stage(EndpointStage(src_meta["stage"], link.src_key)))
        target = resources.enter_context(managed_stage(EndpointStage(tgt_meta["stage"], link.tgt_key)))
        stage = resources.enter_context(managed_stage(RelationStage(service._db)))
        src, tgt = source.table, target.table
        cursors = [src.c.ordinal.label("source_ordinal"), tgt.c.ordinal.label("target_ordinal")]
        query = select(*cursors, src.c.entity_id.label("src"), tgt.c.entity_id.label("tgt")).select_from(
            src.join(tgt, (src.c.join_hash == tgt.c.join_hash) & (src.c.join_value == tgt.c.join_value)))
        for page in _join_pages(service._db, query, cursors):
            stage.append([{
                "id": service._stable_relation_id(ontology_id, row["src"], row["tgt"],
                    link.relation_type, f"link_mapping:{link.id}"),
                "source_entity": row["src"], "target_entity": row["tgt"],
                "properties": {"mapping_type": "link_mapping", "src_key": link.src_key,
                    "tgt_key": link.tgt_key, "__link_mapping_id__": link.id},
            } for row in page])
        count, cardinality = stage.publish(ontology_id, link)
        service._record_link_mapping_versions(link, src_meta, tgt_meta)
        return {"src": src_meta["entity_class"], "tgt": tgt_meta["entity_class"],
            "rel_type": link.relation_type, "src_key": link.src_key, "tgt_key": link.tgt_key,
            "link_mapping_id": link.id, "count": count, "cardinality": cardinality,
            "warning": None if count else "No rows matched this link mapping"}
