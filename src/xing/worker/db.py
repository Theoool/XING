"""兼容转发。实现已移到 ``xing.db.worker``（Agent Runtime 与 worker 共用）。

保留此模块是为了不打断既有 ``from xing.worker.db import WorkerSession`` 引用；
新代码请直接从 ``xing.db.worker`` 导入。
"""

from xing.db.worker import WorkerSession, dispose, worker_engine

__all__ = ["WorkerSession", "dispose", "worker_engine"]
