"""决策服务（ORM 版）。

两条硬规则，与实现无关：

1. ``strategy_version_id`` 由服务端解析（``app.current_strategy_version``），
   客户端只能表达「基于哪个战略」，不能指定「基于哪一版」。
2. 不存模型的思维链。``decision_traces`` 里存的是可审计的证据与打分，
   而不是推理过程原文。

``traces`` 用 ``lazy="selectin"``：详情与列表都要带证据，selectin 是
「一条主查询 + 一条 IN 查询」，不会像 join 那样放大主表行数。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Uuid, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from xing.core.errors import ApiError
from xing.models.decision import Decision, DecisionStatus, DecisionTrace
from xing.models.strategy import Strategy, StrategyStatus
from xing.schemas.decision import DecisionCreate, DecisionOut


def _to_out(decision: Decision) -> DecisionOut:
    """ORM 对象 -> 响应模型。

    ``traces`` 必须手工转 dict：schema 里它是 ``list[dict]``，为的是保持
    与老版本 ``json_agg`` 完全相同的响应形状（不含 trace 的 id 与 workspace_id）。
    直接交给 Pydantic 会拒绝 ORM 实例。
    """
    return DecisionOut(
        id=decision.id,
        strategy_version_id=decision.strategy_version_id,
        topic_id=decision.topic_id,
        decision_type=decision.decision_type,
        status=decision.status,
        selected_option=decision.selected_option,
        rationale=decision.rationale,
        # numeric 列是 Decimal，响应模型声明的是 float
        score=float(decision.score) if decision.score is not None else None,
        decided_by=decision.decided_by,
        decided_at=decision.decided_at,
        created_at=decision.created_at,
        traces=[
            {
                "trace_type": trace.trace_type,
                "source_type": trace.source_type,
                "source_id": trace.source_id,
                "data": trace.data,
            }
            for trace in decision.traces
        ],
    )


async def _resolve_strategy_version(session: AsyncSession, workspace_id: UUID) -> UUID:
    version_id = (
        await session.execute(
            select(func.app.current_strategy_version(Strategy.id, type_=Uuid())).where(
                Strategy.workspace_id == workspace_id,
                Strategy.status == StrategyStatus.ACTIVE,
            )
        )
    ).scalar()

    if version_id is None:
        raise ApiError(
            409,
            "NO_ACTIVE_STRATEGY",
            "没有生效中的战略（或其当前版本不存在），无法记录决策",
        )
    return version_id


async def create_decision(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    user_id: UUID,
    payload: DecisionCreate,
) -> DecisionOut:
    version_id = await _resolve_strategy_version(session, workspace_id)

    decision = Decision(
        workspace_id=workspace_id,
        strategy_version_id=version_id,
        topic_id=payload.topic_id,
        decision_type=payload.decision_type,
        status=DecisionStatus.PROPOSED,
        selected_option=payload.selected_option,
        rationale=payload.rationale,
        score=payload.score,
        decided_by=user_id,
    )
    session.add(decision)
    await session.flush()

    # 多条证据一次性 add_all，flush 时是 executemany
    session.add_all(
        [
            DecisionTrace(
                workspace_id=workspace_id,
                decision_id=decision.id,
                trace_type=trace.trace_type,
                source_type=trace.source_type,
                source_id=trace.source_id,
                data=trace.data,
            )
            for trace in payload.traces
        ]
    )

    await session.commit()
    return await get_decision(session, workspace_id=workspace_id, decision_id=decision.id)


async def get_decision(
    session: AsyncSession, *, workspace_id: UUID, decision_id: UUID
) -> DecisionOut:
    decision = (
        await session.execute(
            select(Decision)
            .where(Decision.id == decision_id, Decision.workspace_id == workspace_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()

    if decision is None:
        raise ApiError(404, "DECISION_NOT_FOUND", "决策不存在")
    return _to_out(decision)


async def list_decisions(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    decision_type: str | None,
    limit: int,
) -> list[DecisionOut]:
    stmt = (
        select(Decision)
        .where(Decision.workspace_id == workspace_id)
        .order_by(Decision.created_at.desc())
        .limit(limit)
    )
    if decision_type:
        stmt = stmt.where(Decision.decision_type == decision_type)

    decisions = (await session.execute(stmt)).scalars().all()
    return [_to_out(decision) for decision in decisions]


async def review_decision(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    decision_id: UUID,
    user_id: UUID,
    approve: bool,
    comment: str | None,
) -> DecisionOut:
    """批准/驳回。只允许从 PROPOSED 迁移，重复审批返回 409 而不是静默覆盖。

    老实现靠「UPDATE ... WHERE status='PROPOSED' 影响 0 行」反推是 404 还是 409，
    要补一次查询；现在锁住行之后直接看得见状态，判断是直白的。
    """
    decision = (
        await session.execute(
            select(Decision)
            .where(Decision.id == decision_id, Decision.workspace_id == workspace_id)
            .with_for_update()
        )
    ).scalar_one_or_none()

    if decision is None:
        raise ApiError(404, "DECISION_NOT_FOUND", "决策不存在")
    if decision.status != DecisionStatus.PROPOSED:
        raise ApiError(
            409,
            "DECISION_NOT_PROPOSED",
            "只有 PROPOSED 状态的决策可以审核",
        )

    decision.status = DecisionStatus.APPROVED if approve else DecisionStatus.REJECTED
    # coalesce：已经有决定人时不覆盖（例如系统先记了 propose 的操作人）
    decision.decided_by = decision.decided_by or user_id
    decision.decided_at = func.now()
    # coalesce(:comment, rationale)：没给意见就保留原来的理由
    if comment is not None:
        decision.rationale = comment

    await session.commit()
    return await get_decision(session, workspace_id=workspace_id, decision_id=decision_id)
