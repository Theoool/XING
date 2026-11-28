"""Reviewer：给内容做一次规则化的自检，然后把决定权交回给人。

它**不做通过/驳回的决定**。产出只有两样：

1. ``agent_runs.output`` 里的结构化判定（verdict / score / issues）—— 这是
   审计意义上的「Agent 当时怎么想」。
2. 一条 ``approvals(stage=CONTENT, status=PENDING)`` —— 这是给人处理的待办。

为什么不让 Agent 直接写 APPROVED：``guard_content_approval_status`` 触发器
本来就禁止无审批记录时置 APPROVED，而「谁来审批」是产品定义，不是模型判断。
Agent 越界写通过，出事时责任链就断了。

幂等靠 ``approvals_one_pending_per_stage``（部分唯一索引）：同一内容同一阶段
只允许一条 PENDING。重跑要么复用既有的那条，要么插一条新的 —— 两条路径都
由唯一索引裁决，不靠先查后插的窗口。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from xing.agents import runtime, schemas, upstream
from xing.agents.writer import length_guide
from xing.core.errors import NonRetryableError
from xing.db.worker import WorkerSession
from xing.models.content import (
    Approval,
    ApprovalStage,
    ApprovalStatus,
    Content,
    ContentStatus,
    ContentVersion,
)
from xing.models.event import ExternalEvent
from xing.models.runtime import LlmPurpose
from xing.models.task import Task
from xing.models.topic import TopicEvent
from xing.worker.context import TaskContext
from xing.worker.handlers import handler

log = structlog.get_logger(__name__)

AGENT_NAME = "reviewer"

#: 送审正文的截断长度。审核要的是判断力，不是把全文塞进窗口；
#: 超长内容的分段审核是 Phase 3 的事，现在截断并显式告知模型。
MAX_BODY_CHARS = 6000

SYSTEM_PROMPT = """你是 XING 的内容审核 Agent。

对照审核要点检查内容，从三个维度打分：事实准确性、合规风险、与目标的一致性。
- verdict：PASS 表示可以进入人工终审，FAIL 表示存在必须修改的问题。
- score：0-100，60 以下一律 FAIL。
- issues：逐条列出具体问题，没有就给空数组。不要评价文风偏好。

事实准确性按下面的口径判，不要"看起来合理"就放行：
1. 正文里出现的具体数据、报告名、机构结论，必须在「事实基线」里有对应表述；
   基线里没有，就算 issue（编造），不论它听起来多可信。
2. 语气被加强也算失真：基线说「预测/尚未发布」，正文写成「指出/已证明」，
   记一条 issue 并扣分 —— 结论强度是事实的一部分。
3. 基线本身为空时，正文仍可以写，但凡出现出处与数字一律记 issue。
4. 基线带 [编号] 时，正文里标的编号必须是基线里存在的；引用的意思要与原文一致 ——
   把「预测」写成「已证实」、把个案写成普遍现象，都算失真。

