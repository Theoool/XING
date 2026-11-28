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
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
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

    # 种子溯源。一次「展开」是一个 task，这一批判的候选都指回它。
    # 刻意与 primary_event_id 分开：那个字段是「主证据」，这里是「从哪来的」。
    # 合用一个字段会被 topics_one_per_primary_event 的唯一索引挡住 ——
    # 一个事件只能挂一个选题，与「一个事件展开多个选题」冲突。
    seed_task_id: Mapped[UUID | None] = mapped_column(Uuid)
    seed_event_id: Mapped[UUID | None] = mapped_column(Uuid)

    # 深入产出：expand 写 angle/audience/thesis，deepen 合并成完整版。
    # 一个选题一份，1:1 关系不值得建表。
    brief: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

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
        # 必须显式指定：topics 与 external_events 之间现在有两条外键
        # （primary_event_id 单列，以及 (seed_event_id, workspace_id) 复合），
        # 不指定的话 SQLAlchemy 无法判断这条关系走哪条，会在 mapper 配置期
        # 抛 AmbiguousForeignKeysError。
        foreign_keys="Topic.primary_event_id",
    )

    __table_args__ = (
        # 供 topic_stances 的复合外键引用。worker 是 BYPASSRLS，
        # 只有复合键能挡住「把 A 租户的选题挂到 B 租户的立场上」。
        UniqueConstraint("id", "workspace_id", name="topics_id_workspace_key"),
        # 种子引用必须是同租户的。worker 是 BYPASSRLS，写错 workspace_id
        # 不会被策略挡住，只有复合外键能拦（0007 踩过同样的坑）。
        # ON DELETE SET NULL 必须指定列：不指定会连 workspace_id 一起置空，
        # 而它是 NOT NULL，删任务时直接报错。
        ForeignKeyConstraint(
            ["seed_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            name="topics_seed_task_workspace_fkey",
            ondelete="SET NULL (seed_task_id)",
        ),
        ForeignKeyConstraint(
            ["seed_event_id", "workspace_id"],
            ["external_events.id", "external_events.workspace_id"],
            name="topics_seed_event_workspace_fkey",
            ondelete="SET NULL (seed_event_id)",
        ),
        Index("idx_topics_workspace_status", "workspace_id", "status"),
        Index("idx_topics_workspace_created", "workspace_id", text("created_at DESC")),
        Index("idx_topics_primary_event", "primary_event_id"),
        # 部分索引：规则版 recommend 建的选题没有种子，不该进索引。
        Index(
            "idx_topics_seed_task",
            "workspace_id",
            "seed_task_id",
            postgresql_where=text("seed_task_id IS NOT NULL"),
        ),
        Index(
            "idx_topics_seed_event",
            "workspace_id",
            "seed_event_id",
            postgresql_where=text("seed_event_id IS NOT NULL"),
        ),
        # 一个事件最多转成一个选题。recommend 的 INSERT ... ON CONFLICT DO NOTHING
        # 靠这个部分唯一索引吸收并发重复插入（0004 迁移）。
        # 注意它只约束 primary_event_id，不约束 seed_event_id —— 展开多个选题
        # 靠的正是这个区分。
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
    #: 该证据的原文片段。深入阶段写入时已校验它是 ``external_events.summary``
    #: 的子串 —— 所以审核 Agent 可以拿它当「这句到底有没有出处」的判据。
    #: 可以为空：规则版 recommend 建的关系没有原文可引。
    quote: Mapped[str | None] = mapped_column(
        Text,
        comment="该证据的原文片段；落库前已校验它是 external_events.summary 的子串",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (Index("idx_topic_events_event", "event_id"),)

    def __repr__(self) -> str:
        return f"<TopicEvent {self.topic_id}/{self.event_id} {self.role}>"


class OpinionStance(Base):
    """一次展开采集到的一条舆论立场。

    存在的理由只有一个：**让「切面」有出处**。展开阶段的候选必须引用这里的
    行，引用不到就不该存在。``quote`` 落库前已机检为 ``external_events.summary``
    的子串，所以「这个立场真的有人这么说」可以被复核，而不是模型的断言。

    ``camp`` 不是情感极性，而是「站在哪一边」：

    - SUPPORT     支持某方做法/判断
    - OPPOSE      反对某方做法/判断
    - CONDITIONAL 有条件地同意（最容易被模型忽略、也最能出好选题的一类）
    - EXPERIENCE  亲历者/从业者的第一人称说法
    - OBSERVER    旁观分析（媒体、研究者、评论者）
    """

    __tablename__ = "opinion_stances"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("uuidv7()"))
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "workspaces.id",
            ondelete="CASCADE",
            name="opinion_stances_workspace_id_fkey",
        ),
        nullable=False,
    )
    seed_task_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    event_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    camp: Mapped[str] = mapped_column(Text, nullable=False)
    claim: Mapped[str] = mapped_column(Text, nullable=False)
    audience: Mapped[str] = mapped_column(Text, nullable=False)
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("id", "workspace_id", name="opinion_stances_id_workspace_key"),
        ForeignKeyConstraint(
            ["seed_task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            name="opinion_stances_seed_task_workspace_fkey",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["event_id", "workspace_id"],
            ["external_events.id", "external_events.workspace_id"],
            name="opinion_stances_event_workspace_fkey",
            ondelete="CASCADE",
        ),
        Index(
            "opinion_stances_seed_event_label",
            "seed_task_id",
            "event_id",
            "label",
            unique=True,
        ),
        Index("idx_opinion_stances_seed", "workspace_id", "seed_task_id"),
        Index("idx_opinion_stances_event", "event_id"),
        {
            "comment": (
                "Public-opinion stances extracted from retrieved material for one expand "
                "task. Every candidate topic must cite at least one of these."
            )
        },
    )

    def __repr__(self) -> str:
        return f"<OpinionStance {self.label[:16]} {self.camp}>"


class TopicStance(Base):
    """候选引用了哪些立场。没有行 = 这个候选是模型先验，不是舆论切面。"""

    __tablename__ = "topic_stances"

    topic_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    stance_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["topic_id", "workspace_id"],
            ["topics.id", "topics.workspace_id"],
            name="topic_stances_topic_workspace_fkey",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["stance_id", "workspace_id"],
            ["opinion_stances.id", "opinion_stances.workspace_id"],
            name="topic_stances_stance_workspace_fkey",
            ondelete="CASCADE",
        ),
        Index("idx_topic_stances_stance", "stance_id"),
        {
            "comment": (
                "Which public-opinion stances a candidate topic is built on. "
                "A candidate with no rows here is a model prior, not a grounded topic."
            )
        },
    )

    def __repr__(self) -> str:
        return f"<TopicStance {self.topic_id}/{self.stance_id}>"


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
