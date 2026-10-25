"""Relational FK inference and explicit LinkMapping projection behavior."""
from __future__ import annotations

import logging

from sqlalchemy.orm.attributes import flag_modified

from app.ontologies.mappings.models import OntologyMapping
from app.ontologies.mappings.errors import MappingApplyError, MappingSourceError

logger = logging.getLogger(__name__)


class RelationProcessingMixin:
    """Relation inference and LinkMapping processing used by MappingService."""

    def _infer_and_write_relations(self, ontology_id: str, mappings: list[OntologyMapping],
                                   mapping_meta: dict) -> list[dict]:
        from app.ontologies.mappings.sql_fk_inference import infer_relations
        return infer_relations(self, ontology_id, mappings, mapping_meta)

    def _detect_alt_key_columns(self, rows: list[dict], pk_col: str | None) -> list[str]:
        """检测备用键: 近唯一(≥90%)、非纯数字、值长度足够的非主键列 (如 供应商名称)"""
        import re
        if not rows:
            return []
        alt: list[str] = []
        # 小样本中 status/type 等枚举很容易“碰巧唯一”（两行 approved/pending），
        # 但它们不是实体身份键；拿它们跨表连边会生成语义错误且违反基数。
        non_identity_tokens = {
            "status", "state", "type", "category", "level", "rating", "stage"}
        non_identity_cjk = {"状态", "类型", "类别", "分类", "等级", "评级", "阶段", "标志", "是否"}
        for col in rows[0].keys():
            if col == pk_col:
                continue
            normalized_col = str(col).strip().lower().replace("_", " ")
            col_tokens = set(re.findall(r"[a-z0-9]+", normalized_col))
            if (col_tokens & non_identity_tokens
                    or any(token in normalized_col for token in non_identity_cjk)):
                continue
            vals = [str(r.get(col, "") or "").strip() for r in rows]
            vals = [v for v in vals if v]
            if len(vals) < 2:
                continue
            if all(re.fullmatch(r"[\d\s\.,%\-]+", v) for v in vals):
                continue  # 纯数字列(金额/比率)易误连
            if sum(len(v) >= 3 for v in vals) / len(vals) < 0.8:
                continue  # 短值枚举列(等级/状态)
            if len(set(vals)) / len(vals) < 0.9:
                continue
            alt.append(col)
        return alt

    def _infer_alt_key_relations(self, ontology_id: str, src_m: OntologyMapping, src_meta: dict,
                                 tgt_m: OntologyMapping, tgt_meta: dict,
                                 skip_src_cols: set[str], link_model) -> list[dict]:
        """源列值(支持逗号等分隔的多值)与目标备用键值重叠 → 推断关系。

        典型场景: 文档记录的 organizations 字段命中 Supplier.供应商名称。
        """
        import re
        from app.models.entity import Entity
        from app.models.relation import Relation

        tgt_rows = tgt_meta.get("rows", [])
        tgt_pk_col = tgt_meta["pk_col"]
        tgt_id_map = tgt_meta["entity_id_map"]
        tgt_class = tgt_meta["entity_class"]
        results: list[dict] = []

        # 跳过文档级元数据列与常量列: 全行同值的列不携带行级链接信息,
        # 否则文档摘要里提到的公司会让该文档所有行都连过去
        meta_cols = {"doc_summary", "sections", "source_file", "filename",
                     "markdown_text", "document_text", "extraction_method"}
        src_rows = src_meta.get("rows", [])
        usable_src_cols = []
        for col in src_meta["columns"]:
            if col in meta_cols or col == src_meta["pk_col"]:
                continue
            vals = {str(r.get(col, "") or "").strip() for r in src_rows}
            vals.discard("")
            if len(vals) <= 1:
                continue
            usable_src_cols.append(col)

        rel_name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", tgt_class).upper()
        rel_name = re.sub(r"[^A-Z0-9_]", "", rel_name) or "REF"
        rel_type = f"HAS_{rel_name}"

        for alt_col in self._detect_alt_key_columns(tgt_rows, tgt_pk_col):
            alt_to_eid: dict[str, str] = {}
            for row in tgt_rows:
                v = str(row.get(alt_col, "") or "").strip()
                if not v:
                    continue
                eid = tgt_id_map.get(self._row_identity_value(row, tgt_pk_col))
                if eid:
                    alt_to_eid[self._normalize_fk_value(v)] = eid
            if len(alt_to_eid) < 2:
                continue

            col_pairs: dict[str, set[tuple[str, str]]] = {}
            for row in src_meta["rows"]:
                src_eid = src_meta["entity_id_map"].get(
                    self._row_identity_value(row, src_meta["pk_col"]))
                if not src_eid:
                    continue
                for col in usable_src_cols:
                    if col in skip_src_cols:
                        continue
                    raw = str(row.get(col, "") or "").strip()
                    if not raw:
                        continue
                    parts = [p.strip() for p in re.split(r"[,，、;；|]", raw) if p.strip()]
                    for part in parts:
                        norm = self._normalize_fk_value(part)
                        if len(norm) < 3:
                            continue
                        tgt_eid = alt_to_eid.get(norm)
                        if tgt_eid:
                            col_pairs.setdefault(col, set()).add((src_eid, tgt_eid))

            for col, pairs in col_pairs.items():
                if not pairs:
                    continue  # 全名精确匹配(归一化≥3字符)误连概率低, 单行命中即视为有效
                written = 0
                src_values, tgt_values = [], []
                for src_eid, tgt_eid in pairs:
                    if not self._db.query(Entity).filter(Entity.id == src_eid).first():
                        continue
                    if not self._db.query(Entity).filter(Entity.id == tgt_eid).first():
                        continue
                    rel = Relation(
                        id=self._stable_relation_id(ontology_id, src_eid, tgt_eid, rel_type, "fk_inference"),
                        ontology_id=ontology_id,
                        source_entity=src_eid, target_entity=tgt_eid,
                        type=rel_type,
                        properties={"fk_column": col, "alt_column": alt_col,
                                    "via": "alternate_key", "source": "fk_inference"},
                        confidence=0.75,
                    )
                    self._db.merge(rel)
                    src_values.append(src_eid)
                    tgt_values.append(tgt_eid)
                    written += 1
                if not written:
                    continue
                cardinality = self._infer_cardinality(src_values, tgt_values)
                inferred_link = self._upsert_inferred_link_mapping(
                    ontology_id=ontology_id,
                    src_dataset_id=src_m.curated_dataset_id,
                    tgt_dataset_id=tgt_m.curated_dataset_id,
                    relation_type=rel_type,
                    src_key=col,
                    tgt_key=alt_col,
                    model=link_model,
                )
                self._db.flush()
                results.append({"src": src_meta["entity_class"], "tgt": tgt_class,
                                "rel_type": rel_type, "fk_col": col, "alt_col": alt_col,
                                "via": "alternate_key", "count": written,
                                "cardinality": cardinality,
                                "link_mapping_id": inferred_link.id if inferred_link else None,
                                "link_mapping_status": inferred_link.status if inferred_link else None})
        return results

    def _upsert_inferred_link_mapping(
        self,
        ontology_id: str,
        src_dataset_id: str | None,
        tgt_dataset_id: str | None,
        relation_type: str,
        src_key: str,
        tgt_key: str,
        model,
    ):
        if not src_dataset_id or not tgt_dataset_id:
            return None
        existing = self._db.query(model).filter(
            model.ontology_id == ontology_id,
            model.src_dataset_id == src_dataset_id,
            model.tgt_dataset_id == tgt_dataset_id,
            model.relation_type == relation_type,
            model.src_key == src_key,
            model.tgt_key == tgt_key,
        ).first()
        if existing:
            if existing.status not in ("active", "inferred"):
                existing.status = "inferred"
            return existing
        link = model(
            ontology_id=ontology_id,
            src_dataset_id=src_dataset_id,
            tgt_dataset_id=tgt_dataset_id,
            relation_type=relation_type,
            src_key=src_key,
            tgt_key=tgt_key,
            status="inferred",
        )
        self._db.add(link)
        return link

    def _detect_fk_columns(
        self, src_cols: list[str], tgt_entity_class: str, tgt_dataset_name: str,
        src_sample_rows: list[dict] | None = None,
        tgt_pk_values: set[str] | None = None,
    ) -> list[tuple[str, str]]:
        """多级 FK 检测: 1)标准_id/.id 2)语义词 3)值重叠 4)值模式 5)LLM"""
        candidates = []
        import re
        tgt_lower = tgt_entity_class.lower()
        tgt_name_lower = (tgt_dataset_name or "").lower()
        tgt_parts = [p.lower() for p in re.split(r'[_\-\s]|(?<=[a-z])(?=[A-Z])', tgt_entity_class) if p]
        tgt_parts.extend([p.lower() for p in re.split(r'[_\-\s]', tgt_name_lower) if p])

        for col in src_cols:
            col_lower = col.lower().rstrip("s")
            # 点号(JSON flatten 产物)与空格/连字符统一归一化为下划线
            col_clean = re.sub(r'[\s\-\.]', '_', col_lower)

            is_standard_fk = col_clean.endswith("_id") or col.endswith("Id") or col.endswith("ID")
            if is_standard_fk:
                col_prefix = re.sub(r'[_]?id$', '', col_clean)
                if (col_prefix in tgt_lower or tgt_lower in col_prefix or
                    any(part in col_prefix for part in tgt_parts if len(part) > 2)):
                    rel_name = col_prefix.upper().replace("-", "_") or tgt_lower.upper()
                    rel_type = f"HAS_{rel_name}" if not rel_name.startswith("HAS_") else rel_name
                    candidates.append((col, rel_type))
                    continue

            col_words = set(re.split(r'[_\-\s]', col_clean))
            tgt_keywords = set(tgt_parts) | {tgt_lower, tgt_name_lower}
            semantic_match = {w for w in (col_words & tgt_keywords) if len(w) > 1}
            if semantic_match:
                rel_name = max(semantic_match, key=len).upper().replace("-", "_")
                rel_type = f"HAS_{rel_name}" if not rel_name.startswith("HAS_") else rel_name
                candidates.append((col, rel_type))
                continue

            # 值重叠检测: 列值与目标主键值高度重合即判定 FK(对中文列名等无法靠列名匹配的情况有效)
            if tgt_pk_values and src_sample_rows:
                tgt_norm = {self._normalize_fk_value(v) for v in tgt_pk_values}
                sample_vals = [str(row.get(col, "")).strip() for row in src_sample_rows[:20] if row.get(col) not in (None, "")]
                if len(sample_vals) >= 2:
                    matched = sum(1 for v in sample_vals if self._normalize_fk_value(v) in tgt_norm)
                    if matched >= 2 and matched / len(sample_vals) >= 0.5:
                        rel_name = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', '_', tgt_entity_class).upper()
                        rel_name = re.sub(r'[^A-Z0-9_]', '', rel_name) or "REF"
                        candidates.append((col, f"HAS_{rel_name}"))
                        continue

            if src_sample_rows and len(src_sample_rows) > 0:
                sample_vals = [str(row.get(col, "")) for row in src_sample_rows[:10] if row.get(col)]
                id_matches = [v for v in sample_vals if re.match(r'^[A-Za-z]+[-_]?\d+$', v)]
                if len(id_matches) >= 2:
                    prefixes = [re.match(r'^[A-Za-z]+', v) for v in id_matches]
                    prefixes = [m.group(0).upper() for m in prefixes if m]
                    if prefixes:
                        rel_type = f"HAS_{max(set(prefixes), key=prefixes.count)}"
                        candidates.append((col, rel_type))

        # 策略 4: LLM 辅助语义 FK 检测（默认关闭，避免构建时被外部服务阻塞）
        import os
        llm_fk_enabled = os.getenv("ENABLE_LLM_FK_DETECTION", "").lower() in ("1", "true", "yes")
        if llm_fk_enabled and not candidates and src_cols:
            try:
                llm_candidates = self._llm_detect_fk(src_cols, tgt_entity_class, tgt_dataset_name)
                candidates.extend(llm_candidates)
            except Exception:
                pass

        return candidates

    def _llm_detect_fk(self, src_cols: list[str], tgt_entity_class: str, tgt_dataset_name: str) -> list[tuple[str, str]]:
        """使用用户配置的 LLM 检测中文列名→英文实体名的 FK 关系"""
        try:
            from app.services import llm_service
            from app.services.model_config_selector import llm_call_kwargs, select_llm_model_config
            import json

            call_kwargs = llm_call_kwargs(select_llm_model_config(
                self._db,
                purpose_tags=("FK检测", "关系推断", "Link推断"),
                allow_vlm=False,
            ))
            if not call_kwargs:
                return []

            prompt = f"""判断以下列中哪些是外键指向目标实体。
源列名: {json.dumps(src_cols, ensure_ascii=False)}
目标实体: {tgt_entity_class}
目标数据集: {tgt_dataset_name}
规则：列名语义关联目标实体（如中文"供应商"→Supplier），或列值像ID。
返回JSON对象 {{"links":[{{"column":"列名","relation_type":"HAS_XXX"}}]}}，无匹配返回 {{"links":[]}}。只返回JSON。"""
            raw = llm_service._call_llm(
                **call_kwargs,
                messages=[{"role": "system", "content": "输出JSON。"}, {"role": "user", "content": prompt}]
            )
            result = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(result, dict):
                result = result.get("links", [])
            if isinstance(result, list):
                return [(r["column"], r["relation_type"]) for r in result if r.get("column")]
            return []
        except Exception:
            return []

    def _process_link_mappings(self, ontology_id: str, mapping_meta: dict) -> list[dict]:
        from app.ontologies.mappings.models import OntologyLinkMapping, OntologyMapping as OM
        from app.models.ontology_formal import LinkType

        links = self._db.query(OntologyLinkMapping).filter(
            OntologyLinkMapping.ontology_id == ontology_id,
            OntologyLinkMapping.status == "active",
        ).all()
        results = []
        for link in links:
            src_meta = tgt_meta = None
            link_type = self._db.query(LinkType).filter(
                LinkType.id == link.link_type_id,
                LinkType.ontology_id == ontology_id,
            ).first()
            for mid, meta in mapping_meta.items():
                m = self._db.query(OM).filter(OM.id == mid).first()
                if not m:
                    continue
                target_type_id = (
                    meta.get("target_object_type_id")
                    or m.target_object_type_id
                )
                if (
                    m.curated_dataset_id == link.src_dataset_id
                    and (
                        link_type is None
                        or target_type_id == link_type.source_object_type_id
                    )
                ):
                    src_meta = meta
                if (
                    m.curated_dataset_id == link.tgt_dataset_id
                    and (
                        link_type is None
                        or target_type_id == link_type.target_object_type_id
                    )
                ):
                    tgt_meta = meta
            if not src_meta or not tgt_meta: continue
            # edge_dataset_id 有值 → 连接表「胖关系」（边可带属性）；否则 → 直连外键「瘦关系」
            if getattr(link, "edge_dataset_id", None):
                results.append(self._process_edge_table_link(ontology_id, link, src_meta, tgt_meta))
            else:
                results.append(self._process_direct_fk_link(ontology_id, link, src_meta, tgt_meta))
        return results

    def _pk_value_to_eid(self, meta: dict) -> dict[str, str]:
        """endpoint 对象映射的「主键值 → 实体 id」查找表：供连接表外键反查两端实体。"""
        out: dict[str, str] = {}
        pk_col = meta["pk_col"]
        for row in meta["rows"]:
            eid = meta["entity_id_map"].get(self._row_identity_value(row, pk_col))
            v = str(row.get(pk_col, "")).strip()
            if v and eid:
                out[v] = eid
        return out

    def _process_direct_fk_link(self, ontology_id: str, link, src_meta: dict, tgt_meta: dict) -> dict:
        """直连外键瘦关系：源表某列 == 目标表某列 → 建边（无自有属性）。原行为保持不变。"""
        from app.models.relation import Relation
        if src_meta.get("stage") is not None and tgt_meta.get("stage") is not None:
            from app.ontologies.mappings.sql_link_materialization import process_direct_link
            return process_direct_link(self, ontology_id, link, src_meta, tgt_meta)

        # 行→实体 id 必须按行自身的主键标识反查，不能 zip(rows, dict.items())——
        # entity_id_map 按 pk 去重后条目数可能少于行数，zip 会截断且错位连边
        def _eid_of(meta: dict, row: dict) -> str | None:
            return meta["entity_id_map"].get(self._row_identity_value(row, meta["pk_col"]))

        tgt_val_to_eid = {}
        for row in tgt_meta["rows"]:
            eid = _eid_of(tgt_meta, row)
            v = str(row.get(link.tgt_key, "")).strip()
            if v and eid: tgt_val_to_eid[v] = eid
        written = 0
        current_relation_ids: set[str] = set()
        src_values: list[str] = []
        tgt_values: list[str] = []
        for row in src_meta["rows"]:
            src_eid = _eid_of(src_meta, row)
            src_val = str(row.get(link.src_key, "")).strip()
            if not src_val or not src_eid: continue
            tgt_eid = tgt_val_to_eid.get(src_val)
            if not tgt_eid: continue
            src_values.append(src_eid)
            tgt_values.append(tgt_eid)
            relation_id = self._stable_relation_id(
                ontology_id, src_eid, tgt_eid, link.relation_type,
                f"link_mapping:{link.id}")
            rel = Relation(
                id=relation_id,
                ontology_id=ontology_id,
                source_entity=src_eid, target_entity=tgt_eid,
                type=link.relation_type,
                properties={"mapping_type": "link_mapping", "src_key": link.src_key,
                            "tgt_key": link.tgt_key, "__link_mapping_id__": link.id},
                confidence=0.9,
            )
            self._db.merge(rel); current_relation_ids.add(relation_id); written += 1
        cardinality = self._infer_cardinality(src_values, tgt_values)
        for stale_rel in self._db.query(Relation).filter(
                Relation.ontology_id == ontology_id).all():
            if ((stale_rel.properties or {}).get("__link_mapping_id__") == link.id
                    and stale_rel.id not in current_relation_ids):
                self._db.delete(stale_rel)
        if written:
            for rel in self._db.query(Relation).filter(
                Relation.ontology_id == ontology_id,
                Relation.type == link.relation_type,
            ).all():
                props = dict(rel.properties or {})
                if props.get("mapping_type") == "link_mapping" and props.get("src_key") == link.src_key and props.get("tgt_key") == link.tgt_key:
                    props["cardinality"] = cardinality
                    rel.properties = props
            self._db.flush()
        self._record_link_mapping_versions(link, src_meta, tgt_meta)
        self._db.flush()
        if written:
            logger.info("Link: " + src_meta["entity_class"] + "-[" + str(link.relation_type) + "]->" + tgt_meta["entity_class"] + " " + str(written) + "条")
        return {"src": src_meta["entity_class"], "tgt": tgt_meta["entity_class"],
                "rel_type": link.relation_type, "src_key": link.src_key, "tgt_key": link.tgt_key,
                "link_mapping_id": link.id,
                "count": written, "cardinality": cardinality,
                "warning": None if written else "No rows matched this link mapping"}

    def _process_edge_table_link(self, ontology_id: str, link, src_meta: dict, tgt_meta: dict) -> dict:
        """连接表胖关系：遍历连接表每行，两端外键各自反查实体，按 field_mapping 采集边属性。

        同一对实体之间可有多条边（各连接表行独立成边），稳定 id 纳入连接表行主键防去重合并。
        """
        if src_meta.get("stage") is not None and tgt_meta.get("stage") is not None:
            from app.ontologies.mappings.sql_link_materialization import process_edge_link
            return process_edge_link(self, ontology_id, link, src_meta, tgt_meta)
        from app.models.relation import Relation
        from app.data_channel.curated.approved_version_reader import (
            latest_dataset_version)
        from app.data_channel.datasets.models import Dataset
        from app.data_channel.curated.models import CuratedDataset
        from app.config import settings

        fmap = {k: v for k, v in dict(getattr(link, "field_mapping", None) or {}).items()
                if not str(k).startswith("__")}
        edge_version = latest_dataset_version(self._db, link.edge_dataset_id)
        try:
            edge_dataset = self._db.query(Dataset).filter(
                Dataset.id == link.edge_dataset_id).first()
            if edge_dataset:
                if edge_dataset.kind == "curated":
                    from app.data_channel.curated.approved_version_reader import (
                        iter_rows_with_edits)
                    # 湖表版本分批流式读取 + 批内叠加行编辑；下游的
                    # cardinality 推断与存量边对账结构上需要全量值集，
                    # 收集点收拢于此（峰值内存 = 一份全量 + 一批）
                    edge_rows = [
                        row
                        for batch in iter_rows_with_edits(
                            self._db, link.edge_dataset_id,
                            require_approved=True, version=edge_version)
                        for row in batch
                    ]
                else:
                    from app.data_channel.datasets.service import DatasetService
                    edge_rows = DatasetService(self._db).load_all_rows(
                        link.edge_dataset_id,
                        edge_version.version_no if edge_version is not None else None)
            else:
                legacy = self._db.query(CuratedDataset).filter(
                    CuratedDataset.id == link.edge_dataset_id).first()
                if settings.environment == "production" or legacy is None or legacy.status != "approved":
                    raise MappingSourceError("legacy 连接表不允许进入生产投影")
                from app.data_channel.datasets.service import DatasetService
                edge_rows = DatasetService(self._db).preview(
                    link.edge_dataset_id, None, limit=None)
        except Exception as e:  # noqa: BLE001
            raise MappingSourceError(
                f"关系映射 {link.id} 的连接表 {link.edge_dataset_id} 无法严格读取/审批: {e}") from e
        if not edge_rows:
            raise MappingSourceError(
                f"关系映射 {link.id} 的连接表当前审批版本为空，拒绝空跑")

        src_v2e = self._pk_value_to_eid(src_meta)
        tgt_v2e = self._pk_value_to_eid(tgt_meta)
        edge_pk_col = self._choose_pk_col(edge_rows)
        pending: list[tuple] = []
        src_values: list[str] = []
        tgt_values: list[str] = []
        for erow in edge_rows:
            sv = str(erow.get(link.src_key, "")).strip()
            tv = str(erow.get(link.tgt_key, "")).strip()
            if not sv or not tv:
                continue
            src_eid = src_v2e.get(sv)
            tgt_eid = tgt_v2e.get(tv)
            if not src_eid or not tgt_eid:
                continue
            edge_props = {prop: erow.get(col) for prop, col in fmap.items() if col in erow}
            edge_key = self._row_identity_value(erow, edge_pk_col)
            pending.append((src_eid, tgt_eid, edge_props, edge_key))
            src_values.append(src_eid)
            tgt_values.append(tgt_eid)

        cardinality = self._infer_cardinality(src_values, tgt_values)
        written = 0
        current_relation_ids: set[str] = set()
        for src_eid, tgt_eid, edge_props, edge_key in pending:
            props = {"mapping_type": "link_mapping", "__edge_key__": edge_key,
                     "__link_mapping_id__": link.id,
                     "cardinality": cardinality, **edge_props}
            relation_id = self._stable_relation_id(
                ontology_id, src_eid, tgt_eid, link.relation_type,
                f"link_mapping:{link.id}:{edge_key}")
            rel = Relation(
                id=relation_id,
                ontology_id=ontology_id,
                source_entity=src_eid, target_entity=tgt_eid,
                type=link.relation_type,
                properties=props,
                confidence=1.0,
            )
            self._db.merge(rel)
            current_relation_ids.add(relation_id)
            written += 1
        for stale_rel in self._db.query(Relation).filter(
                Relation.ontology_id == ontology_id).all():
            if ((stale_rel.properties or {}).get("__link_mapping_id__") == link.id
                    and stale_rel.id not in current_relation_ids):
                self._db.delete(stale_rel)
        if written:
            self._db.flush()
        self._record_link_mapping_versions(
            link, src_meta, tgt_meta,
            edge_version_id=getattr(edge_version, "id", None))
        self._db.flush()
        if written:
            logger.info("Link(连接表): %s-[%s]->%s %d条 边属性=%s",
                        src_meta["entity_class"], link.relation_type, tgt_meta["entity_class"],
                        written, list(fmap.keys()))
        return {"src": src_meta["entity_class"], "tgt": tgt_meta["entity_class"],
                "rel_type": link.relation_type, "src_key": link.src_key, "tgt_key": link.tgt_key,
                "link_mapping_id": link.id,
                "edge_dataset_id": link.edge_dataset_id, "edge_properties": list(fmap.keys()),
                "count": written, "cardinality": cardinality,
                "warning": None if written else "连接表未匹配到任何两端实体"}

    def _record_link_mapping_versions(
        self, link, src_meta: dict, tgt_meta: dict,
        *, edge_version_id: str | None = None,
    ) -> None:
        """Persist exact immutable lake versions consumed by a LinkMapping."""
        from app.data_channel.curated.approved_version_reader import (
            latest_dataset_version,
        )
        from app.config import settings

        version_pairs = (
            ("source", link.src_dataset_id, src_meta.get("source_dataset_version_id")),
            ("target", link.tgt_dataset_id, tgt_meta.get("source_dataset_version_id")),
            ("edge", link.edge_dataset_id, edge_version_id),
        )
        field_mapping = dict(link.field_mapping or {})
        for role, dataset_id, version_id in version_pairs:
            if not dataset_id:
                continue
            current = latest_dataset_version(self._db, dataset_id)
            if current is None and settings.environment != "production":
                # Read-only compatibility for pre-canonical CuratedDataset rows.
                # Production release gates reject these because no immutable
                # DatasetVersion lineage can be proven.
                continue
            if current is None or current.id != version_id:
                current_label = f"v{current.version_no}" if current is not None else "无版本"
                raise MappingApplyError(
                    f"关系映射 {link.id} 执行期间 {role} 数据集已更新为 {current_label}；"
                    "拒绝记录过期投影")
            field_mapping[f"__applied_{role}_version_id__"] = version_id
        link.field_mapping = field_mapping
        self._db.flush()
