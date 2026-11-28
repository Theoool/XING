"""单个任务的执行：handler + 心跳 + 状态落库。

四条不变量：

1. **只有持有效租约代次的 worker 能改状态**。收尾一律走 :func:`queue.finalize`，
   它把 ``lease_generation`` 写进 WHERE；0 行影响意味着「结果作废」，
   不是错误。
2. **失败分两种**。有重试额度 → ``RETRYING`` + 退避；耗尽 → ``FAILED``。
   两种都必须清空租约，否则任务永远显示「有人在跑」。
3. **状态由 worker 写，Agent 不写**。handler 负责业务数据（contents 等），
   任务状态只在这里改。
4. **handler 不碰 Task 对象**。它拿到的是 :class:`TaskContext`（值对象），
   既不能改状态，也没有 session 可用来开长事务。这样「谁有权写状态」
   在代码层面只有一处。
"""

import asyncio
import contextlib
import os
import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog

from xing.core.errors import NonRetryableError
from xing.models.task import Task, TaskStatus
from xing.worker import queue
from xing.worker.context import TaskContext
from xing.worker.db import WorkerSession
from xing.worker.handlers import get_handler

log = structlog.get_logger(__name__)

#: 心跳间隔。必须明显小于租约时长，否则一次抖动就会丢租约。
HEARTBEAT_INTERVAL = 30


class LeaseLost(Exception):
    """租约已被他人接管。此时**不允许**再写任何状态。"""


@dataclass(frozen=True)
class Outcome:
    status: str  # SUCCEEDED / FAILED / RETRYING / SKIPPED / LEASE_LOST
    detail: str = ""


def make_worker_id() -> str:
    """worker 标识：主机名 + pid。只用于租约归属，不要求全局唯一到进程实例之外。"""
    return f"{socket.gethostname()}:{os.getpid()}"


async def _heartbeat_loop(
    task_id: UUID,
    worker_id: str,
    generation: int,
    lease_seconds: int,
    lost: asyncio.Event,
) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        try:
            async with WorkerSession() as session:
                ok = await queue.heartbeat(
                    session,
                    task_id,
                    worker_id=worker_id,
                    generation=generation,
                    lease_seconds=lease_seconds,
                )
        except Exception:
            # 数据库抖动不等于丢租约。记一笔继续跑，真正的丢租约靠下面这行判断。
            log.warning("worker.heartbeat_error", task_id=str(task_id), exc_info=True)
            continue

        if not ok:
            log.warning(
                "worker.lease_lost",
                task_id=str(task_id),
                worker_id=worker_id,
                generation=generation,
            )
            lost.set()
            return


