import hashlib
import json
from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy.orm import sessionmaker

from app.super_assistant.models import SuperAssistantConversation
from app.super_assistant.kernel.models import Artifact, ExecutionRun
from app.super_assistant.kernel.schemas import InputRequest


def test_kernel_create_get_and_cancel_contract(client, db, admin_user, auth_headers):
    conversation = SuperAssistantConversation(owner_id=admin_user.id, title="kernel api")
    db.add(conversation)
    db.commit()

    payload = {"goal": "make a plan", "idempotency_key": "api-create-1"}
    headers = {**auth_headers, "Idempotency-Key": "api-create-1"}
    created = client.post(
        f"/api/v2/super-assistant/conversations/{conversation.id}/runs",
        json=payload,
        headers=headers,
    )
    assert created.status_code == 202, created.text
    run_id = created.json()["run_id"]

    fetched = client.get(f"/api/v2/super-assistant/runs/{run_id}", headers=auth_headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["status"] == "queued"

    missing_if_match = client.post(
        f"/api/v2/super-assistant/runs/{run_id}/cancel",
        json={"idempotency_key": "api-cancel-1"}, headers=auth_headers,
    )
    assert missing_if_match.status_code == 428

    cancelled = client.post(
        f"/api/v2/super-assistant/runs/{run_id}/cancel",
        json={"idempotency_key": "api-cancel-1"},
        headers={**auth_headers, "If-Match": '"1"', "Idempotency-Key": "api-cancel-1"},
    )
    assert cancelled.status_code == 202, cancelled.text
    assert cancelled.json()["status"] == "cancel_requested"


def test_kernel_create_requires_matching_idempotency_header(client, db, admin_user, auth_headers):
    conversation = SuperAssistantConversation(owner_id=admin_user.id, title="kernel api")
    db.add(conversation)
    db.commit()
    response = client.post(
        f"/api/v2/super-assistant/conversations/{conversation.id}/runs",
        json={"goal": "x", "idempotency_key": "body-key"},
        headers={**auth_headers, "Idempotency-Key": "other-key"},
    )
    assert response.status_code == 422


def test_kernel_input_content_limit_is_utf8_bytes():
    # 100k CJK code points fit the old character-count check but exceed 256 KiB.
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        InputRequest(content="中" * 100_000, idempotency_key="utf8-cap")


def test_kernel_input_requires_headers_and_rejects_terminal_run(client, db, admin_user, auth_headers):
    conversation = SuperAssistantConversation(owner_id=admin_user.id, title="kernel input contract")
    db.add(conversation)
    db.commit()
    created = client.post(
        f"/api/v2/super-assistant/conversations/{conversation.id}/runs",
        json={"goal": "terminal input", "idempotency_key": "terminal-create"},
        headers={**auth_headers, "Idempotency-Key": "terminal-create"},
    )
    run_id = created.json()["run_id"]
    run = db.get(ExecutionRun, run_id)
    run.status = "completed"
    db.commit()
    missing = client.post(
        f"/api/v2/super-assistant/runs/{run_id}/inputs",
        json={"content": "x", "idempotency_key": "input-missing"},
        headers=auth_headers,
    )
    assert missing.status_code == 428
    rejected = client.post(
        f"/api/v2/super-assistant/runs/{run_id}/inputs",
        json={"content": "x", "idempotency_key": "input-terminal"},
        headers={**auth_headers, "If-Match": '"1"', "Idempotency-Key": "input-terminal"},
    )
    assert rejected.status_code == 409


def test_kernel_control_and_input_wake_waiting_run(client, db, admin_user, auth_headers):
    conversation = SuperAssistantConversation(owner_id=admin_user.id, title="kernel controls")
    db.add(conversation)
    db.commit()
    created = client.post(
        f"/api/v2/super-assistant/conversations/{conversation.id}/runs",
        json={"goal": "wait for a detail", "idempotency_key": "control-create"},
        headers={**auth_headers, "Idempotency-Key": "control-create"},
    )
    run_id = created.json()["run_id"]
    from app.super_assistant.kernel.models import ExecutionRun

    run = db.get(ExecutionRun, run_id)
    run.status = "active"
    db.commit()
    paused = client.post(
        f"/api/v2/super-assistant/runs/{run_id}/pause",
        json={"idempotency_key": "pause-1"},
        headers={**auth_headers, "If-Match": '"1"', "Idempotency-Key": "pause-1"},
    )
    assert paused.status_code == 202
    resumed = client.post(
        f"/api/v2/super-assistant/runs/{run_id}/resume",
        json={"idempotency_key": "resume-1"},
        headers={**auth_headers, "If-Match": '"2"', "Idempotency-Key": "resume-1"},
    )
    assert resumed.status_code == 202
    run = db.get(ExecutionRun, run_id)
    run.status = "waiting_input"
    run.wait_reason = "question"
    db.commit()
    from app.super_assistant.kernel.models import InboxItem as KernelInboxItem

    db.add(KernelInboxItem(
        run_id=run_id, kind="question_answer", priority=30, status="pending",
        question_id="q1", target_ref="q1", payload={"question": "需要什么细节？"},
        source="system", accepted_at=run.created_at, idempotency_key="question-q1",
    ))
    db.commit()
    answer = client.post(
        f"/api/v2/super-assistant/runs/{run_id}/inputs",
        json={"kind": "question_answer", "question_id": "q1", "content": "detail", "idempotency_key": "input-1"},
        headers={**auth_headers, "If-Match": '"3"', "Idempotency-Key": "input-1"},
    )
    assert answer.status_code == 202, answer.text
    replay = client.post(
        f"/api/v2/super-assistant/runs/{run_id}/inputs",
        json={"kind": "question_answer", "question_id": "q1", "content": "detail", "idempotency_key": "input-1"},
        headers={**auth_headers, "If-Match": '"4"', "Idempotency-Key": "input-1"},
    )
    assert replay.status_code == 202
    assert replay.json()["inbox_id"] == answer.json()["inbox_id"]
    from app.super_assistant.kernel.models import ExecutionDispatchOutbox
    assert db.query(ExecutionDispatchOutbox).filter_by(run_id=run_id).count() >= 2


def test_kernel_retry_creates_new_run_without_reopening_failed_run(client, db, admin_user, auth_headers):
    conversation = SuperAssistantConversation(owner_id=admin_user.id, title="kernel retry")
    db.add(conversation)
    db.commit()
    created = client.post(
        f"/api/v2/super-assistant/conversations/{conversation.id}/runs",
        json={"goal": "retryable task", "idempotency_key": "retry-source"},
        headers={**auth_headers, "Idempotency-Key": "retry-source"},
    )
    source_id = created.json()["run_id"]
    from app.super_assistant.kernel.models import ExecutionRun

    source = db.get(ExecutionRun, source_id)
    source.status = "failed"
    db.commit()
    retried = client.post(
        f"/api/v2/super-assistant/runs/{source_id}/retry",
        json={"idempotency_key": "retry-new", "max_steps": 3},
        headers={**auth_headers, "Idempotency-Key": "retry-new", "If-Match": '"1"'},
    )
    assert retried.status_code == 202, retried.text
    new_id = retried.json()["run_id"]
    assert new_id != source_id
    new_run = db.get(ExecutionRun, new_id)
    assert new_run.goal == source.goal
    assert new_run.status == "queued"
    assert db.get(ExecutionRun, source_id).status == "failed"

    replay = client.post(
        f"/api/v2/super-assistant/runs/{source_id}/retry",
        json={"idempotency_key": "retry-new", "max_steps": 3},
        headers={**auth_headers, "Idempotency-Key": "retry-new", "If-Match": '"1"'},
    )
    assert replay.status_code == 202
    assert replay.json()["run_id"] == new_id


def test_kernel_sse_snapshot_shape_and_cursor_validation(client, db, admin_user, auth_headers, monkeypatch):
    conversation = SuperAssistantConversation(owner_id=admin_user.id, title="kernel sse")
    db.add(conversation)
    db.commit()
    created = client.post(
        f"/api/v2/super-assistant/conversations/{conversation.id}/runs",
        json={"goal": "sse snapshot", "idempotency_key": "sse-create"},
        headers={**auth_headers, "Idempotency-Key": "sse-create"},
    )
    run_id = created.json()["run_id"]
    run = db.get(ExecutionRun, run_id)
    run.status = "completed"
    db.commit()
    # The production stream intentionally uses a fresh SessionLocal. Point it
    # at this test database so the polling generator sees the fixture rows.
    local_session = sessionmaker(bind=db.get_bind())
    monkeypatch.setattr("app.super_assistant.kernel.router.SessionLocal", local_session)
    response = client.get(f"/api/v2/super-assistant/runs/{run_id}/events", headers=auth_headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    snapshot_line = next(line for line in response.text.splitlines() if line.startswith("data: "))
    snapshot = json.loads(snapshot_line.removeprefix("data: "))
    assert snapshot["run"]["run_id"] == run_id
    assert snapshot["run"]["conversation_id"] == conversation.id
    assert {"status", "version", "goal", "current_inbox", "calls", "artifacts", "binding_snapshot"} <= set(snapshot["run"])
    negative = client.get(
        f"/api/v2/super-assistant/runs/{run_id}/events",
        headers={**auth_headers, "Last-Event-ID": f"{run_id}:-1"},
    )
    assert negative.status_code == 400


def test_kernel_openapi_declares_sse_and_artifact_media_types(client):
    from app.main import app

    schema = app.openapi()
    sse = schema["paths"]["/api/v2/super-assistant/runs/{run_id}/events"]["get"]["responses"]["200"]["content"]
    download = schema["paths"]["/api/v2/super-assistant/runs/{run_id}/artifacts/{artifact_id}/download"]["get"]["responses"]["200"]["content"]
    assert "text/event-stream" in sse
    assert "application/octet-stream" in download


def test_kernel_run_list_is_owner_scoped_and_keeps_independent_runs(client, db, admin_user, auth_headers):
    conversation = SuperAssistantConversation(owner_id=admin_user.id, title="kernel run list")
    db.add(conversation)
    db.commit()
    for index in range(2):
        response = client.post(
            f"/api/v2/super-assistant/conversations/{conversation.id}/runs",
            json={"goal": f"task {index}", "idempotency_key": f"list-{index}"},
            headers={**auth_headers, "Idempotency-Key": f"list-{index}"},
        )
        assert response.status_code == 202
    listed = client.get(
        f"/api/v2/super-assistant/conversations/{conversation.id}/runs?limit=10",
        headers=auth_headers,
    )
    assert listed.status_code == 200, listed.text
    assert {item["goal"] for item in listed.json()} == {"task 0", "task 1"}


def _artifact_fixture(db, owner, *, content: bytes, storage_ref: str | None = None, inline: bool = False, mime_type: str = "application/octet-stream", status: str = "complete", retention_until=None):
    conversation = SuperAssistantConversation(owner_id=owner.id, title="artifact")
    db.add(conversation)
    db.flush()
    run = ExecutionRun(owner_id=owner.id, conversation_id=conversation.id, goal="artifact", status="completed", idempotency_key="artifact-fixture", payload_hash="fixture")
    db.add(run)
    db.flush()
    artifact = Artifact(
        owner_id=owner.id, run_id=run.id, kind="file", mime_type=mime_type,
        size=len(content), checksum="sha256:" + hashlib.sha256(content).hexdigest(),
        storage_ref=storage_ref or "pending://artifact", inline_content=content.decode("utf-8") if inline else None,
        status=status, integrity_status="verified" if status == "complete" else "pending", business_status="success",
        retention_until=retention_until,
    )
    db.add(artifact)
    db.flush()
    if storage_ref is None:
        artifact.storage_ref = f"s3://assistant-workspace/{owner.id}/{run.id}/{artifact.id}"
    db.commit()
    return run, artifact


def test_kernel_inline_artifact_download_remains_compatible(client, db, admin_user, auth_headers):
    data = "内联文本".encode("utf-8")
    run, artifact = _artifact_fixture(db, admin_user, content=data, inline=True, mime_type="text/plain", storage_ref="inline://artifact")
    response = client.get(f"/api/v2/super-assistant/runs/{run.id}/artifacts/{artifact.id}/download", headers=auth_headers)
    assert response.status_code == 200
    assert response.content == data
    assert response.headers["content-type"].startswith("text/plain")


def test_kernel_artifact_download_verifies_object_bytes_and_preserves_json_shape(client, db, admin_user, auth_headers, monkeypatch):
    data = b"binary artifact\x00"
    run, artifact = _artifact_fixture(db, admin_user, content=data)

    class Store:
        def get_object(self, uri):
            assert uri == artifact.storage_ref
            return data

    monkeypatch.setattr("app.super_assistant.kernel.router.get_storage_service", lambda: Store())
    fetched = client.get(f"/api/v2/super-assistant/runs/{run.id}/artifacts/{artifact.id}", headers=auth_headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["content"] is None
    assert fetched.json()["download_url"].endswith("/download")
    downloaded = client.get(f"/api/v2/super-assistant/runs/{run.id}/artifacts/{artifact.id}/download", headers=auth_headers)
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == data
    assert downloaded.headers["x-artifact-checksum"] == artifact.checksum


def test_kernel_artifact_download_rejects_corrupt_or_unscoped_object(client, db, admin_user, auth_headers, monkeypatch):
    data = b"correct"
    run, artifact = _artifact_fixture(db, admin_user, content=data)

    class Store:
        def get_object(self, uri):
            return b"corrupt"

    monkeypatch.setattr("app.super_assistant.kernel.router.get_storage_service", lambda: Store())
    corrupt = client.get(f"/api/v2/super-assistant/runs/{run.id}/artifacts/{artifact.id}/download", headers=auth_headers)
    assert corrupt.status_code == 409
    artifact.storage_ref = "s3://assistant-workspace/other-owner/other-run/other-artifact"
    db.commit()
    unscoped = client.get(f"/api/v2/super-assistant/runs/{run.id}/artifacts/{artifact.id}/download", headers=auth_headers)
    assert unscoped.status_code == 409


def test_kernel_artifact_download_expired_deleted_and_cross_owner_are_not_read(client, db, admin_user, editor_user, auth_headers, monkeypatch):
    data = b"secret"
    run, artifact = _artifact_fixture(db, admin_user, content=data, retention_until=datetime.now(timezone.utc) - timedelta(seconds=1))
    monkeypatch.setattr("app.super_assistant.kernel.router.get_storage_service", lambda: (_ for _ in ()).throw(AssertionError("storage must not be touched")))
    expired = client.get(f"/api/v2/super-assistant/runs/{run.id}/artifacts/{artifact.id}/download", headers=auth_headers)
    assert expired.status_code == 410
    artifact.retention_until = None
    artifact.status = "deleted"
    db.commit()
    deleted = client.get(f"/api/v2/super-assistant/runs/{run.id}/artifacts/{artifact.id}/download", headers=auth_headers)
    assert deleted.status_code == 410
    # A different authenticated owner cannot even discover the row.
    from app.auth.service import create_access_token
    other_headers = {"Authorization": "Bearer " + create_access_token({"sub": editor_user.id})}
    cross_owner = client.get(f"/api/v2/super-assistant/runs/{run.id}/artifacts/{artifact.id}/download", headers=other_headers)
    assert cross_owner.status_code in {401, 404}
