"""战略服务（ORM 版）。

战略是版本化对象。三条约束全部由 schema 保证，服务层只负责：
 - 创建时同时落 v1（不会出现「有战略没版本」的中间态）
 - 新版本必须递增，且在行锁内完成（并发提交不会撞版本号）
 - activate 时先把其他 ACTIVE 降级（部分唯一索引要求一 workspace 一条 ACTIVE）

ORM 化之后有三处必须留意的陷阱，都在下面各自的位置写了注释：
 1. ``populate_existing=True`` —— commit 后 identity map 里的对象不会自动变新
 2. 锁查询必须 ``lazyload`` 关系 —— ``FOR UPDATE`` 不能作用在 outer join 的可空侧
 3. ``current_version`` 是 viewonly 的按值关系 —— 只能读，指针必须手改
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import lazyload

from xing.core.errors import ApiError
from xing.models.strategy import Strategy, StrategyStatus, StrategyVersion
from xing.schemas.strategy import StrategyOut, StrategyVersionListOut


def _to_out(strategy: Strategy) -> StrategyOut:
    """ORM 对象 -> 响应模型。

    ``current_version`` 是 eager load（joined）过来的，序列化时不会触发懒加载，
    所以 async 下不会踩 MissingGreenlet。
    """
    return StrategyOut.model_validate(strategy)


async def create_strategy(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    user_id: UUID,
    name: str,
    strategy_config: dict[str, Any],
    change_summary: str | None,
) -> StrategyOut:
    strategy = Strategy(
        workspace_id=workspace_id,
        name=name,
        status=StrategyStatus.DRAFT,
        current_version_number=1,
        lock_version=0,
    )
    session.add(strategy)

    await session.flush()

    session.add(
        StrategyVersion(
            workspace_id=workspace_id,
            strategy_id=strategy.id,
            version_number=1,
            strategy_config=strategy_config,
            change_summary=change_summary,
            created_by=user_id,
        )
    )
    await session.commit()
    return await get_strategy(session, workspace_id=workspace_id, strategy_id=strategy.id)


async def list_strategies(session: AsyncSession, *, workspace_id: UUID) -> list[StrategyOut]:
    strategies = (
        (
            await session.execute(
                select(Strategy)
                .where(Strategy.workspace_id == workspace_id)
                .order_by(Strategy.created_at.desc())
            )
        )
        .scalars()
        .unique()
        .all()
    )
    return [_to_out(strategy) for strategy in strategies]


async def get_strategy(
    session: AsyncSession, *, workspace_id: UUID, strategy_id: UUID
) -> StrategyOut:
    strategy = (
        await session.execute(
            select(Strategy)
            .where(Strategy.workspace_id == workspace_id, Strategy.id == strategy_id)
            # 会话是 expire_on_commit=False，commit 之后对象属性不会自动失效。
            # 不加这个，add_version / activate 提交后重查会拿回 identity map 里的旧对象，
            # 返回的 current_version 和 status 都是改之前的样子。
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()

    if strategy is None:
        raise ApiError(404, "STRATEGY_NOT_FOUND", "战略不存在")
    return _to_out(strategy)


async def get_active_strategy(session: AsyncSession, *, workspace_id: UUID) -> StrategyOut:
    """当前生效战略。一个 workspace 最多一条（部分唯一索引保证）。"""
    strategy = (
        await session.execute(
            select(Strategy)
            .where(
                Strategy.workspace_id == workspace_id,
                Strategy.status == StrategyStatus.ACTIVE,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()

    if strategy is None:
        raise ApiError(404, "NO_ACTIVE_STRATEGY", "当前没有生效中的战略")
    return _to_out(strategy)


async def rename_strategy(
    session: AsyncSession, *, workspace_id: UUID, strategy_id: UUID, name: str
) -> StrategyOut:
    """改名。不动 current_version_number，也不动 lock_version ——

    lock_version 记的是「配置改过几次」，名字不是配置。改个名字就让版本号
    看起来变紧，会让审计里对不上号。
    """
    strategy = (
        await session.execute(
            select(Strategy)
            .options(lazyload(Strategy.current_version))
            .where(Strategy.workspace_id == workspace_id, Strategy.id == strategy_id)
        )
    ).scalar_one_or_none()

    if strategy is None:
        raise ApiError(404, "STRATEGY_NOT_FOUND", "战略不存在")

    strategy.name = name
    await session.commit()
    return await get_strategy(session, workspace_id=workspace_id, strategy_id=strategy_id)


async def list_versions(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    strategy_id: UUID,
    limit: int = 20,
) -> list[StrategyVersionListOut]:
    """版本历史，新到旧。

    先确认战略属于本工作区：直接查版本表也能得到「空列表」，但空列表和
    「战略不存在」在前端是两种完全不同的提示（一个是「刚建还没改过」，
    另一个是「你拿错 id 了」），所以这里必须先把 404 分出来。

    只取 ``current_version_number`` 而不是整个 ``Strategy``：``current_version``
    是 joined eager load，为了一个整数把版本行再 join 一次没有意义。
    """
    current = (
        await session.execute(
            select(Strategy.current_version_number).where(
                Strategy.workspace_id == workspace_id, Strategy.id == strategy_id
            )
        )
    ).scalar_one_or_none()

    if current is None:
        raise ApiError(404, "STRATEGY_NOT_FOUND", "战略不存在")

    versions = (
        (
            await session.execute(
                select(StrategyVersion)
                .where(
                    StrategyVersion.workspace_id == workspace_id,
                    StrategyVersion.strategy_id == strategy_id,
                )
                .order_by(StrategyVersion.version_number.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    return [
        StrategyVersionListOut(
            id=version.id,
            version_number=version.version_number,
            strategy_config=version.strategy_config,
            change_summary=version.change_summary,
            created_by=version.created_by,
            created_at=version.created_at,
            is_current=version.version_number == current,
        )
        for version in versions
    ]


async def add_version(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    strategy_id: UUID,
    user_id: UUID,
    strategy_config: dict[str, Any],
    change_summary: str | None,
) -> StrategyOut:
    strategy = (
        await session.execute(
            select(Strategy)
            # FOR UPDATE 不能作用在 outer join 的可空侧。current_version 是 joined
            # eager load，会生成 LEFT OUTER JOIN strategy_versions —— 不降级为 lazyload
            # 的话 Postgres 直接报 "FOR UPDATE cannot be applied to the nullable side"。
            .options(lazyload(Strategy.current_version))
            .where(Strategy.workspace_id == workspace_id, Strategy.id == strategy_id)
            .with_for_update()
        )
    ).scalar_one_or_none()

    if strategy is None:
        raise ApiError(404, "STRATEGY_NOT_FOUND", "战略不存在")
    if strategy.status == StrategyStatus.ARCHIVED:
        raise ApiError(409, "STRATEGY_ARCHIVED", "已归档的战略不能新增版本")

    next_version = strategy.current_version_number + 1

    session.add(
        StrategyVersion(
            workspace_id=workspace_id,
            strategy_id=strategy_id,
            version_number=next_version,
            strategy_config=strategy_config,
            change_summary=change_summary,
            created_by=user_id,
        )
    )

    strategy.current_version_number = next_version
    # 用 SQL 表达式自增，避免「读出来 +1 再写回」在并发下丢掉别人的一次变更。
    # 注意它目前只是变更计数，不是乐观锁 —— 没有任何 WHERE lock_version = :expected。
    strategy.lock_version = Strategy.lock_version + 1

    await session.commit()
    return await get_strategy(session, workspace_id=workspace_id, strategy_id=strategy_id)


async def activate_strategy(
    session: AsyncSession, *, workspace_id: UUID, strategy_id: UUID
) -> StrategyOut:
    target = (
        await session.execute(
            select(Strategy)
            .options(lazyload(Strategy.current_version))
            .where(Strategy.workspace_id == workspace_id, Strategy.id == strategy_id)
            .with_for_update()
        )
    ).scalar_one_or_none()

    if target is None:
        raise ApiError(404, "STRATEGY_NOT_FOUND", "战略不存在")
    if target.status == StrategyStatus.ARCHIVED:
        raise ApiError(409, "STRATEGY_ARCHIVED", "已归档的战略不能激活")

    # 顺序很重要：先把其他 ACTIVE 降级，再激活目标，否则中间态会撞
    # strategies_one_active_per_workspace 这个部分唯一索引（23505）。
    await session.execute(
        update(Strategy)
        .where(
            Strategy.workspace_id == workspace_id,
            Strategy.status == StrategyStatus.ACTIVE,
            Strategy.id != strategy_id,
        )
        .values(status=StrategyStatus.PAUSED)
        .execution_options(synchronize_session=False)
    )
    target.status = StrategyStatus.ACTIVE

    await session.commit()
    return await get_strategy(session, workspace_id=workspace_id, strategy_id=strategy_id)
