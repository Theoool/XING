"""第一个真实 Agent：把一条任务变成一篇待审内容。

刻意做小。这里不含规划、不含多步推理、不含工具调用 —— Phase 1b 的目标不是
「智能」，而是让 **一次真实执行** 在数据库里留下完整痕迹：
``agent_runs`` 一行、``llm_calls`` 一行、``contents`` + ``content_versions``
各一行，金额与 token 能对得上。

三件必须有的事：

1. **幂等**。任务因租约丢失被接管后会重跑，重跑不能产出第二篇内容。
   判据是 ``contents.source_task_id``（唯一索引），不是内存状态。
2. **失败留痕**。预算耗尽、模型空响应都必须让 run 进 FAILED 并带上 code，
   而不是抛个字符串了事。
3. **越权检查**。worker 是 BYPASSRLS，``topic_id``/``decision_id`` 指向别的
   租户不会被数据库挡住，所以这里显式校验归属。

上游调研结论通过 ``agents.upstream`` 读取，只取摘要文本，不取整条外部事件 ——
引用关系靠 ``content_versions.metadata.event_id`` 记，正文里不塞 URL。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select

from xing.agents import runtime, upstream
from xing.core.errors import NonRetryableError
from xing.db.worker import WorkerSession
from xing.models.content import Content, ContentStatus, ContentType, ContentVersion
from xing.models.core import WorkspaceMember, WorkspaceRole
from xing.models.decision import Decision
from xing.models.runtime import LlmPurpose
from xing.models.topic import Topic
from xing.worker.context import TaskContext
from xing.worker.handlers import handler

log = structlog.get_logger(__name__)

AGENT_NAME = "writer"

SYSTEM_PROMPT = """你是 XING 的内容撰写 Agent。
只输出正文，不要标题、不要 Markdown 代码块、不要解释你在做什么。
语言与用户给定的 brief 一致。不编造 brief 与调研材料中没有的事实、数据或引用。
"""


@handler("write_content")
async def write_content(task: TaskContext) -> dict[str, Any]:
    brief = str(task.input.get("brief") or "").strip()
    if not brief:
        raise NonRetryableError("write_content 需要 input.brief", code="MISSING_BRIEF")

    title = str(task.input.get("title") or _fallback_title(brief)).strip()
    content_type = str(task.input.get("content_type") or ContentType.VIDEO_SCRIPT)
    if content_type not in _CONTENT_TYPES:
        raise NonRetryableError(f"未知 content_type: {content_type}", code="BAD_CONTENT_TYPE")

    # 重跑幂等：这个任务已经产出过内容就直接复用，不再调模型、不再计费。
    reused = await _find_existing(task)
    if reused is not None:
        content_id, version_number = reused
        log.info(
            "agent.write_reused",
            task_id=str(task.id),
            content_id=str(content_id),
            version=version_number,
        )
        return {
            "content_id": str(content_id),
            "version_number": version_number,
            "reused": True,
        }

    actor_id = await _resolve_actor(task)
    await _assert_owned(task)
    research = await _read_research(task)

    run_id = await runtime.start_run(
        runtime.RunSpec(
            workspace_id=task.workspace_id,
            agent_name=AGENT_NAME,
            goal=brief,
            task_id=task.id,
            attempt=task.attempt,
            parent_run_id=_uuid_or_none(task.input.get("parent_run_id")),
            max_cost=_max_cost(task),
        )
    )

    try:
        step_no = await runtime.begin_step(run_id)
        response = await runtime.call_model(
            run_id,
            purpose=LlmPurpose.GENERATE,
            step_no=step_no,
            system=SYSTEM_PROMPT,
            prompt=_build_prompt(
                title=title, content_type=content_type, brief=brief, research=research
            ),
            max_output_tokens=_max_output_tokens(task),
        )

        body = response.text.strip()
        if not body:
            raise NonRetryableError("模型返回空正文", code="EMPTY_COMPLETION")

        content_id, version_number = await _write_draft(
            task=task,
            actor_id=actor_id,
            title=title,
            content_type=content_type,
            body=body,
            run_id=run_id,
            research=research,
        )
        await runtime.finish_run(
            run_id,
            output={"content_id": str(content_id), "version_number": version_number},
        )
    except BaseException as exc:
        # 预算耗尽 / 取消 / 任何异常：run 必须收尾，否则永远 RUNNING。
        await runtime.close_run_safely(run_id, exc)
        raise

    return {
        "content_id": str(content_id),
        "version_number": version_number,
        "run_id": str(run_id),
    }


_CONTENT_TYPES = {
    ContentType.VIDEO_SCRIPT,
    ContentType.ARTICLE,
    ContentType.POST,
    ContentType.AUDIO,
    ContentType.OTHER,
}


def _fallback_title(brief: str) -> str:
    """没有显式标题时的兜底。

    取第一个句子而不是硬截 30 字：``brief[:30]`` 会把
    「面向CTO…，撰写60秒短视频脚本。结构采」这种半句话当标题写进内容库。
    真正的标题来自 Planner 的 ``content_title``；这里只服务手工建的任务。
    """
    head = brief.strip().split("。", 1)[0].split("\n", 1)[0]
    return (head or brief).strip()[:40]


def _build_prompt(
    *, title: str, content_type: str, brief: str, research: dict[str, Any] | None
) -> str:
    material = ""
    if research:
        summary = str(research.get("summary") or "").strip()
        keywords = "、".join(str(k) for k in research.get("keywords") or [])
        if summary:
            material = f"\n调研结论（可引用的事实）：\n{summary}\n"
            if keywords:
                material += f"关键词：{keywords}\n"
    return (
        f"标题：{title}\n"
        f"类型：{content_type}\n"
        f"要求：\n{brief}\n"
        f"{material}\n"
        "请直接输出正文。"
    )


async def _read_research(task: TaskContext) -> dict[str, Any] | None:
    """有上游调研就用上，没有也不失败 —— 写作不强制依赖调研（Phase 1 的单任务路径）。"""
    if not task.depends_on:
        return None
    found = await upstream.load(task)
    for item in found:
        if item.task_type == "research_topic":
            return item.output
    return None


def _max_cost(task: TaskContext) -> Decimal:
    raw = task.input.get("max_cost")
    return Decimal(str(raw)) if raw is not None else Decimal("2.0")


def _max_output_tokens(task: TaskContext) -> int:
    raw = task.input.get("max_output_tokens")
    return int(raw) if raw is not None else 1600


async def _find_existing(task: TaskContext) -> tuple[UUID, int] | None:
    """按 ``contents.source_task_id`` 找这个任务已经产出的内容。

    这条查询走的是一列带部分唯一索引的 uuid，而不是 JSONB 里的文本匹配 ——
    后者在任务量上去之后是全表顺序扫描，而重跑恰好是最需要快的路径。
    """
    async with WorkerSession() as session:
        row = (
            await session.execute(
                select(Content.id, Content.current_version_number)
                .where(
                    Content.workspace_id == task.workspace_id,
                    Content.source_task_id == task.id,
                )
                .limit(1)
            )
        ).first()
    return (row[0], int(row[1])) if row is not None else None


async def _resolve_actor(task: TaskContext) -> UUID:
    """内容的 ``created_by``。Agent 不是人，必须挂到一个真实用户上。

    优先级：``input.actor_id``（触发这次生产的人）> workspace OWNER。
    两者都没有就直接失败 —— 用系统账号糊弄会让审批链失去责任人。
    """
    raw = task.input.get("actor_id")
    if raw:
        return UUID(str(raw))

    async with WorkerSession() as session:
        owner = await session.scalar(
            select(WorkspaceMember.user_id)
            .where(
                WorkspaceMember.workspace_id == task.workspace_id,
                WorkspaceMember.role == WorkspaceRole.OWNER,
            )
            .limit(1)
        )
    if owner is None:
        raise NonRetryableError(
            "workspace 没有 OWNER，无法确定内容责任人", code="NO_ACTOR"
        )
    return owner


async def _assert_owned(task: TaskContext) -> None:
    """topic / decision 必须属于当前 workspace。

    数据库只保证「这个 id 存在」，不保证「属于同一租户」；而 worker 是
    BYPASSRLS，策略也挡不住。跨租户引用会静默泄漏内容之间的关联关系。
    """
    topic_id = task.input.get("topic_id")
    decision_id = task.input.get("decision_id")

    async with WorkerSession() as session:
        if topic_id is not None:
            ok = await session.scalar(
                select(Topic.id).where(
                    Topic.id == UUID(str(topic_id)),
                    Topic.workspace_id == task.workspace_id,
                )
            )
            if ok is None:
                raise NonRetryableError(
                    f"topic 不属于该 workspace: {topic_id}", code="CROSS_TENANT_REFERENCE"
                )
        if decision_id is not None:
            ok = await session.scalar(
                select(Decision.id).where(
                    Decision.id == UUID(str(decision_id)),
                    Decision.workspace_id == task.workspace_id,
                )
            )
            if ok is None:
                raise NonRetryableError(
                    f"decision 不属于该 workspace: {decision_id}",
                    code="CROSS_TENANT_REFERENCE",
                )


async def _write_draft(
    *,
    task: TaskContext,
    actor_id: UUID,
    title: str,
    content_type: str,
    body: str,
    run_id: UUID,
    research: dict[str, Any] | None,
) -> tuple[UUID, int]:
    """内容与 v1 在同一个事务里落库，不留「有内容没正文」的中间态。"""
    async with WorkerSession() as session:
        content = Content(
            workspace_id=task.workspace_id,
            topic_id=_uuid_or_none(task.input.get("topic_id")),
            decision_id=_uuid_or_none(task.input.get("decision_id")),
            source_task_id=task.id,
            title=title,
            content_type=content_type,
            status=ContentStatus.DRAFT,
            current_version_number=1,
            lock_version=0,
            created_by=actor_id,
        )
        session.add(content)
        await session.flush()

        session.add(
            ContentVersion(
                workspace_id=task.workspace_id,
                content_id=content.id,
                version_number=1,
                title=title,
                body=body,
                extra_metadata={
                    "task_id": str(task.id),
                    "run_id": str(run_id),
                    "agent": AGENT_NAME,
                    "generated_at": datetime.now(UTC).isoformat(),
                    # 证据指针：审核要能回答「这段结论来自哪条外部信息」。
                    "event_id": (research or {}).get("event_id"),
                    "parent_run_id": task.input.get("parent_run_id"),
                },
                change_summary=f"{AGENT_NAME} 生成",
                created_by=actor_id,
            )
        )
        content_id = content.id
        await session.commit()

    log.info(
        "agent.draft_written",
        task_id=str(task.id),
        content_id=str(content_id),
        chars=len(body),
    )
    return content_id, 1


def _uuid_or_none(value: Any) -> UUID | None:
    return UUID(str(value)) if value else None


__all__ = ["AGENT_NAME", "write_content"]