只输出 JSON：{"verdict": "PASS", "score": 85, "issues": []}
不要输出任何解释、不要 Markdown 代码块。
"""


@dataclass(frozen=True, slots=True)
class Snapshot:
    """送审需要的全部字段。一次性取完，不在 handler 里持有 ORM 对象。

    持有 ORM 对象然后在 session 之外读关系属性，在 async 下会抛
    ``MissingGreenlet``；这正是把「取数」和「判断」分开的理由。
    """

    id: UUID
    title: str
    content_type: str
    status: str
    body: str
    #: 写作时写进 ``content_versions.metadata`` 的篇幅档。审核按**当时**的
    #: 标准判，而不是当前代码里的标准 —— 改了档位不该让旧稿追溯性变短。
    meta: dict[str, Any]


@handler("review_content")
async def review_content(task: TaskContext) -> dict[str, Any]:
    content_id = await _resolve_content_id(task)
    snapshot = await _load_content(task, content_id)

    run_id = await runtime.start_run(
        runtime.RunSpec(
            workspace_id=task.workspace_id,
            agent_name=AGENT_NAME,
            goal=f"审核内容：{snapshot.title}"[:2000],
            task_id=task.id,
            attempt=task.attempt,
            parent_run_id=_uuid_or_none(task.input.get("parent_run_id")),
            max_cost=_decimal(task.input.get("max_cost"), "0.2"),
        )
    )

    try:
        step_no = await runtime.begin_step(run_id)
        verdict = await runtime.call_structured(
            run_id,
            purpose=LlmPurpose.SELF_CHECK,
            step_no=step_no,
            system=SYSTEM_PROMPT,
            prompt=_build_prompt(
                snapshot,
                str(task.input.get("focus") or ""),
                await _load_facts(task),
            ),
            schema=schemas.ReviewVerdict,
            shape=schemas.REVIEW_SHAPE,
            max_output_tokens=1000,
        )
        # 机检在模型之后：字数是硬事实，模型判 PASS 也改不了它。
        verdict = _length_gate(verdict, snapshot)
        approval_id = await _request_approval(task, content_id=snapshot.id)
        await runtime.finish_run(
            run_id,
            output={
                "content_id": str(snapshot.id),
                "approval_id": str(approval_id),
                "verdict": verdict.verdict,
                "score": verdict.score,
                "issues": verdict.issues,
                "chars": len("".join(snapshot.body.split())),
            },
        )
    except BaseException as exc:
        await runtime.close_run_safely(run_id, exc)
        raise

    log.info(
        "agent.review_done",
        task_id=str(task.id),
        content_id=str(snapshot.id),
        verdict=verdict.verdict,
        score=verdict.score,
        chars=len("".join(snapshot.body.split())),
    )
    return {
        "content_id": str(snapshot.id),
        "approval_id": str(approval_id),
        "verdict": verdict.verdict,
        "score": verdict.score,
        # issues 也必须进 task.output：run.output 只有内部审计能读，
        # 而接口对外暴露的是 task.output。少了它，界面只能看到 FAIL
        # 却不知道要改什么，人工返工就无从下手。
        "issues": verdict.issues,
        "chars": len("".join(snapshot.body.split())),
        "run_id": str(run_id),
    }


async def _resolve_content_id(task: TaskContext) -> UUID:
    """内容 id 优先取上游写作任务的输出。

    上游是权威来源：审核的是「这次生产出来的那一篇」，而不是某个调用方
    在 ``input`` 里塞的 id。入参只作为没有上游时的显式指定。
    """
    if task.depends_on:
        found = upstream.pick(await upstream.load(task), "write_content").get("content_id")
        if found:
            return UUID(str(found))

    raw = task.input.get("content_id")
    if not raw:
        raise NonRetryableError("找不到要审核的内容 id", code="MISSING_CONTENT")
    return UUID(str(raw))


async def _load_content(task: TaskContext, content_id: UUID) -> Snapshot:
    async with WorkerSession() as session:
        row = (
            await session.execute(
                select(Content).where(
                    Content.id == content_id, Content.workspace_id == task.workspace_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NonRetryableError(
                f"内容不属于该 workspace: {content_id}", code="CROSS_TENANT_REFERENCE"
            )

        version = (
            await session.execute(
                select(ContentVersion.body, ContentVersion.extra_metadata).where(
                    ContentVersion.content_id == content_id,
                    ContentVersion.version_number == row.current_version_number,
                )
            )
        ).first()
        if version is None:
            raise NonRetryableError(f"内容没有正文版本: {content_id}", code="MISSING_BODY")
        body, meta = version

        return Snapshot(
            id=row.id,
            title=row.title,
            content_type=row.content_type,
            meta=dict(meta or {}),
            status=row.status,
            body=body,
        )


def _build_prompt(snapshot: Snapshot, focus: str, facts: str) -> str:
    truncated = snapshot.body[:MAX_BODY_CHARS]
    note = "" if len(snapshot.body) <= MAX_BODY_CHARS else "\n（正文过长，以下为前 6000 字）"
    return (
        f"标题：{snapshot.title}\n"
        f"类型：{snapshot.content_type}\n"
        f"审核要点：\n{focus or '（未提供，按通用标准审核）'}\n\n"
        f"事实基线（写作时可用的事实，正文不得超出它）：\n{facts or '（本次没有调研材料）'}\n\n"
        f"正文{note}：\n{truncated}"
    )


def _length_gate(verdict: schemas.ReviewVerdict, snapshot: Snapshot) -> schemas.ReviewVerdict:
    """篇幅机检：字数是算得出来的，不该交给 judge 估。

    两档处理 —— 低于下限记一条 issue 扣分；低于下限的 80% 直接判 FAIL。
    「少 30 字」是收尾仓促，补一句就能交；「少 400 字」是没写完，得重写，
    两者返工方式不同，不该给同一个结论。

    下限优先取写作时写进 metadata 的值：改了档位不该让旧稿追溯性变短。
    """
    guide = length_guide(snapshot.content_type)
    raw_floor = snapshot.meta.get("length_floor")
    floor = raw_floor if isinstance(raw_floor, int) and raw_floor > 0 else guide.floor
    chars = len("".join(snapshot.body.split()))
    if chars >= floor:
        return verdict

    hard = chars < int(floor * 0.8)
    issue = f"篇幅不足：正文 {chars} 字，{guide.label}下限 {floor} 字" + (
        "，明显没写完" if hard else "，收尾偏仓促"
    )
    score = 0 if hard else max(0, verdict.score - 15)
    return verdict.model_copy(
        update={
            "verdict": "FAIL" if hard or score < 60 else verdict.verdict,
            "score": score,
            "issues": [*verdict.issues, issue],
        }
    )


async def _load_facts(task: TaskContext) -> str:
    """取写作时用过的事实基线。

    两条路，取决于内容从哪条链来的：

    - 选题链：``topic_id`` → ``topic_events`` 的证据（带角色与原文）。
      这是更硬的一种 —— 每条基线都能指回一条检索材料。
    - 目标链：``research_task_id`` → 该任务 ``output.event_id`` →
      ``external_events.summary``。只有一条摘要，所以它对「正文有没有夸大」
      的判断力天然更弱。

    不直接从 ``tasks.output`` 读 summary：``output`` 只放指针是一条硬约定，
    正文与证据一律回业务表取，否则审核看到的是可能已被覆盖的旧快照，
    而不是这次真正引用的事实。
    """
    topic_id = task.input.get("topic_id")
    if topic_id:
        return await _load_evidence_facts(task, UUID(str(topic_id)))

    raw_task_id = task.input.get("research_task_id")
    if not raw_task_id:
        return ""

    async with WorkerSession() as session:
        output = await session.scalar(
            select(Task.output).where(
                Task.id == UUID(str(raw_task_id)),
                Task.workspace_id == task.workspace_id,
            )
        )
        event_id = (output or {}).get("event_id")
        if not event_id:
            return ""
        summary = await session.scalar(
            select(ExternalEvent.summary).where(
                ExternalEvent.id == UUID(str(event_id)),
                ExternalEvent.workspace_id == task.workspace_id,
            )
        )
    return str(summary or "")


async def _load_evidence_facts(task: TaskContext, topic_id: UUID) -> str:
    """把 ``topic_events`` 渲染成审核读的事实基线。编号与写作时一致。"""
    async with WorkerSession() as session:
        rows = (
            await session.execute(
                select(
                    TopicEvent.role,
                    TopicEvent.quote,
                    ExternalEvent.title,
                    ExternalEvent.summary,
                    ExternalEvent.url,
                    ExternalEvent.raw_payload,
                )
                .join(ExternalEvent, ExternalEvent.id == TopicEvent.event_id)
                .where(
                    TopicEvent.topic_id == topic_id,
                    ExternalEvent.workspace_id == task.workspace_id,
                )
                .order_by(TopicEvent.weight.desc(), TopicEvent.created_at, TopicEvent.event_id)
                .limit(8)
            )
        ).all()

    lines: list[str] = []
    for role, quote, title, summary, url, raw in rows:
        body = str(quote or summary or "").strip()
        if not body:
            continue
        label = str((raw or {}).get("source_label") or "").strip()
        lines.append(
            f"[{len(lines) + 1}] {role}《{str(title)[:80]}》\n"
            f'    原文："{body[:400]}"\n'
            f"    来源：{url or label or '来源未注明'}"
        )
    return "\n".join(lines)


async def _request_approval(task: TaskContext, *, content_id: UUID) -> UUID:
    """建一条待审记录，并把内容推进到 REVIEW。

    顺序是「先 approvals 再 contents」：万一只完成了前一半，内容是 DRAFT 但已有
    待审记录，重跑会复用那条记录并补上状态推进；反过来则会留下一条 REVIEW
    状态却没有待办的内容，没人看得见。
    """
    async with WorkerSession() as session:
        approval_id = await session.scalar(
            pg_insert(Approval)
            .values(
                workspace_id=task.workspace_id,
                content_id=content_id,
                stage=ApprovalStage.CONTENT,
                status=ApprovalStatus.PENDING,
            )
            .on_conflict_do_nothing(
                index_elements=["content_id", "stage"],
                index_where=text("status = 'PENDING'"),
            )
            .returning(Approval.id)
        )
        if approval_id is None:
            approval_id = await session.scalar(
                select(Approval.id).where(
                    Approval.content_id == content_id,
                    Approval.stage == ApprovalStage.CONTENT,
                    Approval.status == ApprovalStatus.PENDING,
                )
            )
        if approval_id is None:
            # 插入被冲突挡下却查不到 PENDING：说明并发下别人正好把它批了。
            # 这不是错误，但本次执行也不该谎报成功。
            raise NonRetryableError(
                f"待审记录状态已变化，未建立新的待审: {content_id}", code="APPROVAL_RACED"
            )

        await session.execute(
            update(Content)
            .where(Content.id == content_id, Content.status == ContentStatus.DRAFT)
            .values(status=ContentStatus.REVIEW)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        return approval_id


def _decimal(raw: Any, default: str) -> Decimal:
    return Decimal(str(raw)) if raw is not None else Decimal(default)


def _uuid_or_none(value: Any) -> UUID | None:
    return UUID(str(value)) if value else None


__all__ = ["AGENT_NAME", "review_content"]
