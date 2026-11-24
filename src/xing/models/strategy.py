"""strategies / strategy_versions 的 ORM 映射。

这里唯一不寻常的是 ``Strategy.current_version``：它不是普通外键关系，而是
「``strategy_versions.version_number`` 等于父行的 ``current_version_number``」这种
按列值匹配的关系。SQLAlchemy 支持用 ``primaryjoin`` + ``foreign()`` 表达，但必须
``viewonly=True`` —— 否则 ORM 会以为它能反向维护这个指针，flush 时写出意料之外的
UPDATE。指针只由 ``strategy_service.add_version`` 显式改。

``innerjoin=True`` 是刻意的：老 SQL 用 inner join，当前版本缺失时那条战略在 API 上
「不存在」。改成 LEFT JOIN 会让 ``StrategyOut.current_version`` 拿到 None，
而它 schema 里是必填，结果从 404 变成 500 —— 那是行为退化。
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    and_,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, foreign, mapped_column, relationship

from xing.db.base import Base


class Strategy(Base):
    __tablename__ = "strategies"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("uuidv7()"))
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("workspaces.id", ondelete="CASCADE", name="strategies_workspace_id_fkey"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)

    # DRAFT / ACTIVE / PAUSED / ARCHIVED，取值域见 StrategyStatus
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'DRAFT'"))

    # 指向 strategy_versions.version_number 的「指针」，没有外键守护，只由服务层维护
    current_version_number: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    # 变更计数。注意：它目前**不是**乐观锁 —— 没有任何 WHERE lock_version = :expected。
    # 类型是 bigint，不是 integer：库侧的 DDL 就是 bigint，写成 Integer 会让
    # alembic check 报 modify_type。
    lock_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    # 注解用 Any：跨类字符串注解要靠 SQLAlchemy 延迟解析，模块加载顺序一变就炸，
    # 而关系目标已由 relationship("StrategyVersion") 给出，注解带不来额外信息。
    current_version: Mapped[Any] = relationship(
        "StrategyVersion",
        primaryjoin=lambda: and_(
            Strategy.id == foreign(StrategyVersion.strategy_id),
            StrategyVersion.version_number == Strategy.current_version_number,
        ),
        viewonly=True,
        uselist=False,
        innerjoin=True,
        lazy="joined",
    )

    __table_args__ = (
        Index("idx_strategies_workspace_status", "workspace_id", "status"),
        # 一个工作区最多一条 ACTIVE。activate 必须先降级其他再激活目标，
        # 否则中间态会撞这个部分唯一索引（23505）。
        Index(
            "strategies_one_active_per_workspace",
            "workspace_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
    )

    def __repr__(self) -> str:
        return f"<Strategy {self.name} {self.status} v{self.current_version_number}>"


class StrategyVersion(Base):
    """不可变快照。只有 INSERT，没有 UPDATE —— 这是审计能成立的前提。"""

    __tablename__ = "strategy_versions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("uuidv7()"))
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("workspaces.id", ondelete="CASCADE", name="strategy_versions_workspace_id_fkey"),
        nullable=False,
    )
    strategy_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("strategies.id", ondelete="CASCADE", name="strategy_versions_strategy_id_fkey"),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    strategy_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    change_summary: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="RESTRICT", name="strategy_versions_created_by_fkey"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("strategy_id", "version_number", name="strategy_versions_unique"),
        Index("idx_strategy_versions_workspace_strategy", "workspace_id", "strategy_id"),
        Index(
            "idx_strategy_versions_created_at",
            "strategy_id",
            text("created_at DESC"),
        ),
    )

    def __repr__(self) -> str:
        return f"<StrategyVersion {self.strategy_id} v{self.version_number}>"


class StrategyStatus:
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    ARCHIVED = "ARCHIVED"
