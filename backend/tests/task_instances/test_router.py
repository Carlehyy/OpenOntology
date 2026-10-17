"""任务实例 API 契约测试（/api/v2/task-instances，admin 全链路 + 越权）。"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app.task_instances.models import TaskTemplate, TaskTemplateRevision
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

    def test_preset_metadata_and_node_count(self, client, auth_headers, db):
        """is_preset / node_count：用户自建为 False + 节点数来自编译产物。"""
        template = _create_template(client, auth_headers)
        assert template["is_preset"] is False
        detail = client.get(f"{BASE}/templates/{template['id']}",
                            headers=auth_headers).json()["data"]
        compiled_nodes = detail["spec_compiled"]["nodes"]
        listed = client.get(f"{BASE}/templates",
                            headers=auth_headers).json()["data"]
        entry = next(t for t in listed if t["id"] == template["id"])
        assert entry["node_count"] == len(compiled_nodes)
        # 种子预置标记：直接落库 is_preset=True 的模板在列表原样返回；
        # 无版本的模板 node_count 为 None（前端隐藏「N 节点」徽章）
        preset = TaskTemplate(name="预设占位", description="占位", is_preset=True)
        db.add(preset)
        db.commit()
        listed_after = client.get(f"{BASE}/templates",
                                  headers=auth_headers).json()["data"]
        preset_entry = next(t for t in listed_after if t["id"] == preset.id)
        assert preset_entry["is_preset"] is True
        assert preset_entry["node_count"] is None

        # 存量无编译产物的 revision：详情惰性补算时同步回填 node_count，
        # 列表维持 None（不为性能在列表侧编译）——口径行为钉死
        db.query(TaskTemplateRevision).filter(
            TaskTemplateRevision.template_id == template["id"]
        ).update({"spec_compiled": None})
        db.commit()
        detail_after = client.get(f"{BASE}/templates/{template['id']}",
                                  headers=auth_headers).json()["data"]
        assert detail_after["spec_compiled"] is not None
        assert detail_after["node_count"] == len(compiled_nodes)
        listed_legacy = client.get(f"{BASE}/templates",
                                   headers=auth_headers).json()["data"]
        legacy_entry = next(t for t in listed_legacy if t["id"] == template["id"])
        assert legacy_entry["node_count"] is None


    def test_preset_templates_reject_update_and_delete(
            self, client, auth_headers, db, inline_dispatch):
        """预置模板写入保护（YIYA-44/45）：PUT/DELETE 在任何写入前 403
        `TEMPLATE_IS_PRESET`（权限先于编译校验，合法/非法 YAML 均 403），
        内容/版本/deleted_at 不变；不存在/已软删仍 404；读取/激活不受影响。"""
        preset = TaskTemplate(name="预置评审流", description="官方预置",
                              is_preset=True)
        db.add(preset)
        db.flush()
        revision = TaskTemplateRevision(
            template_id=preset.id, revision_no=1,
            spec_yaml=make_spec(), canonical_hash="d" * 64)
        db.add(revision)
        db.flush()
        preset.latest_revision_id = revision.id
        db.commit()
        template_id = preset.id

        def snapshot() -> tuple:
            """逐次拒绝后的完整状态快照：字段、指针、版本集、软删标记"""
            db.refresh(preset)
            db.refresh(revision)
            revs = db.query(TaskTemplateRevision).filter(
                TaskTemplateRevision.template_id == template_id
            ).order_by(TaskTemplateRevision.revision_no).all()
            return (preset.name, preset.description, preset.latest_revision_id,
                    preset.deleted_at, tuple((r.revision_no, r.spec_yaml,
                                              r.canonical_hash) for r in revs))

        baseline = snapshot()

        # 1) PUT 预置（合法 YAML）→ 403，且本次拒绝后状态与基线逐项一致
        updated = client.put(
            f"{BASE}/templates/{template_id}",
            json={"spec_yaml": make_spec(corrections=9)},
            headers=auth_headers)
        assert updated.status_code == 403, updated.text
        assert updated.json()["detail"]["code"] == "TEMPLATE_IS_PRESET"
        assert snapshot() == baseline

        # 2) PUT 预置（非法 YAML）：权限拒绝先于编译校验，仍 403 而非 400
        illegal = client.put(
            f"{BASE}/templates/{template_id}",
            json={"spec_yaml": "not: [valid: workflow"},
            headers=auth_headers)
        assert illegal.status_code == 403, illegal.text
        assert illegal.json()["detail"]["code"] == "TEMPLATE_IS_PRESET"
        assert snapshot() == baseline

        # 3) DELETE 预置 → 403，状态仍逐项一致（含 deleted_at 为 NULL）
        deleted = client.delete(f"{BASE}/templates/{template_id}",
                                headers=auth_headers)
        assert deleted.status_code == 403, deleted.text
        assert deleted.json()["detail"]["code"] == "TEMPLATE_IS_PRESET"
        assert snapshot() == baseline
        assert preset.deleted_at is None

        # 不存在 / 已软删（含已软删预置）优先 404，不被 403 遮盖
        missing = client.put(f"{BASE}/templates/nonexistent",
                             json={"spec_yaml": make_spec()},
                             headers=auth_headers)
        assert missing.status_code == 404, missing.text
        assert client.delete(f"{BASE}/templates/nonexistent",
                             headers=auth_headers).status_code == 404
        preset.deleted_at = datetime(2026, 10, 17, tzinfo=timezone.utc)
        db.commit()
        assert client.put(f"{BASE}/templates/{template_id}",
                          json={"spec_yaml": make_spec()},
                          headers=auth_headers).status_code == 404
        assert client.delete(f"{BASE}/templates/{template_id}",
                             headers=auth_headers).status_code == 404
        preset.deleted_at = None
        db.commit()

        # 读取、版本查询与激活不受保护影响
        detail = client.get(f"{BASE}/templates/{template_id}",
                            headers=auth_headers).json()["data"]
        assert detail["is_preset"] is True
        assert detail["spec_yaml"] == make_spec()
        history = client.get(f"{BASE}/templates/{template_id}/revisions",
                             headers=auth_headers).json()["data"]
        assert len(history) == 1
        listed = client.get(f"{BASE}/templates",
                            headers=auth_headers).json()["data"]
        assert any(t["id"] == template_id for t in listed)
        instance = _activate(client, auth_headers, template_id,
                             key="preset-act")
        assert instance["template_revision_id"] == revision.id

    def test_legacy_null_compiled_backfilled_on_detail(
            self, client, auth_headers, db):
        # 存量模板（seed/早期创建）revision.spec_compiled 为空：
        # 详情读取时惰性补算，不落库
        template = TaskTemplate(name="legacy-tpl")
        db.add(template)
        db.flush()
        revision = TaskTemplateRevision(
            template_id=template.id, revision_no=1,
            spec_yaml=make_spec(), spec_compiled=None,
            canonical_hash="c" * 64)
        db.add(revision)
        db.flush()
        template.latest_revision_id = revision.id
        db.commit()
        detail = client.get(f"{BASE}/templates/{template.id}",
                            headers=auth_headers).json()["data"]
        assert detail["spec_compiled"] is not None
        assert detail["spec_compiled"].get("nodes"), "编译产物应含节点结构"
        db.refresh(revision)
        assert revision.spec_compiled is None, "读取补算不得写脏库内数据"

    def test_legacy_invalid_yaml_degrades_to_none(
            self, client, auth_headers, db):
        # 无效存量 YAML：补算失败维持 None，前端空态降级，GET 不 5xx
        template = TaskTemplate(name="broken-tpl")
        db.add(template)
        db.flush()
        revision = TaskTemplateRevision(
            template_id=template.id, revision_no=1,
            spec_yaml="not: [valid: workflow", spec_compiled=None,
            canonical_hash="d" * 64)
        db.add(revision)
        db.flush()
        template.latest_revision_id = revision.id
        db.commit()
        detail = client.get(f"{BASE}/templates/{template.id}",
                            headers=auth_headers).json()["data"]
        assert detail["spec_compiled"] is None

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
        assert response.json()["detail"]["code"] == "MENU_ACCESS_DENIED"
        # 菜单守卫包整个 router：无 task_instances 菜单角色对写操作同样 403，
        # 且先于任何模板级判定（不泄露模板存在性）
        assert client.put(f"{BASE}/templates/any-id",
                          json={"spec_yaml": make_spec()},
                          headers=editor_headers).status_code == 403
        assert client.delete(f"{BASE}/templates/any-id",
                             headers=editor_headers).status_code == 403


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
