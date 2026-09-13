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
        headers={**auth_headers, "Idempotency-Key": "retry-new"},
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
        headers={**auth_headers, "Idempotency-Key": "retry-new"},
    )
    assert replay.status_code == 202
    assert replay.json()["run_id"] == new_id


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
