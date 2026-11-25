"""任务服务（ORM 试点）。

这一版把原来 3 条手写 SQL 全部换成了 SQLAlchemy 2.0 的 ORM 写法，目的不是"少打字"，
而是把三件以前靠人记的事情交给类型系统和模型：

1. 列名 —— 以前 ``select id, task_type, ...`` 写错一个字母，只有跑到那行才炸；
   现在 ``Task.task_type`` 由 IDE 和 mypy 检查。
2. 行形状 —— 以前要手工把 RowMapping 拼成响应结构；现在 ``TaskOut.model_validate``
   直接读属性，字段改名只改一处。
3. JSONB 绑定 —— 以前 ``jsonb_build_object('reason', :reason)`` 因为没有类型上下文，
   asyncpg 传 unknown 导致 Postgres 报 42P18；现在 ``task.error = {...}`` 由
   ``mapped_column(JSONB)`` 负责序列化，这类错误在类型层面就不存在了。

RLS 不受影响：上下文注入挂在 ``Session`` 类的 after_begin 事件上，
ORM 走的还是同一个 session，SQL 仍然被策略过滤。

仍然没有 worker：任务会停在 PENDING。cancel 只改数据库状态。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from xing.core.errors import ApiError
from xing.models.task import Task, TaskStatus
from xing.schemas.task import TaskCreate, TaskOut


def _to_out(task: Task) -> TaskOut:
    """ORM 对象 -> 响应模型。

    from_attributes=True 让 Pydantic 按属性名取值。这里没有关系属性参与，
    所以不会触发懒加载（async 下的 MissingGreenlet 只会在访问未加载的关系时出现）。
    """
    return TaskOut.model_validate(task, from_attributes=True)


async def create_task(session: AsyncSession, *, workspace_id: UUID, payload: TaskCreate) -> TaskOut:
    task = Task(
        workspace_id=workspace_id,
        task_type=payload.task_type,
        priority=payload.priority,
        input=payload.input,
    )
    session.add(task)

    # flush 触发 INSERT ... RETURNING，把 uuidv7()、now() 这些服务端默认值取回来。
    # 不 flush 直接 commit 也可以，但显式 flush 让"默认值已就绪"这件事在代码里可见。
    await session.flush()

    # 初始状态**不在这里决定**。触发器 trg_tasks_promote_on_insert 会看
    # depends_on：无未满足依赖就直接推到 READY，否则留在 PENDING。
    # 因此不能像以前那样写死 status=PENDING —— 那会和触发器打架，
    # 且 PENDING→READY 要多走一次 UPDATE。
    #
    # 必须 refresh：INSERT ... RETURNING 在 AFTER 触发器之前求值，
    # 触发器把状态改成 READY 这件事只有重新 SELECT 才看得见。
    await session.refresh(task)

    await session.commit()
    return _to_out(task)


async def get_task(session: AsyncSession, *, workspace_id: UUID, task_id: UUID) -> TaskOut:
    task = (
        await session.execute(
            select(Task).where(Task.id == task_id, Task.workspace_id == workspace_id)
        )
    ).scalar_one_or_none()

    if task is None:
        # 不存在与不属于本工作区共用 404：不泄露"这个 id 是否存在"。
        raise ApiError(404, "TASK_NOT_FOUND", "任务不存在")
    return _to_out(task)


async def list_tasks(
    session: AsyncSession, *, workspace_id: UUID, status: str | None, limit: int
) -> list[TaskOut]:
    stmt = (
        select(Task)
        .where(Task.workspace_id == workspace_id)
        .order_by(Task.created_at.desc())
        .limit(limit)
    )
    # 以前是字符串拼 " and status = :status"，现在条件是按需叠加的表达式
    if status:
        stmt = stmt.where(Task.status == status)

    tasks = (await session.execute(stmt)).scalars().all()
    return [_to_out(task) for task in tasks]


async def cancel_task(
    session: AsyncSession, *, workspace_id: UUID, task_id: UUID, reason: str | None
) -> TaskOut:
    task = (
        await session.execute(
            select(Task)
            .where(Task.id == task_id, Task.workspace_id == workspace_id)
            .with_for_update()
        )
    ).scalar_one_or_none()

    if task is None:
        raise ApiError(404, "TASK_NOT_FOUND", "任务不存在")
    if task.status not in TaskStatus.CANCELLABLE:
        # 以前靠 "update ... where status in (...)" 影响 0 行、再补一次查询来分辨
        # 404 还是 409；现在锁住行之后直接看得见状态，判断是直白的。
        raise ApiError(409, "TASK_NOT_CANCELLABLE", "任务已结束，无法取消")

    task.status = TaskStatus.CANCELLED
    task.completed_at = func.now()
    task.error = {"reason": reason, "cancelled_by": "user"}

    await session.commit()

    # completed_at 赋的是 SQL 表达式（now()），提交后属性里仍是表达式对象，
    # 直接交给 Pydantic 会校验失败。refresh 一次把它换成真实时间。
    await session.refresh(task)
    return _to_out(task)
