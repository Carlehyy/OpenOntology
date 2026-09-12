"""映射建议编排：知识库 → 规则 → LLM 概念化裁决，全部建议进人工确认队列。

流水线（与 frontend 建议面板约定 camelCase 响应）：
1. 版本守卫：仅草稿且 editing 可生成建议；
2. 飞轮回流：harvest 当前草稿与当前发布快照中人工保存过的映射（幂等）；
3. 逐数据集：湖列契约（get_schema）→ L0 知识库命中 → L1 规则候选 →
   L2 LLM 两段式概念化裁决（AutoSchemaKG 式：先归纳列概念再锚定本体属性）；
4. 服务端二次校验：幻觉属性/类型不兼容/不存在列一律丢弃转 unsure；
5. 无 LLM 时知识库+规则兜底，所有建议标记 unsure（人工确认文化）。

持久化建议队列（OntologyMappingSuggestion）：探索 Agent 的 propose_mapping
工具经 propose_agent_mapping 把映射提案落库为 pending 建议，与 L0-L2 建议
同属人工确认队列语义 —— 确认/应用只发生在映射视图，未确认建议不进草稿快照、
不回流知识库。get_mapping_overview 是同一绑定版本的只读现状视图。
"""
from __future__ import annotations

import json
import logging
from collections import Counter

from fastapi import HTTPException
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from app.ontologies.mappings import mapping_knowledge
from app.ontologies.mappings.suggestion_candidates import (
    pick_primary_key_column,
    score_column_to_property,
    score_dataset_to_object,
    types_compatible,
)

logger = logging.getLogger(__name__)

# AutoSchemaKG 批处理模式：每批列数上限，大表分批以控制单次 prompt 规模。
_LLM_BATCH_SIZE = 16
_LLM_MAX_BATCHES = 4
# 规则分达到该阈值视为高置信，不再占用 LLM 额度（飞轮降本的关键路径）。
_RULE_CONFIDENT_SCORE = 0.9
_RULE_SUGGEST_SCORE = 0.5

_LLM_PURPOSE_TAGS = ("Ontology映射", "Mapping建议", "自动映射")


def generate_mapping_suggestions(
    db: Session,
    ontology_id: str,
    version_id: str,
    dataset_ids: list[str],
) -> dict:
    draft = _require_editable_draft(db, ontology_id, version_id)

    from app.ontologies.versions.snapshot_contract import complete_snapshot

    draft_snapshot = complete_snapshot(draft.snapshot_formal)
    _harvest_confirmed_mappings(db, ontology_id, draft_snapshot)

    inventory = _object_inventory(draft_snapshot)
    if not inventory:
        raise HTTPException(422, detail={
            "code": "empty_ontology",
            "message": "草稿中还没有对象实体，请先在模型结构中建模后再生成映射建议。",
        })
    property_index = {
        (obj["name"], prop["name"]): prop["type"]
        for obj in inventory
        for prop in obj["properties"]
    }
    existing_by_dataset = _existing_mapping_targets(draft_snapshot, inventory)
    llm_kwargs = _resolve_llm_kwargs(db)

    suggestions = []
    knowledge_hits = 0
    for dataset_id in dataset_ids:
        entry = _suggest_for_dataset(
            db, str(dataset_id), inventory, property_index,
            existing_by_dataset, llm_kwargs,
        )
        knowledge_hits += sum(
            1 for item in entry["fieldMappings"] if item["source"] == "knowledge"
        )
        suggestions.append(entry)
    return {
        "llmAvailable": bool(llm_kwargs),
        "knowledgeHits": knowledge_hits,
        "suggestions": suggestions,
    }


# ── 版本守卫（语义对齐 versions/workspace_service，避免跨域私有依赖）────────

def _require_editable_draft(db: Session, ontology_id: str, version_id: str):
    from app.ontologies.versions.models import OntologyVersion

    draft = db.query(OntologyVersion).filter(
        OntologyVersion.id == version_id,
        OntologyVersion.ontology_id == ontology_id,
    ).first()
    if draft is None:
        raise HTTPException(404, "Version not found")
    if draft.node_kind != "draft":
        raise HTTPException(409, detail={
            "code": "immutable_release",
            "message": "发布版本不可修改，请先创建草稿分支",
        })
    if draft.lifecycle_status == "trial_ready":
        raise HTTPException(409, detail={
            "code": "trial_snapshot_frozen",
            "message": "试跑态快照已冻结；如需继续修改，请从该版本创建新的草稿分支",
        })
    if draft.lifecycle_status != "editing":
        raise HTTPException(409, detail={
            "code": "archived_version_immutable",
            "message": "该版本已归档不可修改，请从该版本创建新的草稿分支",
        })
    return draft


def _harvest_confirmed_mappings(db: Session, ontology_id: str, draft_snapshot: dict) -> None:
    """飞轮回流：人工保存过的映射入知识库。失败不阻断建议生成。"""
    from app.models.ontology import OntologyProject
    from app.ontologies.versions.models import OntologyVersion
    from app.ontologies.versions.snapshot_contract import complete_snapshot

    try:
        touched = mapping_knowledge.harvest_snapshot_mappings(db, draft_snapshot)
        project = db.query(OntologyProject).filter(
            OntologyProject.id == ontology_id).first()
        if project and project.current_release_id:
            release = db.query(OntologyVersion).filter(
                OntologyVersion.id == project.current_release_id).first()
            if release is not None:
                touched += mapping_knowledge.harvest_snapshot_mappings(
                    db, complete_snapshot(release.snapshot_formal))
        if touched:
            logger.info("mapping knowledge harvested %s entries for %s", touched, ontology_id)
    except Exception:
        db.rollback()
        logger.exception("mapping knowledge harvest failed for %s", ontology_id)


