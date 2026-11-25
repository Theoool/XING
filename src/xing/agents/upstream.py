"""上游结果的读取。

任务图里下游怎么拿到上游的产出？只有一个答案：**读 ``tasks.output``，
而 ``output`` 里只放指针**（content_id / event_ids / 评分），正文与证据留在
业务表。三条理由：

1. 正文进 ``output`` 会在 ``tasks`` / ``contents`` / ``content_versions`` 留
   三份副本，而 ``tasks.output`` 会被任务列表接口直接返回给前端。
2. 业务表有状态机、版本号、审批链；JSONB 里的一份快照没有，也不该有。
3. 指针可以反查，快照不能 —— 事后想确认「写作时看到的调研结论是哪一版」，
   只有指向业务表的 id 才答得上来。

依赖是否满足由数据库推进，不在这里判断。但这里仍然校验一次：如果真出现
「依赖没成功却被执行」，那是队列层的 bug，继续跑只会把错误数据写进业务表。
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select

from xing.core.errors import NonRetryableError
from xing.db.worker import WorkerSession
from xing.models.task import Task, TaskStatus
from xing.worker.context import TaskContext


@dataclass(frozen=True, slots=True)
class Upstream:
    task_id: UUID
    task_type: str
    output: dict[str, Any]


async def load(task: TaskContext) -> tuple[Upstream, ...]:
    """读取全部前置任务的输出。顺序不保证 —— 依赖是集合，不是序列。"""
    if not task.depends_on:
        return ()

    async with WorkerSession() as session:
        rows = (
            await session.execute(
                select(Task.id, Task.task_type, Task.status, Task.output).where(
                    Task.id.in_(task.depends_on),
                    Task.workspace_id == task.workspace_id,
                )
            )
        ).all()

    if len(rows) != len(task.depends_on):
        raise NonRetryableError(
            f"依赖任务缺失：期望 {len(task.depends_on)} 个，实际 {len(rows)} 个",
            code="UPSTREAM_MISSING",
        )

    upstream: list[Upstream] = []
    for task_id, task_type, status, output in rows:
        if status != TaskStatus.SUCCEEDED:
            raise NonRetryableError(
                f"依赖任务未成功（{task_type} status={status}）", code="UPSTREAM_NOT_READY"
            )
        upstream.append(Upstream(task_id=task_id, task_type=task_type, output=dict(output or {})))
    return tuple(upstream)


def pick(upstream: tuple[Upstream, ...], task_type: str) -> dict[str, Any]:
    """按类型取一个上游输出。同类型多个时取第一个 —— 现有链路里不存在这种图。"""
    for item in upstream:
        if item.task_type == task_type:
            return item.output
    raise NonRetryableError(f"上游缺少 {task_type} 任务的输出", code="UPSTREAM_MISSING")


__all__ = ["Upstream", "load", "pick"]
