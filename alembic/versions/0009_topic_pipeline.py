"""选题管线：种子溯源与深入产出。

用户给一个事件或一个方向，Agent 展开成多个候选选题、打分、人确认、再深入，
最后生成内容。这条链的每一环在库里都已经有承载：

    topics 的六态与四个分数、decisions + decision_traces、topic_events 的 role

缺的只有两样：

1. **种子溯源**（``topics.seed_task_id`` / ``seed_event_id``）。一次展开是一个
   task，N 个候选都指回它 —— 重跑会不会长出第二批、这个种子展开过哪些角度，
   都变成一次索引查找。**不能用 ``primary_event_id`` 表达这件事**：那个字段的
   语义是「这个选题的主证据」，而 ``topics_one_per_primary_event`` 是唯一索引，
   一个事件只能挂一个选题，与「一个事件展开多个选题」直接冲突。
   两个字段分工：``seed_*`` 是来源，``primary_event_id`` 是深入之后才确定的主证据。

2. **深入产出**（``topics.brief``）。expand 写 angle/audience/thesis，
   deepen 合并成完整版（加 evidence/counterpoints/outline）。一个选题一份，
   1:1 关系不建表。

两个复合外键要求被引用侧有 ``(id, workspace_id)`` 唯一约束，``tasks`` 与
``external_events`` 都还没有，这一版一并补。worker 是 ``BYPASSRLS``，
RLS 挡不住写错租户，只有复合外键能挡（0007 已经踩过）。

``ON DELETE SET NULL (列)`` 是 PG 15+ 的定向置空语法：不指定列的话，
``SET NULL`` 会连 ``workspace_id`` 一起置空，而它是 NOT NULL，删除时直接报错。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009_topic_pipeline"
down_revision: str | None = "0008_task_dag_contracts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ==============================================================
    # 1. 复合外键的被引用侧
    # ==============================================================
    # 复合外键要求「被引用列组」唯一。这两张表的 PK 只有 id，所以要显式补。
    # 代价是一条唯一索引，换来的是「引用方不可能跨租户」这条数据库事实。
    op.execute(
        "ALTER TABLE tasks ADD CONSTRAINT tasks_id_workspace_key UNIQUE (id, workspace_id)"
    )
    op.execute(
        "ALTER TABLE external_events ADD CONSTRAINT external_events_id_workspace_key "
        "UNIQUE (id, workspace_id)"
    )

    # ==============================================================
    # 2. topics 三列
    # ==============================================================
    op.execute("ALTER TABLE topics ADD COLUMN seed_task_id uuid")
    op.execute("ALTER TABLE topics ADD COLUMN seed_event_id uuid")
    op.execute("ALTER TABLE topics ADD COLUMN brief jsonb")

    op.execute(
        "ALTER TABLE topics ADD CONSTRAINT topics_seed_task_workspace_fkey "
        "FOREIGN KEY (seed_task_id, workspace_id) REFERENCES tasks (id, workspace_id) "
        "ON DELETE SET NULL (seed_task_id)"
    )
    op.execute(
        "ALTER TABLE topics ADD CONSTRAINT topics_seed_event_workspace_fkey "
        "FOREIGN KEY (seed_event_id, workspace_id) "
        "REFERENCES external_events (id, workspace_id) "
        "ON DELETE SET NULL (seed_event_id)"
    )

    # 部分索引：规则版 recommend 建的选题没有 seed_*，不该进索引。
    op.execute(
        "CREATE INDEX idx_topics_seed_task ON topics (workspace_id, seed_task_id) "
        "WHERE seed_task_id IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX idx_topics_seed_event ON topics (workspace_id, seed_event_id) "
        "WHERE seed_event_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_topics_seed_event")
    op.execute("DROP INDEX IF EXISTS idx_topics_seed_task")
    op.execute("ALTER TABLE topics DROP CONSTRAINT IF EXISTS topics_seed_event_workspace_fkey")
    op.execute("ALTER TABLE topics DROP CONSTRAINT IF EXISTS topics_seed_task_workspace_fkey")
    op.execute("ALTER TABLE topics DROP COLUMN IF EXISTS brief")
    op.execute("ALTER TABLE topics DROP COLUMN IF EXISTS seed_event_id")
    op.execute("ALTER TABLE topics DROP COLUMN IF EXISTS seed_task_id")
    op.execute(
        "ALTER TABLE external_events DROP CONSTRAINT IF EXISTS external_events_id_workspace_key"
    )
    op.execute("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_id_workspace_key")
