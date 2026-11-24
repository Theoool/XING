"""外部事件服务（ORM 版）。

ingest 必须幂等：爬虫重试、上游重复推送都会重复进入这里。
靠 ``(workspace_id, source_type, external_id)`` 唯一键做 upsert，
用 ``xmax = 0`` 判断这次到底是插入还是更新，返回体里带 ``created`` 标志，
路由据此决定 201 还是 200。

为什么不用「先 select 再 insert」：两个并发请求会同时读到「不存在」，
都认为自己是新建，状态码就撒了谎。``xmax`` 是 Postgres 的物理列，
插入的新行 xmax 为 0，被 ON CONFLICT 更新的行不为 0 —— 一次往返拿到真相。

COALESCE 的语义是刻意的：``title`` 覆盖（上游改标题要跟着改），
其余字段只补空不覆盖（避免上游某次返回 null 把已有摘要/链接清掉）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, literal_column, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from xing.api.pagination import decode_cursor, encode_cursor
from xing.core.errors import ApiError
from xing.models.event import ExternalEvent
from xing.schemas.event import EventIngest, EventOut


async def ingest(
    session: AsyncSession, *, workspace_id: UUID, payload: EventIngest
) -> tuple[bool, EventOut]:
    base = pg_insert(ExternalEvent).values(
        workspace_id=workspace_id,
        source_type=payload.source_type,
        external_id=payload.external_id,
        title=payload.title,
        summary=payload.summary,
        url=payload.url,
        source_published_at=payload.source_published_at,
        raw_payload=payload.raw_payload,
    )

    # on_conflict_do_update 的返回类型在类型存根里需要显式泛型参数，这里用 Any 化简
    stmt: Any = base.on_conflict_do_update(
        index_elements=[
            ExternalEvent.workspace_id,
            ExternalEvent.source_type,
            ExternalEvent.external_id,
        ],
        set_={
            "title": base.excluded.title,
            "summary": func.coalesce(base.excluded.summary, ExternalEvent.summary),
            "url": func.coalesce(base.excluded.url, ExternalEvent.url),
            "source_published_at": func.coalesce(
                base.excluded.source_published_at, ExternalEvent.source_published_at
            ),
            "raw_payload": func.coalesce(base.excluded.raw_payload, ExternalEvent.raw_payload),
        },
    ).returning(ExternalEvent, literal_column("xmax = 0").label("inserted"))

    event, created = (await session.execute(stmt)).one()
    await session.commit()
    return bool(created), EventOut.model_validate(event)


async def list_events(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    limit: int,
    cursor: str | None,
    source_type: str | None,
    query: str | None,
) -> tuple[list[EventOut], str | None]:
    stmt = select(ExternalEvent).where(ExternalEvent.workspace_id == workspace_id)

    if source_type:
        stmt = stmt.where(ExternalEvent.source_type == source_type)

    if query:
        pattern = f"%{query}%"
        # 与老 SQL 一致：标题或摘要任一命中。summary 为 null 时 coalesce 成空串，
        # 否则 null ILIKE 会得到 null 而不是 false，整行被过滤掉。
        stmt = stmt.where(
            or_(
                ExternalEvent.title.ilike(pattern),
                func.coalesce(ExternalEvent.summary, "").ilike(pattern),
            )
        )

    if cursor:
        cursor_ts, cursor_id = decode_cursor(cursor)
        # 键集分页：按 (created_at, id) 做行值比较，不用 offset。
        # offset 在深页会全表扫，而且并发插入会导致翻页丢行/重复。
        stmt = stmt.where(
            tuple_(ExternalEvent.created_at, ExternalEvent.id) < tuple_(cursor_ts, cursor_id)
        )

    stmt = stmt.order_by(ExternalEvent.created_at.desc(), ExternalEvent.id.desc()).limit(limit + 1)

    rows = (await session.execute(stmt)).scalars().all()

    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = None
    if has_more and page:
        last = page[-1]
        next_cursor = encode_cursor(last.created_at, last.id)
    return [EventOut.model_validate(event) for event in page], next_cursor


async def get_event(session: AsyncSession, *, workspace_id: UUID, event_id: UUID) -> EventOut:
    event = (
        await session.execute(
            select(ExternalEvent).where(
                ExternalEvent.id == event_id,
                ExternalEvent.workspace_id == workspace_id,
            )
        )
    ).scalar_one_or_none()

    if event is None:
        raise ApiError(404, "EVENT_NOT_FOUND", "事件不存在")
    return EventOut.model_validate(event)
