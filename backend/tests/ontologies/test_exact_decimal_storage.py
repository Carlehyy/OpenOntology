"""materialize → storage → typed-read 数值 token 保真回归。

边界链：
  CSV/湖文本 '100.5000'
    → _coerce_props_to_type（number → 精确 Decimal）
    → fo_object_instances.properties（引擎级 exact JSON serializer，
      shared.database 与 tests.conftest 使用同一个 dumps_exact）
    → typed 读端（properties ->> 'amount' cast NUMERIC）
    → 统一 SUM 保留源 scale（'100.5000'+'50.2500'+'7.0000' → '157.7500'）。

本文件覆盖真实生产物化函数与真实 SQLAlchemy 持久边界（SQLite，与生产同
serializer）；PG 上的 provider/typed SUM 由 test_exact_decimal_storage_pg.py
（显式 DSN 才跑）覆盖。dumps_exact 对不含 Decimal 的载荷直接委托 stdlib，
这是全局 serializer 不改变业务 JSON 的证据。
"""
import json
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql, sqlite

from app.ontologies.entities.models import Entity
from app.ontologies.formal_modeling.models import (
    ObjectInstance, ObjectType, OntologyFunction, PropertyFact,
)
from app.ontologies.formal_modeling.validation import (
    _is_value_of_type,
    property_value_type_issue,
)
from app.ontologies.mappings.formal_projection import project_to_formal_ontology
from app.ontologies.mappings.formal_projection_contract import (
    _coerce_props_to_type,
)
from app.ontologies.agent_runtime.read_queries import typed_property
from app.ontologies.projects.models import OntologyProject
from app.shared.exact_json import dumps_exact
from app.shared.numeric_utils import numeric_values_equal, values_equal

ORACLE_AMOUNTS = ("100.5000", "50.2500", "7.0000")


def _typed_amount() -> list[dict]:
    return [{"name": "amount", "type": "number"}]


def test_dumps_exact_renders_decimal_token_not_string():
    payload = {"contract_id": 1001, "amount": Decimal("100.5000"),
               "note": None, "tags": [1, Decimal("7.0000")]}
    encoded = dumps_exact(payload)
    assert '"amount": 100.5000' in encoded
    assert '"100.5000"' not in encoded
    assert '[1, 7.0000]' in encoded
    # 读回仍是数值（float），不是字符串化的 Decimal
    assert json.loads(encoded)["amount"] == 100.5


def test_dumps_exact_identical_to_stdlib_for_native_json():
    corpus = {
        "empty_dict": {},
        "empty_list": [],
        "basic": {"a": [1, 2.5, "x", True, None, "q\"uote"]},
        "nested": {"a": {"b": {"c": [1, [2, [3]]]}}},
        "nonstr_keys": {1: "a", 2.5: "b", True: "c", None: "d", "e": 1},
        "unicode": {"键": "值", "emoji": "😀", "ctrl": "\x00\x01\n\t"},
        "float_specials": {"nan": float("nan"), "inf": float("inf"),
                           "ninf": float("-inf"), "negzero": -0.0},
        "float_repr": [0.1, 1e-7, 1e16, 1.7976931348623157e308, 5e-324],
        "bigint": 10 ** 400,
        "tuple": (1, (2, 3), []),
        "quotes_slash": "\"\\/\b\f\n\r\t",
    }
    for name, value in corpus.items():
        assert dumps_exact(value) == json.dumps(value), name
    # 任意深度只含原生值：stdlib 快路径原样返回（不触发回退渲染器）
    deep = []
    cursor = deep
    for _ in range(400):
        nxt: list = []
        cursor.append(nxt)
        cursor = nxt
    assert dumps_exact(deep) == json.dumps(deep)


def test_dumps_exact_rejects_non_finite_decimal_and_unknown_types():
    with pytest.raises(TypeError):
        dumps_exact({"amount": Decimal("nan")})
    with pytest.raises(TypeError):
        dumps_exact({"amount": Decimal("Infinity")})
    with pytest.raises(TypeError):
        dumps_exact({"x": object()})
    with pytest.raises(TypeError):
        dumps_exact({"x": b"bytes"})


