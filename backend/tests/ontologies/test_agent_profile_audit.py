"""profile 写入审计（agent_profile_updated）。

profile 授权字段进入远程查询 plan 指纹，查询窗口内的写入会触发
query_context_changed 409；审计记录让偶发 409 可直接查证是否真有并发
配置变更（⑨ 的配置变更日志）。
"""
import pytest

from app.ontologies.agent_runtime import schemas as S
from app.ontologies.agent_runtime import profile_service
from app.ontologies.agent_runtime.boundary import get_or_create_profile
from app.ontologies.inference.models import AuditLog


def _audits(db, ontology_id):
    return (db.query(AuditLog)
            .filter(AuditLog.ontology_id == ontology_id,
                    AuditLog.event_subtype == "agent_profile_updated")
            .all())


def test_update_profile_writes_audit_with_changed_fields(db, admin_user, ontology):
    oid = ontology["id"]
    profile = get_or_create_profile(db, oid)
    assert profile.max_rows_per_query == 50

    profile_service.update_profile(
        db, oid, S.AgentProfileUpdate.model_validate({"maxRowsPerQuery": 200}),
        get_profile_fn=get_or_create_profile,
        actor=admin_user, source="admin_api")

    rows = _audits(db, oid)
    assert len(rows) == 1
    entry = rows[0]
    assert entry.event_type == "edit"
    assert entry.user_id == str(admin_user.id)
    assert entry.meta["source"] == "admin_api"
    assert entry.meta["changedFields"] == ["max_rows_per_query"]
    assert entry.before_state["max_rows_per_query"] == 50
    assert entry.after_state["max_rows_per_query"] == 200


def test_update_profile_without_change_writes_no_audit(db, admin_user, ontology):
    oid = ontology["id"]
    get_or_create_profile(db, oid)
    profile_service.update_profile(
        db, oid, S.AgentProfileUpdate.model_validate({"enabled": True}),
        get_profile_fn=get_or_create_profile, actor=admin_user)
    assert _audits(db, oid) == []


def test_rollback_source_is_recorded(db, admin_user, ontology):
    oid = ontology["id"]
    get_or_create_profile(db, oid)
    profile_service.update_profile(
        db, oid, S.AgentProfileUpdate.model_validate({"maxRowsPerQuery": 10}),
        get_profile_fn=get_or_create_profile,
        source="assistant_evaluation_rollback")
    rows = _audits(db, oid)
    assert rows[0].meta["source"] == "assistant_evaluation_rollback"
