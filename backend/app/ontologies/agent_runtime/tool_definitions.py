"""本体助手工具目录（TOOL_DEFS）— 与执行实现解耦的单一工具清单。

toolkit.py 保留 ``from .tool_definitions import TOOL_DEFS`` 的既有导入契约；
本模块只声明 LLM 可见的工具名/描述/参数 JSON Schema，不包含执行逻辑。
"""
from __future__ import annotations

from app.ontologies.agent_runtime.limits import limit as runtime_limit


# ---------------------------------------------------------------- 工具定义

TOOL_DEFS: list[dict] = [
    {
        "name": "search_objects",
        "description": "在指定对象类型下检索实例。支持属性条件过滤（filters）与全文关键词（keyword）。回答任何关于'有哪些/多少/是什么'的问题前都应先用本工具查证，不要凭空作答。",
        "parameters": {
            "type": "object",
            "properties": {
                "object_type": {"type": "string", "description": "对象类型的 name 或显示名"},
                "filters": {
                    "type": "array",
                    "description": "属性过滤条件，AND 关系",
                    "items": {
                        "type": "object",
                        "properties": {
                            "property": {"type": "string", "description": "属性 name"},
                            "op": {"type": "string", "enum": ["eq", "neq", "gt", "gte", "lt", "lte", "contains", "in"]},
                            "value": {"description": "比较值；op=in 时为数组"},
                        },
                        "required": ["property", "op", "value"],
                    },
                },
                "keyword": {"type": "string", "description": "在所有属性值中做包含匹配的关键词"},
                "limit": {"type": "integer", "description": "返回条数上限（受授权配额约束）"},
            },
            "required": ["object_type"],
        },
    },
    {
        "name": "get_object",
        "description": "查看单个对象实例的全部属性、派生属性，以及它在各链接类型上的邻居概览。",
        "parameters": {
            "type": "object",
            "properties": {
                "instance_id": {"type": "string", "description": "对象实例 id"},
            },
            "required": ["instance_id"],
        },
    },
    {
        "name": "traverse_links",
        "description": "从一个对象实例出发，沿指定链接类型遍历，返回相邻对象。用于回答'X 关联了哪些 Y'类问题。",
        "parameters": {
            "type": "object",
            "properties": {
                "instance_id": {"type": "string", "description": "起点对象实例 id"},
                "link_type": {"type": "string", "description": "链接类型的 name 或显示名"},
                "direction": {"type": "string", "enum": ["out", "in"], "description": "out=作为源对象沿链接向外, in=作为目标对象反向"},
                "limit": {"type": "integer"},
            },
            "required": ["instance_id", "link_type"],
        },
    },
    {
        "name": "traverse_path",
        "description": "从一个起点对象出发，沿指定的链接类型序列做多跳遍历，返回终点对象。用于回答'X 经由 A 再经由 B 关联到哪些 Z'这类多跳 / 影响分析 / 血缘追溯问题。单跳用 traverse_links 即可；本工具用于 2 跳及以上。已内置扇出上限与节点预算，超限会截断并标记。",
        "parameters": {
            "type": "object",
            "properties": {
                "instance_id": {"type": "string", "description": "起点对象实例 id"},
                "path": {
                    "type": "array",
                    "description": f"有序的跳序列，每一跳指定一个链接类型和方向；最多 {runtime_limit('max_hops')} 跳",
                    "items": {
                        "type": "object",
                        "properties": {
                            "link_type": {"type": "string", "description": "链接类型的 name 或显示名"},
                            "direction": {"type": "string", "enum": ["out", "in"], "description": "out=沿链接向外, in=反向；默认 out"},
                        },
                        "required": ["link_type"],
                    },
                },
                "limit": {"type": "integer", "description": "返回终点对象的条数上限（受授权配额约束）"},
            },
            "required": ["instance_id", "path"],
        },
    },
    {
        "name": "find_paths",
        "description": "查找两个具体对象实例之间的最短关系路径，并返回可供图谱高亮的节点与边。用户明确问‘A 到 B 有哪些路径/如何关联’时使用；如果只有名称，先用 search_objects 找到两端 instance_id。",
        "parameters": {
            "type": "object",
            "properties": {
                "source_instance_id": {"type": "string", "description": "起点实例 id"},
                "target_instance_id": {"type": "string", "description": "终点实例 id"},
                "direction": {"type": "string", "enum": ["both", "outgoing", "incoming"], "description": "both=忽略遍历方向寻找连接，outgoing=只沿关系正向，incoming=只沿关系反向"},
                "max_depth": {"type": "integer", "minimum": 1, "maximum": 6},
                "max_paths": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            "required": ["source_instance_id", "target_instance_id"],
        },
    },
    {
        "name": "analyze_change_impact",
        "description": "只读预演某实例字段的拟议变化，沿真实关系计算直接/间接可达范围，并返回可供图谱分层标亮的节点与边。它不修改数据；结果只代表关联范围而非确定因果，回答时必须保留该边界。",
        "parameters": {
            "type": "object",
            "properties": {
                "instance_id": {"type": "string", "description": "拟议变更的实例 id"},
                "property": {"type": "string", "description": "拟议变更的属性 name 或显示名"},
                "proposed_value": {"description": "拟议的新值，可为字符串、数字、布尔或空值"},
                "direction": {"type": "string", "enum": ["both", "outgoing", "incoming"]},
                "max_depth": {"type": "integer", "minimum": 1, "maximum": 4},
            },
            "required": ["instance_id", "property", "proposed_value"],
        },
    },
    {
        "name": "aggregate_objects",
        "description": "对某对象类型做聚合统计：count/sum/avg/min/max，可按属性分组（支持多维）。分组结果附结构化 groupValues 与服务端签名 drilldown（时间桶边界由服务端按日历计算）。已发布的远程绑定对象自动通过统一查询在真实源上统计。趋势/按日周月季年统计：group_by 用 {\"property\": 时间属性, \"bucket\": \"day|week|month|quarter|year\"}，一次取全期间，禁止逐月逐日多次查询；月度趋势等按时间排序用 order_by={\"output\":\"group\",\"direction\":\"asc\"}。日期范围过滤用 gte 起始日期、lt 下一期起始日期。contains 会先在源端模糊枚举取值解析为 in（取值过多无法解析时会提示改用 in/eq）。远程结果为 0 时附 dataCoverage 说明原因，回答时须如实转述。回答统计结论时须注明数据时间范围与截止日期（用 get_data_time_range 获取）。明细列表、按 ID 查单条、排序分页请用 query_objects。completeness 只代表查询执行完成，不代表数据时间覆盖完整。",
        "parameters": {
            "type": "object",
            "properties": {
                "object_type": {"type": "string"},
                "metric": {"type": "string", "enum": ["count", "sum", "avg", "min", "max"]},
                "metric_property": {"type": "string", "description": "metric 非 count 时必填：参与计算的属性 name（min/max 也可用 date/datetime 属性）"},
                "group_by": {
                    "description": "可选：分组维度。字符串=按属性分组；数组=多维分组，每项为属性名或 {property, bucket}（bucket: day/week/month/quarter/year 时间分桶，仅远程绑定对象支持）",
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {
                            "oneOf": [
                                {"type": "string"},
                                {"type": "object", "properties": {
                                    "property": {"type": "string"},
                                    "bucket": {"type": "string", "enum": ["day", "week", "month", "quarter", "year"]}},
                                    "required": ["property"]},
                            ],
                        }},
                    ],
                },
                "order_by": {
                    "type": "object",
                    "description": "可选：分组结果排序，默认按值降序（TopN）；趋势类建议 {\"output\": \"group\", \"direction\": \"asc\"} 按时间升序",
                    "properties": {
                        "output": {"type": "string", "enum": ["value", "group"]},
                        "direction": {"type": "string", "enum": ["asc", "desc"]},
                    },
                    "required": ["output"],
                },
                "filters": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "property": {"type": "string"},
                            "op": {"type": "string", "enum": ["eq", "neq", "gt", "gte", "lt", "lte", "contains", "in"]},
                            "value": {},
                        },
                        "required": ["property", "op", "value"],
                    },
                },
            },
            "required": ["object_type", "metric"],
        },
    },
    {
        "name": "query_objects",
        "description": "统一查询工具：明细列表、按 ID 查单条、排序、分页、多维分组、多指标、沿已发布关系的关联统计，以及受治理的多阶段 SPL/派生查询。远程绑定对象直接在真实源执行（group_by 支持 bucket=day/week/month/quarter/year），本地物化对象走本地快照。按 ID 查单条：filters 用主键属性 eq。关联统计：joins 给沿关系链的对象类型与链接类型序列，服务端解析为已发布关系 join。相对时间：不要自己算当前日期，传 window={property, timezone, last:{unit,count}} 或 {property,timezone,start,end}，服务端固定 now 并返回 resolvedWindow（绝对 [start,end)，timezone 显式）。SPL/多阶段派生：传 spl + semanticBindings（只需 {alias:{objectType}}，服务端钉定当前 ontology/release）+ params+pageSize，禁止任意 SQL、禁止跨 profile/release。分组结果包含结构化 groupValues 和签名 drilldown（多维/NULL/时间桶边界均由服务端处理）。分页：返回 hasNext/cursor 时用同一次执行的 cursor 续页，公开展示不含 cursor；结果另带可重放 querySpec。回答统计结论时须注明 resolvedWindow/dataCoverage 与数据截止日期（get_data_time_range）。",
        "parameters": {
            "type": "object",
            "properties": {
                "object_type": {"type": "string", "description": "主对象类型 name 或显示名"},
                "joins": {
                    "type": "array",
                    "description": "可选：沿关系链依次引入的关联对象",
                    "items": {
                        "type": "object",
                        "properties": {
                            "object_type": {"type": "string", "description": "关联对象类型"},
                            "link_type": {"type": "string", "description": "连接上一对象的链接类型 name 或显示名"},
                            "type": {"type": "string", "enum": ["inner", "left"], "description": "默认 inner"},
                        },
                        "required": ["object_type", "link_type"],
                    },
                },
                "filters": {
                    "type": "array",
                    "description": "可选：过滤条件（AND）；object_type 缺省为主对象",
                    "items": {
                        "type": "object",
                        "properties": {
                            "object_type": {"type": "string"},
                            "property": {"type": "string"},
                            "op": {"type": "string", "enum": ["eq", "neq", "gt", "gte", "lt", "lte", "contains", "in", "is_null", "is_not_null"]},
                            "value": {"description": "op=in 时为数组；is_null/is_not_null 时省略"},
                        },
                        "required": ["property", "op"],
                    },
                },
                "select": {
                    "type": "array",
                    "description": "可选：输出属性（property 或 {object_type, property}）；缺省为主对象全部授权属性",
                    "items": {},
                },
                "group_by": {
                    "type": "array",
                    "description": "可选：分组维度（多维），每项为 {object_type?, property, bucket?}",
                    "items": {
                        "type": "object",
                        "properties": {
                            "object_type": {"type": "string"},
                            "property": {"type": "string"},
                            "bucket": {"type": "string", "enum": ["day", "week", "month", "quarter", "year"]},
                        },
                        "required": ["property"],
                    },
                },
                "aggregates": {
                    "type": "array",
                    "description": "可选：聚合指标（可多个），每项 {fn, object_type?, property?, as?}",
                    "items": {
                        "type": "object",
                        "properties": {
                            "fn": {"type": "string", "enum": ["count", "count_distinct", "sum", "avg", "min", "max"]},
                            "object_type": {"type": "string"},
                            "property": {"type": "string"},
                            "as": {"type": "string", "description": "输出名，缺省为 fn 或 fn_property"},
                        },
                        "required": ["fn"],
                    },
                },
                "order_by": {
                    "type": "array",
                    "description": "可选：排序（输出属性名或聚合 as 名），如 [{output: \"created_at\", direction: \"desc\"}]",
                    "items": {
                        "type": "object",
                        "properties": {
                            "output": {"type": "string"},
                            "direction": {"type": "string", "enum": ["asc", "desc"]},
                        },
                        "required": ["output"],
                    },
                },
                "page_size": {"type": "integer", "description": "单页行数（受授权配额约束，≤200）"},
                "cursor": {"type": "string", "description": "续页游标（上次返回的 cursor）"},
                "window": {
                    "type": "object",
                    "description": "服务端相对时间窗口（不要自算当前时间）。last 与 start/end 二选一；timezone 必填；服务端固定 now 并冻结为绝对 [start,end)。",
                    "properties": {
                        "object_type": {"type": "string", "description": "窗口作用的对象类型；缺省主对象"},
                        "property": {"type": "string", "description": "时间属性 name/显示名"},
                        "timezone": {"type": "string", "description": "IANA 时区，如 Asia/Shanghai"},
                        "last": {
                            "type": "object",
                            "properties": {
                                "unit": {"type": "string", "enum": ["second", "minute", "hour", "day", "week", "month", "quarter", "year"]},
                                "count": {"type": "integer", "minimum": 1, "maximum": 1000},
                            },
                            "required": ["unit", "count"],
                        },
                        "start": {"type": "string", "description": "绝对起点 ISO-8601（与 end 同时提供）"},
                        "end": {"type": "string", "description": "绝对终点 ISO-8601，左闭右开"},
                    },
                    "required": ["property", "timezone"],
                },
                "spl": {
                    "type": "string",
                    "description": "可选：受治理 SPL 文本（with 命名阶段/join/stats/where/sort）。与结构树参数互斥；不能包含任意 SQL。",
                },
                "semanticBindings": {
                    "type": "object",
                    "description": "SPL 叶子语义绑定：alias -> {objectType}；服务端固定当前 ontology/release 并复验授权",
                    "additionalProperties": {
                        "type": "object",
                        "properties": {"objectType": {"type": "string"}},
                        "required": ["objectType"],
                    },
                },
                "params": {"type": "object", "description": "SPL :参数值（可选）"},
            },
            "oneOf": [
                {"required": ["object_type"]},
                {"required": ["spl", "semanticBindings"]},
            ],
        },
    },
    {
        "name": "analyze_sequences",
        "description": "窗口内主体/序列分析（有界、确定性）。默认 mode=distinct：回答“窗口内有多少个/是否 ≥N 个不同主体”，服务端用 count_distinct，不下载明细；重复事件只计一次；distinct 可带 group_by 按任意本体属性/时间桶分组（例如按 module、region 统计窗口内去重登记人，module 只是字段示例，不是平台特例）。只有用户明确说“连续/间隔/顺序”时才用 mode=consecutive|interval|ordered，绝不能把这类问题冒充普通 count。连续/间隔需要 max_gap_seconds（可选 min_gap_seconds）；ordered 需要 sequence=[{filters:[...]}] 阶段。候选事件超过物化预算会显式拒绝（sequence_budget_exceeded），不会截断后给确定结论。window 与 query_objects 相同（timezone 显式、服务端 now、绝对 [start,end)）。",
        "parameters": {
            "type": "object",
            "properties": {
                "object_type": {"type": "string", "description": "事件对象类型"},
                "mode": {"type": "string", "enum": ["distinct", "consecutive", "interval", "ordered"], "description": "默认 distinct；明确连续/间隔/顺序才改用对应模式"},
                "subject_property": {"type": "string", "description": "主体属性（如登记人/用户），缺省时必填"},
                "time_property": {"type": "string", "description": "事件时间属性"},
                "threshold": {"type": "integer", "minimum": 1, "description": "默认 N（distinct=去重主体数；序列=满足模式的主体数）"},
                "filters": {
                    "type": "array",
                    "description": "事件过滤（AND），如 action=register",
                    "items": {
                        "type": "object",
                        "properties": {
                            "property": {"type": "string"},
                            "op": {"type": "string", "enum": ["eq", "neq", "gt", "gte", "lt", "lte", "contains", "in", "is_null", "is_not_null"]},
                            "value": {},
                        },
                        "required": ["property", "op"],
                    },
                },
                "window": {
                    "type": "object",
                    "description": "相对/绝对时间窗口；timezone 必填，服务端固定 now",
                    "properties": {
                        "property": {"type": "string"},
                        "timezone": {"type": "string"},
                        "last": {"type": "object", "properties": {"unit": {"type": "string", "enum": ["second", "minute", "hour", "day", "week", "month", "quarter", "year"]}, "count": {"type": "integer", "minimum": 1, "maximum": 1000}}, "required": ["unit", "count"]},
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                    },
                    "required": ["property", "timezone"],
                },
                "group_by": {
                    "type": "array",
                    "description": "distinct 模式可选：按属性/时间桶分组统计去重主体数（泛化分组，如按模块、区域；属性名只是本体字段）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "property": {"type": "string"},
                            "bucket": {"type": "string", "enum": ["day", "week", "month", "quarter", "year"]},
                        },
                        "required": ["property"],
                    },
                },
                "max_gap_seconds": {"type": "integer", "minimum": 0, "description": "consecutive/interval 的相邻事件最大间隔"},
                "min_gap_seconds": {"type": "integer", "minimum": 0, "description": "interval 的最小间隔，默认 0"},
                "sequence": {
                    "type": "array",
                    "description": "ordered 模式：按顺序命中的阶段过滤器列表",
                    "items": {
                        "type": "object",
                        "properties": {"filters": {"type": "array", "items": {"type": "object"}}},
                        "required": ["filters"],
                    },
                },
                "within_seconds": {"type": "integer", "minimum": 1, "description": "ordered 模式首末阶段的最大总跨度（秒）"},
            },
            "required": ["object_type", "subject_property", "time_property", "mode"],
        },
    },
    {
        "name": "describe_objects",
        "description": "结构发现工具：先看清授权对象类型，再查询。object_types 省略时列出全部授权类型摘要（按页返回，hasMore/next）；传入 1-8 个类型（name/显示名/id）时返回完整授权 properties、已发布 links 与 actions 摘要。只使用当前 AgentProfile 作用域，不泄露隐藏类型/属性，不统计实例数。",
        "parameters": {
            "type": "object",
            "properties": {
                "object_types": {
                    "type": "array",
                    "description": "可选：1-8 个对象类型；省略则列授权类型摘要",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 8,
                },
                "next": {"type": "string", "description": "摘要分页令牌（上次返回的 next）"},
            },
        },
    },
    {
        "name": "get_data_time_range",
        "description": "查询远程绑定对象的数据时间范围（源端一次 min/max 聚合）：返回数据实际覆盖的起止日期。回答趋势/统计结论前先调用本工具，并在回答中注明数据时间范围与截止日期（例如「数据截至 10 月 4 日」），避免把同步截止误当成真实下降。time_property 缺省取第一个时间类型属性；filters 可选（不支持 contains）。",
        "parameters": {
            "type": "object",
            "properties": {
                "object_type": {"type": "string"},
                "time_property": {"type": "string", "description": "可选：时间属性 name；缺省自动选择第一个 date/datetime 属性"},
                "filters": {
                    "type": "array",
                    "description": "可选：先过滤再取范围（AND）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "property": {"type": "string"},
                            "op": {"type": "string", "enum": ["eq", "neq", "gt", "gte", "lt", "lte", "in"]},
                            "value": {},
                        },
                        "required": ["property", "op", "value"],
                    },
                },
                "window": {
                    "type": "object",
                    "description": "可选：先按服务端解析的绝对窗口过滤（timezone 必填），再取 min/max",
                    "properties": {
                        "property": {"type": "string"},
                        "timezone": {"type": "string"},
                        "last": {"type": "object", "properties": {"unit": {"type": "string", "enum": ["second", "minute", "hour", "day", "week", "month", "quarter", "year"]}, "count": {"type": "integer"}}, "required": ["unit", "count"]},
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                    },
                    "required": ["property", "timezone"],
                },
            },
            "required": ["object_type"],
        },
    },
    {
        "name": "get_object_history",
        "description": "查看对象实例的事实流（属性级变更历史）：每次变更的值、来源（人工/采集/某动作）、操作者、因果与置信度。回答'为什么是这个值/谁改的/什么时候变的'用这个。",
        "parameters": {
            "type": "object",
            "properties": {
                "instance_id": {"type": "string"},
                "property_name": {"type": "string", "description": "可选：只看某个属性"},
                "limit": {"type": "integer"},
            },
            "required": ["instance_id"],
        },
    },
    {
        "name": "trace_causal_chain",
        "description": "沿事实流追溯实例属性变化的因果链：哪次决策/哪个动作导致了变更、此前值是什么、之后又被谁覆盖、派生值由哪些输入算出。返回带出处的节点与因果边（决策→动作→事实→后续覆盖）；事实节点自带 causedBy/supersedesId/derivedFrom 指针，决策事实（factKind=decision）会拆出 decision/reason 字段。回答'为什么变了/谁批准的/这个值从哪来/后来变成了什么'这类跨对象因果问题用这个；单实例的单点历史用 get_object_history。链受深度与数量上限约束，返回 truncated=true 时说明这是部分链。",
        "parameters": {
            "type": "object",
            "properties": {
                "instance_id": {"type": "string", "description": "要追溯的实例 id"},
                "property_name": {"type": "string", "description": "可选：只追溯某个属性"},
                "direction": {"type": "string", "enum": ["upstream", "downstream", "both"], "description": "upstream=什么导致了这些变化；downstream=这些变化后来导致了什么；默认 both"},
                "max_depth": {"type": "integer", "minimum": 1, "maximum": 4},
            },
            "required": ["instance_id"],
        },
    },
    {
        "name": "list_actions",
        "description": "列出授权范围内可用的业务动作及其参数说明。",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "explain_sentinel_firing",
        "description": "解释一条哨兵为什么触发（或为什么没触发）：给出条件表达式、每条命中元组的对象与属性值证据、条件与绑定过滤的求值结果、状态语义与动作结果。材料来自触发记录与命中时刻快照，是确定性还原而非猜测。用户问「这条哨兵为什么触发/为什么没触发/为什么执行了动作」时使用；只读，不产生任何提案。",
        "parameters": {
            "type": "object",
            "properties": {
                "sentinel_id": {"type": "string", "description": "哨兵的 id、name 或显示名"},
                "firing_id": {"type": "string", "description": "可选：指定某次触发记录；缺省解释该哨兵最近一次"},
                "match_limit": {"type": "integer", "minimum": 1, "maximum": 10, "description": "最多展开几个命中元组，默认 3"},
            },
            "required": ["sentinel_id"],
        },
    },
    {
        "name": "list_dynamic_sentinels",
        "description": "列出当前发布版本之上的助手动态哨兵。只返回 assistant_dynamic；发布版内置哨兵不在可管理集合中。用户询问已有动态哨兵或准备修改时先调用。",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "propose_dynamic_sentinel_change",
        "description": "生成动态哨兵创建/更新/启用/停用/删除提案。只生成提案，不直接写库；内置哨兵永远不可操作。创建或更新 definition 会接受对象/关系/动作的 id、name 或显示名并由服务端规范化、强校验。",
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["create", "update", "enable", "disable", "delete"]},
                "sentinel_id": {"type": "string", "description": "update/enable/disable/delete 必填，只能来自 list_dynamic_sentinels"},
                "definition": {
                    "type": "object",
                    "description": "create 必填；update 可只给要修改的字段，服务端与当前完整定义合并后校验",
                    "properties": {
                        "name": {"type": "string", "description": "稳定英文技术名，如 overdue_order_alert"},
                        "displayName": {"type": "string"},
                        "description": {"type": "string"},
                        "bindings": {"type": "array", "items": {"type": "object", "properties": {
                            "alias": {"type": "string"}, "objectType": {"type": "string"}, "filter": {"type": "string"}
                        }, "required": ["alias", "objectType"]}},
                        "links": {"type": "array", "items": {"type": "object", "properties": {
                            "from": {"type": "string"}, "linkType": {"type": "string"}, "to": {"type": "string"}
                        }, "required": ["from", "linkType", "to"]}},
                        "condition": {"type": "string"},
                        "primaryAlias": {"type": "string"},
                        "actions": {"type": "array", "items": {"type": "string"}},
                        "actionParameters": {"type": "object", "additionalProperties": True},
                        "onChange": {"type": "boolean"},
                        "onSchedule": {"type": "boolean"},
                        "scanIntervalSeconds": {"type": "integer"},
                        "triggerMode": {"type": "string", "enum": ["on_enter", "on_enter_leave", "run_on_all", "on_pattern"]},
                        "pattern": {
                            "type": "object",
                            "description": "triggerMode=on_pattern 时必填的 CEP 事件模式；stages 的 alias/objectTypeId 必须与 bindings 镜像一致，onChange/onSchedule 必须同时为 true，窗口不得小于 scanIntervalSeconds",
                            "properties": {
                                "stages": {"type": "array", "minItems": 1, "maxItems": 4, "items": {"type": "object", "properties": {
                                    "alias": {"type": "string"},
                                    "objectTypeId": {"type": "string"},
                                    "filter": {"type": "string", "description": "如 \"a.status == 'submitted'\"；在事件时刻求值，应引用被监听的变更属性"},
                                    "within": {"type": "integer", "description": "完成本 stage 允许的秒数窗口（60~604800），缺省用 pattern.within"}
                                }, "required": ["alias", "objectTypeId"]}},
                                "within": {"type": "integer", "description": "缺省窗口秒数，默认 3600"},
                                "absence": {"type": "object", "properties": {"enabled": {"type": "boolean"}}, "required": ["enabled"], "description": "缺失分支：stage 超时未完成时以 edge=absence 触发"},
                                "aggregate": {"type": "object", "description": "单 stage 窗口聚合（count/avg/sum/min/max），带滞回防抖", "properties": {
                                    "property": {"type": "string"},
                                    "function": {"type": "string", "enum": ["count", "avg", "sum", "min", "max"]},
                                    "window": {"type": "integer"},
                                    "threshold": {"type": "number"},
                                    "comparison": {"type": "string", "enum": ["gte", "lte", "gt", "lt"]}
                                }, "required": ["property", "function", "window", "threshold"]},
                                "condition": {"type": "string", "description": "模式级最终条件，可引用各 stage 别名；支持 changed_within('a.prop',秒)/prev('a.prop') 时间算子"}
                            },
                            "required": ["stages"]
                        },
                        "muted": {"type": "boolean"}
                    },
                    "additionalProperties": False
                }
            },
            "required": ["operation"]
        }
    },
    {
        "name": "run_decision_simulation",
        "description": "在当前发布版本和授权范围的冻结快照上运行独立决策推演。用户说‘推演未来/比较方案/辅助决策/如果…怎么办’时使用。引擎会让多个独立角色提出可能性，再由确定性评分比较方案；结果不是概率或因果证明，也不会执行动作或写回真实对象。",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "要辅助决定的完整问题，需包含目标或待比较的选择"},
                "alternatives": {
                    "type": "array",
                    "description": "可选，用户已明确的 2-6 个互斥方案；未明确时由场景编译器生成包含维持现状的候选方案",
                    "items": {"type": "string"},
                    "minItems": 2,
                    "maxItems": 6,
                },
                "horizon": {"type": "string", "description": "可选，推演时间范围或决策窗口"},
            },
            "required": ["question"],
        },
    },
    {
        "name": "list_world_model_services",
        "description": "列出当前本体可用的世界模型推演服务（已发布且语义注册到本本体的服务）。可用服务附带示例入参 exampleInput（context/actions/horizon 的期望形状）；因本体版本演进（依赖对象类型缺失）或数据不足（前置条件不满足）暂不可用的服务会给出原因。做未来态势/what-if 数据推演前先调用本工具。",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "run_world_model_simulation",
        "description": "调用一个世界模型推演服务做确定性推演：context 按服务示例入参的形状、用查询工具收集的真实数据组装，actions 为干预动作，horizon 为推演步长。返回结构化结果与调用记录编号；结果中的数字必须原样引用，不得自行改算或外推。",
        "parameters": {
            "type": "object",
            "properties": {
                "service_id": {"type": "string", "description": "服务 id（来自 list_world_model_services）"},
                "context": {"type": "object", "description": "推演入参，形状参照服务 exampleInput 的 context", "additionalProperties": True},
                "actions": {"type": "array", "description": "干预动作列表，形状参照 exampleInput 的 actions", "items": {}},
                "horizon": {"type": "integer", "description": "推演步长/时间窗口", "minimum": 0},
            },
            "required": ["service_id", "context"],
        },
    },
    {
        "name": "propose_action",
        "description": "对某动作做预演（dry-run）：跑完整校验并模拟全部效果，但不落任何实际变更。这是 agent 唯一的'写'入口——预演结果会作为提案展示给用户，由用户决定是否真实执行。需要修改数据时必须用本工具，禁止假装已经执行。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "动作的 name 或显示名"},
                "parameters": {"type": "object", "description": "动作参数", "additionalProperties": True},
                "target_instance_id": {"type": "string", "description": "动作作用的目标实例 id（动作绑定对象类型时必填）"},
            },
            "required": ["action"],
        },
    },
]
