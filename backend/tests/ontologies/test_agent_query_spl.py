"""SPL 文本查询面的语义契约（goal A-02/A-28 文本 SPL lowering）。

覆盖范围：
- wire 契约：spl + semanticBindings 三元组 + params/pageSize，与结构树互斥；
- 翻译：with 命名阶段 → 结构树派生源；join 证据必须命中本人 assertion
  （缺证 422 join_semantics_unverified，绝不自动创造证据）；
- 语义：count() 计行 / count(字段) 只计非 NULL；一元 NULL 谓词；
  join 前 where 落 sourceFilter、join 后 where 落结构树 where；limit 拒绝；
- lineage：分组键多级传递（两级阶段之上的 join 键仍可溯源），聚合输出不可；
- parity：文本计划与等价结构树逐字段一致（model_dump 全等）；
- 回放：wire 请求 model_dump(by_alias) 可原样重验（cursor 重验路径）。

纯翻译用注入的 evidence resolver；断言查库用独立测试 db fixture，
不触业务源。
"""
from __future__ import annotations

import pytest

from app.data_channel.query.parser import parse_spl_plan
from app.ontologies.agent_query import remote_query as RQ
from app.ontologies.agent_query import schemas as QS
from app.ontologies.agent_query.models import AgentJoinAssertion
from app.ontologies.agent_query.service import QueryError


BINDINGS = {
    "cust": {"ontologyId": "ont-a", "releaseId": "rel-a",
             "objectTypeId": "ot-customer"},
    "contract": {"ontologyId": "ont-a", "releaseId": "rel-a",
                 "objectTypeId": "ot-contract"},
    "orders": {"ontologyId": "ont-b", "releaseId": "rel-b",
               "objectTypeId": "ot-order"},
    "ship": {"ontologyId": "ont-c", "releaseId": "rel-c",
             "objectTypeId": "ot-shipment"},
}

# 与 docs/development/ontology-network-goal.md 第 8 节 oracle 同构的
# 三域四表两级计划（count() 计行；先按订单/客户聚合再跨本体 join）。
CANONICAL_SPL = (
    "with ship_agg = from ship as s | stats sum(s.qty) as qty by s.order_id; "
    "with crm = from cust as c | join contract as ct "
    "on c.customer_id = ct.customer_id type=left "
    "| stats sum(ct.amount) as contract_sum, count() as contract_count "
    "by c.customer_id, c.region; "
    "with erp = from orders as o | join ship_agg as sh "
    "on o.order_id = sh.order_id type=left "
    "| stats sum(sh.qty) as ship_qty by o.customer_id; "
    "from crm as a | join erp as b on a.customer_id = b.customer_id type=left "
    "| where a.region = :region "
    "| stats sum(a.contract_sum) as contract_sum, sum(b.ship_qty) as ship_qty"
)


def _evidence(left, right):
    return {"keyType": "string", "businessMeaning": "fixture",
            "evidenceRefs": [f"assertion:{left[2]}:{right[2]}"],
            "cardinality": "N:M"}


def _translate(text=None, params=None, page_size=20, resolver=_evidence,
               bindings=None):
    plan = parse_spl_plan(text or CANONICAL_SPL)
    use_bindings = bindings or BINDINGS
    models = {name: QS.SemanticBinding.model_validate(binding)
              for name, binding in use_bindings.items()}
    resolved = params if params is not None else {"region": "north"}
    return RQ._translate_spl_plan(plan, models, resolved,
                                  page_size, resolver)


# ---------------------------------------------------------------------------
# wire 契约


def test_wire_spl_request_validates_and_round_trips():
    request = QS.QueryRequest.model_validate(
        {"spl": CANONICAL_SPL, "semanticBindings": BINDINGS,
         "params": {"region": "north"}, "pageSize": 20})
    assert request.spl == CANONICAL_SPL
    replay = QS.QueryRequest.model_validate(request.model_dump(by_alias=True))
    assert replay.spl == request.spl
    assert replay.semantic_bindings == request.semantic_bindings
    assert replay.params == {"region": "north"}


