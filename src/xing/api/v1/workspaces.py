"""工作区接口。

workspaces / workspace_members 没有开 RLS，所以这几个接口用普通会话，
鉴权完全依赖 service 里的成员关系过滤（每一条 SQL 都带 user_id）。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from xing.api.deps import CurrentUserId
from xing.db.session import get_session
from xing.schemas.workspace import WorkspaceCreate, WorkspaceOut, WorkspaceUpdate
from xing.services import workspace_service

router = APIRouter(tags=["workspaces"])

DbSession = Annotated[AsyncSession, Depends(get_session)]


@router.get("/workspaces", response_model=list[WorkspaceOut], summary="我参与的工作区")
async def list_workspaces(
    session: DbSession,
    user_id: CurrentUserId,
    include_archived: Annotated[bool, Query()] = False,
) -> list[WorkspaceOut]:
    return await workspace_service.list_workspaces(
        session, user_id=user_id, include_archived=include_archived
    )


@router.post("/workspaces", response_model=WorkspaceOut, status_code=201, summary="创建工作区")
async def create_workspace(
    payload: WorkspaceCreate, session: DbSession, user_id: CurrentUserId
) -> WorkspaceOut:
    return await workspace_service.create_workspace(session, user_id=user_id, name=payload.name)


@router.get("/workspaces/{workspace_id}", response_model=WorkspaceOut, summary="工作区详情")
async def get_workspace(
    workspace_id: UUID, session: DbSession, user_id: CurrentUserId
) -> WorkspaceOut:
    return await workspace_service.get_workspace(
        session, workspace_id=workspace_id, user_id=user_id
    )


@router.patch("/workspaces/{workspace_id}", response_model=WorkspaceOut, summary="更新工作区")
async def update_workspace(
    workspace_id: UUID,
    payload: WorkspaceUpdate,
    session: DbSession,
    user_id: CurrentUserId,
) -> WorkspaceOut:
    return await workspace_service.update_workspace(
        session,
        workspace_id=workspace_id,
        user_id=user_id,
        name=payload.name,
        status=payload.status,
    )
