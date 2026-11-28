"""链路状态的派生：把散在 ``tasks`` / ``topics`` / ``contents`` 里的事实合成一份七环状态。

为什么是派生而不是新建一张 ``chains`` 表：链路的真实状态已经全部落库了 ——
任务状态在 ``tasks``、人的决策在 ``topics.status``、成稿在 ``contents``。
再存一份「链路表」就有了第二个真相，两者不一致时没人知道该信谁。
这里的成本只是几次按 ``dedup_key`` 的定点查询，全部命中唯一索引。

``dedup_key`` 是唯一的寻址方式：``{seed}:opinion`` / ``{seed}:score`` /
``{topic}:deepen`` / ``{topic}:write`` / ``{topic}:review``。用 ``topic_id`` 去扫
``tasks.input`` 也能查，但那要遍历 JSONB，且任务多了以后前端还得自己挑最新的。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xing.cache import heartbeat
from xing.core.errors import ApiError
from xing.models.content import Content
from xing.models.task import Task, TaskStatus
from xing.models.topic import Topic, TopicStatus
from xing.schemas.chain import ChainReview, ChainStatus, ChainStep, RetryStep
from xing.schemas.task import TaskCreate
from xing.services.task_service import create_task

#: 七环的展示名。key 与 docs/TOPIC_PIPELINE.md §1 对齐。
STEP_LABELS: dict[str, str] = {
    "expand": "展开",
    "opinion": "舆论检测",
    "score": "评分",
    "select": "选定",
    "deepen": "深入",
    "write": "成稿",
    "review": "审核",
}

_ORDER = ("expand", "opinion", "score", "select", "deepen", "write", "review")

#: tasks.status -> 单步状态。task 是执行事实，step 是给人看的粒度，两者不是一一对应。
_TASK_TO_STEP: dict[str, str] = {
    TaskStatus.PENDING: "PENDING",
    TaskStatus.READY: "WAITING",
    TaskStatus.RUNNING: "RUNNING",
    TaskStatus.RETRYING: "RETRYING",
    TaskStatus.SUCCEEDED: "DONE",
    TaskStatus.FAILED: "FAILED",
    TaskStatus.CANCELLED: "CANCELLED",
}

_ACTIVE = ("WAITING", "QUEUED", "RUNNING", "RETRYING")
_RETRYABLE = ("deepen", "write", "review")
_STEP_TASK_TYPE = {
    "deepen": "deepen_topic",
    "write": "write_content",
    "review": "review_content",
}
_STEP_AGENT = {"deepen": "deepener", "write": "writer", "review": "reviewer"}

#: 选定前置：舆论检测必须是这两个结论之一。UNGROUNDED 的题已经被置 REJECTED。
_ALLOWED_VERDICTS = ("GROUNDED", "WEAK")

#: 「上一步已完成、下一步还没有任务」这个空窗最长能容忍多久（秒）。
#:
#: 排产分两段：``select_topic`` 先 commit 选定、再 ``create_task`` 排深入；
#: ``write`` / ``review`` 由 worker 在收尾时接力排下一条。两段中间都有
#: 「前一步已完成、后一步还没落库」的瞬间，轮询恰好撞上是正常的。
#: 超过这个窗口仍然没有任务，就不是空窗，而是排产失败 —— 必须说出来。
_ENQUEUE_GRACE_S = 90


async def _get_topic(session: AsyncSession, workspace_id: UUID, topic_id: UUID) -> Topic:
    topic = (
        await session.execute(
            select(Topic)
            .where(Topic.id == topic_id, Topic.workspace_id == workspace_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if topic is None:
        raise ApiError(404, "TOPIC_NOT_FOUND", "选题不存在")
    return topic


def _step(
    key: str,
    status: str,
    *,
    task: Task | None = None,
    retryable: bool = False,
    error: dict | None = None,
) -> ChainStep:
    waiting_s = None
    if status == "WAITING" and task is not None and task.started_at is None:
        waiting_s = _age_seconds(task.created_at)
    if error is None and task is not None and task.error:
        error = dict(task.error)
    return ChainStep(
        key=key,
        label=STEP_LABELS[key],
        status=status,  # type: ignore[arg-type]
        task_id=task.id if task is not None else None,
        # 合成出来的失败（没有任务可读 error）也要带原因，否则界面上只剩一个
        # 「失败」而说不出失败在哪一步。
        error=error,
        retryable=retryable,
        waiting_s=waiting_s,
    )


def _task_step(key: str, task: Task | None, *, retryable: bool = False) -> ChainStep:
    """有任务就按任务状态，没任务就是「还没排产」—— 没排产的东西谈不上重跑。"""
    if task is None:
        return _step(key, "PENDING")
    status = _TASK_TO_STEP.get(task.status, "PENDING")
    return _step(key, status, task=task, retryable=retryable and status in ("FAILED", "CANCELLED"))


def _age_seconds(value: datetime | None) -> int | None:
    """距离某个时间点过了多少秒。迁移前的老行可能是 naive，按 UTC 解释。"""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return max(0, int((datetime.now(UTC) - value).total_seconds()))


def _enqueue_lost(since: datetime | None) -> bool:
    """上一步早就完成了，下一步却仍没有任务 —— 这是排产失败，不是还在跑。

    时间戳缺失时也判为丢失：宁可给一个重跑按钮（重跑是幂等的，``dedup_key``
    唯一），也不要让人对着「正在执行」等到前端超时。
    """
    age = _age_seconds(since)
    return age is None or age > _ENQUEUE_GRACE_S


def _opinion_verdict(topic: Topic) -> str | None:
    opinion = (topic.brief or {}).get("opinion") or {}
    if not isinstance(opinion, dict) or not opinion.get("checked"):
        return None
    verdict = opinion.get("verdict")
    return str(verdict) if verdict else None


def _review_from(task: Task | None) -> ChainReview | None:
    output = (task.output or {}) if task is not None else {}
    verdict = output.get("verdict")
    if verdict not in ("PASS", "FAIL"):
        return None
    raw_score = output.get("score")
    score = float(raw_score) if isinstance(raw_score, int | float) else 0.0
    issues = output.get("issues")
    return ChainReview(
        verdict=verdict,
        score=score,
        issues=[str(item) for item in issues] if isinstance(issues, list) else [],
    )


async def get_status(session: AsyncSession, *, workspace_id: UUID, topic_id: UUID) -> ChainStatus:
    topic = await _get_topic(session, workspace_id, topic_id)
    seed = topic.seed_task_id

    keys = [f"{topic_id}:{key}" for key in _RETRYABLE]
    if seed is not None:
        keys += [f"{seed}:opinion", f"{seed}:score"]
    rows = (
        (
            await session.execute(
                select(Task).where(
                    Task.workspace_id == workspace_id,
                    Task.dedup_key.in_(keys),
                )
            )
        )
        .scalars()
        .all()
    )
    by_key = {row.dedup_key: row for row in rows if row.dedup_key}

    expand: Task | None = None
    if seed is not None:
        expand = (
            await session.execute(
                select(Task).where(Task.id == seed, Task.workspace_id == workspace_id)
            )
        ).scalar_one_or_none()

    opinion_task = by_key.get(f"{seed}:opinion") if seed is not None else None
    score_task = by_key.get(f"{seed}:score") if seed is not None else None
    deepen_task = by_key.get(f"{topic_id}:deepen")
    write_task = by_key.get(f"{topic_id}:write")
    review_task = by_key.get(f"{topic_id}:review")

    verdict = _opinion_verdict(topic)
    selected = topic.status in (TopicStatus.SELECTED, TopicStatus.USED)

    if topic.status == TopicStatus.REJECTED:
        select_status = "SKIPPED"
    elif selected:
        select_status = "DONE"
    else:
        select_status = "WAITING"

    # 选定已生效、深入任务却不存在：要么是 ``select_topic`` 在 commit 之后
    # ``create_task`` 失败（它自己会提交，没法并进同一事务），要么有人直接改了
    # ``topics.status``。以前这里一律当「正在深入」——于是界面永远显示执行中，
    # 而没有任何任务会来推进它，只能等前端 15 分钟超时。
    # 空窗期内的缺失仍按「还没排产」处理；过了空窗就标成排产失败并允许重跑
    # （``retry_step`` 支持「还没有任务」的 deepen，键唯一，不会长出第二条）。
    deepen_step = _task_step("deepen", deepen_task, retryable=True)
    if selected and deepen_task is None:
        if _enqueue_lost(topic.selected_at):
            deepen_step = _step(
                "deepen",
                "FAILED",
                retryable=True,
                error={
                    "code": "DEEPEN_NOT_ENQUEUED",
                    "message": "任务没有排上（排产失败），点重跑补排",
                },
            )
        else:
            deepen_step = _step("deepen", "PENDING")

    steps: list[ChainStep] = [
        _task_step("expand", expand),
        _task_step("opinion", opinion_task),
        _task_step("score", score_task),
        _step("select", select_status),
        deepen_step,
        _task_step("write", write_task, retryable=True),
        _task_step("review", review_task, retryable=True),
    ]

    # 同样的缺失也会发生在下游：``write`` / ``review`` 由 deepener 在深入收尾时
    # 一次性排好，正常情况下不存在「前一步 DONE、后一步没有任务」的稳定状态。
    # 真出现了（任务被手工删掉、或旧数据）就不能再装作在跑：相位会停在上一环，
    # 界面上同样是永远「正在执行」。这里标成失败，但不给重跑按钮 —— 这两步的
    # 重跑需要按原输入重建（brief / upstream_task_id），没有任务就没有输入可复制。
    status_by_key = {step.key: step.status for step in steps}
    for key, upstream, since in (
        ("write", "deepen", deepen_task.completed_at if deepen_task is not None else None),
        ("review", "write", write_task.completed_at if write_task is not None else None),
    ):
        index = _ORDER.index(key)
        if status_by_key.get(upstream) != "DONE" or steps[index].task_id is not None:
            continue
        if _enqueue_lost(since):
            steps[index] = _step(
                key,
                "FAILED",
                error={
                    "code": f"{key.upper()}_NOT_ENQUEUED",
                    "message": "上一步已完成，但这一步的任务不存在（排产失败），无法自动补排",
                },
            )

    states = {step.key: step.status for step in steps}
    review = _review_from(review_task)
    content_id = (
        await session.execute(
            select(Content.id)
            .where(Content.workspace_id == workspace_id, Content.topic_id == topic_id)
            .order_by(Content.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    failed_step = next((key for key in _ORDER if states.get(key) in ("FAILED", "CANCELLED")), None)

    if topic.status == TopicStatus.REJECTED:
        phase = "REJECTED"
    elif states["review"] == "DONE":
        phase = "DONE"
    elif failed_step is not None:
        phase = "FAILED"
    elif states["review"] in _ACTIVE or (
        states["write"] == "DONE" and states["review"] == "PENDING"
    ):
        phase = "REVIEWING"
    elif states["write"] in _ACTIVE or (
        states["write"] == "PENDING" and states["deepen"] == "DONE"
    ):
        # deepen 已 DONE 而 write 还没落库：这是写入队前的空窗，不是「还在深入」。
        phase = "WRITING"
    elif states["deepen"] in _ACTIVE or (states["deepen"] == "PENDING" and selected):
        # ``selected`` 只兜住「选定已提交、深入任务刚排上」的那几秒；
        # 超过空窗仍然缺失的，上面已经标成 FAILED，会走 failed_step 分支，
        # 不会再停在这一环假装在跑。
        phase = "DEEPENING"
    elif states["score"] in _ACTIVE:
        phase = "SCORING"
    elif states["opinion"] in _ACTIVE:
        phase = "CHECKING"
    elif states["expand"] in _ACTIVE:
        phase = "EXPANDING"
    else:
        phase = "AWAITING_SELECTION"

    can_select, blocked = _selectability(topic, states, verdict)
    workers = await heartbeat.live()

    return ChainStatus(
        topic_id=topic_id,
        seed_task_id=seed,
        phase=phase,  # type: ignore[arg-type]
        steps=steps,
        content_id=content_id,
        review=review,
        can_select=can_select,
        select_blocked_reason=blocked,
        failed_step=failed_step,
        workers_online=None if workers is None else len(workers),
    )


def _selectability(
    topic: Topic, states: Mapping[str, str], verdict: str | None
) -> tuple[bool, str | None]:
    """选定的前置条件。

    舆论检测没跑完就不能选 —— 这是闸门，不是提示。前端可以据此禁用按钮，
    但真正的保证在后面 ``select_topic`` 的 409 上：界面永远不是防线。
    """
    if topic.status not in TopicStatus.SELECTABLE:
        return False, "TOPIC_NOT_SELECTABLE"
    if verdict is None:
        if states.get("opinion") in ("FAILED", "CANCELLED"):
            return False, "OPINION_FAILED"
        return False, "OPINION_PENDING"
    if verdict not in _ALLOWED_VERDICTS:
        return False, "OPINION_UNGROUNDED"
    return True, None


async def retry_step(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    topic_id: UUID,
    step: RetryStep,
    user_id: UUID,
) -> ChainStatus:
    """重跑一个失败环节。

    ``dedup_key`` 是「同一环节只能有一条活任务」的保证，所以重跑不能简单再插一条
    —— 那会得到两条同名任务，链路上出现两个真相。做法是把失败任务的键让出来
    （置空），新任务接管同一个键：历史里仍能看到失败的那次，活任务永远只有一条。

    输入从失败任务复制，而不是在这里重建 brief —— 写作者当时读到的就是那份文本，
    重跑应当重试同一次写作，而不是重新拼一个可能已经变化的 prompt。
    """
    topic = (
        await session.execute(
            select(Topic).where(Topic.id == topic_id, Topic.workspace_id == workspace_id)
        )
    ).scalar_one_or_none()
    if topic is None:
        raise ApiError(404, "TOPIC_NOT_FOUND", "选题不存在")

    key = f"{topic_id}:{step}"
    task = (
        await session.execute(
            select(Task).where(Task.workspace_id == workspace_id, Task.dedup_key == key)
        )
    ).scalar_one_or_none()

    if step != "deepen" and topic.status not in (TopicStatus.SELECTED, TopicStatus.USED):
        raise ApiError(409, "TOPIC_NOT_SELECTED", "选题还没选定，不能重跑后续环节")
    if task is not None and task.status not in (TaskStatus.FAILED, TaskStatus.CANCELLED):
        raise ApiError(409, "STEP_NOT_RETRYABLE", f"{STEP_LABELS[step]}还没失败，不需要重跑")
    if task is None and step != "deepen":
        raise ApiError(409, "STEP_NOT_FOUND", f"还没有{STEP_LABELS[step]}任务，无法按原输入重跑")

    payload = dict(task.input or {}) if task is not None else {}
    payload.setdefault("topic_id", str(topic_id))
    payload.setdefault("actor_id", str(user_id))

    depends_on: list[UUID] = []
    if step in ("write", "review"):
        upstream_step = "deepen" if step == "write" else "write"
        upstream_key = f"{topic_id}:{upstream_step}"
        upstream = (
            await session.execute(
                select(Task.id).where(
                    Task.workspace_id == workspace_id,
                    Task.dedup_key == upstream_key,
                )
            )
        ).scalar_one_or_none()
        if upstream is None:
            raise ApiError(409, "STEP_NOT_FOUND", "上游环节还没有成功，不能重跑这一步")
        depends_on.append(upstream)
        if step == "review":
            # 上游可能也被重跑过：重新指向当前那一条，别审一份已经被替换的稿子。
            payload["upstream_task_id"] = str(upstream)

    if task is not None:
        task.dedup_key = None
        await session.flush()

    await create_task(
        session,
        workspace_id=workspace_id,
        payload=TaskCreate(
            task_type=_STEP_TASK_TYPE[step],
            input=payload,
            depends_on=depends_on,
            assigned_agent=_STEP_AGENT[step],
            dedup_key=key,
        ),
    )
    return await get_status(session, workspace_id=workspace_id, topic_id=topic_id)
