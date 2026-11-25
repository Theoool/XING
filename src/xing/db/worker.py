"""worker / agent 运行时专用数据库连接。

与主应用的 ``xing.db.session`` 刻意分开，两个原因：

1. **身份不同**。worker 用 ``xing_worker``（BYPASSRLS），主应用用 ``xing_app``
   （NOBYPASSRLS）。领取查询要跨租户扫 ``tasks``，应用角色做不到。
   见 ``scripts/worker_role.sql``。

2. **不注入 RLS 上下文**。``SessionEvents.after_begin`` 的注入是 opt-in 的
   （没绑过上下文就跳过），这里不调 ``bind_rls``，所以天然干净。
   租户隔离改由 ``workspace_id`` 显式传参保证 —— 这是唯一被信任跨租户的
   角色，所以它必须自己守住这条线。

放在 ``db/`` 而不是 ``worker/``：Agent Runtime 也要用它，若留在 ``worker/``
就会变成 ``agents → worker`` 的反向依赖，而 ``worker.handlers`` 本身就依赖
``agents``。``worker/db.py`` 保留为转发模块，不破坏既有引用。
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from xing.core.config import settings

worker_engine = create_async_engine(
    settings.worker_database_url,
    echo=settings.sql_echo,
    pool_size=max(4, settings.db_pool_size // 2),
    max_overflow=settings.db_max_overflow,
    pool_recycle=settings.db_pool_recycle,
    pool_pre_ping=True,
)

WorkerSession = async_sessionmaker(
    bind=worker_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def dispose() -> None:
    await worker_engine.dispose()
