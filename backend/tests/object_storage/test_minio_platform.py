from __future__ import annotations

import io
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth.models import User
from app.deps import get_current_user, get_db
from app.settings.object_storage.models import MinioOperationAudit
from app.settings.object_storage.service import (
    MinioServiceError,
    WorkspaceMinioService,
    execute_minio_tool,
    minio_tool_manifest,
    validate_bucket_name,
    validate_object_key,
)
from app.shared.database import Base
from app.super_assistant import router as super_assistant_router
from app.super_assistant.models import SuperAssistantMcpServer
from app.super_assistant.kernel.models import CapabilityRevision


WORKSPACE_BUCKET = "assistant-workspace"


class _Response(io.BytesIO):
    def release_conn(self):
        return None


class FakeMinio:
    def __init__(self):
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}
        self.buckets: set[str] = set()

    def bucket_exists(self, bucket):
        return bucket in self.buckets

    def make_bucket(self, bucket, location=None):
        self.buckets.add(bucket)

    def put_object(self, bucket, key, data, length, content_type):
        self.objects[(bucket, key)] = (data.read(length), content_type)
        return SimpleNamespace(etag="etag-upload", version_id=None)

    def list_objects(self, bucket, prefix=None, recursive=True, start_after=None):
        for (item_bucket, key), (data, content_type) in sorted(self.objects.items()):
            if item_bucket != bucket or (prefix and not key.startswith(prefix)) or (start_after and key <= start_after):
                continue
            yield SimpleNamespace(
                object_name=key, size=len(data), etag="etag-list", last_modified=datetime.now(timezone.utc),
                content_type=content_type, version_id=None, is_dir=False,
            )

    def stat_object(self, bucket, key):
        data, content_type = self.objects[(bucket, key)]
        return SimpleNamespace(
            object_name=key, size=len(data), etag="etag-stat", last_modified=datetime.now(timezone.utc),
            content_type=content_type, version_id=None, is_dir=False, metadata={"x-amz-meta-test": "yes"},
        )

    def get_object(self, bucket, key):
        return _Response(self.objects[(bucket, key)][0])

    def remove_object(self, bucket, key):
        self.objects.pop((bucket, key), None)

    def copy_object(self, bucket, key, source):
        self.objects[(bucket, key)] = self.objects[(source.bucket_name, source.object_name)]
        return SimpleNamespace(etag="etag-copy", version_id=None)

    def presigned_get_object(self, bucket, key, expires):
        return f"https://storage.invalid/{bucket}/{key}?get=1"

    def presigned_put_object(self, bucket, key, expires):
        return f"https://storage.invalid/{bucket}/{key}?put=1"


def _workspace_service(fake: FakeMinio, *, allow_delete: bool = False) -> WorkspaceMinioService:
    service = WorkspaceMinioService(
        endpoint="minio.invalid:9000",
        access_key="access",
        secret_key="secret",
        secure=False,
        bucket=WORKSPACE_BUCKET,
        allow_delete=allow_delete,
    )
    service._client = fake
    return service


def test_bucket_and_key_validation():
    assert validate_bucket_name("assistant-workspace") == "assistant-workspace"
    with pytest.raises(MinioServiceError):
        validate_bucket_name("192.168.1.2")
    with pytest.raises(MinioServiceError):
        validate_bucket_name("Bad_Bucket")
    assert validate_object_key("dir/file.txt") == "dir/file.txt"
    with pytest.raises(MinioServiceError):
        validate_object_key("/absolute")


def test_workspace_service_round_trip_pinned_to_workspace_bucket():
    fake = FakeMinio()
    service = _workspace_service(fake)

    uploaded = service.upload_bytes(key="docs/hello.txt", data="你好 MinIO".encode(), content_type="text/plain")
    assert uploaded["uri"] == f"s3://{WORKSPACE_BUCKET}/docs/hello.txt"
    listing = service.list_objects(search="HELLO")
    assert [item["key"] for item in listing["objects"]] == ["docs/hello.txt"]
    assert listing["bucket"] == WORKSPACE_BUCKET
    assert service.read_object(key="docs/hello.txt")["content"] == "你好 MinIO"
    copied = service.copy_object(source_key="docs/hello.txt", destination_key="archive/hello.txt")
    assert copied["destination"] == f"s3://{WORKSPACE_BUCKET}/archive/hello.txt"
    assert "get=1" in service.presign(key="docs/hello.txt")["url"]
    # 任何对象都不会落到工作区以外的桶
    assert {bucket for bucket, _ in fake.objects} == {WORKSPACE_BUCKET}


def test_workspace_bucket_created_lazily_once():
    fake = FakeMinio()
    service = _workspace_service(fake)
    assert fake.buckets == set()
    status = service.status()
    assert status == {
        "connected": True,
        "bucket": WORKSPACE_BUCKET,
        "allow_delete": False,
    }
    assert fake.buckets == {WORKSPACE_BUCKET}
    service.list_objects()
    assert fake.buckets == {WORKSPACE_BUCKET}


