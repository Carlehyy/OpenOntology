"""钉定 release 的远程绑定聚合自动接线统一查询（execute_unified_query）。

契约分组：
  1. 请求装配与身份传递：count 日期范围请求的源三元组/where/aggregates、
     真实用户与会话（session_kind=ontology_agent）透传
  2. 过滤语义转换：eq/neq/in 含 null 的远程语义；contains 与范围配 null 拒绝
  3. 结果诚实性：partial/多行标量不冒充完整；group 分页不伪造 matched
  4. 数值精确：大数/小数走无损编码（number_or_lossless_string）
  5. 门禁与透传：缺 user、profile 额度 0、QueryError 权限/release 冲突透传
  6. 接线：经真实 prepare_remote_plan + 已安装端口的端到端集成（仅替换
     物理执行端口），以及 orchestrator.chat 假 LLM 的生产接线集成
"""
import uuid

import pytest

from app.ontologies.agent_runtime.boundary import ToolError, build_scope
from app.ontologies.agent_runtime.toolkit import ToolRunner
from tests.ontologies.test_agent_runtime import modeled_ontology

# 发布快照里的 ot-supplier 远程绑定：比 fence 测试的描述符多登记
# region/rating/since_day 三列，供分组、求和与日期范围过滤使用。
_RICH_REMOTE_DESCRIPTOR = {
    "assetId": "asset-remote-agg",
    "datasetId": "ds-remote-agg",
    "connectionId": "conn-remote-agg",
    "schemaName": "public",
    "tableName": "suppliers",
    "schemaFingerprint": "a" * 64,
    "columns": [
        {"name": "supplier_name", "type": "string"},
        {"name": "region", "type": "string"},
        {"name": "rating", "type": "decimal", "precision": 30, "scale": 4},
        {"name": "since_day", "type": "date"},
    ],
    "primaryKey": ["supplier_name"],
}

_SUPPLIER_EXTRA_PROPERTIES = [
    {"id": "p-region", "name": "region", "displayName": "区域", "type": "string"},
    {"id": "p-rating", "name": "rating", "displayName": "评级分", "type": "number"},
    {"id": "p-since", "name": "since_day", "displayName": "入驻日期", "type": "date"},
]


def _freeze_with_rich_remote_supplier(
        db, ontology_id: str, *, mapped_properties=(), unmapped_properties=()) -> str:
    """冻结当前 release：ot-supplier 增补 region/rating/since_day 属性并登记远程绑定。

    ``mapped_properties`` 追加同名列映射（用于宽对象 >32 属性投影测试），
    ``unmapped_properties`` 只追加本体声明、不登记远程列（用于 queryable 标记）。
    """
    from app.models.ontology import OntologyProject
    from app.models.ontology_formal import ObjectInstance
    from app.models.ontology_version import OntologyVersion
    from app.ontologies.versions.evolution_service import snapshot_hash
    from app.ontologies.versions.workspace_service import _snapshot_formal

    project = db.query(OntologyProject).filter_by(id=ontology_id).one()
    release = db.query(OntologyVersion).filter_by(
        id=project.current_release_id).one()
    snapshot = _snapshot_formal(db, ontology_id)
    supplier = next(item for item in snapshot["objectTypes"]
                    if item.get("id") == "ot-supplier")
    extra = [dict(item) for item in _SUPPLIER_EXTRA_PROPERTIES]
    columns = [dict(item) for item in _RICH_REMOTE_DESCRIPTOR["columns"]]
    field_mapping = {"supplier_name": "sname", "region": "region",
                     "rating": "rating", "since_day": "since_day",
                     "__primary_key__": "supplier_name"}
    mapped_properties = [str(name) for name in mapped_properties]
    for name in mapped_properties:
        extra.append({"id": f"p-{name}", "name": name,
                      "displayName": name, "type": "string"})
        columns.append({"name": name, "type": "string"})
        field_mapping[name] = name
    for name in (str(item) for item in unmapped_properties):
        extra.append({"id": f"p-{name}", "name": name,
                      "displayName": name, "type": "string"})
    supplier.setdefault("properties", []).extend(extra)
    descriptor = {**_RICH_REMOTE_DESCRIPTOR, "columns": columns}
    snapshot.setdefault("mappings", []).append({
        "id": "map-remote-agg-supplier",
        "targetObjectTypeId": "ot-supplier",
        "entityClass": "Supplier",
        "curatedDatasetId": "ds-remote-agg",
        "bindingMode": "remote",
        "remote": descriptor,
        "fieldMapping": field_mapping,
        "status": "published",
    })
    release.snapshot_formal = snapshot
    release.snapshot_hash = snapshot_hash(snapshot)
    for instance in db.query(ObjectInstance).filter_by(
            ontology_id=ontology_id).all():
        instance.ontology_release_id = release.id
    db.commit()
    return str(release.id)


_WIDE_MAPPED_PROPERTIES = [f"field_{index:02d}" for index in range(43)]


def _freeze_with_wide_remote_supplier(db, ontology_id: str) -> str:
    """47 属性远程供应商（sname + 3 基础 + 43 payload），用于投影上限测试。"""
    return _freeze_with_rich_remote_supplier(
        db, ontology_id, mapped_properties=_WIDE_MAPPED_PROPERTIES)


def _remote_runner(db, ontology_id: str, release_id: str, *,
                   user=None, session_id: str = "") -> ToolRunner:
    _, _, scope = build_scope(db, ontology_id, release_id=release_id)
    return ToolRunner(db, scope, user=user, query_session_id=session_id)


def _patch_execute_unified(monkeypatch, result=None, error=None) -> list:
    """替换统一查询执行体（授权/装配仍是真实代码），记录每次调用。"""
    from app.ontologies.agent_query import service as query_service

    calls = []

    def fake_execute(db, user, request, *, session_kind, session_id,
                      include_page_size=False):
        calls.append({"user": user, "request": request,
                      "session_kind": session_kind, "session_id": session_id})
        if error is not None:
            raise error
        return result

    from app.ontologies.agent_query import remote_query
    monkeypatch.setattr(query_service, "execute_unified_query", fake_execute)
    monkeypatch.setattr(remote_query, "cancel_remote_query", lambda *args: None)
    return calls


def _patch_execute_sequence(monkeypatch, results: list) -> list:
    """按序返回每次统一查询结果（耗尽后重复最后一个），记录全部调用。"""
    from app.ontologies.agent_query import service as query_service

    calls = []

    def fake_execute(db, user, request, *, session_kind, session_id,
                      include_page_size=False):
        calls.append({"user": user, "request": request,
                      "session_kind": session_kind, "session_id": session_id})
        index = min(len(calls) - 1, len(results) - 1)
        return results[index]

    from app.ontologies.agent_query import remote_query
    monkeypatch.setattr(query_service, "execute_unified_query", fake_execute)
    monkeypatch.setattr(remote_query, "cancel_remote_query", lambda *args: None)
    return calls