def test_wire_spl_rejects_mixed_shape_and_bad_bindings():
    with pytest.raises(Exception):
        QS.QueryRequest.model_validate(
            {"spl": CANONICAL_SPL, "semanticBindings": BINDINGS,
             "sources": [{"alias": "x", "ontologyId": "a", "releaseId": "b",
                          "objectTypeId": "c"}]})
    with pytest.raises(Exception):
        QS.QueryRequest.model_validate({"spl": CANONICAL_SPL})
    with pytest.raises(Exception):
        QS.QueryRequest.model_validate(
            {"spl": CANONICAL_SPL,
             "semanticBindings": {"x": {"ontologyId": "a", "releaseId": "b",
                                        "objectTypeId": "c", "assetId": "d"}}})
    with pytest.raises(Exception):
        # semanticBindings/params 只属于 spl 形态
        QS.QueryRequest.model_validate(
            {"sources": [{"alias": "x", "ontologyId": "a", "releaseId": "b",
                          "objectTypeId": "c"}],
             "semanticBindings": BINDINGS})
    # 结构树形态仍然可用（回归：非 spl 请求不受影响）
    plain = QS.QueryRequest.model_validate(
        {"sources": [{"alias": "x", "ontologyId": "a", "releaseId": "b",
                      "objectTypeId": "c"}],
         "select": [{"source": "x", "property": "id", "as": "id"}]})
    assert plain.spl is None


def test_facade_build_query_request_spl_branch():
    from app.ontologies.agent_runtime.assistant_facade import build_query_request

    request = build_query_request(
        {"spl": CANONICAL_SPL, "semanticBindings": BINDINGS,
         "params": {"region": "south"}, "pageSize": 7})
    assert isinstance(request, QS.QueryRequest)
    assert request.spl == CANONICAL_SPL
    assert request.params == {"region": "south"}
    assert request.page_size == 7
    assert build_query_request(
        {"cursor": "mem.abcdefghijklmnop"}).cursor == "mem.abcdefghijklmnop"


def test_facade_rejects_spl_mixed_shapes():
    """spl 与结构树/cursor 混形必须显式拒绝，不得静默丢弃任何一半。"""
    from app.ontologies.agent_runtime.assistant_facade import (
        FacadeError, build_query_request,
    )

    leaf = {"alias": "x", "ontologyId": "a", "releaseId": "b",
            "objectTypeId": "c"}
    mixed_cases = [
        {"spl": CANONICAL_SPL, "semanticBindings": BINDINGS,
         "sources": [leaf]},
        {"spl": CANONICAL_SPL, "semanticBindings": BINDINGS,
         "joins": [{"left": "a", "right": "b", "type": "left",
                    "on": {"kind": "declared_relation",
                           "linkTypeId": "lt",
                           "direction": "source_to_target"}}]},
        {"spl": CANONICAL_SPL, "semanticBindings": BINDINGS,
         "select": [{"source": "a", "property": "id", "as": "id"}]},
        {"spl": CANONICAL_SPL, "semanticBindings": BINDINGS, "distinct": True},
        {"spl": CANONICAL_SPL, "cursor": "mem.abcdefghijklmnop"},
    ]
    for arguments in mixed_cases:
        with pytest.raises(FacadeError) as excinfo:
            build_query_request(arguments)
        assert excinfo.value.status == 422
        assert excinfo.value.code == "invalid_request_shape"


