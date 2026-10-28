"""Authoritative discovery must not depend on the graph document mirror."""
import pytest

from app.ontologies.agent_runtime.catalog import (
    catalog_get_document, catalog_object_types, catalog_search,
)
from app.ontologies.agent_runtime.structure_service import StructureError
from app.ontologies.agent_runtime.models import AgentProfile
from app.ontologies.projects.models import OntologyProject
from app.ontologies.versions.models import OntologyVersion


def _user(db):
    from app.auth.models import User
    return db.query(User).filter(User.role == "admin").first()


def test_catalog_lists_current_release_without_business_document(client, auth_headers, ontology, db):
    user = _user(db)
    result = catalog_search(db, user)
    item = next(item for item in result["items"] if item["ontologyId"] == ontology["id"])
    assert item["hasDocumentMd"] is False
    assert item["sourceStatus"] == "current"
    assert item["releaseId"] == db.get(OntologyProject, ontology["id"]).current_release_id
    with pytest.raises(StructureError) as error:
        catalog_get_document(db, user, ontology_id=ontology["id"])
    assert error.value.code == "no_document_md"


def test_document_pages_reject_controlled_revision_change(client, auth_headers, ontology, db):
    user = _user(db)
    project = db.get(OntologyProject, ontology["id"])
    release = db.get(OntologyVersion, project.current_release_id)
    release.snapshot_semantic = {"documentMd": "业务文档" * 3000}
    db.commit()
    first = catalog_get_document(db, user, ontology_id=project.id)
    assert len(first["documentMd"]) == 8000
    assert first["hasNext"]
    release.snapshot_semantic = {"documentMd": "业务文档变更" * 3000}
    db.commit()
    with pytest.raises(StructureError) as error:
        catalog_get_document(db, user, ontology_id=project.id, cursor=first["cursor"])
    assert error.value.code == "document_context_changed"


def test_catalog_rechecks_profile_for_document_access(client, auth_headers, ontology, db):
    user = _user(db)
    profile = db.query(AgentProfile).filter_by(ontology_id=ontology["id"]).first()
    if profile is None:
        profile = AgentProfile(ontology_id=ontology["id"], enabled=False)
        db.add(profile)
    else:
        profile.enabled = False
    db.commit()
    assert all(item["ontologyId"] != ontology["id"] for item in catalog_search(db, user)["items"])
    with pytest.raises(StructureError) as error:
        catalog_get_document(db, user, ontology_id=ontology["id"])
    assert error.value.status == 403


def test_object_type_catalog_pages_entire_authorized_release(client, auth_headers, ontology, db):
    user = _user(db)
    project = db.get(OntologyProject, ontology["id"])
    release = db.get(OntologyVersion, project.current_release_id)
    release.snapshot_formal = {"objectTypes": [
        {"id": f"type-{index:02}", "name": f"Type{index}", "displayName": f"类型{index}"}
        for index in range(25)]}
    db.commit()
    search = catalog_search(db, user)
    item = next(item for item in search["items"] if item["ontologyId"] == project.id)
    assert len(item["objectTypes"]) == 10 and item["objectTypesHasNext"]
    first = catalog_object_types(db, user, ontology_id=project.id)
    second = catalog_object_types(db, user, ontology_id=project.id, cursor=first["cursor"])
    ids = [item["id"] for page in (first, second) for item in page["items"]]
    assert ids == [f"type-{index:02}" for index in range(25)]
    assert not second["hasNext"] and second["cursor"] is None


def test_object_type_pages_reject_changed_allowlist(client, auth_headers, ontology, db):
    user = _user(db)
    project = db.get(OntologyProject, ontology["id"])
    release = db.get(OntologyVersion, project.current_release_id)
    release.snapshot_formal = {"objectTypes": [
        {"id": f"type-{index:02}", "name": f"Type{index}"} for index in range(25)]}
    profile = AgentProfile(ontology_id=project.id, enabled=True)
    db.add(profile)
    db.commit()
    first = catalog_object_types(db, user, ontology_id=project.id)
    profile.allowed_object_type_ids = ["type-24"]
    db.commit()
    with pytest.raises(StructureError) as error:
        catalog_object_types(db, user, ontology_id=project.id, cursor=first["cursor"])
    assert error.value.code == "catalog_context_changed"
    fresh = catalog_object_types(db, user, ontology_id=project.id)
    assert [item["id"] for item in fresh["items"]] == ["type-24"]


def test_document_facade_preserves_stale_release_error(client, auth_headers, ontology, db):
    from app.ontologies.agent_runtime.assistant_facade import FacadeError, catalog_get_document as read

    with pytest.raises(FacadeError) as error:
        read(db, _user(db), ontology_id=ontology["id"], release_id="stale-release")
    assert error.value.status == 409
    assert error.value.code == "release_context_changed"
