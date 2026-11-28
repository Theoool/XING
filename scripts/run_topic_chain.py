"""跑一遍完整链路，用于人工验证（不进 CI，不参与 pytest）。

    方向 → 展开 → 打分 → 选定 → 深入（检索） → 写作 → 审核

为什么单独一个脚本：这条链的每一环都有单测，但单测用的是 stub provider 与
stub 检索器 —— 它们能证明「管道接对了」，证明不了「真实模型跑得通」。
真实跑的失败模式（结构化输出不合格、检索没有结果、引文全是改写）只有在这
里才会出现。

用法（容器内，需要真实 key）：

    LLM_PROVIDER=openai SEARCH_PROVIDER=qwen \
        python scripts/run_topic_chain.py "AI 落地为什么在企业里卡住"

不传方向时用一个内置的默认方向。脚本会建一个临时 workspace，跑完打印
brief、证据、成稿与成本；数据留在库里便于用 SQL 继续查。
"""

from __future__ import annotations

import asyncio
import json
import sys
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from xing.core.config import settings
from xing.db.worker import WorkerSession, dispose
from xing.models.content import Content, ContentVersion
from xing.models.event import ExternalEvent
from xing.models.runtime import AgentRun, LlmCall
from xing.models.task import Task, TaskStatus
from xing.models.topic import OpinionStance, Topic, TopicEvent
from xing.worker import runner

DEFAULT_DIRECTION = "AI 落地为什么在企业里卡住：不是模型能力，是没人敢签字"


async def main(direction: str) -> None:
    workspace_id, user_id = await _bootstrap()
    print(f"workspace = {workspace_id}")

    expand_id = await _enqueue(
        workspace_id,
        "expand_topics",
        {"seed_type": "direction", "direction": direction},
        dedup_key=f"{uuid4()}:expand",
    )
    await _drain()
    expand = await _task(expand_id)
    output = expand.output or {}
    print(
        f"\n[展开] {expand.status}  provider={output.get('provider')} "
        f"docs={output.get('doc_count')} stances={output.get('stance_count')} "
        f"dropped_stances={output.get('dropped_stances')} "
        f"dropped_candidates={output.get('dropped_candidates')}"
    )
    if expand.status != TaskStatus.SUCCEEDED:
        print(json.dumps(expand.error, ensure_ascii=False, indent=2))
        return

    _print_supplement(output.get("supplement") or {})

    pool = await _stances(expand_id)
    print(f"\n[立场池] {len(pool)} 条")
    for stance in pool:
        print(f"  [{stance.camp}] {stance.label}｜{stance.claim}")
        print(f"      谁在说：{stance.audience}")
        print(f"      原文：{stance.quote[:90]}")

    topics = await _topics(workspace_id)
    print(f"\n[候选] {len(topics)} 个")
    for topic in topics:
        brief = topic.brief or {}
        opinion = brief.get("opinion") or {}
        print(
            f"  {float(topic.trend_score or 0):.3f} rel={float(topic.relevance_score or 0):.3f} "
            f"pot={float(topic.potential_score or 0):.3f} {topic.status:<12} "
            f"舆论={opinion.get('verdict')}  {topic.title}"
        )
        print(f"      角度：{brief.get('angle')}")
        labels = "、".join(item["label"] for item in brief.get("stances") or [])
        print(f"      建立在：{labels or '（无）'}")
        if opinion.get("verdict") != "GROUNDED":
            print(f"      检测：{opinion.get('reason')}")

    chosen = next((t for t in topics if t.status in ("RECOMMENDED", "CANDIDATE")), topics[0])
    print(f"\n[选定] {chosen.title}")

    deepen_id = await _enqueue(
        workspace_id,
        "deepen_topic",
        {"topic_id": str(chosen.id), "actor_id": str(user_id)},
        dedup_key=f"{chosen.id}:deepen",
    )
    await _drain()

    deepen = await _task(deepen_id)
    print(f"\n[深入] {deepen.status}")
    if deepen.status != TaskStatus.SUCCEEDED:
        print(json.dumps(deepen.error, ensure_ascii=False, indent=2))
        return

    topic = await _topic(chosen.id)
    brief = topic.brief or {}
    print(
        f"  provider = {(brief.get('search') or {}).get('provider')}  "
        f"degraded={(brief.get('search') or {}).get('degraded')}"
    )
    queries = json.dumps((brief.get("search") or {}).get("queries"), ensure_ascii=False)
    print(f"  检索 query：{queries}")
    print(f"  论点：{brief.get('thesis')}")
    print(f"  差异化：{brief.get('differentiator')}")
    print("  结构：")
    for index, item in enumerate(brief.get("outline") or [], start=1):
        print(f"    {index}. {item}")
    print("  反例：")
    for item in brief.get("counterpoints") or []:
        print(f"    - {item}")

    links = await _evidence(chosen.id)
    print(f"\n[证据] {len(links)} 条")
    for link in links:
        print(f"  [{link.role}] {link.quote[:120]}")
        print(f"       → {link.title[:60]} / {link.url or link.label}")

    content = await _content()
    if content is not None:
        print(f"\n[成稿] {content.title}（{len(content.body)} 字）")
        print("  " + content.body[:600].replace("\n", "\n  "))

    await _report_costs(workspace_id)