# ── 本体清单 ─────────────────────────────────────────────────────────────

def _object_inventory(snapshot: dict) -> list[dict]:
    inventory = []
    for item in (snapshot.get("objectTypes") or []):
        if not isinstance(item, dict):
            continue
        properties = []
        for prop in item.get("properties") or []:
            if not isinstance(prop, dict):
                continue
            if prop.get("computed") or prop.get("source") == "computed":
                continue
            name = str(prop.get("name") or "").strip()
            if not name:
                continue
            properties.append({
                "name": name,
                "displayName": str(prop.get("displayName") or prop.get("display_name") or ""),
                "type": str(prop.get("type") or "string"),
            })
        name = str(item.get("name") or "").strip()
        if name:
            inventory.append({
                "id": str(item.get("id") or ""),
                "name": name,
                "displayName": str(item.get("displayName") or item.get("display_name") or ""),
                "primaryKey": item.get("primaryKey") or item.get("primary_key"),
                "properties": properties,
            })
    return inventory


def _existing_mapping_targets(snapshot: dict, inventory: list[dict]) -> dict[str, str]:
    """草稿快照中已有映射的 数据集id → 对象实体id。"""
    by_name = {obj["name"]: obj["id"] for obj in inventory}
    targets: dict[str, str] = {}
    for mapping in snapshot.get("mappings") or []:
        if not isinstance(mapping, dict):
            continue
        dataset_id = mapping.get("curated_dataset_id") or mapping.get("curatedDatasetId")
        if not dataset_id:
            continue
        target = mapping.get("target_object_type_id") or mapping.get("targetObjectTypeId")
        if not target:
            entity_class = mapping.get("entity_class") or mapping.get("entityClass")
            target = by_name.get(str(entity_class or ""))
        if target:
            targets[str(dataset_id)] = str(target)
    return targets


# ── LLM 通道（沿用 AutoMapper 的 patch seam）──────────────────────────────

def _resolve_llm_kwargs(db: Session) -> dict | None:
    from app.services.model_config_selector import (
        llm_call_kwargs,
        select_llm_model_config,
    )

    try:
        return llm_call_kwargs(select_llm_model_config(
            db, purpose_tags=_LLM_PURPOSE_TAGS, allow_vlm=False))
    except Exception:
        logger.info("mapping suggestion: no usable LLM config", exc_info=True)
        return None


def _llm_adjudicate(
    llm_kwargs: dict,
    inventory: list[dict],
    dataset_name: str,
    columns: list[dict],
    examples: list[str],
    pairing_hint: dict | None,
) -> dict:
    """AutoSchemaKG 式两段式：先归纳每列业务概念，再把概念锚定到本体属性。"""
    from app.services import llm_service

    inventory_lines = []
    for obj in inventory:
        props = "、".join(
            f"{prop['name']}（{prop['displayName'] or prop['name']}, {prop['type']}）"
            for prop in obj["properties"]
        )
        inventory_lines.append(
            f"- id={obj['id']} 对象 {obj['name']}（{obj['displayName'] or obj['name']}）：{props}"
        )
    column_lines = []
    for index, col in enumerate(columns, start=1):
        display = str(col.get("display_name") or "").strip()
        samples = json.dumps(col.get("sample_values") or [], ensure_ascii=False)
        flags = "，主键列" if col.get("is_primary_key") else ""
        column_lines.append(
            f"{index}. {col.get('name')}（{display or '无显示名'}）"
            f"类型 {col.get('type') or 'string'}{flags}，样例：{samples}"
        )
    example_block = "\n".join(f"- {line}" for line in examples) or "（无）"
    hint_block = (
        f"当前候选配对：{pairing_hint['name']}（{pairing_hint['displayName']}），可纠正。"
        if pairing_hint else "当前无候选配对。"
    )
    prompt = f"""请把数据资产湖的表映射到本体对象实体，分两步：先归纳每列的业务概念（概念化），再把概念锚定到本体属性。

【本体对象清单】（仅可锚定到下列对象与属性）
{chr(10).join(inventory_lines)}

【数据集】{dataset_name}
列文档：
{chr(10).join(column_lines)}

【已确认的历史映射】（供参考复用）
{example_block}

{hint_block}

【输出要求】只输出 JSON：
{{
  "pairing": {{"object_type_id": "清单中的对象 id 或 null", "verdict": "match 或 unsure", "reason": "配对理由"}},
  "column_concepts": [{{"column": "列名", "concept": "该列的业务概念"}}],
  "field_mappings": [{{"column": "列名", "property": "属性名", "verdict": "match 或 unsure 或 skip", "reason": "理由"}}],
  "primary_key_column": "主键列名或 null"
}}
约束：property 必须来自配对对象的属性清单且类型兼容；拿不准用 unsure；无合适属性用 skip。"""
    raw = llm_service._call_llm(
        **llm_kwargs,
        messages=[
            {"role": "system", "content": "你是数据建模专家。只输出 JSON，不要输出其他文字。"},
            {"role": "user", "content": prompt},
        ],
    )
    return _parse_llm_json(raw)


def _parse_llm_json(raw: object) -> dict:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
        text = text.strip()
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("LLM 返回不是 JSON")
        payload = json.loads(text[start:end + 1])
    if not isinstance(payload, dict):
        raise ValueError("LLM 返回不是 JSON 对象")
    return payload


# ── 单数据集建议 ─────────────────────────────────────────────────────────

