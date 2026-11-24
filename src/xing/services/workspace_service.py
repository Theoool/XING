"""工作区服务（ORM 版）。

workspaces / workspace_members 没有开 RLS（它们是租户的源头，开 RLS 会自引用），
所以这里每一个查询都必须显式按成员关系过滤——这是唯一的防线，不能省。

这个约束在 ORM 里反而更安全：成员过滤变成 ``join(WorkspaceMember)`` 的一部分，
是一条必写的语句，而不是一段可以被忘记拼进字符串的 WHERE 片段。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from xing.core.errors import ApiError
from xing.models.core import Workspace, WorkspaceMember, WorkspaceRole
from xing.schemas.workspace import WorkspaceOut


def _member_scope(stmt: Select[Any], user_id: UUID) -> Select[Any]:
    """把「必须是成员」这个条件钉进查询。

    抽成函数是为了让新增查询不可能漏掉它 —— 漏掉的后果是跨租户读取。
    """
    return stmt.join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id).where(
        WorkspaceMember.user_id == user_id
    )


async def list_workspaces(
    session: AsyncSession, *, user_id: UUID, include_archived: bool = False
) -> list[WorkspaceOut]:
    stmt = _member_scope(select(Workspace), user_id).order_by(Workspace.created_at.desc())
    if not include_archived:
        stmt = stmt.where(Workspace.status != "ARCHIVED")

    workspaces = (await session.execute(stmt)).scalars().unique().all()
    return [WorkspaceOut.model_validate(workspace) for workspace in workspaces]


async def create_workspace(session: AsyncSession, *, user_id: UUID, name: str) -> WorkspaceOut:
    workspace = Workspace(name=name, status="ACTIVE", created_by=user_id)
    session.add(workspace)
    await session.flush()

    # 创建者自动成 OWNER。两个 INSERT 同一个事务：不能出现「工作区存在但没人属于它」
    session.add(
        WorkspaceMember(
            workspace_id=workspace.id,
            user_id=user_id,
            role=WorkspaceRole.OWNER,
        )
    )
    await session.commit()
    return await get_workspace(session, workspace_id=workspace.id, user_id=user_id)


async def get_workspace(
    session: AsyncSession, *, workspace_id: UUID, user_id: UUID
) -> WorkspaceOut:
    workspace = (
        await session.execute(
            _member_scope(select(Workspace), user_id)
            .where(Workspace.id == workspace_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()

    if workspace is None:
        # 不区分「不存在」和「不是成员」，避免探测他人工作区是否存在。
        raise ApiError(404, "WORKSPACE_NOT_FOUND", "工作区不存在或无权访问")
    return WorkspaceOut.model_validate(workspace)


async def update_workspace(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    user_id: UUID,
    name: str | None = None,
    status: str | None = None,
) -> WorkspaceOut:
    if name is None and status is None:
        return await get_workspace(session, workspace_id=workspace_id, user_id=user_id)

    workspace = (
        await session.execute(
            _member_scope(select(Workspace), user_id)
            .where(Workspace.id == workspace_id)
            # 只锁 workspaces 这一侧：FOR UPDATE 默认会把 join 里的
            # workspace_members 也锁上，那是没必要的争用。
            .with_for_update(of=Workspace)
        )
    ).scalar_one_or_none()

    if workspace is None:
        raise ApiError(404, "WORKSPACE_NOT_FOUND", "工作区不存在或无权访问")

    if name is not None:
        workspace.name = name
    if status is not None:
        workspace.status = status

    await session.commit()
    return await get_workspace(session, workspace_id=workspace_id, user_id=user_id)
