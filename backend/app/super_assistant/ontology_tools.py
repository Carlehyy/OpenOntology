"""超级助手本体工具 — 经白名单门面到达本体域的唯一桥接。

架构边界：本模块是 ``app.super_assistant`` 内唯一允许 import
``app.ontologies.agent_runtime.assistant_facade`` 的文件（架构守卫精确到
模块）；门面只暴露“用”能力（发现/结构/查询/准备/确认执行），没有任何
定义管理入口。会话/意图/来源由执行器（本模块）派生，模型 schema 里不出现。
"""
from __future__ import annotations

import copy
import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.user import User
from app.ontologies.agent_runtime.assistant_facade import (
    FacadeError, build_query_request, catalog_object_types,
    confirmation_status, current_release_map, decide_confirmation,
    execute_confirmation, prepare_action, query, query_page, structure,
    confirmation_owner_lookup, reconcile_confirmation,
    register_join_assertion,
)
from app.super_assistant.models import (
    SuperAssistantConversation, SuperAssistantMessage, SuperAssistantToolRun,
)

logger = logging.getLogger(__name__)

SESSION_KIND = "super_assistant"

ONTOLOGY_CATALOG_TOOL = "ontology_catalog"
ONTOLOGY_STRUCTURE_TOOL = "ontology_structure"
ONTOLOGY_QUERY_TOOL = "ontology_query"
ONTOLOGY_PREPARE_TOOL = "ontology_prepare_action"
ONTOLOGY_EXECUTE_TOOL = "ontology_execute_action"
ONTOLOGY_REGISTER_JOIN_TOOL = "ontology_register_join_assertion"

ONTOLOGY_TOOL_NAMES = {
    ONTOLOGY_CATALOG_TOOL,
    ONTOLOGY_STRUCTURE_TOOL,
    ONTOLOGY_QUERY_TOOL,
    ONTOLOGY_PREPARE_TOOL,
    ONTOLOGY_EXECUTE_TOOL,
    ONTOLOGY_REGISTER_JOIN_TOOL,
}

# 与本体统一查询契约一致：8 源 / 7 关联（覆盖 事件→模块→子产品→产品→产品族 全链）
_MAX_QUERY_SOURCES = 8
_MAX_REF_ONTOLOGIES = 20
# 对象类型目录页大小 20；翻页上界只防病态分页，正常按目标类型早停。
_MAX_REF_CATALOG_PAGES = 64


def _load_user(db: Session, owner_id: str):
    user = db.query(User).filter(User.id == owner_id).first()
    if user is None:
        return None
    return user


def _root_intent_id(db: Session, conversation_id: str) -> str | None:
    """本轮人类意图 = 会话内最近一条用户消息（受认证用户提交事件）。"""
    message = (
        db.query(SuperAssistantMessage)
        .filter(SuperAssistantMessage.conversation_id == conversation_id,
                SuperAssistantMessage.role == "user")
        .order_by(SuperAssistantMessage.created_at.desc())
        .first())
    return message.id if message else None


def _require_conversation(db: Session, owner_id: str, conversation_id: str):
    conversation = db.query(SuperAssistantConversation).filter(
        SuperAssistantConversation.id == conversation_id,
        SuperAssistantConversation.owner_id == owner_id).first()
    return conversation


# ------------------------------------------------------------- schemas

filter_leaf = {
    "type": "object",
    "properties": {
        "source": {"type": "string"},
        "property": {"type": "string"},
        "op": {"type": "string", "enum": [
            "eq", "neq", "gt", "gte", "lt", "lte",
            "contains", "in", "is_null", "is_not_null"]},
        "value": {},
    },
    "required": ["source", "property", "op"],
    "additionalProperties": False,
}
filter_node = {
    "anyOf": [
        filter_leaf,
        {"type": "object", "properties": {
            "and": {"type": "array", "items": {"$ref": "#/$defs/filterNode"}}},
         "required": ["and"], "additionalProperties": False},
        {"type": "object", "properties": {
            "or": {"type": "array", "items": {"$ref": "#/$defs/filterNode"}}},
         "required": ["or"], "additionalProperties": False},
        {"type": "object", "properties": {"not": {"$ref": "#/$defs/filterNode"}},
         "required": ["not"], "additionalProperties": False},
    ],
}


