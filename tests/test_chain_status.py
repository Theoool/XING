"""链路状态与选定闸门。

这两个东西解决同一类问题：**链路的真相只有一个，界面不能自己猜**。

1. 七环状态从 ``tasks`` / ``topics`` / ``contents`` 派生（``chain_service``），
   前端不再靠「拉任务列表再找」判断走到哪一步；
2. 舆论检测没结束就不能选定 —— 检测任务和选定是两个请求，中间有 8~15 秒窗口，
   ``check_opinion`` 只捞 ``CANDIDATE/RECOMMENDED``，已选定的题会被静默排除。
   闸门必须在服务层拒绝，不能只靠界面禁用按钮。

测试用手写任务构造状态，不跑模型：这里验的是状态推导与拒绝逻辑，
跑一次真实展开既慢又和它无关。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from xing.cache import heartbeat
from xing.core.config import settings
from xing.core.errors import ApiError
from xing.db.worker import WorkerSession
from xing.db.worker import dispose as dispose_worker
from xing.models.task import Task, TaskStatus
from xing.models.topic import Topic, TopicStatus
from xing.services import chain_service, task_service, topic_service


async def _no_workers() -> list[dict[str, object]]:
    """没有 worker 在跑。单独提出来给 monkeypatch 用，测试不该依赖真实 Redis 的内容。"""
    return []


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
                    "values (:email, 'chain-test') returning id"
                ),
                {"email": f"chain-{uuid4()}@x.test"},
            )
        ).scalar_one()
        ws_id = (
            await conn.execute(
                text(
                    "insert into workspaces (name, created_by) "
                    "values ('chain-test', :uid) returning id"
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
    ws: UUID,
    task_type: str,
    *,
    key: str,
    status: str = TaskStatus.SUCCEEDED,
    input: dict[str, Any] | None = None,
    output: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    depends_on: list[UUID] | None = None,
) -> UUID:
    async with WorkerSession() as session:
        task = Task(
            workspace_id=ws,
            task_type=task_type,
            input=input or {},
            dedup_key=key,
            status=status,
            output=output,
            error=error,
            depends_on=depends_on or [],
            completed_at=datetime.now(UTC) if status == TaskStatus.SUCCEEDED else None,
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task.id


async def _add_topic(
    ws: UUID,
    *,
    seed_id: UUID,
    brief: dict[str, Any] | None,
    status: str = TopicStatus.CANDIDATE,
) -> UUID:
    async with WorkerSession() as session:
        topic = Topic(
            workspace_id=ws,
            title="链路测试",
            status=status,
            seed_task_id=seed_id,
            brief=brief,
        )
        session.add(topic)
        await session.commit()
        await session.refresh(topic)
        return topic.id


async def _chain(ws: UUID, topic_id: UUID):
    admin = create_async_engine(settings.migration_database_url, poolclass=NullPool)
    try:
        async with AsyncSession(admin, expire_on_commit=False) as session:
            return await chain_service.get_status(session, workspace_id=ws, topic_id=topic_id)
    finally:
        await admin.dispose()


def _opinion(verdict: str) -> dict[str, Any]:
    return {"opinion": {"checked": True, "verdict": verdict}}


async def test_chain_waits_for_opinion_before_allowing_select(workspace):
    ws, _ = workspace
    seed = await _add_task(ws, "expand_topics", key=f"{uuid4()}:expand")
    await _add_task(ws, "check_opinion", key=f"{seed}:opinion", status=TaskStatus.RUNNING)
    topic_id = await _add_topic(ws, seed_id=seed, brief=None)

    status = await _chain(ws, topic_id)

    assert status.phase == "CHECKING"
    assert status.can_select is False
    assert status.select_blocked_reason == "OPINION_PENDING"
    assert [step.key for step in status.steps] == [
        "expand",
        "opinion",
        "score",
        "select",
        "deepen",
        "write",
        "review",
    ]


async def test_chain_allows_select_after_grounded_opinion(workspace):
    ws, _ = workspace
    seed = await _add_task(ws, "expand_topics", key=f"{uuid4()}:expand")
    await _add_task(ws, "check_opinion", key=f"{seed}:opinion")
    await _add_task(ws, "score_topics", key=f"{seed}:score")
    topic_id = await _add_topic(ws, seed_id=seed, brief=_opinion("GROUNDED"))

    status = await _chain(ws, topic_id)

    assert status.phase == "AWAITING_SELECTION"
    assert status.can_select is True
    assert status.select_blocked_reason is None


async def test_failed_write_is_marked_and_retryable(workspace):
    ws, user_id = workspace
    seed = await _add_task(ws, "expand_topics", key=f"{uuid4()}:expand")
    await _add_task(ws, "check_opinion", key=f"{seed}:opinion")
    await _add_task(ws, "score_topics", key=f"{seed}:score")
    topic_id = await _add_topic(
        ws, seed_id=seed, brief=_opinion("GROUNDED"), status=TopicStatus.SELECTED
    )
    deepen = await _add_task(ws, "deepen_topic", key=f"{topic_id}:deepen")
    await _add_task(
        ws,
        "write_content",
        key=f"{topic_id}:write",
        status=TaskStatus.FAILED,
        input={"topic_id": str(topic_id), "title": "链路测试", "brief": "标题：链路测试"},
        error={"code": "MODEL_TIMEOUT", "message": "模型超时"},
        depends_on=[deepen],
    )

    status = await _chain(ws, topic_id)
    assert status.phase == "FAILED"
    assert status.failed_step == "write"
    write_step = next(step for step in status.steps if step.key == "write")
    assert write_step.retryable is True
    assert write_step.error == {"code": "MODEL_TIMEOUT", "message": "模型超时"}

    admin = create_async_engine(settings.migration_database_url, poolclass=NullPool)
    try:
        async with AsyncSession(admin, expire_on_commit=False) as session:
            retried = await chain_service.retry_step(
                session,
                workspace_id=ws,
                topic_id=topic_id,
                step="write",
                user_id=user_id,
            )

        assert retried.phase == "WRITING"
        write_step = next(step for step in retried.steps if step.key == "write")
        assert write_step.status == "WAITING"
        assert write_step.retryable is False, "已经重排的任务不该还能再点一次重跑"

        async with WorkerSession() as session:
            rows = list(
                (
                    await session.execute(
                        select(Task).where(
                            Task.workspace_id == ws, Task.task_type == "write_content"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 2, "重跑应当新增一条任务，而不是改旧任务"
        assert sum(1 for row in rows if row.dedup_key == f"{topic_id}:write") == 1
        assert sum(1 for row in rows if row.dedup_key is None) == 1, "失败的那条应当让出键"

        # 新任务还没失败，不能再重跑
        async with AsyncSession(admin, expire_on_commit=False) as session:
            with pytest.raises(ApiError) as exc:
                await chain_service.retry_step(
                    session,
                    workspace_id=ws,
                    topic_id=topic_id,
                    step="write",
                    user_id=user_id,
                )
        assert exc.value.code == "STEP_NOT_RETRYABLE"
    finally:
        await admin.dispose()


async def test_chain_reports_review_verdict(workspace):
    ws, _ = workspace
    seed = await _add_task(ws, "expand_topics", key=f"{uuid4()}:expand")
    await _add_task(ws, "check_opinion", key=f"{seed}:opinion")
    await _add_task(ws, "score_topics", key=f"{seed}:score")
    topic_id = await _add_topic(
        ws, seed_id=seed, brief=_opinion("GROUNDED"), status=TopicStatus.SELECTED
    )
    deepen = await _add_task(ws, "deepen_topic", key=f"{topic_id}:deepen")
    write = await _add_task(ws, "write_content", key=f"{topic_id}:write", depends_on=[deepen])
    await _add_task(
        ws,
        "review_content",
        key=f"{topic_id}:review",
        depends_on=[write],
        output={"verdict": "PASS", "score": 88, "issues": ["第 3 段缺少出处"]},
    )

    status = await _chain(ws, topic_id)

    assert status.phase == "DONE"
    assert status.review is not None
    assert status.review.verdict == "PASS"
    assert status.review.score == 88.0
    assert status.review.issues == ["第 3 段缺少出处"]


async def test_select_rejects_topic_without_opinion(workspace):
    ws, user_id = workspace
    admin = create_async_engine(settings.migration_database_url, poolclass=NullPool)
    async with admin.begin() as conn:
        strategy_id = (
            await conn.execute(
                text(
                    "insert into strategies (workspace_id, name, status, current_version_number) "
                    "values (:ws, 'gate-test', 'ACTIVE', 1) returning id"
                ),
                {"ws": ws},
            )
        ).scalar_one()
        await conn.execute(
            text(
                "insert into strategy_versions "
                "(workspace_id, strategy_id, version_number, strategy_config, created_by) "
                "values (:ws, :sid, 1, '{}'::jsonb, :uid)"
            ),
            {"ws": ws, "sid": strategy_id, "uid": user_id},
        )
        topic_id = (
            await conn.execute(
                text(
                    "insert into topics (workspace_id, title, status) "
                    "values (:ws, '没有检测', 'CANDIDATE') returning id"
                ),
                {"ws": ws},
            )
        ).scalar_one()

    try:
        async with AsyncSession(admin, expire_on_commit=False) as session:
            with pytest.raises(ApiError) as exc:
                await topic_service.select_topic(
                    session,
                    workspace_id=ws,
                    topic_id=topic_id,
                    user_id=user_id,
                    selected_option=None,
                    rationale=None,
                )
        assert exc.value.code == "OPINION_PENDING"

        async with admin.begin() as conn:
            await conn.execute(
                text("update topics set brief = cast(:brief as jsonb) where id = :id"),
                {
                    "brief": '{"opinion": {"checked": true, "verdict": "UNGROUNDED"}}',
                    "id": topic_id,
                },
            )

        async with AsyncSession(admin, expire_on_commit=False) as session:
            with pytest.raises(ApiError) as exc:
                await topic_service.select_topic(
                    session,
                    workspace_id=ws,
                    topic_id=topic_id,
                    user_id=user_id,
                    selected_option=None,
                    rationale=None,
                )
        assert exc.value.code == "OPINION_UNGROUNDED"
    finally:
        await admin.dispose()


async def test_waiting_step_says_how_long_it_has_waited(workspace, monkeypatch):
    """READY 的任务不是「在跑」，是「没人领」。排队时长是唯一能区分两者的线索。"""
    ws, _ = workspace
    seed = await _add_task(ws, "expand_topics", key=f"{uuid4()}:expand")
    await _add_task(ws, "check_opinion", key=f"{seed}:opinion")
    await _add_task(ws, "score_topics", key=f"{seed}:score")
    topic_id = await _add_topic(
        ws, seed_id=seed, brief=_opinion("GROUNDED"), status=TopicStatus.SELECTED
    )
    await _add_task(ws, "deepen_topic", key=f"{topic_id}:deepen", status=TaskStatus.READY)

    monkeypatch.setattr(heartbeat, "live", _no_workers)
    status = await _chain(ws, topic_id)

    deepen = next(step for step in status.steps if step.key == "deepen")
    assert deepen.status == "WAITING"
    assert deepen.waiting_s is not None
    assert deepen.waiting_s < 10
    # 已选定的题一定在深入这一环：没有 worker 就是在排队等死。
    assert status.phase == "DEEPENING"
    assert status.workers_online == 0


async def test_chain_reports_worker_presence(workspace, monkeypatch):
    """有 worker 心跳和没有心跳，界面上必须是两句话。"""
    ws, _ = workspace
    seed = await _add_task(ws, "expand_topics", key=f"{uuid4()}:expand")
    topic_id = await _add_topic(ws, seed_id=seed, brief=_opinion("GROUNDED"))

    monkeypatch.setattr(heartbeat, "live", _no_workers)
    assert (await _chain(ws, topic_id)).workers_online == 0

    async def _one_worker():
        return [{"worker_id": "w-test", "agent": None, "beat": 0}]

    monkeypatch.setattr(heartbeat, "live", _one_worker)
    assert (await _chain(ws, topic_id)).workers_online == 1


async def test_queue_status_separates_queued_from_running(workspace, monkeypatch):
    """「排队」和「执行中」是两回事：前者没有任何进程在看它。"""
    ws, _ = workspace

    async with WorkerSession() as session:
        empty = await task_service.queue_status(session, workspace_id=ws)
    assert empty.queued == 0
    assert empty.running == 0
    assert empty.stalled is False
    assert empty.oldest_wait_s is None

    await _add_task(ws, "expand_topics", key=f"{uuid4()}:expand", status=TaskStatus.READY)
    await _add_task(ws, "check_opinion", key=f"{uuid4()}:opinion", status=TaskStatus.RUNNING)

    monkeypatch.setattr(heartbeat, "live", _no_workers)
    async with WorkerSession() as session:
        stalled = await task_service.queue_status(session, workspace_id=ws)
    assert (stalled.queued, stalled.running) == (1, 1)
    assert stalled.workers_online == 0
    assert stalled.stalled is True
    assert stalled.oldest_wait_s is not None

    async def _one_worker():
        return [{"worker_id": "w-test", "agent": None, "beat": 0}]

    monkeypatch.setattr(heartbeat, "live", _one_worker)
    async with WorkerSession() as session:
        healthy = await task_service.queue_status(session, workspace_id=ws)
    assert healthy.workers_online == 1
    assert healthy.stalled is False


async def test_unreadable_heartbeat_is_not_reported_as_no_worker(workspace, monkeypatch):
    """Redis 读不到 ≠ 没有 worker。前者不能下结论，否则一次抖动就被当成故障。"""
    ws, _ = workspace
    seed = await _add_task(ws, "expand_topics", key=f"{uuid4()}:expand")
    topic_id = await _add_topic(ws, seed_id=seed, brief=_opinion("GROUNDED"))
    await _add_task(ws, "deepen_topic", key=f"{topic_id}:deepen", status=TaskStatus.READY)

    async def _unreadable() -> None:
        return None

    monkeypatch.setattr(heartbeat, "live", _unreadable)

    assert (await _chain(ws, topic_id)).workers_online is None

    async with WorkerSession() as session:
        status = await task_service.queue_status(session, workspace_id=ws)
    assert status.workers_online is None
    assert status.stalled is False, "不知道有没有 worker 时不能断言队列卡住"
