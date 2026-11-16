"""Pinned Multica reads/writes preserve uncertainty and ordinary comment behavior."""
from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from app.super_assistant import multica_client as client

BASE = "http://127.0.0.1:18080"


def response(payload, *, status=200, headers=None):
    return httpx.Response(status, json=payload, headers=headers or {})


@pytest.mark.parametrize("kind", ["activity", "comment", "activity,comment", "true"])
def test_timeline_truncation_is_never_complete(monkeypatch, kind):
    monkeypatch.setattr(client, "_request", lambda *a, **kw: response(
        [{"id": "activity-1"}], headers={"X-Timeline-Truncated": kind}))
    result = client.list_timeline(BASE, "fixture-pat", "workspace-a", "TASK-1")
    assert not result.complete
    assert result.reason == "timeline_server_truncated_no_cursor"
    assert result.items == [{"id": "activity-1"}]


@pytest.mark.parametrize("direction", ["has_more_before", "has_more_after"])
def test_timeline_both_pagination_directions_are_incomplete(monkeypatch, direction):
    monkeypatch.setattr(client, "_request", lambda *a, **kw: response(
        {"entries": [{"id": "activity-1"}], direction: True}))
    assert not client.list_timeline(BASE, "fixture-pat", "workspace-a", "TASK-1").complete


def test_comment_pagination_preserves_opaque_nano_cursor_and_rereads(monkeypatch):
    calls = []
    nano = "2026-11-14T03:04:05.123456789Z"
    pages = iter([
        response([{"id": "new", "created_at": "2026-11-14T03:05:00Z"}], headers={
            "X-Multica-Next-Before": nano, "X-Multica-Next-Before-ID": "boundary"}),
        response([{"id": "old"}]),
        response([{"id": "new"}, {"id": "late", "created_at": "2025-01-01T00:00:00Z"}]),
    ])

    def request(method, url, **kw):
        calls.append(kw)
        return next(pages)

    monkeypatch.setattr(client, "_request", request)
    first = client.list_comments(BASE, "fixture-pat", "workspace-a", "TASK-1")
    second = client.list_comments(BASE, "fixture-pat", "workspace-a", "TASK-1")
    assert first.complete and second.complete
    assert calls[1]["params"] == {"recent": "100", "before": nano, "before_id": "boundary"}
    assert calls[2]["params"] == {"recent": "100"}
    assert "late" in {item["id"] for item in second.items}
    assert all(kw["headers"]["X-Workspace-ID"] == "workspace-a" for kw in calls)
    assert all(kw["headers"]["Authorization"] == "Bearer fixture-pat" for kw in calls)
    assert all(kw["follow_redirects"] is False for kw in calls)
    assert all("summary" not in kw["params"] and "fold" not in kw["params"] for kw in calls)


def test_comment_incomplete_cursor_and_repeated_boundary_do_not_prove_absence(monkeypatch):
    monkeypatch.setattr(client, "_request", lambda *a, **kw: response(
        [{"id": "a"}], headers={"X-Multica-Next-Before": "opaque"}))
    result = client.list_comments(BASE, "fixture-pat", "workspace-a", "TASK-1")
    assert not result.complete and result.reason == "comment_cursor_invalid"


def test_active_run_steering_is_explicit_and_plain_comment_still_triggers(monkeypatch):
    task = str(uuid4())
    other = str(uuid4())
    calls = []

    def request(method, url, **kw):
        calls.append((method, url, kw))
        if method == "GET":
            return response({"tasks": [{"id": other, "status": "running"},
                                       {"id": task, "status": "running"}]})
        return response({"id": "comment-1", "supplements": None,
                         "trigger_outcomes": [{"status": "queued"}]})

    monkeypatch.setattr(client, "_request", request)
    result = client.create_comment(BASE, "fixture-pat", "workspace-a", "TASK-1",
        content="Please continue", steer_task_id=task)
    assert calls[-1][2]["json"] == {"content": "Please continue", "steer_task_ids": [task]}
    assert result.comment_id == "comment-1" and result.execution_state == "unknown"
    calls.clear()
    client.create_comment(BASE, "fixture-pat", "workspace-a", "TASK-1", content="ordinary reply")
    assert len(calls) == 1 and calls[0][0] == "POST"
    assert calls[0][2]["json"] == {"content": "ordinary reply"}


