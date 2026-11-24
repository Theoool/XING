"""topics / topic_events 的 ORM 映射。

Topic 与 ExternalEvent 是两个层次的东西：事件是「外面发生了什么」，
选题是「我决定拿什么来做内容」。``primary_event_id`` 只是主证据，
其余支撑证据在 ``topic_events`` 里（多对多，带 role 与 weight）。

``Topic.primary_event`` 刻意用 ``lazy="raise"``：异步会话里访问未加载的关系
会抛 ``MissingGreenlet``，那个报错很难懂；改成 raise 之后，忘记 eager load
会在访问的第一行就明确报「这个关系没加载」，而不是等到序列化时才崩。

关系属性一律标 ``Any``：``Mapped["ExternalEvent"]`` 这类跨模块字符串注解
依赖 SQLAlchemy 的延迟解析，一旦模块加载顺序变化就会在 mapper 配置期炸掉，
而关系的目标类已经由 ``relationship("ExternalEvent")`` 明确给出，注解不携带额外信息。
"""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    Text,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from xing.db.base import Base


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("uuidv7()"))
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("workspaces.id", ondelete="CASCADE", name="topics_workspace_id_fkey"),
        nullable=False,
    )
    primary_event_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("external_events.id", ondelete="SET NULL", name="topics_primary_event_id_fkey"),
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    # CANDIDATE / RECOMMENDED / SELECTED / REJECTED / EXPIRED / USED，见 TopicStatus
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'CANDIDATE'"))

    # numeric(8,4) 在驱动层是 Decimal。写进 jsonb 前必须显式转 float，
    # 否则 json 序列化会抛 "Object of type Decimal is not JSON serializable"。
    trend_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    relevance_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    potential_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    # 恒为 NULL：当前打分不含 LLM，没有可信的风险信号来源
    risk_score: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))

    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    selected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    primary_event: Mapped[Any] = relationship(
        "ExternalEvent",
        uselist=False,
        lazy="raise",
        viewonly=True,
    )

    __table_args__ = (
        Index("idx_topics_workspace_status", "workspace_id", "status"),
        Index("idx_topics_workspace_created", "workspace_id", text("created_at DESC")),
        Index("idx_topics_primary_event", "primary_event_id"),
        # 一个事件最多转成一个选题。recommend 的 INSERT ... ON CONFLICT DO NOTHING
        # 靠这个部分唯一索引吸收并发重复插入（0004 迁移）。
        Index(
            "topics_one_per_primary_event",
            "workspace_id",
            "primary_event_id",
            unique=True,
            postgresql_where=text("primary_event_id IS NOT NULL"),
        ),
        {
            "comment": (
                "Content topics selected or evaluated by AIXING. Distinct from external events."
            )
        },
    )

    def __repr__(self) -> str:
        return f"<Topic {self.title[:20]} {self.status}>"


class TopicEvent(Base):
    """选题与事件的关联表。没有 id，主键是 (topic_id, event_id)。

    RLS 策略不直接看 workspace_id（这张表没有），而是 exists 到 topics 判断。
    """

    __tablename__ = "topic_events"

    topic_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("topics.id", ondelete="CASCADE", name="topic_events_topic_id_fkey"),
        primary_key=True,
    )
    event_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("external_events.id", ondelete="CASCADE", name="topic_events_event_id_fkey"),
        primary_key=True,
    )
    # PRIMARY / SUPPORTING / CONTRADICTING / BACKGROUND
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'SUPPORTING'"))
    weight: Mapped[Decimal] = mapped_column(
        Numeric(8, 4), nullable=False, server_default=text("1.0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (Index("idx_topic_events_event", "event_id"),)

    def __repr__(self) -> str:
        return f"<TopicEvent {self.topic_id}/{self.event_id} {self.role}>"


class TopicStatus:
    CANDIDATE = "CANDIDATE"
    RECOMMENDED = "RECOMMENDED"
    SELECTED = "SELECTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    USED = "USED"

    #: 只有这两种状态可以被选定/拒绝；已 SELECTED/USED 的再决策会让决策与状态对不上
    SELECTABLE = (CANDIDATE, RECOMMENDED)


class TopicEventRole:
    PRIMARY = "PRIMARY"
    SUPPORTING = "SUPPORTING"
    CONTRADICTING = "CONTRADICTING"
    BACKGROUND = "BACKGROUND"