def test_dumps_exact_mixed_decimal_payload_coerces_stdlib_keys():
    payload = {1: Decimal("1.5"), None: Decimal("2.0"),
               2.5: Decimal("3")}
    encoded = dumps_exact(payload)
    assert encoded == '{"1": 1.5, "null": 2.0, "2.5": 3}'
    assert json.loads(encoded) == {"1": 1.5, "null": 2.0, "2.5": 3}


def test_derived_expression_keeps_exact_integral_bigint_semantics():
    """旧 int-first 语义：带小数尾缀的整值在表达式里仍是精确 int。"""
    from types import SimpleNamespace

    from app.ontologies.formal_modeling.function_engine import (
        evaluate_function_contract,
    )

    def function(body: str):
        return SimpleNamespace(
            id="f", name="f", display_name="f", enabled=True,
            language="expression", function_type="object", body=body,
            target_object_type_id="ot", parameters=[])

    integral = evaluate_function_contract(
        function("object.bigid + 1"),
        obj_props={"bigid": Decimal("9007199254740993.00")})
    assert integral["success"] is True
    assert integral["result"] == 9007199254740994
    assert isinstance(integral["result"], int)

    fractional = evaluate_function_contract(
        function("object.amount * 2"),
        obj_props={"amount": Decimal("1.5")})
    assert fractional["success"] is True
    assert fractional["result"] == 3.0
    assert isinstance(fractional["result"], float)


@pytest.mark.parametrize("key", ["k", 1, 2.5, True, None])
def test_dumps_exact_mixed_payload_renders_stdlib_keys_exactly(key):
    # 混合 Decimal 载荷走 fallback 时，原生 key/结构仍与 stdlib 逐字节一致。
    assert (dumps_exact({key: [1, Decimal("1.5")]})
            == json.dumps({key: [1, 1.5]}))


@pytest.mark.parametrize("key", [("a", "b"), b"k", Decimal("1"), frozenset({1})])
def test_dumps_exact_mixed_payload_rejects_non_stdlib_keys(key):
    with pytest.raises(TypeError):
        dumps_exact({key: Decimal("1.5")})


def test_number_contract_accepts_exact_decimal():
    assert _is_value_of_type(Decimal("100.5000"), "number") is True
    assert property_value_type_issue(
        {"name": "amount", "type": "number"}, Decimal("100.5000")) is None
    # 巨型/非有限 Decimal 与巨型 int 同口径拒绝
    assert _is_value_of_type(Decimal(10) ** 400, "number") is False
    assert _is_value_of_type(Decimal("nan"), "number") is False
    assert _is_value_of_type(9007199254740993, "number") is True


def test_values_equal_compares_numbers_across_representation():
    # 旧值 JSON 读回是 float，新值 coercion 是 Decimal：同一业务值必须判等。
    assert values_equal(Decimal("0.1"), 0.1) is True
    assert values_equal(Decimal("100.5000"), 100.5) is True
    assert values_equal(Decimal("7.0000"), 7) is True
    assert values_equal({"a": [Decimal("1.50")]}, {"a": [1.5]}) is True
    assert values_equal(Decimal("1.1"), 1.2) is False
    assert values_equal(True, 1) is False  # bool 不与数值混同
    assert numeric_values_equal(float("inf"), Decimal("Infinity")) is True


def test_object_instance_column_stores_exact_token_with_native_mix(db):
    """真实 ORM 持久边界：Decimal 与 native float/int 同 payload 各保语义。"""
    db.add(ObjectInstance(
        id="inst-exact-mixed", ontology_id="ont-exact-dec-test",
        object_type_id="ot-any",
        properties={"amount": Decimal("100.5000"), "score": 0.1,
                    "contract_id": 1001, "note": None},
    ))
    db.commit()
    raw = db.execute(text(
        "SELECT properties FROM fo_object_instances "
        "WHERE id = 'inst-exact-mixed'")).scalar_one()
    assert isinstance(raw, str)
    assert '"amount": 100.5000' in raw
    assert '"score": 0.1' in raw
    assert '"contract_id": 1001' in raw
    db.expire_all()
    reloaded = db.get(ObjectInstance, "inst-exact-mixed")
    assert reloaded.properties["amount"] == 100.5
    assert isinstance(reloaded.properties["amount"], float)
    assert reloaded.properties["score"] == 0.1
    assert isinstance(reloaded.properties["score"], float)
    assert reloaded.properties["contract_id"] == 1001


