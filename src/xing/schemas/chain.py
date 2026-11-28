"""选题链路的派生状态。

这条链横跨七环、五个 task_type 和三张业务表，但前端只需要回答两个问题：
「现在走到哪」「这一步能不能点」。把答案放在一个派生端点里，
前端就不必再靠 ``tasks 列表 + task_type + input.topic_id`` 去猜 ——
猜法在任务一多时会漏，在失败时会给错。
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

#: 链路相位。和 docs/TOPIC_PIPELINE.md §1 的七环一一对应。
ChainPhase = Literal[
    "EXPANDING",
    "CHECKING",
    "SCORING",
    "AWAITING_SELECTION",
    "DEEPENING",
    "WRITING",
    "REVIEWING",
    "DONE",
    "FAILED",
    "REJECTED",
]

#: 单步状态。WAITING = 已排产待领取，PENDING = 还没排产。
StepStatus = Literal[
    "PENDING",
    "WAITING",
    "QUEUED",
    "RUNNING",
    "RETRYING",
    "DONE",
    "FAILED",
    "CANCELLED",
    "SKIPPED",
]

#: 可重跑的环节。expand 不能重跑（会换掉整批候选），opinion/score 重跑没有意义。
RetryStep = Literal["deepen", "write", "review"]


class ChainStep(BaseModel):
    key: str
    label: str
    status: StepStatus
    task_id: UUID | None = None
    error: dict | None = None
    #: 只有 FAILED / CANCELLED 的任务可以重跑；前端据此显示按钮。
    retryable: bool = False
    #: WAITING（已排产、还没被领取）时已等了多久。其余状态为 null。
    #: 它是「有人会来领」和「永远没人来领」的唯一区分线索，所以由后端算。
    waiting_s: int | None = None


class ChainReview(BaseModel):
    verdict: Literal["PASS", "FAIL"]
    score: float
    issues: list[str] = Field(default_factory=list)


class ChainStatus(BaseModel):
    topic_id: UUID
    seed_task_id: UUID | None
    phase: ChainPhase
    steps: list[ChainStep]
    content_id: UUID | None = None
    review: ChainReview | None = None
    #: 能不能点「选定」。false 时 reason 给出机器可读的原因。
    can_select: bool = False
    select_blocked_reason: str | None = None
    #: 失败发生在哪一步（phase=FAILED 时非空）。
    failed_step: str | None = None
    #: 存活 worker 数。0 表示没有进程在消费队列：这时 WAITING 不会自己变成
    #: RUNNING，界面必须说出来，而不是继续转圈。None = 读不到（Redis 不可用）。
    workers_online: int | None = None


class ChainRetry(BaseModel):
    step: RetryStep
