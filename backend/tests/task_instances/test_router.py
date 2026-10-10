"""任务实例 API 契约测试（/api/v2/task-instances，admin 全链路 + 越权）。"""
from __future__ import annotations

import json

from tests.task_instances.conftest import make_spec

BASE = "/api/v2/task-instances"


def _create_template(client, headers, spec_yaml=None) -> dict:
    response = client.post(
        f"{BASE}/templates",
        json={"spec_yaml": spec_yaml or make_spec()},
        headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _activate(client, headers, template_id: str, *, inputs=None,
              key: str = "idem-1") -> dict:
    response = client.post(
        f"{BASE}/templates/{template_id}/instances",
        json={"name": "实例A", "goal": "跑通", "inputs": inputs or {}},
        headers={**headers, "Idempotency-Key": key})
    assert response.status_code == 202, response.text
    return response.json()["data"]


class TestTemplates:
    def test_crud_and_revisions(self, client, auth_headers):
        template = _create_template(client, auth_headers)
        assert template["latest_revision_no"] == 1
        detail = client.get(f"{BASE}/templates/{template['id']}",
                            headers=auth_headers).json()["data"]
        assert "spec_yaml" in detail
        # 内容变化 → 新 revision
        updated = client.put(
            f"{BASE}/templates/{template['id']}",
            json={"spec_yaml": make_spec(corrections=3)},
            headers=auth_headers).json()["data"]
        assert updated["latest_revision_no"] == 2
        # 内容不变 → 幂等命中既有 revision
        again = client.put(
            f"{BASE}/templates/{template['id']}",
            json={"spec_yaml": make_spec(corrections=3)},
            headers=auth_headers).json()["data"]
        assert again["latest_revision_no"] == 2
        revisions = client.get(
            f"{BASE}/templates/{template['id']}/revisions",
            headers=auth_headers).json()["data"]
        assert len(revisions) == 2
        listed = client.get(f"{BASE}/templates",
                            headers=auth_headers).json()["data"]
        assert any(t["id"] == template["id"] for t in listed)
        deleted = client.delete(f"{BASE}/templates/{template['id']}",
                                headers=auth_headers).json()["data"]
        assert deleted["status"] == "deleted"
        assert client.get(f"{BASE}/templates/{template['id']}",
                          headers=auth_headers).status_code == 404

    def test_validate_and_invalid_spec(self, client, auth_headers):
        ok = client.post(f"{BASE}/templates/validate",
                         json={"spec_yaml": make_spec()},
                         headers=auth_headers).json()["data"]
        assert ok["valid"] is True
        bad = make_spec().replace("from: gate.approved", "from: gate.noport")
        response = client.post(f"{BASE}/templates",
                               json={"spec_yaml": bad},
                               headers=auth_headers)
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert detail["code"] == "TEMPLATE_INVALID"

    def test_editor_without_menu_key_forbidden(self, client, db, editor_user):
        token_response = client.post(
            "/api/v1/auth/login",
            json={"username": "editor", "password": "editor123"})
        editor_headers = {
            "Authorization": f"Bearer {token_response.json()['data']['access_token']}"}
        response = client.get(f"{BASE}/templates", headers=editor_headers)
        assert response.status_code == 403


class TestInstances:
    def test_full_frontend_flow(self, client, auth_headers, inline_dispatch):
        template = _create_template(client, auth_headers)
        data = _activate(client, auth_headers, template["id"], inputs={
            "__simulate": {"outputs": {
                "analyze": {"category": "frontend", "summary": "登录页"}}}})
        instance_id = data["id"]
        # 幂等键复用
        again = _activate(client, auth_headers, template["id"], key="idem-1")
        assert again["id"] == instance_id
        detail = client.get(f"{BASE}/instances/{instance_id}",
                            headers=auth_headers).json()["data"]
        human = next(n for n in detail["nodes"]
                     if n["node_id"] == "human_review")
        assert human["status"] == "waiting_human"
        # 人工交活 → 关口审批
        submitted = client.post(
            f"{BASE}/instances/{instance_id}/nodes/human_review/human/submit",
            json={"output": {"summary": "评审通过"}},
            headers=auth_headers)
        assert submitted.status_code == 200, submitted.text
        detail = submitted.json()["data"]
        approval = detail["approvals"][-1]
        assert approval["status"] == "pending"
        # 拒绝必须带理由
        no_reason = client.post(
            f"{BASE}/instances/{instance_id}/nodes/gate/approvals/"
            f"{approval['id']}/decision",
            json={"decision": "rejected", "reason": "  "},
            headers=auth_headers)
        assert no_reason.status_code == 400
        # 批准 → 完成
        approved = client.post(
            f"{BASE}/instances/{instance_id}/nodes/gate/approvals/"
            f"{approval['id']}/decision",
            json={"decision": "approved"},
            headers=auth_headers)
        assert approved.status_code == 200
        assert approved.json()["data"]["status"] == "completed"

    def test_reject_rework_and_budget_409(
            self, client, auth_headers, inline_dispatch):
        template = _create_template(client, auth_headers)
        data = _activate(client, auth_headers, template["id"], key="rw", inputs={
            "__simulate": {"outputs": {
                "analyze": {"category": "defect_fix", "summary": "崩溃"}}}})
        instance_id = data["id"]
        for round_no in range(3):
            detail = client.get(f"{BASE}/instances/{instance_id}",
                                headers=auth_headers).json()["data"]
            approval = next(a for a in detail["approvals"]
                            if a["status"] == "pending")
            response = client.post(
                f"{BASE}/instances/{instance_id}/nodes/gate/approvals/"
                f"{approval['id']}/decision",
                json={"decision": "rejected", "reason": f"第{round_no}轮"},
                headers=auth_headers)
            assert response.status_code == 200, response.text
        detail = client.get(f"{BASE}/instances/{instance_id}",
                            headers=auth_headers).json()["data"]
        approval = next(a for a in detail["approvals"]
                        if a["status"] == "pending")
        exhausted = client.post(
            f"{BASE}/instances/{instance_id}/nodes/gate/approvals/"
            f"{approval['id']}/decision",
            json={"decision": "rejected", "reason": "超预算"},
            headers=auth_headers)
        assert exhausted.status_code == 409
        assert exhausted.json()["detail"]["code"] == "REWORK_BUDGET_EXHAUSTED"

    def test_human_reject_endpoint(self, client, auth_headers, inline_dispatch):
        template = _create_template(client, auth_headers)
        data = _activate(client, auth_headers, template["id"], key="hr", inputs={
            "__simulate": {"outputs": {
                "analyze": {"category": "frontend", "summary": "初次"}}}})
        instance_id = data["id"]
        response = client.post(
            f"{BASE}/instances/{instance_id}/nodes/human_review/reject",
            json={"reason": "摘要不完整"},
            headers=auth_headers)
        assert response.status_code == 200, response.text
        detail = response.json()["data"]
        analyze_runs = [n for n in detail["nodes"]
                        if n["node_id"] == "analyze"]
        assert analyze_runs[-1]["rework_count"] == 1
        human = next(n for n in detail["nodes"]
                     if n["node_id"] == "human_review")
        assert human["status"] == "waiting_human"  # 重开等待再审

    def test_cancel_endpoint(self, client, auth_headers, inline_dispatch):
        template = _create_template(client, auth_headers)
        data = _activate(client, auth_headers, template["id"], key="cx", inputs={
            "__simulate": {"outputs": {
                "analyze": {"category": "frontend", "summary": "s"}}}})
        response = client.post(
            f"{BASE}/instances/{data['id']}/cancel",
            json={"reason": "不需要了"},
            headers=auth_headers)
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "cancelled"

    def test_missing_idempotency_key_rejected(self, client, auth_headers):
        template = _create_template(client, auth_headers)
        response = client.post(
            f"{BASE}/templates/{template['id']}/instances",
            json={"name": "x", "goal": "y"},
            headers=auth_headers)
        assert response.status_code == 422  # FastAPI 必填 Header 校验

    def test_instance_list_filters(self, client, auth_headers, inline_dispatch):
        template = _create_template(client, auth_headers)
        _activate(client, auth_headers, template["id"], key="l1")
        listed = client.get(f"{BASE}/instances?status=active",
                            headers=auth_headers).json()["data"]
        assert listed["total"] >= 1
        mine = client.get(f"{BASE}/instances?mine=true",
                          headers=auth_headers).json()["data"]
        assert mine["total"] >= 1


class TestSteering:
    def test_steering_on_running_node_accepted(
            self, client, auth_headers, inline_dispatch):
        template = _create_template(client, auth_headers)
        data = _activate(client, auth_headers, template["id"], key="st", inputs={
            "__simulate": {"hold_nodes": ["analyze"],
                           "outputs": {"analyze": {
                               "category": "frontend", "summary": "s"}}}})
        response = client.post(
            f"{BASE}/instances/{data['id']}/steering",
            json={"node_id": "analyze", "content": "补充要求：关注兼容性"},
            headers={**auth_headers, "Idempotency-Key": "steer-1"})
        assert response.status_code == 202, response.text
        # 幂等重发命中同一条消息
        again = client.post(
            f"{BASE}/instances/{data['id']}/steering",
            json={"node_id": "analyze", "content": "补充要求：关注兼容性"},
            headers={**auth_headers, "Idempotency-Key": "steer-1"})
        assert again.json()["data"]["id"] == response.json()["data"]["id"]

    def test_steering_on_waiting_node_409(
            self, client, auth_headers, inline_dispatch):
        template = _create_template(client, auth_headers)
        data = _activate(client, auth_headers, template["id"], key="sw", inputs={
            "__simulate": {"outputs": {
                "analyze": {"category": "frontend", "summary": "s"}}}})
        response = client.post(
            f"{BASE}/instances/{data['id']}/steering",
            json={"node_id": "human_review", "content": "插话"},
            headers=auth_headers)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "NODE_NOT_RUNNING"


class TestSSE:
    def test_terminal_instance_stream_completes(
            self, client, auth_headers, inline_dispatch, monkeypatch):
        # SSE 轮询默认用 app 库；测试把读批会话换绑到夹具库
        from app.task_instances import router as ti_router
        from tests.conftest import TestSession

        monkeypatch.setattr(ti_router, "SessionLocal", TestSession)
        template = _create_template(client, auth_headers)
        data = _activate(client, auth_headers, template["id"], key="ss", inputs={
            "__simulate": {"outputs": {
                "analyze": {"category": "defect_fix", "summary": "s"}}}})
        instance_id = data["id"]
        detail = client.get(f"{BASE}/instances/{instance_id}",
                            headers=auth_headers).json()["data"]
        approval = next(a for a in detail["approvals"]
                        if a["status"] == "pending")
        client.post(
            f"{BASE}/instances/{instance_id}/nodes/gate/approvals/"
            f"{approval['id']}/decision",
            json={"decision": "approved"},
            headers=auth_headers)
        chunks: list[str] = []
        with client.stream(
                "GET", f"{BASE}/instances/{instance_id}/events",
                headers=auth_headers) as response:
            assert response.status_code == 200
            for line in response.iter_lines():
                chunks.append(line)
                if len(chunks) > 200:
                    break
        text = "\n".join(chunks)
        assert "event: instance.snapshot" in text
        assert "instance.terminal" in text
