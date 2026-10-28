"""Pinned, governed source batches for mapping and isolated trial materialization."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.ontologies.mappings.errors import MappingSourceError
from app.ontologies.mappings.models import OntologyMapping


@dataclass(frozen=True)
class MappingSourceBatches:
    """A fixed version and its complete iterator, never the mutable latest blob."""

    version: object
    metadata: dict
    batches: Iterator[list[dict]]


def prepare_mapping_source(
    db: Session, mapping: OntologyMapping, *,
    require_approved: bool = True,
    allow_empty_version: bool = False,
    version_id: str | None = None,
    batch_size: int = 500,
) -> MappingSourceBatches:
    """Resolve governance once and iterate all rows of that immutable version.

    The canonical datasets port verifies seals, manifests, PKs and storage
    integrity. Curated sources additionally keep their approved row-edit overlay.
    This new bounded port deliberately requires a real version: old preview-only
    curated assets cannot establish trial/release lineage.
    """
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 1000:
        raise ValueError("Mapping batch_size must be between 1 and 1000")
    dataset_id = mapping.curated_dataset_id
    if not dataset_id:
        raise MappingSourceError(f"Mapping {mapping.id} 未绑定数据集")

    from app.ontologies.mappings.remote_binding import (
        BINDING_MODE_REMOTE,
        mapping_binding_mode,
    )
    if mapping_binding_mode(
            getattr(mapping, "field_mapping", None)) == BINDING_MODE_REMOTE:
        raise MappingSourceError(
            f"Mapping {mapping.id} 是远程绑定（binding_mode=remote），"
            "不存在本地数据版本可读取；本地物化路径已隔离")

    from app.data_channel.datasets.models import Dataset, DatasetVersion
    from app.data_channel.curated.approved_version_reader import (
        ReviewApprovalError, iter_rows_with_edits, latest_dataset_version,
        require_version_approved,
    )
    from app.data_channel.datasets.service import (
        DatasetReadError, describe_version, iter_version_rows,
    )
    from app.data_channel.datasets.integrated_store import (
        IntegratedVersionError, decode_version_row,
    )

    dataset = db.query(Dataset).filter(Dataset.id == dataset_id).first()
    if dataset is None:
        raise MappingSourceError(f"映射数据集 {dataset_id} 不存在真实版本资产")
    if version_id is None:
        version = latest_dataset_version(db, dataset_id)
    else:
        version = db.query(DatasetVersion).filter(
            DatasetVersion.dataset_id == dataset_id,
            DatasetVersion.id == version_id,
        ).first()
    if version is None:
        raise MappingSourceError(f"映射数据集 {dataset_id} 不存在指定不可变版本")
    try:
        metadata = describe_version(db, dataset_id, version.id)
        if require_approved and dataset.kind == "curated":
            require_version_approved(db, dataset_id, version)
    except (ValueError, DatasetReadError, ReviewApprovalError, IntegratedVersionError) as exc:
        raise MappingSourceError(str(exc)) from exc
    row_count = metadata.get("row_count")
    if row_count is None:
        raise MappingSourceError(f"映射版本 {version.id} 缺少权威行数")
    if not row_count and not allow_empty_version:
        raise MappingSourceError(f"映射数据集「{dataset.name}」当前版本为空，已拒绝空跑投影")

    def batches():
        seen = 0
        if dataset.kind == "curated":
            iterator = iter_rows_with_edits(
                db, dataset_id, require_approved=require_approved,
                version=version, batch_size=batch_size,
            )
        else:
            iterator = iter_version_rows(
                db, dataset_id, version.id, batch_size=batch_size,
            )
        try:
            for batch in iterator:
                if len(batch) > batch_size:
                    raise MappingSourceError("数据版本读取器返回超出契约的批大小")
                seen += len(batch)
                if seen > row_count:
                    raise MappingSourceError(f"映射版本 {version.id} 行数超过密封清单")
                if batch:
                    # The lake port intentionally exposes canonical TEXT/NULL.
                    # Decode only integrated sources against the frozen native
                    # schema; physical TEXT must never become semantic string.
                    yield ([decode_version_row(row, metadata["source_schema"]) for row in batch]
                           if dataset.kind == "integrated" else batch)
        except (ValueError, DatasetReadError, ReviewApprovalError, IntegratedVersionError) as exc:
            raise MappingSourceError(str(exc)) from exc
        if seen != row_count:
            raise MappingSourceError(
                f"映射版本 {version.id} 行数不完整：expected={row_count} actual={seen}")

    return MappingSourceBatches(version, metadata, batches())
