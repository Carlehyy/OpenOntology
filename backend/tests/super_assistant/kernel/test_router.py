from app.super_assistant.models import SuperAssistantConversation


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
        headers={**auth_headers, "If-Match": '"1"'},
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
        headers={**auth_headers, "If-Match": '"1"'},
    )
    assert paused.status_code == 202
    resumed = client.post(
        f"/api/v2/super-assistant/runs/{run_id}/resume",
        json={"idempotency_key": "resume-1"},
        headers={**auth_headers, "If-Match": '"2"'},
    )
    assert resumed.status_code == 202
    run = db.get(ExecutionRun, run_id)
    run.status = "waiting_input"
    run.wait_reason = "question"
    db.commit()
    answer = client.post(
        f"/api/v2/super-assistant/runs/{run_id}/inputs",
        json={"kind": "question_answer", "question_id": "q1", "content": "detail", "idempotency_key": "input-1"},
        headers=auth_headers,
    )
    assert answer.status_code == 202, answer.text
    replay = client.post(
        f"/api/v2/super-assistant/runs/{run_id}/inputs",
        json={"kind": "question_answer", "question_id": "q1", "content": "detail", "idempotency_key": "input-1"},
        headers=auth_headers,
    )
    assert replay.status_code == 202
    assert replay.json()["inbox_id"] == answer.json()["inbox_id"]