def _unified_result(rows, *, partial=False, has_next=False, truncated=False,
                    total_row_count=None) -> dict:
    return {
        "queryId": "q-agg-1",
        "rows": rows,
        "partial": partial,
        "hasNext": has_next,
        "truncated": truncated,
        "totalRowCount": (total_row_count if total_row_count is not None
                          else len(rows)),
        "observedAt": "2026-11-02T00:00:00+00:00",
        "countsScope": "complete_snapshot",
        "citations": [],
    }


# ------------------------------------------------ 1. 请求装配与身份传递


def test_count_date_range_request_carries_identity_and_session(
        modeled_ontology, db, admin_user, monkeypatch):
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    runner = _remote_runner(db, oid, release_id,
                            user=admin_user, session_id="conv-agg-1")
    calls = _patch_execute_unified(
        monkeypatch, result=_unified_result([{"value": 7}]))

    out = runner.run("aggregate_objects", {
        "object_type": "Supplier", "metric": "count",
        "filters": [
            {"property": "since_day", "op": "gte", "value": "2026-01-01"},
            {"property": "since_day", "op": "lt", "value": "2026-02-01"},
        ]})

    # 真实用户与会话透传到统一查询（不是匿名/空会话）。
    assert len(calls) == 1
    assert calls[0]["user"] is admin_user
    assert calls[0]["session_kind"] == "ontology_agent"
    assert calls[0]["session_id"] == "conv-agg-1"

    request = calls[0]["request"]
    source = request.sources[0]
    # 叶子绑定三元组：本体/发布/类型全部来自钉定的当前 release。
    assert (source.ontology_id, source.release_id, source.object_type_id) == (
        oid, release_id, "ot-supplier")
    assert [(leaf.op, leaf.value) for leaf in request.where] == [
        ("gte", "2026-01-01"), ("lt", "2026-02-01")]
    assert all(leaf.property == "since_day" and leaf.source == "objects"
               for leaf in request.where)
    assert [agg.model_dump(by_alias=True) for agg in request.aggregates] == [
        {"fn": "count", "source": None, "property": None, "as": "value"}]
    assert request.page_size == 1  # 标量统计只取一行

    assert out["value"] == 7 and out["matched"] == 7
    assert out["dataMode"] == "remote"
    assert out["countSemantics"] == "bound_rows"
    assert out["releaseId"] == release_id
    assert out["queryId"] == "q-agg-1"
    assert out["observedAt"] == "2026-11-02T00:00:00+00:00"
    assert out["countsScope"] == "complete_snapshot"
    assert out["exact"] is True and out["partial"] is False
    assert out["completeness"] == "complete"
    assert out["valueType"] == "integer"
    assert out["valueEncoding"] == "number_or_lossless_string"


# ------------------------------------------------ 2. 过滤语义转换


def _leaf(op, value):
    return {"source": "objects", "property": "region", "op": op, "value": value}


def test_null_filter_semantics_are_converted_for_remote(
        modeled_ontology, db, admin_user, monkeypatch):
    """本地 eq/neq/in 对 null 的宽松语义必须翻译成远程可表达的谓词。"""
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    runner = _remote_runner(db, oid, release_id, user=admin_user)
    # 非零结果：0 结果会触发 dataCoverage 伴随探查，多出额外查询。
    calls = _patch_execute_unified(
        monkeypatch, result=_unified_result([{"value": 3}]))

    cases = [
        ({"source": "objects", "property": "region", "value": "华南"},
         _leaf("eq", "华南")),
        (_leaf("eq", None), _leaf("is_null", None)),
        (_leaf("neq", None), _leaf("is_not_null", None)),
        # neq 在 SQL 语义下不包含 NULL 行：显式补 or is_null
        (_leaf("neq", "华南"),
         {"or": [_leaf("neq", "华南"), _leaf("is_null", None)]}),
        # in 含 null：非空值走 in，null 走 or is_null
        (_leaf("in", ["华南", None]),
         {"or": [_leaf("in", ["华南"]), _leaf("is_null", None)]}),
        (_leaf("in", [None]), _leaf("is_null", None)),
        # 标量 in 归一为单元素数组
        (_leaf("in", "华南"), _leaf("in", ["华南"])),
    ]
    for raw, _ in cases:
        out = runner.run("aggregate_objects", {
            "object_type": "Supplier", "metric": "count", "filters": [raw]})
        assert out["value"] == 3

    assert len(calls) == len(cases)
    for call, (_, expected) in zip(calls, cases):
        dumped = [node.model_dump(by_alias=True) for node in call["request"].where]
        assert dumped == [expected]


# ------------------------------------------------ 3. 装配期拒绝


def test_null_valued_contains_and_range_filters_are_rejected_before_query(
        modeled_ontology, db, admin_user, monkeypatch):
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    runner = _remote_runner(db, oid, release_id, user=admin_user)
    calls = _patch_execute_unified(
        monkeypatch, result=_unified_result([{"value": 0}]))

    # contains 配 null 没有可解析的模糊语义：装配期拒绝。
    with pytest.raises(ToolError, match="remote_aggregate_filter_unsupported"):
        runner.run("aggregate_objects", {
            "object_type": "Supplier", "metric": "count",
            "filters": [{"property": "sname", "op": "contains", "value": None}]})
    for op in ("gt", "gte", "lt", "lte"):
        with pytest.raises(ToolError, match="remote_aggregate_filter_unsupported"):
            runner.run("aggregate_objects", {
                "object_type": "Supplier", "metric": "count",
                "filters": [{"property": "rating", "op": op, "value": None}]})
    # 拒绝发生在请求装配期：一次都不许触达统一查询。
    assert calls == []


# ------------------------------------------------ 3b. contains 源端枚举解析


