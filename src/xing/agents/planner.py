"""Orchestrator：把一条 goal 展开成三个任务。

它只做三件事：想清楚三步各做什么、把任务写进 ``tasks``、收尾。**不生成内容、
不写业务表、不调工具**。这条边界不是洁癖：一旦派发者也开始产出内容，
「谁对结果负责」就说不清了，审计时查到某段文字不知道是哪个角色写的。

## 为什么图的结构是代码常量

文档里写过「模型输出 DAG」，实现时改成固定三步。原因是通用 DAG 换来的只有
两类新失败（漏步、成环），零收益 —— 业务只有一条链路。模型真正该决定的是
**每一步做什么**（三段 brief），结构由代码保证。这不是简化，是把「什么该由
模型判断」划清楚。

## 成本怎么切

一条 goal 的预算按固定比例分给四个 run，加起来只用掉 60%：

    本步（plan） 20%   调研 15%   写作 35%   审核 10%

留 40% 是给重试的：子任务租约丢失重跑会真的多花一次钱，如果按 100% 分完，
第一个重试就会撞预算上限，表现为「任务莫名其妙 FAILED」。
这是**分配**而不是**共享账本** —— 共享账本要跨 run 事务，代价远大于收益，
Phase 4 的 workspace 配额才是它该出现的地方。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog

from xing.agents import runtime, schemas
from xing.core.errors import NonRetryableError
from xing.db.worker import WorkerSession
from xing.models.runtime import LlmPurpose
from xing.schemas.task import TaskCreate
from xing.services.task_service import create_task
from xing.worker.context import TaskContext
from xing.worker.handlers import handler

log = structlog.get_logger(__name__)

AGENT_NAME = "orchestrator"

#: 子任务类型。写成常量而不是字面量散落各处：改链路时只改这里。
RESEARCH = "research_topic"
WRITE = "write_content"
REVIEW = "review_content"

DEFAULT_GOAL_BUDGET = Decimal("2.0")

#: 预算分配比例。和恒为 0.8，留 0.2 给重试。
ALLOCATION = {
    "plan": Decimal("0.20"),
    RESEARCH: Decimal("0.15"),
    WRITE: Decimal("0.35"),
    REVIEW: Decimal("0.10"),
}

SYSTEM_PROMPT = """你是 XING 的内容生产调度 Agent。

用户给出一个目标，你要先定标题，再把它拆成三步，并为每一步写一段具体的执行说明：

0. content_title：这条内容最终发布的标题。要像人写的标题（不超过 30 字），
   不要出现「撰写」「写一条」「面向某某」这类指令口吻 —— 那是 brief 的语气。
1. research_brief：需要查清哪些事实、数据、背景。要具体到可检索。
2. write_brief：写什么、面向谁、用什么结构、多少篇幅。
3. review_focus：审核时最该盯住的三个点（事实准确性、合规、与目标的一致性）。

只输出 JSON，四个字段：
{"content_title": "...", "research_brief": "...",
 "write_brief": "...", "review_focus": "..."}
