"""选题。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

TopicStatus = Literal["CANDIDATE", "RECOMMENDED", "SELECTED", "REJECTED", "EXPIRED", "USED"]


class TopicOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    #: 产出这条候选的展开任务。前端据此跳到「取证过程」——
    #: 「这个切面凭什么成立」的答案全在那个任务的轨迹里。
    seed_task_id: UUID | None = None
    primary_event_id: UUID | None
    title: str
    description: str | None
    status: str
    trend_score: float | None
    relevance_score: float | None
    potential_score: float | None
    risk_score: float | None
    discovered_at: datetime
    selected_at: datetime | None
    #: 深入产出的完整 brief（stage=EXPANDED / DEEPENED）。带 evidence_refs 与
    #: search 元信息 —— 这是「这条选题值不值得写、依据是什么」对外的唯一出口。
    brief: dict[str, Any] | None = None


class TopicRecommendation(TopicOut):
    """推荐结果附带综合分。

    overall_score 是本次计算的产物，不落库：它依赖当时的权重，
    存下来会在权重调整后变成假历史。
    """

    overall_score: float


class TopicSelect(BaseModel):
    selected_option: str | None = Field(default=None, max_length=200)
    rationale: str | None = None


class TopicReject(BaseModel):
    reason: str | None = None
