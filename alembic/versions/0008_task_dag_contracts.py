"""Phase 2 的任务图契约：计划幂等、内容溯源、失败向下传播。

Phase 2 把「一条 goal」变成「一张由 ``depends_on`` 串联的任务图」。任务图一旦
真的跑起来，会在真实并发下暴露三个 Phase 1 结构上挡不住的问题。这一版只补
这三条，不新增表、不改业务语义：

1. **计划必须幂等**（``tasks.dedup_key``）。``plan_content`` 因租约丢失重跑时
   会再插一套子任务，结果是同一个 goal 长出两条生产线：两篇内容、两份成本、
   两套审批。这不是理论风险 —— 租约回收本来就是常态路径。
   唯一索引 ``(workspace_id, dedup_key)`` 让「同一计划步骤只存在一次」成为
   数据库事实，插入方用 ``ON CONFLICT DO NOTHING`` + 查回拿到既有 id。

2. **失败必须向下传播**（``app.cancel_blocked_dependents``）。现在的
   ``promote_dependent_tasks`` 只在 ``SUCCEEDED`` 时推进，于是上游永久
   ``FAILED`` 后，下游永远停在 ``PENDING`` —— 没有报错、没有告警、没有任何
   迹象。任务图里只要有一个节点会永久失败，这条就是硬缺陷。
   一条 ``WITH RECURSIVE`` 把所有后代 ``PENDING`` 一次置 ``CANCELLED``，
   而不是让触发器递归推进：深度和触发次数都可控。
   ``UNION``（不是 ``UNION ALL``）顺带挡住 ``depends_on`` 成环时的无限递归。

3. **内容要能溯源**（``contents.source_task_id``）。Phase 1 的写任务幂等靠
   ``content_versions.metadata->>'task_id'`` 顺序扫描，任务上千后重跑路径会
   变慢，且「这篇内容由哪个任务产出」无法反查。加成带索引的列之后，
   幂等判定和溯源都变成一次索引查找。

三条都只加列与索引。``contents.source_task_id`` 用 ``ON DELETE SET NULL``：
任务不会被删（只 CANCELLED），但即便被删，内容也不该跟着消失。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_task_dag_contracts"
down_revision: str | None = "0007_agent_runtime_contracts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ==============================================================
    # 1. 计划幂等
    # ==============================================================
    op.execute("ALTER TABLE tasks ADD COLUMN dedup_key text")
    # 部分唯一索引：绝大多数任务没有 dedup_key，不该进索引。
    op.execute(
        """
        CREATE UNIQUE INDEX tasks_dedup_key
            ON tasks (workspace_id, dedup_key)
         WHERE dedup_key IS NOT NULL
        """
    )

    # ==============================================================
    # 2. 内容溯源
    # ==============================================================
    op.execute(
        """
        ALTER TABLE contents
            ADD COLUMN source_task_id uuid
            CONSTRAINT contents_source_task_id_fkey
                REFERENCES tasks (id) ON DELETE SET NULL
        """
    )
    # 一个生产任务只产出一篇内容。多条产出的需求（一稿多平台）出现时，
    # 唯一范围要改成 (source_task_id, ordinal)，语义随之改变 —— 届时另起迁移。
    op.execute(
        """
        CREATE UNIQUE INDEX contents_source_task
            ON contents (source_task_id)
         WHERE source_task_id IS NOT NULL
        """
    )

    # ==============================================================
    # 3. 失败向下传播
    # ==============================================================
    # 为什么是一条语句递归到底，而不是像 promote 那样一跳一跳：
    # 跳一次要等一次触发器，深度不可控、日志不可读，且中间态（A 已取消、
    # B 还没）会被并发的查询看到。一条语句要么全改要么全不改。
    #
    # UNION 去重不只是省事：depends_on 没有成环约束，INSERT 时挡不住
    # A→B→A 这种图（只能靠调用方自觉）。UNION 让环退化成一个点，
    # 递归必然终止；换成 UNION ALL 就是一个死循环占满连接。
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.cancel_blocked_dependents()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.status NOT IN ('FAILED', 'CANCELLED') THEN
                RETURN NULL;
            END IF;

            WITH RECURSIVE doomed AS (
                SELECT t.id
                  FROM tasks t
                 WHERE t.status = 'PENDING'
                   AND t.workspace_id = NEW.workspace_id
                   AND t.depends_on @> ARRAY[NEW.id]
                UNION
                SELECT t.id
                  FROM tasks t
                  JOIN doomed d ON t.depends_on @> ARRAY[d.id]
                 WHERE t.status = 'PENDING'
                   AND t.workspace_id = NEW.workspace_id
            )
            UPDATE tasks
               SET status = 'CANCELLED',
                   completed_at = now(),
                   error = jsonb_build_object(
                       'code', 'UPSTREAM_FAILED',
                       'message', '上游任务未成功，本任务被取消'
                   )
             WHERE id IN (SELECT id FROM doomed);

            RETURN NULL;
        END;
        $$;
        """
    )
    # 只关心「进入终态失败」这一个方向。WHEN 子句让成功路径完全不触发，
    # 省掉每完成一个任务就进一次 plpgsql 的开销。
    op.execute(
        """
        CREATE TRIGGER trg_tasks_cancel_dependents
            AFTER UPDATE OF status ON tasks
            FOR EACH ROW
            WHEN (NEW.status IN ('FAILED', 'CANCELLED'))
            EXECUTE FUNCTION app.cancel_blocked_dependents()
        """
    )

    # ==============================================================
    # 4. 授权
    # ==============================================================
    # 触发器函数的 EXECUTE 在 CREATE TRIGGER 时校验，运行时不校验；这里显式
    # 授权是为了让「谁可以手动调用」也成文，且与 0006 的处理保持一致。
    op.execute(
        """
        DO $$
        DECLARE r text;
        BEGIN
            FOREACH r IN ARRAY ARRAY['xing_app', 'xing_worker'] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
                    EXECUTE format(
                        'GRANT EXECUTE ON FUNCTION app.cancel_blocked_dependents() TO %I', r
                    );
                END IF;
            END LOOP;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_tasks_cancel_dependents ON tasks")
    op.execute("DROP FUNCTION IF EXISTS app.cancel_blocked_dependents()")

    op.execute("DROP INDEX IF EXISTS contents_source_task")
    op.execute(
        "ALTER TABLE contents DROP CONSTRAINT IF EXISTS contents_source_task_id_fkey"
    )
    op.execute("ALTER TABLE contents DROP COLUMN IF EXISTS source_task_id")

    op.execute("DROP INDEX IF EXISTS tasks_dedup_key")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS dedup_key")
