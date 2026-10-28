"""Bounded authoritative discovery, independent of asynchronous graph mirrors."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time

from sqlalchemy import func, or_, select

from app.auth.permissions import user_has_menu_access
from app.ontologies.access import require_ontology_access
from app.ontologies.agent_runtime.models import AgentProfile
from app.ontologies.agent_runtime.structure_service import StructureError
from app.ontologies.projects.models import OntologyProject
from app.ontologies.release_context import current_release_context
from app.ontologies.versions.models import OntologyVersion
from app.shared.config import settings

PAGE_SIZE = 20
DOCUMENT_PAGE_CHARS = 8000


def _authorize(db, user):
    if (not user or not getattr(user, "is_active", False)
            or not user_has_menu_access(db, user, "agent")):
        raise StructureError(403, "MENU_ACCESS_DENIED", "当前用户无本体助手发现权限")


def _sign(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    signature = hmac.new(settings.secret_key.encode(), raw, hashlib.sha256).hexdigest()
    return body + "." + signature


def _decode(token, user):
    try:
        body, signature = token.rsplit(".", 1)
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        expected = hmac.new(settings.secret_key.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError()
        if payload.get("owner") != str(user.id) or payload.get("v") != 1:
            raise ValueError()
        if type(payload.get("exp")) is not int or payload["exp"] <= time.time():
            raise StructureError(410, "catalog_cursor_expired", "目录游标已过期，请重新检索")
        return payload
    except (ValueError, TypeError, KeyError, binascii.Error, UnicodeError) as exc:
        raise StructureError(400, "invalid_catalog_cursor", "目录游标无效") from exc


def catalog_search(db, user, *, query=None, cursor=None):
    _authorize(db, user)
    after = ""
    text = str(query or "").strip()
    if len(text) > 200:
        raise StructureError(422, "catalog_query_too_long", "目录检索词最多 200 字符")
    if cursor:
        payload = _decode(cursor, user)
        if payload.get("kind") != "catalog":
            raise StructureError(400, "invalid_catalog_cursor", "游标用途不匹配")
        if query is not None and text != payload.get("query"):
            raise StructureError(400, "invalid_catalog_cursor", "游标检索条件已变化")
        text = payload["query"]
        after = payload["after"]
    statement = (select(OntologyProject, OntologyVersion, AgentProfile)
                 .join(OntologyVersion, (OntologyVersion.id == OntologyProject.current_release_id)
                       & (OntologyVersion.ontology_id == OntologyProject.id))
                 .outerjoin(AgentProfile, AgentProfile.ontology_id == OntologyProject.id)
                 .where(OntologyVersion.node_kind == "release",
                        OntologyVersion.lifecycle_status == "released",
                        or_(AgentProfile.id.is_(None), AgentProfile.enabled.is_(True)),
                        OntologyProject.id > after))
    if text:
        lowered = text.lower()
        statement = statement.where(or_(
            func.lower(OntologyProject.name).contains(lowered, autoescape=True),
            func.lower(OntologyProject.description).contains(lowered, autoescape=True)))
    rows = db.execute(statement.order_by(OntologyProject.id).limit(PAGE_SIZE + 1)).all()
    items = []
    for project, release, profile in rows[:PAGE_SIZE]:
        require_ontology_access(db, project.id, user, write=False)
        snapshot = release.snapshot_formal or {}
        allowed = None if profile is None else profile.allowed_object_type_ids
        types = [item for item in snapshot.get("objectTypes", [])
                 if allowed is None or item.get("id") in allowed]
        semantic = release.snapshot_semantic or {}
        has_document = bool(str(semantic.get("documentMd") or "").strip())
        items.append({"ontologyId": project.id, "releaseId": release.id,
                      "name": project.name, "title": semantic.get("documentTitle") or project.name,
                      "objectTypeCount": len(types), "hasDocumentMd": has_document,
                      "objectTypes": [{"id": item["id"], "name": item.get("name"),
                                       "displayName": item.get("displayName")}
                                      for item in types[:10]],
                      "objectTypesHasNext": len(types) > 10,
                      "objectTypesOperation": "object_types",
                      "structureRequired": True,
                      "sourceStatus": "current", "catalogConsistency": "live_keyset"})
    more = len(rows) > PAGE_SIZE
    next_cursor = _sign({"v": 1, "kind": "catalog", "owner": str(user.id),
                         "query": text, "after": rows[PAGE_SIZE - 1][0].id,
                         "exp": int(time.time()) + 300}) if more else None
    return {"items": items, "hasNext": more, "cursor": next_cursor,
            "authority": "current_release_pointer"}


def catalog_object_types(db, user, *, ontology_id, release_id=None, cursor=None):
    _authorize(db, user)
    after = ""
    payload = None
    if cursor:
        payload = _decode(cursor, user)
        if payload.get("kind") != "object_types" or payload.get("ontology") != ontology_id:
            raise StructureError(400, "invalid_catalog_cursor", "对象目录游标用途不匹配")
        if release_id is not None and release_id != payload.get("release"):
            raise StructureError(400, "invalid_catalog_cursor", "对象目录发布范围已变化")
        release_id = payload["release"]
        after = payload["after"]
    require_ontology_access(db, ontology_id, user, write=False)
    context = current_release_context(db, ontology_id, expected_release_id=release_id)
    profile = db.scalar(select(AgentProfile).where(AgentProfile.ontology_id == ontology_id)
                        .execution_options(populate_existing=True))
    if profile is not None and not profile.enabled:
        raise StructureError(403, "profile_disabled", "该本体助手未启用")
    allowed = None if profile is None else profile.allowed_object_type_ids
    scope_hash = hashlib.sha256(json.dumps(allowed, sort_keys=True).encode()).hexdigest()
    if payload is not None and payload.get("scope") != scope_hash:
        raise StructureError(409, "catalog_context_changed", "对象授权范围已变化，请重新检索")
    types = sorted((item for item in context.snapshot.get("objectTypes", [])
                    if isinstance(item, dict) and item.get("id")
                    and (allowed is None or item["id"] in allowed)), key=lambda item: item["id"])
    page = [item for item in types if item["id"] > after][:PAGE_SIZE + 1]
    more = len(page) > PAGE_SIZE
    next_cursor = _sign({"v": 1, "kind": "object_types", "owner": str(user.id),
                         "ontology": ontology_id, "release": context.release.id,
                         "scope": scope_hash, "after": page[PAGE_SIZE - 1]["id"],
                         "exp": int(time.time()) + 300}) if more else None
    return {"ontologyId": ontology_id, "releaseId": context.release.id,
            "items": [{"id": item["id"], "name": item.get("name"),
                       "displayName": item.get("displayName")} for item in page[:PAGE_SIZE]],
            "objectTypeCount": len(types), "hasNext": more, "cursor": next_cursor,
            "structureRequired": True, "authority": "current_release_pointer"}


def catalog_get_document(db, user, *, ontology_id, release_id=None, cursor=None):
    _authorize(db, user)
    offset = 0
    payload = None
    if cursor:
        payload = _decode(cursor, user)
        if payload.get("kind") != "document" or payload.get("ontology") != ontology_id:
            raise StructureError(400, "invalid_catalog_cursor", "文档游标用途不匹配")
        if release_id is not None and release_id != payload.get("release"):
            raise StructureError(400, "invalid_catalog_cursor", "文档发布范围已变化")
        release_id = payload["release"]
        offset = payload["offset"]
        if type(offset) is not int or offset < 0:
            raise StructureError(400, "invalid_catalog_cursor", "文档位置无效")
    require_ontology_access(db, ontology_id, user, write=False)
    context = current_release_context(db, ontology_id, expected_release_id=release_id)
    profile = db.scalar(select(AgentProfile).where(AgentProfile.ontology_id == ontology_id)
                        .execution_options(populate_existing=True))
    if profile is not None and not profile.enabled:
        raise StructureError(403, "profile_disabled", "该本体助手未启用")
    semantic = context.release.snapshot_semantic or {}
    document = str(semantic.get("documentMd") or "").strip()
    if not document:
        raise StructureError(404, "no_document_md", "该发布版本尚未提供业务文档，请使用权威结构发现")
    fingerprint = hashlib.sha256(document.encode()).hexdigest()
    if payload is not None and payload.get("fingerprint") != fingerprint:
        raise StructureError(409, "document_context_changed", "业务文档已更新，请从第一页重新读取")
    end = min(offset + DOCUMENT_PAGE_CHARS, len(document))
    more = end < len(document)
    next_cursor = _sign({"v": 1, "kind": "document", "owner": str(user.id),
                         "ontology": ontology_id, "release": context.release.id,
                         "fingerprint": fingerprint, "offset": end,
                         "exp": int(time.time()) + 300}) if more else None
    return {"ontologyId": ontology_id, "releaseId": context.release.id,
            "title": semantic.get("documentTitle") or context.project.name,
            "documentMd": document[offset:end], "fingerprint": fingerprint,
            "offset": offset, "totalChars": len(document), "hasNext": more,
            "cursor": next_cursor, "sourceStatus": "current"}
