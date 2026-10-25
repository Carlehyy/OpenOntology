#!/usr/bin/env python3
"""Real HTTP integrated-source smoke checks; never reports full-chain acceptance.

Prerequisites: isolated running API, genuine fixed-revision dbx, registered
47-column scale_rows assets, and deterministic source oracles. Credentials are
read from dedicated environment variables, never from dotenv or CLI arguments.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

DBX_COMMIT = "0137e0cbc8b663da5a17ec424c6d47e4e5570ead"
DBX_VERSION = "0.6.53"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def unwrap(value):
    return value["data"] if isinstance(value, dict) and "data" in value else value


class Acceptance:
    def __init__(self, url: str, username: str, password: str, rows: int):
        parsed = urlsplit(url)
        require(parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port == 48004,
                "only the approved isolated http://127.0.0.1:48004 API is allowed")
        self.client = httpx.Client(base_url=url, timeout=600, trust_env=False)
        self.rows = rows
        self.evidence: list[dict] = []
        try:
            login = self.request("POST", "/api/v1/auth/login", json={"username": username, "password": password})
            self.client.headers["Authorization"] = f"Bearer {login['access_token']}"
        except Exception:
            self.client.close()
            raise

    def request(self, method: str, path: str, *, status: int = 200, **kwargs):
        response = self.client.request(method, path, **kwargs)
        # Do not print response bodies/headers: they can contain secrets/config.
        require(response.status_code == status, f"{method} {path} expected {status}, got {response.status_code}")
        return unwrap(response.json())

    def query(self, query: str, bindings: dict, params: dict | None = None, page_size: int = 100):
        result = self.request("POST", "/api/v2/query", json={"language": "spl", "query": query,
                              "bindings": bindings, "params": params or {}, "pageSize": page_size})
        require(result["exactness"] == "exact_within_scope", "query was not exact within authorized source snapshots")
        require(result["countsScope"] == "authorized_query_scope_at_source_snapshots", "missing honest count scope")
        require(not result["resultLimited"], "unrequested semantic truncation")
        require(bool(result["sourceStates"]) and all(state.get("completeness") == "complete" for state in result["sourceStates"]),
                "source completeness evidence was missing or incomplete")
        return result

    def asset(self, asset_id: str):
        asset = self.request("GET", f"/api/v2/integrated-datasets/{asset_id}")
        require(len(asset["columns"]) == 47, "fixture must have all 47 columns")
        require(asset["primary_key"] == ["id"], "fixture must have a stable source PK")
        require(asset["backends"] == {"snapshot": "native", "mutation": "native", "metadata": "dbx"}, "unexpected source adapter lanes")
        status = asset["dbx"]
        require(status.get("configured") is True and status.get("available") is True,
                "genuine dbx is not configured and available")
        # Exact provenance key is owned by source adapter; inspect both exposed
        # revision spellings without accepting an omitted revision.
        require(status.get("commit", status.get("revision")) == DBX_COMMIT, "dbx pinned revision is not proven")
        require(status.get("version") == DBX_VERSION, "dbx runtime version is not the pinned release")
        return asset

    def check_single(self, source: str, asset_id: str):
        asset = self.asset(asset_id)
        result = self.query("from source as s | stats count() as n, count_distinct(s.id) as pk, sum(s.amount_cents) as total",
                            {"source": {"kind": "integrated", "assetId": asset_id}})
        cycles, rem = divmod(self.rows, 100000)
        expected_total = cycles*99999*100000//2 + rem*(rem+1)//2
        require(result["totalRows"] == 1 and len(result["rows"]) == 1, "aggregate must return exactly one row")
        row = result["rows"][0]
        require((int(row["n"]), int(row["pk"]), int(row["total"])) == (self.rows, self.rows, expected_total), "source aggregate disagrees with deterministic oracle")
        late = self.query("from source as s | where s.id = :last | project s.id, s.join_key, s.c40",
                          {"source": {"kind": "integrated", "assetId": asset_id}}, {"last": self.rows})
        require(late["totalRows"] == 1, "last source row was truncated")
        tail = late["rows"][0]
        require(int(tail["id"]) == self.rows and int(tail["join_key"]) == 9007199254740992+self.rows, "bigint precision or tail-row loss")
        require(tail["c40"] == f"v40-{self.rows % 1000000:06d}", "47th source field mismatch")
        self.evidence.append({"stage": "source_query", "source": source, "asset_id": asset_id,
                              "rows": self.rows, "columns": 47, "aggregate_query_id": result["queryId"],
                              "tail_query_id": late["queryId"], "passed": True})
        return asset

    def check_join(self, mysql_id: str, pg_id: str):
        result = self.query("from orders as o | join customers as c on o.join_key = c.join_key type=left | stats count() as n, count_distinct(o.id) as pk",
                            {"orders": {"kind": "integrated", "assetId": mysql_id},
                             "customers": {"kind": "integrated", "assetId": pg_id}})
        require(result["consistency"] == "cross_source_non_atomic", "federated source snapshots were misrepresented as atomic")
        require(result["totalRows"] == 1 and len(result["rows"]) == 1, "join aggregate row count mismatch")
        require(int(result["rows"][0]["n"]) == self.rows and int(result["rows"][0]["pk"]) == self.rows, "cross-source join count mismatch")
        self.evidence.append({"stage": "cross_source_join", "query_id": result["queryId"], "rows": self.rows,
                              "consistency": result["consistency"], "passed": True})

    def sync(self, source: str, asset_id: str):
        version = self.request("POST", f"/api/v2/integrated-datasets/{asset_id}/sync", status=202,
                               params={"batch_size": 1000})
        self.evidence.append({"stage": "sync_requested", "source": source,
                              "run_id": version["run_id"], "status": version["status"]})
        deadline = time.monotonic() + 15 * 60
        while version["status"] in {"queued", "starting", "loading"}:
            require(time.monotonic() < deadline, "background sync did not finish within 15 minutes")
            time.sleep(1)
            version = self.request("GET", f"/api/v2/integrated-datasets/{asset_id}/sync-runs/{version['run_id']}")
        require(version["status"] == "sealed" and version["complete"] is True and bool(version["version_id"]),
                "background sync failed or did not publish a complete sealed version")
        require(version["row_count"] == self.rows and version["expected_rows"] == self.rows, "sealed version lost rows")
        require(bool(version.get("checksum")), "sealed version lacks checksum evidence")
        schema = self.request("GET", f"/api/v2/integrated-datasets/{asset_id}/schema")
        require(bool(schema.get("schema_fingerprint")) and len(schema["columns"]) == 47, "source schema integrity was not proven")
        result = self.query("from version as v | stats count() as n, count_distinct(v.id) as pk",
                            {"version": {"kind": "dataset", "datasetId": version["dataset_id"], "versionId": version["version_id"]}})
        require(result["consistency"] == "immutable_versions", "version query was not pinned immutable")
        require(int(result["rows"][0]["n"]) == self.rows and int(result["rows"][0]["pk"]) == self.rows, "version query disagrees with sealed row count")
        self.evidence.append({"stage": "complete_version", "source": source, "passed": True, **version})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:48004")
    parser.add_argument("--rows", type=int, required=True)
    parser.add_argument("--mysql-asset", required=True)
    parser.add_argument("--postgres-asset", required=True)
    parser.add_argument("--sync", action="store_true")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    artifacts = root / ".artifacts"
    report_path = args.report.resolve()
    require(report_path.is_relative_to(artifacts.resolve()), "report must be below this runtime's .artifacts directory")
    require(1 <= args.rows <= 10_000_000, "rows must be 1..10000000")
    username = os.environ.get("INTEGRATED_E2E_USERNAME")
    password = os.environ.get("INTEGRATED_E2E_PASSWORD")
    require(bool(username and password), "set dedicated INTEGRATED_E2E_USERNAME/PASSWORD without dotenv")
    started = time.monotonic()
    acceptance = None
    report = {"rows_per_source": args.rows, "columns": 47, "full_e2e_pass": False,
              "is_final_scale": args.rows in (2_300_000, 10_000_000), "evidence": [],
              "not_verified": ["browser CRUD", "mapping/trial/promote", "Neo4j", "sentinel", "genuine LLM assistant"]}
    code = 1
    try:
        acceptance = Acceptance(args.api_url, username, password, args.rows)
        acceptance.check_single("mysql", args.mysql_asset)
        acceptance.check_single("postgres", args.postgres_asset)
        acceptance.check_join(args.mysql_asset, args.postgres_asset)
        if args.sync:
            acceptance.sync("mysql", args.mysql_asset)
            acceptance.sync("postgres", args.postgres_asset)
        if args.sync:
            report["status"] = "http_source_and_version_checks_passed_not_full_e2e"
        else:
            report["not_verified"].append("complete version sync")
            report["status"] = "http_source_checks_passed_not_full_e2e"
        code = 0
    except (RuntimeError, KeyError, ValueError, httpx.HTTPError) as error:
        report["status"] = "failed"
        report["error_class"] = type(error).__name__
        print(f"integrated-e2e failed: {error}", file=sys.stderr)
    finally:
        if acceptance:
            report["evidence"] = acceptance.evidence
            acceptance.client.close()
        report["wall_seconds"] = round(time.monotonic()-started, 3)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
