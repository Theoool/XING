"""performance_snapshots / feedback 的 ORM 映射。

快照按「发布后多久」对齐（1H/6H/24H/7D/30D），而不是按抓取时间：
只有同口径才能跨内容比较，否则一个发了 3 天的和一个刚发的没法放在一起看。

feedback 有 ``CHECK (content_id 非空) + (publication_id 非空) = 1``，
所以一条反馈只能挂一个业务目标。这个 CHECK 不在模型里声明
（Alembic 不比较 CHECK），但插入时必须自己保证 —— 两个都不给或都给会直接 23514。
"""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from xing.db.base import Base


class PerformanceSnapshot(Base):
    __tablename__ = "performance_snapshots"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("uuidv7()"))
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "workspaces.id", ondelete="CASCADE", name="performance_snapshots_workspace_id_fkey"
        ),
        nullable=False,
    )
    publication_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "publications.id",
            ondelete="CASCADE",
            name="performance_snapshots_publication_id_fkey",
        ),
        nullable=False,
    )
    # 1H / 6H / 24H / 7D / 30D
    age_bucket: Mapped[str] = mapped_column(Text, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    views: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    likes: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    comments: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    shares: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    saves: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    # 这个没有 CHECK：掉粉是负数，是合法数据
    followers_gained: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    watch_time_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))

    completion_rate: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    engagement_rate: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    raw_metrics: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        # 同一发布 + 同一时间桶只有一条规范快照；重抓要 UPSERT 而不是再插一条
        UniqueConstraint("publication_id", "age_bucket", name="performance_snapshot_unique"),
        Index("idx_performance_publication", "publication_id"),
        Index(
            "idx_performance_workspace_bucket",
            "workspace_id",
            "age_bucket",
            text("captured_at DESC"),
        ),
        {"comment": ("Canonical age-aligned performance snapshots for cross-content comparison.")},
    )

    def __repr__(self) -> str:
        return f"<PerformanceSnapshot {self.publication_id} {self.age_bucket}>"


class Feedback(Base):
    """人/系统反馈。定量、可重复的评测将来应该另建 evaluations 表，不要塞这里。"""

    __tablename__ = "feedback"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("uuidv7()"))
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("workspaces.id", ondelete="CASCADE", name="feedback_workspace_id_fkey"),
        nullable=False,
    )
    # content_id 与 publication_id 二选一，且必须恰好一个（CHECK feedback_exactly_one_target）
    content_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("contents.id", ondelete="CASCADE", name="feedback_content_id_fkey"),
    )
    publication_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("publications.id", ondelete="CASCADE", name="feedback_publication_id_fkey"),
    )
    # HUMAN_REVIEW / USER_EDIT / PERFORMANCE / SYSTEM / OTHER
    feedback_type: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(Text)
    rating: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    comment: Mapped[str | None] = mapped_column(Text)

    extra_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    created_by: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="SET NULL", name="feedback_created_by_fkey"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("idx_feedback_workspace_created", "workspace_id", text("created_at DESC")),
        Index("idx_feedback_content", "content_id"),
        Index("idx_feedback_publication", "publication_id"),
        {
            "comment": (
                "Human/system feedback. Quantitative repeatable evaluations "
                "belong in a future evaluations table."
            )
        },
    )

    def __repr__(self) -> str:
        return f"<Feedback {self.feedback_type}>"


class AgeBucket:
    HOUR_1 = "1H"
    HOUR_6 = "6H"
    HOUR_24 = "24H"
    DAY_7 = "7D"
    DAY_30 = "30D"


class FeedbackType:
    HUMAN_REVIEW = "HUMAN_REVIEW"
    USER_EDIT = "USER_EDIT"
    PERFORMANCE = "PERFORMANCE"
    SYSTEM = "SYSTEM"
    OTHER = "OTHER"
