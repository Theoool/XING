"""FastAPI 应用入口。

启动顺序：日志 -> Redis -> 数据库连通性校验 -> 对外提供服务。
任何一步失败都直接抛出，让容器退出而不是带病运行。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from xing import __version__
from xing.api.router import api_router
from xing.cache.redis import close_redis, init_redis
from xing.core.config import settings
from xing.core.errors import register_exception_handlers
from xing.core.logging import setup_logging
from xing.db.session import dispose_engine, ping_db

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    setup_logging(settings.log_level, json_logs=not settings.is_dev)
    log.info(
        "app.startup",
        env=settings.app_env,
        version=__version__,
        debug=settings.app_debug,
    )

    await init_redis()
    await ping_db()
    log.info("app.ready")

    try:
        yield
    finally:
        await close_redis()
        await dispose_engine()
        log.info("app.shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description="AIXING 后端 API",
        lifespan=lifespan,
        docs_url="/docs" if settings.app_debug else None,
        redoc_url="/redoc" if settings.app_debug else None,
        openapi_url="/openapi.json" if settings.app_debug else None,
    )

    origins = settings.cors_origin_list
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=origins != ["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router)
    register_exception_handlers(app)
    return app


app = create_app()