def _suggest_for_dataset(
    db: Session,
    dataset_id: str,
    inventory: list[dict],
    property_index: dict[tuple[str, str], str],
    existing_by_dataset: dict[str, str],
    llm_kwargs: dict | None,
) -> dict:
    from app.data_channel.datasets.query_service import get_schema
    from app.data_channel.datasets.models import Dataset

    base = {
        "datasetId": dataset_id,
        "datasetName": "",
        "objectTypeId": None,
        "pairingVerdict": "unsure",
        "pairingReason": "",
        "primaryKeyColumn": None,
        "existingObjectTypeId": None,
        "fieldMappings": [],
        "skippedColumns": [],
        "error": None,
    }
    dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
    if dataset is None:
        return {**base, "error": "数据集不存在或尚未迁入资产湖"}
    base["datasetName"] = dataset.name
    try:
        schema = get_schema(dataset_id, db)
    except Exception:
        logger.info("mapping suggestion: schema unavailable for %s", dataset_id, exc_info=True)
        return {**base, "error": "数据集结构暂不可用"}
    columns = [
        col for col in (schema.get("columns") or [])
        if isinstance(col, dict) and col.get("name")
    ]
    if not columns:
        return {**base, "error": "暂未识别到字段"}

    # ── 配对：已有映射 > 知识库投票 > 规则 > LLM（在 LLM 阶段补充裁决）──
    existing_target = existing_by_dataset.get(dataset_id)
    by_id = {obj["id"]: obj for obj in inventory}
    by_name = {obj["name"]: obj for obj in inventory}

    kb_top: dict[str, object] = {}
    kb_object_votes: Counter[str] = Counter()
    for column in columns:
        hits = mapping_knowledge.lookup(db, column, property_index, limit=1)
        if hits:
            kb_top[str(column["name"])] = hits[0]
            kb_object_votes[hits[0].object_name] += 1

    paired: dict | None = None
    if existing_target and existing_target in by_id:
        paired = by_id[existing_target]
        base["existingObjectTypeId"] = existing_target
        base["pairingVerdict"] = "match"
        base["pairingReason"] = "该数据集在草稿中已有映射，沿用既有配对"
    elif kb_object_votes:
        voted = kb_object_votes.most_common(1)[0][0]
        if voted in by_name:
            paired = by_name[voted]
            base["pairingVerdict"] = "match"
            base["pairingReason"] = "列级历史映射多数指向该对象（数据飞轮复用）"
    if paired is None:
        rule_best = max(
            inventory,
            key=lambda obj: score_dataset_to_object(dataset.name, obj),
            default=None,
        )
        if rule_best is not None and score_dataset_to_object(dataset.name, rule_best) >= 0.5:
            paired = rule_best
            base["pairingVerdict"] = "unsure"
            base["pairingReason"] = "按数据集名称与对象名称相似度推荐，请确认"

    # ── 字段级：L0 知识库 / L1 规则高置信先行，其余交给 LLM ──
    paired_props = (paired or {}).get("properties") or []
    field_mappings: list[dict] = []
    skipped: list[dict] = []
    llm_pending: list[dict] = []
    for column in columns:
        name = str(column["name"])
        hit = kb_top.get(name)
        if hit is not None and paired is not None and hit.object_name == paired["name"]:
            field_mappings.append({
                "column": name,
                "property": hit.property_name,
                "verdict": "match",
                "confidence": min(0.99, 0.8 + 0.02 * (hit.confirm_count or 1)),
                "reason": f"历史映射复用·已确认 {hit.confirm_count or 1} 次",
                "source": "knowledge",
            })
            continue
        best_prop, best_score = _best_rule_property(column, paired_props)
        # 高置信规则命中仅在 LLM 可用时直接标 match；LLM 缺失时全部转人工确认。
        if (
            best_prop is not None
            and best_score >= _RULE_CONFIDENT_SCORE
            and llm_kwargs
        ):
            field_mappings.append({
                "column": name,
                "property": best_prop["name"],
                "verdict": "match",
                "confidence": best_score,
                "reason": "列名与属性精确匹配",
                "source": "rule",
            })
            continue
        llm_pending.append(column)

    # ── L2 LLM 概念化裁决（无 LLM 时规则兜底全部 unsure）──
    llm_failed = False
    if llm_kwargs and (llm_pending or paired is None):
        examples = mapping_knowledge.few_shot_examples(db, columns, property_index)
        for batch in _batches(llm_pending, _LLM_BATCH_SIZE, _LLM_MAX_BATCHES):
            try:
                payload = _llm_adjudicate(
                    llm_kwargs, inventory, dataset.name, batch, examples, paired)
            except Exception:
                logger.info("mapping suggestion: LLM adjudication failed", exc_info=True)
                llm_failed = True
                break
            if paired is None:
                candidate = _validate_llm_pairing(payload, by_id)
                if candidate is not None:
                    paired = candidate
                    base["pairingVerdict"] = "unsure"
                    base["pairingReason"] = str(
                        (payload.get("pairing") or {}).get("reason") or "LLM 推荐配对，请确认")
                    paired_props = paired["properties"]
            accepted = _validate_llm_field_mappings(payload, batch, paired_props)
            for column in batch:
                name = str(column["name"])
                if name in accepted:
                    field_mappings.append(accepted[name])
                    llm_pending.remove(column)

    for column in llm_pending:
        name = str(column["name"])
        best_prop, best_score = _best_rule_property(column, paired_props)
        if best_prop is not None and best_score >= _RULE_SUGGEST_SCORE:
            field_mappings.append({
                "column": name,
                "property": best_prop["name"],
                "verdict": "unsure",
                "confidence": best_score,
                "reason": "LLM 不可用，按名称相似度推荐，请人工确认" if not llm_kwargs or llm_failed
                else "LLM 未给出结论，按名称相似度推荐，请人工确认",
                "source": "rule",
            })
        else:
            skipped.append({"column": name, "reason": "未找到可信的本体属性对应"})

    if paired is not None:
        base["objectTypeId"] = paired["id"]
    base["primaryKeyColumn"] = pick_primary_key_column(
        columns, dataset.schema_json if isinstance(dataset.schema_json, dict) else {})
    base["fieldMappings"] = field_mappings
    base["skippedColumns"] = skipped
    return base


