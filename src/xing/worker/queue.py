"""tasks 的队列操作：领取 / 续租 / 回收 / 推进 / 收尾。

全部用 ORM 表达，包括 ``FOR UPDATE SKIP LOCKED``。不是为了好看 ——
状态迁移的合法性由数据库触发器守卫，这层只负责「谁来改」和「什么时候改」，
两者分开之后，任何一处写错都会立刻被 guard 或 CHECK 拒绝。

五个操作的发起方（见 docs/AGENT_DESIGN.md §3.3）：

    领取   READY   → RUNNING   worker，持租约，lease_generation + 1
    续租   RUNNING → RUNNING   只有 locked_by 与 generation 同时匹配才能续
    回收   RUNNING → RETRYING  租约过期，retry_count + 1，lease_generation + 1
    推进   RETRYING → READY    退避到期，重新可领
    收尾   任意    → 终态      必须带 fence，见 :func:`finalize`

递归/自引用一律不用：``RETRYING`` 必须先变 ``READY`` 才可领，
「哪个状态可领」只有一个答案。

**为什么每个写操作都要 generation**：``locked_by`` 只回答「是谁」，
不回答「是第几代」。容器重启后 hostname:pid 往往完全一致，
身份相同、代次不同的两个执行者无法用身份区分。详见 0006 迁移的注释。
"""

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from xing.models.task import Task, TaskStatus

#: 租约时长。worker 崩溃后最多这么久任务会被回收。
DEFAULT_LEASE_SECONDS = 90

#: 回收后的退避。Phase 1a 用固定值；指数退避需要逐行算 retry_count，
#: 等有真实失败分布再做，现在做只是猜。
RECLAIM_BACKOFF = timedelta(seconds=10)


def _now() -> datetime:
    return datetime.now(UTC)