def test_contains_is_resolved_via_remote_enumeration_into_in(
        modeled_ontology, db, admin_user, monkeypatch):
    """contains 先下推源端 LIKE 枚举；源端零命中回退全枚举做大小写不敏感匹配。"""
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    runner = _remote_runner(db, oid, release_id, user=admin_user)

    # 场景 1：源端 LIKE 已命中（MySQL ci / PG 精确大小写）→ 直接改写为 in。
    enum_rows = [{"group": "华南", "value": 4}, {"group": "North", "value": 3}]
    calls = _patch_execute_sequence(monkeypatch, [
        _unified_result(enum_rows, total_row_count=2),
        _unified_result([{"value": 7}]),
    ])
    out = runner.run("aggregate_objects", {
        "object_type": "Supplier", "metric": "count",
        "filters": [{"property": "region", "op": "contains", "value": "北"}]})
    assert out["value"] == 7
    assert len(calls) == 2
    enum_request = calls[0]["request"]
    assert [item.property for item in enum_request.group_by] == ["region"]
    # LIKE 条件下推到源端枚举查询
    dumped = [node.model_dump(by_alias=True) for node in enum_request.where]
    assert dumped == [{"source": "objects", "property": "region",
                       "op": "contains", "value": "北"}]
    assert enum_request.page_size >= 50  # 分组/枚举页由注册表上限约束
    main_request = calls[1]["request"]
    dumped = [node.model_dump(by_alias=True) for node in main_request.where]
    assert dumped == [{"source": "objects", "property": "region",
                       "op": "in", "value": ["华南", "North"]}]

    # 场景 2：源端 LIKE（PG 大小写敏感）零命中 → 回退全枚举 + 本地 ci 匹配。
    calls = _patch_execute_sequence(monkeypatch, [
        _unified_result([], total_row_count=0),          # 源端 LIKE 无命中
        _unified_result([{"group": "华南", "value": 4},   # 全枚举
                         {"group": "North", "value": 3}], total_row_count=2),
        _unified_result([{"value": 3}]),
    ])
    out = runner.run("aggregate_objects", {
        "object_type": "Supplier", "metric": "count",
        "filters": [{"property": "region", "op": "contains", "value": "north"}]})
    assert out["value"] == 3
    assert len(calls) == 3
    dumped = [node.model_dump(by_alias=True) for node in calls[2]["request"].where]
    assert dumped == [{"source": "objects", "property": "region",
                       "op": "in", "value": ["North"]}]


def test_contains_enumeration_without_match_returns_provable_zero(
        modeled_ontology, db, admin_user, monkeypatch):
    """源端 LIKE 与完整枚举均无命中：0 是被证明的空集，不再执行主查询。"""
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    runner = _remote_runner(db, oid, release_id, user=admin_user)

    calls = _patch_execute_sequence(monkeypatch, [
        _unified_result([], total_row_count=0),
        _unified_result([], total_row_count=0),
    ])

    out = runner.run("aggregate_objects", {
        "object_type": "Supplier", "metric": "count",
        "filters": [{"property": "region", "op": "contains", "value": "不存在"}]})

    assert len(calls) == 2  # LIKE 下推 + 全枚举兜底；主查询被短路
    assert out["value"] == 0 and out["matched"] == 0
    assert out["dataCoverage"]["reason"] == "filter_no_match"
    assert "无任何取值命中" in out["dataCoverage"]["note"]

    # 分组形态同样短路：可证明空分组。
    calls = _patch_execute_sequence(monkeypatch, [
        _unified_result([], total_row_count=0),
        _unified_result([], total_row_count=0),
    ])
    grouped = runner.run("aggregate_objects", {
        "object_type": "Supplier", "metric": "count", "group_by": "region",
        "filters": [{"property": "region", "op": "contains", "value": "不存在"}]})
    assert grouped["groups"] == [] and grouped["totalGroups"] == 0
    assert grouped["dataCoverage"]["reason"] == "filter_no_match"
    assert len(calls) == 2


def test_contains_enumeration_truncated_without_match_is_unresolved(
        modeled_ontology, db, admin_user, monkeypatch):
    """兜底枚举超过一页且无一命中：无法证明空集，引导改用 in/eq 明确取值。"""
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    runner = _remote_runner(db, oid, release_id, user=admin_user)
    calls = _patch_execute_sequence(monkeypatch, [
        _unified_result([], total_row_count=0),               # 源端 LIKE 无命中
        _unified_result([{"group": "华南", "value": 4}], total_row_count=80),
    ])

    with pytest.raises(ToolError, match="remote_aggregate_contains_unresolved"):
        runner.run("aggregate_objects", {
            "object_type": "Supplier", "metric": "count",
            "filters": [{"property": "region", "op": "contains", "value": "不存在"}]})
    assert len(calls) == 2


def test_contains_truncated_enumeration_with_partial_matches_fails_closed(
        modeled_ontology, db, admin_user, monkeypatch):
    """截断枚举只看到少量命中时不得冒充完整 contains 解析。"""
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    runner = _remote_runner(db, oid, release_id, user=admin_user)

    # LIKE 枚举本身就被截断：即使拿到了命中值也不能用它改写 in。
    calls = _patch_execute_sequence(monkeypatch, [
        _unified_result([{"group": "华南", "value": 4}], total_row_count=80),
    ])
    with pytest.raises(ToolError, match="remote_aggregate_contains_unresolved"):
        runner.run("aggregate_objects", {
            "object_type": "Supplier", "metric": "count",
            "filters": [{"property": "region", "op": "contains", "value": "南"}]})
    assert len(calls) == 1  # 主统计绝不能被执行

    # 全枚举兜底被截断且本地大小写不敏感匹配有命中：同样 fail closed。
    calls = _patch_execute_sequence(monkeypatch, [
        _unified_result([], total_row_count=0),
        _unified_result([{"group": "North", "value": 3}], total_row_count=80),
    ])
    with pytest.raises(ToolError, match="remote_aggregate_contains_unresolved"):
        runner.run("aggregate_objects", {
            "object_type": "Supplier", "metric": "count",
            "filters": [{"property": "region", "op": "contains", "value": "north"}]})
    assert len(calls) == 2


# ------------------------------------------------ 3c. 0 结果覆盖探查


def test_zero_with_range_filter_reports_no_data_in_range(
        modeled_ontology, db, admin_user, monkeypatch):
    """0 + 时间范围 + 名称等值：等值在范围外仍匹配 → 时段无数据，不是名称不存在。"""
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    runner = _remote_runner(db, oid, release_id, user=admin_user)
    calls = _patch_execute_sequence(monkeypatch, [
        _unified_result([{"value": 0}]),   # 主查询：范围+等值 → 0
        _unified_result([{"value": 5}]),   # 探查 A：仅等值 → 5
    ])

    out = runner.run("aggregate_objects", {
        "object_type": "Supplier", "metric": "count",
        "filters": [
            {"property": "region", "op": "eq", "value": "华南"},
            {"property": "since_day", "op": "gte", "value": "2026-10-01"},
            {"property": "since_day", "op": "lt", "value": "2026-11-01"},
        ]})

    assert out["value"] == 0
    coverage = out["dataCoverage"]
    assert coverage["reason"] == "no_data_in_range"
    assert coverage["matchedOutsideRange"] == 5
    assert "无数据" in coverage["note"]
    # 探查 A 只保留等值条件、剥离范围条件。
    probe_where = [(leaf.op, leaf.value) for leaf in calls[1]["request"].where]
    assert probe_where == [("eq", "华南")]
    assert calls[1]["request"].page_size == 1


