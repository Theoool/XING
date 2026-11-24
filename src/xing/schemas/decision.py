"""决策。

decisions 不接收 strategy_version_id —— 那个值由服务端按 active 战略的
current_version_number 解析。否则客户端可以永远传 v1，
三个月后审计会发现所有决策都声称基于初版战略。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

DecisionType = Literal[
    "TOPIC_SELECTION",
    "ANGLE_SELECTION",
    "CONTENT_DIRECTION",
    "PUBLISHING",
    "STRATEGY",
]
TraceType = Literal["EVIDENCE", "SCORE", "RULE", "ALTERNATIVE", "CONSTRAINT"]


class DecisionTraceIn(BaseModel):
    trace_type: TraceType
    source_type: str | None = None
    source_id: UUID | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class DecisionCreate(BaseModel):
    decision_type: DecisionType
    topic_id: UUID | None = None
    selected_option: str | None = Field(default=None, max_length=200)
    rationale: str | None = None
    score: float | None = Field(default=None, ge=0, le=1)
    traces: list[DecisionTraceIn] = Field(default_factory=list)


class DecisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    strategy_version_id: UUID
    topic_id: UUID | None
    decision_type: str
    status: str
    selected_option: str | None
    rationale: str | None
    score: float | None
    decided_by: UUID | None
    decided_at: datetime | None
    created_at: datetime
    traces: list[dict[str, Any]] = Field(default_factory=list)


class DecisionReview(BaseModel):
    comment: str | None = None
