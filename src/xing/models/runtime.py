"""Agent 运行时的两张表：``agent_runs``（一次执行）与 ``llm_calls``（一次模型调用）。

这两个概念**必须分开**，混用是这类系统最常见的错误来源：

- ``agent_runs`` 回答「这次执行的目标是什么、计划是什么、花了多少」。
  有 ``goal``（必填，审计时唯一能解释"为什么跑这一趟"的字段）、``plan``、
  ``autonomy_mode``、三层预算（步数 / token / 金额）与累计花费。
- ``llm_calls`` 回答「这一步用了哪个模型、多少 token、多少钱、返回什么」。
  是成本的**唯一明细源**；``agent_runs.total_*`` 只是它的事务内累加快照，
  由触发器 ``app.rollup_llm_call_cost`` 维护，应用层不直接写。

预算拦截**不是**「先花钱再记账然后靠 CHECK 反悔」。LLM 调用在数据库事务
之外，CHECK 拦得住记账、拦不住花钱；越界时触发器回滚掉的正是那条明细，
结果是供应商收了钱而库里查无此账。

所以用「预留 + 结算」两段（0007）：

    预留  UPDATE ... SET reserved_cost = reserved_cost + :上界
              WHERE total_cost + reserved_cost + :上界 <= max_cost
    调用  在事务之外，不持有任何连接
    结算  释放预留 → 插入 llm_calls（触发器累加 total_cost）

只要预留额是真实花费的上界（按模型单价 × 最大输出算），结算时
``total_cost + reserved_cost <= max_cost`` 必然仍成立，永远不会触发 23514。
预算因此只在「能不能开始这一步」这一个点被判定，且是原子的一条 UPDATE。

``cost`` 是 ``numeric(18,8)``：单次调用可能低到 1e-6 美元，精度不能省。

关于 ``request`` / ``response``：存**脱敏后的**完整往返，不存思维链。
它是录制回放的燃料，也是 debug 时唯一的一手证据。``replay_key`` 唯一索引
保证同一请求只录一次。
"""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from xing.db.base import Base


class AgentRun(Base):
    """一次 Agent 执行。对应 docs/AGENT_DESIGN.md §2.3。"""

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
    # 子 Agent（如 Review 核查）挂在父 run 下。RESTRICT 不是保守，是必须：
    # 级联删会把子 run 的审计一起带走，而审计的全部价值就在事后能查。
    parent_run_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("agent_runs.id", ondelete="RESTRICT", name="agent_runs_parent_run_id_fkey"),
    )

    agent_name: Mapped[str] = mapped_column(Text, nullable=False)
    #: 同一 task 的第几次执行（= tasks.retry_count + 1）。**run 级**，
    #: 与调用次数无关 —— 调用级的序号是 LlmCall.call_seq。
    attempt: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))

    goal: Mapped[str] = mapped_column(Text, nullable=False)
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # SUGGEST / SEMI_AUTO / AUTO，取值由 CHECK 约束
    autonomy_mode: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'SEMI_AUTO'")
    )

    # RUNNING / SUCCEEDED / FAILED / CANCELLED
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'RUNNING'"))

    # ---------------- 三层预算 ----------------
    # 用独立数值列而不是 budget jsonb：JSONB 里的数值加不了 CHECK，
    # 等于放弃数据库层拦截，而这三项恰好是最需要硬拦截的。
    max_steps: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("20"))
    step_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("200000")
    )
    total_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    max_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, server_default=text("2.0")
    )
    total_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, server_default=text("0")
    )
    #: 已预留但未结算的额度。调用前占、结算时释放，保证「先花钱后记账」
    #: 不会把预算撑破；同一 run 并发多步时也不会各自看到同一个余额。
    reserved_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, server_default=text("0")
    )

    # 结构化结果（内容 id、引用 id、判定结论），**不存正文**。
    # 正文归 contents / content_versions，否则同一份文本会散落三处。
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index(
            "agent_runs_task_attempt",
            "task_id",
            "attempt",
            unique=True,
            postgresql_where=text("task_id IS NOT NULL"),
        ),
        Index("idx_agent_runs_workspace_started", "workspace_id", text("started_at DESC")),
        Index("idx_agent_runs_task", "task_id"),
        Index("idx_agent_runs_parent", "parent_run_id"),
        # 复合外键的被引用侧。只为满足「被引用列组必须唯一」这条语法要求，
        # 但它把「run 属于哪个租户」变成了可被 llm_calls 引用的既有事实。
        UniqueConstraint("id", "workspace_id", name="agent_runs_id_workspace_key"),
        {"comment": ("One agent execution: goal, plan, autonomy mode, three-layer budget.")},
    )

    def __repr__(self) -> str:
        return f"<AgentRun {self.agent_name} {self.status} {self.step_count}/{self.max_steps}>"


