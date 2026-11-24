"""战略接口。没有「PUT 整条战略」——战略是版本化对象。"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from xing.api.deps import CurrentUserId, WorkspaceDb
from xing.schemas.strategy import (
    StrategyCreate,
    StrategyOut,
    StrategyVersionCreate,
)
from xing.services import strategy_service

router = APIRouter(prefix="/workspaces/{workspace_id}", tags=["strategies"])


@router.get("/strategies", response_model=list[StrategyOut], summary="战略列表")
async def list_strategies(workspace_id: UUID, session: WorkspaceDb) -> list[StrategyOut]:
    return await strategy_service.list_strategies(session, workspace_id=workspace_id)


@router.post("/strategies", response_model=StrategyOut, status_code=201, summary="创建战略")
async def create_strategy(
    workspace_id: UUID, payload: StrategyCreate, session: WorkspaceDb, user_id: CurrentUserId
) -> StrategyOut:
    return await strategy_service.create_strategy(
        session,
        workspace_id=workspace_id,
        user_id=user_id,
        name=payload.name,
        strategy_config=payload.strategy_config,
        change_summary=payload.change_summary,
    )


@router.get("/strategy", response_model=StrategyOut, summary="当前生效战略")
async def get_active_strategy(workspace_id: UUID, session: WorkspaceDb) -> StrategyOut:
    return await strategy_service.get_active_strategy(session, workspace_id=workspace_id)


@router.get("/strategies/{strategy_id}", response_model=StrategyOut, summary="战略详情")
async def get_strategy(workspace_id: UUID, strategy_id: UUID, session: WorkspaceDb) -> StrategyOut:
    return await strategy_service.get_strategy(
        session, workspace_id=workspace_id, strategy_id=strategy_id
    )


@router.post(
    "/strategies/{strategy_id}/versions",
    response_model=StrategyOut,
    status_code=201,
    summary="新增战略版本",
)
async def add_strategy_version(
    workspace_id: UUID,
    strategy_id: UUID,
    payload: StrategyVersionCreate,
    session: WorkspaceDb,
    user_id: CurrentUserId,
) -> StrategyOut:
    return await strategy_service.add_version(
        session,
        workspace_id=workspace_id,
        strategy_id=strategy_id,
        user_id=user_id,
        strategy_config=payload.strategy_config,
        change_summary=payload.change_summary,
    )


@router.post("/strategies/{strategy_id}/activate", response_model=StrategyOut, summary="激活战略")
async def activate_strategy(
    workspace_id: UUID, strategy_id: UUID, session: WorkspaceDb
) -> StrategyOut:
    return await strategy_service.activate_strategy(
        session, workspace_id=workspace_id, strategy_id=strategy_id
    )
