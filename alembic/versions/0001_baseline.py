"""baseline: 通用基础设施

Revision ID: 0001_baseline
Revises:
Create Date: 2026-11-25

只放全库共用的基础设施，不含业务表。
扩展（vector / pg_trgm / pg_stat_statements）由 docker/postgres/init 在
启动时创建，需要超级用户权限，因此不放在迁移里。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 所有带 updated_at 的表统一挂这个触发器，避免各处手写时间戳逻辑
    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS set_updated_at();")
