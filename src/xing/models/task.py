"""tasks 表的 ORM 映射（ORM 试点，先只映射这一张表）。

为什么把约束名和索引名写死成 ``tasks_pkey`` / ``idx_tasks_pending`` 这种"难看"的名字：
0002 迁移里的 DDL 是手写 SQL，Postgres 自动生成的名字就是这些。如果这里用
``Base.metadata`` 的 naming_convention 重新推导（会得到 ``pk_tasks`` / ``ix_...``），
``alembic check`` 就会报一堆 drop/create —— 那是噪音，会掩盖真正的模型漂移。

所以规矩是：**模型侧迁就库侧**。DDL 改成什么名，这里跟什么名。
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    Uuid,
    desc,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from xing.db.base import Base


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("uuidv7()"))

    workspace_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "workspaces.id",
            ondelete="CASCADE",
            name="tasks_workspace_id_fkey",
        ),
        nullable=False,
    )

    task_type: Mapped[str] = mapped_column(Text, nullable=False)

    # 状态取值由 CHECK(tasks_status_check) 约束：
    # PENDING/RUNNING/WAITING/SUCCEEDED/FAILED/CANCELLED。
    # CHECK 没有在这里声明 —— Alembic autogenerate 不比较 CHECK，
    # 声明了反而要处理命名差异，收益为零。取值域放在 TaskStatus 常量里。
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'PENDING'"))

    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))

    # input 是保留字式的列名，但作为属性没问题：Task.input 读起来就是"任务的输入"
    input: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("idx_tasks_workspace_status", "workspace_id", "status"),
        # 部分索引：worker 领取任务时只扫 PENDING，索引体积与积压量同阶
        Index(
            "idx_tasks_pending",
            "workspace_id",
            desc("priority"),
            "created_at",
            postgresql_where=text("status = 'PENDING'"),
        ),
    )

    def __repr__(self) -> str:
        return f"<Task {self.id} {self.task_type} {self.status}>"


class TaskStatus:
    """状态常量。跟 CHECK 约束保持同步，服务层用它做流转判断，避免散落的字面量。"""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    #: 只有这三个状态可以取消。SUCCEEDED/FAILED/CANCELLED 是终态。
    CANCELLABLE = (PENDING, RUNNING, WAITING)
