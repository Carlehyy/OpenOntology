"""Transaction-local SQL indexes for complete isolated trial projections.

Only join keys live in this staging area; the immutable trial tables remain the
candidate authority. No production objects, links, facts or actions are written.
"""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
import hashlib
import uuid

from sqlalchemy import (
    Column, Index, Integer, JSON, MetaData, String, Table, Text, and_, func, or_, select,
)

from app.ontologies.mappings.source_staging import join_text


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@contextmanager
def trial_join_index(db):
    """Create disposable, transaction-bound indexes without persistent schema."""
    suffix = uuid.uuid4().hex
    metadata = MetaData()
    lookups = Table(
        f"trial_join_{suffix}", metadata,
        Column("scope", String, nullable=False), Column("column_name", String, nullable=False),
        Column("key_hash", String(64), nullable=False), Column("key_text", Text, nullable=False),
        Column("object_id", String, nullable=False), Column("entity_id", String, nullable=False),
        prefixes=["TEMPORARY"],
    )
    Index(f"ix_trial_join_{suffix}", lookups.c.scope, lookups.c.column_name, lookups.c.key_hash)
    edges = Table(
        f"trial_edge_{suffix}", metadata,
        Column("ordinal", Integer, primary_key=True, autoincrement=False),
        Column("scope", String, nullable=False), Column("row_json", JSON, nullable=False),
        Column("properties", JSON, nullable=False), Column("source_hash", String(64), nullable=False),
        Column("source_text", Text, nullable=False), Column("target_hash", String(64), nullable=False),
        Column("target_text", Text, nullable=False), prefixes=["TEMPORARY"],
    )
    edge_keys = Table(
        f"trial_edge_key_{suffix}", metadata,
        Column("scope", String, nullable=False), Column("column_name", String, nullable=False),
        Column("key_text", Text, nullable=False), prefixes=["TEMPORARY"],
    )
    Index(f"ix_trial_edge_{suffix}", edges.c.scope, edges.c.ordinal)
    Index(f"ix_trial_edge_key_{suffix}", edge_keys.c.scope, edge_keys.c.column_name)
    connection = db.connection()
    metadata.create_all(connection)
    try:
        yield TrialJoinIndex(db, lookups, edges, edge_keys)
    finally:
        metadata.drop_all(connection)


class TrialJoinIndex:
    def __init__(self, db, lookups, edges, edge_keys):
        self.db, self.lookups, self.edges, self.edge_keys = db, lookups, edges, edge_keys
        self.edge_ordinal = 0

    def add_endpoints(self, scope, rows, columns):
        values = []
        for row, object_id, entity_id in rows:
            for column in columns:
                text = join_text(row.get(column))
                if text in (None, ""):
                    continue
                values.append(dict(scope=scope, column_name=column, key_hash=_digest(text),
                                   key_text=text, object_id=object_id, entity_id=entity_id))
                if len(values) == 1000:
                    self.db.execute(self.lookups.insert(), values)
                    values.clear()
        if values:
            self.db.execute(self.lookups.insert(), values)

    def add_edges(self, scope, rows, source_key, target_key, field_mapping, *, collect_identity_keys=True):
        values, keys = [], []
        for row in rows:
            self.edge_ordinal += 1
            # Keep unmatched source rows for exact PK selection, but never
            # turn missing/blank join values into a matching endpoint key.
            source = join_text(row.get(source_key)) or ""
            target = join_text(row.get(target_key)) or ""
            values.append(dict(
                ordinal=self.edge_ordinal, scope=scope, row_json=row,
                properties={str(prop): row[column] for prop, column in field_mapping.items()
                            if not str(prop).startswith("__") and column in row},
                source_hash=_digest(source), source_text=source,
                target_hash=_digest(target), target_text=target,
            ))
            for column in row if collect_identity_keys else ():
                if column != "content":
                    keys.append(dict(scope=scope, column_name=column,
                                     key_text=str(row.get(column, "")).strip()))
                    if len(keys) == 1000:
                        self.db.execute(self.edge_keys.insert(), keys)
                        keys.clear()
        if values:
            self.db.execute(self.edges.insert(), values)
        if keys:
            self.db.execute(self.edge_keys.insert(), keys)

    def choose_edge_primary_key(self, scope, columns, row_count):
        """Exact global equivalent of MappingService._choose_pk_col."""
        if not row_count:
            return "id"
        columns = [column for column in columns if column != "content"]
        id_like = [column for column in columns if (
            column.lower() == "id" or column.lower().endswith("_id")
            or column.endswith("ID") or "id" in column.lower()
        )]
        preferred = [column for column in (
            "filename", "source_file", "source_dataset_id", "order_id", "订单号", "供应商ID",
        ) if column in columns]
        for column in dict.fromkeys(id_like + preferred + columns):
            query = select(
                func.count(), func.count(func.distinct(self.edge_keys.c.key_text)),
                func.min(func.length(self.edge_keys.c.key_text)),
            ).where(self.edge_keys.c.scope == scope, self.edge_keys.c.column_name == column)
            count, unique, minimum = self.db.execute(query).one()
            if count == unique == row_count and minimum:
                return column
        return "__row_hash__"

    def pairs(self, source, target, source_column, target_column, *, edge_scope=None):
        """Yield every SQL-joined pair in bounded keyset pages, never OFFSET."""
        src, tgt = self.lookups.alias("src"), self.lookups.alias("tgt")
        filters = [src.c.scope == source, tgt.c.scope == target,
                   src.c.column_name == source_column, tgt.c.column_name == target_column]
        if edge_scope is None:
            query = select(src.c.object_id, src.c.entity_id, tgt.c.object_id, tgt.c.entity_id).select_from(
                src.join(tgt, and_(src.c.key_hash == tgt.c.key_hash, src.c.key_text == tgt.c.key_text)),
            )
            order = [src.c.object_id, tgt.c.object_id]
        else:
            edge = self.edges.alias("edge")
            query = select(src.c.object_id, src.c.entity_id, tgt.c.object_id, tgt.c.entity_id,
                           edge.c.row_json, edge.c.properties, edge.c.ordinal).select_from(
                edge.join(src, and_(edge.c.source_hash == src.c.key_hash,
                                   edge.c.source_text == src.c.key_text)).join(
                    tgt, and_(edge.c.target_hash == tgt.c.key_hash,
                              edge.c.target_text == tgt.c.key_text)),
            )
            filters.append(edge.c.scope == edge_scope)
            order = [edge.c.ordinal, src.c.object_id, tgt.c.object_id]
        after = None
        while True:
            page = query.where(*filters)
            if after is not None:
                page = page.where(or_(*[
                    and_(*(order[j] == after[j] for j in range(i)), order[i] > after[i])
                    for i in range(len(order))
                ]))
            rows = self.db.execute(page.order_by(*order).limit(500)).all()
            if not rows:
                return
            last = rows[-1]
            after = (last[0], last[2]) if edge_scope is None else (last[6], last[0], last[2])
            yield rows


