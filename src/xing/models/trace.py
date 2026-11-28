"""``retrieval_traces`` 的 ORM 映射。

它是**诊断数据**，不是业务状态：任务失败、重试、被取消都不删它，因为它记录的
正是「那一刻到底发生了什么」。写入只增不改（``attempt`` 区分重试），
所以模型里没有 update 相关的约定需要遵守 —— 插进去就别动。
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from xing.db.base import Base


class RetrievalTrace(Base):
    __tablename__ = "retrieval_traces"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("uuidv7()"))
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "workspaces.id",
            ondelete="CASCADE",
            name="retrieval_traces_workspace_id_fkey",
        ),
        nullable=False,
    )
    task_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    #: 展开时的 run。深入/写作暂时不写检索轨迹，所以可能为空。
    run_id: Mapped[UUID | None] = mapped_column(Uuid)
    attempt: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    #: 0 = 首轮，1 = 补采。补采最多一轮，但保留字段以便以后放开。
    round: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    #: 本轮内的第几条 query。展示顺序按它，不按 created_at（并发时时间会交错）。
    seq: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    # MEDIA / DISCUSSION / SUPPLEMENT
    layer: Mapped[str] = mapped_column(Text, nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    #: 规划阶段给的检索意图（DISPUTE / OPPOSE / VOICE ...）。模板生成的没有。
    target: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    #: 真正下发的参数，不是调用方以为的参数。
    options: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # OK / EMPTY / ERROR
    status: Mapped[str] = mapped_column(Text, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    result_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    #: 去重与限额之后真正进入材料池的条数。
    new_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    #: 每条文档：rank / title / url / domain / source_kind / kind_rule /
    #: published_at / kept / drop_reason。只给读，不做查询条件。
    docs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            name="retrieval_traces_task_workspace_fkey",
            ondelete="CASCADE",
        ),
        # 同一任务同一次重试的第 N 轮第 M 条只该有一行：重跑幂等，不会翻倍。
        UniqueConstraint(
            "task_id",
            "attempt",
            "round",
            "seq",
            name="retrieval_traces_attempt_seq_key",
        ),
        Index("idx_retrieval_traces_task", "task_id", "round", "seq"),
        {
            "comment": (
                "One row per executed search: the params actually sent, what came "
                "back, and why each document was kept or dropped. Diagnostic data, "
                "not business state."
            )
        },
    )


__all__ = ["RetrievalTrace"]