def _best_rule_property(column: dict, properties: list[dict]) -> tuple[dict | None, float]:
    best_prop, best_score = None, 0.0
    for prop in properties:
        score = score_column_to_property(column, prop)
        if score > best_score:
            best_prop, best_score = prop, score
    return best_prop, best_score


def _batches(columns: list[dict], size: int, max_batches: int) -> list[list[dict]]:
    batches = [columns[index:index + size] for index in range(0, len(columns), size)]
    return batches[:max_batches]


def _validate_llm_pairing(payload: dict, by_id: dict[str, dict]) -> dict | None:
    pairing = payload.get("pairing")
    if not isinstance(pairing, dict):
        return None
    object_type_id = str(pairing.get("object_type_id") or "").strip()
    return by_id.get(object_type_id)


def _validate_llm_field_mappings(
    payload: dict,
    batch: list[dict],
    properties: list[dict],
) -> dict[str, dict]:
    """服务端二次校验：幻觉属性/类型不兼容/不存在列一律丢弃。"""
    column_types = {str(col["name"]): col.get("type") for col in batch}
    prop_by_name = {prop["name"]: prop for prop in properties}
    accepted: dict[str, dict] = {}
    raw_items = payload.get("field_mappings")
    if not isinstance(raw_items, list):
        return accepted
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        column = str(item.get("column") or "")
        prop_name = str(item.get("property") or "")
        verdict = str(item.get("verdict") or "unsure")
        if column not in column_types or prop_name not in prop_by_name:
            continue
        if verdict == "skip":
            continue
        if not types_compatible(column_types[column], prop_by_name[prop_name]["type"]):
            continue
        accepted[column] = {
            "column": column,
            "property": prop_name,
            "verdict": "match" if verdict == "match" else "unsure",
            "confidence": 0.85 if verdict == "match" else 0.5,
            "reason": str(item.get("reason") or "LLM 概念化锚定"),
            "source": "llm",
        }
    return accepted


# ── Agent 映射提案（持久化人工确认队列）──────────────────────────────────
# 探索 Agent 的 get_mapping_overview / propose_mapping 工具的服务层入口。
# 与 L0-L2 流水线同一确认纪律：建议落库即 pending，确认/应用只发生在映射视图；
# 版本守卫直接复用 _require_editable_draft（draft + editing 才接受提案）。

_OVERVIEW_OBJECT_LIMIT = 20
_OVERVIEW_MAPPING_LIMIT = 20
_OVERVIEW_COLUMN_LIMIT = 60
_PROPOSE_FIELD_LIMIT = 500


def _version_or_404(db: Session, ontology_id: str, version_id: str):
    from app.ontologies.versions.models import OntologyVersion

    version = db.query(OntologyVersion).filter(
        OntologyVersion.id == version_id,
        OntologyVersion.ontology_id == ontology_id,
    ).first()
    if version is None:
        raise HTTPException(404, detail={
            "code": "version_not_found",
            "message": "绑定的本体版本不存在，请重新绑定后再查看映射",
        })
    return version


def _dataset_columns(dataset_id: str, db: Session) -> tuple[object, list[dict]]:
    """数据集行 + 湖列契约；任一环节缺失都转成带业务语义的工具错误。"""
    from app.data_channel.datasets.models import Dataset
    from app.data_channel.datasets.query_service import get_schema

    dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
    if dataset is None:
        raise HTTPException(404, detail={
            "code": "dataset_not_found",
            "message": f"数据集「{dataset_id}」不存在或尚未迁入资产湖",
        })
    try:
        schema = get_schema(dataset_id, db)
    except HTTPException:
        raise
    except Exception:
        logger.info("mapping overview: schema unavailable for %s",
                    dataset_id, exc_info=True)
        raise HTTPException(422, detail={
            "code": "schema_unavailable",
            "message": f"数据集「{dataset.name}」结构暂不可用",
        }) from None
    columns = [
        col for col in (schema.get("columns") or [])
        if isinstance(col, dict) and col.get("name")
    ]
    return dataset, columns


def _resolve_target_object(inventory: list[dict], object_ref: str) -> dict:
    """按 id → name → displayName 解析目标对象；歧义/缺失都给可行动的清单。"""
    ref = str(object_ref or "").strip()
    if not ref:
        raise HTTPException(422, detail={
            "code": "object_ref_required",
            "message": "缺少目标对象（target_object），请传对象 id 或名称",
        })
    for key in ("id", "name", "displayName"):
        hits = [obj for obj in inventory if str(obj.get(key) or "") == ref]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise HTTPException(422, detail={
                "code": "object_ref_ambiguous",
                "message": f"目标对象「{ref}」命中多个对象，请改用对象 id",
            })
    raise HTTPException(422, detail={
        "code": "object_not_found",
        "message": (
            f"对象「{ref}」不在该版本的对象清单中。可用对象："
            + "、".join(obj["name"] for obj in inventory[:20])
        ),
    })


def _pending_suggestions(db: Session, ontology_id: str, version_id: str) -> list:
    from app.ontologies.mappings.models import OntologyMappingSuggestion

    return db.query(OntologyMappingSuggestion).filter(
        OntologyMappingSuggestion.ontology_id == ontology_id,
        OntologyMappingSuggestion.version_id == version_id,
        OntologyMappingSuggestion.status == "pending",
    ).all()


