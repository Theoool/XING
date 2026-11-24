"""选题接口。recommend 是这一层唯一的「算法型」接口。

注意路由顺序：/recommend 必须声明在 /{topic_id} 之前，
否则 FastAPI 会把 "recommend" 当作 UUID 解析，返回 422。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from xing.api.deps import CurrentUserId, WorkspaceDb
from xing.schemas.topic import (
    TopicOut,
    TopicRecommendation,
    TopicReject,
    TopicSelect,
    TopicStatus,
)
from xing.services import topic_service

router = APIRouter(prefix="/workspaces/{workspace_id}/topics", tags=["topics"])


@router.post(
    "/recommend",
    response_model=list[TopicRecommendation],
    summary="推荐选题",
    description="把近期未被覆盖的事件转为候选，按战略相关性/新鲜度/素材完备度打分排序。幂等。",
)
async def recommend_topics(
    workspace_id: UUID,
    session: WorkspaceDb,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    lookback_days: Annotated[int, Query(ge=1, le=90)] = 14,
) -> list[TopicRecommendation]:
    return await topic_service.recommend(
        session, workspace_id=workspace_id, limit=limit, lookback_days=lookback_days
    )


@router.get("", response_model=list[TopicOut], summary="选题列表")
async def list_topics(
    workspace_id: UUID,
    session: WorkspaceDb,
    status: TopicStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[TopicOut]:
    return await topic_service.list_topics(
        session, workspace_id=workspace_id, status=status, limit=limit
    )


@router.get("/{topic_id}", response_model=TopicOut, summary="选题详情")
async def get_topic(workspace_id: UUID, topic_id: UUID, session: WorkspaceDb) -> TopicOut:
    return await topic_service.get_topic(session, workspace_id=workspace_id, topic_id=topic_id)


@router.post("/{topic_id}/select", response_model=TopicOut, summary="选定选题")
async def select_topic(
    workspace_id: UUID,
    topic_id: UUID,
    payload: TopicSelect,
    session: WorkspaceDb,
    user_id: CurrentUserId,
) -> TopicOut:
    """选定会同时写一条 TOPIC_SELECTION 决策，绑定到当前生效战略版本。"""
    topic, _decision_id = await topic_service.select_topic(
        session,
        workspace_id=workspace_id,
        topic_id=topic_id,
        user_id=user_id,
        selected_option=payload.selected_option,
        rationale=payload.rationale,
    )
    return topic


@router.post("/{topic_id}/reject", response_model=TopicOut, summary="拒绝选题")
async def reject_topic(
    workspace_id: UUID,
    topic_id: UUID,
    payload: TopicReject,
    session: WorkspaceDb,
    user_id: CurrentUserId,
) -> TopicOut:
    return await topic_service.reject_topic(
        session,
        workspace_id=workspace_id,
        topic_id=topic_id,
        user_id=user_id,
        reason=payload.reason,
    )