@pytest.mark.parametrize("status", ["pending", "completed", "failed", "cancelled"])
def test_stale_steer_target_causes_no_write(monkeypatch, status):
    task = str(uuid4())
    calls = []

    def request(method, url, **kw):
        calls.append(method)
        return response({"tasks": [{"id": task, "status": status}]})

    monkeypatch.setattr(client, "_request", request)
    with pytest.raises(client.MulticaClientError, match="当前有效运行"):
        client.create_comment(BASE, "fixture-pat", "workspace-a", "TASK-1", content="reply", steer_task_id=task)
    assert calls == ["GET"]


@pytest.mark.parametrize("matching, timestamp, expected", [
    (True, "2026-11-14T03:00:00Z", "confirmed"),
    (False, "2026-11-14T03:00:00Z", "unknown"),
    (True, None, "unknown"),
])
def test_saved_comment_and_delivery_are_distinct(monkeypatch, matching, timestamp, expected):
    task = str(uuid4())
    monkeypatch.setattr(client, "get_active_task", lambda *a: {"tasks": [{"id": task, "status": "running"}]})
    monkeypatch.setattr(client, "_request", lambda *a, **kw: response({"id": "saved-comment",
        "supplements": [{"task_id": task if matching else str(uuid4()), "status": "delivered",
                         "delivered_at": timestamp}]}))
    result = client.create_comment(BASE, "fixture-pat", "workspace-a", "TASK-1", content="reply", steer_task_id=task)
    assert result.comment_id == "saved-comment" and result.execution_state == expected


def test_create_preserves_priority_assignment_and_remote_initial_status(monkeypatch):
    payloads = []

    def request(*a, **kw):
        payloads.append(kw["json"])
        return response({"id": "issue-1", "status": "todo", "priority": "urgent"})

    monkeypatch.setattr(client, "_request", request)
    issue = client.create_issue(BASE, "fixture-pat", "workspace-a", title="Title", description="Details",
        assignee_id="agent-1", priority="urgent")
    assert payloads == [{"title": "Title", "description": "Details", "priority": "urgent",
                         "assignee_type": "agent", "assignee_id": "agent-1"}]
    assert issue["status"] == "todo"
    with pytest.raises(client.MulticaClientError, match="priority"):
        client.create_issue(BASE, "fixture-pat", "workspace-a", title="Title", priority="critical")
    assert len(payloads) == 1


@pytest.mark.parametrize("method, expected", [("GET", "failed"), ("POST", "unknown")])
def test_transport_error_is_sanitized_and_write_is_unknown(monkeypatch, method, expected):
    def request(*a, **kw):
        raise httpx.ReadTimeout("fixture-sensitive-token in https://private.invalid")

    monkeypatch.setattr(client, "_request", request)
    with pytest.raises(client.MulticaRequestError) as error:
        client._call(method, BASE, "fixture-sensitive-token", "/api/issues", workspace_id="workspace-a")
    assert error.value.outcome == expected
    assert "fixture-sensitive-token" not in str(error.value)
    assert "private.invalid" not in str(error.value)


@pytest.mark.parametrize("status, expected", [(400, "failed"), (409, "failed"), (500, "unknown"), (503, "unknown")])
def test_error_body_cannot_leak_token_or_claim_nonexecution(monkeypatch, status, expected):
    monkeypatch.setattr(client, "_request", lambda *a, **kw: response(
        {"error": "fixture-sensitive-token internal details"}, status=status))
    with pytest.raises(client.MulticaRequestError) as error:
        client.create_comment(BASE, "fixture-sensitive-token", "workspace-a", "TASK-1", content="reply")
    assert error.value.outcome == expected and error.value.status_code == status
    assert "fixture-sensitive-token" not in str(error.value)


def test_success_without_remote_identity_remains_unknown(monkeypatch):
    monkeypatch.setattr(client, "_request", lambda *a, **kw: response({"success": True}))
    with pytest.raises(client.MulticaRequestError) as error:
        client.create_comment(BASE, "fixture-pat", "workspace-a", "TASK-1", content="reply")
    assert error.value.outcome == "unknown"


@pytest.mark.parametrize("url", ["https://evil.invalid/workspace-a/issues/issue-1", "javascript:alert(1)",
    "http://user:password@127.0.0.1:18080/workspace-a/issues/issue-1", BASE + "/",
    BASE + "/workspace-a/issues/issue-1#unsafe", "/workspace-a/issues/issue-1",
    BASE + "/settings", BASE + "/api/issues/issue-1/comments", BASE + "/workspace-a/issues/another",
    BASE + "/workspace-a/issues/issue-1?redirect=unsafe", BASE + "/%2e%2e/issues/issue-1",
    BASE + "/workspace-a/issues/issue%2f1", BASE + "/work%5cspace/issues/issue-1",
    "\n" + BASE + "/workspace-a/issues/issue-1"])
