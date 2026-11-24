"""通用响应结构。"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class Page(BaseModel, Generic[T]):  # noqa: UP046 - Pydantic 对 PEP 695 泛型的支持仍有限
    """键集分页结果。没有 total —— 大表上它是全表扫，需要总数请单独出统计接口。"""

    items: list[T]
    next_cursor: str | None = None
