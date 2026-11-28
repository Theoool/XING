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


class StrategyUpdate(BaseModel):
    """只改名字。

    配置不走这里：它是版本化内容，改配置必须新增版本（否则 v2 的审计追溯
    会指向一个和当时不一样的配置）。名字是标签，没有版本语义。
    """

    name: str = Field(min_length=1, max_length=100)


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


class StrategyVersionListOut(StrategyVersionOut):
    """历史版本条目。

    比 ``StrategyVersionOut`` 多一个 ``is_current``：前端要能一眼看出「哪一版
    正在生效」。让前端自己拿 ``current_version_number`` 去比也能做，但那样
    列表里每一行都要回查父行，等于把一致性判断推给 UI —— 服务端已经知道答案。
    """

    is_current: bool


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