def _query_request_properties(sources_items: dict[str, Any]) -> dict[str, Any]:
    """统一关联查询的公共属性；sources 项按层深注入（叶子或派生源）。

    派生源（subquery）与 QS.QuerySource 对齐：subquery 存在时禁止
    ontologyId/releaseId/objectTypeId 三元组；嵌套最多两层（外层 →
    mid → inner，inner 仅叶子），全部叶子对象源 ≤16 与 lineage 校验由
    服务端负责——schema 只表达形状，不重复执行预算判定。
    """
    return {
        "sources": {
            "type": "array", "minItems": 1, "maxItems": _MAX_QUERY_SOURCES,
            "items": sources_items,
        },
        "joins": {
            "type": "array", "maxItems": 7,
            "items": {
                "type": "object",
                "properties": {
                    "left": {"type": "string"},
                    "right": {"type": "string"},
                    "type": {"type": "string", "enum": ["inner", "left"]},
                    "on": {
                        "anyOf": [
                            {
                                "type": "object",
                                "properties": {
                                    "kind": {"const": "explicit_key"},
                                    "leftProperty": {"type": "string"},
                                    "rightProperty": {"type": "string"},
                                    "keyType": {"type": "string",
                                                "enum": ["string", "number",
                                                         "uuid"]},
                                    "cardinality": {
                                        "type": "string",
                                        "enum": ["1:1", "1:N", "N:1",
                                                 "N:M"]},
                                    "businessMeaning": {"type": "string"},
                                    "evidenceRefs": {
                                        "type": "array",
                                        "items": {"type": "string"}},
                                },
                                "required": ["kind", "leftProperty",
                                             "rightProperty", "keyType",
                                             "cardinality",
                                             "businessMeaning",
                                             "evidenceRefs"],
                                "additionalProperties": False,
                            },
                            {
                                "type": "object",
                                "properties": {
                                    "kind": {"const": "declared_relation"},
                                    "linkTypeId": {"type": "string"},
                                    "direction": {
                                        "type": "string",
                                        "enum": ["source_to_target",
                                                 "target_to_source"]},
                                },
                                "required": ["kind", "linkTypeId",
                                             "direction"],
                                "additionalProperties": False,
                            },
                        ],
                    },
                },
                "required": ["left", "right", "type", "on"],
                "additionalProperties": False,
            },
        },
        "sourceFilters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "filter": filter_node,
                },
                "required": ["source", "filter"],
                "additionalProperties": False,
            },
        },
        "where": {"type": "array", "items": filter_node},
        "select": {
            "type": "array", "maxItems": 32,
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "property": {"type": "string"},
                    "as": {"type": "string"},
                },
                "required": ["source", "property", "as"],
                "additionalProperties": False,
            },
        },
        "groupBy": {
            "type": "array", "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {"source": {"type": "string"},
                               "property": {"type": "string"},
                               "bucket": {"type": "string",
                                          "enum": ["day", "week", "month",
                                                   "quarter", "year"]}},
                "required": ["source", "property"],
                "additionalProperties": False,
            },
        },
        "aggregates": {
            "type": "array", "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "fn": {"type": "string", "enum": [
                        "count", "count_distinct", "sum", "avg", "min",
                        "max"]},
                    "source": {"type": "string"},
                    "property": {"type": "string"},
                    "as": {"type": "string"},
                },
                "required": ["fn", "as"],
                "additionalProperties": False,
            },
        },
        "distinct": {"type": "boolean"},
        "orderBy": {
            "type": "array", "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "output": {"type": "string"},
                    "direction": {"type": "string",
                                  "enum": ["asc", "desc"]},
                    "nulls": {"type": "string",
                              "enum": ["first", "last"]},
                },
                "required": ["output"],
                "additionalProperties": False,
            },
        },
        "pageSize": {"type": "integer", "minimum": 1, "maximum": 200},
    }


_object_source_schema = {
    "type": "object",
    "properties": {
        "alias": {"type": "string"},
        "ontologyId": {"type": "string"},
        "releaseId": {"type": "string"},
        "objectTypeId": {"type": "string"},
    },
    "required": ["alias", "ontologyId", "releaseId", "objectTypeId"],
    "additionalProperties": False,
}


def _derived_source_schema(def_name: str) -> dict[str, Any]:
    """派生源：同源先聚合防 fanout；subquery 内不可再带对象三元组。"""
    return {
        "type": "object",
        "properties": {
            "alias": {"type": "string"},
            "subquery": {"$ref": f"#/$defs/{def_name}"},
        },
        "required": ["alias", "subquery"],
        "additionalProperties": False,
    }


_inner_request_def = {
    "type": "object",
    "properties": _query_request_properties({
        "type": "object",
        "properties": dict(_object_source_schema["properties"]),
        "required": list(_object_source_schema["required"]),
        "additionalProperties": False,
    }),
    "required": ["sources"],
    "additionalProperties": False,
}

_mid_request_def = {
    "type": "object",
    "properties": _query_request_properties({
        "anyOf": [
            _object_source_schema,
            _derived_source_schema("queryRequestInner"),
        ],
    }),
    "required": ["sources"],
    "additionalProperties": False,
}

query_request = {
    "type": "object",
    "properties": _query_request_properties({
        "anyOf": [
            _object_source_schema,
            _derived_source_schema("queryRequestMid"),
        ],
    }),
    "required": ["sources"],
    "additionalProperties": False,
}

_QUERY_REQUEST_DEFS = {
    "filterNode": filter_node,
    "queryRequestMid": _mid_request_def,
    "queryRequestInner": _inner_request_def,
}