class TrialProjectionReader:
    """Read-only candidate port used by isolated Sentinel Action previews.

    ``iter`` is reiterable at the method level and keyset-paged; ``find`` never
    falls back to mutable Formal tables. Cross-page uniqueness/cardinality use
    scalar SQL scans, with the canonical validator owning error semantics.
    """

    def __init__(self, db, objects, links, models):
        self.db, self.objects, self.links, self.models = db, objects, links, models

    @staticmethod
    def object_model(item):
        return SimpleNamespace(id=item.object_id, object_type_id=item.object_type_id,
                               properties=item.properties or {}, computed=item.computed or {},
                               ontology_release_id=None)

    @staticmethod
    def link_model(item):
        return SimpleNamespace(id=item.link_id, link_type_id=item.link_type_id,
                               source_object_id=item.source_object_id, target_object_id=item.target_object_id,
                               properties=item.properties or {}, ontology_release_id=None)

    def iter(self, key, **filters):
        from app.ontologies.versions.batching import query_batches
        from app.ontologies.versions.models import OntologyTrialObject, OntologyTrialLink
        if key not in ("objects", "links"):
            aliases = {"object_types": "objectTypes", "link_types": "linkTypes"}
            yield from self.models.get(aliases.get(key, key), [])
            return
        object_rows = key == "objects"
        model = OntologyTrialObject if object_rows else OntologyTrialLink
        query = self.objects if object_rows else self.links
        row_key = model.object_id if object_rows else model.link_id
        for field, value in filters.items():
            if value is not None:
                query = query.filter(getattr(model, "object_id" if field == "id" and object_rows
                                              else "link_id" if field == "id" else field) == value)
        for page in query_batches(query, row_key):
            for item in page:
                yield self.object_model(item) if object_rows else self.link_model(item)

    def has_link(self, link_type_id, source_id, target_id):
        from app.ontologies.versions.models import OntologyTrialLink as L
        return self.links.filter(L.link_type_id == link_type_id,
                                 L.source_object_id == source_id,
                                 L.target_object_id == target_id).first() is not None

    @contextmanager
    def filtered_objects(self, type_id, predicate):
        """Stage every matching ID; candidate tuple cap is not a source cap."""
        from app.ontologies.versions.models import OntologyTrialObject as O
        from app.ontologies.versions.batching import query_batches
        table = Table(f"trial_filter_{uuid.uuid4().hex}", MetaData(),
                      Column("object_id", String, primary_key=True), prefixes=["TEMPORARY"])
        connection = self.db.connection()
        table.create(connection)
        try:
            ids = []
            for item in self.iter("objects", object_type_id=type_id):
                if predicate(item):
                    ids.append({"object_id": item.id})
                    if len(ids) == 200:
                        self.db.execute(table.insert(), ids)
                        ids.clear()
            if ids:
                self.db.execute(table.insert(), ids)
            def iterate():
                query = self.objects.join(table, table.c.object_id == O.object_id)
                for page in query_batches(query, O.object_id):
                    yield from (self.object_model(item) for item in page)
            yield iterate
        finally:
            table.drop(connection)

    def find(self, key, item_id):
        if key in ("objects", "links"):
            return next(self.iter(key, id=item_id), None)
        return next((item for item in self.iter(key) if str(item.id) == str(item_id)), None)

    def validate_object_candidate(self, candidate, extras):
        from app.ontologies.formal_modeling import validation
        types = self.models["objectTypes"]
        errors = validation.validate_instance_contract(types, [candidate], validate_ids={candidate.id})
        owner = next((item for item in types if str(item.id) == str(candidate.object_type_id)), None)
        if owner is None or not owner.primary_key:
            return errors
        prop = next((item for item in validation._property_defs(owner).values()
                     if item.get("id") == owner.primary_key or item.get("name") == owner.primary_key), None)
        if prop is None:
            return errors
        name = str(prop["name"])
        properties = candidate.properties or {}
        value = properties.get(name) if isinstance(properties, dict) else None
        if validation._is_empty(value):
            return errors
        # canonical JSON deliberately distinguishes 1, 1.0 and True. Database
        # JSON scalar equality does not, so stream only the relevant type's PK.
        canonical, count = validation._canonical(value), 1
        for page in self._property_pages(owner.id):
            for object_id, properties in page:
                if str(object_id) == str(candidate.id):
                    continue
                other = (properties or {}).get(name)
                if not validation._is_empty(other) and validation._canonical(other) == canonical:
                    count += 1
        seen = set()
        for item in extras:
            if item.id == candidate.id or item.id in seen or self.find("objects", item.id) is not None:
                continue
            seen.add(item.id)
            if str(item.object_type_id) == str(owner.id):
                other = (item.properties or {}).get(name)
                if not validation._is_empty(other) and validation._canonical(other) == canonical:
                    count += 1
        if count > 1:
            errors.append(validation._err("duplicate_primary_key", "objectInstance",
                                         f"同一对象类型下主键值重复（{count} 个实例）", item_id=candidate.id))
        return errors

    def _property_pages(self, type_id):
        from app.ontologies.versions.models import OntologyTrialObject as O
        after = None
        while True:
            query = self.objects.with_entities(O.object_id, O.properties).filter(O.object_type_id == type_id)
            if after is not None:
                query = query.filter(O.object_id > after)
            page = query.order_by(O.object_id).limit(200).all()
            if not page:
                return
            after = page[-1][0]
            yield page

    def validate_link_candidate(self, candidate, extra_objects, extra_links, excluded):
        from app.ontologies.formal_modeling import validation
        from app.ontologies.versions.models import OntologyTrialLink as L
        endpoint_ids = {str(candidate.source_object_id), str(candidate.target_object_id)}
        endpoints = [item for item_id in endpoint_ids
                     if (item := self.find("objects", item_id)) is not None]
        known = {item.id for item in endpoints}
        for item in extra_objects:
            if item.id not in known:
                endpoints.append(item)
                known.add(item.id)
        types = self.models["linkTypes"]
        errors = validation.validate_link_instance_contract(types, endpoints, [candidate], validate_ids={candidate.id})
        endpoint_errors = {"link_type_not_found", "source_instance_not_found", "target_instance_not_found",
                           "source_type_mismatch", "target_type_mismatch"}
        if any(item["code"] in endpoint_errors for item in errors):
            return errors
        owner = next(item for item in types if str(item.id) == str(candidate.link_type_id))
        source, target = str(candidate.source_object_id), str(candidate.target_object_id)
        # Only incident links can influence the selected candidate's duplicate
        # or cardinality contract. Their full properties are paged, not listed.
        query = self.links.filter(L.link_type_id == owner.id, or_(
            L.source_object_id == source, L.target_object_id == target))
        from app.ontologies.versions.batching import query_batches
        duplicate_count = 1
        id_count = 1 + (0 if str(candidate.id) in excluded else
                        self.links.filter(L.link_id == candidate.id).count())
        source_violation = target_violation = False
        key = validation._canonical(candidate.properties or {})

        def visit(item, *, virtual=False):
            nonlocal duplicate_count, id_count, source_violation, target_violation
            if not virtual and str(item.id) in excluded:
                return
            if virtual and item.id == candidate.id:
                id_count += 1
            if str(item.link_type_id) != str(owner.id):
                return
            src, tgt = str(item.source_object_id), str(item.target_object_id)
            # Canonical duplicate/cardinality validation omits links whose
            # endpoints are missing or of the wrong type. The local endpoint
            # view preserves base-before-extra precedence and bounded memory.
            by_id = {str(obj.id): obj for obj in endpoints}
            for endpoint in (src, tgt):
                if endpoint not in by_id:
                    obj = self.find("objects", endpoint)
                    if obj is not None:
                        by_id[endpoint] = obj
            if (getattr(by_id.get(src), "object_type_id", None) != owner.source_object_type_id
                    or getattr(by_id.get(tgt), "object_type_id", None) != owner.target_object_type_id):
                return
            if src == source and tgt == target and validation._canonical(item.properties or {}) == key:
                duplicate_count += 1
            source_violation |= src == source and tgt != target
            target_violation |= tgt == target and src != source

        for page in query_batches(query, L.link_id):
            for item in page:
                visit(self.link_model(item))
        for item in extra_links:
            visit(item, virtual=True)
        if id_count > 1:
            errors.append(validation._err("duplicate_link_instance_id", "linkInstance",
                                         f'链接实例 ID "{candidate.id}" 重复（{id_count} 个）', item_id=candidate.id))
        if duplicate_count > 1:
            errors.append(validation._err("duplicate_link", "linkInstance",
                                         f"相同类型、端点和属性的链接重复（{duplicate_count} 条）", item_id=candidate.id))
        for violation, cardinalities, direction, opposite in (
            (source_violation, ("one-to-one", "many-to-one"), "源", "目标"),
            (target_violation, ("one-to-one", "one-to-many"), "目标", "源"),
        ):
            if violation and owner.cardinality in cardinalities:
                errors.append(validation._err("cardinality_violation", "linkInstance",
                                             f"关系「{validation._label(owner)}」要求每个{direction}实例最多关联一个{opposite}实例",
                                             validation._label(owner), candidate.id))
        return errors


