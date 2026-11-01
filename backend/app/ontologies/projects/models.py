import uuid
from datetime import datetime, timezone
from sqlalchemy import Integer, String, DateTime, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base

class OntologyProject(Base):
    __tablename__ = "ontology_projects"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    domain: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    # A stable built-in icon key (for example ``network`` or ``shopping-cart``).
    # Keep this nullable so historical rows remain readable without a backfill.
    icon: Mapped[str | None] = mapped_column(String(50), nullable=True)
    version: Mapped[str] = mapped_column(String(20), default="v0")
    status: Mapped[str] = mapped_column(String(20), default="draft")
    # PostgreSQL/Formal 是运行时真相，Neo4j 是可重建查询投影。任何会改变图
    # 当前态的事务都先把该围栏置为 projecting；只有经校验的全量重建成功后
    # 才能恢复 ready。失败状态必须耐久化，避免图接口读取陈旧或半成品数据。
    projection_status: Mapped[str] = mapped_column(
        String(20), default="ready", server_default="ready", nullable=False,
    )
    projection_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 结构投影（OntologyStructureNode 探索图）独立 fence：SQL release 快照
    # 是权威，本图可重建。状态 empty/building/ready/failed 落库；stale 为
    # 读侧对当前发布指针/哈希的派生态。失败耐久可见，绝不假 ready。
    structure_projection_status: Mapped[str] = mapped_column(
        String(20), default="empty", server_default="empty", nullable=False,
    )
    structure_projection_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    structure_projection_release_id: Mapped[str | None] = mapped_column(
        String, nullable=True,
    )
    structure_projection_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    # 运行时唯一读取指针。不要再通过“最大的版本号”推断当前发布版。
    current_release_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey(
            "ontology_versions.id", ondelete="SET NULL", use_alter=True,
            name="fk_ontology_projects_current_release_id",
        ),
        nullable=True,
    )
    build_mode: Mapped[str] = mapped_column(String(30), default="manual", nullable=True)
    # 本体助手卡片的全局选用次数（不按用户区分）。只由助手卡片确认选中时
    # 自增；下拉框切换与深链进入不计入，保证指标只反映卡片选用热度。
    assistant_card_clicks: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False,
    )
    created_by: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