def test_delete_and_move_require_allow_delete():
    fake = FakeMinio()
    service = _workspace_service(fake)
    service.upload_bytes(key="docs/a.txt", data=b"a")
    with pytest.raises(MinioServiceError, match="MINIO_MCP_ALLOW_DELETE"):
        service.delete_object(key="docs/a.txt")
    with pytest.raises(MinioServiceError, match="MINIO_MCP_ALLOW_DELETE"):
        service.move_object(source_key="docs/a.txt", destination_key="docs/b.txt")

    deletable = _workspace_service(fake, allow_delete=True)
    moved = deletable.move_object(source_key="docs/a.txt", destination_key="docs/b.txt")
    assert moved["moved"] is True
    assert deletable.delete_object(key="docs/b.txt")["deleted"] is True
    assert fake.objects == {}


def test_tool_manifest_is_workspace_scoped_without_bucket_arguments():
    tools = minio_tool_manifest()
    names = [tool["name"] for tool in tools]
    assert names == [
        "minio_status",
        "minio_list_objects",
        "minio_get_object_metadata",
        "minio_read_object",
        "minio_upload_text",
        "minio_upload_base64",
        "minio_copy_object",
        "minio_move_object",
        "minio_delete_object",
        "minio_get_presigned_url",
    ]
    for tool in tools:
        assert "bucket" not in tool["input_schema"]["properties"]
        assert "source_bucket" not in tool["input_schema"]["properties"]
        assert "destination_bucket" not in tool["input_schema"]["properties"]


def _audit_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'mcp.db'}")
    Base.metadata.create_all(bind=engine, tables=[MinioOperationAudit.__table__])
    return sessionmaker(bind=engine)


def test_execute_minio_tool_round_trip_and_audit(tmp_path, monkeypatch):
    Session = _audit_session(tmp_path)
    fake = FakeMinio()
    service = _workspace_service(fake)
    monkeypatch.setattr(
        "app.settings.object_storage.service.get_workspace_minio_service",
        lambda: service,
    )
    with Session() as db:
        output = execute_minio_tool(db, "minio_upload_text", {
            "key": "mcp/note.md", "content": "MCP upload",
        }, actor_type="super_assistant", actor_id="user-1")
        assert '"ok": true' in output
        read = execute_minio_tool(db, "minio_read_object", {"key": "mcp/note.md"})
        assert "MCP upload" in read
        audits = db.query(MinioOperationAudit).order_by(MinioOperationAudit.created_at).all()
        assert len(audits) == 2
        assert audits[0].actor_type == "super_assistant"
        assert audits[0].actor_id == "user-1"
        assert audits[0].bucket == WORKSPACE_BUCKET
        assert audits[0].object_key == "mcp/note.md"
        assert all(audit.success for audit in audits)


def test_execute_minio_tool_rejects_legacy_bucket_argument(tmp_path, monkeypatch):
    Session = _audit_session(tmp_path)
    service = _workspace_service(FakeMinio())
    monkeypatch.setattr(
        "app.settings.object_storage.service.get_workspace_minio_service",
        lambda: service,
    )
    with Session() as db:
        with pytest.raises(MinioServiceError, match=WORKSPACE_BUCKET):
            execute_minio_tool(db, "minio_read_object", {
                "bucket": "raw-datasets", "key": "mcp/note.md",
            })
        audit = db.query(MinioOperationAudit).one()
        assert audit.operation == "minio_read_object"
        assert audit.success is False
        assert audit.bucket == WORKSPACE_BUCKET


def test_super_assistant_installs_builtin_without_db_config(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'assistant.db'}", connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine, tables=[
        User.__table__, MinioOperationAudit.__table__,
        SuperAssistantMcpServer.__table__, CapabilityRevision.__table__,
    ])
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    admin = User(
        id="admin-2", username="assistant-owner", email="owner@example.com",
        password_hash="unused", role="admin",
    )
    with Session() as db:
        db.add(admin)
        db.commit()

    def override_db():
        with Session() as db:
            yield db

    fake_service = SimpleNamespace(status=lambda: {"connected": True, "bucket": WORKSPACE_BUCKET})
    monkeypatch.setattr(
        super_assistant_router,
        "get_workspace_minio_service",
        lambda: fake_service,
    )
    app = FastAPI()
    app.include_router(super_assistant_router.router, prefix="/api/v2/super-assistant")
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user] = lambda: admin
    client = TestClient(app)

    installed = client.post("/api/v2/super-assistant/mcp-servers/platform-minio")
    assert installed.status_code == 200, installed.text
    payload = installed.json()
    assert payload["builtin_key"] == "minio"
    assert payload["url"] == "builtin://minio"
    assert len(payload["tool_manifest"]) == 10
    assert payload["header_names"] == []

    connection_edit = client.patch(
        f"/api/v2/super-assistant/mcp-servers/{payload['id']}",
        json={"url": "https://attacker.invalid/mcp"},
    )
    assert connection_edit.status_code == 400
    toggled = client.patch(
        f"/api/v2/super-assistant/mcp-servers/{payload['id']}",
        json={"enabled": False, "require_confirmation": False},
    )
    assert toggled.status_code == 200
    assert toggled.json()["enabled"] is False

    with Session() as db:
        saved = db.get(SuperAssistantMcpServer, payload["id"])
        assert saved.builtin_key == "minio"
