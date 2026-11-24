"""决策接口。AIXING 与普通 CMS 的分界线：这里记录「为什么」。"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from xing.api.deps import CurrentUserId, WorkspaceDb
from xing.schemas.decision import DecisionCreate, DecisionOut, DecisionReview, DecisionType
from xing.services import decision_service

router = APIRouter(prefix="/workspaces/{workspace_id}/decisions", tags=["decisions"])


@router.post("", response_model=DecisionOut, status_code=201, summary="记录决策")
async def create_decision(
    workspace_id: UUID,
    payload: DecisionCreate,
    session: WorkspaceDb,
    user_id: CurrentUserId,
) -> DecisionOut:
    """strategy_version_id 由服务端按当前生效战略解析，请求体里没有这个字段。"""
    return await decision_service.create_decision(
        session, workspace_id=workspace_id, user_id=user_id, payload=payload
    )


@router.get("", response_model=list[DecisionOut], summary="决策列表")
async def list_decisions(
    workspace_id: UUID,
    session: WorkspaceDb,
    decision_type: DecisionType | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[DecisionOut]:
    return await decision_service.list_decisions(
        session, workspace_id=workspace_id, decision_type=decision_type, limit=limit
    )


@router.get("/{decision_id}", response_model=DecisionOut, summary="决策详情（含证据）")
async def get_decision(workspace_id: UUID, decision_id: UUID, session: WorkspaceDb) -> DecisionOut:
    return await decision_service.get_decision(
        session, workspace_id=workspace_id, decision_id=decision_id
    )


@router.post("/{decision_id}/approve", response_model=DecisionOut, summary="批准决策")
async def approve_decision(
    workspace_id: UUID,
    decision_id: UUID,
    payload: DecisionReview,
    session: WorkspaceDb,
    user_id: CurrentUserId,
) -> DecisionOut:
    return await decision_service.review_decision(
        session,
        workspace_id=workspace_id,
        decision_id=decision_id,
        user_id=user_id,
        approve=True,
        comment=payload.comment,
    )


@router.post("/{decision_id}/reject", response_model=DecisionOut, summary="否决决策")
async def reject_decision(
    workspace_id: UUID,
    decision_id: UUID,
    payload: DecisionReview,
    session: WorkspaceDb,
    user_id: CurrentUserId,
) -> DecisionOut:
    return await decision_service.review_decision(
        session,
        workspace_id=workspace_id,
        decision_id=decision_id,
        user_id=user_id,
        approve=False,
        comment=payload.comment,
    )