def test_zero_without_range_reports_filter_no_match(
        modeled_ontology, db, admin_user, monkeypatch):
    """0 + 仅等值：不再为区分成因额外跑全表计数（0 结果不应更贵）。"""
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    runner = _remote_runner(db, oid, release_id, user=admin_user)
    calls = _patch_execute_sequence(monkeypatch, [
        _unified_result([{"value": 0}]),
    ])

    out = runner.run("aggregate_objects", {
        "object_type": "Supplier", "metric": "count",
        "filters": [{"property": "region", "op": "eq", "value": "不存在的区域"}]})

    assert out["dataCoverage"]["reason"] == "filter_no_match"
    assert out["dataCoverage"]["unfilteredCount"] is None
    assert "全表计数" in out["dataCoverage"]["note"]
    assert len(calls) == 1  # 等值无匹配不再触发额外全表计数


def test_zero_reports_source_no_data(
        modeled_ontology, db, admin_user, monkeypatch):
    """源本身无数据：无过滤 0 直接标注；全范围过滤 0 + 探查 0 同样归因。"""
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    runner = _remote_runner(db, oid, release_id, user=admin_user)

    calls = _patch_execute_sequence(monkeypatch, [_unified_result([{"value": 0}])])
    out = runner.run("aggregate_objects",
                     {"object_type": "Supplier", "metric": "count"})
    assert out["dataCoverage"]["reason"] == "source_no_data"
    assert out["dataCoverage"]["unfilteredCount"] is None
    assert len(calls) == 1  # 主查询即无过滤，无需探查

    calls = _patch_execute_sequence(monkeypatch, [
        _unified_result([{"value": 0}]),
        _unified_result([{"value": 0}]),
    ])
    ranged = runner.run("aggregate_objects", {
        "object_type": "Supplier", "metric": "count",
        "filters": [{"property": "since_day", "op": "gte", "value": "2026-10-01"}]})
    assert ranged["dataCoverage"]["reason"] == "source_no_data"
    assert len(calls) == 2  # 全范围过滤：探查 A 即无过滤计数


def test_zero_group_result_reports_coverage(
        modeled_ontology, db, admin_user, monkeypatch):
    """分组聚合 0 组同样附覆盖说明，不冒充整期已完整统计。"""
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    runner = _remote_runner(db, oid, release_id, user=admin_user)
    calls = _patch_execute_sequence(monkeypatch, [
        _unified_result([], total_row_count=0),
    ])

    out = runner.run("aggregate_objects", {
        "object_type": "Supplier", "metric": "count", "group_by": "region",
        "filters": [{"property": "region", "op": "eq", "value": "华南"}]})

    assert out["groups"] == [] and out["totalGroups"] == 0
    assert out["matched"] == 0
    assert out["dataCoverage"]["reason"] == "filter_no_match"
    assert out["dataCoverage"]["unfilteredCount"] is None
    assert len(calls) == 1  # 等值 0 组不再额外全表计数


# ------------------------------------------------ 4. 标量完整性


def test_partial_or_multirow_scalar_result_is_refused(
        modeled_ontology, db, admin_user, monkeypatch):
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    runner = _remote_runner(db, oid, release_id, user=admin_user)

    bad_results = [
        {**_unified_result([{"value": 1}]), "exact": False},
        {**_unified_result([{"value": 1}]), "completeness": "unknown"},
        _unified_result([{"value": 1}], partial=True),
        _unified_result([{"value": 1}, {"value": 2}]),  # 标量统计多行=不完整
        _unified_result([{"value": 1}], has_next=True),
        _unified_result([{"value": 1}], truncated=True),
    ]
    for bad in bad_results:
        _patch_execute_unified(monkeypatch, result=bad)
        with pytest.raises(ToolError, match="remote_aggregate_incomplete"):
            runner.run("aggregate_objects",
                       {"object_type": "Supplier", "metric": "count"})


# ------------------------------------------------ 5. 分组形状与分页诚实性


def test_group_result_shape_and_pagination_honesty(
        modeled_ontology, db, admin_user, monkeypatch):
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    runner = _remote_runner(db, oid, release_id, user=admin_user)

    rows = [{"group0": "华南", "value": 4}, {"group0": "华东", "value": 3}]
    calls = _patch_execute_unified(monkeypatch, result=_unified_result(
        rows, total_row_count=10))
    out = runner.run("aggregate_objects", {
        "object_type": "Supplier", "metric": "count", "group_by": "region"})

    request = calls[0]["request"]
    # 分组摘要页走注册表上限（默认 200，> profile 50），并有界统计防高基数分组回流
    assert request.page_size > 50
    assert request.bounded_stats is True
    assert [item.source for item in request.group_by] == ["objects"]
    assert [item.property for item in request.group_by] == ["region"]
    assert [item.as_ for item in request.select] == ["group0"]
    assert [(item.output, item.direction) for item in request.order_by] == [
        ("value", "desc"), ("group0", "asc")]

    assert out["groupBy"] == "region"
    assert [(g["group"], g["value"], g["count"]) for g in out["groups"]] == [
        ("华南", 4, 4), ("华东", 3, 3)]  # count 语义：组计数即 value
    assert out["totalGroups"] == 10
    assert out["truncated"] is True
    # 分页未取全部分组：绝不把当前页求和冒充全量 matched。
    assert out["matched"] is None

    # 未截断：matched 是各组计数的精确和。
    _patch_execute_unified(monkeypatch, result=_unified_result(rows))
    out = runner.run("aggregate_objects", {
        "object_type": "Supplier", "metric": "count", "group_by": "region"})
    assert out["truncated"] is False
    assert out["matched"] == 7
    assert out["totalGroups"] == 2


# ------------------------------------------------ 5b. pageSize 受控上限


def _set_remote_supplier_page_cap(db, ontology_id: str, cap: int) -> None:
    from app.ontologies.agent_runtime.boundary import get_or_create_profile

    profile = get_or_create_profile(db, ontology_id)
    profile.max_rows_per_query = cap
    db.commit()


def _supplier_page_request(oid: str, release_id: str, page_size: int):
    from app.ontologies.agent_query.schemas import QueryRequest

    return QueryRequest.model_validate({
        "sources": [{"alias": "objects", "ontologyId": oid,
                     "releaseId": release_id, "objectTypeId": "ot-supplier"}],
        "select": [{"source": "objects", "property": "sname", "as": "sname"}],
        "pageSize": page_size,
    })