不要输出任何解释、不要 Markdown 代码块。
"""


@handler("plan_content")
async def plan_content(task: TaskContext) -> dict[str, Any]:
    goal = str(task.input.get("goal") or "").strip()
    if not goal:
        raise NonRetryableError("plan_content 需要 input.goal", code="MISSING_GOAL")

    budget = _decimal(task.input.get("budget"), DEFAULT_GOAL_BUDGET)
    run_id = await runtime.start_run(
        runtime.RunSpec(
            workspace_id=task.workspace_id,
            agent_name=AGENT_NAME,
            goal=goal,
            task_id=task.id,
            attempt=task.attempt,
            max_cost=_share(budget, "plan"),
        )
    )

    try:
        step_no = await runtime.begin_step(run_id)
        plan = await runtime.call_structured(
            run_id,
            purpose=LlmPurpose.PLAN,
            step_no=step_no,
            system=SYSTEM_PROMPT,
            prompt=f"目标：{goal}\n\n请输出三步计划。",
            schema=schemas.Plan,
            shape=schemas.PLAN_SHAPE,
            max_output_tokens=1200,
        )
        await runtime.record_plan(run_id, plan.model_dump())

        tasks = await _enqueue_chain(task, plan, budget, run_id)
        await runtime.finish_run(run_id, output=tasks)
    except BaseException as exc:
        await runtime.close_run_safely(run_id, exc)
        raise

    log.info(
        "agent.plan_dispatched",
        task_id=str(task.id),
        run_id=str(run_id),
        research=str(tasks["research_task_id"]),
    )
    return tasks


async def _enqueue_chain(
    task: TaskContext,
    plan: schemas.Plan,
    budget: Decimal,
    run_id: Any,
) -> dict[str, Any]:
    """按依赖顺序建三个任务。

    必须顺序建：``write`` 的 ``depends_on`` 需要 ``research`` 的 id。
    每一步都带 ``dedup_key``，所以这次执行中途崩溃、租约被回收、整个任务重跑，
    都不会长出第二套任务 —— 重复创建返回既有行。

    上游已成功的场景（``research`` 跑得比这里建 ``write`` 还快）不需要特判：
    ``AFTER INSERT`` 触发器看到依赖全部满足，会直接把新行推到 ``READY``。
    """
    common = _passthrough(task)

    research = await _create(
        task,
        TaskCreate(
            task_type=RESEARCH,
            input={**common, "brief": plan.research_brief, "parent_run_id": str(run_id)},
            assigned_agent="researcher",
            dedup_key=f"{task.id}:research",
        ),
        _share(budget, RESEARCH),
    )
    write = await _create(
        task,
        TaskCreate(
            task_type=WRITE,
            depends_on=[research],
            input={
                **common,
                "title": plan.content_title,
                "brief": plan.write_brief,
                "parent_run_id": str(run_id),
                "upstream_task_id": str(research),
            },
            assigned_agent="writer",
            dedup_key=f"{task.id}:write",
        ),
        _share(budget, WRITE),
    )
    review = await _create(
        task,
        TaskCreate(
            task_type=REVIEW,
            depends_on=[write],
            input={
                "focus": plan.review_focus,
                "parent_run_id": str(run_id),
                "upstream_task_id": str(write),
                # 审核要判断「事实是否被内容夸大」，就必须拿到写作当时看到的
                # 那份事实基线。它不是本任务的直接依赖（图仍是一条直链），
                # 所以用 id 显式传递，由 reviewer 回查业务表。
                "research_task_id": str(research),
            },
            assigned_agent="reviewer",
            dedup_key=f"{task.id}:review",
        ),
        _share(budget, REVIEW),
    )

    return {
        "research_task_id": str(research),
        "write_task_id": str(write),
        "review_task_id": str(review),
        "budget": {k: str(v) for k, v in _allocations(budget).items()},
    }


async def _create(task: TaskContext, payload: TaskCreate, max_cost: Decimal) -> UUID:
    """建一个子任务，并把分配到的预算塞进 ``input.max_cost``。

    预算放在 ``input`` 而不是新列：它是「这次执行的参数」，与 goal、brief 同类；
    加列会让 ``tasks`` 多一个只被一种任务类型使用的字段。
    """
    payload.input.setdefault("max_cost", str(max_cost))
    async with WorkerSession() as session:
        created = await create_task(session, workspace_id=task.workspace_id, payload=payload)
    return created.id


def _passthrough(task: TaskContext) -> dict[str, Any]:
    """从父任务透传的字段。只传「身份与素材」，不传预算 —— 预算是重新分的。"""
    keys = ("actor_id", "topic_id", "decision_id", "title", "content_type", "goal")
    return {key: task.input[key] for key in keys if key in task.input}


def _allocations(budget: Decimal) -> dict[str, Decimal]:
    return {name: _share(budget, name) for name in ALLOCATION}


def _share(budget: Decimal, name: str) -> Decimal:
    # 量化到 8 位：agent_runs.max_cost 是 numeric(18,8)，不经量化会带上来
    # 一串需要舍入的小数，预留上界向上取整后可能刚好越界。
    return (budget * ALLOCATION[name]).quantize(Decimal("0.00000001"))


def _decimal(raw: Any, default: Decimal) -> Decimal:
    return Decimal(str(raw)) if raw is not None else default


__all__ = ["AGENT_NAME", "plan_content"]