def _verify_remote_object_mappings(
        db, run, snap, errors, warnings, object_types, *,
        sample_limit: int) -> tuple[dict, dict, dict]:
    """Remote branch of the isolated trial: verify metadata, never materialize.

    Returns ``(remote_endpoint, remote_checks, remote_datasets)`` where
    ``remote_endpoint`` maps ``(datasetId, objectTypeId)`` to the frozen
    descriptor.  Sample row values are consumed in-memory only; the durable
    check record carries counts/fingerprints, never values.
    """
    from datetime import datetime, timezone

    from app.ontologies.mappings.remote_binding import (
        BINDING_MODE_REMOTE,
        RemoteBindingError,
        REMOTE_SAMPLE_ROW_LIMIT,
        remote_field_mapping_errors,
        snapshot_entry_binding,
        verify_remote_binding,
    )
    from app.ontologies.mappings.formal_projection import _coerce_props_to_type

    user = None
    from app.auth.models import User
    user = db.query(User).filter(User.id == run.created_by).first()

    remote_endpoint: dict[tuple[str, str], dict] = {}
    remote_checks: dict[str, dict] = {}
    remote_datasets: set[str] = set()

    for raw in snap["mappings"]:
        if not isinstance(raw, dict):
            continue
        mapping_id = str(raw.get("id") or "")
        entity_class = str(raw.get("entityClass") or "")
        try:
            mode, descriptor = snapshot_entry_binding(raw)
        except RemoteBindingError as exc:
            errors.append(dict(code=exc.code, kind="mapping", id=mapping_id,
                               name=entity_class,
                               message=f"远程绑定描述符非法：{exc.message}",
                               **exc.context))
            continue
        if mode != BINDING_MODE_REMOTE or descriptor is None:
            continue
        target = None
        for candidate in object_types.values():
            if (str(raw.get("targetObjectTypeId") or "") == str(candidate.get("id"))
                    or entity_class in (str(candidate.get("name") or ""),
                                        str(candidate.get("displayName") or ""))):
                target = candidate
                break
        if target is None:
            errors.append(dict(code="mapping_object_type_not_found", kind="mapping",
                               id=mapping_id, name=entity_class,
                               message="远程 Mapping 绑定的 ObjectType 不存在"))
            continue
        dataset_id = str(raw.get("curatedDatasetId") or "")
        field_map = raw.get("fieldMapping") or {}
        field_map = field_map if isinstance(field_map, dict) else {}
        pk = str(field_map.get("__primary_key__") or "")
        structural = remote_field_mapping_errors(
            field_map, descriptor, kind="mapping",
            item_id=mapping_id, name=entity_class)
        errors.extend(structural)
        if user is None:
            errors.append(dict(code="remote_user_missing", kind="mapping",
                               id=mapping_id, name=entity_class,
                               message="远程绑定验证需要试跑发起人账号存在"))
            continue
        try:
            verification = verify_remote_binding(
                db, user, descriptor,
                sample=True,
                sample_limit=min(sample_limit, REMOTE_SAMPLE_ROW_LIMIT) or
                REMOTE_SAMPLE_ROW_LIMIT)
        except RemoteBindingError as exc:
            errors.append(dict(code=exc.code, kind="mapping", id=mapping_id,
                               name=entity_class,
                               message=f"远程绑定验证失败：{exc.message}",
                               **exc.context))
            continue
        # 受控读仅在内存中验证类型兼容；失败只记属性名，绝不落样本值。
        sample_type_errors: list[str] = []
        for row in verification["sampleRows"]:
            props = {
                str(name): row[column]
                for column, name in field_map.items()
                if not str(column).startswith("__") and column in row
            }
            if pk and not all(row.get(column) not in (None, "")
                              for column in pk.split(",") if column):
                sample_type_errors.append("__primary_key__")
                break
            try:
                _coerce_props_to_type(props, list(target.get("properties") or []))
            except ValueError:
                sample_type_errors.extend(sorted(props))
                break
        if sample_type_errors:
            errors.append(dict(
                code="remote_sample_type_mismatch", kind="mapping",
                id=mapping_id, name=entity_class,
                message="远程受控读样本无法按声明的属性类型/主键契约解读"
                        "（仅记录属性名，不落样本值）",
                fields=sorted(set(sample_type_errors))))
            continue
        if dataset_id:
            remote_datasets.add(dataset_id)
            remote_endpoint[(dataset_id, str(target.get("id")))] = descriptor
        remote_checks[mapping_id] = {
            "kind": "mapping",
            "status": "verified",
            "materialized": False,
            "datasetId": dataset_id,
            "assetId": descriptor.get("assetId"),
            "schemaFingerprint": descriptor.get("schemaFingerprint"),
            "sampleRowCount": verification["sampleRowCount"],
            "verifiedAt": datetime.now(timezone.utc).isoformat(),
        }
        # 显式声明“未物化、非全量质量”：远程验证不冒充本地试跑口径。
        warnings.append(dict(
            code="remote_binding_not_materialized", kind="mapping",
            id=mapping_id, name=entity_class,
            message=(
                f"Mapping「{entity_class}」为远程绑定：试跑仅完成远程元数据与"
                f"受控读验证（{verification['sampleRowCount']} 行样本，未落值），"
                "未做本地物化，不计入对象/关系计数，不等价于全量数据质量验证")))
    return remote_endpoint, remote_checks, remote_datasets


