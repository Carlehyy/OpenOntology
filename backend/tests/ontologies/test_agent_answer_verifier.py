"""确定性结论校验器（answer_verifier.py）的纯函数单元测试。"""
from decimal import Decimal

import pytest

from app.ontologies.agent_runtime.answer_verifier import (
    answer_numbers,
    correction_prompt,
    result_number_universe,
    unverified_notice,
    verify_answer,
)


def _step(result: dict) -> dict:
    return {"tool": "aggregate_objects", "result": result}


class TestNumberExtraction:
    def test_plain_and_thousands_and_decimals(self):
        assert answer_numbers("共 12 个，金额 1,234.56 元") == [12.0, 1234.56]

    def test_negative_and_percent(self):
        assert answer_numbers("下降 -3.5%，共 -12 个") == [-3.5, -12.0]

    def test_years_are_filtered(self):
        assert answer_numbers("2026 年发布，共 5 个版本") == [5.0]

    def test_no_numbers(self):
        assert answer_numbers("没有数字的回答。") == []

    def test_large_plain_integer_and_thousands_are_not_split(self):
        assert answer_numbers("规模 2300000，另有 2,300,000 条") == [2300000.0, 2300000.0]

    def test_adjacent_unsafe_bigints_remain_distinct(self):
        steps = [_step({"value": "9007199254740993"})]
        assert verify_answer("总数 9007199254740992。", steps).passed is False
        assert verify_answer("总数 9007199254740993。", steps).passed is True


class TestDateMasking:
    def test_date_syntaxes_are_masked(self):
        for date in ("2026年5月", "2026年5月1日", "2026-05", "2026-05-01",
                     "2026/05/01", "5月", "5月份", "5月1日", "2026 年 5 月 1 日",
                     "2026 / 05 / 01", "5 月"):
            assert answer_numbers(f"{date}新增 3 条记录") == [3.0], date

    def test_durations_and_counts_stay_verifiable(self):
        assert answer_numbers("项目持续 5 个月") == [5.0]
        assert answer_numbers("共5个里程碑") == [5.0]

    def test_amount_with_dot_is_not_masked_as_date(self):
        # "." 不是日期分隔符：金额不走日期屏蔽，抽取结果与历史行为一致
        # （"2026" 头仍被年份豁免剥离，".5" 中的 5 照旧单独提取）。
        assert answer_numbers("金额 2026.5万元，共 3 笔") == [5.0, 3.0]

    def test_date_strings_in_results_do_not_leak_numbers(self):
        assert result_number_universe([_step({"window": "2026-05"})]) == set()
        assert result_number_universe([_step({"label": "5月1日"})]) == set()

    def test_date_label_cannot_back_count_claim(self):
        steps = [_step({"window": "2026年5月", "total": 1})]
        result = verify_answer("2026年5月共 5 个。", steps)
        assert result.passed is False
        assert result.unverified == [Decimal("5")]

    def test_failed_tool_with_date_restatement_only_passes(self):
        steps = [{"tool": "search_objects", "result": {"error": "对象类型不存在"}}]
        result = verify_answer("2026年5月的待支付订单数据暂时无法获取。", steps)
        assert result.passed is True
        assert result.unverified == []


class TestNumberUniverse:
    def test_collects_from_nested_results(self):
        steps = [_step({
            "objectType": "订单",
            "metric": "count",
            "value": 7,
            "groups": [{"value": 3}, {"value": 4}],
            "label": "SO-001",
        })]
        universe = result_number_universe(steps)
        assert 7.0 in universe and 3.0 in universe and 4.0 in universe

    def test_ratio_variants(self):
        universe = result_number_universe([_step({"value": 0.35})])
        assert 0.35 in universe and 35.0 in universe  # 百分比表达兼容

    def test_incomplete_or_inexact_input_never_backs_precise_claim(self):
        for status in ({"partial": True}, {"exact": False}, {"completeness": "unknown"}):
            assert result_number_universe([_step({"value": 2300000, **status})]) == set()
        assert verify_answer("总量 2300000", [_step({"total": 2300000,
            "truncated": True, "exact": True, "completeness": "complete"})]).passed

    def test_decimal_claim_and_lossless_summary(self):
        result = verify_answer("金额 9007199254740993.0123456789。",
                               [_step({"value": "9007199254740993.0123456788"})])
        assert result.passed is False
        assert result.summary()["unverified"] == ["9007199254740993.0123456789"]

    def test_error_and_truncated_results_do_not_back_claims(self):
        universe = result_number_universe([
            {"tool": "search_objects", "result": {"error": "越界"}},
            {"tool": "search_objects", "result": {"_truncated": True, "preview": "9"}},
        ])
        assert universe == set()


class TestCitationAndIdentifierMasking:
    def test_bracket_citations_are_not_numeric_claims(self):
        assert answer_numbers("结论[1]成立，另见 [2,3] 与「4」、『5』。") == []
        assert answer_numbers("编号「12」的引用") == []

    def test_citation_numbers_do_not_trigger_warning(self):
        steps = [_step({"value": 42})]
        result = verify_answer("共 42 个对象[1]，详见「2」。", steps)
        assert result.passed is True
        assert result.unverified == []

    def test_plain_numbers_next_to_brackets_stay_verifiable(self):
        # 方括号外的数字仍是数值断言：3 须有证据，42 已有证据。
        steps = [_step({"value": 42})]
        result = verify_answer("共 42 个（见[1]），另约 3 个。", steps)
        assert result.passed is False
        assert result.unverified == [Decimal("3")]

    def test_identifier_keys_do_not_seed_universe(self):
        steps = [_step({
            "queryId": "6f1e2a3c-4b5d-6789-abcd-ef0123456789",
            "releaseId": "rel-1",
            "instanceId": "inst-88",
            "observedAt": "2026-11-03T10:20:30+00:00",
            "value": 42,
        })]
        universe = result_number_universe(steps)
        assert 42.0 in universe
        assert 6789.0 not in universe and 88.0 not in universe
        assert 10.0 not in universe and 20.0 not in universe and 30.0 not in universe

    def test_uuid_and_stamp_values_do_not_seed_universe(self):
        steps = [_step({
            "token": "6f1e2a3c-4b5d-6789-abcd-ef0123456789",
            "stamp": "2026-11-03T10:20:30.123+00:00",
            "cursor": "2026-11-03T09:08:07Z",
        })]
        assert result_number_universe(steps) == set()

    def test_fabricated_number_not_rescued_by_identifiers(self):
        steps = [_step({
            "queryId": "1f0e2a3c-4b5d-6789-abcd-ef0123456789",
            "observedAt": "2026-11-03T10:20:30+00:00",
            "value": 42,
        })]
        result = verify_answer("共 43 个。", steps)
        assert result.passed is False
        assert result.unverified == [Decimal("43")]


