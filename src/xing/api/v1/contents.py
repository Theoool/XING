"""内容接口。

改正文用 POST /versions，不是 PATCH body：
每一次人工修改都是一个版本，将来可以用来反推稳定偏好。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from xing.api.deps import CurrentUserId, WorkspaceDb
from xing.schemas.content import (
    ApprovalOut,
    ContentCreate,
    ContentOut,
    ContentReview,
    ContentVersionCreate,
    ContentVersionOut,
)
from xing.services import content_service

router = APIRouter(prefix="/workspaces/{workspace_id}/contents", tags=["contents"])


@router.post("", response_model=ContentOut, status_code=201, summary="创建内容")
async def create_content(
    workspace_id: UUID, payload: ContentCreate, session: WorkspaceDb, user_id: CurrentUserId
) -> ContentOut:
    return await content_service.create_content(
        session, workspace_id=workspace_id, user_id=user_id, payload=payload
    )


@router.get("", response_model=list[ContentOut], summary="内容列表")
async def list_contents(
    workspace_id: UUID,
    session: WorkspaceDb,
    status: str | None = None,
    topic_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[ContentOut]:
    return await content_service.list_contents(
        session,
        workspace_id=workspace_id,
        status=status,
        topic_id=topic_id,
        limit=limit,
    )


@router.get("/{content_id}", response_model=ContentOut, summary="内容详情（含当前版本）")
async def get_content(workspace_id: UUID, content_id: UUID, session: WorkspaceDb) -> ContentOut:
    return await content_service.get_content(
        session, workspace_id=workspace_id, content_id=content_id
    )


@router.get("/{content_id}/versions", response_model=list[ContentVersionOut], summary="版本历史")
async def list_versions(
    workspace_id: UUID, content_id: UUID, session: WorkspaceDb
) -> list[ContentVersionOut]:
    return await content_service.list_versions(
        session, workspace_id=workspace_id, content_id=content_id
    )


@router.post(
    "/{content_id}/versions",
    response_model=ContentOut,
    status_code=201,
    summary="新增版本",
    description="已通过审批的内容新增版本后会自动回到 REVIEW —— 审批是针对某一版的。",
)
async def add_version(
    workspace_id: UUID,
    content_id: UUID,
    payload: ContentVersionCreate,
    session: WorkspaceDb,
    user_id: CurrentUserId,
) -> ContentOut:
    return await content_service.add_version(
        session,
        workspace_id=workspace_id,
        content_id=content_id,
        user_id=user_id,
        body=payload.body,
        title=payload.title,
        change_summary=payload.change_summary,
    )


@router.post("/{content_id}/approve", response_model=ContentOut, summary="审核通过")
async def approve_content(
    workspace_id: UUID,
    content_id: UUID,
    payload: ContentReview,
    session: WorkspaceDb,
    user_id: CurrentUserId,
) -> ContentOut:
    return await content_service.approve_content(
        session,
        workspace_id=workspace_id,
        content_id=content_id,
        user_id=user_id,
        comment=payload.comment,
    )


@router.post("/{content_id}/reject", response_model=ContentOut, summary="审核驳回")
async def reject_content(
    workspace_id: UUID,
    content_id: UUID,
    payload: ContentReview,
    session: WorkspaceDb,
    user_id: CurrentUserId,
) -> ContentOut:
    return await content_service.reject_content(
        session,
        workspace_id=workspace_id,
        content_id=content_id,
        user_id=user_id,
        comment=payload.comment,
    )


@router.get("/{content_id}/approvals", response_model=list[ApprovalOut], summary="审批历史")
async def list_approvals(
    workspace_id: UUID, content_id: UUID, session: WorkspaceDb
) -> list[ApprovalOut]:
    return await content_service.list_approvals(
        session, workspace_id=workspace_id, content_id=content_id
    )