def test_remote_plan_page_size_over_profile_cap_reports_control_fields(
        modeled_ontology, db, admin_user):
    """profile cap=50 时请求 51 → 422 带 field/requested/max/default。"""
    from app.ontologies.agent_query import remote_query
    from app.ontologies.agent_query.service import QueryError

    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    _set_remote_supplier_page_cap(db, oid, 50)

    with pytest.raises(QueryError) as error:
        remote_query.prepare_remote_plan(
            db, admin_user,
            _supplier_page_request(oid, release_id, 51))
    assert (error.value.status, error.value.code) == (
        422, "invalid_parameter")
    assert error.value.extra["field"] == "pageSize"
    assert error.value.extra["requestedPageSize"] == 51
    assert error.value.extra["maxPageSize"] == 50
    assert error.value.extra["defaultPageSize"] == 50

    # profile cap=200 时 200 可请求、实际计划页长一致。
    _set_remote_supplier_page_cap(db, oid, 200)
    plan = remote_query.prepare_remote_plan(
        db, admin_user, _supplier_page_request(oid, release_id, 200))
    assert plan["pageSize"] == 200


def test_group_limit_pushes_top_n_to_source_and_reports_total_groups(
        modeled_ontology, db, admin_user, monkeypatch):
    """Top-N 用 limit 下推源端 LIMIT；totalGroups 精确、truncated/matched 诚实。"""
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    runner = _remote_runner(db, oid, release_id, user=admin_user)
    calls = _patch_execute_sequence(monkeypatch, [
        _unified_result([{"group0": "华南", "value": 9},
                         {"group0": "华东", "value": 3}],
                        total_row_count=57),
    ])

    out = runner.run("aggregate_objects", {
        "object_type": "Supplier", "metric": "count", "group_by": "region",
        "limit": 10})

    request = calls[0]["request"]
    assert request.page_size == 10
    assert request.bounded_stats is True
    assert [(item.output, item.direction) for item in request.order_by] == [
        ("value", "desc"), ("group0", "asc")]
    assert out["groupLimit"] == 10
    assert out["returned"] == 2
    assert out["totalGroups"] == 57
    assert out["truncated"] is True
    assert out["matched"] is None
    assert "10" in out["truncationNote"] and "57" in out["truncationNote"]


def test_group_limit_rejects_scalar_and_out_of_range_before_query(
        modeled_ontology, db, admin_user, monkeypatch):
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    runner = _remote_runner(db, oid, release_id, user=admin_user)
    calls = _patch_execute_sequence(monkeypatch, [_unified_result([])])

    with pytest.raises(ToolError, match="aggregate_objects_invalid_limit"):
        runner.run("aggregate_objects", {
            "object_type": "Supplier", "metric": "count", "limit": 10})
    with pytest.raises(ToolError, match="aggregate_objects_invalid_limit"):
        runner.run("aggregate_objects", {
            "object_type": "Supplier", "metric": "count",
            "group_by": "region", "limit": 0})
    with pytest.raises(ToolError, match="aggregate_objects_invalid_limit"):
        runner.run("aggregate_objects", {
            "object_type": "Supplier", "metric": "count",
            "group_by": "region", "limit": 201})
    assert calls == []


def test_grouped_drilldown_projection_is_bounded_and_keeps_query_spec(
        modeled_ontology, db, admin_user, monkeypatch):
    """47 属性对象的聚合下钻明细投影也受 32 项上限，querySpec 不得静默丢失。"""
    oid = modeled_ontology["id"]
    release_id = _freeze_with_wide_remote_supplier(db, oid)
    runner = _remote_runner(db, oid, release_id, user=admin_user,
                            session_id="conv-wide-agg")
    calls = _patch_execute_sequence(monkeypatch, [
        _unified_result([{"group0": "华南", "value": 4}], total_row_count=1),
    ])

    out = runner.run("aggregate_objects", {
        "object_type": "Supplier", "metric": "count", "group_by": "region"})

    assert len(calls) == 1
    spec = out["querySpec"]
    detail_select = spec["drilldownBase"]["select"]
    assert len(detail_select) == 32
    assert [item["property"] for item in detail_select][:2] == ["sname", "region"]
    omitted = spec["description"]["drilldownOmittedFields"]
    assert omitted and "field_42" in omitted
    assert spec["description"]["groupBy"] == [
        {"property": "region", "bucket": None}]
    assert out["querySpecHash"]


def test_unmapped_remote_property_is_rejected_before_any_source_query(
        modeled_ontology, db, admin_user, monkeypatch):
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(
        db, oid, unmapped_properties=["internal_note"])
    runner = _remote_runner(db, oid, release_id, user=admin_user)
    calls = _patch_execute_sequence(monkeypatch, [_unified_result([{"value": 1}])])

    with pytest.raises(ToolError, match="remote_aggregate_property_unqueryable"):
        runner.run("aggregate_objects", {
            "object_type": "Supplier", "metric": "count",
            "filters": [{"property": "internal_note", "op": "eq", "value": "x"}]})
    with pytest.raises(ToolError, match="remote_aggregate_property_unqueryable"):
        runner.run("aggregate_objects", {
            "object_type": "Supplier", "metric": "count",
            "group_by": "internal_note"})
    assert calls == []  # 校验在 contains/枚举/主查询之前完成


# ------------------------------------------------ 6. 数值精确


def test_sum_avg_shape_and_lossless_big_numbers(
        modeled_ontology, db, admin_user, monkeypatch):
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    runner = _remote_runner(db, oid, release_id, user=admin_user)

    big = 100000000000000000001  # 超 JS 安全整数
    calls = _patch_execute_unified(monkeypatch, result=_unified_result(
        [{"value": big, "matched": 3}]))
    out = runner.run("aggregate_objects", {
        "object_type": "Supplier", "metric": "sum", "metric_property": "rating"})
    assert out["value"] == "100000000000000000001"  # 无损字符串，不走 float
    assert out["matched"] == 3
    assert out["valueType"] == "decimal"
    assert out["valueEncoding"] == "number_or_lossless_string"
    # 非 count 聚合必须同时带回行数（matched），否则答不了"统计了多少行"。
    assert [agg.model_dump(by_alias=True) for agg in calls[0]["request"].aggregates] == [
        {"fn": "sum", "source": "objects", "property": "rating", "as": "value"},
        {"fn": "count", "source": None, "property": None, "as": "matched"},
    ]

    _patch_execute_unified(monkeypatch, result=_unified_result(
        [{"value": 123.45, "matched": 2}]))
    out = runner.run("aggregate_objects", {
        "object_type": "Supplier", "metric": "avg", "metric_property": "rating"})
    assert out["value"] == "123.45"  # 小数同样无损，不得四舍五入
    assert out["matched"] == 2


# ------------------------------------------------ 7. 门禁