def test_facade_rejects_spl_page_size_outside_schema_without_echo():
    """SPL 形态的 0/201/非法类型不得静默回落默认值，也不回显输入原值。"""
    import json

    from app.ontologies.agent_runtime.assistant_facade import (
        FacadeError, build_query_request,
    )

    base = {"spl": CANONICAL_SPL, "semanticBindings": BINDINGS}
    for bad in (0, 201, -1, "7", "abc", True, 1.5):
        with pytest.raises(FacadeError) as excinfo:
            build_query_request({**base, "pageSize": bad})
        assert (excinfo.value.status, excinfo.value.code) == (
            422, "invalid_parameter")
        assert excinfo.value.extra["field"] == "pageSize"
        assert excinfo.value.extra["maxPageSize"] == 200
        # schema 级错误不携带输入值（受控键只有 field/maxPageSize）。
        assert set(excinfo.value.extra) == {"field", "maxPageSize"}
        rendered = json.dumps(excinfo.value.detail(), ensure_ascii=False)
        if isinstance(bad, str):
            assert bad not in rendered
    # 边界值 200 仍按 schema 接受（上限不因受控化而收紧）。
    assert build_query_request({**base, "pageSize": 200}).page_size == 200


def _oneof_branch_matches(branch: dict, keys: set) -> bool:
    """按目录 oneOf 实际使用的关键字（required / not / anyOf）求值。"""
    if not set(branch.get("required", ())) <= keys:
        return False
    for forbidden in branch.get("not", {}).get("anyOf", ()):
        if set(forbidden.get("required", ())) <= keys:
            return False
    return True


def test_tool_schema_one_of_excludes_mixed_shapes():
    """oneOf 三分支两两互斥：混形在目录层就非法（不只靠请求模型兜底）。

    求值器只实现本目录 oneOf 实际使用的 required/not/anyOf 关键字，
    作用在真实 schema 对象上，不复制 schema 内容。
    """
    from app.super_assistant.ontology_tools import ontology_tool_schemas

    schema = next(tool for tool in ontology_tool_schemas()
                  if tool["name"] == "ontology_query")
    branches = schema["parameters"]["oneOf"]
    assert len(branches) == 3

    def matched(arguments: dict) -> int:
        keys = set(arguments)
        return sum(1 for branch in branches
                   if _oneof_branch_matches(branch, keys))

    leaf = {"alias": "x"}
    assert matched({"sources": [leaf], "select": []}) == 1        # 结构树
    assert matched({"cursor": "mem.abcdefghijklmnop"}) == 1       # 续查
    assert matched({"spl": CANONICAL_SPL,
                    "semanticBindings": BINDINGS}) == 1           # SPL 文本
    for mixed in ({"sources": [leaf], "spl": CANONICAL_SPL,
                   "semanticBindings": BINDINGS},                  # 树+spl
                  {"cursor": "mem.abcdefghijklmnop", "spl": CANONICAL_SPL,
                   "semanticBindings": BINDINGS},                  # cursor+spl
                  {"cursor": "mem.abcdefghijklmnop",
                   "sources": [leaf]}):                            # cursor+树
        assert matched(mixed) == 0, f"混形不应命中任何分支：{sorted(mixed)}"


# ---------------------------------------------------------------------------
# 翻译语义


