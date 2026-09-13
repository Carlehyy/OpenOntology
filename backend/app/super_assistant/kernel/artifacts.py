"""Artifact checksum、chunk 和最终业务状态判定。"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .contracts import ContractError

MAX_ARTIFACT_BYTES = 1 * 1024 * 1024 * 1024


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