def ontology_tool_schemas() -> list[dict[str, Any]]:
    return [
        {
            "name": ONTOLOGY_CATALOG_TOOL,
            "description": (
                "权威 published 本体目录：按当前用户权限列出本体及其当前 "
                "release（含是否有发布文档与可用对象类型概要），或分片读取"
                "某本体发布文档正文（每片约 8000 字符，正文较大时返回 "
                "documentCursor，带同一 ontologyId 续读，不要靠截断硬读）。"
                "大本体类型发现：operation='object_types' 分页列出该本体"
                "全部可用对象类型（每页 20，按 id 排序，cursor 翻页）——"
                "structure 只认已知 typeId，目录概要只给前 10 个，超出时"
                "必须先经此入口补齐类型索引。发现链路：search 确定本体 →"
                "（类型多时）object_types 分页 → structure 读结构 → "
                "ontology_query 查数据；目录为权威源，不要依赖记忆宫殿"
                "图谱做本体发现（图谱异步构建可能缺项）。目录翻页只传 "
                "cursor。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string", "enum": ["object_types"],
                        "description": "对象类型索引分页模式；缺省=目录检索"
                                       "或（带 ontologyId）读发布文档"},
                    "query": {
                        "type": "string",
                        "description": "标题关键词过滤；翻页/取文档时不传"},
                    "cursor": {
                        "type": "string", "minLength": 16,
                        "description": "目录翻页游标；或正文/类型索引续读游标"
                                       "（须同时携带 ontologyId）"},
                    "ontologyId": {"type": "string"},
                    "releaseId": {"type": "string"},
                },
                "oneOf": [
                    {"required": ["ontologyId"]},
                    {"required": ["cursor"], "not": {"required": ["ontologyId"]}},
                    {"not": {"anyOf": [{"required": ["ontologyId"]},
                                       {"required": ["cursor"]}]}},
                ],
                "additionalProperties": False,
            },
        },
        {
            "name": ONTOLOGY_STRUCTURE_TOOL,
            "description": (
                "按需读取本体当前发布的结构片段（对象类型/属性/关系/动作），"
                "并携带准确 releaseId/version 来源证据；只读，不加载完整 schema。"
                "需要先经 discovery 确定 ontologyId 与 releaseId。"
                "objectTypes 的 dataAccess 标注访问模式（remote=联邦远程、"
                "materialized=平台物化）：remote 分组的数据必须经 "
                "ontology_query 的受治理查询（含 subquery 同源聚合）取数，"
                "禁止凭结构或样例在文本里推算远程结果。"
                "响应中的 queryLimits 给出本本体有效的分页上限"
                "（maxPageSize/defaultPageSize，受 profile 与 200 的共同约束），"
                "ontology_query 的新查询分页应据此设置 pageSize。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "ontologyId": {"type": "string"},
                    "releaseId": {"type": "string"},
                    "objectTypeIds": {"type": "array",
                                      "items": {"type": "string"}},
                    "linkTypeIds": {"type": "array",
                                    "items": {"type": "string"}},
                    "actionIds": {"type": "array",
                                  "items": {"type": "string"}},
                },
                "required": ["ontologyId", "releaseId"],
                "additionalProperties": False,
            },
        },
        {
            "name": ONTOLOGY_QUERY_TOOL,
            "description": (
                "统一关联查询：同一本体内多对象类型、或有明确业务键依据的跨本体"
                "关联；服务端完成过滤/投影/聚合/排序与稳定分页。两种等价形态："
                "结构树（sources/joins/...）或 SPL 文本（spl + semanticBindings："
                "绑定名→ontologyId/releaseId/objectTypeId 三元组；with 命名阶段"
                "先同源聚合，最终 from 做联邦 join；join 键必须有本人 "
                "join-assertions 登记的声明证据，否则 422；不支持 limit，分页用 "
                "pageSize）。字段/类型/基数依据必须来自 ontology_structure 的"
                "当前发布结构。新查询可用 pageSize（有效上限见 "
                "ontology_structure 的 queryLimits.maxPageSize，缺省取 "
                "queryLimits.defaultPageSize）；翻页只传 cursor，不要同时携带 "
                "pageSize/params/semanticBindings/queryId。统计类问题优先用 "
                "where 过滤 + aggregates 一次取聚合结论，不要逐页取明细再自行"
                "加总。成功响应回显实际生效的 pageSize；终页（hasNext=false）会"
                "自动释放查询句柄并返回 released/releasedAt，之后不要再沿用旧 "
                "cursor。结构树形态可用派生源（subquery）"
                "先在同源内聚合防 fanout：派生源只给 alias+subquery（禁止再带"
                "对象三元组），嵌套最多两层，全部叶子对象源 ≤16；引用派生输出名"
                "的 lineage 依据由服务端校验。"),
            "parameters": {
                "type": "object",
                # 模块级共享常量按请求深拷贝入目录：禁止跨请求原地修改泄漏
                # （先例见 runtime._WEB_FETCH_TOOL_SCHEMA 的拷贝约定）
                "$defs": copy.deepcopy(_QUERY_REQUEST_DEFS),
                "properties": {
                    **copy.deepcopy(query_request["properties"]),
                    "cursor": {"type": "string", "minLength": 16},
                    "spl": {
                        "type": "string", "minLength": 8, "maxLength": 32768,
                        "description": (
                            "SPL 文本计划：`with 名 = from 源 as 别名 | 命令; "
                            "... from 源 as 别名 | 命令`。每条管道以 fields 或 "
                            "stats 投影收尾（阶段内排序需放在投影之前）；阶段"
                            "只能引用更早阶段或绑定叶子；join 写作 "
                            "`join 源 as 别名 on 左.键 = 右.键 type=left|inner`，"
                            "键必须可追溯到发布属性（聚合输出不可作 join 键）；"
                            "count() 计行、count(字段) 只计非 NULL 值；过滤参数"
                            "用 :名 并在 params 给值"),
                    },
                    "semanticBindings": {
                        "type": "object",
                        "minProperties": 1, "maxProperties": 16,
                        "description": "SPL 源名 → 发布三元组绑定（仅 spl 形态）",
                        "additionalProperties": {
                            "type": "object",
                            "properties": {
                                "ontologyId": {"type": "string"},
                                "releaseId": {"type": "string"},
                                "objectTypeId": {"type": "string"},
                            },
                            "required": ["ontologyId", "releaseId",
                                         "objectTypeId"],
                            "additionalProperties": False,
                        },
                    },
                    "params": {
                        "type": "object",
                        "description": "SPL `:参数` 名 → 值（仅 spl 形态）",
                    },
                },
                "oneOf": [
                    {"required": ["sources"],
                     "not": {"anyOf": [{"required": ["cursor"]},
                                       {"required": ["spl"]}]}},
                    {"required": ["cursor"],
                     "not": {"anyOf": [{"required": ["sources"]},
                                       {"required": ["spl"]}]}},
                    {"required": ["spl", "semanticBindings"],
                     "not": {"anyOf": [{"required": ["sources"]},
                                       {"required": ["cursor"]}]}},
                ],
                "additionalProperties": False,
            },
        },
        {
            "name": ONTOLOGY_PREPARE_TOOL,
            "description": (
                "持久准备一个业务动作确认请求（无业务效果）：展开默认值并校验"
                "参数后返回 confirmationId 与确认卡载荷。执行必须经"
                " ontology_execute_action 且需要用户二次确认。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "ontologyId": {"type": "string"},
                    "releaseId": {"type": "string"},
                    "actionId": {"type": "string"},
                    "targetInstanceId": {"type": "string"},
                    "targetRef": {
                        "type": "object",
                        "description": (
                            "资产/远程对象行定位（无本地实例时必用，与 "
                            "targetInstanceId 互斥）：objectTypeId + 属性名到"
                            "主键值的 key；资产身份由服务端按发布映射派生"),
                        "properties": {
                            "objectTypeId": {"type": "string"},
                            "key": {
                                "type": "object",
                                "description": "主键属性名 → 值（须完整）",
                            },
                        },
                        "required": ["objectTypeId", "key"],
                        "additionalProperties": False,
                    },
                    "parameters": {"type": "object"},
                    "parameterRefs": {"type": "object"},
                },
                "required": ["ontologyId", "releaseId", "actionId"],
                "additionalProperties": False,
            },
        },
        {
            "name": ONTOLOGY_EXECUTE_TOOL,
            "description": (
                "唤醒一个已获用户确认的本体动作（只传 confirmationId）。"
                "该调用本身需要用户在界面上二次确认；确认前不产生任何业务效果。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "confirmationId": {"type": "string"},
                },
                "required": ["confirmationId"],
                "additionalProperties": False,
            },
        },
        {
            "name": ONTOLOGY_REGISTER_JOIN_TOOL,
            "description": (
                "登记一条跨本体业务键关联声明（explicit_key join 的 assertion "
                "证据）。这是用户显式确认的写操作：平台会向用户展示两端本体/"
                "类型/属性、键型与业务含义，批准后才保存，拒绝则不登记。"
                "跨本体 explicit_key 查询前必须已有本人声明（或使用本体声明的 "
                "declared_relation）；不得把用户话里的模糊提及当作授权。"),
            "parameters": {
                "type": "object",
                "properties": {
                    "leftOntologyId": {"type": "string"},
                    "leftObjectTypeId": {"type": "string"},
                    "leftProperty": {"type": "string"},
                    "rightOntologyId": {"type": "string"},
                    "rightObjectTypeId": {"type": "string"},
                    "rightProperty": {"type": "string"},
                    "keyType": {
                        "type": "string", "enum": ["string", "number", "uuid"]},
                    "businessMeaning": {
                        "type": "string",
                        "description": "一两句话说明这个键关联的业务含义"},
                },
                "required": [
                    "leftOntologyId", "leftObjectTypeId", "leftProperty",
                    "rightOntologyId", "rightObjectTypeId", "rightProperty",
                    "keyType", "businessMeaning",
                ],
                "additionalProperties": False,
            },
        },
    ]


