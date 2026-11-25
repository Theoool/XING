"""worker 骨架的并发、状态与 fencing 测试。

六件事，每件都是「推理不可靠、只能实测」的：

1. 领取后状态与租约正确，代次递增
2. 多 worker 并发领取时，同一个任务不会被执行两次
3. 失败按重试额度分流到 RETRYING / FAILED
4. 租约过期能被回收，且重试额度耗尽时直接 FAILED
5. **旧代次的写入被 fence 掉**（A 卡住 → B 接管 → A 恢复，A 的结果必须作废）
6. 创建时的初始状态由依赖决定（无依赖 READY / 依赖未满足 PENDING）

用 ``xing_worker`` 连接（BYPASSRLS），seed 数据走迁移用连接。
worker engine 是模块级全局，必须在用例结束时释放，否则下一个用例
会在已关闭的事件循环上复用连接（与 conftest 里主 engine 同一个坑）。
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from xing.core.config import settings
from xing.models.task import Task, TaskStatus
from xing.worker import handlers, queue, runner
from xing.worker.db import WorkerSession
from xing.worker.db import dispose as dispose_worker


@pytest.fixture(autouse=True)
async def _dispose_worker_engine():
    yield
    await dispose_worker()


@pytest.fixture
async def workspace_id():
    admin = create_async_engine(settings.migration_database_url, poolclass=NullPool)
    async with admin.begin() as conn:
        user_id = (
            await conn.execute(
                text(
                    "insert into users (email, display_name) "
                    "values (:email, 'worker-test') returning id"
                ),
                {"email": f"worker-{uuid4()}@x.test"},
            )
        ).scalar_one()
        ws_id = (
            await conn.execute(
                text(
                    "insert into workspaces (name, created_by) "
                    "values ('worker-test', :uid) returning id"
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

    yield ws_id

    async with admin.begin() as conn:
        await conn.execute(
            text("delete from workspaces where id = :id"), {"id": ws_id}
        )
    await admin.dispose()


async def _add_task(
    ws: UUID,
    *,
    status: str | None = None,
    task_type: str = "echo",
    payload: dict | None = None,
    **extra: Any,
) -> UUID:
    """插一个任务。

    ``status`` 默认不传：初始状态由触发器 ``trg_tasks_promote_on_insert``
    按 ``depends_on`` 决定 —— 无依赖 READY，有未满足依赖 PENDING。
    测试若硬写 READY，就等于绕过了被测行为，所以默认交给数据库。
    """
    async with WorkerSession() as session:
        kwargs: dict[str, Any] = {}
        if status is not None:
            kwargs["status"] = status
        task = Task(
            workspace_id=ws,
            task_type=task_type,
            input=payload or {},
            **kwargs,
            **extra,
        )
        session.add(task)
        await session.commit()
        return task.id


async def _get(task_id: UUID) -> Task | None:
    async with WorkerSession() as session:
        return await session.get(Task, task_id)


async def test_claim_marks_running_with_lease(workspace_id):
    task_id = await _add_task(workspace_id)

    async with WorkerSession() as session:
        claimed = await queue.claim_one(session, worker_id="w1")
        await session.commit()
        assert claimed is not None
        assert claimed.id == task_id
        assert claimed.status == TaskStatus.RUNNING
        assert claimed.locked_by == "w1"
        assert claimed.lease_expires_at is not None
        assert claimed.started_at is not None
        # 领取即换代：0 → 1
        assert claimed.lease_generation == 1

    # 已不是 READY，不能再被领
    async with WorkerSession() as session:
        assert await queue.claim_one(session, worker_id="w2") is None


async def test_concurrent_workers_execute_each_task_once(workspace_id, monkeypatch):
    calls: Counter[UUID] = Counter()

    async def counting_handler(task) -> dict:  # noqa: ANN001
        calls[task.id] += 1
        await asyncio.sleep(0.01)
        return {"task_id": str(task.id)}

    monkeypatch.setitem(handlers._REGISTRY, "echo", counting_handler)

    total = 20
    ids = {await _add_task(workspace_id) for _ in range(total)}

    async def consume(name: str) -> int:
        handled = 0
        while (await runner.claim_and_run(worker_id=name)) is not None:
            handled += 1
        return handled

    # 12 个 worker 抢 20 个任务：必然有 worker 空手而归
    await asyncio.gather(*[consume(f"w{i}") for i in range(12)])

    assert set(calls) == ids, "有任务没被执行"
    assert max(calls.values()) == 1, f"存在重复执行: {calls.most_common(3)}"

    for task_id in ids:
        task = await _get(task_id)
        assert task is not None
        assert task.status == TaskStatus.SUCCEEDED
        assert task.locked_by is None
        assert task.lease_expires_at is None
        assert task.completed_at is not None


async def test_failure_moves_to_retrying_then_failed(workspace_id):
    # 额度 1：第一次失败进 RETRYING，第二次耗尽进 FAILED
    task_id = await _add_task(
        workspace_id, payload={"fail_with": "boom"}, max_retries=1
    )

    first = await runner.claim_and_run(worker_id="w1")
    assert first is not None and first.status == "RETRYING"
    task = await _get(task_id)
    assert task is not None
    assert task.status == TaskStatus.RETRYING
    assert task.retry_count == 1
    assert task.error is not None and task.error["code"] == "RuntimeError"
    assert task.locked_by is None

    # 让退避立即到期，避免测试等 10 秒
    async with WorkerSession() as session:
        await session.execute(
            text("update tasks set available_at = now() where id = :id"), {"id": task_id}
        )
        await session.commit()
        assert await queue.promote_retrying(session) >= 1

    second = await runner.claim_and_run(worker_id="w1")
    assert second is not None and second.status == "FAILED"
    task = await _get(task_id)
    assert task is not None
    assert task.status == TaskStatus.FAILED
    assert task.completed_at is not None
    assert task.locked_by is None


async def test_no_handler_fails_fast(workspace_id):
    task_id = await _add_task(workspace_id, task_type="not-registered")

    outcome = await runner.claim_and_run(worker_id="w1")
    assert outcome is not None and outcome.status == "FAILED"

    task = await _get(task_id)
    assert task is not None
    assert task.status == TaskStatus.FAILED
    assert task.error is not None and task.error["code"] == "NO_HANDLER"


async def test_reclaim_expired_lease(workspace_id):
    expired = datetime.now(UTC) - timedelta(seconds=5)
    task_id = await _add_task(
        workspace_id,
        status=TaskStatus.RUNNING,
        locked_by="dead-worker",
        locked_at=expired,
        lease_expires_at=expired,
        max_retries=3,
    )

    async with WorkerSession() as session:
        to_retry, to_failed = await queue.reclaim_expired(session)
    assert (to_retry, to_failed) == (1, 0)

    task = await _get(task_id)
    assert task is not None
    assert task.status == TaskStatus.RETRYING
    assert task.retry_count == 1
    assert task.locked_by is None
    assert task.error is not None and task.error["code"] == "LEASE_EXPIRED"


async def test_reclaim_without_retry_budget_fails(workspace_id):
    expired = datetime.now(UTC) - timedelta(seconds=5)
    task_id = await _add_task(
        workspace_id,
        status=TaskStatus.RUNNING,
        locked_by="dead-worker",
        locked_at=expired,
        lease_expires_at=expired,
        retry_count=2,
        max_retries=2,
    )

    async with WorkerSession() as session:
        to_retry, to_failed = await queue.reclaim_expired(session)
    assert (to_retry, to_failed) == (0, 1)

    task = await _get(task_id)
    assert task is not None
    assert task.status == TaskStatus.FAILED


async def test_illegal_transition_is_rejected_by_database(workspace_id):
    """服务层没有 if，非法迁移必须由数据库拒绝。这里验证那道防线确实在。"""
    task_id = await _add_task(workspace_id, status=TaskStatus.SUCCEEDED)

    async with WorkerSession() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        task.status = TaskStatus.RUNNING
        with pytest.raises(DBAPIError) as exc:
            await session.commit()
        await session.rollback()

    assert "非法迁移" in str(exc.value)


async def test_stale_generation_write_is_fenced_out(workspace_id):
    """A 卡住 → 租约过期 → B 接管 → A 恢复。A 的结果必须被丢弃。

    这是 fencing 的唯一存在理由。没有它，A 的 UPDATE 只带主键，
    会把 B 正在执行的任务标成 SUCCEEDED 并清空 B 的租约；
    而 ``guard_task_status_transition`` 看到 RUNNING→SUCCEEDED 是合法边，
    **会放行** —— 它校验边，不校验谁有权走。
    """
    task_id = await _add_task(workspace_id)

    # 1. A 领取 → generation = 1
    async with WorkerSession() as session:
        claimed = await queue.claim_one(session, worker_id="worker-A")
        await session.commit()
        assert claimed is not None and claimed.id == task_id
        gen_a = claimed.lease_generation
    assert gen_a == 1

    # 2. A 卡住，租约过期被回收 → generation = 2，状态 RETRYING
    async with WorkerSession() as session:
        await session.execute(
            text("update tasks set lease_expires_at = now() - interval '1 second' where id = :id"),
            {"id": task_id},
        )
        await session.commit()
    async with WorkerSession() as session:
        to_retry, _ = await queue.reclaim_expired(session)
    assert to_retry >= 1

    # 3. 退避到期，B 接管 → generation = 3
    async with WorkerSession() as session:
        await session.execute(
            text("update tasks set available_at = now() where id = :id"), {"id": task_id}
        )
        await session.commit()
        await queue.promote_retrying(session)
    async with WorkerSession() as session:
        claimed = await queue.claim_one(session, worker_id="worker-B")
        await session.commit()
        assert claimed is not None and claimed.id == task_id
        gen_b = claimed.lease_generation
    assert gen_b == 3

    # 4. A 恢复，用旧代次写结果：必须落空
    async with WorkerSession() as session:
        ok = await queue.finalize(
            session,
            task_id,
            worker_id="worker-A",
            generation=gen_a,
            status=TaskStatus.SUCCEEDED,
            completed_at=datetime.now(UTC),
            output={"from": "stale-A"},
        )
    assert ok is False, "旧代次的写入竟然成功了 —— fencing 失效"

    task = await _get(task_id)
    assert task is not None
    assert task.status == TaskStatus.RUNNING, "B 正在执行的任务被 A 改掉了"
    assert task.locked_by == "worker-B", "B 的租约被 A 清空了"
    assert task.output is None

    # 5. B 用当前代次写：必须成功
    async with WorkerSession() as session:
        ok = await queue.finalize(
            session,
            task_id,
            worker_id="worker-B",
            generation=gen_b,
            status=TaskStatus.SUCCEEDED,
            completed_at=datetime.now(UTC),
            output={"from": "B"},
        )
    assert ok is True

    task = await _get(task_id)
    assert task is not None
    assert task.status == TaskStatus.SUCCEEDED
    assert task.locked_by is None
    assert task.output == {"from": "B"}


async def test_created_task_is_ready_when_no_dependency(workspace_id):
    """无依赖的任务创建即 READY。停在 PENDING 的话，没有任何事件会推进它。"""
    task_id = await _add_task(workspace_id)
    task = await _get(task_id)
    assert task is not None
    assert task.status == TaskStatus.READY


async def test_created_task_waits_when_dependency_unmet(workspace_id):
    """上游未完成时，下游必须留在 PENDING —— 不能抢跑。"""
    upstream = await _add_task(workspace_id)
    downstream = await _add_task(workspace_id, depends_on=[upstream])

    task = await _get(downstream)
    assert task is not None
    assert task.status == TaskStatus.PENDING

    # 上游跑完，下游自动转 READY
    async with WorkerSession() as session:
        claimed = await queue.claim_one(session, worker_id="w1")
        await session.commit()
        assert claimed is not None and claimed.id == upstream
    await runner.run_task(upstream, worker_id="w1")

    task = await _get(downstream)
    assert task is not None
    assert task.status == TaskStatus.READY


async def test_created_task_is_ready_when_dependency_already_done(workspace_id):
    """上游早已 SUCCEEDED，之后才创建下游 —— 上游不会再成功一次，
    所以必须由 AFTER INSERT 触发器推进。这是原实现漏掉的核心路径。"""
    upstream = await _add_task(workspace_id)

    async with WorkerSession() as session:
        claimed = await queue.claim_one(session, worker_id="w1")
        await session.commit()
        assert claimed is not None and claimed.id == upstream
    await runner.run_task(upstream, worker_id="w1")

    done = await _get(upstream)
    assert done is not None and done.status == TaskStatus.SUCCEEDED

    downstream = await _add_task(workspace_id, depends_on=[upstream])
    task = await _get(downstream)
    assert task is not None
    assert task.status == TaskStatus.READY
