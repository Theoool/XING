"""Researcher：把一段调研 brief 变成一条可引用的事实摘要。

产出落在 ``external_events`` —— 不新建「调研结果表」。理由：这张表已经是
「外部世界的一条信息」的权威表示（带来源、时间、全文检索索引），调研结论就是
其中一种。为它单开一张表会立刻产生第二个「事实从哪来」的答案。

一条调研 = 一行。``external_id = 'task:{task_id}'``，配合
``external_events_source_unique`` 让重跑幂等：租约丢失后重跑不会留下第二行，
也不会因为有旧行就跳过 —— 用 ``ON CONFLICT DO NOTHING`` 再查回，两条路径合一。

``source_type`` 用 ``OTHER`` 而不是 ``MANUAL``：这条记录不是人手工录入的，
标错来源会让后续「哪些事实是人给的、哪些是模型推的」这个判断失效。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from xing.agents import runtime, schemas
from xing.core.errors import NonRetryableError
from xing.db.worker import WorkerSession
from xing.models.event import ExternalEvent, SourceType
from xing.models.runtime import LlmPurpose
from xing.worker.context import TaskContext
from xing.worker.handlers import handler

log = structlog.get_logger(__name__)

AGENT_NAME = "researcher"

SYSTEM_PROMPT = """你是 XING 的调研 Agent。

根据给定的调研 brief，输出一份可以直接给写作者使用的事实摘要：
- summary：3-6 句话，只写你有把握的事实与背景，不要推测、不要编造数据与引用。
- keywords：5 个以内的关键词，用于后续检索。
- source_notes：如果 brief 里提到了具体来源，逐条记下；没有就给空数组。

只输出 JSON：{"summary": "...", "keywords": ["..."], "source_notes": ["..."]}
不要输出任何解释、不要 Markdown 代码块。
"""


@handler("research_topic")
async def research_topic(task: TaskContext) -> dict[str, Any]:
    brief = str(task.input.get("brief") or "").strip()
    if not brief:
        raise NonRetryableError("research_topic 需要 input.brief", code="MISSING_BRIEF")

    run_id = await runtime.start_run(
        runtime.RunSpec(
            workspace_id=task.workspace_id,
            agent_name=AGENT_NAME,
            goal=brief,
            task_id=task.id,
            attempt=task.attempt,
            parent_run_id=_uuid_or_none(task.input.get("parent_run_id")),
            max_cost=_decimal(task.input.get("max_cost"), "0.3"),
        )
    )

    try:
        step_no = await runtime.begin_step(run_id)
        result = await runtime.call_structured(
            run_id,
            purpose=LlmPurpose.GENERATE,
            step_no=step_no,
            system=SYSTEM_PROMPT,
            prompt=f"调研 brief：\n{brief}",
            schema=schemas.ResearchResult,
            shape=schemas.RESEARCH_SHAPE,
            max_output_tokens=_max_output_tokens(task),
        )
        event_id = await _upsert_event(task, result, run_id)
        output = {
            "event_id": str(event_id),
            "summary": result.summary,
            "keywords": result.keywords,
        }
        await runtime.finish_run(run_id, output=output)
    except BaseException as exc:
        await runtime.close_run_safely(run_id, exc)
        raise

    log.info("agent.research_done", task_id=str(task.id), event_id=str(event_id))
    return {**output, "run_id": str(run_id)}


async def _upsert_event(task: TaskContext, result: schemas.ResearchResult, run_id: UUID) -> UUID:
    external_id = f"task:{task.id}"
    async with WorkerSession() as session:
        inserted = await session.scalar(
            pg_insert(ExternalEvent)
            .values(
                workspace_id=task.workspace_id,
                source_type=SourceType.OTHER,
                external_id=external_id,
                title=_title(task, result),
                summary=result.summary,
                raw_payload={
                    "agent": AGENT_NAME,
                    "run_id": str(run_id),
                    "brief": str(task.input.get("brief") or ""),
                    "keywords": result.keywords,
                    "source_notes": result.source_notes,
                },
            )
            .on_conflict_do_nothing(index_elements=["workspace_id", "source_type", "external_id"])
            .returning(ExternalEvent.id)
        )
        if inserted is not None:
            await session.commit()
            return inserted

        existing = await session.scalar(
            select(ExternalEvent.id).where(
                ExternalEvent.workspace_id == task.workspace_id,
                ExternalEvent.source_type == SourceType.OTHER,
                ExternalEvent.external_id == external_id,
            )
        )
        await session.commit()
        if existing is None:
            # 冲突了却查不到：只可能是同事务内被删或跨租户撞键，两种都不该继续。
            raise NonRetryableError(
                f"调研记录插入失败且查不到既有行: {external_id}", code="EVENT_UPSERT_FAILED"
            )
        return existing


def _title(task: TaskContext, result: schemas.ResearchResult) -> str:
    raw = str(task.input.get("title") or task.input.get("brief") or "").strip()
    keyword = result.keywords[0] if result.keywords else ""
    return (raw or keyword or f"调研 {task.id}")[:200]


def _max_output_tokens(task: TaskContext) -> int:
    raw = task.input.get("max_output_tokens")
    return int(raw) if raw is not None else 1200


def _decimal(raw: Any, default: str) -> Decimal:
    return Decimal(str(raw)) if raw is not None else Decimal(default)


def _uuid_or_none(value: Any) -> UUID | None:
    return UUID(str(value)) if value else None


__all__ = ["AGENT_NAME", "research_topic"]
