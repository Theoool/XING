"""后台执行层。

``tasks`` 是唯一队列，worker 是 ``tasks.status`` 的唯一写入者。
Agent 只写 ``agent_runs`` 与业务数据 —— 这条边界是并发安全的基础，
详见 docs/AGENT_DESIGN.md §3.3。
"""

from xing.worker.handlers import get_handler, handler, registered_types

__all__ = ["get_handler", "handler", "registered_types"]