async def claim_one(
    session: AsyncSession,
    *,
    worker_id: str,
    agent: str | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> Task | None:
    """领取一个任务并置为 RUNNING。

    只扫 ``READY``：``RETRYING`` 由 :func:`promote_retrying` 转成 ``READY``
    之后才有资格被抢，这样不存在「两处判断谁能领」的分叉。

    ``SKIP LOCKED`` 让多个 worker 永远不会抢到同一行；配合租约，
    不需要任何分布式锁。
    """
    stmt = (
        select(Task)
        .where(
            Task.status == TaskStatus.READY,
            Task.available_at <= func.now(),
        )
        .order_by(Task.priority.desc(), Task.available_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if agent is not None:
        stmt = stmt.where(
            or_(Task.assigned_agent.is_(None), Task.assigned_agent == agent)
        )

    task = (await session.execute(stmt)).scalar_one_or_none()
    if task is None:
        return None

    now = _now()
    task.status = TaskStatus.RUNNING
    task.locked_by = worker_id
    task.locked_at = now
    task.lease_expires_at = now + timedelta(seconds=lease_seconds)
    # 在 Python 里加而不是 SET lease_generation = lease_generation + 1：
    # 行已被 FOR UPDATE 锁住，读到的值就是权威值，Python 加法结果与之等价，
    # 但属性里立刻拿到新代次，调用方不必再 SELECT 一次（async 下很贵）。
    task.lease_generation = (task.lease_generation or 0) + 1
    if task.started_at is None:
        task.started_at = now

    return task


async def heartbeat(
    session: AsyncSession,
    task_id: UUID,
    *,
    worker_id: str,
    generation: int,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    """续租。返回 False 表示租约已经不属于自己，必须立刻停手。

    三个条件缺一不可：``locked_by`` 挡别人、``status`` 挡已终态、
    ``lease_generation`` 挡「同一身份的更早代次」。少了最后一条，
    容器重启后 pid 相同的新进程会替已失效的旧代次续命。
    """
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(Task)
            .where(
                Task.id == task_id,
                Task.locked_by == worker_id,
                Task.status == TaskStatus.RUNNING,
                Task.lease_generation == generation,
            )
            .values(lease_expires_at=_now() + timedelta(seconds=lease_seconds))
        ),
    )
    await session.commit()
    return result.rowcount == 1


async def reclaim_expired(
    session: AsyncSession,
    *,
    backoff: timedelta = RECLAIM_BACKOFF,
) -> tuple[int, int]:
    """回收租约过期的任务。返回 (转入 RETRYING 数, 转入 FAILED 数)。

    分两条语句是因为重试额度决定去向，而 ``retry_count + 1`` 是否越界
    只有数据库知道（``tasks_retry_within_limit``）。交给 WHERE 分流，
    比先查再改少一次竞态。
    """
    now = _now()
    expired = (Task.status == TaskStatus.RUNNING, Task.lease_expires_at < now)

    # 额度已耗尽：直接终态，避免「永远重试」把队列堵住
    exhausted = cast(
        "CursorResult[Any]",
        await session.execute(
            update(Task)
            .where(*expired, Task.retry_count >= Task.max_retries)
            .values(
                status=TaskStatus.FAILED,
                lease_generation=Task.lease_generation + 1,
                completed_at=now,
                error={"code": "LEASE_EXPIRED", "message": "worker 租约过期且重试耗尽"},
                locked_by=None,
                locked_at=None,
                lease_expires_at=None,
            )
        ),
    )

    retryable = cast(
        "CursorResult[Any]",
        await session.execute(
            update(Task)
            .where(*expired, Task.retry_count < Task.max_retries)
            .values(
                status=TaskStatus.RETRYING,
                # 回收即换代：旧持有者的任何后续写入都必须落空。
                # 否则它可以在任务被重新执行期间把结果覆盖上去。
                lease_generation=Task.lease_generation + 1,
                retry_count=Task.retry_count + 1,
                available_at=now + backoff,
                error={"code": "LEASE_EXPIRED", "message": "worker 租约过期"},
                locked_by=None,
                locked_at=None,
                lease_expires_at=None,
            )
        ),
    )

    await session.commit()
    return retryable.rowcount, exhausted.rowcount


async def promote_retrying(session: AsyncSession) -> int:
    """退避到期的 RETRYING 转 READY，重新进入可领队列。"""
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(Task)
            .where(
                Task.status == TaskStatus.RETRYING,
                Task.available_at <= func.now(),
            )
            .values(status=TaskStatus.READY)
        ),
    )
    await session.commit()
    return result.rowcount


async def finalize(
    session: AsyncSession,
    task_id: UUID,
    *,
    worker_id: str,
    generation: int,
    status: str,
    **values: Any,
) -> bool:
    """收尾写入。返回 ``False`` 表示租约已失效，**本次结果必须丢弃**。

    这是整个队列里唯一允许把任务带到终态的入口，它同时做两件事：
    写状态、清租约。分开做会违反 ``tasks_lease_consistency``
    （``locked_by`` 与 ``lease_expires_at`` 必须同时有值或同时为空）。

    为什么不用 ORM 对象赋值后 commit：那样生成的 UPDATE 只带主键，
    会把「已经不属于自己的行」一起改掉。带上 ``lease_generation`` 之后，
    失效代次的写入是 0 行，调用方据此放弃结果 —— 这不是异常，
    是正常的并发结果，所以用返回值而不是抛异常表达。
    """
    if "status" in values:
        raise TypeError("status 是位置参数，不要在 values 里重复传")

    stmt = (
        update(Task)
        .where(
            Task.id == task_id,
            Task.locked_by == worker_id,
            Task.status == TaskStatus.RUNNING,
            Task.lease_generation == generation,
        )
        .values(
            status=status,
            locked_by=None,
            locked_at=None,
            lease_expires_at=None,
            **values,
        )
        # session 里的 Task 对象可能是过期快照（handler 改过业务数据并 commit），
        # 让 ORM 试着同步它只会引入无谓的分支，这里直接跳过。
        .execution_options(synchronize_session=False)
    )
    result = cast("CursorResult[Any]", await session.execute(stmt))
    await session.commit()
    return result.rowcount == 1
