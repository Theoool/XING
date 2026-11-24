"""选题服务（ORM 版）。

recommend 是 AIXING 第一个真正的业务算法接口，这一版不含 LLM：
    近期未被覆盖的事件 -> 生成候选选题 -> 按战略关键词命中度/新鲜度打分 -> 排序

打分逻辑仍是纯函数（``_score``）：它是接 AI 之前唯一可解释的东西，
坏掉时必须能单独测出来。

推荐是幂等的：重复调用不重复建选题，只重算分数并返回同一批结果。
「不重复建」依赖 ``topics_one_per_primary_event`` 这个部分唯一索引 +
``ON CONFLICT DO NOTHING``，并发调用时后到的那个静默跳过。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Interval, Uuid, func, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from xing.core.errors import ApiError
from xing.models.decision import Decision, DecisionStatus, DecisionTrace, DecisionType, TraceType
from xing.models.event import ExternalEvent
from xing.models.strategy import Strategy, StrategyStatus
from xing.models.topic import Topic, TopicStatus
from xing.schemas.topic import TopicOut, TopicRecommendation

# 时效性权重：社交平台上的东西半衰期更短，权重给高
_SOURCE_WEIGHT = {
    "SOCIAL": 1.0,
    "NEWS": 0.9,
    "SEARCH": 0.85,
    "RSS": 0.7,
    "API": 0.7,
    "MANUAL": 0.5,
    "OTHER": 0.5,
}


def _as_float(value: Any) -> float | None:
    """numeric 列在驱动层是 Decimal，写进 jsonb 前必须转成 float。"""
    return None if value is None else float(value)


def _collect_phrases(config: dict[str, Any]) -> list[str]:
    """把战略配置里所有字符串当成关键词。

    支持 ``Decimal`` 在内的非字符串一律跳过：``strategy_config`` 里可能有
    ``goals.monthly_publish = 8`` 这类数字，它们不是关键词。
    """
    phrases: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if len(normalized) >= 2:
                phrases.append(normalized)
        elif isinstance(value, list | tuple):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)

    walk(config)
    return list(dict.fromkeys(phrases))


def _score(
    *,
    title: str,
    summary: str | None,
    source_type: str | None,
    occurred_at: datetime | None,
    url: str | None,
    phrases: list[str],
    now: datetime,
) -> dict[str, float]:
    """返回 trend / relevance / potential 三个 0~1 的分值。

    三个信号的语义必须是可解释的，否则调参就是玄学：
      trend      —— 事件多新（24 小时衰减），乘平台权重
      relevance  —— 命中战略关键词的条数，3 条即满分
      potential  —— 素材完备度（有摘要/有链接/有发布时间），
                    它是「能不能立刻开工」的代理指标，不是传播预测
    """
    blob = f"{title} {summary or ''}".lower()

    hits = sum(1 for phrase in phrases if phrase in blob)
    relevance = min(1.0, hits / 3.0) if phrases else 0.0

    if occurred_at is None:
        trend = 0.0
    else:
        age_hours = max(0.0, (now - occurred_at).total_seconds() / 3600.0)
        decay = 1.0 / (1.0 + age_hours / 24.0)
        trend = min(1.0, decay * _SOURCE_WEIGHT.get(source_type or "OTHER", 0.5))

    potential = 0.0
    if summary:
        potential += 0.4
    if url:
        potential += 0.3
    if occurred_at is not None:
        potential += 0.3

    return {
        "trend_score": round(trend, 4),
        "relevance_score": round(relevance, 4),
        "potential_score": round(potential, 4),
    }


def _overall(scores: dict[str, float]) -> float:
    return round(
        0.55 * scores["relevance_score"]
        + 0.30 * scores["trend_score"]
        + 0.15 * scores["potential_score"],
        4,
    )


async def _active_strategy(session: AsyncSession, workspace_id: UUID) -> Strategy:
    """当前生效战略（带当前版本快照）。"""
    strategy = (
        await session.execute(
            select(Strategy).where(
                Strategy.workspace_id == workspace_id,
                Strategy.status == StrategyStatus.ACTIVE,
            )
        )
    ).scalar_one_or_none()

    if strategy is None:
        raise ApiError(
            409,
            "NO_ACTIVE_STRATEGY",
            "没有生效中的战略，无法评估选题相关性；请先创建并激活一条战略",
        )
    return strategy


async def recommend(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    limit: int = 10,
    lookback_days: int = 14,
) -> list[TopicRecommendation]:
    strategy = await _active_strategy(session, workspace_id)
    phrases = _collect_phrases(strategy.current_version.strategy_config or {})

    # 1) 把近期还没被任何选题覆盖的事件转成候选。
    #    集合插入 + ON CONFLICT DO NOTHING：并发调用时后到的那个不会插重复行，
    #    也不会报错（靠 topics_one_per_primary_event 这个部分唯一索引）。
    covered = (
        select(1)
        .where(
            Topic.workspace_id == ExternalEvent.workspace_id,
            Topic.primary_event_id == ExternalEvent.id,
        )
        .exists()
    )
    cutoff = func.now() - literal(timedelta(days=lookback_days), Interval)

    await session.execute(
        pg_insert(Topic)
        .from_select(
            ["workspace_id", "primary_event_id", "title", "description", "status"],
            select(
                ExternalEvent.workspace_id,
                ExternalEvent.id,
                # 标题截断到 500：上游标题可能很长，topics.title 没有长度限制，
                # 但 500 是数据库之外的第二道闸，避免异常输入把列表页撑爆
                func.left(ExternalEvent.title, 500),
                ExternalEvent.summary,
                literal(TopicStatus.CANDIDATE),
            ).where(
                ExternalEvent.workspace_id == workspace_id,
                func.coalesce(ExternalEvent.source_published_at, ExternalEvent.discovered_at)
                >= cutoff,
                ~covered,
            ),
        )
        .on_conflict_do_nothing()
    )

    # 2) 重新给所有候选打分。每次都重算，权重调整后结果立刻反映出来。
    topics = (
        (
            await session.execute(
                select(Topic)
                .options(joinedload(Topic.primary_event))
                .where(
                    Topic.workspace_id == workspace_id,
                    Topic.status == TopicStatus.CANDIDATE,
                )
                .order_by(Topic.discovered_at.desc())
                .limit(200)
            )
        )
        .scalars()
        .unique()
        .all()
    )

    now = datetime.now(UTC)
    updates: list[dict[str, Any]] = []
    results: list[TopicRecommendation] = []

    for topic in topics:
        event = topic.primary_event
        occurred_at = None
        if event is not None:
            occurred_at = event.source_published_at or event.discovered_at

        scores = _score(
            title=topic.title,
            summary=event.summary if event is not None else topic.description,
            source_type=event.source_type if event is not None else None,
            occurred_at=occurred_at,
            url=event.url if event is not None else None,
            phrases=phrases,
            now=now,
        )
        updates.append({**scores, "id": topic.id})
        results.append(
            TopicRecommendation(
                id=topic.id,
                primary_event_id=topic.primary_event_id,
                title=topic.title,
                description=topic.description,
                status=topic.status,
                trend_score=scores["trend_score"],
                relevance_score=scores["relevance_score"],
                potential_score=scores["potential_score"],
                risk_score=_as_float(topic.risk_score),
                discovered_at=topic.discovered_at,
                selected_at=topic.selected_at,
                overall_score=_overall(scores),
            )
        )

    if updates:
        # ORM 的「按主键批量 UPDATE」：一次 executemany，不是 200 条单发 UPDATE。
        # synchronize_session=False 是必须的 —— 会话里已加载的 Topic 对象不会
        # 因为这条语句而更新，反正下面返回的是重新构造的响应对象。
        await session.execute(
            update(Topic).execution_options(synchronize_session=False),
            updates,
        )

    await session.commit()

    results.sort(key=lambda item: item.overall_score, reverse=True)
    return results[:limit]


async def list_topics(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    status: str | None,
    limit: int,
) -> list[TopicOut]:
    stmt = (
        select(Topic)
        .where(Topic.workspace_id == workspace_id)
        .order_by(Topic.discovered_at.desc())
        .limit(limit)
    )
    if status:
        stmt = stmt.where(Topic.status == status)

    topics = (await session.execute(stmt)).scalars().all()
    return [TopicOut.model_validate(topic) for topic in topics]


async def get_topic(session: AsyncSession, *, workspace_id: UUID, topic_id: UUID) -> TopicOut:
    topic = (
        await session.execute(
            select(Topic)
            .where(Topic.id == topic_id, Topic.workspace_id == workspace_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()

    if topic is None:
        raise ApiError(404, "TOPIC_NOT_FOUND", "选题不存在")
    return TopicOut.model_validate(topic)


async def _resolve_strategy_version(session: AsyncSession, workspace_id: UUID) -> UUID:
    """决策必须绑定到某一版战略，版本由服务端解析，不接受客户端传。

    ``app.current_strategy_version`` 是数据库函数：它按 ``current_version_number``
    把「战略」解析成「具体某一版」。客户端只能表达「基于当前战略」，
    不能表达「基于 v1」—— 否则审计链可以被伪造。
    """
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


async def select_topic(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    topic_id: UUID,
    user_id: UUID,
    selected_option: str | None,
    rationale: str | None,
) -> tuple[TopicOut, UUID]:
    topic = await _lock_selectable_topic(session, workspace_id, topic_id)
    version_id = await _resolve_strategy_version(session, workspace_id)

    topic.status = TopicStatus.SELECTED
    topic.selected_at = func.now()

    decision = Decision(
        workspace_id=workspace_id,
        strategy_version_id=version_id,
        topic_id=topic_id,
        decision_type=DecisionType.TOPIC_SELECTION,
        status=DecisionStatus.APPROVED,
        selected_option=selected_option or topic.title,
        rationale=rationale,
        score=topic.relevance_score,
        decided_by=user_id,
        decided_at=func.now(),
    )
    session.add(decision)
    await session.flush()

    # 记下当时凭什么选它。这一步是决策可审计的全部价值所在。
    session.add(
        DecisionTrace(
            workspace_id=workspace_id,
            decision_id=decision.id,
            trace_type=TraceType.EVIDENCE,
            source_type="TOPIC",
            source_id=topic_id,
            # numeric 列回来是 Decimal，JSON 序列化不了，统一转 float
            data={
                "trend_score": _as_float(topic.trend_score),
                "relevance_score": _as_float(topic.relevance_score),
                "potential_score": _as_float(topic.potential_score),
                "risk_score": _as_float(topic.risk_score),
            },
        )
    )
    await session.commit()

    return (
        await get_topic(session, workspace_id=workspace_id, topic_id=topic_id),
        decision.id,
    )


async def reject_topic(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    topic_id: UUID,
    user_id: UUID,
    reason: str | None,
) -> TopicOut:
    topic = await _lock_selectable_topic(session, workspace_id, topic_id)
    version_id = await _resolve_strategy_version(session, workspace_id)

    topic.status = TopicStatus.REJECTED

    session.add(
        Decision(
            workspace_id=workspace_id,
            strategy_version_id=version_id,
            topic_id=topic_id,
            decision_type=DecisionType.TOPIC_SELECTION,
            status=DecisionStatus.REJECTED,
            rationale=reason,
            decided_by=user_id,
            decided_at=func.now(),
        )
    )
    await session.commit()

    return await get_topic(session, workspace_id=workspace_id, topic_id=topic_id)


async def _lock_selectable_topic(
    session: AsyncSession, workspace_id: UUID, topic_id: UUID
) -> Topic:
    topic = (
        await session.execute(
            select(Topic)
            .where(Topic.id == topic_id, Topic.workspace_id == workspace_id)
            .with_for_update()
        )
    ).scalar_one_or_none()

    if topic is None:
        raise ApiError(404, "TOPIC_NOT_FOUND", "选题不存在")
    if topic.status not in TopicStatus.SELECTABLE:
        raise ApiError(
            409,
            "TOPIC_NOT_SELECTABLE",
            f"选题当前状态为 {topic.status}，不能再次决策",
        )
    return topic
