"""路由汇总。业务路由按域挂到 /api/v1 下。"""

from fastapi import APIRouter

from xing import __version__
from xing.api.routes import health
from xing.api.v1.router import router as v1_business

api_router = APIRouter()
api_router.include_router(health.router)

v1 = APIRouter(prefix="/api/v1")
v1.include_router(v1_business)


@v1.get("/ping", tags=["meta"])
async def ping() -> dict[str, str]:
    return {"message": "pong", "version": __version__}


api_router.include_router(v1)
