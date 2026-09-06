"""Mapping identity, primary-key, display-name and property metadata rules."""
from __future__ import annotations

import json
import re
import uuid as _uuid

from app.ontologies.mappings.models import OntologyMapping
from app.ontologies.mappings.errors import MappingSourceError


class IdentityMetadataMixin:
    """Cohesive identity and metadata behavior exposed through MappingService."""

    def _normalize_mapping(self, mapping: OntologyMapping, rows: list[dict]) -> None:
        if not rows:
            return
        sample = rows[0]
        field_map = dict(mapping.field_mapping or {})
        ignored_fields = {
            str(item) for item in (field_map.get("__ignored_fields__") or [])
        }
        targets = [
            str(value) for key, value in field_map.items()
            if not str(key).startswith("__") and str(key) not in ignored_fields
        ]
        duplicate_targets = sorted({
            value for value in targets if targets.count(value) > 1
        })
        if duplicate_targets:
            raise MappingSourceError(
                "历史字段映射存在多个源列写入同一目标属性，已阻断静默覆盖："
                + "、".join(duplicate_targets))
        for col in sample.keys():
            if col == "content":
                continue
            if col not in field_map and col not in ignored_fields:
                field_map[col] = col
        # Entity.id/name/source_id are runtime-owned fields.  Mapping a business
        # column onto one of them used to overwrite the value and then made the
        # Formal layer guess another primary key.  Preserve the business value
        # under an explicit, non-reserved property instead.
        reserved_outputs = {
            "id", "ontology_id", "source_id", "object_type",
            "source_row_count", "name", "name_cn", "name_en",
            "display_name", "__mapping_ids__",
        }
        declared_target_properties: set[str] = set()
        if mapping.target_object_type_id:
            from app.models.ontology_formal import ObjectType
            target_type = self._db.query(ObjectType).filter(
                ObjectType.id == mapping.target_object_type_id,
                ObjectType.ontology_id == mapping.ontology_id,
            ).first()
            declared_target_properties = {
                str(item.get("name"))
                for item in (getattr(target_type, "properties", None) or [])
                if isinstance(item, dict) and item.get("name")
            }
        occupied = {
            str(prop) for source, prop in field_map.items()
            if not str(source).startswith("__") and str(prop) not in reserved_outputs
        }
        for col in sample.keys():
            prop = str(field_map.get(col) or col)
            if prop not in reserved_outputs or prop in declared_target_properties:
                continue
            candidate = "business_id" if prop == "id" else f"business_{prop}"
            if candidate in occupied:
                candidate = f"source_{re.sub(r'[^A-Za-z0-9_]', '_', str(col))}_value"
            field_map[col] = candidate
            occupied.add(candidate)
        canonical_dataset, declared = self._dataset_primary_key(
            mapping.curated_dataset_id)
        if canonical_dataset:
            if not declared:
                raise MappingSourceError(
                    f"映射数据集 {mapping.curated_dataset_id} 尚未声明资产湖主键契约")
            missing = [col for col in self._pk_columns(declared) if col not in sample]
            if missing:
                raise MappingSourceError(
                    f"映射数据缺少资产湖主键列：{', '.join(missing)}")
            if not self._is_unique_key(rows, declared):
                raise MappingSourceError(
                    f"映射数据违反资产湖主键 {declared}：存在空值或重复值")
            # Canonical datasets have exactly one identity authority.  Repair
            # historical mappings that once carried an independently selected key.
            field_map["__primary_key__"] = declared
            field_map["__pk_source__"] = "lake"

        pk_col = field_map.get("__primary_key__")
        order_pk_can_merge = (
            not canonical_dataset
            and bool(pk_col)
            and pk_col != "__row_hash__"
            and all(col in sample for col in self._pk_columns(pk_col))
            and ("order" in (mapping.entity_class or "").lower() or "订单" in str(mapping.entity_class or ""))
            and all(self._has_complete_pk(row, pk_col) for row in rows)
        )
        if (
            not canonical_dataset
            and (
            not pk_col
            or (
                pk_col != "__row_hash__"
                and not order_pk_can_merge
                and (not all(col in sample for col in self._pk_columns(pk_col))
                     or not self._is_unique_key(rows, pk_col))
            )
            )
        ):
            # 主键优先级：湖中声明的契约（入湖闸门校验过存在/非空/唯一）
            # > 按本次数据启发式猜测。声明来源打进 __pk_source__ 供下游区分
            field_map["__primary_key__"] = declared or self._choose_pk_col(rows)
            field_map["__pk_source__"] = "lake" if declared else "inferred"
        field_map["__properties__"] = self._property_metadata(rows, field_map)
        if field_map != (mapping.field_mapping or {}):
            mapping.field_mapping = field_map
            self._db.flush()

    def _property_metadata(self, rows: list[dict], field_map: dict) -> list[dict]:
        if not rows:
            return []
        sample = rows[0]
        existing = {
            item.get("column"): item
            for item in field_map.get("__properties__", [])
            if isinstance(item, dict) and item.get("column")
        }
        technical_cols = {
            "content", "storage_uri", "markdown_text", "structured_extraction_error",
            "structured_extraction_ok", "extraction_strategy", "extraction_method",
            "source_dataset_id",
        }
        result = []
        pk_columns = self._pk_columns(field_map.get("__primary_key__"))
        ignored_fields = {
            str(item) for item in (field_map.get("__ignored_fields__") or [])
        }
        for col in sample.keys():
            if col == "content":
                continue
            if col in ignored_fields and col not in pk_columns:
                continue
            current = dict(existing.get(col) or {})
            pk_part = pk_columns.index(col) + 1 if col in pk_columns else None
            result.append({
                "column": col,
                "property": field_map.get(col, col),
                "type": current.get("type") or self._infer_property_type(rows, col),
                # Identity fields must remain projected and inspectable.  Hiding a
                # PK component would make the Formal type unable to represent its
                # own canonical identity contract.
                "hidden": False if pk_part else bool(
                    current.get("hidden", col in technical_cols or col.startswith("__"))),
                "technical": bool(current.get("technical", col in technical_cols or col.startswith("__"))),
                "primaryKeyPart": pk_part,
                "confidence": float(current.get("confidence", 0.85)),
                "description": current.get("description", ""),
            })
        return result

    def _property_metadata_by_column(self, field_map: dict) -> dict:
        return {
            item.get("column"): item
            for item in (field_map or {}).get("__properties__", [])
            if isinstance(item, dict) and item.get("column")
        }

    def _infer_property_type(self, rows: list[dict], col: str) -> str:
        from app.data_channel.pipelines.steps.schema_inference import SchemaInferenceStep

        for row in rows[:20]:
            value = row.get(col)
            if value not in (None, ""):
                return SchemaInferenceStep._infer_type(str(value).strip())
        return "string"

    def _dataset_primary_key(self, dataset_id: str | None) -> tuple[bool, str | None]:
        """Return ``(is_canonical_dataset, canonical_pk_spec)``.

        ``isinstance`` deliberately excludes permissive MagicMock/legacy rows:
        only v2_datasets is the contract authority.
        """
        if not dataset_id:
            return False, None
        from app.data_channel.datasets.lake_gate import split_pk
        from app.data_channel.datasets.models import Dataset

        dataset = self._db.query(Dataset).filter(Dataset.id == dataset_id).first()
        if not isinstance(dataset, Dataset):
            return False, None
        schema = dataset.schema_json if isinstance(dataset.schema_json, dict) else {}
        columns = split_pk(schema.get("primary_key"))
        return True, ",".join(columns) or None

    def _declared_pk_col(self, dataset_id: str | None, rows: list[dict]) -> str | None:
        """资产湖中该数据集声明的主键列（schema_json.primary_key，入湖闸门维护）。

        实例身份优先采用湖中契约而非按本次加载的数据猜测——启发式对数据
        敏感（今天唯一的列明天未必），主键一漂移就是一整批实例身份作废。
        复合主键保留规范化后的逗号分隔形式，由 identity 编码为稳定 JSON。
        """
        canonical, pk = self._dataset_primary_key(dataset_id)
        if not canonical or not pk:
            return None
        if rows:
            missing = [col for col in self._pk_columns(pk) if col not in rows[0]]
            if missing:
                raise MappingSourceError(
                    f"数据集 {dataset_id} 声明的主键列不在当前数据中：{', '.join(missing)}")
        return pk

    def _choose_pk_col(self, rows: list[dict]) -> str:
        if not rows:
            return "id"
        cols = [c for c in rows[0].keys() if c != "content"]

        id_like_cols = []
        for col in cols:
            lower = col.lower()
            if lower == "id" or lower.endswith("_id") or col.endswith("ID") or "id" in lower:
                id_like_cols.append(col)
        for col in id_like_cols:
            if self._is_unique_col(rows, col):
                return col
        for preferred in ("filename", "source_file", "source_dataset_id", "order_id", "订单号", "供应商ID"):
            if preferred in cols and self._is_unique_col(rows, preferred):
                return preferred
        for col in cols:
            if self._is_unique_col(rows, col):
                return col
        return "__row_hash__"

    def _is_unique_col(self, rows: list[dict], col: str) -> bool:
        values = [str(row.get(col, "")).strip() for row in rows]
        return bool(values) and all(values) and len(set(values)) == len(values)

    @staticmethod
    def _pk_columns(pk_col: str | None) -> list[str]:
        if not pk_col or pk_col == "__row_hash__":
            return []
        return [part.strip() for part in str(pk_col).split(",") if part.strip()]

    def _has_complete_pk(self, row: dict, pk_col: str | None) -> bool:
        columns = self._pk_columns(pk_col)
        return bool(columns) and all(
            col in row and self._has_display_value(row.get(col)) for col in columns)

    def _is_unique_key(self, rows: list[dict], pk_col: str) -> bool:
        columns = self._pk_columns(pk_col)
        if not rows or not columns:
            return False
        values: list[tuple[str, ...]] = []
        for row in rows:
            if not self._has_complete_pk(row, pk_col):
                return False
            values.append(tuple(str(row.get(col)) for col in columns))
        return len(set(values)) == len(values)

    def _row_hash(self, row: dict) -> str:
        return json.dumps(
            {k: v for k, v in row.items() if k != "content"},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

    @staticmethod
    def _normalize_fk_value(value: str) -> str:
        """FK 值归一化: 大写并去掉分隔符，容错 SUP-001 / sup_001 / SUP001 等格式差异"""
        import re
        return re.sub(r'[\s\-_]', '', str(value)).upper()

    def _row_identity_value(self, row: dict, pk_col: str | None) -> str:
        columns = self._pk_columns(pk_col)
        if columns and self._has_complete_pk(row, pk_col):
            if len(columns) == 1:
                # Preserve IDs already materialized for single-column keys.
                # f-string 的隐式文本化让 int/str 主键值产生同一身份，
                # 这是跨源 join 与主键文本化（type_normalization）的口径基础。
                return f"{columns[0]}:{row.get(columns[0])}"
            # 复合主键的分值显式文本化：湖侧主键文本化（PR-B）之后原生
            # int 与文本 "1" 不能再产生两套身份；与 _lookup_identity_value
            # 的 join 侧序列化保持同一形态。
            payload = {
                "columns": columns,
                "values": [str(row.get(col)) for col in columns],
            }
            return "composite_pk:" + json.dumps(
                payload, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), default=str)
        return f"row_hash:{self._row_hash(row)}"

    def _lookup_identity_value(self, pk_col: str | None, value: str) -> str:
        columns = self._pk_columns(pk_col)
        if len(columns) == 1:
            return f"{columns[0]}:{value}"
        if len(columns) > 1 and isinstance(value, (list, tuple)) and len(value) == len(columns):
            return "composite_pk:" + json.dumps(
                {"columns": columns, "values": [str(item) for item in value]},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                default=str)
        return value

    def _stable_row_id(self, mapping: OntologyMapping, row: dict, pk_col: str | None) -> str:
        identity = self._row_identity_value(row, pk_col)
        return str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"{mapping.ontology_id}:{mapping.entity_class}:{identity}"))

    def _stable_relation_id(self, ontology_id: str, src_eid: str, tgt_eid: str, rel_type: str, source: str) -> str:
        return str(_uuid.uuid5(
            _uuid.NAMESPACE_URL,
            f"{ontology_id}:{src_eid}:{rel_type}:{tgt_eid}:{source}",
        ))

    def _infer_cardinality(self, source_ids: list[str], target_ids: list[str]) -> str:
        if not source_ids or not target_ids:
            return "unknown"
        source_unique = len(set(source_ids)) == len(source_ids)
        target_unique = len(set(target_ids)) == len(target_ids)
        if source_unique and target_unique:
            return "one_to_one"
        if source_unique and not target_unique:
            return "many_to_one"
        if not source_unique and target_unique:
            return "one_to_many"
        return "many_to_many"

    def _has_display_value(self, value) -> bool:
        if value in (None, ""):
            return False
        try:
            return value == value
        except Exception:
            return True

    def _join_display_parts(self, row: dict, cols: tuple[str, ...], min_parts: int = 1) -> str | None:
        parts = []
        for col in cols:
            value = row.get(col)
            if self._has_display_value(value):
                parts.append(str(value))
        if len(parts) >= min_parts:
            return " / ".join(parts)
        return None

    def _display_name(self, mapping: OntologyMapping, row: dict, pk_col: str | None, index: int) -> str:
        row_type = str(row.get("row_type") or "")
        source_file = row.get("source_file") or row.get("filename")
        if row_type == "rule":
            condition = str(row.get("condition") or "").strip()
            suffix = f" #{row.get('rule_index')}" if row.get("rule_index") not in (None, "") else ""
            return f"{source_file or mapping.entity_class} / Rule{suffix}: {condition[:80]}".strip()
        if row_type == "table_row":
            section = row.get("section_title") or "Table"
            table_idx = row.get("table_index") or 1
            row_idx = row.get("table_row_index") or index + 1
            return f"{source_file or mapping.entity_class} / {section} / T{table_idx}R{row_idx}"
        if row_type == "section":
            section = row.get("section_title") or "Section"
            section_idx = row.get("section_index") or index + 1
            return f"{source_file or mapping.entity_class} / {section} #{section_idx}"
        if row.get("record_id") not in (None, "") and pk_col == "__row_hash__":
            return str(row.get("record_id"))

        if len(self._pk_columns(pk_col)) > 1 and self._has_complete_pk(row, pk_col):
            return " / ".join(
                f"{col}={row.get(col)}" for col in self._pk_columns(pk_col))

        entity_class_lower = (mapping.entity_class or "").lower()
        if "order" in entity_class_lower or "订单" in str(mapping.entity_class or ""):
            order_label = self._join_display_parts(row, ("order_id", "order_name"), min_parts=1)
            if order_label:
                return order_label
            order_label = self._join_display_parts(row, ("订单号", "订单名称"), min_parts=1)
            if order_label:
                return order_label

        for cols in (
            ("order_id", "items.sku"),
            ("order_id", "items.name"),
            ("订单号", "物料编码"),
        ):
            label = self._join_display_parts(row, cols, min_parts=2)
            if label:
                return label

        inventory_label = self._join_display_parts(row, ("日期", "物料编码", "操作类型", "所在仓库"), min_parts=3)
        if inventory_label:
            return f"{inventory_label} #{index + 1}"

        supplier_label = self._join_display_parts(row, ("供应商ID", "供应商名称"), min_parts=2)
        if supplier_label:
            return supplier_label

        if self._has_complete_pk(row, pk_col):
            col = self._pk_columns(pk_col)[0]
            return str(row.get(col))

        candidates = []
        for col in row.keys():
            lower = col.lower()
            if any(token in lower for token in ("name", "title")) or any(token in col for token in ("名称", "标题", "文件名")):
                candidates.append(col)
        for col in (
            pk_col,
            "order_id", "订单号", "运单号", "供应商ID", "物料编码",
            "filename", "source_file",
        ):
            if col and col != "__row_hash__" and col in row:
                candidates.append(col)
        for col in candidates:
            value = row.get(col)
            if value not in (None, ""):
                return str(value)
        return f"{mapping.entity_class} #{index + 1}"

    def _first_value(self, row: dict, cols: list[str]) -> str | None:
        for col in cols:
            if not col or col == "__row_hash__" or col not in row:
                continue
            value = row.get(col)
            if self._has_display_value(value):
                return str(value)
        return None

    def _identity_columns(self, row: dict, pk_col: str | None) -> list[str]:
        cols = list(row.keys())
        result: list[str] = []
        result.extend(self._pk_columns(pk_col))
        for col in cols:
            lower = col.lower()
            if (
                lower == "id"
                or lower.endswith("_id")
                or col.endswith("ID")
                or "编号" in col
                or "编码" in col
                or col in ("订单号", "运单号", "单号")
            ):
                result.append(col)
        return list(dict.fromkeys(result))

    def _name_columns(self, row: dict) -> list[str]:
        result: list[str] = []
        for col in row.keys():
            lower = col.lower()
            if any(token in lower for token in ("name", "title")) or any(token in col for token in ("名称", "姓名", "标题")):
                result.append(col)
        return result

    def _has_cjk(self, value: str | None) -> bool:
        return bool(value and re.search(r"[\u4e00-\u9fff]", value))

    def _instance_names(self, mapping: OntologyMapping, row: dict, pk_col: str | None, index: int) -> dict[str, str]:
        """Return row-instance names. These must describe the record, not the schema/table."""
        display = self._display_name(mapping, row, pk_col, index)
        id_value = self._first_value(row, self._identity_columns(row, pk_col))
        name_value = self._first_value(row, self._name_columns(row))

        if not self._has_cjk(display):
            en = display
        elif id_value and name_value and id_value != name_value and not self._has_cjk(name_value):
            en = f"{id_value} / {name_value}"
        else:
            en = display

        return {
            "display_name": display,
            "name_cn": str(display)[:200],
            "name_en": str(en)[:200],
        }

    def _rows_to_entities(self, mapping: OntologyMapping, rows: list[dict]) -> list[dict]:
        field_map = mapping.field_mapping or {}
        pk_col = field_map.get("__primary_key__") or self._choose_pk_col(rows)
        property_meta = self._property_metadata_by_column(field_map)
        runtime_keys = {
            "id", "ontology_id", "source_id", "object_type", "source_row_count",
            "name", "name_cn", "name_en", "display_name", "__mapping_ids__",
        }
        entities_by_id: dict[str, dict] = {}
        for index, row in enumerate(rows):
            props: dict = {"ontology_id": mapping.ontology_id}
            business_runtime_values: dict = {}
            for col, prop in field_map.items():
                if col.startswith("__"):
                    continue
                if property_meta.get(col, {}).get("hidden"):
                    continue
                if col in row:
                    if prop in runtime_keys:
                        business_runtime_values[prop] = row[col]
                    else:
                        props[prop] = row[col]
            if len(self._pk_columns(pk_col)) > 1:
                # Formal ObjectType currently exposes one primary_key property.
                # Preserve every component as required business fields and add a
                # deterministic scalar identity solely for that compatibility
                # slot; instance IDs continue to derive from the same JSON value.
                props["__composite_identity__"] = self._row_identity_value(
                    row, pk_col)
            props["id"] = self._stable_row_id(
                mapping, row, pk_col if self._has_complete_pk(row, pk_col) else None)
            props["source_id"] = props["id"]
            props.update(self._instance_names(mapping, row, pk_col, index))
            props["name"] = props["name_cn"]
            props["object_type"] = mapping.entity_class
            # Ownership metadata powers source-of-truth reconciliation.  It is
            # reserved platform metadata and is not exposed as a business
            # ObjectType property by formal_projection.
            props["__mapping_ids__"] = [mapping.id]
            if business_runtime_values:
                # Entity's legacy envelope owns keys such as id/name.  Keep
                # explicitly mapped business properties separately so the
                # Formal projection can restore them without corrupting the
                # runtime identity/display metadata.
                props["__business_properties__"] = business_runtime_values
            if props["id"] in entities_by_id:
                existing = entities_by_id[props["id"]]
                existing["source_row_count"] = int(existing.get("source_row_count", 1)) + 1
                continue
            props["source_row_count"] = 1
            entities_by_id[props["id"]] = props
        return list(entities_by_id.values())
