"""选题与来源事件的一对一约束

Revision ID: 0004_topic_uniqueness
Revises: 0003_p0_fixes
Create Date: 2026-11-25

recommend 接口会把「近期未被覆盖的事件」转成候选选题。
没有唯一约束时，并发调用（或两个请求同时打到不同实例）会为同一个事件
建出两个选题，之后的所有统计都会重复计数。

用部分唯一索引而不是改表结构：primary_event_id 本身允许为 NULL
（人工创建的选题可以没有来源事件），而 NULL 在唯一索引里互不相等，
正好符合语义。
"""

from alembic import op

revision: str = "0004_topic_uniqueness"
down_revision: str | None = "0003_p0_fixes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 先收敛历史重复：同一 (workspace, 事件) 只保留最早的一条，
    # 否则索引在有数据的库上建不起来。删掉的选题会级联删除 topic_events，
    # 并把 decisions.topic_id 置空——这是可接受的，因为重复选题本身就是脏数据。
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY workspace_id, primary_event_id
                       ORDER BY created_at, id
                   ) AS rn
              FROM topics
             WHERE primary_event_id IS NOT NULL
        )
        DELETE FROM topics t
         USING ranked r
         WHERE t.id = r.id
           AND r.rn > 1
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX topics_one_per_primary_event
            ON topics (workspace_id, primary_event_id)
         WHERE primary_event_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS topics_one_per_primary_event")
