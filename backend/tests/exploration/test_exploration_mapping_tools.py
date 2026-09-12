"""映射工具（toolkit.get_mapping_overview / propose_mapping）：

  1. 条件挂载：仅当会话同时绑定 ontology_id + ontology_version_id 才挂载，
     未绑定/只绑本体未绑版本的会话不出现（同 apply_draft 的挂载先例）；
  2. get_mapping_overview 只读：对象清单/既有映射/待确认建议数/未映射对象/
     涉及数据集列元信息，清单截断给出 total + truncated 标记；
  3. propose_mapping 写入人工确认队列：建议落库即 pending，不进入草稿快照
     mappings（不直写映射），同一提案幂等复用；
  4. 友好工具错误：数据集/对象/列/属性不存在、类型不兼容、版本不可编辑
     各有明确错误；未绑定会话 bindingRequired；
  5. Agent 无确认路径：工具集中不存在任何 confirm/apply mapping 类工具。
"""
from __future__ import annotations

import re
import uuid

from app.data_channel.datasets.models import Dataset
from app.exploration.toolkit import (APPLY_DRAFT_TOOL, MAPPING_TOOLS,
                                     OFFICE_TOOL, TOOL_DEFS, USE_SKILL_TOOL,
                                     ExplorationToolRunner)
from app.models.ontology import OntologyProject
from app.ontologies.mappings.models import OntologyMappingSuggestion
from app.ontologies.versions.models import OntologyVersion

from tests.exploration.test_exploration import _tool_session


# ---------------------------------------------------------------- 构造工具

OBJECT_TYPES = [
    {
        "id": "ot-customer",
        "name": "Customer",
        "displayName": "客户",
        "primaryKey": "customer_id",
        "properties": [
            {"id": "p-id", "name": "customer_id", "displayName": "客户编号",
             "type": "string"},
            {"id": "p-name", "name": "customer_name", "displayName": "客户名称",
             "type": "string"},
            {"id": "p-age", "name": "age", "displayName": "年龄",
             "type": "integer"},
        ],
    },
    {
        "id": "ot-order",
        "name": "Order",
        "displayName": "订单",
        "primaryKey": "order_id",
        "properties": [
            {"id": "p-oid", "name": "order_id", "displayName": "订单编号",
             "type": "string"},
        ],
    },
]


def _snapshot(mappings=None, object_types=None):
    return {
        "objectTypes": OBJECT_TYPES if object_types is None else object_types,
        "linkTypes": [],
        "actions": [],
        "functions": [],
        "sentinels": [],
        "mappings": mappings or [],
        "linkMappings": [],
    }


def _make_ontology(db, admin_user) -> OntologyProject:
    project = OntologyProject(
        id=f"ont-{uuid.uuid4().hex[:8]}", name="客户本体", domain="test",
        created_by=admin_user.id)
    db.add(project)
    db.commit()
    return project


def _make_version(db, project, snapshot=None, *, status="editing",
                  kind="draft", number="v0.1") -> OntologyVersion:
    version = OntologyVersion(
        id=f"ver-{uuid.uuid4().hex[:8]}",
        ontology_id=project.id,
        version_number=number,
        node_kind=kind,
        lifecycle_status=status,
        snapshot_formal=_snapshot() if snapshot is None else snapshot,
        created_by=project.created_by,
    )
    db.add(version)
    db.commit()
    return version


def _make_dataset(db, name="客户表", columns=None) -> Dataset:
    columns = columns if columns is not None else [
        ("cust_id", "客户编号", "string"),
        ("cust_name", "客户名称", "string"),
        ("age", "年龄", "integer"),
    ]
    dataset = Dataset(
        id=f"ds-{uuid.uuid4().hex[:8]}",
        name=name,
        kind="structured",
        schema_json={
            "types_source": "declared",
            "primary_key": "cust_id",
            "columns_typed": [
                {"name": col, "type": col_type, "display_name": display}
                for col, display, col_type in columns
            ],
        },
    )
    db.add(dataset)
    db.commit()
    return dataset


def _bound_runner(db, admin_user, project, version, canvas=None):
    """绑定本体版本的会话 + 工具执行器。"""
    row = _tool_session(db, ontology_id=project.id, canvas=canvas)
    row.ontology_version_id = version.id
    db.commit()
    return row, ExplorationToolRunner(db, row, user=admin_user)


# ---------------------------------------------------------------- 条件挂载

