"""Artifact checksum、chunk 和最终业务状态判定。"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .contracts import ContractError

MAX_ARTIFACT_BYTES = 1 * 1024 * 1024 * 1024


def artifact_is_expired(*, status: str, retention_until: datetime | None, now: datetime | None = None) -> bool:
    """Return whether an artifact must no longer be readable.

    Deletion and retention are enforced before touching object storage.  This
    keeps an old object URI from becoming a way to resurrect a deleted asset.
    """
    if status == "deleted":
        return True
    if retention_until is None:
        return False
    current = now or datetime.now(timezone.utc)
    expiry = retention_until
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry <= current


def validate_object_storage_ref(storage_ref: str, *, owner_id: str, run_id: str, artifact_id: str) -> None:
    """Validate that an object URI is namespaced by its owning run.

    The database owner predicate protects the API row; this second boundary
    protects against a row whose URI was accidentally pointed at another
    user's object.  Keys may have an implementation-specific prefix, but the
    owner, run and artifact identifiers must occur in that order as complete
    path components.
    """
    parsed = urlsplit(storage_ref)
    if parsed.scheme not in {"s3", "local"} or not parsed.netloc or not parsed.path:
        raise ValueError("artifact storage_ref must be an s3:// or local:// URI")
    parts = [part for part in parsed.path.lstrip("/").split("/") if part]
    expected = [str(owner_id), str(run_id), str(artifact_id)]
    position = 0
    for part in parts:
        if part == expected[position]:
            position += 1
            if position == len(expected):
                return
    raise ValueError("artifact storage_ref is outside the owning run namespace")


@dataclass(frozen=True, slots=True)
class ArtifactIntegrity:
    checksum: str
    size: int
    integrity_status: str
    business_status: str


def verify_artifact(data: bytes, *, expected_checksum: str, expected_size: int) -> ArtifactIntegrity:
    if expected_size < 0 or expected_size > MAX_ARTIFACT_BYTES:
        raise ContractError("artifact size exceeds configured maximum")
    actual = "sha256:" + hashlib.sha256(data).hexdigest()
    if len(data) != expected_size or actual != expected_checksum:
        return ArtifactIntegrity(actual, len(data), "failed", "integrity_failed")
    return ArtifactIntegrity(actual, len(data), "verified", "pending")


def complete_business_artifact(integrity: ArtifactIntegrity, *, success: bool) -> ArtifactIntegrity:
    if integrity.integrity_status != "verified":
        raise ContractError("artifact must pass integrity verification first")
    return ArtifactIntegrity(
        integrity.checksum, integrity.size, integrity.integrity_status,
        "success" if success else "business_failed",
    )
