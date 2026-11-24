"""内容战略。

战略是版本化对象：主表只有名字、状态和「当前版本号」，
真正的配置存在 strategy_versions 里。所以没有「PUT 整条战略」这种接口。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StrategyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    strategy_config: dict[str, Any] = Field(default_factory=dict)
    change_summary: str | None = None


class StrategyVersionCreate(BaseModel):
    strategy_config: dict[str, Any]
    change_summary: str | None = None


class StrategyVersionOut(BaseModel):
    # 允许直接从 StrategyVersion ORM 对象校验
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    version_number: int
    strategy_config: dict[str, Any]
    change_summary: str | None
    created_by: UUID
    created_at: datetime


class StrategyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    workspace_id: UUID
    name: str
    status: Literal["DRAFT", "ACTIVE", "PAUSED", "ARCHIVED"]
    current_version_number: int
    lock_version: int
    created_at: datetime
    updated_at: datetime
    current_version: StrategyVersionOut