def test_direct_materialize_preserves_numeric_entity_json_token(db, admin_user):
    """Direct projection reads Entity JSON as text, not ORM float."""
    project = OntologyProject(
        id="ont-direct-exact", name="Direct exact", domain="test",
        status="draft", created_by=admin_user.id)
    object_type = ObjectType(
        id="ot-direct-exact", ontology_id=project.id, name="Contract",
        display_name="Contract", primary_key="pk",
        properties=[{"id": "pk", "name": "pk", "type": "string"},
                    {"id": "amount", "name": "amount", "type": "number"}])
    db.add_all([project, object_type])
    db.flush()
    db.execute(Entity.__table__.insert(), [{
        "id": "direct-e1", "ontology_id": project.id, "type": "Contract",
        "name_cn": "c1",
        "properties": {"pk": "c1", "amount": Decimal("100.5000")},
    }])
    db.commit()
    meta = {"m": {
        "entity_class": "Contract", "pk_col": "pk",
        "target_object_type_id": object_type.id,
        "property_mappings": [
            {"column": "pk", "property": "pk"},
            {"column": "amount", "property": "amount"},
        ],
    }}
    project_to_formal_ontology(db, project.id, meta)
    raw = db.execute(text(
        "SELECT properties FROM fo_object_instances "
        "WHERE ontology_id = :oid"), {"oid": project.id}).scalar_one()
    assert '"amount": 100.5000' in raw


def test_typed_read_compiles_json_extraction_not_plain_slice():
    """回归：列类型不能被自定义 TypeDecorator 吃掉 JSON 比较器语义。"""
    stmt = select(typed_property(
        {"name": "amount", "type": "number"}, "amount"))
    pg_sql = str(stmt.compile(dialect=postgresql.dialect()))
    lite_sql = str(stmt.compile(dialect=sqlite.dialect())).lower()
    assert "fo_object_instances.properties ->>" in pg_sql
    assert "json_extract" in lite_sql
    assert "fo_object_instances.properties[:" not in pg_sql


def _seed_contract_projection(db, admin_user, *, with_derived: bool = False):
    """真实生产物化输入：OntologyProject + ObjectType + Entity 行。"""
    project = OntologyProject(
        id="ont-exact-dec", name="Exact decimals", domain="test",
        status="draft", created_by=admin_user.id,
    )
    db.add(project)
    db.flush()
    props = [
        {"id": "pk", "name": "pk", "type": "string", "primaryKeyPart": 1},
        {"id": "amount", "name": "amount", "type": "number"},
        {"id": "qty", "name": "qty", "type": "integer"},
    ]
    function = None
    if with_derived:
        function = OntologyFunction(
            id="fn-tax", ontology_id=project.id, name="tax", display_name="tax",
            function_type="object", language="expression",
            target_object_type_id="ot-contract",
            body="(object.amount or 0) * 1.1",
            parameters=[], return_type="number", enabled=True,
        )
        db.add(function)
        props.append({"id": "tax", "name": "tax", "type": "number",
                      "source": "computed", "functionId": function.id})
    object_type = ObjectType(
        id="ot-contract", ontology_id=project.id, name="Contract",
        display_name="Contract", primary_key="pk", properties=props,
    )
    db.add(object_type)
    db.flush()
    rows = [
        {"id": "e1", "ontology_id": project.id, "type": "Contract",
         "name_cn": "c1",
         "properties": {"pk": "c1", "amount": "100.5000", "qty": "10"}},
        {"id": "e2", "ontology_id": project.id, "type": "Contract",
         "name_cn": "c2",
         "properties": {"pk": "c2", "amount": "50.2500", "qty": "20"}},
        {"id": "e3", "ontology_id": project.id, "type": "Contract",
         "name_cn": "c3",
         "properties": {"pk": "c3", "amount": "7.0000", "qty": "30"}},
        {"id": "e4", "ontology_id": project.id, "type": "Contract",
         "name_cn": "c4",
         "properties": {"pk": "c4", "amount": "", "qty": ""}},
    ]
    db.execute(Entity.__table__.insert(), rows)
    db.commit()
    meta = {"m": {
        "entity_class": "Contract", "pk_col": "pk",
        "target_object_type_id": object_type.id,
        "property_mappings": [
            {"column": "pk", "property": "pk"},
            {"column": "amount", "property": "amount"},
            {"column": "qty", "property": "qty"},
        ],
        "binding_context": {"source_dataset_version_id": "sv1"},
    }}
    return project, object_type, meta