def _mounted_tool_names(db, monkeypatch, session_row):
    from app.exploration import orchestrator as OR

    monkeypatch.setattr(OR, "select_llm_model_config",
                        lambda db, model_id=None: object())
    monkeypatch.setattr(OR, "llm_call_kwargs", lambda cfg: {"model": "fake"})
    seen: list[list[str]] = []

    def fake_chat(call_kwargs, messages, tools):
        seen.append([tool["name"] for tool in tools])
        return {"content": "好", "tool_calls": [], "usage": None}

    monkeypatch.setattr(OR.llm_bridge, "chat", fake_chat)
    list(OR.run_exploration_turn(db, session_row.id, user=object(), message="你好"))
    return seen[-1]


def test_mapping_tools_mounted_only_for_version_bound_sessions(db, monkeypatch):
    """映射工具仅对「绑定本体版本」的会话挂载：未绑定、只绑本体未绑版本都不出现。"""
    from app.exploration import canvas as C

    unbound = _tool_session(db, canvas=C.empty_canvas())
    names = _mounted_tool_names(db, monkeypatch, unbound)
    assert "get_mapping_overview" not in names
    assert "propose_mapping" not in names

    # 只绑本体、未绑版本：同样不挂载（工具锚定具体版本）
    half_bound = _tool_session(db, ontology_id=str(uuid.uuid4()),
                               canvas=C.empty_canvas())
    names = _mounted_tool_names(db, monkeypatch, half_bound)
    assert "get_mapping_overview" not in names
    assert "propose_mapping" not in names

    bound = _tool_session(db, ontology_id=str(uuid.uuid4()),
                          canvas=C.empty_canvas())
    bound.ontology_version_id = str(uuid.uuid4())
    db.commit()
    names = _mounted_tool_names(db, monkeypatch, bound)
    assert "get_mapping_overview" in names
    assert "propose_mapping" in names


# ---------------------------------------------------------------- get_mapping_overview

def test_mapping_overview_shape_and_counts(db, admin_user):
    project = _make_ontology(db, admin_user)
    mapped_dataset = _make_dataset(db)
    version = _make_version(db, project, _snapshot(mappings=[{
        "id": "map-1",
        "curatedDatasetId": mapped_dataset.id,
        "entityClass": "Customer",
        "targetObjectTypeId": "ot-customer",
        "fieldMapping": {"cust_id": "customer_id", "__primary_key__": "cust_id"},
    }]))
    _, runner = _bound_runner(db, admin_user, project, version)

    result = runner.run("get_mapping_overview", {})
    assert "error" not in result, result
    assert result["versionId"] == version.id
    assert result["editable"] is True
    assert result["objectsTotal"] == 2
    assert result["objectsTruncated"] is False
    customer = next(obj for obj in result["objects"] if obj["name"] == "Customer")
    assert {prop["name"] for prop in customer["properties"]} == {
        "customer_id", "customer_name", "age"}
    # 既有映射清单：字段数不含 __primary_key__ 等保留键
    assert result["mappingsTotal"] == 1
    assert result["mappings"][0]["fieldCount"] == 1
    assert result["pendingSuggestions"] == 0
    # Customer 已被映射，Order 未映射
    assert result["unmappedObjects"] == {"count": 1, "names": ["Order"]}
    # 涉及的数据集带只读列元信息
    dataset_entry = next(d for d in result["datasets"]
                         if d["id"] == mapped_dataset.id)
    assert dataset_entry["name"] == "客户表"
    assert dataset_entry["columnsTotal"] == 3
    assert {col["name"] for col in dataset_entry["columns"]} == {
        "cust_id", "cust_name", "age"}


def test_mapping_overview_truncation_and_focus_dataset(db, admin_user):
    project = _make_ontology(db, admin_user)
    version = _make_version(db, project)
    dataset = _make_dataset(db, name="订单表", columns=[
        ("order_id", "订单编号", "string")])
    _, runner = _bound_runner(db, admin_user, project, version)

    truncated = runner.run("get_mapping_overview", {"object_limit": 1})
    assert truncated["objectsTruncated"] is True
    assert truncated["objectsTotal"] == 2
    assert len(truncated["objects"]) == 1

    # dataset_id 聚焦：把尚未参与映射的数据集列清单拉进来
    focused = runner.run("get_mapping_overview", {"dataset_id": dataset.id})
    entry = next(d for d in focused["datasets"] if d["id"] == dataset.id)
    assert entry["columnsTotal"] == 1
    assert entry["columns"][0]["name"] == "order_id"

    missing = runner.run("get_mapping_overview", {"dataset_id": "ds-ghost"})
    assert "不存在" in missing["error"]


