"""v1 路由汇总。"""

from fastapi import APIRouter

from xing.api.v1 import contents, decisions, dev, events, strategies, tasks, topics, workspaces

router = APIRouter()

for module in (
    workspaces,
    strategies,
    events,
    topics,
    decisions,
    contents,
    tasks,
    dev,
):
    router.include_router(module.router)
