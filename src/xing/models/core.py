"""users / workspaces / workspace_members 的 ORM 映射。

这三张表是租户链条的根：``workspace_members`` 决定「谁能看到哪个租户」，
而它自己不能开 RLS（否则要自引用判断成员身份），所以成员过滤只能靠显式 join ——
`workspace_service` 里每一条查询都必须带上 ``WorkspaceMember.user_id == user_id``。

命名一律迁就库侧（``workspace_members_unique``、``users_email_lower_unique`` 等），
不用 ``Base.metadata`` 的 naming_convention 重新推导：那样会让 ``alembic check``
报一堆 drop/create 噪音，掩盖真正的模型漂移。
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from xing.db.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("uuidv7()"))
    email: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("idx_users_created_at", "created_at"),
        # 大小写不敏感唯一：A@x.com 与 a@x.com 是同一个账号。
        # 0003 迁移把原来的 users_email_unique 换成了这个表达式索引。
        Index("users_email_lower_unique", text("lower(email)"), unique=True),
    )

    def __repr__(self) -> str:
        return f"<User {self.email}>"


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("uuidv7()"))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'ACTIVE'"))
    created_by: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="RESTRICT", name="workspaces_created_by_fkey"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    def __repr__(self) -> str:
        return f"<Workspace {self.name}>"


class WorkspaceMember(Base):
    """成员关系。没有 RLS —— 它就是 RLS 策略要查的那张表。"""

    __tablename__ = "workspace_members"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, server_default=text("uuidv7()"))
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "workspaces.id",
            ondelete="CASCADE",
            name="workspace_members_workspace_id_fkey",
        ),
        nullable=False,
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE", name="workspace_members_user_id_fkey"),
        nullable=False,
    )
    # OWNER / ADMIN / EDITOR / VIEWER，由 CHECK 约束守护，取值域见 WorkspaceRole
    role: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="workspace_members_unique"),
        Index("idx_workspace_members_user", "user_id"),
        Index("idx_workspace_members_workspace_role", "workspace_id", "role"),
    )

    def __repr__(self) -> str:
        return f"<WorkspaceMember {self.workspace_id}/{self.user_id} {self.role}>"


class WorkspaceStatus:
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class WorkspaceRole:
    OWNER = "OWNER"
    ADMIN = "ADMIN"
    EDITOR = "EDITOR"
    VIEWER = "VIEWER"
