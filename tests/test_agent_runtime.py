"""Agent Runtime 的契约测试。

这里不测「模型好不好」，只测四件**推理不可靠、必须实测**的事：

1. 步数预算是原子的条件递增，到上限就拒绝（不是应用层 if）
2. 金额预算是「先预留后结算」，预留是上界，结算不会把账滚回去
3. 每次调用（含失败、含回放）都在 ``llm_calls`` 里留一行
4. 跨租户写 ``llm_calls`` 会被复合外键拒绝

用 ``xing_worker``（BYPASSRLS）连接，provider 用确定性的 stub：
没有 API key 也能把「预算 → 调用 → 记账 → 回放」整条链路跑通。
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from xing.agents import runtime
from xing.agents.provider import StubProvider
from xing.core.config import settings
from xing.db.worker import WorkerSession
from xing.db.worker import dispose as dispose_worker
from xing.models.runtime import AgentRun, AgentRunStatus, LlmCall, LlmCallStatus


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
                    "values (:email, 'runtime-test') returning id"
                ),
                {"email": f"runtime-{uuid4()}@x.test"},
            )
        ).scalar_one()
        ws_id = (
            await conn.execute(
                text(
                    "insert into workspaces (name, created_by) "
                    "values ('runtime-test', :uid) returning id"
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


async def _run(run_id: UUID) -> AgentRun:
    async with WorkerSession() as session:
        return (await session.execute(select(AgentRun).where(AgentRun.id == run_id))).scalar_one()


async def _calls(run_id: UUID) -> list[LlmCall]:
    async with WorkerSession() as session:
        return list(
            (
                await session.execute(
                    select(LlmCall).where(LlmCall.run_id == run_id).order_by(LlmCall.call_seq)
                )
            )
            .scalars()
            .all()
        )


async def test_step_budget_is_atomic_and_terminal(workspace):
    ws, _ = workspace
    run_id = await runtime.start_run(
        runtime.RunSpec(workspace_id=ws, agent_name="t", goal="steps", max_steps=2)
    )

    assert await runtime.begin_step(run_id) == 1
    assert await runtime.begin_step(run_id) == 2

    with pytest.raises(runtime.StepBudgetExceeded):
        await runtime.begin_step(run_id)

    row = await _run(run_id)
    assert row.step_count == 2
    assert row.status == AgentRunStatus.RUNNING


async def test_reservation_blocks_overspend_before_calling(workspace):
    ws, _ = workspace
    # 上限设得比任何一次调用的预留还小：预留阶段就该被拒
    run_id = await runtime.start_run(
        runtime.RunSpec(workspace_id=ws, agent_name="t", goal="poor", max_cost=Decimal("0.0001"))
    )
    step = await runtime.begin_step(run_id)

    with pytest.raises(runtime.BudgetExceeded):
        await runtime.call_model(run_id, purpose="generate", step_no=step, prompt="x" * 400)

    assert await _calls(run_id) == []
    assert await runtime.fail_run(run_id, code="BUDGET_EXCEEDED", message="too poor")

    row = await _run(run_id)
    assert row.status == AgentRunStatus.FAILED
    # 失败必须把预留清干净，否则余额被永久占住，重试也永远过不去
    assert row.reserved_cost == Decimal(0)


async def test_successful_call_records_detail_and_rolls_up(workspace):
    ws, _ = workspace
    run_id = await runtime.start_run(
        runtime.RunSpec(workspace_id=ws, agent_name="t", goal="generate")
    )
    step = await runtime.begin_step(run_id)

    response = await runtime.call_model(
        run_id, purpose="generate", step_no=step, prompt="写一段关于取舍的话"
    )
    assert response.text

    calls = await _calls(run_id)
    assert len(calls) == 1
    call = calls[0]
    assert call.status == LlmCallStatus.SUCCEEDED
    assert call.call_seq == 1
    assert call.purpose == "generate"
    assert call.cost > 0

    row = await _run(run_id)
    # 触发器累加：明细是权威源，run 上是快照
    assert row.total_cost == call.cost
    assert row.total_tokens == call.input_tokens + call.output_tokens
    # 预留已经释放
    assert row.reserved_cost == Decimal(0)
    # step_count 只由 Runtime 递增，不随调用次数变化
    assert row.step_count == 1


async def test_replay_returns_recorded_response_without_cost(workspace):
    ws, _ = workspace
    prompt = "同一个请求应该只花一次钱"

    first = await runtime.start_run(runtime.RunSpec(workspace_id=ws, agent_name="t", goal="first"))
    step = await runtime.begin_step(first)
    original = await runtime.call_model(
        first, purpose="generate", step_no=step, prompt=prompt, provider=StubProvider()
    )
    await runtime.finish_run(first)
    first_row = await _run(first)

    second = await runtime.start_run(
        runtime.RunSpec(workspace_id=ws, agent_name="t", goal="second")
    )
    step = await runtime.begin_step(second)
    replayed = await runtime.call_model(
        second, purpose="generate", step_no=step, prompt=prompt, provider=StubProvider()
    )

    assert replayed.text == original.text
    calls = await _calls(second)
    assert len(calls) == 1
    assert calls[0].status == LlmCallStatus.REPLAYED
    assert calls[0].cost == Decimal(0)

    second_row = await _run(second)
    assert second_row.total_cost == Decimal(0)
    assert second_row.total_tokens == 0
    # 第一次的钱照样记着，没有被回放覆盖
    assert first_row.total_cost > 0


async def test_llm_call_cannot_be_written_to_another_tenant(workspace):
    ws, _ = workspace
    run_id = await runtime.start_run(
        runtime.RunSpec(workspace_id=ws, agent_name="t", goal="tenant")
    )

    async with WorkerSession() as session:
        session.add(
            LlmCall(
                workspace_id=uuid4(),  # 不属于这个 run 的租户
                run_id=run_id,
                purpose="generate",
                attempt=1,
                step_no=1,
                call_seq=99,
                model_provider="stub",
                model_name="stub-1",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


async def test_finish_run_is_idempotent(workspace):
    ws, _ = workspace
    run_id = await runtime.start_run(
        runtime.RunSpec(workspace_id=ws, agent_name="t", goal="finish")
    )

    assert await runtime.finish_run(run_id, output={"ok": True}) is True
    # 第二次收尾必须落空，且不能把 completed_at 改掉
    assert await runtime.finish_run(run_id) is False

    row = await _run(run_id)
    assert row.status == AgentRunStatus.SUCCEEDED
    assert row.output == {"ok": True}