def test_missing_user_and_zero_cap_are_rejected(
        modeled_ontology, db, admin_user):
    from app.ontologies.agent_runtime.models import AgentProfile

    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)

    runner = _remote_runner(db, oid, release_id)  # 本地直连：无认证用户
    with pytest.raises(ToolError, match="remote_query_user_required"):
        runner.run("aggregate_objects",
                   {"object_type": "Supplier", "metric": "count"})

    profile = db.query(AgentProfile).filter_by(ontology_id=oid).one()
    profile.max_rows_per_query = 0
    db.commit()
    runner = _remote_runner(db, oid, release_id, user=admin_user)
    with pytest.raises(ToolError, match="profile_query_disabled"):
        runner.run("aggregate_objects",
                   {"object_type": "Supplier", "metric": "count"})


def test_query_error_passes_code_and_message_through(
        modeled_ontology, db, admin_user, monkeypatch):
    from app.ontologies.agent_query.service import QueryError

    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    runner = _remote_runner(db, oid, release_id, user=admin_user)

    _patch_execute_unified(monkeypatch, error=QueryError(
        403, "property_forbidden", "属性不在授权范围内"))
    with pytest.raises(ToolError, match="property_forbidden: 属性不在授权范围内"):
        runner.run("aggregate_objects", {
            "object_type": "Supplier", "metric": "count",
            "filters": [{"property": "region", "op": "eq", "value": "x"}]})

    _patch_execute_unified(monkeypatch, error=QueryError(
        409, "release_context_changed", "发布版本已切换"))
    with pytest.raises(ToolError, match="release_context_changed: 发布版本已切换"):
        runner.run("aggregate_objects",
                   {"object_type": "Supplier", "metric": "count"})


def test_local_types_keep_local_aggregation(
        modeled_ontology, db, admin_user, monkeypatch):
    """同一钉定 scope 内本地物化类型不受远程接线影响。"""
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    runner = _remote_runner(db, oid, release_id, user=admin_user)
    calls = _patch_execute_unified(
        monkeypatch, result=_unified_result([{"value": -1}]))

    agg = runner.run("aggregate_objects",
                     {"object_type": "Order", "metric": "count"})
    assert agg["value"] == 2
    assert "dataMode" not in agg  # 本地路径不带远程标记
    assert calls == []  # 本地统计不经过统一查询

    found = runner.run("search_objects", {"object_type": "Order", "limit": 5})
    assert found["total"] == 2 and found["exact"] is True


# ------------------------------------------------ 8. 真实接线集成


class _RecordingPort:
    """替换已安装端口的物理执行体；计划编制/复核仍走真实代码。"""

    def __init__(self, rows):
        self.rows = rows
        self.executed = []
        self.cancelled = []

    def execute(self, db, user, plan, *, request, session_kind, session_id):
        self.executed.append({"user": user, "plan": plan,
                              "session_kind": session_kind,
                              "session_id": session_id})
        return _unified_result(self.rows)

    def fetch(self, *args, **kwargs):  # pragma: no cover - 标量聚合不分页
        raise AssertionError("标量聚合不应分页")

    def cancel(self, db, user, query_id):
        self.cancelled.append((user.id, query_id))
        return {"queryId": query_id, "cancelled": True}


class _SequencePort:
    """按调用序返回不同结果；每次真实 prepare_remote_plan 的 plan 全部记录。

    计划编制/授权/profile 帽复核仍是真实代码（``remote_query._port`` 只替换
    物理执行），因此「枚举页是否越过授权上限」会被真实校验捕获。
    """

    def __init__(self, results):
        self.results = list(results)
        self.executed = []
        self.cancelled = []

    def execute(self, db, user, plan, *, request, session_kind, session_id):
        self.executed.append({"user": user, "plan": plan, "request": request,
                              "session_kind": session_kind,
                              "session_id": session_id})
        index = min(len(self.executed) - 1, len(self.results) - 1)
        return self.results[index]

    def fetch(self, *args, **kwargs):  # pragma: no cover - 聚合不分页取页
        raise AssertionError("聚合不分页取页")

    def cancel(self, db, user, query_id):
        self.cancelled.append((user.id, query_id))
        return {"queryId": query_id, "cancelled": True}


def test_remote_aggregate_wires_through_real_plan_and_installed_port(
        modeled_ontology, db, admin_user, monkeypatch):
    """端到端接线：真实 prepare_remote_plan（授权/绑定/列映射）+ 已安装端口。"""
    from app.ontologies.agent_query import remote_query

    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    runner = _remote_runner(db, oid, release_id,
                            user=admin_user, session_id="conv-wire")
    port = _RecordingPort([{"value": 4}])
    monkeypatch.setattr(remote_query, "_port", port)

    out = runner.run("aggregate_objects", {
        "object_type": "Supplier", "metric": "count",
        "filters": [{"property": "region", "op": "eq", "value": "华南"}]})

    assert out["value"] == 4 and out["dataMode"] == "remote"
    assert port.cancelled == [(admin_user.id, "q-agg-1")]
    assert len(port.executed) == 1
    plan = port.executed[0]["plan"]
    source = plan["sources"][0]
    # 发布快照的远程绑定编译为集成资产 + 物理 propertyMap。
    assert source["binding"] == {"kind": "integrated", "assetId": "asset-remote-agg"}
    assert source["propertyMap"] == {
        "sname": "supplier_name", "region": "region",
        "rating": "rating", "since_day": "since_day"}
    assert source["schemaFingerprint"] == "a" * 64
    assert source["ontology"] == {"ontologyId": oid, "releaseId": release_id,
                                  "objectTypeId": "ot-supplier"}
    # where 已翻到物理列；聚合输出与分页大小按请求装配。
    assert plan["where"] == [{"source": "objects", "property": "region",
                              "op": "eq", "value": "华南"}]
    assert plan["aggregates"] == [{"fn": "count", "source": None,
                                   "property": None, "as": "value"}]
    assert plan["pageSize"] == 1
    assert plan["releases"]["objects"] == {
        "ontologyId": oid, "releaseId": release_id,
        "objectTypeId": "ot-supplier", "dataMode": "remote"}
    assert plan["citations"] == [{"ontologyId": oid, "releaseId": release_id,
                                  "objectTypeId": "ot-supplier",
                                  "sourceAlias": "objects"}]
    assert port.executed[0]["user"] is admin_user
    assert port.executed[0]["session_kind"] == "ontology_agent"
    assert port.executed[0]["session_id"] == "conv-wire"


# ------------------------- 8b. contains 枚举页不得越过 profile 授权上限（P-23）


_CONTAINS_FILTER = {"property": "region", "op": "contains", "value": "南"}
_CONTAINS_ENUM_ROWS = [{"group": "华南", "value": 4}]


