#!/usr/bin/env python3
"""Run the Sentinel CEP production path against a live (staging) backend.

This is intentionally not a pytest/TestClient test.  It talks to a running
OpenOntology backend, publishes an ontology with three CEP sentinels
(temporal condition / sequence+absence / windowed aggregate), then feeds
real instance changes through the public API and verifies the complete
chain: 变更 → 事件日志 → 水位推进 → 模式命中 → 动作 → 触发记录.

Runtime instance writes go through the direct instance API, which is
rejected only when ``settings.environment == "production"``; run this
script against an isolated staging/canary environment (真实环境 E2E 门禁),
never against production.  Evidence belongs in ``.artifacts/``.

治理前置（当前脚本尚未自动化，跑通前需人工补齐或改用既有发布本体
+ 动态哨兵链路验证）：
  1. trial_object_mapping_required —— 草稿需要至少一个绑定数据集并完成
     全部存储属性映射的对象实体（参照 run_sentinel_real_data_e2e.py 的
     create_table + upload + mapping 流程）；
  2. semantic_business_missing —— 试跑/发布门禁要求业务语义层
     （snapshot_semantic，探索 apply 正门写入）；全新 API 直建的本体
     语义层为空，结构会被全部计为缺失。

Covered invariants:
  M1  changed_within / prev 时间算子按事件日志判定（UTC 基准）；
  M2  序列模式完成即触发一次、水位幂等（手动重跑不再放炮）、
      缺失分支超时以 edge=absence 触发、聚合滞回不重复放炮；
  治理  发布门禁接受合法 pattern；CDC 全程无死信、worker 存活。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import httpx

POLL_SECONDS = 0.5


class CheckFailed(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def unwrap(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


class Api:
    def __init__(self, base_url: str) -> None:
        self.client = httpx.Client(base_url=base_url.rstrip("/"), timeout=90.0)
        self.headers: dict[str, str] = {}

    def close(self) -> None:
        self.client.close()

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = dict(self.headers)
        headers.update(kwargs.pop("headers", {}))
        response = self.client.request(method, path, headers=headers, **kwargs)
        if not response.is_success:
            raise CheckFailed(
                f"{method} {path} failed ({response.status_code}): "
                f"{response.text[:3000]}"
            )
        if response.status_code == 204:
            return None
        return unwrap(response.json())

    def login(self, username: str, password: str) -> None:
        data = self.request(
            "POST", "/api/v1/auth/login",
            json={"username": username, "password": password},
        )
        token = data.get("access_token")
        require(bool(token), "login response did not contain access_token")
        self.headers = {"Authorization": f"Bearer {token}"}


def wait_for_cdc(
        api: Api, ontology_id: str, *, timeout_seconds: float = 120.0) -> dict:
    """Wait until the durable CDC consumer is healthy and quiescent."""
    deadline = time.monotonic() + timeout_seconds
    last_status: dict = {}
    while time.monotonic() < deadline:
        status = api.request(
            "GET",
            f"/api/v1/ontologies/{ontology_id}/sentinels/cdc-status",
        )
        last_status = status
        durable = status.get("durable") or {}
        require(status.get("worker_alive") is True, f"CDC worker dead: {status}")
        require(
            int(durable.get("dead") or 0) == 0,
            f"CDC dead letters appeared: {status}")
        if status.get("healthy") is True and status.get("quiescent") is True:
            return status
        time.sleep(POLL_SECONDS)
    raise CheckFailed(
        f"CDC did not become quiescent in {timeout_seconds:.0f}s: {last_status}")


def firings_of(api: Api, ontology_id: str, sentinel_id: str,
               *, include_history: bool = True) -> list[dict]:
    rows = api.request(
        "GET",
        f"/api/v1/ontologies/{ontology_id}/sentinels/firings",
        params={
            "sentinel_id": sentinel_id,
            "limit": 50,
            **({"include_history": True} if include_history else {}),
        },
    )
    return rows if isinstance(rows, list) else (rows.get("items") or [])


def wait_for(predicate, *, timeout_seconds: float, description: str):
    deadline = time.monotonic() + timeout_seconds
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(POLL_SECONDS)
    raise CheckFailed(f"timed out waiting for {description}: last={last!r}")


def update_instance(api: Api, ontology_id: str, instance_id: str,
                    properties: dict) -> Any:
    return api.request(
        "PUT",
        f"/api/v2/formal/ontologies/{ontology_id}/instances/{instance_id}",
        json={"properties": properties},
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    api = Api(args.base_url)
    try:
        api.login(args.username, args.password)
        suffix = f"{int(time.time() * 1000):x}"
        device_type_id = f"ot-cep-device-{suffix}"
        mark_action_id = f"act-cep-mark-{suffix}"
        temporal_sentinel_id = f"sentinel-cep-temporal-{suffix}"
        sequence_sentinel_id = f"sentinel-cep-sequence-{suffix}"
        aggregate_sentinel_id = f"sentinel-cep-aggregate-{suffix}"

        ontology = api.request(
            "POST", "/api/v1/ontologies",
            json={
                "name": f"哨兵CEP真实闭环-{suffix}",
                "domain": "制造",
                "description": "Sentinel CEP production-path verification",
            },
        )
        ontology_id = str(ontology["id"])
        tree = api.request(
            "GET", f"/api/v2/ontologies/{ontology_id}/version-tree")
        root = next(
            item for item in tree["versions"]
            if item["version_number"] == "v0"
        )
        draft = api.request(
            "POST",
            f"/api/v2/ontologies/{ontology_id}/versions/{root['id']}/drafts",
            json={
                "versionLabel": "CEP 哨兵验证",
                "description": "时间算子/序列缺失/窗口聚合真实链路",
            },
        )

        workspace = {
            "baseRevision": f"{draft['revision']}:{draft['snapshot_hash']}",
            "version": draft["version_number"],
            "objectTypes": [{
                "id": device_type_id,
                "name": "CepDevice",
                "displayName": "CEP 验证设备",
                "primaryKey": "device_no",
                "positionX": 100,
                "positionY": 100,
                "properties": [
                    {"id": "p-no", "name": "device_no",
                     "displayName": "设备编号", "type": "string",
                     "required": True},
                    {"id": "p-status", "name": "status",
                     "displayName": "状态", "type": "string"},
                    {"id": "p-temp", "name": "temp",
                     "displayName": "温度", "type": "number"},
                ],
            }],
            "linkTypes": [],
            "actions": [{
                "id": mark_action_id,
                "name": "mark_cep_seen",
                "displayName": "标记 CEP 已观察",
                "objectTypeId": device_type_id,
                "parameters": [],
                "requiresApproval": False,
                "rules": [{
                    "type": "update_property", "name": "set-seen",
                    "enabled": True, "order": 0,
                    "config": {
                        "targetProperty": "status",
                        "valueSource": "constant",
                        "value": "\"cep_seen\"",
                    },
                }],
            }],
            "functions": [],
            "instances": [
                {
                    "id": f"cep-dev-1-{suffix}",
                    "objectTypeId": device_type_id,
                    "properties": {
                        "device_no": "CEP-001", "status": "submitted",
                        "temp": 70,
                    },
                },
                {
                    "id": f"cep-dev-2-{suffix}",
                    "objectTypeId": device_type_id,
                    "properties": {
                        "device_no": "CEP-002", "status": "idle",
                        "temp": 40,
                    },
                },
            ],
            "linkInstances": [],
        }
        saved = api.request(
            "PUT",
            f"/api/v2/ontologies/{ontology_id}/versions/{draft['id']}/workspace",
            json=workspace,
        )

        def stage_bindings(*aliases):
            return [
                {"alias": alias, "objectTypeId": device_type_id, "filter": None}
                for alias in aliases
            ]

        sentinels_payload = [
            # M1：时间增强条件（changed_within 多属性时间对齐）。
            {
                "id": temporal_sentinel_id,
                "name": "cep_temporal_alignment",
                "displayName": "温度湿度时间对齐",
                "bindings": stage_bindings("a"),
                "links": [],
                "condition": (
                    "a.temp > 80 and changed_within('a.temp', 600)"),
                "conditionRows": [], "conditionLogic": "and",
                "primaryAlias": "a",
                "actionIds": [], "actionParameters": {},
                "onChange": True, "onSchedule": False,
                "scanIntervalSeconds": 300,
                "triggerMode": "on_enter",
                "muted": False, "enabled": True, "status": "draft",
            },
            # M2：序列 + 缺失分支（submitted → paid）。
            {
                "id": sequence_sentinel_id,
                "name": "cep_status_sequence",
                "displayName": "状态变迁序列（含缺失分支）",
                "bindings": stage_bindings("a", "b"),
                "links": [],
                "condition": None,
                "pattern": {
                    "stages": [
                        {"alias": "a", "objectTypeId": device_type_id,
                         "filter": "a.status == 'submitted'"},
                        {"alias": "b", "objectTypeId": device_type_id,
                         "filter": "b.status == 'paid'", "within": 180},
                    ],
                    "absence": {"enabled": True},
                    "within": 180,
                },
                "conditionRows": [], "conditionLogic": "and",
                "primaryAlias": "a",
                "actionIds": [mark_action_id], "actionParameters": {},
                "onChange": True, "onSchedule": True,
                "scanIntervalSeconds": 60,
                "triggerMode": "on_pattern",
                "muted": False, "enabled": True, "status": "draft",
            },
            # M2：窗口聚合（5 分钟内 3 次温度超标，滞回）。
            {
                "id": aggregate_sentinel_id,
                "name": "cep_temp_burst",
                "displayName": "温度超标窗口聚合",
                "bindings": stage_bindings("a"),
                "links": [],
                "condition": None,
                "pattern": {
                    "stages": [
                        {"alias": "a", "objectTypeId": device_type_id,
                         "filter": "a.temp > 80"},
                    ],
                    "aggregate": {
                        "property": "temp", "function": "count",
                        "window": 300, "threshold": 3, "comparison": "gte",
                    },
                },
                "conditionRows": [], "conditionLogic": "and",
                "primaryAlias": "a",
                "actionIds": [], "actionParameters": {},
                "onChange": True, "onSchedule": True,
                "scanIntervalSeconds": 60,
                "triggerMode": "on_pattern",
                "muted": False, "enabled": True, "status": "draft",
            },
        ]
        mapping_saved = api.request(
            "PUT",
            (
                f"/api/v2/ontologies/{ontology_id}/versions/"
                f"{draft['id']}/workspace/mappings"
            ),
            json={
                "baseRevision": saved["revision"],
                "mappings": [],
                "linkMappings": [],
                "sentinels": sentinels_payload,
            },
        )
        require(bool(mapping_saved.get("snapshotHash")), "sentinel save failed")

        trial = api.request(
            "POST",
            (
                f"/api/v2/ontologies/{ontology_id}/versions/"
                f"{draft['id']}/trial-runs"
            ),
            json={},
        )
        require(trial["status"] == "passed", f"trial failed: {trial}")
        impact = api.request(
            "GET",
            f"/api/v2/ontologies/{ontology_id}/versions/{draft['id']}/impact",
        )
        readiness = impact.get("releaseReadiness") or {}
        require(
            readiness.get("ready") is True,
            f"release readiness failed (pattern 校验未通过?): {readiness}")
        release = api.request(
            "POST",
            (
                f"/api/v2/ontologies/{ontology_id}/versions/"
                f"{draft['id']}/promote"
            ),
            json={
                "trialRunId": trial["id"],
                "impactHash": impact["impactHash"],
                "versionLabel": "CEP 哨兵发布",
            },
        )
        require(
            release.get("node_kind") == "release",
            f"promotion failed: {release}")
        wait_for_cdc(api, ontology_id)

        device1 = f"cep-dev-1-{suffix}"
        device2 = f"cep-dev-2-{suffix}"
        results: dict[str, Any] = {"ontologyId": ontology_id}

        # ---- 步骤 1（M1 时间算子）：新温度 85 且刚变更 → 触发一次 -------
        update_instance(
            api, ontology_id, device1,
            {"device_no": "CEP-001", "status": "submitted", "temp": 85})
        wait_for_cdc(api, ontology_id)
        temporal_fired = wait_for(
            lambda: [
                row for row in firings_of(
                    api, ontology_id, temporal_sentinel_id)
                if row.get("status") == "fired"
            ],
            timeout_seconds=60,
            description="temporal sentinel firing")
        require(
            len(temporal_fired) == 1 and temporal_fired[0]["matchCount"] == 1,
            f"temporal firing shape unexpected: {temporal_fired}")

        # ---- 步骤 2（M2 序列）：submitted → paid 完成即触发 --------------
        update_instance(
            api, ontology_id, device1,
            {"device_no": "CEP-001", "status": "paid", "temp": 85})
        wait_for_cdc(api, ontology_id)
        seq_fired = wait_for(
            lambda: [
                row for row in firings_of(
                    api, ontology_id, sequence_sentinel_id)
                if row.get("status") == "fired"
            ],
            timeout_seconds=90,
            description="sequence sentinel firing")
        require(
            len(seq_fired) == 1,
            f"sequence should fire exactly once: {seq_fired}")
        require(
            all(
                key.startswith("pattern:") and key.count(":") >= 2
                for row in seq_fired for key in row.get("entered") or []),
            f"sequence firing keys malformed: {seq_fired}")
        # 动作标记已写入（update_property 把 status 改为 cep_seen）。
        instances = api.request(
            "GET",
            f"/api/v2/formal/ontologies/{ontology_id}/instances")
        device1_row = next(
            row for row in (instances if isinstance(instances, list)
                            else instances.get("items") or [])
            if row.get("id") == device1)
        require(
            (device1_row.get("properties") or {}).get("status") == "cep_seen",
            f"pattern action did not write back: {device1_row}")

        # ---- 步骤 3（M2 水位幂等）：手动重跑不再放炮 ----------------------
        api.request(
            "POST",
            f"/api/v1/ontologies/{ontology_id}/sentinels/run",
        )
        wait_for_cdc(api, ontology_id)
        seq_all = firings_of(api, ontology_id, sequence_sentinel_id)
        fired_rows = [row for row in seq_all if row.get("status") == "fired"]
        require(
            len(fired_rows) == 1,
            f"watermark idempotency broken, refired: {fired_rows}")

        # ---- 步骤 4（M2 缺失分支）：只有起点无终点 → absence 超时触发 ----
        update_instance(
            api, ontology_id, device2,
            {"device_no": "CEP-002", "status": "submitted", "temp": 40})
        wait_for_cdc(api, ontology_id)
        absence_fired = wait_for(
            lambda: [
                row for row in firings_of(
                    api, ontology_id, sequence_sentinel_id)
                if row.get("status") == "fired"
                # 定时扫描触发的 trigger_source 是 "sch:<hex>" 控制源。
                and str(row.get("triggerSource") or "").startswith("sch:")
                and any(
                    key.startswith(f"pattern:{device2}:")
                    for key in row.get("entered") or [])
            ],
            timeout_seconds=360,
            description="absence-branch firing (within=180s + scan=60s)")
        require(len(absence_fired) == 1, f"absence fired twice: {absence_fired}")

        # ---- 步骤 5（M2 聚合滞回）：3 次超标触发一次，第 4 次不放炮 -------
        for temp in (82, 88, 91):
            update_instance(
                api, ontology_id, device2,
                {"device_no": "CEP-002", "status": "submitted",
                 "temp": temp})
            wait_for_cdc(api, ontology_id)
        agg_fired = wait_for(
            lambda: [
                row for row in firings_of(
                    api, ontology_id, aggregate_sentinel_id)
                if row.get("status") == "fired"
            ],
            timeout_seconds=120,
            description="aggregate sentinel firing")
        require(len(agg_fired) == 1, f"aggregate fired: {agg_fired}")
        update_instance(
            api, ontology_id, device2,
            {"device_no": "CEP-002", "status": "submitted", "temp": 95})
        wait_for_cdc(api, ontology_id)
        time.sleep(5)
        agg_all = firings_of(api, ontology_id, aggregate_sentinel_id)
        agg_fired_rows = [
            row for row in agg_all if row.get("status") == "fired"]
        require(
            len(agg_fired_rows) == 1,
            f"aggregate hysteresis broken, refired: {agg_fired_rows}")

        wait_for_cdc(api, ontology_id)
        results["temporalFirings"] = 1
        results["sequenceFirings"] = len(fired_rows)
        results["absenceFirings"] = len(absence_fired)
        results["aggregateFirings"] = len(agg_fired_rows)
        results["sequenceActionWriteBack"] = "cep_seen"
        return results
    finally:
        api.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="admin123")
    return parser.parse_args()


def main() -> int:
    try:
        result = run(parse_args())
    except (CheckFailed, httpx.HTTPError, OSError, ValueError) as exc:
        print(f"SENTINEL CEP E2E FAILED: {exc}", file=sys.stderr)
        return 1
    print("SENTINEL CEP E2E PASSED")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
