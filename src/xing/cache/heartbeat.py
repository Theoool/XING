"""worker 心跳。

队列是拉模型：没人领，任务就一直躺在 ``READY``。而「没人领」和「正在跑」在界面上
长得一模一样 —— 都是一直转圈，直到十分钟后超时。worker 进程挂掉本身也不会有人知道，
因为没有任何东西在观察它。

这里让活着的 worker 在 Redis 留一个带 TTL 的键，接口据此回答「现在到底有没有人在
消费队列」。TTL 是 30 秒、5 秒一跳：进程被 SIGKILL 时来不及删键，键也会自己过期。
"""

from __future__ import annotations

import json
import time

import structlog

from xing.cache.redis import get_redis

log = structlog.get_logger(__name__)

PREFIX = "xing:worker:"
#: 心跳间隔。要远小于 TTL：一次 GC 停顿或一次慢查询不该让 worker 被判死。
INTERVAL = 5.0
#: 键的存活时间。必须大于「跑一个任务」的时间是不可能的 ——
#: 所以心跳由独立协程发，不跟着主循环走（主循环跑任务时会占住几分钟）。
TTL = 30

#: 一条心跳：worker_id / agent / beat（unix 秒）。
WorkerBeat = dict[str, object]


def key(worker_id: str) -> str:
    return f"{PREFIX}{worker_id}"


async def beat(worker_id: str, *, agent: str | None = None) -> None:
    payload = json.dumps(
        {"worker_id": worker_id, "agent": agent, "beat": round(time.time(), 3)},
        ensure_ascii=False,
    )
    await get_redis().set(key(worker_id), payload, ex=TTL)


async def retire(worker_id: str) -> None:
    """正常退出时删键：别让人等到 TTL 过期才发现少了一个 worker。"""
    await get_redis().delete(key(worker_id))


async def live() -> list[WorkerBeat] | None:
    """当前活着的 worker；``None`` 表示**读不到**（Redis 不可用）。

    这两种情况必须分开：Redis 抖动一次就报「没有 worker 在跑」，会让人去重启一个
    本来好好的进程。读不到时给 ``None``，由调用方决定怎么表达「不知道」。
    """
    client = get_redis()
    try:
        keys = [raw async for raw in client.scan_iter(match=f"{PREFIX}*", count=100)]
        if not keys:
            return []
        values = await client.mget(keys)
    except Exception as exc:  # noqa: BLE001 - 心跳读不到不该让主流程 500
        log.warning("worker.heartbeat_unreadable", error=str(exc))
        return None

    workers: list[WorkerBeat] = []
    for raw in values:
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        if isinstance(item, dict):
            workers.append(item)
    return workers