def test_mapping_overview_release_version_readonly(db, admin_user):
    project = _make_ontology(db, admin_user)
    release = _make_version(db, project, kind="release", status="released",
                            number="v0")
    _, runner = _bound_runner(db, admin_user, project, release)
    result = runner.run("get_mapping_overview", {})
    assert "error" not in result
    assert result["editable"] is False


# ---------------------------------------------------------------- propose_mapping

def test_propose_mapping_enqueues_pending_suggestion(db, admin_user):
    project = _make_ontology(db, admin_user)
    version = _make_version(db, project)
    dataset = _make_dataset(db)
    _, runner = _bound_runner(db, admin_user, project, version)

    result = runner.run("propose_mapping", {
        "dataset_id": dataset.id,
        "target_object": "Customer",
        "field_mapping": {"cust_id": "customer_id", "cust_name": "customer_name"},
        "primary_key_column": "cust_id",
        "note": "列名与属性同名",
    })
    assert "error" not in result, result
    assert result["status"] == "pending"
    assert result["reused"] is False
    assert result["objectTypeId"] == "ot-customer"
    assert result["fieldCount"] == 2
    assert "人工确认队列" in result["note"] and "确认后才生效" in result["note"]

    # 建议落库且状态为待确认
    stored = db.query(OntologyMappingSuggestion).all()
    assert len(stored) == 1
    row = stored[0]
    assert row.id == result["suggestionIds"][0]
    assert row.status == "pending"
    assert row.source == "agent"
    assert row.version_id == version.id
    assert row.dataset_id == dataset.id
    assert row.field_mapping == {"cust_id": "customer_id",
                                 "cust_name": "customer_name"}
    assert row.primary_key_column == "cust_id"

    # 不直写映射：草稿快照的 mappings 不为建议所动
    db.refresh(version)
    assert (version.snapshot_formal.get("mappings") or []) == []

    # overview 立刻可见待确认数
    overview = runner.run("get_mapping_overview", {})
    assert overview["pendingSuggestions"] == 1

    # 同一提案幂等复用，不堆叠重复建议
    again = runner.run("propose_mapping", {
        "dataset_id": dataset.id,
        "target_object": "ot-customer",
        "field_mapping": {"cust_id": "customer_id", "cust_name": "customer_name"},
        "primary_key_column": "cust_id",
    })
    assert again["reused"] is True
    assert again["suggestionIds"] == result["suggestionIds"]
    assert db.query(OntologyMappingSuggestion).count() == 1


def test_propose_mapping_friendly_validation_errors(db, admin_user):
    project = _make_ontology(db, admin_user)
    version = _make_version(db, project)
    dataset = _make_dataset(db)
    _, runner = _bound_runner(db, admin_user, project, version)

    def propose(**overrides):
        args = {"dataset_id": dataset.id, "target_object": "Customer",
                "field_mapping": {"cust_id": "customer_id"}}
        args.update(overrides)
        return runner.run("propose_mapping", args)

    # 数据集不存在
    missing_dataset = propose(dataset_id="ds-ghost")
    assert "不存在" in missing_dataset["error"]

    # 目标对象不存在：错误附可用对象清单
    missing_object = propose(target_object="Ghost")
    assert "object_not_found" == missing_object["code"]
    assert "Customer" in missing_object["error"]

    # 数据集列不存在：错误附可用列清单
    bad_column = propose(field_mapping={"ghost_col": "customer_id"})
    assert bad_column["code"] == "unknown_columns"
    assert "cust_id" in bad_column["error"]

    # 本体属性不存在
    bad_property = propose(field_mapping={"cust_id": "ghost_prop"})
    assert bad_property["code"] == "unknown_properties"
    assert "customer_id" in bad_property["error"]

    # 类型不兼容（integer 列 → string 属性）整体拒绝，不落半截建议
    incompatible = propose(field_mapping={"age": "customer_name"})
    assert incompatible["code"] == "incompatible_types"
    assert "age" in incompatible["error"]

    # 主键列必须是数据集已有列
    bad_pk = propose(primary_key_column="ghost_pk")
    assert bad_pk["code"] == "unknown_columns"

    # 参数形状错误在工具边界即被拒
    assert "field_mapping" in runner.run("propose_mapping", {
        "dataset_id": dataset.id, "target_object": "Customer"})["error"]
    assert "dataset_id" in runner.run("propose_mapping", {})["error"]

    # 以上全部失败均不产生建议行
    assert db.query(OntologyMappingSuggestion).count() == 0


