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
