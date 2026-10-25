"""Bounded source-to-Entity materialization; SQL staging owns current key sets."""
from __future__ import annotations

from app.data_channel.connections.type_normalization import normalize_cell
from app.ontologies.mappings.errors import MappingSourceError
from app.ontologies.mappings.source_batches import MappingSourceBatches
from app.ontologies.mappings.source_staging import MappingStage, MAPPING_BATCH_SIZE


def materialize_mapping(service, mapping, source, source_version, *, released, stages):
    """Consume every immutable batch with bounded Entity and SQL key writes."""
    from app.ontologies.mappings.formal_projection_contract import projection_property_mappings

    metadata = source.metadata if isinstance(source, MappingSourceBatches) else {}
    if isinstance(source, MappingSourceBatches):
        batches = source.batches
    else:
        # Keep the established loader/list patch seam and legacy API usable.
        batches = (source[start:start + MAPPING_BATCH_SIZE]
                   for start in range(0, len(source), MAPPING_BATCH_SIZE))
    stage = None
    sample = []
    count = 0
    pk_col = (mapping.field_mapping or {}).get("__primary_key__")
    if isinstance(source, MappingSourceBatches) and not pk_col:
        pk_col = ",".join(metadata.get("primary_key_columns") or []) or None
        if not pk_col:
            raise MappingSourceError("映射不可变版本缺少显式主键契约")
    from app.ontologies.mappings.raw_json_guard import structured_json_columns, reject_raw_json_columns
    structured_columns = structured_json_columns(service._db, mapping)
    service._adopt_legacy_projection_ownership(mapping)
    for batch in batches:
        reject_raw_json_columns(batch, structured_columns)
        if not sample:
            sample = list(batch)
            if not released:
                if isinstance(source, MappingSourceBatches):
                    service._normalize_mapping(mapping, sample, source_metadata=metadata)
                else:
                    service._normalize_mapping(mapping, sample)
                schema_types = {item["name"]: item.get("type")
                                for item in metadata.get("source_schema", [])}
                if schema_types:
                    field_map = dict(mapping.field_mapping or {})
                    field_map["__properties__"] = [
                        {**item, "type": schema_types.get(item["column"]) or item.get("type")}
                        for item in field_map.get("__properties__", [])]
                    mapping.field_mapping = field_map
            if not isinstance(source, MappingSourceBatches):
                pk_col = (mapping.field_mapping or {}).get("__primary_key__") or service._choose_pk_col(sample)
            stage = MappingStage(service._db, metadata.get("columns") or list(batch[0]))
            stages.append(stage)
        batch = [normalize_cell(row) for row in batch]
        if isinstance(source, MappingSourceBatches):
            if any(not service._has_complete_pk(row, pk_col) for row in batch):
                raise MappingSourceError("映射数据行缺少规范主键分值，已拒绝回退行哈希")
            entities = service._rows_to_entities(mapping, batch, pk_col=pk_col)
        else:
            entities = service._rows_to_entities(mapping, batch)
        # Canonical sealed versions enforce unique complete PKs. The SQL stage
        # retains every row even when an older noncanonical mapping merges IDs.
        stage.append(batch, [service._stable_row_id(
            mapping, row, pk_col if service._has_complete_pk(row, pk_col) else None)
            for row in batch])
        service._write_v1_entities(mapping, entities)
        count += len(entities)
    if stage is None:
        stage = MappingStage(service._db, metadata.get("columns") or [])
        stages.append(stage)
    stale_count = service._reconcile_mapping_entities(
        mapping, stage.entity_ids,
        source_dataset_version_id=getattr(source_version, "id", None),
        collect_ids=False)
    # Only bounded discovery samples remain in memory; all executable joins use
    # stage's complete relation, never this preview.
    return {
        "mapping_id": mapping.id, "curated_dataset_id": mapping.curated_dataset_id,
        "source_dataset_version_id": getattr(source_version, "id", None),
        "entity_class": mapping.entity_class, "pk_col": pk_col,
        "pk_source": (mapping.field_mapping or {}).get("__pk_source__"),
        "target_object_type_id": mapping.target_object_type_id,
        "rows": sample, "row_count": stage.count, "stage": stage,
        "entity_id_map": {service._row_identity_value(row, pk_col): service._stable_row_id(
            mapping, row, pk_col if service._has_complete_pk(row, pk_col) else None) for row in sample},
        "columns": stage.columns,
        "property_mappings": projection_property_mappings(mapping.field_mapping),
        "source_schema": metadata.get("source_schema", []),
    }, count, stale_count
