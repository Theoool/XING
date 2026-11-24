"""内容资产与版本。

用户修改内容本身就是数据：所以改正文走「新增版本」，不提供 PATCH body。
这样 v1(AI) -> v2(人工改) -> v3(发布) 的差异将来可以反推成偏好与 Pattern。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

ContentType = Literal["VIDEO_SCRIPT", "ARTICLE", "POST", "AUDIO", "OTHER"]
ApprovalStatus = Literal["PENDING", "APPROVED", "REJECTED", "CANCELLED"]


class ContentCreate(BaseModel):
    title: str = Field(min_length=1)
    content_type: ContentType = "VIDEO_SCRIPT"
    topic_id: UUID | None = None
    decision_id: UUID | None = None
    # 给了 body 就同时落 v1，否则内容只有标题、没有版本
    body: str | None = None


class ContentVersionCreate(BaseModel):
    body: str = Field(min_length=1)
    title: str | None = None
    change_summary: str | None = None


class ContentVersionOut(BaseModel):
    """内容版本响应。

    ``metadata`` 这个字段名撞上了 SQLAlchemy Declarative 的保留属性
    （``Base.metadata``），所以 ORM 侧的属性叫 ``extra_metadata``、列名仍是
    ``metadata``。这里用 validation_alias 把两者接起来：
    从 ORM 对象读 ``extra_metadata``，序列化出去仍然是 ``metadata``，API 契约不变。
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    version_number: int
    title: str
    body: str
    metadata: dict[str, Any] = Field(validation_alias="extra_metadata")
    change_summary: str | None
    created_by: UUID | None
    created_at: datetime


class ContentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    topic_id: UUID | None
    decision_id: UUID | None
    title: str
    content_type: str
    status: str
    current_version_number: int
    lock_version: int
    created_at: datetime
    updated_at: datetime
    current_version: ContentVersionOut | None = None


class ContentReview(BaseModel):
    comment: str | None = None


class ApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    content_id: UUID
    stage: str
    status: str
    reviewer_id: UUID | None
    comment: str | None
    decided_at: datetime | None
    created_at: datetime
