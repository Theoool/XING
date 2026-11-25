"""第一个真实 Agent（writer）的端到端测试。

验证的不是「文章写得好不好」—— stub provider 的文本是伪随机串。
要验证的是三件工程事实：

1. 一次任务执行在库里留下**完整可对上账**的痕迹：
   ``tasks`` / ``agent_runs`` / ``llm_calls`` / ``contents`` / ``content_versions``
2. 预算耗尽时**不留半成品**，且不进重试（确定性失败重试只是烧钱）
3. 同一任务重跑不会产出第二篇内容（幂等靠 ``metadata->>'task_id'``）
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from xing.core.config import settings
from xing.db.worker import WorkerSession
from xing.db.worker import dispose as dispose_worker
from xing.models.content import Content, ContentStatus, ContentVersion
from xing.models.runtime import AgentRun, AgentRunStatus, LlmCall
from xing.models.task import Task, TaskStatus
from xing.worker import handlers, runner
from xing.worker.context import TaskContext


@pytest.fixture(autouse=True)
async def _dispose_worker_engine():
    yield
    await dispose_worker()


@pytest.fixture
async def workspace():
    admin = create_async_engine(settings.migration_database_url, poolclass=NullPool)
    async with admin.begin() as conn:
        user_id = (
            await conn.execute(
                text(
                    "insert into users (email, display_name) "
                    "values (:email, 'writer-test') returning id"
                ),
                {"email": f"writer-{uuid4()}@x.test"},
            )
        ).scalar_one()
        ws_id = (
            await conn.execute(
                text(
                    "insert into workspaces (name, created_by) "
                    "values ('writer-test', :uid) returning id"
                ),
                {"uid": user_id},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "insert into workspace_members (workspace_id, user_id, role) "
                "values (:ws, :uid, 'OWNER')"
            ),
            {"ws": ws_id, "uid": user_id},
        )

    yield ws_id, user_id

    async with admin.begin() as conn:
        await conn.execute(text("delete from workspaces where id = :id"), {"id": ws_id})
    await admin.dispose()


async def _add_task(ws: UUID, payload: dict[str, Any]) -> UUID:
    async with WorkerSession() as session:
        task = Task(
            workspace_id=ws,
            task_type="write_content",
            input=payload,
        )
        session.add(task)
        await session.commit()
        return task.id


async def _task(task_id: UUID) -> Task:
    async with WorkerSession() as session:
        return (await session.execute(select(Task).where(Task.id == task_id))).scalar_one()


async def _runs(task_id: UUID) -> list[AgentRun]:
    async with WorkerSession() as session:
        return list(
            (await session.execute(select(AgentRun).where(AgentRun.task_id == task_id)))
            .scalars()
            .all()
        )


async def _contents(ws: UUID) -> list[Content]:
    async with WorkerSession() as session:
        return list(
            (await session.execute(select(Content).where(Content.workspace_id == ws)))
            .scalars()
            .all()
        )


async def test_write_content_leaves_a_complete_audit_trail(workspace):
    ws, user_id = workspace
    task_id = await _add_task(
        ws,
        {
            "title": "为什么先做执行底座",
            "brief": "解释为什么 Agent 系统要先有可审计的执行层，再谈智能。",
        },
    )

    outcome = await runner.claim_and_run(worker_id="w1")
    assert outcome is not None
    assert outcome.status == "SUCCEEDED", outcome

    task = await _task(task_id)
    assert task.status == TaskStatus.SUCCEEDED
    assert task.error is None
    content_id = UUID(task.output["content_id"])

    contents = await _contents(ws)
    assert len(contents) == 1
    assert contents[0].id == content_id
    assert contents[0].status == ContentStatus.DRAFT
    assert contents[0].current_version_number == 1
    # Agent 不是人：created_by 必须挂到真实用户（这里是 OWNER）
    assert contents[0].created_by == user_id

    async with WorkerSession() as session:
        version = (
            await session.execute(
                select(ContentVersion).where(ContentVersion.content_id == content_id)
            )
        ).scalar_one()
    assert version.version_number == 1
    assert version.body
    assert version.extra_metadata["task_id"] == str(task_id)

    runs = await _runs(task_id)
    assert len(runs) == 1
    run = runs[0]
    assert run.status == AgentRunStatus.SUCCEEDED
    assert run.agent_name == "writer"
    assert run.attempt == 1
    assert run.step_count == 1
    assert run.total_cost > 0
    assert run.reserved_cost == 0
    assert run.output["content_id"] == str(content_id)

    async with WorkerSession() as session:
        calls = list(
            (
                await session.execute(select(LlmCall).where(LlmCall.run_id == run.id))
            )
            .scalars()
            .all()
        )
    assert len(calls) == 1
    assert calls[0].purpose == "generate"
    assert calls[0].cost == run.total_cost
    assert calls[0].response


async def test_budget_exhausted_leaves_no_partial_content(workspace):
    ws, _ = workspace
    task_id = await _add_task(
        ws,
        {"brief": "预算不够时不应该留下任何内容", "max_cost": "0.0001"},
    )

    outcome = await runner.claim_and_run(worker_id="w1")
    assert outcome is not None
    # 确定性失败：直接终态，不进 RETRYING
    assert outcome.status == "FAILED", outcome

    task = await _task(task_id)
    assert task.status == TaskStatus.FAILED
    assert task.error is not None
    assert task.error["code"] == "BUDGET_EXCEEDED"
    assert task.retry_count == 0, "预算类失败不应消耗重试额度"

    assert await _contents(ws) == [], "超预算仍写出了内容，说明写入没有排在预算之后"

    runs = await _runs(task_id)
    assert len(runs) == 1
    assert runs[0].status == AgentRunStatus.FAILED
    assert runs[0].error is not None
    assert runs[0].error["code"] == "BUDGET_EXCEEDED"
    # 预留必须释放，否则这个 run 的余额被永久占住
    assert runs[0].reserved_cost == 0


async def test_rerun_of_same_task_reuses_the_draft(workspace):
    ws, _ = workspace
    task_id = await _add_task(
        ws, {"title": "重跑不应重复产出", "brief": "同一个任务执行两次只应留下一篇内容。"}
    )

    assert (await runner.claim_and_run(worker_id="w1")).status == "SUCCEEDED"  # type: ignore[union-attr]

    # 模拟租约丢失后由新 worker 接管：handler 会被再跑一次
    fn = handlers.get_handler("write_content")
    assert fn is not None
    task = await _task(task_id)
    second = await fn(TaskContext.from_task(task))

    assert second["reused"] is True
    assert second["content_id"] == task.output["content_id"]

    contents = await _contents(ws)
    assert len(contents) == 1, f"重跑产出了重复内容: {[str(c.id) for c in contents]}"

    runs = await _runs(task_id)
    assert len(runs) == 1, "幂等命中不应再开一次 run（那会重复计费）"
