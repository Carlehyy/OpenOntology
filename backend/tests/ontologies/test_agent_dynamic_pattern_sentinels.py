"""助手动态链路的 CEP 模式哨兵：校验 → 创建 → 试跑回放 → 启用。

复用 test_agent_dynamic_sentinels 的 published_runtime fixture（一个带
status 属性与 update_property 动作的不可变发布版）。
"""
import pytest

from tests.ontologies.test_agent_dynamic_sentinels import (
    _fo,
    published_runtime,
)


def _pattern_definition(*, name="assistant_pattern_order", within=3600,
                        scan_interval=120):
    return {
        "name": name,
        "displayName": "订单状态变迁模式哨兵",
        "description": "由智能助手管理的 CEP 模式",
        "bindings": [
            {"alias": "a", "objectTypeId": "ot-order", "filter": None},
            {"alias": "b", "objectTypeId": "ot-order", "filter": None},
        ],
        "links": [],
        "condition": None,
        "conditionRows": [],
        "conditionLogic": "and",
        "primaryAlias": "a",
        "actionIds": [],
        "actionParameters": {},
        "onChange": True,
        "onSchedule": True,
        "scanIntervalSeconds": scan_interval,
        "triggerMode": "on_pattern",
        "pattern": {
            "stages": [
                {"alias": "a", "objectTypeId": "ot-order",
                 "filter": "a.status == 'pending'"},
                {"alias": "b", "objectTypeId": "ot-order",
                 "filter": "b.status == 'paid'"},
            ],
            "absence": {"enabled": True},
            "within": within,
        },
        "muted": False,
    }


def _create(client, auth_headers, runtime, definition):
    return client.post(
        f"{_fo(runtime['ontology_id'])}/agent/dynamic-sentinels",
        headers=auth_headers,
        json={
            "releaseId": runtime["release_id"],
            "definition": definition,
        },
    )


def test_dynamic_pattern_sentinel_full_governance_flow(
        client, auth_headers, published_runtime):
    runtime = published_runtime
    ontology_id = runtime["ontology_id"]

    response = _create(
        client, auth_headers, runtime, _pattern_definition())
    assert response.status_code == 201, response.text
    body = response.json()["data"] if "data" in response.json() else (
        response.json())
    sentinel_id = body["id"]
    assert body["triggerMode"] == "on_pattern"
    assert body["pattern"]["stages"][0]["alias"] == "a"
    assert body["enabled"] is False

    # 试跑：事件日志回放预演（空日志 → replayCoverage=empty，零错误通过）。
    trial = client.post(
        f"{_fo(ontology_id)}/agent/dynamic-sentinels/{sentinel_id}/trial",
        headers=auth_headers,
        json={"releaseId": runtime["release_id"]},
    )
    assert trial.status_code == 200, trial.text
    trial_body = trial.json().get("data", trial.json())
    report = trial_body.get("lastTrialReport") or {}
    assert report.get("passed") is True, report
    # 刚发布的本体事件历史必然短于回放窗口（fixture 自身产生了 created 事件）。
    assert report.get("replayCoverage") in {"empty", "partial"}

    # 启用：试跑凭据 + 校验双门槛。
    enabled = client.post(
        f"{_fo(ontology_id)}/agent/dynamic-sentinels/{sentinel_id}/enabled",
        headers=auth_headers,
        json={
            "releaseId": runtime["release_id"],
            "enabled": True,
            "expectedRevision": trial_body["definitionRevision"],
        },
    )
    assert enabled.status_code == 200, enabled.text
    enabled_body = enabled.json().get("data", enabled.json())
    assert enabled_body["enabled"] is True


def test_dynamic_pattern_window_below_scan_interval_rejected(
        client, auth_headers, published_runtime):
    runtime = published_runtime

    response = _create(client, auth_headers, runtime, _pattern_definition(
        name="assistant_pattern_bad_window",
        within=60, scan_interval=300))

    assert response.status_code == 422, response.text
    detail = response.json().get("detail") or {}
    codes = {
        error.get("code") for error in detail.get("errors") or []
    }
    assert "sentinel_pattern_window_below_scan" in codes, codes


def test_dynamic_pattern_requires_both_trigger_flags(
        client, auth_headers, published_runtime):
    runtime = published_runtime
    definition = _pattern_definition(name="assistant_pattern_no_schedule")
    definition["onSchedule"] = False

    response = _create(client, auth_headers, runtime, definition)

    # Pydantic 模型级校验先于共享发布门禁拒绝（FastAPI 422 列表结构）。
    assert response.status_code == 422, response.text
    assert "onSchedule" in response.text or "onChange" in response.text


def test_dynamic_pattern_without_trigger_mode_rejected(
        client, auth_headers, published_runtime):
    runtime = published_runtime
    definition = _pattern_definition(name="assistant_pattern_bad_mode")
    definition["triggerMode"] = "on_enter"
    definition["pattern"] = definition["pattern"]  # 携带 pattern 但非 on_pattern

    response = _create(client, auth_headers, runtime, definition)

    # Pydantic 模型级校验先拒绝（pattern 仅在 on_pattern 时允许出现）。
    assert response.status_code == 422, response.text
    assert "pattern" in response.text
