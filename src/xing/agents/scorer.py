"""选题打分：把候选排成有区分度的一列。

三个分数里只有两个来自模型，这是刻意的：

- ``relevance_score`` / ``potential_score`` —— 模型判。前者对照战略配置里的
  受众与调性，后者判「这个题能不能做出好内容」。
- ``trend_score`` —— **算出来**。取种子的新鲜度（24 小时衰减）。模型没有
  时间感知，让它「估计热度」只会得到一个和摘要措辞相关的数。
- ``risk_score`` —— 这一版留空。填它之前要先有一份用户可编辑的红线清单，
  否则「风险 20 分」没有任何可解释性，比 NULL 更糟。

排序用加权总分，前 ``RECOMMEND_LIMIT`` 个标 ``RECOMMENDED``，其余留在
``CANDIDATE`` —— 两者都在 ``TopicStatus.SELECTABLE`` 里，人都能选。
``overall`` 本身不落库：它依赖当时的权重，存下来会在调权重后变成假历史
（与 ``topic_service.recommend`` 的处理一致）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import func, select, update

from xing.agents import runtime, schemas
from xing.core.errors import NonRetryableError
from xing.db.worker import WorkerSession
from xing.models.event import ExternalEvent
from xing.models.runtime import LlmPurpose
from xing.models.strategy import Strategy, StrategyStatus, StrategyVersion
from xing.models.topic import Topic, TopicStatus
from xing.worker.context import TaskContext
from xing.worker.handlers import handler

log = structlog.get_logger(__name__)

AGENT_NAME = "scorer"

#: 进入「推荐」的数量。与展开数量（6）配套：6 产 3，有得挑但不泛滥。
RECOMMEND_LIMIT = 3

MAX_OUTPUT_TOKENS = 1800

#: 权重。有 trend 时三档，缺 trend 时把它的份额按比例分给另外两个 ——
#: 直接少一项会让总分上限降到 0.8，排序看起来像「全都很差」。
_W_TREND = Decimal("0.20")
_W_RELEVANCE = Decimal("0.45")
_W_POTENTIAL = Decimal("0.35")

#: 舆论检测结论的排序档位。没有检测结论（历史数据）按 WEAK 处理：
#: 它仍然可以推荐，但排在已检测且成立的那些之后。
_OPINION_TIER = {"GROUNDED": 0, "WEAK": 1}

SYSTEM_PROMPT = """你是 XING 的选题评估 Agent。你会看到一批候选选题，逐个打分。

打分维度只有两个：
- relevance（0~1）：与账号定位的相关度。看受众是否重合、论点是否落在这个账号的边界内。
  没有给定位时，按「这个受众是否清晰可触达」判断。
- potential（0~1）：这个选题能不能做出有价值的内容。看它是否有可被反驳的判断、
  是否有足够的讨论空间、是否只是一个人人都同意的常识。

不要因为题材严肃、偏法律或偏财务就压低 potential —— 那类题往往正是决策者真正在
权衡的东西。判断标准始终是「它有没有具体、可反驳、带场景的判断」，不是「读起来轻不轻松」。

评分必须拉开差距。**不要把分数堆在 0.7~0.85**：如果两个候选确实一好一差，
就给 0.9 和 0.3。全部挤在中间等于没排。

reason 写清「为什么是这个分」，一句话，要能被复核。不要复述选题。

