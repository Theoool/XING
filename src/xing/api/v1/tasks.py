"""任务接口。

这一版没有 worker，任务会停在 PENDING —— 接口先立起来，
是为了让「生产 / 发布」从第一天就走 202 + task_id + 轮询的模式。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from xing.api.deps import WorkspaceDb
from xing.schemas.task import TaskCreate, TaskOut
from xing.services import task_service

router = APIRouter(prefix="/workspaces/{workspace_id}/tasks", tags=["tasks"])


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
