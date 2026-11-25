"""task_type → handler 的注册表。

handler 是**代码**，不是数据。工具声明做成数据库行会失去版本控制，
而这里的映射恰好需要跟着代码走：改了行为就要改文件、跑测试、发版。

两个基础设施 handler 与四个内容 Agent：

- ``echo``：不碰 LLM，用来单独验证「领取 → 执行 → 续租 → 完成 → 依赖推进」。
  先接 LLM 再接队列，出问题时分不清是模型还是并发。
- ``plan_content``：Orchestrator，一条 goal 展开成三个任务（``agents.planner``）。
- ``research_topic`` / ``write_content`` / ``review_content``：三个执行 Agent。

handler 拿到的是 :class:`~xing.worker.context.TaskContext`（纯值对象），
不是 ORM 对象，也没有 session —— 这样它既改不了 ``tasks.status``，
也不会把 30 秒的模型调用拖进一个长事务。
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from xing.worker.context import TaskContext

Handler = Callable[[TaskContext], Awaitable[dict[str, Any]]]

_REGISTRY: dict[str, Handler] = {}


def handler(task_type: str) -> Callable[[Handler], Handler]:
    """注册 handler。重复注册直接报错 —— 静默覆盖会让线上行为取决于 import 顺序。"""

    def register(fn: Handler) -> Handler:
        if task_type in _REGISTRY:
            raise RuntimeError(f"task_type 已注册: {task_type}")
        _REGISTRY[task_type] = fn
        return fn

    return register


def get_handler(task_type: str) -> Handler | None:
    return _REGISTRY.get(task_type)


def registered_types() -> list[str]:
    return sorted(_REGISTRY)


@handler("echo")
async def echo_handler(task: TaskContext) -> dict[str, Any]:
    """回声 handler：睡一会，把 input 原样返回。

    ``fail_with`` 用来在测试里制造失败，验证重试与终态两条路径。
    """
    await asyncio.sleep(float(task.input.get("sleep", 0.2)))

    if code := task.input.get("fail_with"):
        raise RuntimeError(str(code))

    return {"echo": task.input, "task_id": str(task.id)}


# 真实 Agent 的注册。放在文件末尾：它们会反过来导入上面的 ``handler``，
# 若在这里之前导入，装饰器还没定义。依赖方向仍是单向的 worker → agents。
#
# 注册即「这个 task_type 有执行者」。注册表里没有的类型，任务会一直留在
# READY 直到有人实现它 —— 这是可见的失败，比静默当成成功好。
from xing.agents import planner as _planner  # noqa: E402,F401
from xing.agents import researcher as _researcher  # noqa: E402,F401
from xing.agents import reviewer as _reviewer  # noqa: E402,F401
from xing.agents import writer as _writer  # noqa: E402,F401
