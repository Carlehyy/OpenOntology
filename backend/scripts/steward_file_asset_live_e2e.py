#!/usr/bin/env python3
"""Live n8n -> platform file gateway -> MinIO -> curated lake acceptance test.

The script is deliberately intended to run *inside the production backend
container*.  It reads the existing ``WorkflowConfig`` through the normal
service, so no n8n URL, API key, password, gateway bearer token, share token, or
object-store credential is accepted on the command line or embedded here.

The only required input is a temporary public origin that remote n8n can reach.
All remote and local fixtures are unique and are removed in ``finally``.  The
normal JSON report contains booleans only; failure diagnostics are bounded and
redacted, and capability URLs or tokens are never printed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


_OUTPUT_NODE = "输出业务行"
_FORBIDDEN_LAKE_KEYS = {
    "api_key",
    "authorization",
    "file_gateway",
    "gateway_token",
    "object_key",
    "storage_uri",
    "token",
}
_SHARE_PATH_RE = re.compile(
    r"/api/public/file-assets/[^/\s?#]+/download", re.IGNORECASE
)
_JWT_RE = re.compile(
    r"\b[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"
)
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[^\s\"'<>]+")


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a cleaned live attachment acceptance test using the production "
            "database WorkflowConfig and dependency settings."
        )
    )
    parser.add_argument(
        "--public-root",
        required=True,
        help="Temporary credential-free HTTP(S) origin reachable by remote n8n.",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=120,
        help="Maximum wait for the correlated n8n execution (10-600).",
    )
    values = parser.parse_args(argv)
    if not 10 <= values.wait_seconds <= 600:
        parser.error("--wait-seconds must be between 10 and 600")
    values.public_root = _normalize_public_root(values.public_root)
    return values


def _normalize_public_root(value: str) -> str:
    """Return a credential-free HTTP(S) origin, rejecting paths and ambiguity."""
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("--public-root is not a valid HTTP(S) origin") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "--public-root must be a credential-free HTTP(S) origin without "
            "a path, query, or fragment"
        )
    return f"{parsed.scheme}://{parsed.netloc}"


def _workflow_payload(*, name: str, webhook_path: str) -> dict[str, Any]:
    """Build a secret-free workflow whose final node emits JSON plus one FileRef."""
    webhook = {
        "id": str(uuid.uuid4()),
        "name": "Webhook",
        "type": "n8n-nodes-base.webhook",
        "typeVersion": 2,
        "position": [0, 300],
        "parameters": {
            "httpMethod": "POST",
            "path": webhook_path,
            "responseMode": "lastNode",
            "options": {},
        },
        "webhookId": str(uuid.uuid4()),
    }
    generate = {
        "id": str(uuid.uuid4()),
        "name": "生成唯一附件",
        "type": "n8n-nodes-base.code",
        "typeVersion": 2,
        "position": [280, 300],
        "parameters": {
            "mode": "runOnceForAllItems",
            "jsCode": (
                "const runId = String($input.first().json.body.run_id);\n"
                "const content = `OpenOntology FileRef E2E ${runId}\\n`;\n"
                "return [{\n"
                "  json: { record_id: runId, title: '附件链路验收' },\n"
                "  binary: { data: {\n"
                "    data: Buffer.from(content, 'utf8').toString('base64'),\n"
                "    mimeType: 'text/plain',\n"
                "    fileName: 'openontology-file-e2e.txt'\n"
                "  } }\n"
                "}];"
            ),
        },
    }
    upload = {
        "id": str(uuid.uuid4()),
        "name": "上传平台附件",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [560, 300],
        "parameters": {
            "method": "POST",
            "url": (
                '={{ $node["Webhook"].json.body.file_gateway.upload_url }}'
            ),
            "sendHeaders": True,
            "headerParameters": {
                "parameters": [
                    {
                        "name": "Authorization",
                        "value": (
                            "={{ 'Bearer ' + "
                            '$node["Webhook"].json.body.file_gateway.token }}'
                        ),
                    },
                    {"name": "Bypass-Tunnel-Reminder", "value": "true"},
                ]
            },
            "sendBody": True,
            "contentType": "multipart-form-data",
            "bodyParameters": {
                "parameters": [
                    {
                        "parameterType": "formBinaryData",
                        "name": "file",
                        "inputDataFieldName": "data",
                    },
                    {
                        "name": "idempotency_key",
                        "value": (
                            '={{ $node["Webhook"].json.body.run_id '
                            "+ ':attachment' }}"
                        ),
                    },
                ]
            },
            "options": {},
        },
    }
    output = {
        "id": str(uuid.uuid4()),
        "name": _OUTPUT_NODE,
        "type": "n8n-nodes-base.set",
        "typeVersion": 3.4,
        "position": [840, 300],
        "parameters": {
            "mode": "manual",
            "assignments": {
                "assignments": [
                    {
                        "id": "record-id",
                        "name": "record_id",
                        "type": "string",
                        "value": (
                            '={{ $node["生成唯一附件"].json.record_id }}'
                        ),
                    },
                    {
                        "id": "title",
                        "name": "title",
                        "type": "string",
                        "value": '={{ $node["生成唯一附件"].json.title }}',
                    },
                    {
                        "id": "attachment",
                        "name": "attachment",
                        "type": "object",
                        "value": "={{ $json.file_ref }}",
                    },
                ]
            },
            "options": {},
        },
    }
    return {
        "name": name,
        "nodes": [webhook, generate, upload, output],
        "connections": {
            "Webhook": {
                "main": [[
                    {"node": "生成唯一附件", "type": "main", "index": 0}
                ]]
            },
            "生成唯一附件": {
                "main": [[
                    {"node": "上传平台附件", "type": "main", "index": 0}
                ]]
            },
            "上传平台附件": {
                "main": [[
                    {"node": _OUTPUT_NODE, "type": "main", "index": 0}
                ]]
            },
        },
        "settings": {"executionOrder": "v1"},
    }


def _safe_error(exc: BaseException | str, *sensitive_values: str | None) -> str:
    """Return a bounded diagnostic with bearer-like capabilities redacted."""
    text = str(exc)
    for value in sensitive_values:
        if value:
            text = text.replace(str(value), "[REDACTED]")
    text = _BEARER_RE.sub(r"\1[REDACTED]", text)
    text = _JWT_RE.sub("[REDACTED]", text)
    text = _SHARE_PATH_RE.sub("/api/public/file-assets/[REDACTED]/download", text)
    return text[:1000]


def _forbidden_keys(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key).strip().lower()
            if key in _FORBIDDEN_LAKE_KEYS:
                found.add(key)
            found.update(_forbidden_keys(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_forbidden_keys(child))
    return found


def _json_object_cell(value: Any) -> dict[str, Any] | None:
    """Decode one declared JSON lake cell without guessing arbitrary strings.

    Curated Parquet snapshots intentionally persist every cell as text for
    backward-compatible round trips.  The dataset schema still declares the
    attachment column as JSON, so acceptance must decode that one typed cell
    before checking the canonical FileRef contract.
    """
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _install_backend_path() -> Path:
    """Support both an in-repository run and ``docker cp ... /tmp`` execution."""
    candidates = [
        Path(__file__).resolve().parents[1],
        Path.cwd(),
        Path("/app"),
    ]
    for candidate in candidates:
        # A container copy under /tmp has `/` as one parent, and `/app` is a
        # directory there—but `/` is not the Python project root.  Require the
        # nested package marker so the inserted path can actually resolve
        # `app.config`.
        if (candidate / "app" / "__init__.py").is_file():
            value = str(candidate)
            if value not in sys.path:
                sys.path.insert(0, value)
            return candidate
    raise RuntimeError(
        "Cannot locate the backend package; run inside the backend container "
        "or from the backend directory"
    )


def _verify_public_health(public_root: str, *, timeout: float) -> None:
    import httpx

    response = httpx.get(
        f"{public_root}/api/health",
        headers={"Bypass-Tunnel-Reminder": "true"},
        timeout=timeout,
        follow_redirects=False,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"temporary public gateway health returned HTTP {response.status_code}"
        )


def _delete_remote_workflow(client, workflow_id: str) -> bool:
    """Deactivate, delete, and confirm 404 without exposing its webhook path."""
    from app.settings.workflows.n8n_client import N8nApiError

    try:
        current = client.get_workflow(workflow_id)
        if bool(current.get("active")):
            client.deactivate_workflow(workflow_id)
    except N8nApiError as exc:
        if exc.status_code == 404:
            return True
        raise
    client.delete_workflow(workflow_id)
    try:
        client.get_workflow(workflow_id)
    except N8nApiError as exc:
        if exc.status_code == 404:
            return True
        raise
    return False


def _matching_remote_workflow_ids(client, workflow_name: str) -> set[str]:
    """Recover an id after an ambiguous create timeout using the unique name."""
    return {
        str(item["id"])
        for item in client.list_workflows(limit=250)
        if isinstance(item, dict)
        and str(item.get("name") or "") == workflow_name
        and item.get("id") is not None
    }


def _cleanup_local_state(
    db,
    *,
    pipeline_id: str,
    known_asset_ids: set[str],
    known_storage_uris: set[str],
) -> dict[str, bool]:
    """Use the product deletion path and storage outbox, then prove zero residue."""
    from app.data_channel.curated.router import delete_curated
    from app.data_channel.datasets.models import Dataset, DatasetVersion
    from app.data_channel.datasets.service import (
        drain_storage_deletion_outbox,
        enqueue_storage_deletions,
    )
    from app.data_channel.file_assets.models import PipelineFileAsset
    from app.data_channel.pipelines.models import Pipeline, PipelineRun, PipelineVersion
    from app.data_channel.steward.models import N8nPipeline
    from app.shared.storage import get_storage_service

    db.rollback()

    # PipelineRun has a restrictive FK to DatasetVersion.  Runs are temporary
    # fixtures here, so remove them before calling the same curated deletion
    # endpoint used by administrators.
    db.query(PipelineRun).filter(
        PipelineRun.pipeline_id == pipeline_id
    ).delete(synchronize_session=False)
    db.commit()

    datasets = db.query(Dataset).filter(
        Dataset.producer_pipeline_id == pipeline_id
    ).all()
    dataset_ids = {item.id for item in datasets}
    # delete_curated 现在从依赖对象取决定人 id 写删除审计（admin=True 的
    # 旧占位写法随参数更名失效）；这里传真实管理员，保持审计口径一致。
    from types import SimpleNamespace

    from app.auth.models import User

    cleanup_admin = db.query(User).filter(
        User.role == "admin", User.is_active.is_(True)).first()
    cleanup_actor = cleanup_admin or SimpleNamespace(id="live-e2e-cleanup")
    for dataset_id in sorted(dataset_ids):
        delete_curated(dataset_id, force=False, db=db, admin=cleanup_actor)

    # A failed run may leave a deleted audit row without a DatasetVersion.
    # Queue any still-backed object before removing these test-only rows.
    remaining_assets = db.query(PipelineFileAsset).filter(
        PipelineFileAsset.pipeline_id == pipeline_id
    ).all()
    known_asset_ids.update(item.id for item in remaining_assets)
    known_storage_uris.update(
        str(item.storage_uri)
        for item in remaining_assets
        if item.storage_uri
    )
    enqueue_storage_deletions(
        db,
        [str(item.storage_uri) for item in remaining_assets if item.storage_uri],
    )
    if remaining_assets:
        db.query(PipelineFileAsset).filter(
            PipelineFileAsset.pipeline_id == pipeline_id
        ).delete(synchronize_session=False)

    db.query(N8nPipeline).filter(
        N8nPipeline.pipeline_id == pipeline_id
    ).delete(synchronize_session=False)
    db.query(PipelineVersion).filter(
        PipelineVersion.pipeline_id == pipeline_id
    ).delete(synchronize_session=False)
    db.query(Pipeline).filter(
        Pipeline.id == pipeline_id
    ).delete(synchronize_session=False)
    db.commit()

    # The product deletion endpoint already drains once.  A second bounded pass
    # retries transient failures and is idempotent.
    drain_storage_deletion_outbox(db, limit=1000, strict_schema=True)
    storage = get_storage_service()
    objects_deleted = all(
        not storage.object_exists(uri) for uri in sorted(known_storage_uris)
    )
    database_deleted = (
        db.query(Pipeline).filter(Pipeline.id == pipeline_id).first() is None
        and db.query(N8nPipeline).filter(
            N8nPipeline.pipeline_id == pipeline_id
        ).first() is None
        and db.query(PipelineRun).filter(
            PipelineRun.pipeline_id == pipeline_id
        ).first() is None
        and not db.query(Dataset).filter(Dataset.id.in_(dataset_ids)).first()
        and not db.query(DatasetVersion).filter(
            DatasetVersion.dataset_id.in_(dataset_ids)
        ).first()
        and not db.query(PipelineFileAsset).filter(
            PipelineFileAsset.id.in_(known_asset_ids)
        ).first()
    )
    return {
        "databaseDeleted": database_deleted,
        "storageObjectsDeleted": objects_deleted,
    }


def main(argv: list[str] | None = None) -> int:
    try:
        args = _args(argv)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2

    _install_backend_path()
    logging.basicConfig(level=logging.WARNING)

    # Imports happen only after the production backend path is resolved.  In a
    # container this loads exactly the same environment and database settings as
    # the running API process.
    import httpx

    from app.config import settings
    from app.database import SessionLocal
    from app.model_registry import import_all_models
    from app.data_channel.datasets.models import DatasetVersion
    from app.data_channel.datasets.service import DatasetService
    from app.data_channel.file_assets.models import PipelineFileAsset
    from app.data_channel.pipelines.models import Pipeline, PipelineRun
    from app.data_channel.steward import service as steward_service
    from app.data_channel.steward.models import N8nPipeline
    from app.data_channel.steward.runner import run_n8n_pipeline
    from app.auth.models import User
    from app.settings.workflows.n8n_client import N8nClient
    from app.shared.storage import get_storage_service

    # Standalone scripts do not pass through FastAPI's lifespan, so register
    # the full metadata graph before flushing PipelineRun.  In particular,
    # PipelineRun.task_id references v2_pipeline_tasks even when this fixture
    # intentionally has no task.
    import_all_models()

    suffix = uuid.uuid4().hex
    workflow_name = f"OB-FILE-ASSET-LIVE-E2E-{suffix[:12]}"
    webhook_path = f"ob-file-asset-live-e2e-{suffix}"
    pipeline_id = str(uuid.uuid4())
    binding_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    expected_content = f"OpenOntology FileRef E2E {run_id}\n".encode("utf-8")
    expected_sha256 = hashlib.sha256(expected_content).hexdigest()

    report: dict[str, Any] = {
        "ok": False,
        "checks": {
            "publicGatewayReachable": False,
            "workflowCreated": False,
            "workflowActivated": False,
            "pipelineRunSuccess": False,
            "singleLakeRow": False,
            "committedFileRef": False,
            "externalMinioObjectVerified": False,
            "lakeRowCredentialFree": False,
            "anonymousShareDownloadVerified": False,
        },
        "cleanup": {
            "remoteWorkflowDeleted": False,
            "databaseDeleted": False,
            "storageObjectsDeleted": False,
            "anonymousShareUnavailable": False,
        },
    }
    workflow_id: str | None = None
    remote_workflow_ids: set[str] = set()
    share_url: str | None = None
    client = None
    db = SessionLocal()
    asset_ids: set[str] = set()
    storage_uris: set[str] = set()
    original_origins = (
        settings.pipeline_file_gateway_base_url,
        settings.pipeline_file_public_app_base_url,
        settings.pipeline_file_public_api_base_url,
    )

    try:
        if settings.environment != "production":
            raise RuntimeError("this acceptance script requires ENVIRONMENT=production")

        _verify_public_health(
            args.public_root, timeout=min(float(args.wait_seconds), 30.0)
        )
        report["checks"]["publicGatewayReachable"] = True

        settings.pipeline_file_gateway_base_url = (
            f"{args.public_root}/api/v2/file-transfer"
        )
        settings.pipeline_file_public_app_base_url = args.public_root
        settings.pipeline_file_public_api_base_url = args.public_root

        client = steward_service.get_n8n_client(db)
        client.test_connection()
        created = client.create_workflow(
            _workflow_payload(name=workflow_name, webhook_path=webhook_path)
        )
        workflow_id = str(created.get("id") or "").strip()
        if not workflow_id:
            matches = _matching_remote_workflow_ids(client, workflow_name)
            if len(matches) == 1:
                workflow_id = next(iter(matches))
            else:
                raise RuntimeError(
                    "n8n create workflow response did not include a recoverable id"
                )
        remote_workflow_ids.add(workflow_id)
        report["checks"]["workflowCreated"] = True

        client.activate_workflow(workflow_id)
        remote = client.get_workflow(workflow_id)
        if not bool(remote.get("active")):
            raise RuntimeError("n8n workflow activation could not be confirmed")
        report["checks"]["workflowActivated"] = True

        snapshot = N8nClient.sanitize_workflow(remote)
        contract = steward_service.validate_managed_workflow_contract(snapshot)
        revision = N8nClient.workflow_revision(remote)
        missing_revision = [
            field for field in ("versionId", "updatedAt") if not revision.get(field)
        ]
        if missing_revision:
            raise RuntimeError(
                "n8n workflow did not expose the revision fields required by "
                "the published runtime contract"
            )
        if (
            revision.get("activeVersionId")
            and revision["activeVersionId"] != revision["versionId"]
        ):
            raise RuntimeError(
                "n8n active version differs from the current workflow version"
            )

        owner = db.query(User).filter(
            User.role == "admin", User.is_active.is_(True)
        ).first()
        owner_id = owner.id if owner is not None else None
        definition = {
            "engine": "n8n",
            "nodes": [],
            "edges": [],
            "n8n": {
                "steward_id": binding_id,
                "workflow_id": workflow_id,
                "webhook_path": webhook_path,
                "managed_contract": contract,
                "revision": revision,
                "expected_columns": ["record_id", "title", "attachment"],
                "wait_seconds": args.wait_seconds,
            },
        }
        pipeline = Pipeline(
            id=pipeline_id,
            name=workflow_name,
            domain="智能编排",
            description="Temporary cleaned live FileRef acceptance fixture",
            route="A",
            spec={},
            definition=definition,
            column_definitions=[
                {
                    "source_key": "record_id",
                    "field_key": "record_id",
                    "field_name": "记录编号",
                    "field_type": "string",
                    "is_primary_key": True,
                    "nullable": False,
                },
                {
                    "source_key": "title",
                    "field_key": "title",
                    "field_name": "标题",
                    "field_type": "string",
                    "is_primary_key": False,
                    "nullable": False,
                },
                {
                    "source_key": "attachment",
                    "field_key": "attachment",
                    "field_name": "附件",
                    "field_type": "json",
                    "is_primary_key": False,
                    "nullable": False,
                },
            ],
            status="published",
            enabled=True,
            created_by=owner_id,
        )
        binding = N8nPipeline(
            id=binding_id,
            name=workflow_name,
            description="Temporary cleaned live FileRef acceptance fixture",
            n8n_workflow_id=workflow_id,
            status="draft",
            workflow_snapshot=snapshot,
            pipeline_id=pipeline_id,
            created_by=owner_id,
        )
        run = PipelineRun(
            id=run_id,
            pipeline_id=pipeline_id,
            status="pending",
        )
        db.add_all([pipeline, binding, run])
        db.commit()

        run_n8n_pipeline(
            db,
            pipeline,
            run,
            {"mode": "overwrite", "skip_empty": True},
        )
        db.expire_all()
        run = db.query(PipelineRun).filter(PipelineRun.id == run_id).one()
        if run.status != "success":
            raise RuntimeError(
                "managed n8n pipeline run failed: "
                + _safe_error(run.error_log or "no persisted error detail", webhook_path)
            )
        report["checks"]["pipelineRunSuccess"] = True

        if not run.dataset_version_id:
            raise RuntimeError("successful run did not bind a DatasetVersion")
        version = db.query(DatasetVersion).filter(
            DatasetVersion.id == run.dataset_version_id
        ).one()
        if int(version.rowcount or 0) != 1:
            raise RuntimeError("curated DatasetVersion did not persist exactly one row")

        assets = db.query(PipelineFileAsset).filter(
            PipelineFileAsset.pipeline_id == pipeline_id,
            PipelineFileAsset.invocation_id == run_id,
        ).all()
        asset_ids.update(item.id for item in assets)
        storage_uris.update(
            str(item.storage_uri) for item in assets if item.storage_uri
        )
        if len(assets) != 1:
            raise RuntimeError("formal invocation did not produce exactly one FileRef")
        asset = assets[0]
        if (
            asset.status != "committed"
            or asset.dataset_version_id != version.id
            or not asset.share_token_hash
            or not asset.share_token_encrypted
            or asset.expires_at is not None
        ):
            raise RuntimeError("FileRef was not atomically committed to its lake version")
        report["checks"]["committedFileRef"] = True

        if not asset.storage_uri or not asset.storage_uri.startswith("s3://media/"):
            raise RuntimeError("FileRef was not stored in the external MinIO media bucket")
        storage = get_storage_service()
        if not storage.object_exists(asset.storage_uri):
            raise RuntimeError("FileRef MinIO object does not exist")
        stored_content = storage.get_object(asset.storage_uri)
        if (
            stored_content != expected_content
            or hashlib.sha256(stored_content).hexdigest() != expected_sha256
            or asset.sha256 != expected_sha256
            or int(asset.size) != len(expected_content)
        ):
            raise RuntimeError("FileRef MinIO content or SHA-256 does not match")
        report["checks"]["externalMinioObjectVerified"] = True

        rows = DatasetService(db).load_all_rows(version.dataset_id)
        if len(rows) != 1:
            raise RuntimeError("curated lake did not return exactly one row")
        row = rows[0]
        attachment = _json_object_cell(row.get("attachment"))
        if (
            row.get("record_id") != run_id
            or row.get("title") != "附件链路验收"
            or attachment is None
            or attachment.get("$type") != "file_ref"
            or attachment.get("id") != asset.id
            or attachment.get("sha256") != expected_sha256
        ):
            raise RuntimeError("curated lake row or canonical FileRef is incorrect")
        report["checks"]["singleLakeRow"] = True

        decoded_row = dict(row)
        decoded_row["attachment"] = attachment
        leaked_keys = _forbidden_keys(decoded_row)
        if leaked_keys:
            raise RuntimeError(
                "curated lake row contains forbidden runtime/storage fields: "
                + ", ".join(sorted(leaked_keys))
            )
        share_url = str(attachment.get("share_url") or "")
        parsed_share = urlsplit(share_url)
        parsed_root = urlsplit(args.public_root)
        if (
            not share_url
            or parsed_share.scheme != parsed_root.scheme
            or parsed_share.netloc != parsed_root.netloc
            or not _SHARE_PATH_RE.fullmatch(parsed_share.path)
            or parsed_share.query
            or parsed_share.fragment
        ):
            raise RuntimeError("canonical FileRef did not contain a trusted share URL")
        report["checks"]["lakeRowCredentialFree"] = True

        shared = httpx.get(
            share_url,
            headers={"Bypass-Tunnel-Reminder": "true"},
            timeout=min(float(args.wait_seconds), 30.0),
            follow_redirects=False,
        )
        if shared.status_code != 200 or shared.content != expected_content:
            raise RuntimeError(
                "anonymous FileRef download did not return the exact source bytes"
            )
        report["checks"]["anonymousShareDownloadVerified"] = True
    except Exception as exc:  # noqa: BLE001 - cleanup and redacted report are mandatory
        report["error"] = (
            f"{type(exc).__name__}: "
            f"{_safe_error(exc, webhook_path, share_url)}"
        )
    finally:
        if client is not None:
            try:
                # The exact-name lookup also recovers a workflow created by a
                # remote POST that timed out before returning its id.
                remote_workflow_ids.update(
                    _matching_remote_workflow_ids(client, workflow_name)
                )
                deleted = True
                for item_id in sorted(remote_workflow_ids):
                    deleted = _delete_remote_workflow(client, item_id) and deleted
                leftovers = _matching_remote_workflow_ids(client, workflow_name)
                report["cleanup"]["remoteWorkflowDeleted"] = (
                    deleted and not leftovers
                )
            except Exception as exc:  # noqa: BLE001
                report["cleanup"]["remoteWorkflowError"] = _safe_error(
                    exc, webhook_path, share_url
                )

        try:
            local_cleanup = _cleanup_local_state(
                db,
                pipeline_id=pipeline_id,
                known_asset_ids=asset_ids,
                known_storage_uris=storage_uris,
            )
            report["cleanup"].update(local_cleanup)
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            report["cleanup"]["localStateError"] = _safe_error(
                exc, webhook_path, share_url
            )

        if share_url:
            try:
                unavailable = httpx.get(
                    share_url,
                    headers={"Bypass-Tunnel-Reminder": "true"},
                    timeout=min(float(args.wait_seconds), 30.0),
                    follow_redirects=False,
                )
                report["cleanup"]["anonymousShareUnavailable"] = (
                    unavailable.status_code == 404
                )
            except Exception as exc:  # noqa: BLE001
                report["cleanup"]["shareVerificationError"] = _safe_error(
                    exc, webhook_path, share_url
                )

        (
            settings.pipeline_file_gateway_base_url,
            settings.pipeline_file_public_app_base_url,
            settings.pipeline_file_public_api_base_url,
        ) = original_origins
        db.close()

    checks_ok = all(report["checks"].values())
    cleanup_ok = all(
        report["cleanup"].get(key) is True
        for key in (
            "remoteWorkflowDeleted",
            "databaseDeleted",
            "storageObjectsDeleted",
            "anonymousShareUnavailable",
        )
    )
    report["ok"] = checks_ok and cleanup_ok
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
