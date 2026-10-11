"""任务实例 — 产物对象存储写（底层 seam，无域内依赖）。

service（人工交活）与 container_runtime（容器产物回收）共用；
测试 monkeypatch 本模块以隔离 MinIO。
"""
from __future__ import annotations


def write_artifact_object(key: str, content: bytes, mime_type: str) -> str:
    """产物上传：先落 MinIO 再登记（容器销毁不丢证据，设计 §6.2）。"""
    from app.shared.storage import get_storage_service

    return get_storage_service().put_bytes(
        "intermediate", f"task-instances/{key}", content,
        content_type=mime_type)
