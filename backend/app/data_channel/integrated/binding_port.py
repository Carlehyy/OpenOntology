"""Native metadata/read adapter for published remote ontology bindings."""
from __future__ import annotations

import sqlalchemy as sa

from app.data_channel.datasets.models import Dataset
from app.data_channel.integrated.native import authorize, get_asset, open_read_scope
from app.data_channel.integrated.models import IntegratedAsset


class IntegratedRemoteBindingValidator:
    def _asset(self, db, user, descriptor):
        authorize(db, user)
        if descriptor.get("assetId"):
            return get_asset(db, user, descriptor["assetId"])
        asset = db.scalar(sa.select(IntegratedAsset).where(
            IntegratedAsset.dataset_id == descriptor.get("datasetId")))
        if asset is None:
            from fastapi import HTTPException
            raise HTTPException(404, detail={"code": "remote_binding_asset_missing",
                                            "message": "集成数据集没有已登记的远程资产"})
        return asset

    def accepts(self, db, user, descriptor: dict) -> dict:
        asset = self._asset(db, user, descriptor)
        dataset = db.get(Dataset, asset.dataset_id)
        if dataset is None or dataset.kind != "integrated":
            from fastapi import HTTPException
            raise HTTPException(409, detail={"code": "remote_binding_asset_invalid",
                                            "message": "远程映射必须引用已登记的集成资产"})
        # Opening the native scope reflects the live schema and verifies the
        # registered fingerprint, without reading a source record.
        with open_read_scope(db, user, [asset.id], statement_timeout_ms=10000) as scope:
            meta = scope.meta[asset.id]
            return {"assetId": asset.id, "datasetId": asset.dataset_id,
                    "connectionId": asset.connection_id,
                    "schemaName": asset.schema_name, "tableName": asset.table_name,
                    "columns": meta["columns"],
                    "primaryKey": asset.schema_json["primary_key"],
                    "schemaFingerprint": meta["schema_fingerprint"]}

    def sample(self, db, user, descriptor: dict, *, limit: int) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Remote binding probe must contain 1..100 rows")
        asset = self._asset(db, user, descriptor)
        rows = []
        with open_read_scope(db, user, [asset.id], statement_timeout_ms=10000) as scope:
            table = scope.tables[asset.id]
            statement = sa.select(table).limit(limit)
            for batch in scope.iter_batches(statement, batch_size=min(limit, 100)):
                rows.extend(batch)
        return rows