# --------------------------------------------- 跨轮结构引用授权校验


def _probe_object_type_scope(
    db: Session, user, ontology_id: str, wanted_types: set[str],
) -> tuple[str | None, set[str]] | None:
    """经门面 catalog_object_types 收集当前允许的对象类型（有界翻页）。

    返回 ``(current_release_id, allowed_object_type_ids)``；无法证明
    （权限/目录异常、分页游标缺失或超出上界）返回 ``None``，调用方
    fail-closed 丢弃该本体的引用。目录 items 已按当前 profile
    ``allowed_object_type_ids`` 过滤，因此这里就是“当前可访问类型”。
    """
    allowed: set[str] = set()
    release_id: str | None = None
    cursor: str | None = None
    for _page in range(_MAX_REF_CATALOG_PAGES):
        try:
            probe = catalog_object_types(
                db, user, ontology_id=ontology_id, cursor=cursor)
        except FacadeError:
            return None
        except Exception:  # noqa: BLE001 - 能力异常按不可授权处理
            logger.warning("跨轮结构引用对象类型校验失败", exc_info=True)
            return None
        if release_id is None:
            release_id = str(probe.get("releaseId") or "") or None
        items = probe.get("items")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                type_id = item.get("id")
                if isinstance(type_id, str) and type_id:
                    allowed.add(type_id)
        if not wanted_types or wanted_types <= allowed:
            # 无类型引用只需本体/发布授权；目标类型已全部找到则无需继续翻页。
            return release_id, allowed
        if not probe.get("hasNext"):
            return release_id, allowed
        next_cursor = probe.get("cursor")
        if not isinstance(next_cursor, str) or not next_cursor:
            return None
        cursor = next_cursor
    return None


