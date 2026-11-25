"""Phase 2 任务图：一条 goal 跑完全链路。

要验证的四件工程事实（不是「内容好不好」）：

1. 一条 ``plan_content`` 能在库里长出一张正确的任务图，并逐个被执行完；
2. 四个 run 的成本能按 ``parent_run_id`` 加总，且等于 ``llm_calls`` 明细之和；
3. 计划重跑不会长出第二条生产线（``tasks.dedup_key``）；
4. 上游永久失败时下游变成 ``CANCELLED``，而不是永远 ``PENDING``。
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from xing.core.config import settings
from xing.db.worker import WorkerSession
from xing.db.worker import dispose as dispose_worker
from xing.models.content import Approval, ApprovalStatus, Content, ContentStatus
from xing.models.runtime import AgentRun, AgentRunStatus, LlmCall
from xing.models.task import Task, TaskStatus
from xing.worker import handlers, reaper, runner
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
                    "values (:email, 'pipeline-test') returning id"
                ),
                {"email": f"pipe-{uuid4()}@x.test"},
            )
        ).scalar_one()
        ws_id = (
            await conn.execute(
                text(
                    "insert into workspaces (name, created_by) "
                    "values ('pipeline-test', :uid) returning id"
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


async def _add_task(
    ws: UUID, task_type: str, payload: dict[str, Any], depends_on: list[UUID] | None = None
) -> UUID:
    async with WorkerSession() as session:
        task = Task(
            workspace_id=ws, task_type=task_type, input=payload, depends_on=depends_on or []
        )
        session.add(task)
        await session.commit()
        return task.id


async def _task(task_id: UUID) -> Task:
    async with WorkerSession() as session:
        return (await session.execute(select(Task).where(Task.id == task_id))).scalar_one()


async def _tasks(ws: UUID) -> list[Task]:
    async with WorkerSession() as session:
        return list(
            (await session.execute(select(Task).where(Task.workspace_id == ws))).scalars().all()
        )


async def _drain(limit: int = 12) -> None:
    """把队列跑空。每个子任务成功后会由触发器把下游推进到 READY。"""
    for _ in range(limit):
        outcome = await runner.claim_and_run(worker_id="w-pipeline")
        if outcome is None:
            return
        assert outcome.status in {"SUCCEEDED", "SKIPPED", "FAILED"}, outcome


async def test_goal_produces_a_reviewable_draft_end_to_end(workspace):
    ws, _ = workspace
    plan_id = await _add_task(
        ws, "plan_content", {"goal": "讲清楚为什么先做执行层", "budget": "2.0"}
    )

    await _drain()

    plan = await _task(plan_id)
    assert plan.status == TaskStatus.SUCCEEDED, plan.error
    assert plan.output is not None

    tasks = {t.id: t for t in await _tasks(ws)}
    assert len(tasks) == 4, [f"{t.task_type}:{t.status}" for t in tasks.values()]
    assert all(t.status == TaskStatus.SUCCEEDED for t in tasks.values()), [
        (t.task_type, t.status, t.error) for t in tasks.values()
    ]

    # ---- 内容 ----
    async with WorkerSession() as session:
        contents = list(
            (await session.execute(select(Content).where(Content.workspace_id == ws)))
            .scalars()
            .all()
        )
        approvals = list(
            (await session.execute(select(Approval).where(Approval.workspace_id == ws)))
            .scalars()
            .all()
        )
    assert len(contents) == 1
    content = contents[0]
    assert content.status == ContentStatus.REVIEW, "审核完成后内容应进入待审"
    assert str(content.source_task_id) == plan.output["write_task_id"]

    assert len(approvals) == 1
    assert approvals[0].status == ApprovalStatus.PENDING
    assert approvals[0].stage == "CONTENT"
    assert content.status == ContentStatus.REVIEW

    # ---- 成本树 ----
    async with WorkerSession() as session:
        runs = list(
            (await session.execute(select(AgentRun).where(AgentRun.workspace_id == ws)))
            .scalars()
            .all()
        )
        call_total = await session.scalar(
            select(func.coalesce(func.sum(LlmCall.cost), 0)).where(LlmCall.workspace_id == ws)
        )
    assert len(runs) == 4, [r.agent_name for r in runs]
    assert all(r.status == AgentRunStatus.SUCCEEDED for r in runs)

    plan_run = next(r for r in runs if r.agent_name == "orchestrator")
    assert plan_run.plan is not None and "research_brief" in plan_run.plan
    children = [r for r in runs if r.agent_name != "orchestrator"]
    assert all(r.parent_run_id == plan_run.id for r in children), "子 run 必须挂在计划 run 下"

    run_total = sum(r.total_cost for r in runs)
    assert run_total == call_total, "run 的成本快照与 llm_calls 明细对不上"
    assert all(r.reserved_cost == 0 for r in runs)


async def test_replanning_the_same_task_does_not_duplicate_the_chain(workspace):
    """租约丢失 → 计划任务重跑：子任务必须复用，不能长出第二条生产线。"""
    ws, _ = workspace
    plan_id = await _add_task(ws, "plan_content", {"goal": "重跑不应重复建任务", "budget": "2.0"})
    task = await _task(plan_id)

    fn = handlers.get_handler("plan_content")
    assert fn is not None

    first = await fn(TaskContext.from_task(task))
    # 模拟第一次执行收尾前崩溃、租约被回收后第二次执行（attempt 由 retry_count 决定）
    second = await fn(dataclasses.replace(TaskContext.from_task(task), retry_count=1))

    assert second["write_task_id"] == first["write_task_id"]
    assert second["research_task_id"] == first["research_task_id"]

    tasks = await _tasks(ws)
    assert len(tasks) == 4, f"重复建了任务: {[t.task_type for t in tasks]}"
    assert all(t.dedup_key is not None for t in tasks if t.id != plan_id)

    # 两条 run 各自独立记账，但用的是同一批子任务
    async with WorkerSession() as session:
        runs = list(
            (await session.execute(select(AgentRun).where(AgentRun.task_id == plan_id)))
            .scalars()
            .all()
        )
    assert len(runs) == 2
    assert {r.attempt for r in runs} == {1, 2}


async def test_upstream_failure_cancels_dependents(workspace):
    """上游确定性失败后，下游必须被取消 —— 而不是永久 PENDING。"""
    ws, _ = workspace
    write_id = await _add_task(
        ws, "write_content", {"brief": "预算不够，必然失败", "max_cost": "0.0001"}
    )
    review_id = await _add_task(ws, "review_content", {"focus": "随便"}, depends_on=[write_id])

    assert (await _task(review_id)).status == TaskStatus.PENDING

    await _drain()

    upstream = await _task(write_id)
    assert upstream.status == TaskStatus.FAILED
    assert upstream.error is not None and upstream.error["code"] == "BUDGET_EXCEEDED"

    downstream = await _task(review_id)
    assert downstream.status == TaskStatus.CANCELLED, "上游失败后下游仍停在 PENDING"
    assert downstream.error is not None
    assert downstream.error["code"] == "UPSTREAM_FAILED"


async def test_reaper_closes_orphaned_runs_without_touching_spend(workspace):
    ws, _ = workspace
    done_id = await _add_task(ws, "echo", {"sleep": 0})
    await _drain()
    assert (await _task(done_id)).status == TaskStatus.SUCCEEDED

    async with WorkerSession() as session:
        orphan = AgentRun(
            workspace_id=ws,
            task_id=done_id,
            agent_name="writer",
            goal="被 SIGKILL 的那次执行",
            reserved_cost=Decimal("0.5"),
            total_cost=Decimal("0.1"),
        )
        # 没有任务可参照的超时 run
        lost = AgentRun(
            workspace_id=ws,
            agent_name="explorer",
            goal="超时的探索性执行",
            started_at=datetime.now(UTC) - timedelta(hours=2),
        )
        session.add_all([orphan, lost])
        await session.commit()
        orphan_id, lost_id = orphan.id, lost.id

        reaped = await reaper.reap_stale_runs(session)
        assert reaped == 2

    async with WorkerSession() as session:
        for run_id in (orphan_id, lost_id):
            run = (
                await session.execute(select(AgentRun).where(AgentRun.id == run_id))
            ).scalar_one()
            assert run.status == AgentRunStatus.FAILED
            assert run.error is not None and run.error["code"] == "ORPHANED_RUN"
            assert run.reserved_cost == 0
            assert run.completed_at is not None
        # 已经花掉的钱不能被抹掉 —— 那等于改账
        spent = (
            await session.execute(select(AgentRun.total_cost).where(AgentRun.id == orphan_id))
        ).scalar_one()
    assert spent > 0
