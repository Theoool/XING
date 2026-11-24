"""数据库引擎与会话。全异步（asyncpg 驱动）。

RLS 上下文为什么必须挂在 SessionEvents.after_begin 上：
    SET LOCAL / set_config(..., true) 的生命周期是「当前事务」。
    如果在路由依赖里执行一次，随后 service 里 commit，设置就失效了；
    同一个请求里的下一条查询落在新事务里，GUC 为空 —— RLS 会把它们全部过滤掉。
    表现出来是「创建接口返回 201，紧接着列表返回空」，而且只在 commit 之后复现，
    用单个事务包住整个用例的测试根本测不出来。

    挂到 after_begin 后，任何事务（含 commit 之后自动开启的下一个）都会自动重设，
    并且因为 is_local=true，连接归还池子时不会把上一个租户的上下文带给别人。
"""

from collections.abc import AsyncIterator
from uuid import UUID

import structlog
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from xing.core.config import settings

log = structlog.get_logger(__name__)

engine = create_async_engine(
    settings.database_url,
    echo=settings.sql_echo,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_recycle=settings.db_pool_recycle,
    pool_pre_ping=True,
)

SessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)

# 会话级上下文，存在 sync_session.info 里，随 AsyncSession 一起被丢弃
RLS_USER_ID = "rls_user_id"
RLS_WORKSPACE_ID = "rls_workspace_id"

_SET_RLS = text(
    "select set_config('app.user_id', :user_id, true),"
    "       set_config('app.workspace_id', :workspace_id, true)"
)


def _rls_params(
    user_id: UUID | str | None,
    workspace_id: UUID | str | None,
) -> dict[str, str]:
    # 空字符串是刻意的：app.current_user_id() 用 NULLIF(setting, '') 兜底，
    # 传 '' 得到 NULL，不会抛 invalid input syntax for type uuid。
    return {
        "user_id": "" if user_id is None else str(user_id),
        "workspace_id": "" if workspace_id is None else str(workspace_id),
    }


@event.listens_for(Session, "after_begin")
def _inject_rls_context(
    session: Session,
    transaction: object,  # noqa: ARG001 - 事件签名固定
    connection: object,
) -> None:
    """每个事务开始时注入 RLS 上下文。

    注册在 Session 类而不是 sessionmaker 上：async_sessionmaker 产生的
    AsyncSession 不会把事件派发到它包装的 sync session，挂在类上才是唯一可靠位置。
    没有绑过上下文的会话（比如登录直接查 users）在这里会直接跳过。
    """
    user_id = session.info.get(RLS_USER_ID)
    workspace_id = session.info.get(RLS_WORKSPACE_ID)
    if user_id is None and workspace_id is None:
        return

    connection.execute(_SET_RLS, _rls_params(user_id, workspace_id))  # type: ignore[attr-defined]


async def bind_rls(
    session: AsyncSession,
    *,
    user_id: UUID | str | None = None,
    workspace_id: UUID | str | None = None,
) -> None:
    """把当前请求的用户/工作区绑定到会话，供 RLS 使用。

    正常路径下会话还没开启事务，绑定后由 after_begin 负责注入。
    如果调用时事务已经开着（依赖顺序反过来、或复用了已用过的会话），
    这里补一次，避免第一条语句落在没有上下文的窗口里。
    """
    info = session.sync_session.info
    info[RLS_USER_ID] = user_id
    info[RLS_WORKSPACE_ID] = workspace_id

    if session.sync_session.in_transaction():
        await session.execute(_SET_RLS, _rls_params(user_id, workspace_id))


def rls_context(session: AsyncSession) -> tuple[str | None, str | None]:
    """调试用：读回当前会话绑定的上下文。"""
    info = session.sync_session.info
    return info.get(RLS_USER_ID), info.get(RLS_WORKSPACE_ID)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每个请求一个会话（不绑定 RLS 上下文）。

    只给不需要租户隔离的接口用，例如登录、注册、健康检查。
    业务接口必须走绑定上下文的那一个（随认证模块一起落地）。
    """
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def ping_db() -> bool:
    async with engine.connect() as conn:
        await conn.execute(text("select 1"))
    return True


async def db_identity() -> dict[str, object]:
    """探测当前连接身份。RLS 是否真的生效完全取决于这三个值。"""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "select current_user as role,"
                    "       r.rolsuper as is_superuser,"
                    "       r.rolbypassrls as bypass_rls"
                    "  from pg_roles r where r.rolname = current_user"
                )
            )
        ).one()
    return {"role": row.role, "is_superuser": row.is_superuser, "bypass_rls": row.bypass_rls}


async def dispose_engine() -> None:
    await engine.dispose()
