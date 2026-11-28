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

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from xing.cache import heartbeat
from xing.core.errors import ApiError
from xing.models.task import Task, TaskStatus
from xing.schemas.task import QueueStatus, TaskCreate, TaskOut

#: 队列里「等人来领」与「正在跑」的分界。PENDING 是在等前置任务，
#: 它没在等人，但也不在执行 —— 两条都不是「执行中」。
_UNCLAIMED = (TaskStatus.PENDING, TaskStatus.READY)
_RUNNING = (TaskStatus.RUNNING, TaskStatus.RETRYING)


def _to_out(task: Task) -> TaskOut:
    """ORM 对象 -> 响应模型。

    from_attributes=True 让 Pydantic 按属性名取值。这里没有关系属性参与，
    所以不会触发懒加载（async 下的 MissingGreenlet 只会在访问未加载的关系时出现）。
    """
    return TaskOut.model_validate(task, from_attributes=True)


async def create_task(session: AsyncSession, *, workspace_id: UUID, payload: TaskCreate) -> TaskOut:
    """创建任务。带 ``dedup_key`` 时幂等 —— 同键第二次提交返回既有行。

    为什么幂等必须在这里而不是在调用方：``plan_content`` 会一次性插入 3-4 个
    子任务，它在插入与收尾之间崩溃时租约会被回收、任务会重跑。若创建不幂等，
    重跑就再插一套子任务，同一个 goal 长出两条生产线 —— 两篇内容、两份成本，
    而且没有任何报错。把去重压到唯一索引上，「计划只展开一次」才是数据库事实，
    不依赖调用方记得先查。
    """
    values = {
        "workspace_id": workspace_id,
        "task_type": payload.task_type,
        "priority": payload.priority,
        "input": payload.input,
        "depends_on": payload.depends_on,
        "assigned_agent": payload.assigned_agent,
        "dedup_key": payload.dedup_key,
    }

    if payload.dedup_key is None:
        task = Task(**values)
        session.add(task)
        # flush 触发 INSERT ... RETURNING，把 uuidv7()、now() 这些服务端默认值取回来。
        await session.flush()
        # 初始状态**不在这里决定**。触发器 trg_tasks_promote_on_insert 会看
        # depends_on：无未满足依赖就直接推到 READY，否则留在 PENDING。
        # 必须 refresh：INSERT ... RETURNING 在 AFTER 触发器之前求值，
        # 触发器把状态改成 READY 这件事只有重新 SELECT 才看得见。
        await session.refresh(task)
        await session.commit()
        return _to_out(task)

    inserted = await session.scalar(
        pg_insert(Task)
        .values(**values)
        .on_conflict_do_nothing(
            index_elements=["workspace_id", "dedup_key"],
            index_where=text("dedup_key IS NOT NULL"),
        )
        .returning(Task.id)
    )
    if inserted is None:
        task = (
            await session.execute(
                select(Task).where(
                    Task.workspace_id == workspace_id,
                    Task.dedup_key == payload.dedup_key,
                )
            )
        ).scalar_one()
    else:
        # 重新 SELECT：RETURNING 在 AFTER 触发器之前求值，状态可能是旧的。
        task = (await session.execute(select(Task).where(Task.id == inserted))).scalar_one()
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


async def queue_status(session: AsyncSession, *, workspace_id: UUID) -> QueueStatus:
    """这个工作区的队列现在是什么样，以及有没有人在消费它。

    任务列表只能回答「每条任务是什么状态」，回答不了「为什么它一直不动」。
    这一步把两个事实并排放：队列里堆了多少，以及活着的 worker 有几个。
    两者相加才是可诊断的状态 —— 没有 worker 时，``READY`` 不是「即将执行」，
    而是「永远不执行」。
    """
    counts = dict(
        (
            await session.execute(
                select(Task.status, func.count())
                .where(
                    Task.workspace_id == workspace_id,
                    Task.status.in_(_UNCLAIMED + _RUNNING),
                )
                .group_by(Task.status)
            )
        ).all()
    )
    queued = sum(counts.get(status, 0) for status in _UNCLAIMED)
    running = sum(counts.get(status, 0) for status in _RUNNING)

    oldest = (
        await session.execute(
            select(func.min(Task.created_at)).where(
                Task.workspace_id == workspace_id,
                Task.status.in_(_UNCLAIMED),
            )
        )
    ).scalar_one_or_none()
    oldest_wait_s = None
    if oldest is not None:
        oldest_wait_s = max(0, int((datetime.now(UTC) - oldest).total_seconds()))

    workers = await heartbeat.live()
    alive = None if workers is None else len(workers)
    return QueueStatus(
        workers_online=alive,
        queued=queued,
        running=running,
        oldest_wait_s=oldest_wait_s,
        # 读不到心跳时（alive is None）不冒充结论：宁可不说，也不要让人去重启一个正常的进程。
        #
        # ``running > 0`` 也要算：租约回收（``reclaim_expired``）同样长在 worker
        # 的主循环里，worker 全掉线时那条 RUNNING 任务没有任何人会回收，会永远
        # 停在执行中 —— 它看起来最像「在跑」，实际上是最不该等的一种。
        stalled=alive == 0 and (queued > 0 or running > 0),
    )


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
