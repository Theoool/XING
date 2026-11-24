"""外部事件：外部世界发生了什么。

注意这不等同于选题。一个选题通常由多条事件支撑，
关系在 topic_events 里。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

SourceType = Literal["NEWS", "SEARCH", "SOCIAL", "RSS", "API", "MANUAL", "OTHER"]


class EventIngest(BaseModel):
    source_type: SourceType
    external_id: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1)
    summary: str | None = None
    url: str | None = None
    source_published_at: datetime | None = None
    raw_payload: dict[str, Any] | None = None


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    source_type: str
    external_id: str
    title: str
    summary: str | None
    url: str | None
    source_published_at: datetime | None
    discovered_at: datetime


class EventIngestResult(BaseModel):
    """created=false 表示命中幂等键，更新了已有记录而不是新建。"""

    created: bool
    event: EventOut