只输出 JSON：{"scores": [{"index": 1, "relevance": 0.8, "potential": 0.6, "reason": "..."}]}
index 是候选的序号（从 1 开始）。不要输出解释、不要 Markdown 代码块。"""


@handler("score_topics")
async def score_topics(task: TaskContext) -> dict[str, Any]:
    raw = task.input.get("seed_task_id")
    if not raw:
        raise NonRetryableError("score_topics 需要 input.seed_task_id", code="MISSING_SEED_TASK")
    seed_task_id = UUID(str(raw))

    candidates = await _load(task.workspace_id, seed_task_id)
    if not candidates:
        raise NonRetryableError("没有可打分的候选选题", code="NO_CANDIDATES")

    # 重跑幂等：已经打过分就复用。调权重属于「重新评估」，应当显式清空分数，
    # 而不是让重跑悄悄覆盖掉人已经看过的结果。
    if all(c.relevance_score is not None for c in candidates):
        log.info("agent.score_reused", task_id=str(task.id), count=len(candidates))
        return {"scored": len(candidates), "reused": True}

    strategy_config = await _active_strategy_config(task.workspace_id)
    trend = await _trend_baseline(task.workspace_id, candidates[0].seed_event_id)

    run_id = await runtime.start_run(
        runtime.RunSpec(
            workspace_id=task.workspace_id,
            agent_name=AGENT_NAME,
            goal=f"给 {len(candidates)} 个候选选题打分",
            task_id=task.id,
            attempt=task.attempt,
            max_cost=_max_cost(task),
            max_steps=3,
        )
    )

    try:
        step_no = await runtime.begin_step(run_id)
        result = await runtime.call_structured(
            run_id,
            purpose=LlmPurpose.SELF_CHECK,
            step_no=step_no,
            system=SYSTEM_PROMPT,
            prompt=_build_prompt(candidates, strategy_config),
            schema=schemas.TopicScores,
            shape=schemas.TOPIC_SCORES_SHAPE,
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )
        ranked = await _apply(candidates, result, trend)
        await runtime.finish_run(run_id, output={"ranking": ranked})
    except BaseException as exc:
        await runtime.close_run_safely(run_id, exc)
        raise

    recommended = [item for item in ranked if item["status"] == TopicStatus.RECOMMENDED]
    log.info(
        "agent.scored",
        task_id=str(task.id),
        scored=len(ranked),
        recommended=len(recommended),
        trend=trend,
    )
    return {
        "scored": len(ranked),
        "recommended": [item["topic_id"] for item in recommended],
        "ranking": ranked,
        "run_id": str(run_id),
    }


async def _load(workspace_id: UUID, seed_task_id: UUID) -> list[Topic]:
    """这一批候选。

    只取 ``CANDIDATE`` / ``RECOMMENDED``：舆论检测判为 UNGROUNDED 的候选已被
    置为 ``REJECTED``，它们不该再参与排序 —— 分数再高也只是「一个没人讨论的
    好角度」。

    序号（index）就是这个顺序，**必须稳定**：模型按 prompt 里的序号回分，
    重跑时如果顺序变了，分数就会贴到别的候选上。``created_at`` 不够 ——
    同一事务插入的多行时间是同一个值，所以要加 ``id`` 作二级键
    （uuidv7 单调递增，与插入顺序一致）。
    """
    async with WorkerSession() as session:
        rows = (
            await session.execute(
                select(Topic)
                .where(
                    Topic.workspace_id == workspace_id,
                    Topic.seed_task_id == seed_task_id,
                    Topic.status.in_((TopicStatus.CANDIDATE, TopicStatus.RECOMMENDED)),
                )
                .order_by(Topic.created_at, Topic.id)
            )
        ).scalars()
        return list(rows)


def _build_prompt(candidates: list[Topic], strategy: dict[str, Any] | None) -> str:
    lines: list[str] = []
    if strategy:
        lines.append("账号定位：\n" + json.dumps(strategy, ensure_ascii=False)[:1500] + "\n")

    for index, topic in enumerate(candidates, start=1):
        brief = topic.brief or {}
        lines.append(
            f"{index}. 标题：{topic.title}\n"
            f"   角度：{brief.get('angle', '（未提供）')}\n"
            f"   受众：{brief.get('audience', '（未提供）')}\n"
            f"   论点：{brief.get('thesis', '（未提供）')}\n"
            f"   时效：{brief.get('why_now', '（未提供）')}"
        )
    return "\n".join(lines)


async def _trend_baseline(workspace_id: UUID, event_id: UUID | None) -> Decimal | None:
    """种子的新鲜度。没有种子事件（方向型）就返回 None —— 空着比编一个数诚实。"""
    if event_id is None:
        return None

    async with WorkerSession() as session:
        occurred = (
            await session.execute(
                select(
                    func.coalesce(ExternalEvent.source_published_at, ExternalEvent.discovered_at)
                ).where(
                    ExternalEvent.id == event_id,
                    ExternalEvent.workspace_id == workspace_id,
                )
            )
        ).scalar()

    if occurred is None:
        return None
    age_hours = max(0.0, (datetime.now(UTC) - occurred).total_seconds() / 3600.0)
    return Decimal(str(round(1.0 / (1.0 + age_hours / 24.0), 4)))


async def _apply(
    candidates: list[Topic],
    result: schemas.TopicScores,
    trend: Decimal | None,
) -> list[dict[str, Any]]:
    """把评分写回，并按总分排序。

    越界的 ``index`` 直接忽略：模型偶尔会多给一条。整批失败会让链路极脆，
    而漏掉一条只是少一个候选的分 —— 它留在 CANDIDATE，人一样能看到。
    """
    by_index = {score.index: score for score in result.scores}

    w_relevance, w_potential = _W_RELEVANCE, _W_POTENTIAL
    if trend is None:
        # 把 trend 的份额按原比例摊给另外两个，保持权重和为 1。
        scale = Decimal("1") / (w_relevance + w_potential)
        w_relevance, w_potential = w_relevance * scale, w_potential * scale

    scored: list[dict[str, Any]] = []
    for index, topic in enumerate(candidates, start=1):
        score = by_index.get(index)
        if score is None:
            continue
        relevance = Decimal(str(round(score.relevance, 4)))
        potential = Decimal(str(round(score.potential, 4)))
        overall = w_relevance * relevance + w_potential * potential
        if trend is not None:
            overall += _W_TREND * trend
        verdict = str(((topic.brief or {}).get("opinion") or {}).get("verdict") or "")
        scored.append(
            {
                "topic": topic,
                "relevance": relevance,
                "potential": potential,
                "overall": round(float(overall), 4),
                "reason": score.reason,
                "opinion": verdict or None,
                # 舆论检测过的排前面。分数只回答「这个题好不好」，
                # 不回答「有没有人真的在讨论」—— 后者是门槛，不是加权项。
                "tier": _OPINION_TIER.get(verdict, 1),
            }
        )

    if not scored:
        raise NonRetryableError(
            "模型返回的 index 全部越界，没有一条评分可用", code="NO_VALID_SCORE"
        )

    scored.sort(key=lambda item: (item["tier"], -item["overall"]))

    updates: list[dict[str, Any]] = []
    ranked: list[dict[str, Any]] = []
    for rank, item in enumerate(scored):
        row: Topic = item["topic"]
        status = TopicStatus.RECOMMENDED if rank < RECOMMEND_LIMIT else TopicStatus.CANDIDATE
        updates.append(
            {
                "id": row.id,
                "relevance_score": item["relevance"],
                "potential_score": item["potential"],
                "trend_score": trend,
                "status": status,
                # 保留 expand 写的 angle/thesis，只把打分理由并进去。
                "brief": {**(row.brief or {}), "score_reason": item["reason"]},
            }
        )
        ranked.append(
            {
                "topic_id": str(row.id),
                "title": row.title,
                "overall": item["overall"],
                "relevance": float(item["relevance"]),
                "potential": float(item["potential"]),
                "trend": float(trend) if trend is not None else None,
                "status": status,
                "reason": item["reason"],
                "opinion": item["opinion"],
            }
        )

    async with WorkerSession() as session:
        # executemany，不是 N 条单发 UPDATE。synchronize_session=False 是必须的：
        # 会话里的 Topic 对象是另一个 session 加载的 detached 实例。
        await session.execute(update(Topic).execution_options(synchronize_session=False), updates)
        await session.commit()

    return ranked


async def _active_strategy_config(workspace_id: UUID) -> dict[str, Any] | None:
    async with WorkerSession() as session:
        row = (
            await session.execute(
                select(StrategyVersion.strategy_config)
                .join(Strategy, Strategy.id == StrategyVersion.strategy_id)
                .where(
                    Strategy.workspace_id == workspace_id,
                    Strategy.status == StrategyStatus.ACTIVE,
                    StrategyVersion.version_number == Strategy.current_version_number,
                )
                .limit(1)
            )
        ).first()
    return dict(row[0] or {}) if row is not None else None


def _max_cost(task: TaskContext) -> Decimal:
    raw = task.input.get("max_cost")
    return Decimal(str(raw)) if raw is not None else Decimal("0.3")