def test_canonical_plan_lowers_to_expected_tree():
    request = _translate()
    dumped = request.model_dump(by_alias=True)
    # 主管道：crm + erp 两个派生源，LEFT join，region 参数过滤，纯聚合收尾
    assert [source["alias"] for source in dumped["sources"]] == ["a", "b"]
    (join,) = dumped["joins"]
    assert (join["left"], join["right"], join["type"]) == ("a", "b", "left")
    assert join["on"]["kind"] == "explicit_key"
    assert join["on"]["evidenceRefs"] == ["assertion:customer_id:customer_id"]
    assert dumped["where"] == [{"source": "a", "property": "region",
                                "op": "eq", "value": "north"}]
    assert [(a["fn"], a.get("source"), a.get("property"), a["as"])
            for a in dumped["aggregates"]] == [
        ("sum", "a", "contract_sum", "contract_sum"),
        ("sum", "b", "ship_qty", "ship_qty")]
    assert dumped["pageSize"] == 20
    # crm 阶段：count() 计行（source/property 均空）+ sum + 分组键入 select
    crm = dumped["sources"][0]["subquery"]
    count = next(a for a in crm["aggregates"] if a["fn"] == "count")
    assert count["source"] is None and count["property"] is None
    assert [(g["source"], g["property"]) for g in crm["groupBy"]] == [
        ("c", "customer_id"), ("c", "region")]
    assert [(s["source"], s["property"]) for s in crm["select"]] == [
        ("c", "customer_id"), ("c", "region")]
    # crm 阶段内部的 join 也走证据解析（结构树路径二次核验同一 assertion）
    assert crm["joins"][0]["on"]["evidenceRefs"] == [
        "assertion:customer_id:customer_id"]
    # ship_agg 阶段被 erp 阶段引用为派生源（同源先聚合防 fanout）
    erp = dumped["sources"][1]["subquery"]
    ship = erp["sources"][1]["subquery"]
    assert [(a["fn"], a.get("property"), a["as"])
            for a in ship["aggregates"]] == [("sum", "qty", "qty")]


def test_text_and_typed_trees_are_field_identical():
    """文本计划与等价结构树逐字段一致（A-02 text↔typed parity）。"""
    def typed_evidence(left, right):
        # 结构树 evidenceRefs 由调用方携带；与文本路径 resolver 输出同形
        return _evidence(left, right)

    text_request = _translate(resolver=typed_evidence)

    def leaf_source(alias, name):
        binding = BINDINGS[name]
        return QS.QuerySource(
            alias=alias, ontology_id=binding["ontologyId"],
            release_id=binding["releaseId"],
            object_type_id=binding["objectTypeId"])

    ship_agg = QS.QueryRequest(
        sources=[leaf_source("s", "ship")],
        select=[QS.SelectItem(source="s", property="order_id",
                              **{"as": "order_id"})],
        group_by=[QS.GroupByItem(source="s", property="order_id")],
        aggregates=[QS.AggregateItem(fn="sum", source="s", property="qty",
                                     **{"as": "qty"})])
    crm = QS.QueryRequest(
        sources=[leaf_source("c", "cust"), leaf_source("ct", "contract")],
        joins=[QS.QueryJoin(
            left="c", right="ct", type="left",
            on=QS.ExplicitKeyOn(
                kind="explicit_key", left_property="customer_id",
                right_property="customer_id", key_type="string",
                cardinality="N:M", business_meaning="fixture",
                evidence_refs=["assertion:customer_id:customer_id"]))],
        select=[QS.SelectItem(source="c", property="customer_id",
                              **{"as": "customer_id"}),
                QS.SelectItem(source="c", property="region",
                              **{"as": "region"})],
        group_by=[QS.GroupByItem(source="c", property="customer_id"),
                  QS.GroupByItem(source="c", property="region")],
        aggregates=[QS.AggregateItem(fn="sum", source="ct", property="amount",
                                     **{"as": "contract_sum"}),
                    QS.AggregateItem(fn="count", **{"as": "contract_count"})])
    erp = QS.QueryRequest(
        sources=[leaf_source("o", "orders"),
                 QS.QuerySource(alias="sh", subquery=ship_agg)],
        joins=[QS.QueryJoin(
            left="o", right="sh", type="left",
            on=QS.ExplicitKeyOn(
                kind="explicit_key", left_property="order_id",
                right_property="order_id", key_type="string",
                cardinality="N:M", business_meaning="fixture",
                evidence_refs=["assertion:order_id:order_id"]))],
        select=[QS.SelectItem(source="o", property="customer_id",
                              **{"as": "customer_id"})],
        group_by=[QS.GroupByItem(source="o", property="customer_id")],
        aggregates=[QS.AggregateItem(fn="sum", source="sh", property="qty",
                                     **{"as": "ship_qty"})])
    typed = QS.QueryRequest(
        sources=[QS.QuerySource(alias="a", subquery=crm),
                 QS.QuerySource(alias="b", subquery=erp)],
        joins=[QS.QueryJoin(
            left="a", right="b", type="left",
            on=QS.ExplicitKeyOn(
                kind="explicit_key", left_property="customer_id",
                right_property="customer_id", key_type="string",
                cardinality="N:M", business_meaning="fixture",
                evidence_refs=["assertion:customer_id:customer_id"]))],
        where=[QS.FilterLeaf(source="a", property="region", op="eq",
                             value="north")],
        aggregates=[QS.AggregateItem(fn="sum", source="a",
                                     property="contract_sum",
                                     **{"as": "contract_sum"}),
                    QS.AggregateItem(fn="sum", source="b", property="ship_qty",
                                     **{"as": "ship_qty"})],
        page_size=20)
    assert text_request.model_dump(by_alias=True) == typed.model_dump(
        by_alias=True)