def get_mapping_overview(
    db: Session,
    ontology_id: str,
    version_id: str,
    *,
    dataset_id: str | None = None,
    object_limit: int = _OVERVIEW_OBJECT_LIMIT,
    mapping_limit: int = _OVERVIEW_MAPPING_LIMIT,
    column_limit: int = _OVERVIEW_COLUMN_LIMIT,
) -> dict:
    """绑定版本的映射现状只读视图（探索 Agent 对齐字段用）。

    只读：任何版本可看；对象/映射/数据集清单分别截断并给出 total 与
    truncated 标记，调用方据此收窄（object_limit/mapping_limit/column_limit）。
    """
    from app.data_channel.datasets.models import Dataset
    from app.ontologies.versions.snapshot_contract import complete_snapshot

    version = _version_or_404(db, ontology_id, version_id)
    snapshot = complete_snapshot(version.snapshot_formal)
    inventory = _object_inventory(snapshot)
    object_limit = max(1, min(100, int(object_limit)))
    mapping_limit = max(1, min(100, int(mapping_limit)))
    column_limit = max(1, min(200, int(column_limit)))

    raw_mappings = [
        item for item in (snapshot.get("mappings") or [])
        if isinstance(item, dict)
    ]
    pending = _pending_suggestions(db, ontology_id, version_id)

    mapped_target_ids = set(_existing_mapping_targets(snapshot, inventory).values())
    pending_target_ids = {item.object_type_id for item in pending}
    covered = mapped_target_ids | pending_target_ids
    unmapped = [obj for obj in inventory if obj["id"] not in covered]

    # 涉及的数据集：既有映射 + 待确认建议 + 调用方点名的数据集（去重保序）
    involved_ids: list[str] = []
    for raw in raw_mappings:
        value = raw.get("curatedDatasetId") or raw.get("curated_dataset_id")
        if value and str(value) not in involved_ids:
            involved_ids.append(str(value))
    for item in pending:
        if item.dataset_id not in involved_ids:
            involved_ids.append(item.dataset_id)
    if dataset_id and str(dataset_id) not in involved_ids:
        involved_ids.append(str(dataset_id))
    if dataset_id:
        # 显式聚焦的数据集必须存在；涉及清单里的历史引用才允许静默标记 missing
        _dataset_columns(str(dataset_id), db)

    # 显式点名的数据集钉到清单首位，永不被 involved 截断（工具描述承诺拉取
    # 该数据集列清单）；其余按涉及顺序，超出 10 个截断并如实标记。
    ordered_ids = (
        [str(dataset_id)] + [item for item in involved_ids
                             if item != str(dataset_id)]
        if dataset_id else involved_ids
    )
    datasets_truncated = len(ordered_ids) > 10

    datasets: list[dict] = []
    for involved in ordered_ids[:10]:
        dataset = db.query(Dataset).filter(Dataset.id == involved).first()
        if dataset is None:
            datasets.append({"id": involved, "missing": True,
                             "columns": [], "columnsTotal": 0})
            continue
        entry: dict = {"id": dataset.id, "name": dataset.name,
                       "kind": dataset.kind}
        try:
            _, columns = _dataset_columns(involved, db)
        except HTTPException:
            columns = []
            entry["schemaUnavailable"] = True
        entry["columns"] = [
            {"name": str(col.get("name")),
             "displayName": str(col.get("display_name") or ""),
             "type": str(col.get("type") or "string")}
            for col in columns[:column_limit]
        ]
        entry["columnsTotal"] = len(columns)
        entry["columnsTruncated"] = len(columns) > column_limit
        datasets.append(entry)

    return {
        "ontologyId": ontology_id,
        "versionId": version.id,
        "versionNumber": version.version_number,
        "editable": bool(
            version.node_kind == "draft"
            and version.lifecycle_status == "editing"),
        "objects": inventory[:object_limit],
        "objectsTotal": len(inventory),
        "objectsTruncated": len(inventory) > object_limit,
        "mappings": [
            {
                "id": str(raw.get("id") or ""),
                "datasetId": (raw.get("curatedDatasetId")
                              or raw.get("curated_dataset_id")),
                "entityClass": (raw.get("entityClass")
                                or raw.get("entity_class") or ""),
                "targetObjectTypeId": (raw.get("targetObjectTypeId")
                                       or raw.get("target_object_type_id")),
                "fieldCount": len([
                    key for key in (raw.get("fieldMapping")
                                    or raw.get("field_mapping") or {})
                    if not str(key).startswith("__")
                ]),
            }
            for raw in raw_mappings[:mapping_limit]
        ],
        "mappingsTotal": len(raw_mappings),
        "mappingsTruncated": len(raw_mappings) > mapping_limit,
        "pendingSuggestions": len(pending),
        "unmappedObjects": {
            "count": len(unmapped),
            "names": [obj["name"] for obj in unmapped[:20]],
        },
        "datasets": datasets,
        "datasetsTruncated": datasets_truncated,
    }


