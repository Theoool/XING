"""v0.1 schema — 19 tables, RLS, triggers

Revision ID: 0002_v01_schema
Revises: 0001_baseline
Create Date: 2026-11-25

DDL 正文放在 alembic/sql/v01_schema.sql，便于直接审阅和 diff。
asyncpg 不支持一次执行多语句，且脚本内含 plpgsql 函数体（$$ ... $$），
简单按分号切分会切坏函数体，因此用 xing.db.sql.split_sql 做词法切分。
"""

from pathlib import Path

from alembic import op

from xing.db.sql import split_sql

revision: str = "0002_v01_schema"
down_revision: str | None = "0001_baseline"
branch_labels = None
depends_on = None

SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "v01_schema.sql"

# 逆序删除，避免外键依赖问题
TABLES = [
    "agent_runs",
    "approvals",
    "feedback",
    "performance_snapshots",
    "publications",
    "productions",
    "tasks",
    "content_versions",
    "contents",
    "decision_traces",
    "decisions",
    "topic_events",
    "topics",
    "external_events",
    "strategy_versions",
    "strategies",
    "workspace_members",
    "workspaces",
    "users",
]


def upgrade() -> None:
    for statement in split_sql(SQL_PATH.read_text(encoding="utf-8")):
        op.execute(statement)


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS app.is_workspace_member(uuid)")
    op.execute("DROP FUNCTION IF EXISTS app.current_user_id()")
    op.execute("DROP FUNCTION IF EXISTS app.current_workspace_id()")
    op.execute("DROP FUNCTION IF EXISTS app.set_updated_at()")
    op.execute("DROP SCHEMA IF EXISTS app CASCADE")