def _contains_enum_result(rows=None, *, total_row_count=None):
    return _unified_result(rows if rows is not None else _CONTAINS_ENUM_ROWS,
                           total_row_count=total_row_count)


def _install_sequence_port(monkeypatch, results):
    from app.ontologies.agent_query import remote_query

    port = _SequencePort(results)
    monkeypatch.setattr(remote_query, "_port", port)
    return port


def test_contains_enumeration_page_respects_profile_cap(
        modeled_ontology, db, admin_user, monkeypatch):
    """cap=100 的 contains 标量统计：LIKE 枚举页 = 100（而非注册表 200）。"""
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    _set_remote_supplier_page_cap(db, oid, 100)
    runner = _remote_runner(db, oid, release_id, user=admin_user)
    port = _install_sequence_port(monkeypatch, [
        _contains_enum_result(total_row_count=1),
        _unified_result([{"value": 4}]),
    ])

    out = runner.run("aggregate_objects", {
        "object_type": "Supplier", "metric": "count",
        "filters": [dict(_CONTAINS_FILTER)]})

    assert out["value"] == 4 and out["dataMode"] == "remote"
    assert len(port.executed) == 2
    enum_plan = port.executed[0]["plan"]
    assert enum_plan["pageSize"] == 100  # = min(200, profile cap 100)
    assert "statsLimit" not in enum_plan  # 枚举页不得借 boundedStats 绕过
    assert enum_plan["where"] == [{"source": "objects", "property": "region",
                                   "op": "contains", "value": "南"}]
    assert port.executed[1]["plan"]["pageSize"] == 1


def test_contains_grouped_enum_page_capped_but_main_group_page_kept(
        modeled_ontology, db, admin_user, monkeypatch):
    """cap=50：枚举页夹到 50，主分组 boundedStats 页仍为 200（例外不夹取）。"""
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    _set_remote_supplier_page_cap(db, oid, 50)
    runner = _remote_runner(db, oid, release_id, user=admin_user)
    port = _install_sequence_port(monkeypatch, [
        _contains_enum_result(total_row_count=1),
        _unified_result([{"group0": "华南", "value": 7}], total_row_count=1),
    ])

    out = runner.run("aggregate_objects", {
        "object_type": "Supplier", "metric": "count", "group_by": "region",
        "filters": [dict(_CONTAINS_FILTER)]})

    assert out["groups"] == [{"group": "华南", "value": 7, "count": 7,
                              "groupValues": ["华南"],
                              "groupDisplay": "region=华南"}]
    assert out["totalGroups"] == 1
    assert len(port.executed) == 2
    assert port.executed[0]["plan"]["pageSize"] == 50
    main_plan = port.executed[1]["plan"]
    assert main_plan["pageSize"] == 200
    assert main_plan["statsLimit"] == 200


@pytest.mark.parametrize("limit, expected_main", [
    (None, 200), (20, 20), (50, 50), (100, 100), (150, 150)])
def test_contains_enum_page_cap_independent_of_user_limit(
        modeled_ontology, db, admin_user, monkeypatch, limit, expected_main):
    """cap=100 下 limit 只压主分组页（含 150 > cap 的合法 Top-N），枚举恒为 100。"""
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    _set_remote_supplier_page_cap(db, oid, 100)
    runner = _remote_runner(db, oid, release_id, user=admin_user)
    port = _install_sequence_port(monkeypatch, [
        _contains_enum_result(total_row_count=1),
        _unified_result([{"group0": "华南", "value": 7}], total_row_count=1),
    ])
    arguments = {"object_type": "Supplier", "metric": "count",
                 "group_by": "region", "filters": [dict(_CONTAINS_FILTER)]}
    if limit is not None:
        arguments["limit"] = limit

    out = runner.run("aggregate_objects", arguments)

    assert out["groups"][0]["group"] == "华南"
    assert out["groupLimit"] == expected_main
    assert len(port.executed) == 2
    assert port.executed[0]["plan"]["pageSize"] == 100
    assert port.executed[1]["plan"]["pageSize"] == expected_main
    assert port.executed[1]["plan"]["statsLimit"] == expected_main


def test_contains_like_miss_fallback_page_respects_profile_cap(
        modeled_ontology, db, admin_user, monkeypatch):
    """LIKE 零命中回退全枚举（无 boundedStats）同样受 cap=100 约束。"""
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    _set_remote_supplier_page_cap(db, oid, 100)
    runner = _remote_runner(db, oid, release_id, user=admin_user)
    port = _install_sequence_port(monkeypatch, [
        _unified_result([], total_row_count=0),                       # LIKE 零命中
        _unified_result([{"group": "North", "value": 3}], total_row_count=1),
        _unified_result([{"value": 3}]),
    ])

    out = runner.run("aggregate_objects", {
        "object_type": "Supplier", "metric": "count",
        "filters": [{"property": "region", "op": "contains", "value": "north"}]})

    assert out["value"] == 3
    assert len(port.executed) == 3
    fallback_plan = port.executed[1]["plan"]
    assert fallback_plan["pageSize"] == 100
    assert "statsLimit" not in fallback_plan
    assert fallback_plan["where"] == []  # 回退只做全枚举，不再带 like 条件


def test_contains_enumeration_truncated_after_page_cap_fails_closed(
        modeled_ontology, db, admin_user, monkeypatch):
    """cap=100 枚举满页但 totalRowCount=150：仍 fail closed，主查询 0 次。"""
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    _set_remote_supplier_page_cap(db, oid, 100)
    runner = _remote_runner(db, oid, release_id, user=admin_user)
    rows = [{"group": f"区域{index}", "value": 1} for index in range(100)]
    port = _install_sequence_port(monkeypatch, [
        _unified_result(rows, total_row_count=150),
    ])

    with pytest.raises(ToolError, match="remote_aggregate_contains_unresolved"):
        runner.run("aggregate_objects", {
            "object_type": "Supplier", "metric": "count",
            "filters": [dict(_CONTAINS_FILTER)]})

    assert port.executed[0]["plan"]["pageSize"] == 100
    assert len(port.executed) == 1  # 主统计绝不能被执行


def test_non_contains_group_page_keeps_registry_page_under_profile_cap(
        modeled_ontology, db, admin_user, monkeypatch):
    """非 contains 分组统计不受枚举帽影响：cap=100 时主分组页仍 200。"""
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    _set_remote_supplier_page_cap(db, oid, 100)
    runner = _remote_runner(db, oid, release_id, user=admin_user)
    port = _install_sequence_port(monkeypatch, [
        _unified_result([{"group0": "华南", "value": 4}], total_row_count=1),
    ])

    out = runner.run("aggregate_objects", {
        "object_type": "Supplier", "metric": "count", "group_by": "region"})

    assert out["totalGroups"] == 1
    assert len(port.executed) == 1
    plan = port.executed[0]["plan"]
    assert plan["pageSize"] == 200
    assert plan["statsLimit"] == 200


