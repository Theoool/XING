"""handler 的入参。

为什么不用 ORM 的 ``Task`` 对象，也不用 ``AsyncSession``：

- **不给 session**，handler 就无法写 ``tasks``，也就会被迫在自己的短事务里
  完成业务写入。这不是风格问题 —— 如果 handler 拿着 runner 的 session，
  一次 30 秒的 LLM 调用会让那个事务一直开着，同时造成三件事：租约回收的
  UPDATE 被行锁挡住、连接池被长事务占满、``SKIP LOCKED`` 的并发优势归零。
- **不给 ORM 对象**，handler 就不会顺手改 ``status`` 然后 commit。
  「Worker 是 tasks 状态的唯一写入者」这条规则，靠代码结构保证比靠纪律可靠。

代价是 handler 需要自己开 session 写业务数据，多两行。这个代价换的是
「Agent 不可能越权改任务状态」的结构性保证。
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from xing.models.task import Task


@dataclass(frozen=True, slots=True)
class TaskContext:
    """handler 能看到的全部任务信息。都是值，不是引用。"""

    id: UUID
    workspace_id: UUID
    task_type: str
    input: dict[str, Any]
    retry_count: int
    max_retries: int

    @classmethod
    def from_task(cls, task: Task) -> "TaskContext":
        return cls(
            id=task.id,
            workspace_id=task.workspace_id,
            task_type=task.task_type,
            input=dict(task.input or {}),
            retry_count=task.retry_count,
            max_retries=task.max_retries,
        )

    @property
    def attempt(self) -> int:
        """本次是第几次执行（1 起）。直接对应 ``agent_runs.attempt``。"""
        return self.retry_count + 1
