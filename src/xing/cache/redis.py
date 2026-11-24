"""Redis 连接池。用同一个客户端承载缓存、限流与任务队列。"""

import structlog
from redis.asyncio import ConnectionPool, Redis

from xing.core.config import settings

log = structlog.get_logger(__name__)

_pool: ConnectionPool | None = None
_client: Redis | None = None


def get_redis() -> Redis:
    global _pool, _client
    if _client is None:
        _pool = ConnectionPool.from_url(
            settings.redis_url,
            max_connections=50,
            decode_responses=True,
            health_check_interval=30,
        )
        _client = Redis(connection_pool=_pool)
    return _client


async def init_redis() -> Redis:
    client = get_redis()
    await client.ping()
    log.info("redis.connected", url=settings.redis_url)
    return client


async def ping_redis() -> bool:
    await get_redis().ping()
    return True


async def close_redis() -> None:
    global _pool, _client
    if _client is not None:
        await _client.aclose()
        _client = None
    if _pool is not None:
        await _pool.disconnect()
        _pool = None