def test_issue_url_requires_matching_same_origin_issue_route(url):
    assert client.verified_issue_url(BASE, {"id": "issue-1", "url": url}) is None


def test_issue_url_has_no_guessed_fallback():
    assert client.verified_issue_url(BASE, {"id": "issue-1", "identifier": "TASK-1"}) is None
    assert client.verified_issue_url(BASE, {"description": BASE + "/workspace-a/issues/issue-1"}) is None
    assert client.verified_issue_url(BASE, {"web_url": BASE + "/workspace-a/issues/issue-1"}) is None
    for identity in ("issue-1", "TASK-1"):
        url = BASE + "/workspace-a/issues/" + identity
        assert client.verified_issue_url(BASE, {"id": "issue-1", "identifier": "TASK-1", "web_url": url}) == url
    url = BASE + "/multica/workspace-a/issues/TASK-1"
    assert client.verified_issue_url(BASE + "/multica", {"identifier": "TASK-1", "html_url": url}) == url


def test_messages_use_task_sequence_and_reject_ambiguous_or_duplicate_ids(monkeypatch):
    task = str(uuid4())
    pages = iter([
        response([{"task_id": task, "seq": 2, "content": "later"},
                  {"task_id": task, "seq": 0, "content": "late earlier row"}]),
        response([{"task_id": task, "seq": True}]),
        response([{"task_id": task, "seq": 2}, {"task_id": task, "seq": 2}]),
    ])
    monkeypatch.setattr(client, "_request", lambda *a, **kw: next(pages))
    result = client.list_task_messages(BASE, "fixture-pat", "workspace-a", task)
    assert result.complete
    assert {item["id"] for item in result.items} == {f"{task}:0", f"{task}:2"}
    assert not client.list_task_messages(BASE, "fixture-pat", "workspace-a", task).complete
    assert not client.list_task_messages(BASE, "fixture-pat", "workspace-a", task).complete


def test_issue_full_scan_marks_changed_total_incomplete(monkeypatch):
    pages = iter([response({"issues": [{"id": f"issue-{i}"} for i in range(100)], "total": 101}),
                  response({"issues": [{"id": "issue-100"}], "total": 102})])
    monkeypatch.setattr(client, "_request", lambda *a, **kw: next(pages))
    result = client.list_all_issues(BASE, "fixture-pat", "workspace-a")
    assert not result.complete and result.reason == "issue_snapshot_changed"


@pytest.mark.parametrize("size", [0, 101])
def test_issue_scan_verifies_all_pages_with_stable_order(monkeypatch, size):
    issues = [{"id": f"issue-{i}", "created_at": "2026-11-14T00:00:00Z"} for i in range(size)]
    calls = []

    def request(method, url, **kw):
        params = kw["params"]
        calls.append(params)
        assert method == "GET" and url == BASE + "/api/issues"
        assert params["sort"] == "created_at" and params["direction"] == "desc"
        offset = int(params["offset"])
        return response({"issues": issues[offset:offset + 100], "total": size})

    monkeypatch.setattr(client, "_request", request)
    result = client.list_all_issues(BASE, "fixture-pat", "workspace-a")
    assert result.complete and result.items == issues
    assert result.cursor == {"offset": size, "strategy": "verified_full_reread"}
    assert [int(call["offset"]) for call in calls] == ([0, 100, 0, 100] if size else [0, 0])


@pytest.mark.parametrize("change", ["identity", "content", "order"])
def test_equal_total_issue_changes_are_incomplete(monkeypatch, change):
    original = [{"id": "a", "title": "before"}, {"id": "b", "title": "before"}]
    changed = {
        "identity": [{"id": "a", "title": "before"}, {"id": "c", "title": "before"}],
        "content": [{"id": "a", "title": "after"}, {"id": "b", "title": "before"}],
        "order": list(reversed(original)),
    }[change]
    pages = iter([response({"issues": original, "total": 2}), response({"issues": changed, "total": 2})])
    monkeypatch.setattr(client, "_request", lambda *a, **kw: next(pages))
    result = client.list_all_issues(BASE, "fixture-pat", "workspace-a")
    assert not result.complete and result.reason == "issue_snapshot_changed"
    assert result.items == changed
