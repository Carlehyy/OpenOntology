"""Mapping-test object-store fixture registration."""
from __future__ import annotations

import pytest

from .minio_fixture import IsolatedMinio


@pytest.fixture
def mapping_storage(monkeypatch):
    storage = IsolatedMinio()
    storage.ensure_bucket("test")
    monkeypatch.setattr(
        "app.data_channel.datasets.service.get_storage_service",
        lambda: storage,
    )
    return storage
