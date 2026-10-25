"""Deterministic isolated object-store fixture for mapping projection tests.

The tests deliberately exercise the canonical version reader, which reads its
immutable object from storage. This fake MinIO keeps the contract honest:
the bucket must exist, missing objects are hard failures, and no production
endpoint or ambient bucket is consulted.
"""
from __future__ import annotations

import csv
import hashlib
import io
from urllib.parse import urlsplit

import pytest

from app.data_channel.datasets.models import DatasetVersion
from app.data_channel.curated.models import CuratedReview


class IsolatedMinio:
    """Tiny in-memory MinIO contract: buckets and immutable objects only."""

    def __init__(self):
        self.buckets: dict[str, dict[str, bytes]] = {"test": {}}

    def ensure_bucket(self, bucket: str) -> None:
        if bucket not in self.buckets:
            self.buckets[bucket] = {}

    def put_object(self, bucket: str, key: str, data: bytes) -> str:
        if bucket not in self.buckets:
            raise FileNotFoundError(f"MinIO bucket does not exist: {bucket}")
        if not key:
            raise ValueError("object key is required")
        self.buckets[bucket][key] = bytes(data)
        return f"s3://{bucket}/{key}"

    def get_object(self, uri: str) -> bytes:
        parsed = urlsplit(uri)
        if parsed.scheme != "s3" or not parsed.netloc:
            raise ValueError(f"not an s3 object URI: {uri}")
        bucket, _, key = parsed.netloc, parsed.path.lstrip("/"), None
        key = parsed.path.lstrip("/")
        try:
            return self.buckets[bucket][key]
        except KeyError:
            raise FileNotFoundError(f"MinIO object does not exist: {uri}") from None


def csv_bytes(rows: list[dict]) -> bytes:
    if not rows:
        raise ValueError("fixture rows must not be empty")
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def store_rows(db, storage: IsolatedMinio, dataset, rows: list[dict]) -> None:
    """Attach real deterministic CSV bytes to the dataset's existing version."""
    version = (
        db.query(DatasetVersion)
        .filter(DatasetVersion.dataset_id == dataset.id)
        .order_by(DatasetVersion.version_no.desc())
        .first()
    )
    if version is None:
        raise AssertionError(f"dataset {dataset.id} has no version fixture")
    raw = csv_bytes(rows)
    version.storage_uri = storage.put_object(
        "test", f"{dataset.id}/v1.csv", raw)
    version.rowcount = len(rows)
    version.checksum = hashlib.sha256(raw).hexdigest()
    if dataset.kind == "curated":
        existing = (
            db.query(CuratedReview)
            .filter(
                CuratedReview.curated_dataset_id == dataset.id,
                CuratedReview.dataset_version_id == version.id,
                CuratedReview.status == "approved",
            )
            .first()
        )
        if existing is None:
            db.add(CuratedReview(
                curated_dataset_id=dataset.id,
                dataset_version_id=version.id,
                status="approved",
            ))
    db.commit()


@pytest.fixture
def mapping_storage(monkeypatch):
    storage = IsolatedMinio()
    storage.ensure_bucket("test")
    monkeypatch.setattr(
        "app.data_channel.datasets.service.get_storage_service",
        lambda: storage,
    )
    return storage
