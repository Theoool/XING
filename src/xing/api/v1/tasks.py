"""任务接口。

生产 / 发布从第一天就走 ``202 + task_id + 轮询`` 的模式：创建任务立刻返回，
真实进度看任务本身。队列状态单独一条只读端点，因为「任务为什么不动」
不是任务行能回答的问题。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from xing.api.deps import WorkspaceDb
from xing.schemas.task import QueueStatus, TaskCreate, TaskOut
from xing.schemas.trace import TaskTrace
from xing.services import task_service, trace_service

router = APIRouter(prefix="/workspaces/{workspace_id}/tasks", tags=["tasks"])

#: 队列不是任务集合，单独挂一个前缀：``/tasks/{id}`` 那边是按 id 取行，
#: 把摘要塞进去会让「id 是 UUID」这条约束多一个例外。
queue_router = APIRouter(prefix="/workspaces/{workspace_id}/queue", tags=["tasks"])


@queue_router.get("/status", response_model=QueueStatus, summary="队列状态（谁在消费、堆了多少）")
async def queue_status(workspace_id: UUID, session: WorkspaceDb) -> QueueStatus:
    return await task_service.queue_status(session, workspace_id=workspace_id)


@router.post("", response_model=TaskOut, status_code=201, summary="创建任务")
async def create_task(workspace_id: UUID, payload: TaskCreate, session: WorkspaceDb) -> TaskOut:
    return await task_service.create_task(session, workspace_id=workspace_id, payload=payload)


@router.get("", response_model=list[TaskOut], summary="任务列表")
async def list_tasks(
    workspace_id: UUID,
    session: WorkspaceDb,
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[TaskOut]:
    return await task_service.list_tasks(
        session, workspace_id=workspace_id, status=status, limit=limit
    )


@router.get("/{task_id}", response_model=TaskOut, summary="任务详情（轮询用）")
async def get_task(workspace_id: UUID, task_id: UUID, session: WorkspaceDb) -> TaskOut:
    return await task_service.get_task(session, workspace_id=workspace_id, task_id=task_id)


@router.get("/{task_id}/trail", response_model=TaskTrace, summary="取证轨迹（检索/思考/立场）")
async def task_trail(workspace_id: UUID, task_id: UUID, session: WorkspaceDb) -> TaskTrace:
    """一个任务到底怎么做的：检索参数、回来的材料、模型每一步的输入输出。

    单独一条端点而不是塞进 ``/tasks/{id}``：任务详情是 4 秒轮询的高频路径，
    轨迹是排查时按需拉取的大载荷（含完整 prompt），两者不该共用一个响应。
    """
    return await trace_service.task_trace(session, workspace_id=workspace_id, task_id=task_id)


@router.post("/{task_id}/cancel", response_model=TaskOut, summary="取消任务")
async def cancel_task(
    workspace_id: UUID,
    task_id: UUID,
    session: WorkspaceDb,
    reason: Annotated[str | None, Query()] = None,
) -> TaskOut:
    return await task_service.cancel_task(
        session, workspace_id=workspace_id, task_id=task_id, reason=reason
    )
