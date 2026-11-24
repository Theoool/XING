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


class TaskOut(BaseModel):
    id: UUID
    task_type: str
    status: str
    priority: int
    input: dict[str, Any]
    output: dict[str, Any] | None
    error: dict[str, Any] | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