def test_stage_filters_before_join_become_source_filters():
    request = _translate(
        text=('with s1 = from cust as c | where c.region = "north" '
              "| fields c.customer_id as customer_id; "
              "from s1 as x | fields x.customer_id as customer_id"),
        params={}, bindings={"cust": BINDINGS["cust"]})
    dumped = request.model_dump(by_alias=True)
    stage = dumped["sources"][0]["subquery"]
    assert stage["sourceFilters"] == [
        {"source": "c", "filter": {"source": "c", "property": "region",
                                   "op": "eq", "value": "north"}}]
    assert stage["where"] == []


def test_unary_null_predicates_translate():
    request = _translate(
        text=("from cust as c | where c.note is null and not c.region is null "
              "| fields c.customer_id as customer_id"),
        params={}, bindings={"cust": BINDINGS["cust"]})
    # 单源 join 前 where 落 sourceFilter（下推），一元 NULL 谓词无值参
    (source_filter,) = request.model_dump(by_alias=True)["sourceFilters"]
    assert source_filter["source"] == "c"
    node = source_filter["filter"]
    assert node["and"][0] == {"source": "c", "property": "note",
                              "op": "is_null", "value": None}
    assert node["and"][1] == {"not": {"source": "c", "property": "region",
                                      "op": "is_null", "value": None}}


def test_count_field_keeps_property_and_null_semantics():
    request = _translate(
        text="from orders as o | stats count(o.remark) as remarked, "
             "count() as total",
        params={}, bindings={"orders": BINDINGS["orders"]})
    aggregates = request.model_dump(by_alias=True)["aggregates"]
    by_name = {a["as"]: a for a in aggregates}
    assert by_name["remarked"]["property"] == "remark"   # count(field)
    assert by_name["total"]["property"] is None          # count(*)


def test_group_key_lineage_flows_through_two_stage_levels():
    """两级阶段之上的分组键 join 仍可溯源（多级 lineage 传递）。"""
    seen = []

    def resolver(left, right):
        seen.append((left, right))
        return _evidence(left, right)

    request = _translate(
        text=("with l1 = from ship as s | stats sum(s.qty) as qty "
              "by s.order_id; "
              "with l2 = from l1 as x | fields x.order_id as order_id, "
              "x.qty as qty; "
              "from orders as o | join l2 as g on o.order_id = g.order_id "
              "type=left | stats sum(g.qty) as ship_qty by o.customer_id"),
        params={}, bindings={"orders": BINDINGS["orders"],
                             "ship": BINDINGS["ship"]},
        resolver=resolver)
    # l2 对 l1 分组键 order_id 的直通投影保留了 lineage：
    # join 键可解析到 ship 绑定的 order_id（而不是被当作聚合输出拒绝）
    assert seen == [(("ont-b", "ot-order", "order_id"),
                     ("ont-c", "ot-shipment", "order_id"))]
    assert request.joins[0].on.right_property == "order_id"


