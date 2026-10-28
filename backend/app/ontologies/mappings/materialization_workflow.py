"""HTTP use cases for the existing versioned local materialization lane."""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session


def reject_raw_apply(
    db: Session,
    ontology_id: str,
    mapping_id: str,
    data: list[dict],
) -> None:
    """Reject data that bypasses the versioned dataset source contract."""
    from app.ontologies.mappings.models import OntologyMapping

    # Keep the request parameter in the workflow contract even though the
    # bypass is intentionally rejected after ownership validation.
    del data
    mapping = (
        db.query(OntologyMapping)
        .filter(
            OntologyMapping.id == mapping_id,
            OntologyMapping.ontology_id == ontology_id,
        )
        .first()
    )
    if not mapping:
        raise HTTPException(404, "Mapping not found in this ontology")
    raise HTTPException(
        409,
        "禁止直接提交原始数据执行映射；请使用 apply-from-dataset，"
        "由服务端读取已绑定且通过校验的数据版本",
    )


def apply_mapping_from_dataset(
    db: Session,
    ontology_id: str,
    mapping_id: str,
) -> dict:
    from app.ontologies.mappings.models import OntologyMapping
    from app.ontologies.mappings.mapping_service import (
        MappingApplyError,
        MappingReleaseScopeError,
        MappingService,
        MappingSourceError,
    )

    mapping = (
        db.query(OntologyMapping)
        .filter(
            OntologyMapping.id == mapping_id,
            OntologyMapping.ontology_id == ontology_id,
        )
        .first()
    )
    if not mapping:
        raise HTTPException(404, "Mapping not found")
    if not mapping.curated_dataset_id:
        raise HTTPException(400, "Mapping has no curated_dataset_id")

    service = MappingService(db)
    try:
        result = service.build_all(ontology_id, require_approved=True)
        result["trigger_mapping_id"] = mapping_id
        return result
    except MappingSourceError as exc:
        raise HTTPException(422, str(exc))
    except MappingReleaseScopeError as exc:
        raise HTTPException(
            409,
            detail={
                "code": "mapping_not_in_current_release",
                "message": str(exc),
            },
        )
    except MappingApplyError as exc:
        raise HTTPException(500, str(exc))


def build_all_mappings(db: Session, ontology_id: str) -> dict:
    from app.ontologies.mappings.models import OntologyLinkMapping
    from app.ontologies.mappings.mapping_service import (
        MappingApplyError,
        MappingReleaseScopeError,
        MappingService,
        MappingSourceError,
    )

    service = MappingService(db)
    try:
        result = service.build_all(ontology_id, require_approved=True)
        active_links = (
            db.query(OntologyLinkMapping)
            .filter(
                OntologyLinkMapping.ontology_id == ontology_id,
                OntologyLinkMapping.status == "active",
            )
            .count()
        )
        inferred_links = (
            db.query(OntologyLinkMapping)
            .filter(
                OntologyLinkMapping.ontology_id == ontology_id,
                OntologyLinkMapping.status == "inferred",
            )
            .count()
        )
        result["link_mappings_configured"] = active_links
        result["link_mappings_inferred"] = inferred_links
        return result
    except MappingSourceError as exc:
        raise HTTPException(422, detail=str(exc))
    except MappingReleaseScopeError as exc:
        raise HTTPException(
            409,
            detail={
                "code": "mapping_not_in_current_release",
                "message": str(exc),
            },
        )
    except MappingApplyError as exc:
        raise HTTPException(500, detail=str(exc))
    except Exception as exc:
        raise HTTPException(500, detail=str(exc))
