"""任务：后端异步执行的统一入口。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class TaskCreate(BaseModel):
    task_type: str = Field(min_length=1, max_length=50)
    priority: int = Field(default=0, ge=-100, le=100)
    input: dict[str, Any] = Field(default_factory=dict)
    #: 前置任务。全部 SUCCEEDED 后由触发器推进到 READY，不需要轮询。
    depends_on: list[UUID] = Field(default_factory=list)
    assigned_agent: str | None = Field(default=None, max_length=50)
    #: 幂等键。同键重复提交返回既有任务，不会产生第二条生产线。
    dedup_key: str | None = Field(default=None, min_length=1, max_length=200)


class TaskOut(BaseModel):
    id: UUID
    task_type: str
    status: str
    priority: int
    input: dict[str, Any]
    output: dict[str, Any] | None
    error: dict[str, Any] | None
    depends_on: list[UUID]
    dedup_key: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