def test_propose_mapping_version_guards(db, admin_user):
    from app.exploration import canvas as C

    project = _make_ontology(db, admin_user)
    dataset = _make_dataset(db)
    args = {"dataset_id": dataset.id, "target_object": "Customer",
            "field_mapping": {"cust_id": "customer_id"}}

    # 未绑定会话：先拦绑定
    unbound = _tool_session(db, canvas=C.empty_canvas())
    runner = ExplorationToolRunner(db, unbound, user=admin_user)
    result = runner.run("propose_mapping", args)
    assert result.get("bindingRequired") is True
    assert "绑定" in result["error"]

    # 发布版本不可写（复用建议队列自身的版本守卫）
    release = _make_version(db, project, kind="release", status="released",
                            number="v0")
    _, runner = _bound_runner(db, admin_user, project, release)
    result = runner.run("propose_mapping", args)
    assert "发布版本不可修改" in result["error"]
    assert result["code"] == "immutable_release"

    # 试跑态冻结
    trial = _make_version(db, project, status="trial_ready", number="v0.2")
    _, runner = _bound_runner(db, admin_user, project, trial)
    result = runner.run("propose_mapping", args)
    assert result["code"] == "trial_snapshot_frozen"

    # 空本体（草稿无对象）拒绝并引导建模
    empty = _make_version(db, project, _snapshot(object_types=[]),
                          number="v0.3")
    _, runner = _bound_runner(db, admin_user, project, empty)
    result = runner.run("propose_mapping", args)
    assert result["code"] == "empty_ontology"

    # 版本不属于会话绑定的本体：版本守卫按 (ontology, version) 双锚 404
    other_project = _make_ontology(db, admin_user)
    foreign = _make_version(db, other_project, number="v9.9")
    _, runner = _bound_runner(db, admin_user, project, foreign)
    result = runner.run("propose_mapping", args)
    assert result["code"] == "404"

    assert db.query(OntologyMappingSuggestion).count() == 0


def test_propose_mapping_rejects_force(db, admin_user):
    project = _make_ontology(db, admin_user)
    version = _make_version(db, project)
    dataset = _make_dataset(db)
    _, runner = _bound_runner(db, admin_user, project, version)
    result = runner.run("propose_mapping", {
        "dataset_id": dataset.id, "target_object": "Customer",
        "field_mapping": {"cust_id": "customer_id"}, "force": True})
    assert "force" in result["error"]
    assert db.query(OntologyMappingSuggestion).count() == 0


# ---------------------------------------------------------------- 提示词纪律

def test_mapping_prompt_block_only_for_bound_sessions(db, admin_user):
    """数据映射工作方式条目仅注入绑定本体版本的会话（与工具挂载同口径）。"""
    from app.exploration import canvas as C
    from app.exploration.context_builder import _system_prompt

    unbound = _tool_session(db, canvas=C.empty_canvas())
    assert "数据映射" not in _system_prompt(unbound)

    project = _make_ontology(db, admin_user)
    version = _make_version(db, project)
    bound, _ = _bound_runner(db, admin_user, project, version)
    prompt = _system_prompt(bound)
    assert "数据映射" in prompt
    assert "get_mapping_overview" in prompt and "propose_mapping" in prompt
    assert "确认后才生效" in prompt


# ---------------------------------------------------------------- Agent 无确认路径

def test_no_confirm_or_apply_mapping_tool_exists(db, admin_user):
    """负向断言：确认/应用映射只发生在映射视图队列 UI，工具集中不存在此类工具。"""
    names = {tool["name"] for tool in (
        [*TOOL_DEFS, *MAPPING_TOOLS, APPLY_DRAFT_TOOL, OFFICE_TOOL,
         USE_SKILL_TOOL])}
    mapping_related = sorted(name for name in names if "mapping" in name)
    assert mapping_related == ["get_mapping_overview", "propose_mapping"]
    assert not any(re.search(r"confirm|apply", name) for name in mapping_related)

    # 执行器层同样没有分发路径：任何 confirm/apply mapping 调用都是未知工具
    project = _make_ontology(db, admin_user)
    version = _make_version(db, project)
    _, runner = _bound_runner(db, admin_user, project, version)
    for fake in ("confirm_mapping", "apply_mapping", "confirm_mapping_suggestion",
                 "approve_mapping"):
        result = runner.run(fake, {})
        assert "未知工具" in result["error"]