# ---------------------------------------------------------------------------
# 拒绝语义（fail-closed）


def _translate_raises(text, params=None, resolver=_evidence, bindings=None):
    with pytest.raises(QueryError) as excinfo:
        _translate(text=text, params=params, resolver=resolver,
                   bindings=bindings)
    return excinfo.value


def test_limit_rejected_on_semantic_surface():
    error = _translate_raises("from orders as o | fields o.order_id as oid "
                              "| limit 5")
    assert error.code == "spl_limit_unsupported"


def test_missing_parameter_rejected():
    error = _translate_raises(
        "from cust as c | where c.region = :region "
        "| fields c.customer_id as customer_id", params={})
    assert error.code == "missing_parameter"


def test_join_without_assertion_evidence_rejected():
    def no_evidence(left, right):
        raise QueryError(422, "join_semantics_unverified", "缺证")

    error = _translate_raises(CANONICAL_SPL, resolver=no_evidence)
    assert error.code == "join_semantics_unverified"


def test_join_key_on_aggregate_output_rejected():
    # join 键引用阶段聚合输出（无 lineage）必须拒绝，与结构树同口径
    error = _translate_raises(
        "with agg1 = from orders as o | stats sum(o.amount) as amt "
        "by o.customer_id; "
        "from cust as c | join agg1 as g on c.note = g.amt type=left "
        "| fields c.customer_id as customer_id")
    assert error.code == "join_semantics_unverified"


def test_forward_stage_reference_rejected_by_core():
    text = ("with s2 = from s1 as x | fields x.customer_id as customer_id; "
            "with s1 = from cust as c | fields c.customer_id as customer_id; "
            "from s2 as y | fields y.customer_id as customer_id")
    with pytest.raises(QueryError) as excinfo:
        RQ.prepare_spl_plan(None, None, text, {"cust": BINDINGS["cust"]})
    assert excinfo.value.code == "forward_stage_reference"


def test_structural_validation_runs_before_translation():
    # 未知源（绑定缺失）在 9e 纯核结构校验中拒绝
    text = "from not_bound as x | fields x.id as id"
    with pytest.raises(QueryError) as excinfo:
        RQ.prepare_spl_plan(None, None, text, {"cust": BINDINGS["cust"]})
    assert excinfo.value.code == "unknown_source"


# ---------------------------------------------------------------------------
# assertion 查库（独立测试元数据库）


def _register_assertion(db, user_id="u1", left_prop="customer_id",
                        right_prop="customer_id",
                        left=("ont-a", "ot-customer"),
                        right=("ont-b", "ot-order")):
    row = AgentJoinAssertion(
        id=f"as-{left_prop}-{right_prop}", created_by=user_id,
        left_ontology_id=left[0], left_object_type_id=left[1],
        left_property=left_prop, right_ontology_id=right[0],
        right_object_type_id=right[1], right_property=right_prop,
        key_type="string", business_meaning="fixture join")
    db.add(row)
    db.commit()
    return row


class _User:
    id = "u1"


def test_lookup_join_assertion_exact_direction(db):
    row = _register_assertion(db)
    try:
        evidence = RQ._lookup_join_assertion(
            db, _User(), ("ont-a", "ot-customer", "customer_id"),
            ("ont-b", "ot-order", "customer_id"))
        assert evidence["keyType"] == "string"
        assert evidence["cardinality"] == "N:M"   # 声明无基数 → 保守 N:M
        assert evidence["evidenceRefs"] == [f"assertion:{row.id}"]
        assert evidence["businessMeaning"] == "fixture join"
    finally:
        db.query(AgentJoinAssertion).filter(
            AgentJoinAssertion.id == row.id).delete()
        db.commit()