def propose_agent_mapping(
    db: Session,
    ontology_id: str,
    version_id: str,
    *,
    dataset_id: str,
    object_ref: str,
    field_mapping: dict,
    primary_key_column: str | None = None,
    note: str = "",
) -> dict:
    """把 Agent 的映射提案写入人工确认队列（status=pending）。

    与 L0-L2 建议同一服务端校验口径：版本守卫复用 _require_editable_draft，
    列/属性必须真实存在且类型兼容（types_compatible）——不存在的列、幻觉
    属性、类型不兼容一律整体拒绝并说明，不产生半截建议。重复提交同一提案
    （同数据集+同目标对象+同 field_mapping）幂等复用已有 pending 行。
    """
    from app.ontologies.mappings.models import OntologyMappingSuggestion
    from app.ontologies.versions.snapshot_contract import complete_snapshot

    draft = _require_editable_draft(db, ontology_id, version_id)
    dataset, columns = _dataset_columns(str(dataset_id or "").strip(), db)
    if not columns:
        raise HTTPException(422, detail={
            "code": "no_columns",
            "message": f"数据集「{dataset.name}」暂未识别到字段，无法映射",
        })
    column_docs = {str(col["name"]): col for col in columns}

    inventory = _object_inventory(complete_snapshot(draft.snapshot_formal))
    if not inventory:
        raise HTTPException(422, detail={
            "code": "empty_ontology",
            "message": "草稿中还没有对象实体，请先在模型结构中建模后再提交映射建议。",
        })
    target = _resolve_target_object(inventory, object_ref)
    prop_docs = {prop["name"]: prop for prop in target["properties"]}

    if not isinstance(field_mapping, dict) or not field_mapping:
        raise HTTPException(422, detail={
            "code": "invalid_field_mapping",
            "message": "field_mapping 必须是非空对象（{数据集列名: 本体属性名}）",
        })
    if len(field_mapping) > _PROPOSE_FIELD_LIMIT:
        raise HTTPException(422, detail={
            "code": "invalid_field_mapping",
            "message": f"field_mapping 最多 {_PROPOSE_FIELD_LIMIT} 条，请拆分提交",
        })
    proposal: dict[str, str] = {}
    unknown_columns: list[str] = []
    unknown_properties: list[str] = []
    incompatible: list[str] = []
    for raw_column, raw_prop in field_mapping.items():
        column = str(raw_column or "").strip()
        prop_name = str(raw_prop or "").strip()
        if not column or not prop_name:
            raise HTTPException(422, detail={
                "code": "invalid_field_mapping",
                "message": "field_mapping 的列名与属性名都必须是非空字符串",
            })
        if column not in column_docs:
            unknown_columns.append(column)
            continue
        if prop_name not in prop_docs:
            unknown_properties.append(prop_name)
            continue
        if not types_compatible(
                column_docs[column].get("type"), prop_docs[prop_name]["type"]):
            incompatible.append(
                f"{column}({column_docs[column].get('type') or 'string'})"
                f"→{prop_name}({prop_docs[prop_name]['type']})")
            continue
        proposal[column] = prop_name
    if unknown_columns:
        raise HTTPException(422, detail={
            "code": "unknown_columns",
            "message": (
                f"数据集「{dataset.name}」中不存在列："
                f"{'、'.join(unknown_columns[:10])}；可用列："
                + "、".join(list(column_docs)[:30])),
        })
    if unknown_properties:
        raise HTTPException(422, detail={
            "code": "unknown_properties",
            "message": (
                f"对象「{target['name']}」中不存在属性："
                f"{'、'.join(unknown_properties[:10])}；可用属性："
                + "、".join(list(prop_docs)[:30])),
        })
    if incompatible:
        raise HTTPException(422, detail={
            "code": "incompatible_types",
            "message": f"以下字段映射类型不兼容：{'、'.join(incompatible[:10])}",
        })

    pk_column = str(primary_key_column or "").strip() or None
    if pk_column is not None and pk_column not in column_docs:
        raise HTTPException(422, detail={
            "code": "unknown_columns",
            "message": f"主键列「{pk_column}」不在数据集「{dataset.name}」的列清单中",
        })

    existing = db.query(OntologyMappingSuggestion).filter(
        OntologyMappingSuggestion.ontology_id == ontology_id,
        OntologyMappingSuggestion.version_id == version_id,
        OntologyMappingSuggestion.dataset_id == dataset.id,
        OntologyMappingSuggestion.object_type_id == target["id"],
        OntologyMappingSuggestion.status == "pending",
    ).all()
    for row in existing:
        if (dict(row.field_mapping or {}) == proposal
                and (row.primary_key_column or None) == pk_column):
            return {
                "suggestionIds": [row.id],
                "reused": True,
                "datasetId": dataset.id,
                "datasetName": dataset.name,
                "objectTypeId": target["id"],
                "objectName": target["name"],
                "fieldCount": len(proposal),
            }

    row = OntologyMappingSuggestion(
        ontology_id=ontology_id,
        version_id=version_id,
        dataset_id=dataset.id,
        object_type_id=target["id"],
        entity_class=target["name"],
        field_mapping=proposal,
        primary_key_column=pk_column,
        status="pending",
        source="agent",
        note=str(note or "")[:500],
    )
    db.add(row)
    db.commit()
    return {
        "suggestionIds": [row.id],
        "reused": False,
        "datasetId": dataset.id,
        "datasetName": dataset.name,
        "objectTypeId": target["id"],
        "objectName": target["name"],
        "fieldCount": len(proposal),
    }


# ── 持久建议队列：列表 / 确认 / 驳回 ─────────────────────────────────────
# 闭环纪律：pending 建议只经映射视图的队列 UI 流转；confirm 把建议变成草稿
# 快照里的正式映射 —— 与人工在画布上确认瞬时建议后「保存配置」完全同路径
# （workspace_service.save_draft_mappings：同一守卫、校验、revision/hash 推进
# 与审计），并按飞轮纪律回流知识库（harvest_snapshot_mappings 同一函数）。

_SUGGESTION_STATUSES = frozenset({"pending", "confirmed", "dismissed"})

def _suggestion_or_404(db: Session, ontology_id: str, version_id: str,
                       suggestion_id: str):
    from app.ontologies.mappings.models import OntologyMappingSuggestion

    suggestion = db.query(OntologyMappingSuggestion).filter(
        OntologyMappingSuggestion.id == str(suggestion_id or "").strip(),
        OntologyMappingSuggestion.ontology_id == ontology_id,
        OntologyMappingSuggestion.version_id == version_id,
    ).first()
    if suggestion is None:
        raise HTTPException(404, detail={
            "code": "suggestion_not_found",
            "message": "映射建议不存在或不属于该版本",
        })
    return suggestion


