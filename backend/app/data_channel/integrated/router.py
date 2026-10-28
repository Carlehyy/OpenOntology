"""HTTP adapter: no SQL strings, bulk mutations or cross-source writes."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db
from app.data_channel.integrated import service
from app.data_channel.integrated.contracts import (
    RegisterAsset, RowMutation, SourceReference, SourceSchemaRequest, fail,
)
from app.data_channel.integrated.models import IntegratedMutation
from app.data_channel.integrated.native import authorize, get_asset

router = APIRouter()


@router.get("")
def list_assets(db: Session = Depends(get_db), user=Depends(get_current_user)):
    return service.list_assets(db, user)


@router.post("", status_code=201)
def register_asset(body: RegisterAsset, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return service.register_asset(db, user, body)


# Inline source lifecycle: probe, discovery and physical schema inspection all
# stay inside the integrated module.  Probes never persist anything.
@router.post("/sources/test")
def test_source(body: SourceReference, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return service.test_source(db, user, body)


@router.post("/sources/resources")
def source_resources(body: SourceReference, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return service.list_source_resources(db, user, body)


@router.post("/sources/schema")
def source_schema(body: SourceSchemaRequest, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return service.describe_source_schema(db, user, body)


# Declare the static connection path before the generic asset path.
@router.get("/connections/{connection_id}/resources")
def resources(connection_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    # Topology discovery can expose every schema/table name and therefore is a
    # management operation, unlike reading already-registered asset rows.
    authorize(db, user, write=True)
    connection = service._connection(db, connection_id)
    return {"items": service.dbx().list_resources(connection)}


@router.get("/{asset_id}")
def asset(asset_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return service.asset_dto(db, user, get_asset(db, user, asset_id))


@router.get("/{asset_id}/schema")
def schema(asset_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    record = get_asset(db, user, asset_id)
    payload = {"asset_id": record.id, **record.schema_json,
               "schema_fingerprint": record.schema_fingerprint}
    # Logical presentation for configured assets; schema_fingerprint stays the
    # physical identity and never covers the projection.
    projection = service.asset_projection(record)
    if projection is not None:
        payload["columns"] = projection["columns"]
        payload["primary_key"] = projection["primary_key"]
        payload["field_config"] = projection["field_config"]
        payload["logical_fingerprint"] = projection["fingerprint"]
    return payload


@router.post("/{asset_id}/refresh-schema")
def refresh_schema(asset_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return service.refresh_schema(db, user, asset_id)


# 受控注销：零历史 / 零映射消费引用 / 零任务才允许，否则结构化 409；
# 历史与审计记录（版本/同步运行/写入回执，任何状态）一律不删。
@router.delete("/{asset_id}", status_code=204)
def decommission_asset(asset_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    service.decommission_asset(db, user, asset_id)


@router.get("/{asset_id}/rows")
def rows(asset_id: str, limit: int = Query(100, ge=1, le=1000),
         db: Session = Depends(get_db), user=Depends(get_current_user)):
    return service.preview_rows(db, user, asset_id, limit)


@router.post("/{asset_id}/rows")
def insert_row(asset_id: str, body: RowMutation, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return service.mutate_row(db, user, asset_id, "insert", body)


@router.patch("/{asset_id}/rows")
def update_row(asset_id: str, body: RowMutation, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return service.mutate_row(db, user, asset_id, "update", body)


@router.delete("/{asset_id}/rows")
def delete_row(asset_id: str, body: RowMutation, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return service.mutate_row(db, user, asset_id, "delete", body)


@router.get("/{asset_id}/mutations/{mutation_id}")
def mutation(asset_id: str, mutation_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    get_asset(db, user, asset_id)
    record = db.get(IntegratedMutation, mutation_id)
    if record is None or record.asset_id != asset_id:
        fail(404, "MUTATION_NOT_FOUND", "Mutation receipt not found")
    if record.actor_id != user.id and user.role != "admin":
        fail(403, "MUTATION_ACCESS_DENIED", "Only the actor or admin can view this receipt")
    return service.receipt(record)


@router.get("/{asset_id}/versions")
def versions(asset_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return service.versions(db, user, asset_id)


@router.post("/{asset_id}/sync", status_code=202)
def sync(asset_id: str, batch_size: int = Query(1000, ge=1, le=1000),
         db: Session = Depends(get_db), user=Depends(get_current_user)):
    return service.request_sync(db, user, asset_id, batch_size)


@router.get("/{asset_id}/sync-runs")
def sync_runs(asset_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return service.list_sync_runs(db, user, asset_id)


@router.get("/{asset_id}/sync-runs/{run_id}")
def sync_run(asset_id: str, run_id: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return service.get_sync_run(db, user, asset_id, run_id)
