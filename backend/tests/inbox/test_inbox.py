from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.data_channel.pipeline_tasks.engine import _claim_task, _release_claim
from app.data_channel.pipeline_tasks.models import PipelineTask
from app.inbox.models import InboxDelivery, InboxEventReceipt, InboxItem, InboxOutboxEvent
from app.inbox.schemas import InboxEventIn
from app.inbox.service import (
    enqueue_ontology_approval_decision,
    enqueue_ontology_approval_request,
    drain_outbox,
    publish_event,
)
from app.models.v2.pipeline import Pipeline


def _pipeline_task(db, owner_id: str | None) -> PipelineTask:
    pipeline = Pipeline(
        id="inbox-pipeline",
        name="供应商数据流水线",
        spec={},
        status="published",
        enabled=True,
        created_by=owner_id,
    )
    task = PipelineTask(
        id="inbox-task",
        name="供应商每日同步",
        description="测试失败告警",
        pipeline_id=pipeline.id,
        write_mode="overwrite",
        schedule_type="MANUAL",
        enabled=True,
        status="idle",
        created_by=owner_id,
    )
    db.add_all([pipeline, task])
    db.commit()
    return task


def _release(db, task_id: str, status: str, run_id: str, error: str = "") -> None:
    task, token, claim_error = _claim_task(db, task_id)
    assert task is not None and token and claim_error is None
    assert _release_claim(
        db,
        task,
        token,
        status=status,
        error=error,
        run_id=run_id,
        trigger_type="scheduled",
    )


def _editor_headers(client, editor_user) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": editor_user.username, "password": "editor123"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def test_pipeline_task_failures_aggregate_until_success_then_start_new_incident(
    db, editor_user,
):
    task = _pipeline_task(db, editor_user.id)

    _release(db, task.id, "failed", "run-failed-1", "数据库连接超时")
    item = db.query(InboxItem).one()
    delivery = db.query(InboxDelivery).one()
    assert item.business_state == "open"
    assert item.occurrence_count == 1
    assert item.latest_occurrence_id == "run-failed-1"
    assert item.safe_context["triggerType"] == "scheduled"
    assert delivery.recipient_user_id == editor_user.id

    delivery.delivery_state = "read"
    delivery.read_at = datetime.utcnow()
    db.commit()
    _release(db, task.id, "failed", "run-failed-2", "数据库仍不可达")

    assert db.query(InboxItem).count() == 1
    db.refresh(item)
    db.refresh(delivery)
    assert item.occurrence_count == 2
    assert item.latest_occurrence_id == "run-failed-2"
    assert delivery.delivery_state == "unread"
    assert delivery.read_at is None

    _release(db, task.id, "success", "run-success")
    db.refresh(item)
    assert item.business_state == "resolved"
    assert item.open_key is None
    assert item.resolution_reason == "next_run_succeeded"

    _release(db, task.id, "failed", "run-failed-3", "权限被拒绝")
    assert db.query(InboxItem).count() == 2
    newest = db.query(InboxItem).filter(InboxItem.business_state == "open").one()
    assert newest.occurrence_count == 1
    assert newest.latest_occurrence_id == "run-failed-3"
    assert db.query(InboxOutboxEvent).filter(
        InboxOutboxEvent.status == "completed",
    ).count() == 4


def test_pipeline_failure_without_an_active_recipient_does_not_poison_outbox(db):
    task = _pipeline_task(db, None)

    _release(db, task.id, "failed", "run-without-owner", "连接失败")

    assert db.query(InboxItem).count() == 0
    event = db.query(InboxOutboxEvent).one()
    assert event.status == "completed"
    assert event.attempts == 1


def test_ontology_approval_outbox_projects_and_closes_idempotently(
    db, admin_user,
):
    log_id = "approval-log-1"
    enqueue_ontology_approval_request(
        db,
        log_id=log_id,
        ontology_id="ontology-1",
        action_name="发送通知",
        ontology_version="v1",
        ontology_release_id="release-1",
    )
    db.commit()
    assert drain_outbox(db) == {"processed": 1, "failed": 0}
    item = db.query(InboxItem).one()
    delivery = db.query(InboxDelivery).one()
    assert delivery.recipient_user_id == admin_user.id
    assert item.business_state == "open"
    assert item.safe_context["approvalLogId"] == log_id
    assert item.resource["href"].startswith("/ontologies/ontology-1?")

    # Re-enqueueing the same request is a no-op; a terminal decision closes
    # the existing item through the same correlation key.
    enqueue_ontology_approval_request(
        db,
        log_id=log_id,
        ontology_id="ontology-1",
        action_name="发送通知",
        ontology_version="v1",
        ontology_release_id="release-1",
    )
    enqueue_ontology_approval_decision(
        db,
        log_id=log_id,
        ontology_id="ontology-1",
        decision="approved",
        action_name="发送通知",
        ontology_version="v1",
        ontology_release_id="release-1",
    )
    db.commit()
    assert drain_outbox(db) == {"processed": 1, "failed": 0}
    db.refresh(item)
    assert item.business_state == "resolved"
    assert item.open_key is None