class TestAnswerSideTypoMasking:
    """答案侧排版/标识符屏蔽：序号、时钟、编号、区间不是数值断言。"""

    def test_ordered_list_ordinals_are_masked(self):
        assert answer_numbers("1. 华东销量 100\n2. 华南 200\n3. 华北 300") == [100.0, 200.0, 300.0]
        assert answer_numbers("步骤（1）先筛（2）再聚合（3）输出，合计 42") == [42.0]
        assert answer_numbers("1、2、3 未验证，但 7 天里有 21 条") == [7.0, 21.0]
        assert answer_numbers("共 3、4 两个值，总量 9") == [9.0]

    def test_decimal_after_ordinal_dot_is_preserved(self):
        # "1.5 万" 中的 1.5 是数值断言，不能被行首序号规则误伤。
        assert answer_numbers("1.5 万订单") == [1.5]

    def test_clock_and_space_datetime_are_masked(self):
        assert answer_numbers("时间 2026-11-03 10:20:30，命中 13 条") == [13.0]
        assert answer_numbers("2026年11月3日 13:05 更新了 5 条") == [5.0]

    def test_codes_versions_and_labeled_ids_are_masked(self):
        assert answer_numbers("订单号 SO-001，客户 ID 10086，版本 v1.2.3，总量 500") == [500.0]
        assert answer_numbers("epoch 1730692830 对应毫秒 1730692830123，实际新增 5 条") == [5.0]
        assert answer_numbers("工号 10086 在岗，组内 12 人") == [12.0]

    def test_numeric_ranges_are_masked_whole(self):
        assert answer_numbers("区间 1-2 个，第1名得分 88") == [88.0]
        assert answer_numbers("电话 010-12345678，成功率 92%") == [92.0]
        assert answer_numbers("覆盖 2026-2027 年，共 25 期") == [25.0]

    def test_ordinal_answers_do_not_trigger_warning(self):
        steps = [_step({"groups": [
            {"group": "华东", "value": 100}, {"group": "华南", "value": 200}]})]
        answer = "销量排名：\n1. 华东 100\n2. 华南 200"
        result = verify_answer(answer, steps)
        assert result.passed is True
        assert result.unverified == []

    def test_result_side_identifiers_extended_keys_do_not_seed(self):
        steps = [_step({
            "created_at": "2026-11-03 10:20:30",
            "ids": [1, 2, 3],
            "epoch": 1730692830,
            "value": 42,
        })]
        universe = result_number_universe(steps)
        assert 42.0 in universe
        assert not any(v in universe for v in (1.0, 2.0, 3.0, 1730692830.0))

    def test_query_plan_and_page_size_do_not_seed_universe(self):
        # querySpec/pageSize 是请求计划而不是观测结果：计划中的 50/999
        # 不能为回答里的同值数字背书。
        steps = [_step({
            "rows": [{"event_id": "e1"}],
            "returned": 1,
            "totalRowCount": 1,
            "pageSize": 50,
            "hasNext": True,
            "querySpec": {
                "pageSize": 50,
                "query": {"where": [{"property": "amount", "op": "gt",
                                     "value": 999}]},
            },
        })]
        universe = result_number_universe(steps)
        assert 1.0 in universe
        assert 50.0 not in universe and 999.0 not in universe
        result = verify_answer("共 50 条，金额大于 999。", steps)
        assert result.passed is False
        assert result.unverified == [Decimal("50"), Decimal("999")]
        # 只剔除结果顶层的请求元数据；业务行里恰好叫 pageSize 的字段仍是
        # 观测值（不能因为排除计划参数而误伤业务属性）。
        assert 7.0 in result_number_universe(
            [_step({"rows": [{"pageSize": 7}]})])


class TestVerifyAnswer:
    def test_pass_when_all_numbers_backed(self):
        steps = [_step({"total": 42, "returned": 2})]
        result = verify_answer("共 42 个对象，返回 2 条。", steps)
        assert result.passed
        assert result.unverified == []
        assert result.summary()["passed"] is True

    def test_fail_when_number_not_backed(self):
        steps = [_step({"total": 42})]
        result = verify_answer("共 999 个对象。", steps)
        assert not result.passed
        assert 999.0 in result.unverified

    def test_pass_when_no_steps_or_no_numbers(self):
        assert verify_answer("没有数字。", []).passed
        assert verify_answer("", [_step({"total": 42})]).passed


class TestPrompts:
    def test_correction_prompt_names_values(self):
        text = correction_prompt([999.0, 0.5])
        assert "999" in text and "0.5" in text
        assert "不得编造" in text

    def test_notice_names_values(self):
        text = unverified_notice([999.0])
        assert "999" in text and "未能与工具返回结果对应" in text