class LlmCall(Base):
    """一次模型调用。由 ``agent_runs`` 改名而来（0005）。

    建表顺序上先有它、后有 run，是历史遗留；语义上它属于 run。
    """

    __tablename__ = "llm_calls"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("uuidv7()"))
    #: 两个列都写，但真正的租户保证来自 __table_args__ 里的复合外键：
    #: (run_id, workspace_id) 必须先在 agent_runs 里成对存在。
    workspace_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)

    #: plan / tool_decision / generate / self_check / repair。无默认值 ——
    #: 归类是调用方的责任，数据库不猜。
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    #: run 级：第几次执行。与 agent_runs.attempt 相同，冗余在此便于按调用聚合。
    attempt: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    step_no: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    #: run 内调用序号，从 1 递增。同一 step 同一 purpose 重试两次也不会撞键 ——
    #: 这正是旧唯一键 (run_id, step_no, purpose, attempt) 的错误假设。
    call_seq: Mapped[int] = mapped_column(Integer, nullable=False)

    model_provider: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str] = mapped_column(Text, nullable=False)

    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    cost: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False, server_default=text("0"))
    latency_ms: Mapped[int | None] = mapped_column(BigInteger)

    # RUNNING / SUCCEEDED / FAILED / CANCELLED / REPLAYED
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'RUNNING'"))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    request: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: sha256(model_name ‖ request ‖ prompt_version)，回放时按它命中
    replay_key: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # 真正的租户绑定：run 必须属于这个 workspace，否则插不进去。
        # worker 是 BYPASSRLS，写错 workspace_id 不会被策略挡住，只有这条外键能拦。
        ForeignKeyConstraint(
            ["run_id", "workspace_id"],
            ["agent_runs.id", "agent_runs.workspace_id"],
            name="llm_calls_run_workspace_fkey",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="llm_calls_workspace_id_fkey",
            ondelete="CASCADE",
        ),
        Index("llm_calls_call_seq", "run_id", "call_seq", unique=True),
        # 带租户的 replay 键。全局唯一会让 B 租户撞上 A 已录制的键、
        # 命中后读到别人的响应 —— 这是数据泄漏，不只是键冲突。
        #
        # 只约束 status='SUCCEEDED' 的行：一次回放命中会插入第二行同键记录，
        # 若把 REPLAYED 也算进来，回放自己就会撞键。失败调用没有可用响应，
        # 本来就该允许重新录制。
        Index(
            "llm_calls_replay_key",
            "workspace_id",
            "replay_key",
            unique=True,
            postgresql_where=text("replay_key IS NOT NULL AND status = 'SUCCEEDED'"),
        ),
        Index("idx_llm_calls_run", "run_id", "started_at"),
        Index("idx_llm_calls_run_started", "workspace_id", text("started_at DESC")),
        Index(
            "idx_llm_calls_model",
            "workspace_id",
            "model_provider",
            "model_name",
            text("started_at DESC"),
        ),
        {"comment": ("One model call. Detail source of cost; agent_runs.total_* rolls it up.")},
    )

    def __repr__(self) -> str:
        return f"<LlmCall {self.model_name} {self.purpose} {self.status}>"


class AgentRunStatus:
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    #: 终态。RUNNING 之外都要求 completed_at 非空（CHECK 约束）。
    TERMINAL = (SUCCEEDED, FAILED, CANCELLED)


class LlmCallStatus:
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    #: 回放命中，未发生真实调用，cost 记 0
    REPLAYED = "REPLAYED"


class AutonomyMode:
    """自治级别。决定哪些动作需要人工批准。

    SUGGEST   每步都要批
    SEMI_AUTO 只有对外动作（发布、第三方写接口）要批
    AUTO      内部动作自主；对外动作**仍然**要批（这是硬规则，不随模式变）
    """

    SUGGEST = "SUGGEST"
    SEMI_AUTO = "SEMI_AUTO"
    AUTO = "AUTO"


class LlmPurpose:
    """模型调用的用途。用于按用途拆成本。

    **没有 ``step``。** 这个常量曾经是表默认值，掩盖了「一次调用算不算一个
    step」的歧义：触发器按它 +1、Runtime 也 +1，结果要么 0 要么 2。
    step 是「一次决策循环」，由 Runtime 在循环开始时递增，与调用次数无关。
    """

    PLAN = "plan"
    TOOL_DECISION = "tool_decision"
    GENERATE = "generate"
    ANALYZE = "analyze"
    SELF_CHECK = "self_check"
    REPAIR = "repair"
