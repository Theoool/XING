"""任务：后端异步执行的统一入口。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class TaskCreate(BaseModel):
    task_type: str = Field(min_length=1, max_length=50)
    priority: int = Field(default=0, ge=-100, le=100)
    input: dict[str, Any] = Field(default_factory=dict)
    #: 前置任务。全部 SUCCEEDED 后由触发器推进到 READY，不需要轮询。
    depends_on: list[UUID] = Field(default_factory=list)
    assigned_agent: str | None = Field(default=None, max_length=50)
    #: 幂等键。同键重复提交返回既有任务，不会产生第二条生产线。
    dedup_key: str | None = Field(default=None, min_length=1, max_length=200)


class TaskOut(BaseModel):
    id: UUID
    task_type: str
    status: str
    priority: int
    input: dict[str, Any]
    output: dict[str, Any] | None
    error: dict[str, Any] | None
    depends_on: list[UUID]
    dedup_key: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class QueueStatus(BaseModel):
    """队列的可观察面。

    队列是拉模型，任务停在 ``READY`` 有两种截然不同的原因：队列前面有人排队，
    或者根本没人来领。光看任务列表分不出这两者，所以把「有几个 worker 活着」
    和「最早一条等了多久」放在一起给出来 —— ``stalled`` 就是它们的结论。
    没有心跳时还挂在 ``RUNNING`` 的任务同理：执行者已经不在了，只是还没人收尸。
    """

    #: 存活 worker 数（Redis 心跳，30 秒过期）。0 表示没有进程在消费队列；
    #: null 表示心跳读不到（Redis 不可用）—— 这时 `stalled` 不下结论。
    workers_online: int | None
    #: 已排产待领取 / 等待依赖的任务数。
    queued: int
    #: 正在执行的任务数。
    running: int
    #: 最早一条未被领取的任务等了多久（秒）。没有排队任务时为 null。
    oldest_wait_s: int | None
    #: 有人在排队或卡在执行中，却没有 worker 在跑 —— 这些任务不会自己往前走。
    #: 排队的那批没人领；执行中的那批则是执行者已经死了，而租约回收也长在
    #: worker 的主循环里，所以同样没人收尸。
    stalled: bool
