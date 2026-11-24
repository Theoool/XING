"""agent_runs 的 ORM 映射。

AI 运行的最小记账：谁跑的、哪个模型、多少 token、多少钱、多久、成败。
**不存思维链**，也不存 prompt 全文 —— 需要复盘的输入放 decision_traces 的 data 里，
那边有明确的审计语义，这里只管成本与可观测性。

``cost`` 是 ``numeric(18,8)``：单次调用可能低到 1e-6 美元，精度不能省。
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
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from xing.db.base import Base


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("uuidv7()"))
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("workspaces.id", ondelete="CASCADE", name="agent_runs_workspace_id_fkey"),
        nullable=False,
    )
    # 可空：手工触发的探索性调用还没有对应任务
    task_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("tasks.id", ondelete="SET NULL", name="agent_runs_task_id_fkey"),
    )
    agent_name: Mapped[str] = mapped_column(Text, nullable=False)
    model_provider: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    cost: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False, server_default=text("0"))
    latency_ms: Mapped[int | None] = mapped_column(BigInteger)
    # RUNNING / SUCCEEDED / FAILED / CANCELLED
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'RUNNING'"))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        Index("idx_agent_runs_workspace_started", "workspace_id", text("started_at DESC")),
        Index("idx_agent_runs_task", "task_id"),
        Index(
            "idx_agent_runs_model",
            "workspace_id",
            "model_provider",
            "model_name",
            text("started_at DESC"),
        ),
        {
            "comment": (
                "Minimal AI execution accounting: model, tokens, cost, latency, status. "
                "No chain-of-thought."
            )
        },
    )

    def __repr__(self) -> str:
        return f"<AgentRun {self.agent_name} {self.model_name} {self.status}>"


class AgentRunStatus:
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
