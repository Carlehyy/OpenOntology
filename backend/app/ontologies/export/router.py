import re
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session
from app.deps import get_db, get_current_user
from app.ontologies.access import require_ontology_access
from app.services import export_service

router = APIRouter()

@router.get("")
def export_ontology(
    ontology_id: str,
    _format: Literal["json"] | None = Query(default=None, alias="format"),
    version_id: str | None = Query(default=None, alias="version_id"),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Download the current formal ontology structure as the only supported format: JSON.

    ``version_id`` exports one immutable version workspace (used by the graph
    editor so the downloaded package always mirrors the workspace on screen).
    Without it the current published projection is exported.
    """
    project = require_ontology_access(db, ontology_id, current_user, write=False)
    content = export_service.export_json(db, project, version_id=version_id)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", project.name).strip("._") or "ontology"
    utf8_name = quote(f"{project.name}_{project.version or 'draft'}.json")
    return Response(
        content=content.encode("utf-8"),
        media_type="application/json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{safe_name}_{project.version or "draft"}.json"; '
                f"filename*=UTF-8''{utf8_name}"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )
