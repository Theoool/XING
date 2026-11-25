"""tasks 表的 ORM 映射 —— 可被 worker 领取的执行单元。

``tasks`` 只回答「这件事该谁做、做完没」。一次 Agent 执行（goal / plan / 预算）
在 ``agent_runs``，一次模型调用在 ``llm_calls``。三者粒度不同，不合并。

为什么把约束名和索引名写死成 ``tasks_pkey`` / ``idx_tasks_claimable`` 这种"难看"的名字：
0002/0005 迁移里的 DDL 是手写 SQL，Postgres 自动生成的名字就是这些。如果这里用
``Base.metadata`` 的 naming_convention 重新推导（会得到 ``pk_tasks`` / ``ix_...``），
``alembic check`` 就会报一堆 drop/create —— 那是噪音，会掩盖真正的模型漂移。

所以规矩是：**模型侧迁就库侧**。DDL 改成什么名，这里跟什么名。
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    Uuid,
    desc,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
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

    # 状态取值由 CHECK(tasks_status_check) 约束，合法迁移由触发器
    # app.guard_task_status_transition 守卫（见 0005 迁移）。
    # CHECK 没有在这里声明 —— Alembic autogenerate 不比较 CHECK，
    # 声明了反而要处理命名差异，收益为零。取值域放在 TaskStatus 常量里。
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'PENDING'"))

    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))

    # ---------------- 派发与调度 ----------------
    # NULL = 任意 worker 可领；非空 = 只有该 Agent 能领
    assigned_agent: Mapped[str | None] = mapped_column(Text)

    # 前置任务 id。刻意不加 FK：依赖是同 workspace 内的软引用，
    # 且任务只被 CANCELLED 不删除，悬空风险为零。GIN 索引支撑 @> 反查。
    depends_on: Mapped[list[UUID]] = mapped_column(
        ARRAY(Uuid), nullable=False, server_default=text("'{}'::uuid[]")
    )

    #: 计划幂等键。同一 workspace 内同键只允许存在一行。
    #:
    #: 存在的唯一理由：``plan_content`` 因租约丢失重跑时会再插一套子任务，
    #: 而租约回收是常态路径，不是异常。没有这条唯一约束，同一个 goal 会长出
    #: 两条生产线 —— 两篇内容、两份成本、两套审批，且都没有报错。
    dedup_key: Mapped[str | None] = mapped_column(Text)

    retry_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    max_retries: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("3"))

    # 退避到期时间。RETRYING 到期后转 READY 才可被领取。
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    # 租约三件套：worker 崩溃后靠 lease_expires_at 过期回收，
    # 不依赖进程存活探测。locked_by 与 lease_expires_at 必须同时有值或同时为空。
    locked_by: Mapped[str | None] = mapped_column(Text)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # 租约代次（fencing token）。每次领取 / 回收都 +1。
    #
    # 「谁持有租约」不能只靠 locked_by 判断：它是身份，不是代次。身份相同的
    # 两个执行者（容器重启后 hostname:pid 往往完全一致）无法区分，而代次可以。
    # 所有状态写入都必须带 ``WHERE lease_generation = :gen``，旧代次的写入
    # 影响 0 行 —— 这是唯一能挡住「A 卡住、B 接管、A 恢复后覆盖」的机制。
    lease_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )

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
        # 部分索引：领取时只扫 READY，索引体积与可领任务量同阶，
        # 不受历史任务堆积影响。排序键用 available_at 而非 created_at，
        # 这样退避重试的任务不会插队。
        Index(
            "idx_tasks_claimable",
            "workspace_id",
            desc("priority"),
            "available_at",
            postgresql_where=text("status = 'READY'"),
        ),
        Index("idx_tasks_depends_on", "depends_on", postgresql_using="gin"),
        # 部分唯一索引：绝大多数任务没有 dedup_key，不该进索引。
        Index(
            "tasks_dedup_key",
            "workspace_id",
            "dedup_key",
            unique=True,
            postgresql_where=text("dedup_key IS NOT NULL"),
        ),
    )

    def __repr__(self) -> str:
        return f"<Task {self.id} {self.task_type} {self.status}>"


class TaskStatus:
    """状态常量。跟 CHECK 约束保持同步，服务层用它做流转判断，避免散落的字面量。

    合法迁移由 ``app.guard_task_status_transition`` 在数据库层守卫，
    这里的 ``*_FROM`` 元组只用于服务层的可读性判断，不是权威。
    """

    PENDING = "PENDING"  # 已创建，依赖未满足
    READY = "READY"  # 依赖已满足，可被领取
    RUNNING = "RUNNING"  # 已被 worker 领取并持有租约
    WAITING = "WAITING"  # 等人工审批
    RETRYING = "RETRYING"  # 失败待重试，等退避到期
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    #: 非终态。终态是 SUCCEEDED / FAILED / CANCELLED，不可再变。
    CANCELLABLE = (PENDING, READY, RUNNING, WAITING, RETRYING)

    #: 可被 worker 领取的唯一状态。RETRYING 必须先转 READY。
    CLAIMABLE = (READY,)
