"""僵尸 run 对账。

`agent_runs` 有一个 `close_run_safely` 收不了尾的场景：worker 被 SIGKILL、
容器被驱逐、或租约丢失后 `await` 立刻抛 `CancelledError`。这几种情况下 run 会
永远停在 `RUNNING`，而它带着一行 `reserved_cost`。

僵尸 run 的危害不是「状态不好看」，是**预算判断失真**：`reserved_cost` 一直被
占着，这个 run 永远无法再开下一步；后续若按 workspace 汇总成本，一个卡住的 run
会持续把额度算进去。Phase 1b 时它是偶发（单 Agent、短链路），Phase 2 多 Agent
串联后会变成常态 —— 一条链路四个 run，任一环节被杀就留一个。

判定用两个条件，任一成立即为僵尸：

1. **任务已经不在 RUNNING**。handler 一定先收 run、worker 再收任务（见
   `runner`），所以「任务已终态而 run 还 RUNNING」只可能是收尾被打断。
   这条件不需要任何超时猜测，是最准的那个。
2. **跑太久了**。覆盖 `task_id IS NULL` 的探索性 run（没有任务可参照），
   以及「任务被反复重试、每一代都留下一个 run」这类叠加场景。

收尾只做三件事：置 FAILED、清 `reserved_cost`、写 error。**绝不动
`total_cost`** —— 那是已经花掉的钱，抹掉等于篡改账本。
"""

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from xing.models.runtime import AgentRun, AgentRunStatus
from xing.models.task import Task, TaskStatus

#: 墙钟兜底。正常 run 是 1-3 步、几十秒；15 分钟足够覆盖最慢的链路，
#: 又远短于「一个人放着不管」的时间尺度。
STALE_RUN_AGE = timedelta(minutes=15)


async def reap_stale_runs(
    session: AsyncSession,
    *,
    max_age: timedelta = STALE_RUN_AGE,
) -> int:
    """把僵尸 run 收成 FAILED，返回处理条数。"""
    orphaned_task = AgentRun.task_id.is_not(None) & AgentRun.task_id.in_(
        # 任务已终态（SUCCEEDED / FAILED / CANCELLED），run 不可能还在跑。
        select(Task.id).where(Task.status != TaskStatus.RUNNING)
    )

    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(AgentRun)
            .where(
                AgentRun.status == AgentRunStatus.RUNNING,
                orphaned_task | (AgentRun.started_at < datetime.now(UTC) - max_age),
            )
            .values(
                status=AgentRunStatus.FAILED,
                completed_at=datetime.now(UTC),
                # 只释放预留。total_cost 是已经花出去的钱，抹掉等于改账。
                reserved_cost=0,
                error={
                    "code": "ORPHANED_RUN",
                    "message": "对账扫描判定为僵尸 run：任务已终态或运行超时",
                },
            )
            .execution_options(synchronize_session=False)
        ),
    )
    await session.commit()
    return result.rowcount or 0


__all__ = ["STALE_RUN_AGE", "reap_stale_runs"]
