"""取证轨迹的读取。

一个任务做过什么，分散在四张表里：``retrieval_traces``（搜了什么）、
``agent_runs`` / ``llm_calls``（想了什么）、``opinion_stances`` +
``external_events``（认定了什么）、``tasks``（结果如何）。这个服务把它们
拼成一条可以顺着读的时间线。

只读，不改任何状态 —— 取证的价值在于它是当时事实的记录，读取不该产生副作用。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xing.core.errors import ApiError
from xing.models.event import ExternalEvent
from xing.models.runtime import AgentRun, LlmCall
from xing.models.task import Task
from xing.models.topic import OpinionStance
from xing.models.trace import RetrievalTrace
from xing.schemas.trace import (
    TaskTrace,
    TraceDoc,
    TraceQuery,
    TraceRound,
    TraceStance,
    TraceStep,
    TraceSummary,
)
from xing.search.sources import classify, domain_of

#: 单条 prompt/响应在接口里的上限。展开的立场 prompt 实测 7~9k 字符，
#: 20000 足够完整回放；再长的多半是写作出错时的异常载荷，截断即可。
MAX_TEXT = 20_000


async def task_trace(session: AsyncSession, *, workspace_id: UUID, task_id: UUID) -> TaskTrace:
    task = await session.scalar(
        select(Task).where(Task.workspace_id == workspace_id, Task.id == task_id)
    )
    if task is None:
        raise ApiError(404, "TASK_NOT_FOUND", "任务不存在")

    run = await session.scalar(
        select(AgentRun)
        .where(AgentRun.workspace_id == workspace_id, AgentRun.task_id == task_id)
        .order_by(AgentRun.started_at.desc())
        .limit(1)
    )
    steps = [] if run is None else await _steps(session, run.id)
    rounds = await _rounds(session, task)
    stances = await _stances(session, workspace_id, task_id)

    query_count = sum(len(item.queries) for item in rounds)
    result_count = sum(item.result_count for item in rounds)
    kept = sum(item.new_count for item in rounds)
    mix: Counter[str] = Counter()
    for item in rounds:
        mix.update(item.source_mix)
    doc_total = sum(mix.values())

    return TaskTrace(
        task_id=task.id,
        task_type=task.task_type,
        status=task.status,
        attempt=task.retry_count + 1,
        dedup_key=task.dedup_key,
        created_at=task.created_at,
        started_at=task.started_at,
        completed_at=task.completed_at,
        error=dict(task.error) if task.error else None,
        input=dict(task.input or {}),
        output=dict(task.output) if task.output else None,
        run_id=run.id if run is not None else None,
        agent=run.agent_name if run is not None else None,
        goal=run.goal if run is not None else None,
        run_status=run.status if run is not None else None,
        rounds=rounds,
        steps=steps,
        stances=stances,
        summary=TraceSummary(
            query_count=query_count,
            doc_count=result_count,
            kept_count=kept,
            stance_count=len(stances),
            source_mix=dict(mix),
            ugc_ratio=round(mix["UGC"] / doc_total, 2) if doc_total else 0.0,
        ),
    )


async def _steps(session: AsyncSession, run_id: UUID) -> list[TraceStep]:
    rows = list(
        (
            await session.execute(
                select(LlmCall).where(LlmCall.run_id == run_id).order_by(LlmCall.call_seq)
            )
        )
        .scalars()
        .all()
    )
    return [_step(row) for row in rows]


def _step(row: LlmCall) -> TraceStep:
    request = row.request or {}
    response = row.response or {}
    system = _text(request.get("system"))
    prompt = _text(request.get("prompt"))
    text = _text(response.get("text"))
    truncated = any(
        isinstance(value, str) and len(value) > MAX_TEXT for value in (system, prompt, text)
    )
    return TraceStep(
        step_no=row.step_no,
        call_seq=row.call_seq,
        purpose=row.purpose,
        status=row.status,
        model=f"{row.model_provider}/{row.model_name}",
        latency_ms=row.latency_ms,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        cost=float(row.cost),
        system=system,
        prompt=prompt,
        response=text,
        error=_error(row.error),
        truncated=truncated,
    )


async def _rounds(session: AsyncSession, task: Task) -> list[TraceRound]:
    rows = list(
        (
            await session.execute(
                select(RetrievalTrace)
                .where(RetrievalTrace.task_id == task.id)
                .order_by(RetrievalTrace.round, RetrievalTrace.seq)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return []

    # 重试会留下上一轮的轨迹。默认只展示与任务当前 attempt 一致的那一份 ——
    # 把两次尝试的检索混在一起看，会误以为「这一轮搜了 12 条」。
    attempt = task.retry_count + 1
    current = [row for row in rows if row.attempt == attempt]
    chosen = current or rows

    grouped: dict[int, list[RetrievalTrace]] = defaultdict(list)
    for row in chosen:
        grouped[row.round].append(row)

    rounds: list[TraceRound] = []
    for round_no in sorted(grouped):
        queries = [_query(row) for row in grouped[round_no]]
        mix: Counter[str] = Counter()
        for query in queries:
            for doc in query.docs:
                if doc.kept:
                    mix[doc.source_kind] += 1
        rounds.append(
            TraceRound(
                round=round_no,
                queries=queries,
                query_count=len(queries),
                result_count=sum(query.result_count for query in queries),
                new_count=sum(query.new_count for query in queries),
                source_mix=dict(mix.most_common()),
            )
        )
    return rounds


def _query(row: RetrievalTrace) -> TraceQuery:
    docs = [_doc(item, index) for index, item in enumerate(row.docs or [], start=1)]
    return TraceQuery(
        seq=row.seq,
        layer=row.layer,
        query=row.query,
        target=row.target,
        provider=row.provider,
        options=dict(row.options or {}),
        status=row.status,
        latency_ms=row.latency_ms,
        result_count=row.result_count,
        new_count=row.new_count,
        error=row.error,
        docs=docs,
    )


def _doc(item: dict[str, Any], index: int) -> TraceDoc:
    stored = str(item.get("source_kind") or "UNKNOWN")
    # 有 URL 才重算：没有 URL 的材料（模型端检索）当时的判定依据是标签，
    # 现在拿不到那个标签，重算只会把 MEDIA 误降成 UNKNOWN。
    now: str | None = None
    rule_now: str | None = None
    url = item.get("url")
    if isinstance(url, str) and url:
        kind, rule = classify(url)
        if kind != stored:
            now = kind
            rule_now = rule
    return TraceDoc(
        rank=int(item.get("rank") or index),
        title=str(item.get("title") or ""),
        url=url if isinstance(url, str) else None,
        domain=item.get("domain"),
        source_kind=stored,
        kind_rule=str(item.get("kind_rule") or "unknown"),
        published_at=item.get("published_at"),
        kept=bool(item.get("kept")),
        drop_reason=item.get("drop_reason"),
        source_kind_now=now,
        kind_rule_now=rule_now,
    )


async def _stances(session: AsyncSession, workspace_id: UUID, task_id: UUID) -> list[TraceStance]:
    rows = (
        await session.execute(
            select(OpinionStance, ExternalEvent)
            .join(
                ExternalEvent,
                (ExternalEvent.id == OpinionStance.event_id)
                & (ExternalEvent.workspace_id == OpinionStance.workspace_id),
            )
            .where(
                OpinionStance.workspace_id == workspace_id,
                OpinionStance.seed_task_id == task_id,
            )
            .order_by(OpinionStance.created_at)
        )
    ).all()
    out: list[TraceStance] = []
    for stance, event in rows:
        kind, _rule = classify(event.url)
        out.append(
            TraceStance(
                id=stance.id,
                label=stance.label,
                camp=stance.camp,
                claim=stance.claim,
                audience=stance.audience,
                quote=stance.quote,
                source_kind=kind,
                domain=domain_of(event.url),
                url=event.url,
                title=event.title,
            )
        )
    return out


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:MAX_TEXT]


def _error(value: dict[str, Any] | None) -> str | None:
    if not value:
        return None
    code = value.get("code")
    message = value.get("message")
    if code and message:
        return f"{code}: {message}"
    return str(code or message or value)


__all__ = ["task_trace"]
