"""external_events 的 ORM 映射。

两个特殊列：

``search_vector`` 是 ``GENERATED ALWAYS AS ... STORED`` 的生成列，必须用
``Computed`` 声明。不声明的话 ``alembic check`` 会认为模型少了一列，
每次都催你加一个已经存在的列。

``raw_payload`` 是原始载荷，只进不出（API 不返回它），保留原文便于回放与排查。
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from xing.db.base import Base

# 生成列的表达式必须与 DDL 逐字对齐，否则 autogenerate 会认为需要重建列
_SEARCH_VECTOR_SQL = "to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(summary, ''))"


class ExternalEvent(Base):
    __tablename__ = "external_events"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("uuidv7()"))
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("workspaces.id", ondelete="CASCADE", name="external_events_workspace_id_fkey"),
        nullable=False,
    )
    # NEWS / SEARCH / SOCIAL / RSS / API / MANUAL / OTHER，见 SourceType
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    source_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed(_SEARCH_VECTOR_SQL, persisted=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        # ingest 的幂等键。ON CONFLICT 就是打在这个约束上。
        UniqueConstraint(
            "workspace_id", "source_type", "external_id", name="external_events_source_unique"
        ),
        Index(
            "idx_external_events_workspace_discovered", "workspace_id", text("discovered_at DESC")
        ),
        Index(
            "idx_external_events_source_published",
            "workspace_id",
            "source_type",
            text("source_published_at DESC"),
        ),
        # 全文检索（search_vector）与模糊匹配（title 的 trigram）各一个 GIN
        Index(
            "idx_external_events_search",
            "search_vector",
            postgresql_using="gin",
        ),
        Index(
            "idx_external_events_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
        {"comment": "External world events collected from news/search/social/etc."},
    )

    def __repr__(self) -> str:
        return f"<ExternalEvent {self.source_type}:{self.external_id}>"


class SourceType:
    NEWS = "NEWS"
    SEARCH = "SEARCH"
    SOCIAL = "SOCIAL"
    RSS = "RSS"
    API = "API"
    MANUAL = "MANUAL"
    OTHER = "OTHER"
