"""哨兵 Skill 导出端点回归：包契约、保真与标准包 round-trip。"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest


def _fo(ontology_id: str) -> str:
    return f"/api/v2/formal/ontologies/{ontology_id}"


_DOCUMENT_MD = "# 订单业务文档\n\n监测待支付订单并自动标记已支付。\n"


@pytest.fixture
def skill_runtime(client, auth_headers, ontology, db):
    """当前发布快照内含一条带动作的公共哨兵，并带语义层业务文档。"""
    ontology_id = ontology["id"]
    release_id = ontology["current_release_id"]
    response = client.put(
        f"{_fo(ontology_id)}/full",
        headers=auth_headers,
        json={
            "objectTypes": [{
                "id": "ot-order", "name": "Order", "displayName": "订单",
                "primaryKey": "order_no", "positionX": 0, "positionY": 0,
                "properties": [
                    {"id": "p-order-no", "name": "order_no",
                     "displayName": "订单号", "type": "string", "required": True},
                    {"id": "p-status", "name": "status",
                     "displayName": "状态", "type": "string", "required": False},
                ],
            }],
            "linkTypes": [],
            "actions": [{
                "id": "act-mark-paid", "name": "mark_paid",
                "displayName": "标记已支付", "objectTypeId": "ot-order",
                "parameters": [], "requiresApproval": False, "rules": [],
            }],
            "functions": [],
            "instances": [],
            "linkInstances": [],
        },
    )
    assert response.status_code == 200, response.text

    from app.models.ontology_version import OntologyVersion
    from app.models.sentinel import Sentinel
    from app.ontologies.versions.release_service import (
        collect_publishable_snapshot,
    )
    from app.ontologies.versions.snapshot_contract import snapshot_hash

    db.add(Sentinel(
        id="sentinel-builtin", ontology_id=ontology_id,
        name="builtin_watch", display_name="发布内置哨兵",
        description="监测待支付订单",
        bindings=[{"alias": "o", "objectTypeId": "ot-order", "filter": None}],
        links=[], condition="o.status == 'pending'", primary_alias="o",
        action_ids=["act-mark-paid"],
        action_parameters={
            "act-mark-paid": {"note": "由哨兵自动处理", "snapshot": "{{o.status}}"},
        },
        on_change=True, on_schedule=False, scan_interval_seconds=300,
        trigger_mode="on_enter", enabled=True, status="published",
        origin="release_builtin",
    ))
    db.flush()
    snapshot = collect_publishable_snapshot(db, ontology_id)
    release = db.query(OntologyVersion).filter_by(id=release_id).one()
    release.snapshot_formal = snapshot
    release.snapshot_hash = snapshot_hash(snapshot)
    release.snapshot_semantic = {
        "documentMd": _DOCUMENT_MD,
        "documentTitle": "订单业务文档",
        "documentFingerprint": hashlib.sha256(
            _DOCUMENT_MD.encode("utf-8")).hexdigest(),
    }
    db.commit()
    return {"ontology_id": ontology_id, "release_id": release_id}


def _export(client, auth_headers, ontology_id, sentinel_id):
    return client.get(
        f"/api/v1/ontologies/{ontology_id}/sentinels/{sentinel_id}/export-skill",
        headers=auth_headers,
    )


def _open_zip(response) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(response.content))


def _create_dynamic(client, auth_headers, runtime):
    response = client.post(
        f"{_fo(runtime['ontology_id'])}/agent/dynamic-sentinels",
        headers=auth_headers,
        json={
            "releaseId": runtime["release_id"],
            "definition": {
                "name": "assistant_pending_order",
                "displayName": "待支付订单动态哨兵",
                "description": "由智能助手管理",
                "bindings": [
                    {"alias": "o", "objectTypeId": "ot-order", "filter": None},
                ],
                "links": [],
                "condition": "o.status == 'pending'",
                "conditionRows": [],
                "conditionLogic": "and",
                "primaryAlias": "o",
                "actionIds": [],
                "actionParameters": {},
                "onChange": True, "onSchedule": False,
                "scanIntervalSeconds": 300, "triggerMode": "on_enter",
                "muted": False,
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_builtin_export_round_trips_standard_skill_package(
    client, auth_headers, skill_runtime, tmp_path,
):
    from app.super_assistant.skill_store import (
        import_skill_archive,
        parse_skill_markdown,
    )

    response = _export(
        client, auth_headers, skill_runtime["ontology_id"], "sentinel-builtin",
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/zip")
    assert response.headers["x-content-type-options"] == "nosniff"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "filename*=UTF-8''" in disposition

    archive = _open_zip(response)
    assert set(archive.namelist()) == {
        "SKILL.md",
        "references/sentinel-definition.json",
        "references/business-doc.md",
    }

    markdown = archive.read("SKILL.md").decode("utf-8")
    parsed = parse_skill_markdown(markdown)
    assert parsed["name"] == "builtin-watch"
    assert "测试本体" in parsed["description"]
    assert "发布内置哨兵" in parsed["description"]
    assert "o.status == 'pending'" in parsed["content"]
    assert "标记已支付" in parsed["content"]
    assert "由哨兵自动处理" in parsed["content"]
    assert "{{o.status}}" in parsed["content"]
    assert "能力边界" in parsed["content"]

    package = json.loads(
        archive.read("references/sentinel-definition.json").decode("utf-8")
    )
    assert package["schema"] == "openontology.sentinel-skill/v1"
    assert package["ontology"] == {
        "id": skill_runtime["ontology_id"], "name": "测试本体",
    }
    assert package["sentinel"]["id"] == "sentinel-builtin"
    assert package["sentinel"]["origin"] == "release_builtin"
    assert package["sentinel"]["condition"] == "o.status == 'pending'"
    assert package["sentinel"]["enabled"] is True
    assert package["actions"][0]["id"] == "act-mark-paid"
    assert package["actions"][0]["available"] is True
    assert package["actions"][0]["requiresApproval"] is False

    document = archive.read("references/business-doc.md").decode("utf-8")
    assert "# 订单业务文档" in document
    assert _DOCUMENT_MD.strip() in document

    folder = tmp_path / "imported-skill"
    import_skill_archive(response.content, folder)
    assert (folder / "SKILL.md").is_file()
    assert (folder / "references" / "sentinel-definition.json").is_file()
    assert (folder / "references" / "business-doc.md").is_file()

    # 固定条目顺序与时间戳：同输入产出同字节。
    repeat = _export(
        client, auth_headers, skill_runtime["ontology_id"], "sentinel-builtin",
    )
    assert repeat.content == response.content


def test_dynamic_sentinel_export_uses_live_definition(
    client, auth_headers, skill_runtime,
):
    from app.super_assistant.skill_store import parse_skill_markdown

    dynamic = _create_dynamic(client, auth_headers, skill_runtime)
    response = _export(
        client, auth_headers, skill_runtime["ontology_id"], dynamic["id"],
    )
    assert response.status_code == 200, response.text

    archive = _open_zip(response)
    package = json.loads(
        archive.read("references/sentinel-definition.json").decode("utf-8")
    )
    assert package["sentinel"]["id"] == dynamic["id"]
    assert package["sentinel"]["origin"] == "assistant_dynamic"
    assert package["sentinel"]["name"] == "assistant_pending_order"
    assert package["sentinel"]["enabled"] is False
    assert package["actions"] == []

    parsed = parse_skill_markdown(archive.read("SKILL.md").decode("utf-8"))
    assert parsed["name"] == "assistant-pending-order"
    assert "待支付订单动态哨兵" in parsed["description"]
    assert "动态哨兵" in parsed["content"]
    assert "无处置动作" in parsed["content"]


def test_export_without_business_document_omits_reference(
    client, auth_headers, skill_runtime, db,
):
    from app.models.ontology_version import OntologyVersion

    release = db.query(OntologyVersion).filter_by(
        id=skill_runtime["release_id"],
    ).one()
    release.snapshot_semantic = None
    db.commit()

    response = _export(
        client, auth_headers, skill_runtime["ontology_id"], "sentinel-builtin",
    )
    assert response.status_code == 200, response.text
    archive = _open_zip(response)
    assert "references/business-doc.md" not in archive.namelist()
    markdown = archive.read("SKILL.md").decode("utf-8")
    assert "无业务文档" in markdown


def test_missing_action_reference_is_preserved_not_silently_dropped(
    client, auth_headers, skill_runtime, db,
):
    from app.models.ontology_version import OntologyVersion
    from app.models.sentinel import Sentinel
    from app.ontologies.versions.release_service import (
        collect_publishable_snapshot,
    )
    from app.ontologies.versions.snapshot_contract import snapshot_hash

    row = db.query(Sentinel).filter_by(id="sentinel-builtin").one()
    row.action_ids = ["act-mark-paid", "act-ghost"]
    db.flush()
    snapshot = collect_publishable_snapshot(db, skill_runtime["ontology_id"])
    release = db.query(OntologyVersion).filter_by(
        id=skill_runtime["release_id"],
    ).one()
    release.snapshot_formal = snapshot
    release.snapshot_hash = snapshot_hash(snapshot)
    db.commit()

    response = _export(
        client, auth_headers, skill_runtime["ontology_id"], "sentinel-builtin",
    )
    assert response.status_code == 200, response.text
    archive = _open_zip(response)
    package = json.loads(
        archive.read("references/sentinel-definition.json").decode("utf-8")
    )
    ghost = next(
        item for item in package["actions"] if item["id"] == "act-ghost"
    )
    assert ghost["available"] is False
    markdown = archive.read("SKILL.md").decode("utf-8")
    assert "不可用" in markdown


def test_chinese_technical_name_falls_back_to_stable_slug(
    client, auth_headers, skill_runtime, db,
):
    from app.models.ontology_version import OntologyVersion
    from app.models.sentinel import Sentinel
    from app.ontologies.versions.release_service import (
        collect_publishable_snapshot,
    )
    from app.ontologies.versions.snapshot_contract import snapshot_hash
    from app.super_assistant.skill_store import parse_skill_markdown

    row = db.query(Sentinel).filter_by(id="sentinel-builtin").one()
    row.name = "中文哨兵名"
    db.flush()
    snapshot = collect_publishable_snapshot(db, skill_runtime["ontology_id"])
    release = db.query(OntologyVersion).filter_by(
        id=skill_runtime["release_id"],
    ).one()
    release.snapshot_formal = snapshot
    release.snapshot_hash = snapshot_hash(snapshot)
    db.commit()

    response = _export(
        client, auth_headers, skill_runtime["ontology_id"], "sentinel-builtin",
    )
    assert response.status_code == 200, response.text
    parsed = parse_skill_markdown(
        _open_zip(response).read("SKILL.md").decode("utf-8")
    )
    assert parsed["name"] == "sentinel-sentinel"
    assert "发布内置哨兵" in parsed["description"]


def test_export_unknown_or_foreign_sentinel_returns_404(
    client, auth_headers, skill_runtime,
):
    response = _export(
        client, auth_headers, skill_runtime["ontology_id"], "sentinel-missing",
    )
    assert response.status_code == 404

    other = client.post(
        "/api/v1/ontologies",
        headers=auth_headers,
        json={"name": "另一个本体", "domain": "供应链"},
    )
    assert other.status_code in (200, 201), other.text
    other_id = other.json()["data"]["id"]
    response = _export(client, auth_headers, other_id, "sentinel-builtin")
    assert response.status_code == 404


def test_draft_builtin_not_in_snapshot_is_not_exportable(
    client, auth_headers, skill_runtime, db,
):
    """live 表里的 draft 公共哨兵若未晋级进快照，不得导出（release fence）。"""
    from app.models.sentinel import Sentinel

    db.add(Sentinel(
        id="sentinel-draft", ontology_id=skill_runtime["ontology_id"],
        name="draft_watch", display_name="草稿哨兵",
        bindings=[{"alias": "o", "objectTypeId": "ot-order", "filter": None}],
        links=[], condition=None, primary_alias="o",
        action_ids=[], action_parameters={},
        origin="release_builtin", status="draft",
    ))
    db.commit()

    response = _export(
        client, auth_headers, skill_runtime["ontology_id"], "sentinel-draft",
    )
    assert response.status_code == 404
