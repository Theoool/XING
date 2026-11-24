"""健康检查：/healthz 探活，/readyz 探依赖可用性。"""

import structlog
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from xing import __version__
from xing.cache.redis import ping_redis
from xing.db.session import ping_db

log = structlog.get_logger(__name__)

router = APIRouter(tags=["health"])


@router.get("/healthz", summary="存活探针")
async def healthz() -> dict[str, str]:
    """不触碰任何外部依赖，只证明进程还活着。"""
    return {"status": "ok"}


@router.get("/readyz", summary="就绪探针")
async def readyz() -> JSONResponse:
    """逐个检查下游依赖。任一不可用返回 503，便于编排层做摘流。"""
    checks: dict[str, str] = {}
    healthy = True

    try:
        await ping_db()
        checks["postgres"] = "ok"
    except Exception as exc:  # noqa: BLE001 - 探针需要吞掉所有异常并上报
        checks["postgres"] = f"error: {exc}"
        healthy = False

    try:
        await ping_redis()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["redis"] = f"error: {exc}"
        healthy = False

    if not healthy:
        log.warning("readiness.failed", checks=checks)

    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "ok" if healthy else "degraded",
            "version": __version__,
            "checks": checks,
        },
    )
