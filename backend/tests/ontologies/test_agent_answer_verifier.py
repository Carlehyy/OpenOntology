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
