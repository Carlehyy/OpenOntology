#!/usr/bin/env python3
"""Verify the legacy runtime-secret split against real PostgreSQL storage.

This is a synthetic-only CI/staging check.  It requires an explicit execution
sentinel and refuses non-loopback databases, non-test environments, and database
names outside the ``*_ci``/``*_e2e`` convention.  PostgreSQL fixtures live in
one transaction that is always rolled back; API Hub SQLite and the deployment
app directory live in a temporary directory.  The JSON report contains only
fixed fields, counts, and booleans.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
# Direct ``python scripts/...`` execution makes ``backend/scripts`` sys.path[0].
# Add the backend package root explicitly before the late application imports.
sys.path.insert(0, str(BACKEND_DIR))
POSTGRES_LOCATION_COUNT = 9
SQLITE_LOCATION_COUNT = 0
TOTAL_LOCATION_COUNT = POSTGRES_LOCATION_COUNT + SQLITE_LOCATION_COUNT
SYNTHETIC_MANIFEST_CREDENTIALS = {
    "synthetic-postgres-password",
    "synthetic-redis-password",
    "synthetic-neo4j-password",
    "synthetic-minio-access",
    "synthetic-minio-secret",
    "synthetic-n8n-api-key",
}
REPORT_FIELDS = {
    "schema_version",
    "result",
    "stage",
    "alembic_head",
    "postgres_locations",
    "sqlite_locations",
    "ciphertext_unchanged",
    "decrypt_roundtrips",
    "deploy_secret_split",
    "synthetic_only",
}
REPORT_STAGES = {
    "starting",
    "database_guard",
    "fixture_insert",
    "deploy_secret_split",
    "postgres_verify",
    "complete",
    "self_test",
}
HEAD_PATTERN = re.compile(r"^[0-9]{4}_[a-z0-9_]+$")


def _report_template() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "result": "fail",
        "stage": "starting",
        "alembic_head": "unknown",
        "postgres_locations": POSTGRES_LOCATION_COUNT,
        "sqlite_locations": SQLITE_LOCATION_COUNT,
        "ciphertext_unchanged": 0,
        "decrypt_roundtrips": 0,
        "deploy_secret_split": False,
        "synthetic_only": True,
    }


def _assert_sanitized_report(
    report: dict[str, Any], sensitive_values: set[str]
) -> bytes:
    if set(report) != REPORT_FIELDS:
        raise ValueError("report schema mismatch")
    if report["schema_version"] != 1:
        raise ValueError("report schema version mismatch")
    if report["result"] not in {"pass", "fail"}:
        raise ValueError("invalid report result")
    if report["stage"] not in REPORT_STAGES:
        raise ValueError("invalid report stage")
    head = report["alembic_head"]
    if head != "unknown" and not HEAD_PATTERN.fullmatch(head):
        raise ValueError("unsafe Alembic revision in report")
    for field in (
        "postgres_locations",
        "sqlite_locations",
        "ciphertext_unchanged",
        "decrypt_roundtrips",
    ):
        value = report[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"invalid report count: {field}")
    for field in (
        "deploy_secret_split",
        "synthetic_only",
    ):
        if not isinstance(report[field], bool):
            raise ValueError(f"invalid report boolean: {field}")
    if report["synthetic_only"] is not True:
        raise ValueError("report must be synthetic-only")
    payload = (json.dumps(report, sort_keys=True, indent=2) + "\n").encode()
    for value in sensitive_values:
        if value and value.encode() in payload:
            raise ValueError("sensitive value reached report payload")
    return payload


def _write_report(
    path: Path, report: dict[str, Any], sensitive_values: set[str]
) -> None:
    payload = _assert_sanitized_report(report, sensitive_values)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        path.chmod(0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if tmp.exists():
            tmp.unlink()
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ValueError("report mode must be 0600")


def _self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="runtime-secret-report-self-test-") as raw:
        report_path = Path(raw) / "report.json"
        report = _report_template()
        report.update({"result": "pass", "stage": "self_test"})
        sentinel = "0000_synthetic_sensitive_self_test_sentinel"
        _write_report(report_path, report, {sentinel})
        assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
        assert sentinel not in report_path.read_text(encoding="utf-8")
        unsafe = dict(report)
        unsafe["alembic_head"] = sentinel
        try:
            _assert_sanitized_report(unsafe, {sentinel})
        except ValueError:
            pass
        else:
            raise AssertionError("report sanitizer accepted a sensitive value")
    guarded_keys = (
        "OPENONTOLOGY_RUNTIME_SECRET_E2E",
        "ENVIRONMENT",
        "DATABASE_URL",
    )
    original_environment = {key: os.environ.get(key) for key in guarded_keys}
    try:
        os.environ.update(
            {
                "OPENONTOLOGY_RUNTIME_SECRET_E2E": "1",
                "ENVIRONMENT": "test",
                "DATABASE_URL": (
                    "postgresql://synthetic:synthetic@127.0.0.1/synthetic_e2e"
                ),
            }
        )
        assert _guard_database_url().endswith("/synthetic_e2e")
        redirected_urls = (
            "postgresql://synthetic:synthetic@127.0.0.1/synthetic_e2e"
            "?host=production.invalid",
            "postgresql://synthetic:synthetic@127.0.0.1/synthetic_e2e"
            "?dbname=production",
            "postgresql://synthetic:synthetic@127.0.0.1/synthetic_e2e#unsafe",
        )
        for database_url in redirected_urls:
            os.environ["DATABASE_URL"] = database_url
            try:
                _guard_database_url()
            except ValueError:
                pass
            else:
                raise AssertionError("database guard accepted URL redirection")
    finally:
        for key, value in original_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print("runtime-secret report self-test: pass")


def _guard_database_url() -> str:
    if os.environ.get("OPENONTOLOGY_RUNTIME_SECRET_E2E") != "1":
        raise ValueError("OPENONTOLOGY_RUNTIME_SECRET_E2E must be 1")
    if os.environ.get("ENVIRONMENT") != "test":
        raise ValueError("ENVIRONMENT must be test")
    database_url = os.environ.get("DATABASE_URL", "")
    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("DATABASE_URL must use PostgreSQL")
    if parsed.query or parsed.fragment:
        raise ValueError("DATABASE_URL must not contain query or fragment overrides")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("DATABASE_URL must use a loopback host")
    database_name = parsed.path.lstrip("/")
    if not database_name or not database_name.endswith(("_ci", "_e2e")):
        raise ValueError("database name must end in _ci or _e2e")
    if parsed.username is None or parsed.password is None:
        raise ValueError("DATABASE_URL must contain synthetic test credentials")
    return database_url


def _env_value(path: Path, key: str) -> str:
    matches = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            matches.append(line.split("=", 1)[1])
    if len(matches) != 1:
        raise ValueError(f"expected one canonical {key} assignment")
    return matches[0]


def _legacy_fixture_values() -> tuple[dict[str, str], dict[str, str], str]:
    legacy_secret = _env_value(REPO_ROOT / ".env.example", "SECRET_KEY")
    if legacy_secret not in {
        "dev-secret-key",
        "change-me-to-a-random-32-char-string",
    }:
        raise ValueError(".env.example no longer contains a recognized legacy secret")
    legacy_key = base64.urlsafe_b64encode(
        hashlib.sha256(legacy_secret.encode()).digest()
    ).decode()
    plaintexts = {
        "model_configs.api_key_encrypted": "synthetic-model-api-key",
        "v2_connections.config._encrypted": json.dumps(
            {"host": "synthetic-db", "password": "synthetic-connection-password"},
            separators=(",", ":"),
        ),
        "workflow_config.api_key_encrypted": "synthetic-n8n-api-key",
        "super_assistant_mcp_servers.headers_encrypted": json.dumps(
            {"Authorization": "Bearer synthetic-mcp-token"}, separators=(",", ":")
        ),
        "super_assistant_mcp_servers.env_encrypted": json.dumps(
            {"MCP_SECRET": "synthetic-mcp-secret"}, separators=(",", ":")
        ),
        "v2_steward_browser_sources.endpoint_url_encrypted": (
            "http://synthetic-browser.invalid:9222"
        ),
        "v2_steward_browser_sources.headers_encrypted": json.dumps(
            {"X-Synthetic-CDP": "synthetic-browser-secret"}, separators=(",", ":")
        ),
        "v2_manual_dataset_shares.token_encrypted": "synthetic-manual-share-token",
        "v2_pipeline_file_assets.share_token_encrypted": (
            "synthetic-pipeline-share-token"
        ),
    }
    if len(plaintexts) != TOTAL_LOCATION_COUNT:
        raise AssertionError("persistent ciphertext inventory changed")
    fernet = Fernet(legacy_key.encode())
    ciphertexts = {
        location: fernet.encrypt(value.encode()).decode()
        for location, value in plaintexts.items()
    }
    return plaintexts, ciphertexts, legacy_key


def _insert_postgres_fixtures(conn, ciphertexts: dict[str, str]) -> None:
    now = datetime.now(timezone.utc)
    conn.execute(text("SET LOCAL session_replication_role = replica"))
    statements = [
        ("""INSERT INTO model_configs
          (id,name,config_type,api_key_encrypted,provider,models,options,enabled,is_default,created_by,created_at,updated_at)
          VALUES ('cipher-model','Cipher E2E','llm',:cipher,'compatible',CAST('[]' AS json),CAST('{}' AS json),true,false,'synthetic-user',:now,:now)""",
         {"cipher": ciphertexts["model_configs.api_key_encrypted"], "now": now}),
        ("""INSERT INTO v2_connections
          (id,name,kind,config,status,created_at,updated_at)
          VALUES ('cipher-connection','Cipher E2E','postgres',json_build_object('_encrypted',CAST(:cipher AS text)),'active',:now,:now)""",
         {"cipher": ciphertexts["v2_connections.config._encrypted"], "now": now}),
        ("""INSERT INTO workflow_config
          (id,enabled,api_url,api_key_encrypted,timeout_seconds,created_at,updated_at)
          VALUES ('cipher-workflow',true,'https://synthetic-n8n.invalid',:cipher,30,:now,:now)""",
         {"cipher": ciphertexts["workflow_config.api_key_encrypted"], "now": now}),
        ("""INSERT INTO super_assistant_mcp_servers
          (id,owner_id,name,transport,url,headers_encrypted,header_names,args,env_encrypted,env_names,enabled,require_confirmation,tool_manifest,created_at,updated_at)
          VALUES ('cipher-mcp','synthetic-user','Cipher E2E','streamable_http','https://synthetic-mcp.invalid',:headers,CAST('[\"Authorization\"]' AS json),CAST('[]' AS json),:env,CAST('[\"MCP_SECRET\"]' AS json),true,true,CAST('[]' AS json),:now,:now)""",
         {"headers": ciphertexts["super_assistant_mcp_servers.headers_encrypted"], "env": ciphertexts["super_assistant_mcp_servers.env_encrypted"], "now": now}),
        ("""INSERT INTO v2_steward_browser_sources
          (id,name,source_type,endpoint_url_encrypted,headers_encrypted,device_token_hash,enabled,created_at,updated_at)
          VALUES ('cipher-browser','Cipher E2E','remote_cdp',:endpoint,:headers,'',true,:now,:now)""",
         {"endpoint": ciphertexts["v2_steward_browser_sources.endpoint_url_encrypted"], "headers": ciphertexts["v2_steward_browser_sources.headers_encrypted"], "now": now}),
        ("""INSERT INTO v2_manual_dataset_shares
          (id,dataset_id,token_hash,token_encrypted,permission,label,created_at)
          VALUES ('cipher-manual-share','synthetic-dataset',:token_hash,:cipher,'view','Cipher E2E',:now)""",
         {"token_hash": hashlib.sha256(b"synthetic-manual-share-token").hexdigest(), "cipher": ciphertexts["v2_manual_dataset_shares.token_encrypted"], "now": now}),
        ("""INSERT INTO v2_pipeline_file_assets
          (id,workflow_id,invocation_id,purpose,status,idempotency_key,original_name,object_key,size,content_type,sha256,share_token_hash,share_token_encrypted,created_at)
          VALUES ('cipher-file','synthetic-workflow','synthetic-invocation','run','ready','cipher-e2e','synthetic.txt','synthetic/object',1,'text/plain',:sha,:token_hash,:cipher,:now)""",
         {"sha": hashlib.sha256(b"x").hexdigest(), "token_hash": hashlib.sha256(b"synthetic-pipeline-share-token").hexdigest(), "cipher": ciphertexts["v2_pipeline_file_assets.share_token_encrypted"], "now": now}),
    ]
    for statement, parameters in statements:
        conn.execute(text(statement), parameters)


def _postgres_queries() -> dict[str, str]:
    return {
        "model_configs.api_key_encrypted": "SELECT api_key_encrypted FROM model_configs WHERE id='cipher-model'",
        "v2_connections.config._encrypted": "SELECT config->>'_encrypted' FROM v2_connections WHERE id='cipher-connection'",
        "workflow_config.api_key_encrypted": "SELECT api_key_encrypted FROM workflow_config WHERE id='cipher-workflow'",
        "super_assistant_mcp_servers.headers_encrypted": "SELECT headers_encrypted FROM super_assistant_mcp_servers WHERE id='cipher-mcp'",
        "super_assistant_mcp_servers.env_encrypted": "SELECT env_encrypted FROM super_assistant_mcp_servers WHERE id='cipher-mcp'",
        "v2_steward_browser_sources.endpoint_url_encrypted": "SELECT endpoint_url_encrypted FROM v2_steward_browser_sources WHERE id='cipher-browser'",
        "v2_steward_browser_sources.headers_encrypted": "SELECT headers_encrypted FROM v2_steward_browser_sources WHERE id='cipher-browser'",
        "v2_manual_dataset_shares.token_encrypted": "SELECT token_encrypted FROM v2_manual_dataset_shares WHERE id='cipher-manual-share'",
        "v2_pipeline_file_assets.share_token_encrypted": "SELECT share_token_encrypted FROM v2_pipeline_file_assets WHERE id='cipher-file'",
    }


def _write_synthetic_manifest(path: Path) -> None:
    path.write_text(
        "ENVIRONMENT=production\nPUBLIC_PORT=80\n"
        "POSTGRES_DB=synthetic\nPOSTGRES_USER=synthetic\n"
        "POSTGRES_PASSWORD=synthetic-postgres-password\n"
        "DATABASE_URL=postgresql://synthetic:synthetic-postgres-password@pg.synthetic.invalid:5432/synthetic\n"
        "REDIS_URL=redis://:synthetic-redis-password@redis.synthetic.invalid:6379/0\n"
        "NEO4J_URI=bolt+s://graph.synthetic.invalid:7687\nNEO4J_USER=neo4j\n"
        "NEO4J_PASSWORD=synthetic-neo4j-password\n"
        "NEO4J_AUTH=neo4j/synthetic-neo4j-password\n"
        "MINIO_ENDPOINT=objects.synthetic.invalid:9000\n"
        "MINIO_ACCESS_KEY=synthetic-minio-access\n"
        "MINIO_SECRET_KEY=synthetic-minio-secret\nMINIO_USE_SSL=true\n"
        "N8N_API_URL=https://n8n.synthetic.invalid/api/v1\n"
        "N8N_API_KEY=synthetic-n8n-api-key\nN8N_TIMEOUT_SECONDS=30\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def _read_runtime_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value
    return values


def _run(report_path: Path) -> None:
    report = _report_template()
    sensitive_values: set[str] = set()
    stage = "starting"
    engine = None
    connection = None
    transaction = None
    try:
        _write_report(report_path, report, sensitive_values)
        stage = "database_guard"
        report["stage"] = stage
        database_url = _guard_database_url()
        sensitive_values.add(database_url)
        plaintexts, ciphertexts, legacy_key = _legacy_fixture_values()
        sensitive_values.update(plaintexts.values())
        sensitive_values.update(ciphertexts.values())
        sensitive_values.add(legacy_key)
        sensitive_values.update(SYNTHETIC_MANIFEST_CREDENTIALS)

        engine = create_engine(database_url)
        connection = engine.connect()
        transaction = connection.begin()
        head = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        if not HEAD_PATTERN.fullmatch(head):
            raise ValueError("database is not at a recognized Alembic head")
        report["alembic_head"] = head

        stage = "fixture_insert"
        report["stage"] = stage
        _insert_postgres_fixtures(connection, ciphertexts)

        with tempfile.TemporaryDirectory(
            prefix="openontology-runtime-secret-e2e-"
        ) as raw_temp:
            temp_root = Path(raw_temp)
            app_dir = temp_root / "app"
            app_dir.mkdir()
            shutil.copy2(REPO_ROOT / ".env.example", app_dir / ".env.example")
            shutil.copy2(REPO_ROOT / ".env.example", app_dir / ".env")
            manifest_path = app_dir / "synthetic.dependencies.env"
            _write_synthetic_manifest(manifest_path)

            api_hub_dir = temp_root / "api-hub-data"
            api_hub_dir.mkdir()

            stage = "deploy_secret_split"
            report["stage"] = stage
            deploy_env = os.environ.copy()
            deploy_env.update(
                {
                    "APP_DIR": str(app_dir),
                    "SKIP_GIT": "1",
                    "DEPLOY_VALIDATE_ONLY": "1",
                    "BOOTSTRAP_PRODUCTION_ENV": "0",
                    "DEPENDENCY_CONFIG_FILE": str(manifest_path),
                }
            )
            result = subprocess.run(
                ["bash", str(REPO_ROOT / "deploy" / "deploy-prod.sh")],
                cwd=REPO_ROOT,
                env=deploy_env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError("deployment validation failed")
            generated = _read_runtime_env(app_dir / ".env")
            for key, value in generated.items():
                if (
                    any(
                        marker in key
                        for marker in ("PASSWORD", "SECRET", "KEY", "TOKEN")
                    )
                    or key in {"DATABASE_URL", "REDIS_URL", "NEO4J_AUTH"}
                ):
                    sensitive_values.add(value)
            if generated.get("ENCRYPTION_KEY") != legacy_key:
                raise AssertionError("legacy encryption key was not pinned")
            if len(generated.get("SECRET_KEY", "")) != 64:
                raise AssertionError("JWT signing secret was not rotated")
            combined_output = result.stdout + result.stderr
            if any(value and value in combined_output for value in sensitive_values):
                raise AssertionError("deployment output disclosed a sensitive value")
            report["deploy_secret_split"] = True

            os.environ["SECRET_KEY"] = generated["SECRET_KEY"]
            os.environ["ENCRYPTION_KEY"] = generated["ENCRYPTION_KEY"]
            os.environ["API_HUB_DATA_DIR"] = str(api_hub_dir)
            from app.shared.encryption import decrypt

            stage = "postgres_verify"
            report["stage"] = stage
            queries = _postgres_queries()
            if len(queries) != POSTGRES_LOCATION_COUNT:
                raise AssertionError("PostgreSQL ciphertext inventory changed")
            unchanged = 0
            round_trips = 0
            for location, query in queries.items():
                stored = connection.execute(text(query)).scalar_one()
                if stored != ciphertexts[location]:
                    raise AssertionError("PostgreSQL ciphertext changed")
                unchanged += 1
                if decrypt(stored) != plaintexts[location]:
                    raise AssertionError("PostgreSQL decrypt failed")
                round_trips += 1

            if unchanged != TOTAL_LOCATION_COUNT or round_trips != TOTAL_LOCATION_COUNT:
                raise AssertionError("persistent ciphertext coverage is incomplete")
            report["ciphertext_unchanged"] = unchanged
            report["decrypt_roundtrips"] = round_trips

        if transaction is not None:
            transaction.rollback()
            transaction = None
        stage = "complete"
        report.update({"result": "pass", "stage": stage})
        _write_report(report_path, report, sensitive_values)
        print(
            "runtime-secret split E2E: pass "
            f"({TOTAL_LOCATION_COUNT}/{TOTAL_LOCATION_COUNT} ciphertexts preserved)"
        )
    except Exception:
        if transaction is not None:
            transaction.rollback()
            transaction = None
        report.update({"result": "fail", "stage": stage})
        _write_report(report_path, report, sensitive_values)
        raise SystemExit(
            f"runtime-secret split E2E failed at fixed stage: {stage}"
        ) from None
    finally:
        if transaction is not None:
            transaction.rollback()
        if connection is not None:
            connection.close()
        if engine is not None:
            engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        type=Path,
        help="write a strictly sanitized 0600 JSON report",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="run offline report-schema and redaction checks",
    )
    args = parser.parse_args()
    if args.self_test:
        _self_test()
        return
    if args.report is None:
        parser.error("--report is required unless --self-test is used")
    _run(args.report.resolve())


if __name__ == "__main__":
    main()
