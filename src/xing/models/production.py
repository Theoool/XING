"""productions / publications 的 ORM 映射。

生命周期是分开的：
    contents 是「内容资产」（文字/脚本）
    productions 是「把它做成可发布产物」（视频/音频），一个内容可以有多次生产
    publications 是「把产物发到某个平台」，一个产物可以发多个平台

三张表都没有对应的 API（当前接口只到 contents 与 tasks），但必须映射：
一是为了 ``alembic check`` 能覆盖全库，二是这套闭环迟早要用上。
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from xing.db.base import Base


class Production(Base):
    __tablename__ = "productions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("uuidv7()"))
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("workspaces.id", ondelete="CASCADE", name="productions_workspace_id_fkey"),
        nullable=False,
    )
    content_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("contents.id", ondelete="RESTRICT", name="productions_content_id_fkey"),
        nullable=False,
    )
    # 钉死「生产的是哪一版内容」：内容改了以后旧产物仍然可追溯
    content_version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # PENDING / RUNNING / SUCCEEDED / FAILED / CANCELLED
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'PENDING'"))
    # VIDEO / AUDIO / OTHER
    production_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'VIDEO'")
    )
    output_uri: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)

    extra_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("idx_productions_workspace_status", "workspace_id", "status"),
        Index("idx_productions_content", "content_id"),
        # worker 只扫 PENDING，索引体积与积压量同阶
        Index(
            "idx_productions_pending",
            "workspace_id",
            "created_at",
            postgresql_where=text("status = 'PENDING'"),
        ),
        {"comment": "Media production jobs and resulting assets."},
    )

    def __repr__(self) -> str:
        return f"<Production {self.content_id} v{self.content_version_number} {self.status}>"


class Publication(Base):
    """发布实例。

    唯一性刻意做成部分索引：``UNIQUE(platform, external_post_id) WHERE external_post_id
    IS NOT NULL``。原设计用 ``NULLS NOT DISTINCT`` 会把「所有未发布的记录」当成同一个值，
    一个工作区只能存在一条 PENDING —— 那是设计缺陷，0002 已改。
    """

    __tablename__ = "publications"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("uuidv7()"))
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("workspaces.id", ondelete="CASCADE", name="publications_workspace_id_fkey"),
        nullable=False,
    )
    production_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("productions.id", ondelete="RESTRICT", name="publications_production_id_fkey"),
        nullable=False,
    )
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    platform_account_ref: Mapped[str | None] = mapped_column(Text)
    # PENDING / PUBLISHING / PUBLISHED / FAILED / DELETED
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'PENDING'"))
    external_post_id: Mapped[str | None] = mapped_column(Text)
    publish_url: Mapped[str | None] = mapped_column(Text)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    extra_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index(
            "publications_external_post_unique",
            "platform",
            "external_post_id",
            unique=True,
            postgresql_where=text("external_post_id IS NOT NULL"),
        ),
        Index("idx_publications_workspace_status", "workspace_id", "status"),
        Index("idx_publications_production", "production_id"),
        Index("idx_publications_platform", "workspace_id", "platform", text("published_at DESC")),
        {"comment": "Publishing instances of a production to external platforms."},
    )

    def __repr__(self) -> str:
        return f"<Publication {self.platform} {self.status}>"


class ProductionStatus:
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PublicationStatus:
    PENDING = "PENDING"
    PUBLISHING = "PUBLISHING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    DELETED = "DELETED"