async def _bootstrap() -> tuple[UUID, UUID]:
    admin = create_async_engine(settings.migration_database_url)
    async with admin.begin() as conn:
        user_id = (
            await conn.execute(
                text(
                    "insert into users (email, display_name) "
                    "values (:email, 'chain-smoke') returning id"
                ),
                {"email": f"chain-{uuid4()}@x.test"},
            )
        ).scalar_one()
        workspace_id = (
            await conn.execute(
                text(
                    "insert into workspaces (name, created_by) "
                    "values ('chain-smoke', :uid) returning id"
                ),
                {"uid": user_id},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "insert into workspace_members (workspace_id, user_id, role) "
                "values (:ws, :uid, 'OWNER')"
            ),
            {"ws": workspace_id, "uid": user_id},
        )
    await admin.dispose()
    return workspace_id, user_id


async def _enqueue(ws: UUID, task_type: str, payload: dict, *, dedup_key: str) -> UUID:
    async with WorkerSession() as session:
        task = Task(workspace_id=ws, task_type=task_type, input=payload, dedup_key=dedup_key)
        session.add(task)
        await session.commit()
        return task.id


async def _drain(limit: int = 20) -> None:
    for _ in range(limit):
        outcome = await runner.claim_and_run(worker_id="chain-smoke")
        if outcome is None:
            return
        print(f"    · {outcome.status} {outcome.detail[:120]}")


async def _task(task_id: UUID) -> Task:
    async with WorkerSession() as session:
        return (await session.execute(select(Task).where(Task.id == task_id))).scalar_one()


async def _topic(topic_id: UUID) -> Topic:
    async with WorkerSession() as session:
        return (await session.execute(select(Topic).where(Topic.id == topic_id))).scalar_one()


def _print_supplement(supplement: dict) -> None:
    """补采留痕：补了几轮、补了什么、补完还缺什么。没补也要说出来。"""
    if not supplement.get("rounds"):
        missing = supplement.get("missing_after") or []
        print(f"[补采] 未触发（缺 {'、'.join(missing) or '无'} 但检索器是 stub）")
        return
    print(
        f"[补采] 1 轮：缺 {'、'.join(supplement.get('missing_before') or [])} → "
        f"新增 {supplement.get('new_docs')} 篇 / {supplement.get('new_stances')} 条立场，"
        f"补完仍缺 {'、'.join(supplement.get('missing_after') or []) or '无'}"
    )
    for query in supplement.get("queries") or []:
        print(f"      · {query}")


async def _stances(seed_task_id: UUID) -> list[OpinionStance]:
    async with WorkerSession() as session:
        return list(
            (
                await session.execute(
                    select(OpinionStance)
                    .where(OpinionStance.seed_task_id == seed_task_id)
                    .order_by(OpinionStance.created_at, OpinionStance.id)
                )
            ).scalars()
        )


async def _topics(ws: UUID) -> list[Topic]:
    async with WorkerSession() as session:
        rows = (
            await session.execute(
                select(Topic)
                .where(Topic.workspace_id == ws)
                .order_by(Topic.potential_score.desc().nullslast(), Topic.created_at)
            )
        ).scalars()
        return list(rows)


class _Link:
    def __init__(self, role: str, quote: str, title: str, url: str | None, label: str):
        self.role, self.quote, self.title, self.url, self.label = role, quote, title, url, label


async def _evidence(topic_id: UUID) -> list[_Link]:
    async with WorkerSession() as session:
        rows = (
            await session.execute(
                select(
                    TopicEvent.role,
                    TopicEvent.quote,
                    ExternalEvent.title,
                    ExternalEvent.url,
                    ExternalEvent.raw_payload,
                )
                .join(ExternalEvent, ExternalEvent.id == TopicEvent.event_id)
                .where(TopicEvent.topic_id == topic_id)
                .order_by(TopicEvent.weight.desc())
            )
        ).all()
    return [
        _Link(role, str(quote or ""), title, url, str((raw or {}).get("source_label") or ""))
        for role, quote, title, url, raw in rows
    ]


class _Content:
    def __init__(self, title: str, body: str):
        self.title, self.body = title, body


async def _content() -> _Content | None:
    async with WorkerSession() as session:
        row = (
            await session.execute(
                select(Content, ContentVersion.body)
                .join(ContentVersion, ContentVersion.content_id == Content.id)
                .order_by(Content.created_at.desc())
                .limit(1)
            )
        ).first()
    return None if row is None else _Content(row[0].title, row[1])


async def _report_costs(ws: UUID) -> None:
    async with WorkerSession() as session:
        rows = (
            await session.execute(
                select(AgentRun.agent_name, AgentRun.total_cost, AgentRun.total_tokens)
                .where(AgentRun.workspace_id == ws)
                .order_by(AgentRun.started_at)
            )
        ).all()
        calls = (
            await session.execute(
                select(LlmCall.purpose, LlmCall.input_tokens, LlmCall.output_tokens).where(
                    LlmCall.workspace_id == ws
                )
            )
        ).all()

    total = Decimal(0)
    print("\n[成本]")
    for name, cost, tokens in rows:
        total += cost
        print(f"  {name:<14} {cost}  {tokens} tokens")
    web = sum(1 for purpose, _, _ in calls if purpose == "tool_decision")
    print(f"  合计 {total}   调用 {len(calls)} 次")
    # 两类后端的账不一样：模型端检索走 llm_calls，HTTP 检索（tavily）不进账本，
    # 只体现在 brief.search 的 queries 上。混在一起报会误导。
    print(f"  其中模型端联网检索 {web} 次（tavily 这类 HTTP 检索不计入 llm_calls）")


if __name__ == "__main__":

    async def _amain() -> None:
        try:
            await main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DIRECTION)
        finally:
            # 连接池是模块级全局的，事件循环一关就会在 close 阶段报
            # "Event loop is closed"。显式释放比让它们随进程消失干净。
            await dispose()

    asyncio.run(_amain())