def list_persistent_suggestions(
    db: Session,
    ontology_id: str,
    version_id: str,
    *,
    status: str | None = "pending",
    offset: int = 0,
    limit: int = 50,
) -> dict:
    """持久建议队列只读列表（默认仅 pending；confirmed/dismissed 可显式查询）。"""
    from app.data_channel.datasets.models import Dataset
    from app.ontologies.mappings.models import OntologyMappingSuggestion

    _version_or_404(db, ontology_id, version_id)
    if status and status not in _SUGGESTION_STATUSES:
        raise HTTPException(422, detail={
            "code": "invalid_status",
            "message": (f"status 只支持 {'/'.join(sorted(_SUGGESTION_STATUSES))}"
                        f"，收到: {status}"),
        })
    query = db.query(OntologyMappingSuggestion).filter(
        OntologyMappingSuggestion.ontology_id == ontology_id,
        OntologyMappingSuggestion.version_id == version_id,
    )
    if status:
        query = query.filter(OntologyMappingSuggestion.status == status)
    total = query.count()
    offset = max(0, int(offset))
    limit = max(1, min(200, int(limit)))
    rows = (query.order_by(OntologyMappingSuggestion.created_at.asc())
            .offset(offset).limit(limit).all())
    dataset_names = {
        dataset.id: dataset.name
        for dataset in db.query(Dataset).filter(
            Dataset.id.in_({row.dataset_id for row in rows})).all()
    } if rows else {}
    items = []
    for row in rows:
        items.append({
            "id": row.id,
            "datasetId": row.dataset_id,
            "datasetName": dataset_names.get(row.dataset_id, ""),
            "objectTypeId": row.object_type_id,
            "objectName": row.entity_class,
            "fieldMappings": [
                {
                    "column": column,
                    "property": prop,
                    # 持久建议一律待人工确认（与瞬时建议的 unsure 同一呈现语义）
                    "verdict": "unsure",
                    "confidence": 0.5,
                    "reason": row.note or "Agent 提案，待人工确认",
                    "source": row.source or "agent",
                }
                for column, prop in (row.field_mapping or {}).items()
            ],
            "primaryKeyColumn": row.primary_key_column,
            "source": row.source or "agent",
            "note": row.note or "",
            "status": row.status,
            "statusReason": row.status_reason or "",
            "confirmedMappingId": row.confirmed_mapping_id,
            "createdAt": (row.created_at.isoformat()
                          if row.created_at else None),
        })
    return {
        "suggestions": items,
        "total": total,
        "offset": offset,
        "limit": limit,
        "hasMore": offset + len(rows) < total,
    }