def test_inbox_api_is_user_scoped_and_open_alert_cannot_be_archived(
    client, db, editor_user, admin_user, auth_headers,
):
    task = _pipeline_task(db, editor_user.id)
    _release(
        db,
        task.id,
        "failed",
        "run-api",
        "连接 postgresql://alice:s3cr3t@db/main 失败，token=abc123，返回 503",
    )
    editor_headers = _editor_headers(client, editor_user)

    summary = client.get("/api/v2/inbox/summary", headers=editor_headers)
    assert summary.status_code == 200
    assert summary.json()["data"] == {
        "openAlertCount": 1,
        "actionableCount": 1,
        "unreadCount": 1,
        "resolvedCount": 0,
    }

    response = client.get(
        "/api/v2/inbox",
        headers=editor_headers,
        params={"tab": "actionable", "limit": 10},
    )
    assert response.status_code == 200
    message = response.json()["data"]["items"][0]
    assert message["title"] == "数据任务执行失败：供应商每日同步"
    assert message["resource"]["href"].endswith(
        "task_id=inbox-task&run_id=run-api"
    )
    assert message["safeContext"]["failureCount"] == 1
    assert "s3cr3t" not in message["summary"]
    assert "abc123" not in message["summary"]
    assert "***" in message["summary"]

    forbidden = client.patch(
        f"/api/v2/inbox/{message['id']}",
        headers=editor_headers,
        json={"state": "archived"},
    )
    assert forbidden.status_code == 409

    read = client.patch(
        f"/api/v2/inbox/{message['id']}",
        headers=editor_headers,
        json={"state": "read"},
    )
    assert read.status_code == 200
    assert read.json()["data"]["deliveryState"] == "read"
    # Reading acknowledges only the delivery; the unresolved task remains in
    # the red operational badge.
    after = client.get("/api/v2/inbox/summary", headers=editor_headers).json()["data"]
    assert after["openAlertCount"] == 1
    assert after["unreadCount"] == 0

    # A new occurrence becomes unread again; bulk acknowledgement changes only
    # the personal read state and must leave the operational badge open.
    _release(db, task.id, "failed", "run-api-2", "上游接口仍返回 503")
    bulk_read = client.post("/api/v2/inbox/read-all", headers=editor_headers)
    assert bulk_read.status_code == 200
    assert bulk_read.json()["data"]["updated"] == 1
    after_bulk = client.get(
        "/api/v2/inbox/summary", headers=editor_headers,
    ).json()["data"]
    assert after_bulk["openAlertCount"] == 1
    assert after_bulk["unreadCount"] == 0

    _release(db, task.id, "success", "run-api-success")
    archived = client.patch(
        f"/api/v2/inbox/{message['id']}",
        headers=editor_headers,
        json={"state": "archived"},
    )
    assert archived.status_code == 200
    assert client.get(
        "/api/v2/inbox", headers=editor_headers, params={"tab": "resolved"},
    ).json()["data"]["items"] == []
    assert len(client.get(
        "/api/v2/inbox", headers=editor_headers, params={"tab": "archived"},
    ).json()["data"]["items"]) == 1

    admin_list = client.get("/api/v2/inbox", headers=auth_headers)
    assert admin_list.status_code == 200
    assert admin_list.json()["data"]["items"] == []


def test_inbox_event_contract_is_idempotent_and_rejects_event_id_reuse(
    db, editor_user,
):
    payload = {
        "schemaVersion": "v1",
        "eventId": "contract-event-1",
        "occurredAt": datetime.now(timezone.utc),
        "operation": "upsert",
        "source": {
            "system": "test",
            "type": "failure",
            "id": "source-1",
            "occurrenceId": "occurrence-1",
            "correlationKey": "source-1:failure",
        },
        "item": {
            "kind": "alert",
            "priority": "high",
            "title": "测试告警",
            "summary": "第一次",
        },
        "resource": {
            "type": "test_resource",
            "id": "source-1",
            "label": "测试",
            "href": "/overview",
        },
        "audience": {"type": "user", "userId": editor_user.id},
        "actions": [{"key": "open", "label": "查看", "href": "/overview"}],
    }
    event = InboxEventIn.model_validate(payload)
    first = publish_event(db, event)
    db.commit()
    second = publish_event(db, event)
    db.commit()
    assert first.id == second.id
    assert db.query(InboxItem).count() == 1
    assert db.query(InboxEventReceipt).count() == 1

    changed = InboxEventIn.model_validate({
        **payload,
        "item": {**payload["item"], "summary": "偷偷换掉内容"},
    })
    with pytest.raises(ValueError, match="reused with another payload"):
        publish_event(db, changed)

    external_link = {
        **payload,
        "eventId": "contract-event-external-link",
        "resource": {**payload["resource"], "href": "https://example.com"},
    }
    with pytest.raises(ValueError, match="internal absolute path"):
        InboxEventIn.model_validate(external_link)
