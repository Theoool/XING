"""contents / content_versions / approvals 的 ORM 映射。

一个必须绕开的坑：``content_versions`` 有一列叫 ``metadata``，而 ``metadata``
是 SQLAlchemy Declarative 的保留属性（``Base.metadata`` 是 MetaData 对象）。
在 ORM 类里把属性命名成 ``metadata`` 会直接覆盖它，mapper 配置阶段就崩。

所以属性叫 ``extra_metadata``，列名仍是 ``"metadata"``。Pydantic 那边用
``validation_alias="extra_metadata"`` 把名字接上，API 契约（响应字段叫 metadata）
完全不变 —— 详见 ``schemas/content.py``。

``Content.current_version`` 用 LEFT JOIN（不是 inner）：内容可以先只有标题、
没有正文版本，此时 ``current_version`` 为 null 是合法状态。
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
    UniqueConstraint,
    Uuid,
    and_,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, foreign, mapped_column, relationship

from xing.db.base import Base


class Content(Base):
    __tablename__ = "contents"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("uuidv7()"))
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("workspaces.id", ondelete="CASCADE", name="contents_workspace_id_fkey"),
        nullable=False,
    )
    topic_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("topics.id", ondelete="SET NULL", name="contents_topic_id_fkey"),
    )
    decision_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("decisions.id", ondelete="SET NULL", name="contents_decision_id_fkey"),
    )
    #: 产出这篇内容的任务。SET NULL 而不是 CASCADE：任务不会被删（只 CANCELLED），
    #: 但即便被删，内容作为业务资产也不该跟着消失。
    source_task_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("tasks.id", ondelete="SET NULL", name="contents_source_task_id_fkey"),
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    # VIDEO_SCRIPT / ARTICLE / POST / AUDIO / OTHER
    content_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'VIDEO_SCRIPT'")
    )
    # DRAFT / REVIEW / APPROVED / PRODUCING / READY / PUBLISHED / ARCHIVED。
    # 注意：这个值**由数据库触发器**在 approvals 变化时推导，服务层不直接写
    # （唯一例外是 add_version 把 APPROVED 退回 REVIEW，见 content_service）。
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'DRAFT'"))
    current_version_number: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    # bigint，与库侧一致
    lock_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    created_by: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="RESTRICT", name="contents_created_by_fkey"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    current_version: Mapped[Any] = relationship(
        "ContentVersion",
        primaryjoin=lambda: and_(
            Content.id == foreign(ContentVersion.content_id),
            ContentVersion.version_number == Content.current_version_number,
        ),
        viewonly=True,
        uselist=False,
        lazy="joined",
    )

    __table_args__ = (
        Index("idx_contents_workspace_status", "workspace_id", "status"),
        Index("idx_contents_workspace_updated", "workspace_id", text("updated_at DESC")),
        Index("idx_contents_topic", "topic_id"),
        Index("idx_contents_decision", "decision_id"),
        # 一个生产任务只产出一篇内容；存在的另一层意义是让「这篇是谁产的」可反查。
        Index(
            "contents_source_task",
            "source_task_id",
            unique=True,
            postgresql_where=text("source_task_id IS NOT NULL"),
        ),
        {"comment": "Durable content assets. Production is a separate lifecycle."},
    )

    def __repr__(self) -> str:
        return f"<Content {self.title[:20]} {self.status} v{self.current_version_number}>"


class ContentVersion(Base):
    """不可变版本快照。人工修改本身就是数据：v1(AI) -> v2(人工) -> v3(发布)。"""

    __tablename__ = "content_versions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("uuidv7()"))
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("workspaces.id", ondelete="CASCADE", name="content_versions_workspace_id_fkey"),
        nullable=False,
    )
    content_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("contents.id", ondelete="CASCADE", name="content_versions_content_id_fkey"),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    # 列名是 metadata（保留），属性名避开 Declarative 保留字
    extra_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    change_summary: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="SET NULL", name="content_versions_created_by_fkey"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("content_id", "version_number", name="content_versions_unique"),
        Index("idx_content_versions_content", "content_id", text("version_number DESC")),
        {"comment": "Immutable content snapshots; user edits become explicit versions."},
    )

    def __repr__(self) -> str:
        return f"<ContentVersion {self.content_id} v{self.version_number}>"


class Approval(Base):
    """审批状态机。追加式历史：每次结论都插一行，旧的 PENDING 置 CANCELLED。

    0003 迁移把 ``UNIQUE(content_id, stage)`` 换成了「每个 stage 只能有一条 PENDING」
    的部分唯一索引，所以这里不能声明 full unique —— 那会阻止保留历史。
    """

    __tablename__ = "approvals"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("uuidv7()"))
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("workspaces.id", ondelete="CASCADE", name="approvals_workspace_id_fkey"),
        nullable=False,
    )
    content_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("contents.id", ondelete="CASCADE", name="approvals_content_id_fkey"),
        nullable=False,
    )
    # CONTENT / PRODUCTION / PUBLICATION
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    # PENDING / APPROVED / REJECTED / CANCELLED
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'PENDING'"))
    reviewer_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="SET NULL", name="approvals_reviewer_id_fkey"),
    )
    comment: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index(
            "approvals_one_pending_per_stage",
            "content_id",
            "stage",
            unique=True,
            postgresql_where=text("status = 'PENDING'"),
        ),
        Index("idx_approvals_workspace_status", "workspace_id", "status"),
        Index("idx_approvals_content", "content_id"),
        {"comment": "Business approval state machine. Not generic feedback."},
    )

    def __repr__(self) -> str:
        return f"<Approval {self.stage} {self.status}>"


class ContentStatus:
    DRAFT = "DRAFT"
    REVIEW = "REVIEW"
    APPROVED = "APPROVED"
    PRODUCING = "PRODUCING"
    READY = "READY"
    PUBLISHED = "PUBLISHED"
    ARCHIVED = "ARCHIVED"


class ContentType:
    VIDEO_SCRIPT = "VIDEO_SCRIPT"
    ARTICLE = "ARTICLE"
    POST = "POST"
    AUDIO = "AUDIO"
    OTHER = "OTHER"


class ApprovalStage:
    CONTENT = "CONTENT"
    PRODUCTION = "PRODUCTION"
    PUBLICATION = "PUBLICATION"


class ApprovalStatus:
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