def test_lookup_join_assertion_missing_or_foreign_rejected(db):
    _register_assertion(db, user_id="someone-else")
    try:
        with pytest.raises(QueryError) as excinfo:
            RQ._lookup_join_assertion(
                db, _User(), ("ont-a", "ot-customer", "customer_id"),
                ("ont-b", "ot-order", "customer_id"))
        assert excinfo.value.code == "join_semantics_unverified"
        with pytest.raises(QueryError):
            RQ._lookup_join_assertion(  # 反方向同样无 exact 声明
                db, _User(), ("ont-b", "ot-order", "customer_id"),
                ("ont-a", "ot-customer", "customer_id"))
    finally:
        db.query(AgentJoinAssertion).delete()
        db.commit()


def test_prepare_spl_plan_uses_db_assertion_for_main_join(db):
    row = _register_assertion(db)
    try:
        request = RQ.prepare_spl_plan(
            db, _User(),
            "from cust as c | join orders as o on c.customer_id = o.customer_id "
            "type=left | stats sum(o.amount) as order_sum, count() as order_count "
            "by c.customer_id, c.region",
            {name: binding for name, binding in BINDINGS.items()
             if name in {"cust", "orders"}},
            params={}, page_size=15)
        (join,) = request.joins
        assert join.on.evidence_refs == [f"assertion:{row.id}"]
        assert join.on.key_type == "string"
        assert join.on.cardinality == "N:M"
        assert request.page_size == 15
    finally:
        db.query(AgentJoinAssertion).filter(
            AgentJoinAssertion.id == row.id).delete()
        db.commit()


# ---------------------------------------------------------------------------
# 治理提示（A-03：缺证据 4xx 且带可操作指引，不放松校验）


def test_empty_evidence_refs_rejected_with_governance_guidance():
    with pytest.raises(Exception) as excinfo:
        QS.ExplicitKeyOn.model_validate(
            {"kind": "explicit_key", "leftProperty": "customer_id",
             "rightProperty": "customer_id", "keyType": "string",
             "cardinality": "N:M", "businessMeaning": "客户下订单",
             "evidenceRefs": []})
    message = str(excinfo.value)
    assert "join_semantics" not in message  # 仍是 schema 422，不冒充执行错误
    assert "ontology_register_join_assertion" in message
    assert "join-assertions" in message
    # 非空证据照常通过（伪造证据仍由 verify_join_evidence 422 拒绝）
    ok = QS.ExplicitKeyOn.model_validate(
        {"kind": "explicit_key", "leftProperty": "customer_id",
         "rightProperty": "customer_id", "keyType": "string",
         "cardinality": "N:M", "businessMeaning": "客户下订单",
         "evidenceRefs": ["assertion:fake"]})
    assert ok.evidence_refs == ["assertion:fake"]


def test_missing_assertion_error_names_the_registration_entry(db):
    with pytest.raises(QueryError) as excinfo:
        RQ._lookup_join_assertion(
            db, _User(), ("ont-a", "ot-customer", "customer_id"),
            ("ont-b", "ot-order", "customer_id"))
    assert excinfo.value.code == "join_semantics_unverified"
    assert "ontology_register_join_assertion" in excinfo.value.message


# ---------------------------------------------------------------------------
# 工具目录


def test_query_tool_schema_exposes_spl_shape():
    import json

    from app.super_assistant.ontology_tools import ontology_tool_schemas

    schema = next(tool for tool in ontology_tool_schemas()
                  if tool["name"] == "ontology_query")
    properties = schema["parameters"]["properties"]
    assert {"spl", "semanticBindings", "params", "cursor"} <= set(properties)
    binding = properties["semanticBindings"]["additionalProperties"]
    assert set(binding["required"]) == {"ontologyId", "releaseId",
                                        "objectTypeId"}
    # oneOf 三分支：sources / cursor / spl+semanticBindings 互斥
    branches = schema["parameters"]["oneOf"]
    assert any(set(branch.get("required", [])) == {"spl", "semanticBindings"}
               for branch in branches)
    # JSON 可序列化（目录经 LLM wire 传输）
    json.dumps(schema)
