"""decisions / decision_traces 的 ORM 映射。

``decisions.strategy_version_id`` 没有默认值也没有触发器，它由服务层调用
``app.current_strategy_version(strategy_id)`` 解析后写入。方向是「服务端决定」，
客户端在请求体里连这个字段都没有 —— 否则前端可以永远传 v1，
三个月后审计会发现所有决策都声称基于初版战略，而实际执行的是 v3。

``traces`` 用 ``lazy="selectin"``：决策详情与列表都要带证据，
selectin 是「一条主查询 + 一条 IN 查询」，不会像 join 那样把主表行放大。
顺序按 created_at，与老 SQL 里的 ``json_agg(... order by t.created_at)`` 一致。
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from xing.db.base import Base


class Decision(Base):
    __tablename__ = "decisions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("uuidv7()"))
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("workspaces.id", ondelete="CASCADE", name="decisions_workspace_id_fkey"),
        nullable=False,
    )
    strategy_version_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "strategy_versions.id",
            ondelete="RESTRICT",
            name="decisions_strategy_version_id_fkey",
        ),
        nullable=False,
    )
    topic_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("topics.id", ondelete="SET NULL", name="decisions_topic_id_fkey"),
    )
    # TOPIC_SELECTION / ANGLE_SELECTION / CONTENT_DIRECTION / PUBLISHING / STRATEGY
    decision_type: Mapped[str] = mapped_column(Text, nullable=False)
    # PROPOSED / APPROVED / REJECTED / EXECUTED / SUPERSEDED
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'PROPOSED'"))
    selected_option: Mapped[str | None] = mapped_column(Text)
    rationale: Mapped[str | None] = mapped_column(Text)
    score: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    decided_by: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="SET NULL", name="decisions_decided_by_fkey"),
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    traces: Mapped[list[Any]] = relationship(
        "DecisionTrace",
        order_by="DecisionTrace.created_at",
        lazy="selectin",
        viewonly=True,
    )

    __table_args__ = (
        Index("idx_decisions_workspace_created", "workspace_id", text("created_at DESC")),
        Index("idx_decisions_topic", "topic_id"),
        Index("idx_decisions_strategy_version", "strategy_version_id"),
        {
            "comment": (
                "Business decisions made by human or AI, bound to an exact strategy version."
            )
        },
    )

    def __repr__(self) -> str:
        return f"<Decision {self.decision_type} {self.status}>"


class DecisionTrace(Base):
    """结构化证据。**不存思维链** —— 存的是可复核的输入与打分，不是推理原文。"""

    __tablename__ = "decision_traces"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("uuidv7()"))
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("workspaces.id", ondelete="CASCADE", name="decision_traces_workspace_id_fkey"),
        nullable=False,
    )
    decision_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("decisions.id", ondelete="CASCADE", name="decision_traces_decision_id_fkey"),
        nullable=False,
    )
    # EVIDENCE / SCORE / RULE / ALTERNATIVE / CONSTRAINT
    trace_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str | None] = mapped_column(Text)
    source_id: Mapped[UUID | None] = mapped_column(Uuid)
    data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("idx_decision_traces_decision", "decision_id"),
        {
            "comment": (
                "Structured evidence and scoring records for decisions. "
                "Never store chain-of-thought."
            )
        },
    )

    def __repr__(self) -> str:
        return f"<DecisionTrace {self.trace_type}>"


class DecisionType:
    TOPIC_SELECTION = "TOPIC_SELECTION"
    ANGLE_SELECTION = "ANGLE_SELECTION"
    CONTENT_DIRECTION = "CONTENT_DIRECTION"
    PUBLISHING = "PUBLISHING"
    STRATEGY = "STRATEGY"


class DecisionStatus:
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    SUPERSEDED = "SUPERSEDED"


class TraceType:
    EVIDENCE = "EVIDENCE"
    SCORE = "SCORE"
    RULE = "RULE"
    ALTERNATIVE = "ALTERNATIVE"
    CONSTRAINT = "CONSTRAINT"
