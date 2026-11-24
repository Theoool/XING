"""开发期的固定身份。

开发阶段不做登录，但 RLS 需要一个真实的 user_id + workspace_id 才能注入上下文，
而且 contents.created_by 是 NOT NULL 的外键。所以这里保证库里存在：

    user      dev@xing.local
    workspace XING Dev（该用户是 OWNER）

这不是「绕过认证的捷径」，而是把认证的位置收敛到一个依赖函数
（api/deps.py: get_current_user_id）。将来接入 JWT 时，只替换那个函数，
业务代码与 RLS 上下文注入不需要任何改动。

这两个表都没有 RLS，所以用不绑定上下文的普通会话即可（绑了也没意义，
workspace_id 此时还不存在）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

import structlog
from sqlalchemy import func, select

from xing.db.session import SessionLocal
from xing.models.core import User, Workspace, WorkspaceMember, WorkspaceRole, WorkspaceStatus

log = structlog.get_logger(__name__)

DEV_EMAIL = "dev@xing.local"
DEV_DISPLAY_NAME = "Dev User"
DEV_WORKSPACE_NAME = "XING Dev"


@dataclass(frozen=True, slots=True)
class DevIdentity:
    user_id: UUID
    workspace_id: UUID


_identity: DevIdentity | None = None
_lock = asyncio.Lock()


async def ensure_dev_identity() -> DevIdentity:
    """幂等地保证 dev 用户与工作区存在，结果缓存在进程内。

    不放在 lifespan 里，是为了让测试直接构造 app 而不用跑启动流程；
    加锁是因为并发的第一批请求会同时打进来。
    """
    global _identity
    if _identity is not None:
        return _identity

    async with _lock:
        if _identity is not None:
            return _identity

        async with SessionLocal() as session:
            # 查用 lower(email) 才命中 users_email_lower_unique
            user = (
                await session.execute(select(User).where(func.lower(User.email) == DEV_EMAIL))
            ).scalar_one_or_none()

            if user is None:
                user = User(email=DEV_EMAIL, display_name=DEV_DISPLAY_NAME)
                session.add(user)
                await session.flush()
                log.info("dev_identity.user_created", user_id=str(user.id))

            workspace = (
                await session.execute(
                    select(Workspace)
                    .where(
                        Workspace.created_by == user.id,
                        Workspace.name == DEV_WORKSPACE_NAME,
                        Workspace.status == WorkspaceStatus.ACTIVE,
                    )
                    .order_by(Workspace.created_at)
                    .limit(1)
                )
            ).scalar_one_or_none()

            if workspace is None:
                workspace = Workspace(
                    name=DEV_WORKSPACE_NAME,
                    created_by=user.id,
                    status=WorkspaceStatus.ACTIVE,
                )
                session.add(workspace)
                await session.flush()
                session.add(
                    WorkspaceMember(
                        workspace_id=workspace.id,
                        user_id=user.id,
                        role=WorkspaceRole.OWNER,
                    )
                )
                log.info("dev_identity.workspace_created", workspace_id=str(workspace.id))

            await session.commit()
            user_id, workspace_id = user.id, workspace.id

        _identity = DevIdentity(user_id=user_id, workspace_id=workspace_id)
        return _identity


def reset_dev_identity_cache() -> None:
    """测试用：清掉进程内缓存。"""
    global _identity
    _identity = None
