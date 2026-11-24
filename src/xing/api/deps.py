"""FastAPI 依赖：当前用户、带 RLS 上下文的会话。

这是开发期最关键的一层。三件事都在这里收敛：

1. 当前用户从哪来 —— get_current_user_id()。开发期返回固定 dev 用户，
   将来换成「解 JWT + 查会话」即可，业务路由不用改。
2. 租户从哪来 —— 路径里的 workspace_id，不接受 header。这样「header 与路径
   不一致」这类越权 bug 在结构上就不存在。
3. RLS 上下文怎么注入 —— bind_rls() + SessionEvents.after_begin，
   每次事务自动重设，commit 之后依然有效。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from xing.core.config import settings
from xing.core.dev_identity import ensure_dev_identity
from xing.core.errors import ApiError
from xing.db.session import SessionLocal, bind_rls


async def get_current_user_id(
    x_user_id: Annotated[UUID | None, Header(alias="X-User-ID")] = None,
) -> UUID:
    """当前用户。

    开发期：返回固定 dev 用户；可用 X-User-ID 覆盖，仅用于验证租户隔离。
    生产期：这里换成 Bearer token 解析，其余代码不变。
    """
    if not settings.is_dev:
        # 生产环境没有认证就不能放行——否则整站等于公开的。
        raise ApiError(
            401,
            "AUTH_REQUIRED",
            "认证尚未启用；生产环境必须实现 get_current_user_id",
        )

    if x_user_id is not None:
        return x_user_id

    identity = await ensure_dev_identity()
    return identity.user_id


CurrentUserId = Annotated[UUID, Depends(get_current_user_id)]


async def workspace_db(
    workspace_id: UUID,
    user_id: CurrentUserId,
) -> AsyncIterator[AsyncSession]:
    """绑定租户上下文的会话。所有业务路由都用它。

    注意上下文绑在会话上、注入在事务开始时发生：路由里 commit 多少次都不会丢。
    非成员访问时 RLS 会让读返回空、写抛 42501（映射成 403），不需要额外判权。
    """
    async with SessionLocal() as session:
        await bind_rls(session, user_id=user_id, workspace_id=workspace_id)
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


WorkspaceDb = Annotated[AsyncSession, Depends(workspace_db)]
WorkspaceId = Annotated[UUID, "路径中的工作区 ID（由 workspace_db 绑定 RLS）"]