def confirm_persistent_suggestion(
    db: Session,
    ontology_id: str,
    version_id: str,
    suggestion_id: str,
    current_user,
) -> dict:
    """确认建议 → 草稿快照正式映射（与人工保存画布映射完全同路径）。

    同数据集+同目标对象已有映射条目时按画布保存语义合并 fieldMapping
    （建议键覆盖），不产生重复条目；已确认建议重复确认返回明确 409，
    不双写映射。确认成功后按飞轮纪律回流知识库。

    状态机原子化：建议行 with_for_update() 锁定后再校验状态（confirm×
    dismiss、双 confirm 串行化）；状态翻转（pending→confirmed）在
    save_draft_mappings 之前写入同一 SQLAlchemy 事务 —— save_draft_mappings
    的内部 commit 把「快照映射 + 建议状态」作为一次原子提交落库，任一步
    失败整体回滚，不产生「映射已进草稿而建议仍 pending」的中间态。
    确认成功后 harvest 失败只记日志，不推翻已确认状态。
    """
    import uuid as _uuid

    from app.ontologies.mappings.models import OntologyMappingSuggestion
    from app.ontologies.versions.snapshot_contract import complete_snapshot

    draft = _require_editable_draft(db, ontology_id, version_id)
    # 行锁串行化并发流转：锁定后读到的状态才是当前值（check-then-act 闭环）。
    suggestion = db.query(OntologyMappingSuggestion).filter(
        OntologyMappingSuggestion.id == str(suggestion_id or "").strip(),
        OntologyMappingSuggestion.ontology_id == ontology_id,
        OntologyMappingSuggestion.version_id == version_id,
    ).with_for_update().first()
    if suggestion is None:
        raise HTTPException(404, detail={
            "code": "suggestion_not_found",
            "message": "映射建议不存在或不属于该版本",
        })
    if suggestion.status == "confirmed":
        raise HTTPException(409, detail={
            "code": "already_confirmed",
            "message": "该建议已确认过，映射已在草稿中，请勿重复确认",
            "mappingId": suggestion.confirmed_mapping_id,
        })
    if suggestion.status != "pending":
        raise HTTPException(409, detail={
            "code": "suggestion_not_pending",
            "message": f"该建议已被驳回（{suggestion.status}），不能确认",
        })

    # 与画布「保存配置」完全同形的 mapping 条目（见前端 saveAll 序列化）。
    snapshot = complete_snapshot(draft.snapshot_formal)
    # 锚定复查：提案时锚定的对象必须仍在当前快照中，否则拒绝写悬空映射。
    inventory = _object_inventory(snapshot)
    if not any(obj["id"] == suggestion.object_type_id for obj in inventory):
        raise HTTPException(422, detail={
            "code": "object_not_found",
            "message": (
                f"建议锚定的对象「{suggestion.entity_class}」已不在当前草稿"
                "快照中（可能已被移除），请让 Agent 按现状重新提案"),
        })
    mappings = [dict(item) for item in snapshot["mappings"]]
    field_mapping = dict(suggestion.field_mapping or {})
    if suggestion.primary_key_column:
        field_mapping["__primary_key__"] = suggestion.primary_key_column
    mapping_id = None
    for item in mappings:
        same_dataset = str(item.get("curatedDatasetId")
                           or item.get("curated_dataset_id") or "") == suggestion.dataset_id
        same_target = str(item.get("targetObjectTypeId")
                          or item.get("target_object_type_id") or "") == suggestion.object_type_id
        if same_dataset and same_target:
            merged = dict(item.get("fieldMapping")
                          or item.get("field_mapping") or {})
            merged.update(field_mapping)
            item["fieldMapping"] = merged
            mapping_id = str(item.get("id") or "")
            break
    if mapping_id is None:
        mapping_id = str(_uuid.uuid4())
        mappings.append({
            "id": mapping_id,
            "curatedDatasetId": suggestion.dataset_id,
            "entityClass": suggestion.entity_class,
            "targetObjectTypeId": suggestion.object_type_id,
            "fieldMapping": field_mapping,
            "status": "draft",
            "confidence": 1,
        })

    # 人工确认瞬时建议的服务路径：PUT workspace/mappings → save_draft_mappings
    # （同一守卫、快照校验、revision/hash 推进与审计），baseRevision 与画布
    # 保存同一并发契约。两个注入回调与 versions/router.py 装配到 HTTP 路径的
    # 是同一实现（release_gate_service.raise_publish_errors 即 router 内
    # _raise_publish_errors 的转发目标；_stale_previous_trials 同源
    # trial_service）——直接取 canonical 服务，保持服务层不反向依赖 router。
    from app.ontologies.versions import workspace_service
    from app.ontologies.versions.release_gate_service import raise_publish_errors
    from app.ontologies.versions.trial_service import _stale_previous_trials

    # 状态翻转与快照写入同事务：save_draft_mappings 的内部 commit 把两者作为
    # 一次原子提交；它抛错时整体回滚（建议保持 pending，可修正后重试）。
    suggestion.status = "confirmed"
    suggestion.confirmed_mapping_id = mapping_id
    try:
        saved = workspace_service.save_draft_mappings(
            db,
            ontology_id,
            version_id,
            {
                "mappings": mappings,
                "baseRevision": f"{draft.revision}:{draft.snapshot_hash}",
            },
            current_user,
            _raise_publish_errors=raise_publish_errors,
            _stale_previous_trials=_stale_previous_trials,
        )
    except Exception:
        db.rollback()
        raise

    # 知识飞轮回流：与建议流水线 harvest 人工保存映射同一函数（幂等 upsert）。
    # 失败不推翻已确认状态 —— 回滚 harvest 自身的部分写入并记日志。
    try:
        touched = mapping_knowledge.harvest_snapshot_mappings(
            db, complete_snapshot(draft.snapshot_formal))
    except Exception:  # noqa: BLE001 — 回流失败不影响已确认事实
        db.rollback()
        logger.exception(
            "confirmed suggestion %s knowledge harvest failed", suggestion.id)
        touched = 0

    return {
        "suggestionId": suggestion.id,
        "status": "confirmed",
        "mappingId": mapping_id,
        "datasetId": suggestion.dataset_id,
        "objectTypeId": suggestion.object_type_id,
        "revision": (saved.get("data") or {}).get("revision"),
        "knowledgeHits": touched,
        "message": "建议已确认并写入草稿映射，已随知识飞轮回流",
    }


def dismiss_persistent_suggestion(
    db: Session,
    ontology_id: str,
    version_id: str,
    suggestion_id: str,
    *,
    reason: str = "",
) -> dict:
    """驳回建议（status=dismissed，可带原因）；不触碰草稿快照。

    驳回是队列簿记而非草稿写入，不要求 editable 草稿（冻结版本上的陈旧
    建议同样允许清理）；重复驳回幂等返回现值；已确认建议不可驳回。
    状态翻转用条件更新（WHERE status='pending'）：与并发 confirm 交错时
    更新 0 行，回读现值按终态响应 —— 驳回永不静默推翻确认。
    """
    from app.ontologies.mappings.models import OntologyMappingSuggestion

    _version_or_404(db, ontology_id, version_id)
    suggestion = _suggestion_or_404(db, ontology_id, version_id, suggestion_id)
    if suggestion.status == "confirmed":
        raise HTTPException(409, detail={
            "code": "already_confirmed",
            "message": "该建议已确认并写入草稿映射，不能驳回；如需撤销请编辑映射",
            "mappingId": suggestion.confirmed_mapping_id,
        })
    if suggestion.status == "dismissed":
        return {
            "suggestionId": suggestion.id,
            "status": "dismissed",
            "statusReason": suggestion.status_reason or "",
            "reused": True,
        }
    status_reason = str(reason or "")[:500]
    result = db.execute(
        sa_update(OntologyMappingSuggestion)
        .where(OntologyMappingSuggestion.id == suggestion.id,
               OntologyMappingSuggestion.status == "pending")
        .values(status="dismissed", status_reason=status_reason)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        # 并发翻转：回读现值，按终态响应（已确认 409 / 已驳回幂等返回）
        db.rollback()
        current = _suggestion_or_404(db, ontology_id, version_id, suggestion_id)
        if current.status == "confirmed":
            raise HTTPException(409, detail={
                "code": "already_confirmed",
                "message": "该建议已确认并写入草稿映射，不能驳回；如需撤销请编辑映射",
                "mappingId": current.confirmed_mapping_id,
            })
        return {
            "suggestionId": current.id,
            "status": "dismissed",
            "statusReason": current.status_reason or "",
            "reused": True,
        }
    db.commit()
    return {
        "suggestionId": suggestion.id,
        "status": "dismissed",
        "statusReason": status_reason,
        "reused": False,
    }
