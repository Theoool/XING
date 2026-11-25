"""worker 进程入口。

一个循环干三件事，按间隔节流：

    每轮      领取一个任务并执行（队列空则退避 POLL_INTERVAL）
    每 5 秒   把退避到期的 RETRYING 转成 READY
    每 30 秒  回收租约过期的 RUNNING
    每 120 秒 对账僵尸 run（任务已终态或超时，却还停在 RUNNING）

不做成独立协程，是因为它们都要写同一张 tasks 表，
分开跑反而要处理互相等待；一个循环里按时间戳节流更简单，也更容易在测试里驱动。

用法：
    python -m xing.worker.main                # 消费所有类型
    python -m xing.worker.main --agent writer # 只消费 assigned_agent 为空或 writer 的任务

Phase 1a 只有 echo handler，见 worker/handlers.py。
"""

import argparse
import asyncio
import contextlib
import signal
import time

import structlog

from xing.core.config import settings
from xing.core.logging import setup_logging
from xing.worker import queue, reaper, runner
from xing.worker.db import WorkerSession, dispose
from xing.worker.handlers import registered_types

log = structlog.get_logger(__name__)

POLL_INTERVAL = 1.0
PROMOTE_INTERVAL = 5.0
RECLAIM_INTERVAL = 30.0
REAP_INTERVAL = 120.0


async def serve(
    *,
    agent: str | None,
    stop: asyncio.Event,
    poll_interval: float = POLL_INTERVAL,
) -> None:
    worker_id = runner.make_worker_id()
    log.info(
        "worker.started",
        worker_id=worker_id,
        agent=agent,
        handlers=registered_types(),
        env=settings.app_env,
    )

    last_promote = 0.0
    last_reclaim = 0.0
    last_reap = 0.0
    idle = 0

    while not stop.is_set():
        now = time.monotonic()

        if now - last_promote >= PROMOTE_INTERVAL:
            last_promote = now
            async with WorkerSession() as session:
                promoted = await queue.promote_retrying(session)
            if promoted:
                log.info("worker.promoted", count=promoted)

        if now - last_reclaim >= RECLAIM_INTERVAL:
            last_reclaim = now
            async with WorkerSession() as session:
                retrying, failed = await queue.reclaim_expired(session)
            if retrying or failed:
                log.warning("worker.reclaimed", to_retry=retrying, to_failed=failed)

        if now - last_reap >= REAP_INTERVAL:
            last_reap = now
            async with WorkerSession() as session:
                reaped = await reaper.reap_stale_runs(session)
            if reaped:
                # 这是异常信号：正常路径上 run 一定在任务终结前收尾。
                log.warning("worker.reaped_orphan_runs", count=reaped)

        try:
            outcome = await runner.claim_and_run(worker_id=worker_id, agent=agent)
        except Exception:
            # 单次执行失败不能让整个 worker 退出：队列里还有别人的任务
            log.exception("worker.tick_failed")
            outcome = None

        if outcome is None:
            idle += 1
            # 空闲退避，上限 5 秒。不用固定 1 秒轮询，是为了让空队列时数据库安静。
            await asyncio.sleep(min(poll_interval * min(idle, 5), 5.0))
        else:
            idle = 0

    log.info("worker.stopped", worker_id=worker_id)


async def _main(agent: str | None) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    try:
        await serve(agent=agent, stop=stop)
    finally:
        await dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="XING worker")
    parser.add_argument(
        "--agent",
        default=None,
        help="只消费该 Agent 的任务（assigned_agent 为该值或为空）",
    )
    args = parser.parse_args()

    setup_logging()
    asyncio.run(_main(args.agent))


if __name__ == "__main__":
    main()
