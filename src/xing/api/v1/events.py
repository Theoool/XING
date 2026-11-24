"""外部事件接口。ingest 是热点数据进入系统的唯一入口。"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Response

from xing.api.deps import WorkspaceDb
from xing.schemas.common import Page
from xing.schemas.event import EventIngest, EventIngestResult, EventOut
from xing.services import event_service

router = APIRouter(prefix="/workspaces/{workspace_id}/events", tags=["events"])

SourceTypeFilter = Literal["NEWS", "SEARCH", "SOCIAL", "RSS", "API", "MANUAL", "OTHER"]


@router.post(
    "/ingest",
    response_model=EventIngestResult,
    status_code=201,
    summary="采集事件（幂等）",
    description="同一 workspace 下 (source_type, external_id) 唯一；重复投递返回 200 并更新原记录。",  # noqa: E501
)
async def ingest_event(
    workspace_id: UUID, payload: EventIngest, session: WorkspaceDb, response: Response
) -> EventIngestResult:
    created, event = await event_service.ingest(session, workspace_id=workspace_id, payload=payload)
    if not created:
        response.status_code = 200
    return EventIngestResult(created=created, event=event)


@router.get("", response_model=Page[EventOut], summary="事件列表（键集分页）")
async def list_events(
    workspace_id: UUID,
    session: WorkspaceDb,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
    source_type: SourceTypeFilter | None = None,
    q: Annotated[str | None, Query(description="标题/摘要模糊匹配")] = None,
) -> Page[EventOut]:
    items, next_cursor = await event_service.list_events(
        session,
        workspace_id=workspace_id,
        limit=limit,
        cursor=cursor,
        source_type=source_type,
        query=q,
    )
    return Page[EventOut](items=items, next_cursor=next_cursor)


@router.get("/{event_id}", response_model=EventOut, summary="事件详情")
async def get_event(workspace_id: UUID, event_id: UUID, session: WorkspaceDb) -> EventOut:
    return await event_service.get_event(session, workspace_id=workspace_id, event_id=event_id)