async def _race_with_lease(coro: Any, lost: asyncio.Event) -> dict[str, Any]:
    """跑 handler，同时盯着「租约是否被抢走」。先完成者胜。"""
    work = asyncio.create_task(coro)
    guard = asyncio.create_task(lost.wait())
    try:
        done, pending = await asyncio.wait({work, guard}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (work, guard):
            if not task.done():
                task.cancel()
        for task in (work, guard):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    if work in done and not work.cancelled():
        return work.result()
    raise LeaseLost()


async def run_task(
    task_id: UUID,
    *,
    worker_id: str,
    lease_seconds: int = queue.DEFAULT_LEASE_SECONDS,
) -> Outcome:
    hb: asyncio.Task[None] | None = None
    lost = asyncio.Event()

    try:
        async with WorkerSession() as session:
            task = await session.get(Task, task_id)
            if task is None or task.locked_by != worker_id or task.status != TaskStatus.RUNNING:
                return Outcome("SKIPPED", "租约不属于本 worker")

            # 本次执行的代次。之后所有写入都必须带上它。
            generation = task.lease_generation
            task_type = task.task_type

            hb = asyncio.create_task(
                _heartbeat_loop(task_id, worker_id, generation, lease_seconds, lost)
            )

            fn = get_handler(task_type)
            if fn is None:
                await queue.finalize(
                    session,
                    task_id,
                    worker_id=worker_id,
                    generation=generation,
                    status=TaskStatus.FAILED,
                    completed_at=datetime.now(UTC),
                    error={
                        "code": "NO_HANDLER",
                        "message": f"没有注册 task_type={task_type!r} 的 handler",
                    },
                )
                return Outcome("FAILED", "NO_HANDLER")

            try:
                output = await _race_with_lease(fn(TaskContext.from_task(task)), lost)
            except LeaseLost:
                await session.rollback()
                return Outcome("LEASE_LOST")
            except Exception as exc:
                # 事务可能已经脏了（handler 写到一半），先回滚再另起事务收尾
                await session.rollback()
                return await _record_failure(task_id, worker_id, generation, exc)

            ok = await queue.finalize(
                session,
                task_id,
                worker_id=worker_id,
                generation=generation,
                status=TaskStatus.SUCCEEDED,
                completed_at=datetime.now(UTC),
                output=output,
                error=None,
            )
            if not ok:
                # 执行期间租约被回收并由他人接管。业务副作用已经发生，
                # 但状态归接管者，本次结果必须丢弃 —— 由接管者重跑，
                # 重复副作用靠 1b 的 idempotency_key 兜住。
                log.warning(
                    "worker.finalize_fenced_out",
                    task_id=str(task_id),
                    worker_id=worker_id,
                    generation=generation,
                )
                return Outcome("LEASE_LOST")

            log.info("worker.task_succeeded", task_id=str(task_id))
            return Outcome("SUCCEEDED")
    finally:
        if hb is not None:
            hb.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb


async def _record_failure(
    task_id: UUID, worker_id: str, generation: int, exc: Exception
) -> Outcome:
    async with WorkerSession() as session:
        task = await session.get(Task, task_id)
        if task is None or task.locked_by != worker_id or task.lease_generation != generation:
            return Outcome("LEASE_LOST")

        now = datetime.now(UTC)
        # 确定性失败（预算耗尽、输入非法、模型空响应）重试不会改变结果：
        # 只会烧掉重试额度、堵住队列，并让真实原因淹没在重试记录里。
        permanent = isinstance(exc, NonRetryableError)
        code = exc.code if isinstance(exc, NonRetryableError) else type(exc).__name__
        error = {"code": code, "message": str(exc)[:2000]}
        next_retry = task.retry_count + 1

        if not permanent and next_retry <= task.max_retries:
            ok = await queue.finalize(
                session,
                task_id,
                worker_id=worker_id,
                generation=generation,
                status=TaskStatus.RETRYING,
                retry_count=next_retry,
                available_at=now + timedelta(seconds=10 * next_retry),
                error=error,
            )
            if not ok:
                return Outcome("LEASE_LOST")
            log.warning(
                "worker.task_retrying", task_id=str(task_id), retry=next_retry, error=str(exc)
            )
            return Outcome("RETRYING", str(exc))

        ok = await queue.finalize(
            session,
            task_id,
            worker_id=worker_id,
            generation=generation,
            status=TaskStatus.FAILED,
            completed_at=now,
            error=error,
        )
        if not ok:
            return Outcome("LEASE_LOST")
        log.error("worker.task_failed", task_id=str(task_id), error=str(exc))
        return Outcome("FAILED", str(exc))


async def claim_and_run(
    *,
    worker_id: str,
    agent: str | None = None,
    workspace_id: UUID | None = None,
    lease_seconds: int = queue.DEFAULT_LEASE_SECONDS,
) -> Outcome | None:
    """领取并执行一个任务。队列空时返回 None。

    ``workspace_id`` 透传给 :func:`queue.claim_one`：默认全租户共享，
    需要隔离（或测试里只跑自己的队列）时收紧。
    """
    async with WorkerSession() as session:
        task = await queue.claim_one(
            session,
            worker_id=worker_id,
            agent=agent,
            workspace_id=workspace_id,
            lease_seconds=lease_seconds,
        )
        if task is None:
            return None
        task_id = task.id
        task_type = task.task_type
        await session.commit()

    log.info("worker.task_claimed", task_id=str(task_id), task_type=task_type)
    return await run_task(task_id, worker_id=worker_id, lease_seconds=lease_seconds)


__all__ = [
    "HEARTBEAT_INTERVAL",
    "LeaseLost",
    "Outcome",
    "claim_and_run",
    "make_worker_id",
    "run_task",
]