def authorize_structure_refs(db: Session, *, owner_id: str,
                             conversation_id: str,
                             refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """跨轮结构引用的当前访问校验（只读、有界、不授予任何权限）。

    ``current_release_map`` 只做发布比较、**不是授权**；这里先按当前 user
    经门面 ``catalog_object_types`` 真正校验访问（活跃用户 + 本体助手菜单
    + 本体可读 + profile 启用 + 当前允许对象类型），未授权本体整组丢弃、
    ``objectTypeId`` 不在当前允许集的历史引用也丢弃（无 objectTypeId 的
    ontology/release 引用保留）；再用发布比较标注 ``stale``（仅比较，不
    替代授权）。任何异常/无法证明的分页按“不可注入”处理。
    """
    user = _load_user(db, owner_id)
    if user is None:
        return []
    if _require_conversation(db, owner_id, conversation_id) is None:
        return []
    ontology_ids: list[str] = []
    wanted_types: dict[str, set[str]] = {}
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        ontology_id = ref.get("ontologyId")
        if not isinstance(ontology_id, str) or not ontology_id:
            continue
        if ontology_id not in wanted_types:
            if len(ontology_ids) >= _MAX_REF_ONTOLOGIES:
                continue
            ontology_ids.append(ontology_id)
            wanted_types[ontology_id] = set()
        object_type_id = ref.get("objectTypeId")
        if isinstance(object_type_id, str) and object_type_id:
            wanted_types[ontology_id].add(object_type_id)
    if not ontology_ids:
        return []
    try:
        current_releases = current_release_map(db, ontology_ids)
    except Exception:  # noqa: BLE001 - 比较失败按不可注入处理
        logger.warning("跨轮结构引用发布比较失败", exc_info=True)
        return []
    scopes: dict[str, tuple[str | None, set[str]]] = {}
    for ontology_id in ontology_ids:
        scope = _probe_object_type_scope(
            db, user, ontology_id, wanted_types.get(ontology_id, set()))
        if scope is not None:
            scopes[ontology_id] = scope
    result: list[dict[str, Any]] = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        ontology_id = ref.get("ontologyId")
        scope = scopes.get(ontology_id)
        if scope is None:
            continue
        probed_release, allowed_types = scope
        object_type_id = ref.get("objectTypeId")
        if object_type_id is not None:
            if (not isinstance(object_type_id, str)
                    or object_type_id not in allowed_types):
                # 同一 release 下撤掉对象类型授权：不注入该历史引用。
                continue
        current_release = current_releases.get(ontology_id) or probed_release
        entry = dict(ref)
        entry["stale"] = bool(
            current_release and entry.get("releaseId") != current_release)
        result.append(entry)
    return result


# ------------------------------------------------------------- executor

def execute_ontology_tool(db: Session, *, owner_id: str, conversation_id: str,
                          name: str, arguments: dict[str, Any],
                          tool_run=None) -> str:
    """内置工具分派入口 — 会话/意图/来源在此派生，不经模型。"""
    user = _load_user(db, owner_id)
    if user is None:
        return json.dumps({"error": "用户不存在"}, ensure_ascii=False)
    if _require_conversation(db, owner_id, conversation_id) is None:
        return json.dumps({"error": "会话不存在"}, ensure_ascii=False)
    try:
        if name == ONTOLOGY_REGISTER_JOIN_TOOL:
            return _register_join_assertion_from_tool_run(
                db, user, conversation_id=conversation_id, tool_run=tool_run)
        if name == ONTOLOGY_CATALOG_TOOL:
            return _execute_catalog_tool(db, user, arguments)
        if name == ONTOLOGY_STRUCTURE_TOOL:
            data = structure(
                db, user,
                str(arguments.get("ontologyId") or ""),
                release_id=str(arguments.get("releaseId") or "") or None,
                object_type_ids=arguments.get("objectTypeIds") or None,
                link_type_ids=arguments.get("linkTypeIds") or None,
                action_ids=arguments.get("actionIds") or None)
            return json.dumps(data, ensure_ascii=False, default=str)
        if name == ONTOLOGY_QUERY_TOOL:
            # 统一先经门面请求模型校验：cursor 混用 pageSize/params/
            # semanticBindings/queryId/spl/sources 与 0/201/非法类型
            # pageSize 必须在分派前返回可纠正 422，而不是按 cursor 静默
            # 丢参；合法续查只按校验后的 CursorRequest 取页。
            request = build_query_request(arguments)
            cursor = getattr(request, "cursor", None)
            if cursor is not None:
                data = query_page(db, user, str(cursor),
                                  session_kind=SESSION_KIND,
                                  session_id=conversation_id)
            else:
                data = query(db, user, request,
                             session_kind=SESSION_KIND,
                             session_id=conversation_id)
            return json.dumps(data, ensure_ascii=False, default=str)
        if name == ONTOLOGY_PREPARE_TOOL:
            root_intent_id = _root_intent_id(db, conversation_id)
            if root_intent_id is None:
                return json.dumps(
                    {"error": "会话内没有用户消息，无法建立确认意图"},
                    ensure_ascii=False)
            data = prepare_action(
                db, user,
                str(arguments.get("ontologyId") or ""),
                session_kind=SESSION_KIND,
                session_id=conversation_id,
                root_intent_id=root_intent_id,
                source_call_id=f"{conversation_id}:{root_intent_id}:"
                               f"{arguments.get('actionId')}",
                action_id=str(arguments.get("actionId") or ""),
                release_id=str(arguments.get("releaseId") or ""),
                target_instance_id=arguments.get("targetInstanceId"),
                target_ref=arguments.get("targetRef") or None,
                parameters=arguments.get("parameters") or {},
                parameter_refs=arguments.get("parameterRefs") or None)
            data = dict(data)
            data["next"] = (
                "调用 ontology_execute_action{confirmationId} 交由用户在界面"
                "二次确认；确认前不会执行。")
            return json.dumps(data, ensure_ascii=False, default=str)
        if name == ONTOLOGY_EXECUTE_TOOL:
            cid = str(arguments.get("confirmationId") or "")
            data = execute_confirmation(
                db, user, confirmation_owner_lookup(db, user, cid), cid,
                session_kind=SESSION_KIND, session_id=conversation_id)
            return json.dumps(data, ensure_ascii=False, default=str)
    except FacadeError as exc:
        return json.dumps({"error": exc.message, "code": exc.code,
                           **exc.extra}, ensure_ascii=False, default=str)
    except Exception as exc:  # noqa: BLE001 - 工具错误不冒泡为对话失败
        logger.warning("ontology tool %s failed (%s)", name, type(exc).__name__)
        return json.dumps({"error": "ontology_tool_failed"}, ensure_ascii=False)
    return json.dumps({"error": f"未知工具 {name}"}, ensure_ascii=False)


_JOIN_ASSERTION_FIELDS = (
    "leftOntologyId", "leftObjectTypeId", "leftProperty",
    "rightOntologyId", "rightObjectTypeId", "rightProperty",
    "keyType", "businessMeaning",
)


def _execute_catalog_tool(db: Session, user, arguments: dict[str, Any]) -> str:
    """权威 published 本体目录/文档读取（经门面）。

    目录与文档端口由 ontologies 域的 agent_runtime/catalog 提供并按
    profile + agent 权限过滤；本域只做白名单分派。能力未启用（门面未
    导出）时明确报不可用，不 fallback 到宫殿图谱等非权威源。
    """
    try:
        from app.ontologies.agent_runtime.assistant_facade import (
            catalog_get_document, catalog_object_types, catalog_search,
        )
    except ImportError:
        logger.warning("facade catalog 能力不可用", exc_info=True)
        return json.dumps({
            "error": "ontology_catalog_unavailable",
            "message": "本体目录能力暂不可用，请稍后重试",
        }, ensure_ascii=False)
    try:
        cursor = str(arguments["cursor"]) if arguments.get("cursor") else None
        if arguments.get("operation") == "object_types":
            data = catalog_object_types(
                db, user,
                ontology_id=str(arguments["ontologyId"]),
                release_id=str(arguments.get("releaseId") or "") or None,
                cursor=cursor)
        elif arguments.get("ontologyId"):
            data = catalog_get_document(
                db, user,
                ontology_id=str(arguments["ontologyId"]),
                release_id=str(arguments.get("releaseId") or "") or None,
                cursor=cursor)
        else:
            data = catalog_search(
                db, user,
                query=str(arguments.get("query") or "") or None,
                cursor=cursor)
        return json.dumps(data, ensure_ascii=False, default=str)
    except FacadeError as exc:
        return json.dumps({"error": exc.message, "code": exc.code,
                           **exc.extra}, ensure_ascii=False, default=str)
    except Exception as exc:  # noqa: BLE001 - 工具错误不冒泡为对话失败
        logger.warning("ontology catalog 执行失败", exc_info=True)
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


def execution_result_status(output: str) -> str:
    try:
        payload = json.loads(output)
        state = payload.get("state") if isinstance(payload, dict) else None
    except (TypeError, ValueError):
        state = None
    if state == "succeeded":
        return "success"
    if state in {"confirmed", "dispatch_queued", "executing", "recovering"}:
        return "running"
    if state in {"unknown", "compensation_required", "pending_approval",
                 "denied", "cancelled", "expired", "approval_rejected",
                 "failed_not_executed", "prepared"}:
        return state
    return "error"


def refresh_action_steps(db: Session, owner_id: str, conversation_id: str,
                         messages: list[SuperAssistantMessage]) -> None:
    """历史步骤读取持久业务状态；状态读取不会重新派发动作。"""
    if not any(step.get("toolName") == ONTOLOGY_EXECUTE_TOOL
               for message in messages for step in message.steps or []):
        return
    runs = {run.id: run for run in db.query(SuperAssistantToolRun).filter(
        SuperAssistantToolRun.conversation_id == conversation_id,
        SuperAssistantToolRun.tool_name == ONTOLOGY_EXECUTE_TOOL,
        SuperAssistantToolRun.status != "awaiting_confirmation",
    ).all()}
    changed = False
    for message in messages:
        steps = []
        for step in message.steps or []:
            run = runs.get(step.get("toolRunId"))
            if run is not None:
                confirmation = confirmation_display_for_run(
                    db, owner_id=owner_id, conversation_id=conversation_id,
                    tool_run=run)
                status = (execution_result_status(json.dumps(confirmation))
                          if confirmation is not None else "unknown")
                preview = json.dumps({
                    "confirmationId": (run.arguments or {}).get("confirmationId"),
                    "state": confirmation.get("state") if confirmation else "unknown",
                    "failureCode": confirmation.get("failureCode")
                    if confirmation else "confirmation_state_unavailable",
                    # 资产写真实结果（curated pending_review 等）；本地动作为
                    # None — UI 按 state+outcome 区分"已提交待审"与"已生效"。
                    "outcome": confirmation.get("outcome") if confirmation else None,
                }, ensure_ascii=False)
                step = {**step, "status": status, "preview": preview}
            steps.append(step)
        if steps != (message.steps or []):
            message.steps = steps
            changed = True
    if changed:
        db.commit()


def join_assertion_display(tool_run) -> dict | None:
    """确认卡展示载荷：只从冻结的 tool_run 参数构造，两端/键型/含义齐备。"""
    arguments = tool_run.arguments or {}
    display = {key: arguments.get(key) for key in _JOIN_ASSERTION_FIELDS}
    if any(not str(display[key] or "").strip() for key in _JOIN_ASSERTION_FIELDS):
        return None
    if display.get("keyType") not in ("string", "number", "uuid"):
        return None
    return display


def _register_join_assertion_from_tool_run(db: Session, user,
                                           *, conversation_id: str,
                                           tool_run) -> str:
    """登记跨本体关联声明 — 只认服务端 approved 的 tool_run，拒绝可伪造输入。

    绑定校验：tool_run 必须是本会话内 requires_confirmation 的本次调用且
    状态为 approved（用户在界面批准）；登记参数只取 tool_run 冻结行，不从
    工具入参读取——模型无法凭话术或自授字段触发登记。
    """
    if tool_run is None or not tool_run.requires_confirmation:
        return json.dumps({
            "error": "join_assertion_requires_user_approval",
            "message": "跨本体关联声明必须经用户在界面二次确认后才会保存",
        }, ensure_ascii=False)
    if tool_run.conversation_id != conversation_id:
        return json.dumps({"error": "会话不一致，拒绝登记"}, ensure_ascii=False)
    if tool_run.status != "approved":
        return json.dumps({
            "error": "join_assertion_not_approved",
            "message": f"声明尚未获用户批准（当前状态 {tool_run.status}），不登记",
        }, ensure_ascii=False)
    display = join_assertion_display(tool_run)
    if display is None:
        return json.dumps({
            "error": "join_assertion_invalid_payload",
            "message": "声明参数不完整（两端本体/类型/属性、键型、业务含义）",
        }, ensure_ascii=False)
    try:
        data = register_join_assertion(db, user, {
            "left_ontology_id": str(display["leftOntologyId"]),
            "left_object_type_id": str(display["leftObjectTypeId"]),
            "left_property": str(display["leftProperty"]),
            "right_ontology_id": str(display["rightOntologyId"]),
            "right_object_type_id": str(display["rightObjectTypeId"]),
            "right_property": str(display["rightProperty"]),
            "key_type": str(display["keyType"]),
            "business_meaning": str(display["businessMeaning"]),
        })
        data = dict(data)
        data["next"] = (
            "声明已保存；现在可以用该 explicit_key 关联发起 ontology_query。")
        return json.dumps(data, ensure_ascii=False, default=str)
    except FacadeError as exc:
        return json.dumps({"error": exc.message, "code": exc.code,
                           **exc.extra}, ensure_ascii=False, default=str)
    except ImportError as exc:
        logger.error("facade register_join_assertion 不可用", exc_info=True)
        return json.dumps({
            "error": "join_assertion_capability_unavailable",
            "message": "声明登记能力暂不可用，请稍后重试",
        }, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 - 工具错误不冒泡为对话失败
        logger.warning("join assertion 登记失败", exc_info=True)
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


def decide_from_tool_run(db: Session, current_user, tool_run, decision: str):
    """tool-run approve/deny → 同一确认协议的一次用户二次确认。"""
    arguments = tool_run.arguments or {}
    cid = str(arguments.get("confirmationId") or "")
    ontology_id = confirmation_owner_lookup(db, current_user, cid)
    try:
        status = confirmation_status(
            db, current_user, ontology_id, cid,
            session_kind=SESSION_KIND, session_id=tool_run.conversation_id)
        if decision not in ("approve", "deny"):
            raise FacadeError(422, "invalid_parameter", "未知决策类型")
        return decide_confirmation(
            db, current_user, ontology_id, cid,
            "approve" if decision == "approve" else "deny",
            session_kind=SESSION_KIND, session_id=tool_run.conversation_id,
            decision_id=f"tool-run:{tool_run.id}",
            payload_hash=status["payloadHash"])
    except FacadeError as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=exc.status, detail=exc.detail()) from exc


def confirmation_display_for_run(db: Session, *, owner_id: str,
                                 conversation_id: str, tool_run) -> dict | None:
    """direct 确认卡的服务端持久展示载荷（SSE 与历史恢复共用）。

    经白名单门面按 ``session_kind=super_assistant`` + 会话 + 归属用户读取
    真实确认记录，返回 ``confirmation_out`` 原样载荷（含
    ontology/action/target/release/最终参数/requiresApproval 等完整 display）；
    记录不存在、跨主体/跨会话一律 None——模型与前端都不能凭 confirmationId
    编造确认内容，也不得回退为可批准的裸 ID 卡。
    """
    cid = str((tool_run.arguments or {}).get("confirmationId") or "")
    if not cid:
        return None
    user = _load_user(db, owner_id)
    if user is None:
        return None
    ontology_id = confirmation_owner_lookup(db, user, cid)
    if not ontology_id:
        return None
    try:
        return confirmation_status(
            db, user, ontology_id, cid,
            session_kind=SESSION_KIND, session_id=conversation_id)
    except FacadeError:
        return None


# ------------------------------------------------- kernel gated 写映射（P2-5）

ONTOLOGY_EXECUTE_CAPABILITY = "ontology:confirmed_execute"
ONTOLOGY_READ_CAPABILITIES = (
    "ontology.discover", "ontology.structure", "ontology.query",
)

_KERNEL_SESSION_KIND = "super_assistant"


def build_ontology_execute_request(*, confirmation_id: str, session_id: str,
                                   ontology_id: str, payload_hash: str) -> str:
    """kernel 侧 gated Call 的输入快照（JSON，Approval.parameter_hash 锚）。"""
    return json.dumps({
        "capability": ONTOLOGY_EXECUTE_CAPABILITY,
        "confirmationId": str(confirmation_id),
        "sessionKind": _KERNEL_SESSION_KIND,
        "sessionId": str(session_id),
        "ontologyId": str(ontology_id),
        "payloadHash": str(payload_hash),
    }, ensure_ascii=False, sort_keys=True)


def parse_ontology_execute_request(input_ref) -> dict | None:
    """解析 gated Call 输入；非法/非本体请求返回 None。"""
    try:
        payload = json.loads(str(input_ref or ""))
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("capability") != ONTOLOGY_EXECUTE_CAPABILITY:
        return None
    required = ("confirmationId", "sessionId", "ontologyId", "payloadHash")
    if any(not str(payload.get(key) or "").strip() for key in required):
        return None
    return payload


def kernel_decide_confirmation(db: Session, user, request: dict,
                               decision: str, decision_id: str) -> dict:
    """kernel Approval decision → 确认协议的一次用户二次确认（approve/deny）。

    decision_id 绑定 kernel 审批 id：重复命令经确认协议重放，不产生第二份
    确认决策。payloadHash 来自 Approval.parameter_hash 锚定的输入快照。
    """
    return decide_confirmation(
        db, user, str(request["ontologyId"]),
        str(request["confirmationId"]),
        "approve" if decision == "approved" else "deny",
        session_kind=str(request.get("sessionKind") or _KERNEL_SESSION_KIND),
        session_id=str(request["sessionId"]),
        decision_id=decision_id,
        payload_hash=str(request["payloadHash"]))


def kernel_execute_confirmation(db: Session, user, request: dict) -> dict:
    """outbox 消费侧：唤醒已批准确认（只派发，绝不代 approve）。"""
    return execute_confirmation(
        db, user, str(request["ontologyId"]),
        str(request["confirmationId"]),
        session_kind=str(request.get("sessionKind") or _KERNEL_SESSION_KIND),
        session_id=str(request["sessionId"]))


def kernel_confirmation_status(db: Session, user, request: dict) -> dict | None:
    """收敛扫描用：读取确认真实状态（unknown 不随读执行）。"""
    try:
        return confirmation_status(
            db, user, str(request["ontologyId"]),
            str(request["confirmationId"]),
            session_kind=str(request.get("sessionKind") or _KERNEL_SESSION_KIND),
            session_id=str(request["sessionId"]))
    except FacadeError:
        return None


def kernel_reconcile_confirmation(db: Session, user, request: dict) -> dict | None:
    """kernel 收敛扫描用：先推进确认对账再取真实状态。"""
    try:
        return reconcile_confirmation(
            db, user, str(request["ontologyId"]),
            str(request["confirmationId"]),
            session_kind=str(request.get("sessionKind")
                             or _KERNEL_SESSION_KIND),
            session_id=str(request["sessionId"]))
    except FacadeError:
        return None
