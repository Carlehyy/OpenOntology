"""SchemaInferenceStep 列级投票的类型拓宽契约（D-008 回归）。"""
from app.data_channel.pipelines.base import PipelineContext
from app.data_channel.pipelines.steps.schema_inference import SchemaInferenceStep


def _run(rows: list[dict]) -> dict:
    ctx = PipelineContext(dataset_id="d", version_no=1, route="A")
    SchemaInferenceStep().run(ctx, rows)
    return ctx.meta["inferred_schema"]


def test_mixed_float_integer_column_widens_to_float():
    # ["100.5", "200"]：float 票吸收 integer 票，不再平票裁决为 integer
    rows = [{"amount": "100.5"}, {"amount": "200"}]
    assert _run(rows)["amount"] == "float"


def test_pure_integer_column_stays_integer():
    rows = [{"n": "1"}, {"n": "2,000"}, {"n": "-7"}]
    assert _run(rows)["n"] == "integer"


def test_pure_float_column_stays_float():
    rows = [{"x": "0.5"}, {"x": "1e3"}]
    assert _run(rows)["x"] == "float"


def test_majority_integer_with_single_float_still_float():
    # 拓宽是列级语义：哪怕整数占多数，出现任一小数即整列为小数
    rows = [{"a": "1"}, {"a": "2"}, {"a": "3"}, {"a": "0.5"}]
    assert _run(rows)["a"] == "float"


def test_empty_and_null_only_columns_fall_back_to_string():
    assert _run([{"a": None}, {"a": "  "}])["a"] == "string"
    # 空数据直接原样返回，不写 inferred_schema
    ctx = PipelineContext(dataset_id="d", version_no=1, route="A")
    assert SchemaInferenceStep().run(ctx, []) == []
    assert "inferred_schema" not in ctx.meta


def test_string_integer_tie_keeps_more_specific_type():
    # 非 float/integer 混合的平票裁决行为不变（string vs integer → integer；
    # 注意 "1"/"0" 会投 boolean 票，这里用 "2" 保持纯 integer 票）
    rows = [{"m": "2"}, {"m": "x"}]
    assert _run(rows)["m"] == "integer"