def test_real_materialize_stores_exact_tokens_and_replays_idempotently(
        db, admin_user):
    project, _, meta = _seed_contract_projection(db, admin_user)

    summary = project_to_formal_ontology(db, project.id, meta)
    assert summary["object_instances"] == 4

    # 存储层原始文本（不经 ORM 反序列化）：token 逐字保留源 scale。
    raw_by_pk = {}
    for pk, raw in db.execute(text(
        "SELECT json_extract(properties, '$.pk'), properties "
        "FROM fo_object_instances WHERE ontology_id = :oid"
    ), {"oid": project.id}):
        raw_by_pk[pk] = raw
    assert "100.5000" in raw_by_pk["c1"]
    assert "50.2500" in raw_by_pk["c2"]
    assert "7.0000" in raw_by_pk["c3"]
    assert '"amount": null' in raw_by_pk["c4"]
    assert '"qty": 10' in raw_by_pk["c1"]  # 整数仍是 JSON int，不是 Decimal

    # Python 消费者读回：数值语义不变（int/float），NULL 仍为 None。
    db.expire_all()
    by_pk = {row.properties["pk"]: row.properties
             for row in db.query(ObjectInstance).filter_by(ontology_id=project.id)}
    assert by_pk["c1"]["amount"] == 100.5 and isinstance(by_pk["c1"]["amount"], float)
    assert by_pk["c1"]["qty"] == 10 and isinstance(by_pk["c1"]["qty"], int)
    assert by_pk["c4"]["amount"] is None

    facts_before = db.query(PropertyFact).filter_by(
        ontology_id=project.id).count()
    replay = project_to_formal_ontology(db, project.id, meta)
    # Decimal 新值与 JSON 读回的 float 旧值是同一业务值：重跑零伪更新/零伪事实。
    assert replay.get("updated_instances", 0) == 0
    assert db.query(PropertyFact).filter_by(
        ontology_id=project.id).count() == facts_before


def test_real_materialize_derived_expression_keeps_float_semantics(
        db, admin_user):
    """派生函数在 Decimal 输入上仍按改动前 float 语义算术（不混算崩溃）。"""
    project, _, meta = _seed_contract_projection(db, admin_user, with_derived=True)

    summary = project_to_formal_ontology(db, project.id, meta)
    assert summary["object_instances"] == 4
    db.expire_all()
    by_pk = {row.properties["pk"]: row
             for row in db.query(ObjectInstance).filter_by(ontology_id=project.id)}
    assert by_pk["c1"].computed["tax"] == pytest.approx(100.5 * 1.1)
    assert by_pk["c2"].computed["tax"] == pytest.approx(50.25 * 1.1)


def test_projection_coercion_to_storage_token_chain():
    """coercion 输出可直接被 exact serializer 落库——三值 token 链推演。"""
    tokens = []
    for raw in ORACLE_AMOUNTS:
        coerced = _coerce_props_to_type({"amount": raw}, _typed_amount())
        assert isinstance(coerced["amount"], Decimal)
        assert str(coerced["amount"]) == raw
        tokens.append(coerced["amount"])
    total = sum(tokens, Decimal(0))
    assert str(total) == "157.7500"
    stored = dumps_exact({"rows": [{"amount": t} for t in tokens]})
    assert stored.count("100.5000") == 1
    assert str(total) == "157.7500"  # SUM(token) 字面 = oracle
