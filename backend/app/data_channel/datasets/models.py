import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    String, DateTime, JSON, Integer, BigInteger, ForeignKey, Text, Index,
    LargeBinary, CheckConstraint, Boolean, UniqueConstraint, text,
)
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base

class Dataset(Base):
    __tablename__ = "v2_datasets"
    # curated 数据集按名字复用追加版本，同名双胞胎会让后续运行随机落到其中一个
    __table_args__ = (
        Index("uq_datasets_curated_name", "name", unique=True,
              sqlite_where=text("kind = 'curated'"),
              postgresql_where=text("kind = 'curated'")),
        Index(
            "uq_datasets_producer_output", "producer_pipeline_id", "output_key",
            unique=True,
            sqlite_where=text("producer_pipeline_id IS NOT NULL"),
            postgresql_where=text("producer_pipeline_id IS NOT NULL"),
        ),
        Index(
            "uq_datasets_connection_resource",
            "source_connection_id", "source_resource",
            unique=True,
            sqlite_where=text(
                "source_connection_id IS NOT NULL AND source_resource IS NOT NULL"),
            postgresql_where=text(
                "source_connection_id IS NOT NULL AND source_resource IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    source_connection_id: Mapped[str | None] = mapped_column(String, ForeignKey("v2_connections.id"), nullable=True)
    # Connection 只是数据源容器，resource（表/集合/端点/文件）才是其中的稳定
    # 数据集身份。历史记录允许 NULL，但新连接同步必须同时写入这两列。
    source_resource: Mapped[str | None] = mapped_column(String(500), nullable=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)  # structured|semi|unstructured|curated
    # 成品资产的稳定生产者身份必须是数据库列和唯一约束，不能藏在 JSON 里靠
    # “先查再插”维持。人工/原始数据集两列均为 NULL。
    producer_pipeline_id: Mapped[str | None] = mapped_column(
        String, ForeignKey(
            "v2_pipelines.id", ondelete="RESTRICT", use_alter=True,
            name="fk_v2_datasets_producer_pipeline_id_v2_pipelines",
        ), nullable=True)
    output_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    schema_json: Mapped[dict] = mapped_column(JSON, nullable=True)
    latest_version_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey(
            "v2_dataset_versions.id", ondelete="SET NULL", use_alter=True,
            name="fk_v2_datasets_latest_version_id_v2_dataset_versions",
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

class DatasetVersion(Base):
    __tablename__ = "v2_dataset_versions"
    __table_args__ = (
        Index("uq_dataset_versions_dataset_version", "dataset_id", "version_no", unique=True),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    dataset_id: Mapped[str] = mapped_column(String, ForeignKey("v2_datasets.id", ondelete="CASCADE"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    rowcount: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 结构化/半结构化数据的权威快照直接保存在平台数据库中；成品（curated）
    # 数据集自 0062 起改为物理湖表（lake_ds_*）+ 行级变更集，新版本本列恒为
    # NULL。storage_uri 仅用于非结构化文件和迁移前的历史数据集版本，不能再被
    # 管理员 MinIO 配置隐式重定向。
    # Deferred loading keeps dataset listings/version metadata queries from
    # materializing full snapshots; readers fetch it only for the selected version.
    data_blob: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True, deferred=True)
    # Keep payload presence/size queryable without loading the deferred bytea.
    # NULL identifies pre-migration versions; zero is a valid empty payload.
    data_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    storage_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # End-to-end lineage: which pipeline run produced this immutable version.
    # NULL for versions created outside a pipeline run (manual data writes,
    # import jobs, pre-lineage history).  The run row carries task_id, so
    # version → run → task is one hop; facts reference this id via
    # fo_property_facts.source_dataset_version_id.
    producer_run_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("v2_pipeline_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class IntegratedSyncRun(Base):
    """Frozen source snapshot manifest; only sealed runs expose a version.

    A loading/failed run is not a DatasetVersion.  Rows are committed in bounded
    batches to a private relation, then physical count + ordered checksum are
    verified before the version/outbox/latest pointer become visible together.
    Failed runs restart from a fresh source snapshot, never a guessed offset.
    """
    __tablename__ = "v2_integrated_sync_runs"
    __table_args__ = (
        Index("ix_integrated_sync_runs_dataset_status", "dataset_id", "status"),
        Index("ix_integrated_sync_runs_asset_status", "asset_id", "status"),
        CheckConstraint("expected_rows >= 0", name="ck_integrated_sync_expected_rows"),
        CheckConstraint("row_count >= 0", name="ck_integrated_sync_row_count"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    dataset_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_datasets.id", ondelete="CASCADE"), nullable=False, index=True)
    asset_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_integrated_assets.id", ondelete="RESTRICT"), nullable=False, index=True)
    base_version_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("v2_dataset_versions.id", ondelete="RESTRICT"), nullable=True)
    version_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("v2_dataset_versions.id", ondelete="RESTRICT"), nullable=True, unique=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="loading")
    physical_table: Mapped[str] = mapped_column(String(63), nullable=False, unique=True)
    schema_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    expected_rows: Mapped[int] = mapped_column(BigInteger, nullable=False)
    row_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IntegratedSyncRecovery(Base):
    """Immutable audit receipt for one recovered abandoned integrated run.

    Written in the same transaction as the CAS to ``failed`` and the private
    relation drop.  A unique ``run_id`` makes a repeated recovery idempotent;
    resource cleanup and later retries never rewrite or delete this receipt.
    Deliberately has no FK to the run/dataset: the audit must survive even an
    administrative dataset deletion.
    """
    __tablename__ = "v2_integrated_sync_recoveries"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_integrated_sync_recovery_run"),
        Index("ix_integrated_sync_recoveries_recovered_at", "recovered_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    asset_id: Mapped[str] = mapped_column(String, nullable=False)
    dataset_id: Mapped[str] = mapped_column(String, nullable=False)
    original_status: Mapped[str] = mapped_column(String(20), nullable=False)
    owner_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ownership_method: Mapped[str] = mapped_column(String(40), nullable=False)
    ownership_proof: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    recovered_by: Mapped[str] = mapped_column(String(200), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    physical_table: Mapped[str] = mapped_column(String(63), nullable=False)
    table_existed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    table_dropped: Mapped[bool] = mapped_column(Boolean, nullable=False)
    recovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc))


class DatasetVersionEvent(Base):
    """Transactional outbox event emitted for every immutable lake version.

    The version row and this event are committed together.  Consumers can
    therefore retry after a process restart without reconstructing intent from
    ``latest_version_id`` or relying on an in-process callback that may be lost.
    """
    __tablename__ = "v2_dataset_version_events"
    __table_args__ = (
        Index(
            "uq_v2_dataset_version_events_version_type",
            "dataset_version_id", "event_type", unique=True,
        ),
        Index(
            "ix_v2_dataset_version_events_ready",
            "status", "available_at", "created_at",
        ),
        Index("ix_v2_dataset_version_events_dataset_id", "dataset_id"),
    )

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4()))
    dataset_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_datasets.id", ondelete="CASCADE"), nullable=False)
    dataset_version_id: Mapped[str] = mapped_column(
        String, ForeignKey("v2_dataset_versions.id", ondelete="CASCADE"), nullable=False)
    event_type: Mapped[str] = mapped_column(
        String(40), nullable=False, default="version_published")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc))
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc))


class DatasetChangeset(Base):
    """成品数据集版本的行级变更集（物理湖表 + 版本元数据下的差异载体）。

    change_type=baseline 仅记计数（迁移灌入物理表时的初始全量基线）；
    change_type=run 由 lake_store.upsert_run 逐行记录 old/new，是审核 diff
    与版本逆向回放的唯一依据。一个版本至多一个变更集。
    """
    __tablename__ = "v2_dataset_changesets"
    __table_args__ = (
        Index("ix_v2_dataset_changesets_dataset_id", "dataset_id"),
        Index("uq_v2_dataset_changesets_version_id", "version_id", unique=True),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    dataset_id: Mapped[str] = mapped_column(String, ForeignKey("v2_datasets.id", ondelete="CASCADE"), nullable=False)
    version_id: Mapped[str] = mapped_column(String, ForeignKey("v2_dataset_versions.id", ondelete="CASCADE"), nullable=False)
    change_type: Mapped[str] = mapped_column(String(20), nullable=False)  # baseline|run
    added_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    deleted_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # 变更集规范哈希（排序逐行明细 + 计数的紧凑 JSON 之 SHA-256），同版本行的
    # checksum，供版本不可变性与跨存储一致性校验。
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class DatasetChangesetRow(Base):
    """变更集逐行明细：added 记 new_row，deleted 记 old_row，updated 两者皆记。

    row_pk 与行级审核同一编码口径：单主键为纯文本，复合主键为紧凑 JSON 数组
    （row_pk_encoding='json-array' 前端契约）。
    """
    __tablename__ = "v2_dataset_changeset_rows"
    __table_args__ = (
        Index("ix_v2_dataset_changeset_rows_changeset_row_pk", "changeset_id", "row_pk"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    changeset_id: Mapped[str] = mapped_column(String, ForeignKey("v2_dataset_changesets.id", ondelete="CASCADE"), nullable=False)
    row_pk: Mapped[str] = mapped_column(String(1000), nullable=False)
    change_type: Mapped[str] = mapped_column(String(10), nullable=False)  # added|updated|deleted
    old_row: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    new_row: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class DatasetWriteLock(Base):
    """数据集写锁（跨进程互斥）。

    入湖合并是「读全量→内存合并→写新版」，锁不住就会互相覆盖增量。
    行本身即锁：主键冲突 = 有人持锁；acquired_at 超时视为持有者已崩溃，可被接管。
    """
    __tablename__ = "v2_dataset_write_locks"

    lock_key: Mapped[str] = mapped_column(String(300), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                  default=lambda: datetime.now(timezone.utc))

class MediaItem(Base):
    __tablename__ = "v2_media_items"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    dataset_version_id: Mapped[str] = mapped_column(String, ForeignKey("v2_dataset_versions.id", ondelete="CASCADE"), nullable=False)
    media_type: Mapped[str] = mapped_column(String(20), nullable=False)  # pdf|docx|image|audio
    storage_uri: Mapped[str] = mapped_column(Text, nullable=False)
    ocr_status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|processing|done|failed
    ocr_result_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class StorageDeletionOutbox(Base):
    """已删除资产对应的对象存储清理任务。

    数据库与对象存储无法共享事务。删除 Dataset 时先在同一个数据库事务中写入
    outbox，再删除版本/媒体元数据；提交成功后机会式清理对象。对象存储暂时不可用
    时记录会保留，避免出现“数据库显示已删除，但对象永久泄漏”的静默成功。
    """
    __tablename__ = "v2_storage_deletion_outbox"
    __table_args__ = (
        Index("ix_v2_storage_deletion_outbox_storage_uri", "storage_uri"),
        Index("ix_v2_storage_deletion_outbox_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4()))
    storage_uri: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc), nullable=False)