def persist_trial_projection(db, run, snap, errors, warnings, *, service):
    """Read every governed source row into authoritative isolated SQL tables."""
    from app.ontologies.mappings.source_batches import prepare_mapping_source
    from app.ontologies.mappings.models import OntologyMapping
    from app.ontologies.mappings.formal_projection import (
        _coerce_props_to_type, stable_link_instance_id, stable_object_instance_id,
        stable_pipeline_entity_id, stable_pipeline_relation_id,
    )
    from app.ontologies.versions.models import OntologyTrialObject, OntologyTrialLink
    from app.ontologies.mappings.mapping_service import MappingService, MappingSourceError
    from app.ontologies.mappings.remote_binding import (
        BINDING_MODE_REMOTE,
        RemoteBindingError,
        snapshot_entry_binding,
        snapshot_link_binding_mode,
    )

    object_types = {str(item.get("id")): item for item in snap["objectTypes"]}
    link_types = {str(item.get("id")): item for item in snap["linkTypes"]}
    mappings, mapped_types, pinned = {}, set(), {}
    counts = {"objects": 0, "links": 0, "facts": 0}
    identity_service = MappingService(db)
    object_samples, link_samples = [], []
    objects = db.query(OntologyTrialObject).filter_by(trial_run_id=run.id)
    links = db.query(OntologyTrialLink).filter_by(trial_run_id=run.id)

    remote_endpoint, remote_checks, remote_datasets = (
        _verify_remote_object_mappings(
            db, run, snap, errors, warnings, object_types,
            sample_limit=20))

    def pin(dataset_id, dataset, source):
        version = source.version
        old = pinned.get(dataset_id)
        if old is not None and old["versionId"] != version.id:
            raise MappingSourceError("同一试跑的数据集版本不能变化")
        pinned[dataset_id] = {
            "datasetId": dataset_id, "datasetName": dataset.name,
            "versionId": version.id, "versionNo": version.version_no,
            "checksum": version.checksum, "rowCount": source.metadata["row_count"],
        }

    def source_for(dataset_id, mapping):
        dataset, version = service._latest_version(db, dataset_id)
        source = prepare_mapping_source(db, mapping, require_approved=True,
                                        version_id=version.id, batch_size=500)
        if dataset.kind == "integrated":
            # The frozen integrated port exposes native Python scalars (datetime,
            # Decimal, bytes). Trial objects/links and their SQL join index are
            # JSON/Text columns, so normalize exactly like the live mapping
            # materializer before coercing declared property types.
            from app.data_channel.connections.type_normalization import normalize_cell
            from app.ontologies.mappings.source_batches import MappingSourceBatches
            source = MappingSourceBatches(
                source.version, source.metadata,
                ([normalize_cell(row) for row in batch] for batch in source.batches),
            )
        pin(dataset_id, dataset, source)
        return dataset, source

    with trial_join_index(db) as index:
        for raw in snap["mappings"]:
            mapping_id = str(raw.get("id") or "")
            dataset_id = str(raw.get("curatedDatasetId") or "")
            try:
                entry_mode, _ = snapshot_entry_binding(raw)
            except RemoteBindingError as exc:
                errors.append(dict(code=exc.code, kind="mapping", id=mapping_id,
                                   name=raw.get("entityClass") or "",
                                   message=f"远程绑定描述符非法：{exc.message}",
                                   **exc.context))
                continue
            if entry_mode == BINDING_MODE_REMOTE:
                # 远程绑定已在 _verify_remote_object_mappings 验证，
                # 不读取本地数据版本、不产生隔离实例。
                remote_target = service._object_type_for_mapping(
                    raw, object_types)
                if remote_target is not None:
                    mapped_types.add(str(remote_target.get("id")))
                continue
            target = service._object_type_for_mapping(raw, object_types)
            if target is None:
                errors.append(dict(code="mapping_object_type_not_found", kind="mapping", id=mapping_id,
                                   name=raw.get("entityClass") or "", message="Mapping 绑定的 ObjectType 不存在"))
                continue
            if not dataset_id:
                errors.append(dict(code="mapping_dataset_missing", kind="mapping", id=mapping_id,
                                   name=raw.get("entityClass") or "", message="Mapping 未绑定数据集"))
                continue
            entity_class = str(raw.get("entityClass") or target.get("name") or "")
            target_id = str(target.get("id"))
            mapping = OntologyMapping(
                id=mapping_id or str(uuid.uuid4()), ontology_id=run.ontology_id,
                curated_dataset_id=dataset_id, entity_class=entity_class,
                target_object_type_id=target_id, field_mapping=service.json_safe(raw.get("fieldMapping") or {}),
                status=str(raw.get("status") or "draft"), confidence=raw.get("confidence"),
            )
            try:
                dataset, source = source_for(dataset_id, mapping)
                field_map = raw.get("fieldMapping") or {}
                pk = field_map.get("__primary_key__") or ",".join(
                    source.metadata.get("primary_key_columns") or [])
                if not service._pk_columns(pk):
                    errors.append(dict(code="mapping_primary_key_missing", kind="mapping", id=mapping_id,
                                       name=entity_class, message="数据集没有稳定主键，无法证明对象身份"))
                    continue
                scope = mapping.id
                endpoint_columns = set()
                for link_mapping in snap["linkMappings"]:
                    link_type = link_types.get(str(link_mapping.get("linkTypeId"))) or {}
                    if str(link_mapping.get("srcDatasetId")) == dataset_id and str(link_type.get("sourceObjectTypeId")) == target_id:
                        endpoint_columns.update(service._pk_columns(pk) if link_mapping.get("edgeDatasetId")
                                                else [str(link_mapping.get("srcKey") or "")])
                    if str(link_mapping.get("tgtDatasetId")) == dataset_id and str(link_type.get("targetObjectTypeId")) == target_id:
                        endpoint_columns.update(service._pk_columns(pk) if link_mapping.get("edgeDatasetId")
                                                else [str(link_mapping.get("tgtKey") or "")])
                mappings[(dataset_id, target_id)] = dict(scope=scope, primaryKey=pk, count=0)
                mapped_types.add(target_id)
                ordinal = 0
                for batch in source.batches:
                    prepared = []
                    for row in batch:
                        if not identity_service._has_complete_pk(row, str(pk)):
                            errors.append(dict(code="mapping_primary_key_value_missing", kind="objectInstance",
                                               id=f"{mapping_id}:{ordinal}", name=entity_class,
                                               message="数据行主键为空，无法生成稳定对象身份"))
                            ordinal += 1
                            continue
                        ordinal += 1
                        identity = identity_service._row_identity_value(row, str(pk))
                        entity_id = stable_pipeline_entity_id(run.ontology_id, entity_class, identity)
                        object_id = stable_object_instance_id(run.ontology_id, entity_id)
                        props = {str(name): row[column] for column, name in field_map.items()
                                 if not str(column).startswith("__") and column in row}
                        try:
                            props = _coerce_props_to_type(props, list(target.get("properties") or []))
                        except ValueError as exc:
                            errors.append(dict(code="mapping_property_coercion_failed", kind="objectInstance",
                                               id=object_id, name=entity_class, message=str(exc)))
                        prepared.append((row, object_id, entity_id, props))
                    existing = {item[0] for item in objects.with_entities(OntologyTrialObject.object_id).filter(
                        OntologyTrialObject.object_id.in_([item[1] for item in prepared]),
                    ).all()} if prepared else set()
                    unique = []
                    for row, object_id, entity_id, props in prepared:
                        if object_id in existing:
                            errors.append(dict(code="duplicate_primary_key", kind="objectInstance", id=object_id,
                                               name=entity_class, message="数据集主键重复，多个数据行会覆盖同一对象"))
                            continue
                        existing.add(object_id)
                        unique.append((row, object_id, entity_id))
                        db.add(OntologyTrialObject(
                            trial_run_id=run.id, object_id=object_id, object_type_id=target_id,
                            properties=props, computed={}, source_dataset_id=dataset_id,
                            source_dataset_version_id=source.version.id, external_id=entity_id,
                        ))
                        counts["objects"] += 1
                        counts["facts"] += len(props)
                        mappings[(dataset_id, target_id)]["count"] += 1
                        if len(object_samples) < 10:
                            object_samples.append(dict(objectId=object_id, objectTypeId=target_id, properties=props,
                                                       sourceDatasetId=dataset_id, sourceDatasetVersionId=source.version.id,
                                                       externalId=entity_id))
                    db.flush()
                    index.add_endpoints(scope, unique, endpoint_columns)
            except Exception as exc:
                errors.append(dict(code="mapping_source_unavailable", kind="mapping", id=mapping_id,
                                   name=entity_class, message=str(exc)))

        for object_type in snap["objectTypes"]:
            if str(object_type.get("id")) not in mapped_types:
                warnings.append(dict(code="object_type_unmapped", kind="objectType", id=object_type.get("id"),
                                     message="该对象类型没有数据映射，试跑中不会产生实例"))

        for raw in snap["linkMappings"]:
            link_type_id = str(raw.get("linkTypeId") or "")
            link_type = link_types.get(link_type_id)
            relation_type = str(raw.get("relationType") or (link_type or {}).get("name") or "")
            mapping_id = str(raw.get("id") or "")
            try:
                link_mode = snapshot_link_binding_mode(raw)
            except RemoteBindingError as exc:
                errors.append(dict(code=exc.code, kind="linkMapping",
                                   id=mapping_id, name=relation_type,
                                   message=f"远程关系绑定模式非法：{exc.message}",
                                   **exc.context))
                continue
            if link_mode == BINDING_MODE_REMOTE:
                if link_type is None:
                    errors.append(dict(code="link_mapping_type_not_found",
                                       kind="linkMapping", id=mapping_id,
                                       name=relation_type,
                                       message="远程 LinkMapping 绑定的 LinkType 不存在"))
                    continue
                if raw.get("edgeDatasetId"):
                    errors.append(dict(code="remote_link_edge_forbidden",
                                       kind="linkMapping", id=mapping_id,
                                       name=relation_type,
                                       message="远程关系定义暂不支持连接表（胖边）"))
                    continue
                endpoint_ok = True
                for role, dataset_field, key_field, type_field in (
                        ("source", "srcDatasetId", "srcKey", "sourceObjectTypeId"),
                        ("target", "tgtDatasetId", "tgtKey", "targetObjectTypeId")):
                    endpoint = remote_endpoint.get(
                        (str(raw.get(dataset_field) or ""),
                         str(link_type.get(type_field) or "")))
                    if endpoint is None:
                        errors.append(dict(
                            code="remote_link_endpoint_mapping_required",
                            kind="linkMapping", id=mapping_id, name=relation_type,
                            message=f"远程关系 {role} 数据集未被匹配类型的远程对象映射绑定"))
                        endpoint_ok = False
                        continue
                    columns = {
                        str(column.get("name"))
                        for column in endpoint.get("columns") or []
                        if isinstance(column, dict)
                    }
                    key = str(raw.get(key_field) or "")
                    if not key or key not in columns:
                        errors.append(dict(
                            code="remote_link_key_missing", kind="linkMapping",
                            id=mapping_id, name=relation_type,
                            message=f"远程关系 {role} 端键不在冻结列清单中"))
                        endpoint_ok = False
                if endpoint_ok:
                    remote_checks[mapping_id] = {
                        "kind": "linkMapping",
                        "status": "verified",
                        "materialized": False,
                        "srcDatasetId": str(raw.get("srcDatasetId") or ""),
                        "tgtDatasetId": str(raw.get("tgtDatasetId") or ""),
                        "srcKey": str(raw.get("srcKey") or ""),
                        "tgtKey": str(raw.get("tgtKey") or ""),
                    }
                    warnings.append(dict(
                        code="remote_binding_not_materialized", kind="linkMapping",
                        id=mapping_id, name=relation_type,
                        message=(
                            f"LinkMapping「{relation_type}」为远程关系定义："
                            "试跑仅验证冻结端点契约，未物化边实例，"
                            "不计入关系计数")))
                continue
            remote_refs = [
                field for field in ("srcDatasetId", "tgtDatasetId", "edgeDatasetId")
                if str(raw.get(field) or "") in remote_datasets
            ]
            if remote_refs:
                errors.append(dict(
                    code="link_dataset_remote_bound", kind="linkMapping",
                    id=mapping_id, name=relation_type,
                    message=(
                        f"LinkMapping「{relation_type}」引用远程绑定数据集"
                        f"（{remote_refs}），本地关系物化不可用；"
                        "请改为远程关系定义")))
                continue
            if link_type is None:
                errors.append(dict(code="link_mapping_type_not_found", kind="linkMapping", id=mapping_id,
                                   name=relation_type, message="LinkMapping 绑定的 LinkType 不存在"))
                continue
            source = mappings.get((str(raw.get("srcDatasetId") or ""), str(link_type.get("sourceObjectTypeId") or "")))
            target = mappings.get((str(raw.get("tgtDatasetId") or ""), str(link_type.get("targetObjectTypeId") or "")))
            if source is None or target is None:
                errors.append(dict(code="link_mapping_endpoint_mapping_missing", kind="linkMapping", id=mapping_id,
                                   name=relation_type, message="关系两端必须分别存在与对象类型匹配的数据映射"))
                continue
            try:
                edge_scope, edge_pk = None, None
                source_column, target_column = str(raw.get("srcKey") or ""), str(raw.get("tgtKey") or "")
                if raw.get("edgeDatasetId"):
                    for endpoint in (source, target):
                        if endpoint["count"] and len(service._pk_columns(endpoint["primaryKey"])) != 1:
                            raise MappingSourceError("连接表暂不支持复合主键端点")
                    transient = OntologyMapping(id=f"edge:{mapping_id}", ontology_id=run.ontology_id,
                                                curated_dataset_id=str(raw["edgeDatasetId"]),
                                                entity_class="__edge__", field_mapping={}, status="draft")
                    dataset, edge_source = source_for(str(raw["edgeDatasetId"]), transient)
                    edge_scope = transient.id
                    # The sealed version's ordered PK is authoritative. A unique
                    # single column or row hash must not replace a composite key.
                    edge_pk = ",".join(edge_source.metadata.get("primary_key_columns") or [])
                    if dataset.kind == "integrated" and not edge_pk:
                        raise MappingSourceError("Edge dataset has no canonical primary key")
                    columns, edge_count = [], 0
                    for batch in edge_source.batches:
                        if edge_pk and any(not identity_service._has_complete_pk(row, edge_pk) for row in batch):
                            raise MappingSourceError("Edge dataset primary key value is missing")
                        if not columns and batch:
                            columns = list(batch[0])
                        edge_count += len(batch)
                        index.add_edges(edge_scope, batch, source_column, target_column,
                                        raw.get("fieldMapping") or {}, collect_identity_keys=not edge_pk)
                    if not edge_pk:
                        edge_pk = index.choose_edge_primary_key(edge_scope, columns, edge_count)
                    source_column = next(iter(service._pk_columns(source["primaryKey"])), "")
                    target_column = next(iter(service._pk_columns(target["primaryKey"])), "")
                for batch in index.pairs(source["scope"], target["scope"], source_column, target_column, edge_scope=edge_scope):
                    prepared = []
                    for pair in batch:
                        source_id, source_entity, target_id, target_entity = pair[:4]
                        edge_key = identity_service._row_identity_value(pair[4], edge_pk) if edge_scope else ""
                        props = pair[5] if edge_scope else {}
                        link_id = stable_link_instance_id(run.ontology_id, link_type_id, source_id, target_id, edge_key)
                        try:
                            props = _coerce_props_to_type(props, list(link_type.get("properties") or []))
                        except ValueError as exc:
                            errors.append(dict(code="link_mapping_property_coercion_failed", kind="linkInstance",
                                               id=link_id, name=relation_type, message=str(exc)))
                            continue
                        relation_id = stable_pipeline_relation_id(
                            run.ontology_id, source_entity, target_entity, relation_type,
                            f"link_mapping:{mapping_id}:{edge_key}" if edge_key else f"link_mapping:{mapping_id}",
                        )
                        prepared.append(dict(linkId=link_id, linkTypeId=link_type_id, sourceObjectId=source_id,
                                             targetObjectId=target_id, properties=props, sourceRelationId=relation_id))
                    existing = {item[0] for item in links.with_entities(OntologyTrialLink.link_id).filter(
                        OntologyTrialLink.link_id.in_([item["linkId"] for item in prepared]),
                    ).all()} if prepared else set()
                    for item in prepared:
                        if item["linkId"] in existing:
                            errors.append(dict(code="duplicate_link", kind="linkInstance", id=item["linkId"],
                                               name=relation_type, message="相同身份的链接重复"))
                            continue
                        existing.add(item["linkId"])
                        db.add(OntologyTrialLink(
                            trial_run_id=run.id, link_id=item["linkId"], link_type_id=link_type_id,
                            source_object_id=item["sourceObjectId"], target_object_id=item["targetObjectId"],
                            properties=item["properties"], source_relation_id=item["sourceRelationId"],
                        ))
                        counts["links"] += 1
                        if len(link_samples) < 10:
                            link_samples.append(item)
                    db.flush()
            except Exception as exc:
                errors.append(dict(code="link_mapping_source_unavailable", kind="linkMapping", id=mapping_id,
                                   name=relation_type, message=str(exc)))
    return dict(objects=objects, links=links, counts=counts, pinned=pinned,
                samples=dict(objects=object_samples, links=link_samples),
                remote=remote_checks)