def test_contains_enumeration_page_matches_registry_page_at_high_cap(
        modeled_ontology, db, admin_user, monkeypatch):
    """cap=200（高于注册表）时枚举页仍为 200：高帽环境不回归。"""
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    _set_remote_supplier_page_cap(db, oid, 200)
    runner = _remote_runner(db, oid, release_id, user=admin_user)
    port = _install_sequence_port(monkeypatch, [
        _contains_enum_result(total_row_count=1),
        _unified_result([{"value": 4}]),
    ])

    out = runner.run("aggregate_objects", {
        "object_type": "Supplier", "metric": "count",
        "filters": [dict(_CONTAINS_FILTER)]})

    assert out["value"] == 4
    assert port.executed[0]["plan"]["pageSize"] == 200


def test_chat_remote_aggregate_passes_user_and_conversation_to_port(
        client, auth_headers, modeled_ontology, db, admin_user, monkeypatch):
    """生产接线：chat（假 LLM）→ ToolRunner(user, conversation) → 统一查询端口。"""
    from app.models.model_config import ModelConfig
    from app.ontologies.agent_query import remote_query
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    db.add(ModelConfig(id=str(uuid.uuid4()), name="fake", provider="openai",
                       config_type="llm", models=["fake-model"],
                       created_by=admin_user.id))
    db.commit()

    port = _RecordingPort([{"value": 4}])
    monkeypatch.setattr(remote_query, "_port", port)

    llm_calls = {"n": 0}

    def fake_chat(call_kwargs, messages, tools):
        llm_calls["n"] += 1
        if llm_calls["n"] == 1:
            assert any(t["name"] == "aggregate_objects" for t in tools)
            return {"content": None, "usage": {"inputTokens": 10, "outputTokens": 5},
                    "tool_calls": [{"id": "tc1", "name": "aggregate_objects",
                                    "arguments": {
                                        "object_type": "Supplier", "metric": "count",
                                        "filters": [{"property": "region",
                                                     "op": "eq", "value": "华南"}]}}]}
        tool_payload = messages[-1]["content"]
        assert messages[-1]["role"] == "tool"
        assert '"dataMode":"remote"' in tool_payload
        assert '"value":4' in tool_payload
        return {"content": "华南区域的供应商共 4 家。", "tool_calls": [],
                "usage": {"inputTokens": 20, "outputTokens": 8}}

    from app.ontologies.agent_runtime import llm_bridge
    monkeypatch.setattr(llm_bridge, "chat", fake_chat)

    r = client.post(f"/api/v2/formal/ontologies/{oid}/agent/chat",
                    headers=auth_headers,
                    json={"message": "华南区域的供应商有多少？", "stream": False,
                          "releaseId": release_id})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["error"] is None
    assert data["content"] == "华南区域的供应商共 4 家。"
    assert data["steps"][0]["tool"] == "aggregate_objects"
    assert data["steps"][0]["result"]["dataMode"] == "remote"
    assert data["steps"][0]["result"]["value"] == 4

    # chat 入口把真实登录用户与会话 id 一路传到统一查询端口。
    assert len(port.executed) == 1
    assert port.executed[0]["user"].id == admin_user.id
    assert port.executed[0]["session_kind"] == "ontology_agent"
    assert port.executed[0]["session_id"] == data["conversationId"]
    assert port.executed[0]["plan"]["releases"]["objects"]["releaseId"] == release_id


# ------------------------------------------------ 6. 时间分桶 / 多维分组


def test_bucket_group_by_month_pushes_down_and_orders_asc(
        modeled_ontology, db, admin_user, monkeypatch):
    """趋势问题一次取全期间：bucket 下推源端，默认按时间桶升序。"""
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    runner = _remote_runner(db, oid, release_id, user=admin_user)
    calls = _patch_execute_sequence(monkeypatch, [
        _unified_result([{"group0": "2026-09-01", "value": 5},
                         {"group0": "2026-10-01", "value": 2}], total_row_count=2),
    ])

    out = runner.run("aggregate_objects", {
        "object_type": "Supplier", "metric": "count",
        "group_by": [{"property": "since_day", "bucket": "month"}]})

    request = calls[0]["request"]
    assert request.group_by[0].property == "since_day"
    assert request.group_by[0].bucket == "month"
    assert request.bounded_stats is True
    assert [(item.output, item.direction) for item in request.order_by] == [
        ("group0", "asc"), ("value", "desc")]
    assert out["groupBy"] == "since_day(月)"
    assert [(g["group"], g["value"]) for g in out["groups"]] == [
        ("2026-09-01", 5), ("2026-10-01", 2)]
    assert out["truncated"] is False and out["matched"] == 7


def test_multi_dimension_group_by_builds_group_columns(
        modeled_ontology, db, admin_user, monkeypatch):
    """多维分组一次返回：组键输出 group0/group1，行附 values 列表。"""
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    runner = _remote_runner(db, oid, release_id, user=admin_user)
    calls = _patch_execute_sequence(monkeypatch, [
        _unified_result([{"group0": "华南", "group1": "2026-09-01", "value": 5},
                         {"group0": "华东", "group1": "2026-10-01", "value": 2}],
                        total_row_count=2),
    ])

    out = runner.run("aggregate_objects", {
        "object_type": "Supplier", "metric": "count",
        "group_by": ["region", {"property": "since_day", "bucket": "month"}]})

    request = calls[0]["request"]
    assert [item.property for item in request.group_by] == ["region", "since_day"]
    assert [item.bucket for item in request.group_by] == [None, "month"]
    assert [item.as_ for item in request.select] == ["group0", "group1"]
    assert out["groupBy"] == "region|since_day(月)"
    assert [g["values"] for g in out["groups"]] == [
        ["华南", "2026-09-01"], ["华东", "2026-10-01"]]


def test_explicit_order_by_overrides_default(
        modeled_ontology, db, admin_user, monkeypatch):
    """显式 order_by 优先于默认（TopN 按值降序 / 趋势按桶升序）。"""
    oid = modeled_ontology["id"]
    release_id = _freeze_with_rich_remote_supplier(db, oid)
    runner = _remote_runner(db, oid, release_id, user=admin_user)
    calls = _patch_execute_sequence(monkeypatch, [
        _unified_result([], total_row_count=0),
    ])
    out = runner.run("aggregate_objects", {
        "object_type": "Supplier", "metric": "count", "group_by": "region",
        "order_by": {"output": "group", "direction": "asc"}})
    assert out["groups"] == []
    assert [(item.output, item.direction) for item in
            calls[0]["request"].order_by] == [("group0", "asc")]
