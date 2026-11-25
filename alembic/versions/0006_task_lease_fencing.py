"""租约 fencing + 依赖初始化 —— 1a 的执行底座闭合

Revision ID: 0006_task_lease_fencing
Revises: 0005_agent_layer
Create Date: 2026-11-25

0005 把队列跑通了，但有两条缺陷属于 1a 自己的验收项，不补则"完成"是名义上的：

1. 租约缺 fencing token（P0）
   ``locked_by`` 是**身份**，不是**代次**。身份校验有两个挡不住的场景：

   a) A 读到 RUNNING 后卡住 → 租约过期 → B 回收 → C 领取 →
      A 恢复并 commit。此时 A 的 UPDATE 只带主键，会把 C 正在跑的任务
      标成 SUCCEEDED、清空 C 的租约。``guard_task_status_transition``
      看到 RUNNING→SUCCEEDED 是合法边，**放行** —— 它校验边，不校验谁有权走。

   b) ``make_worker_id()`` 是 hostname:pid。容器里 hostname 是服务名、
      pid 常为 1，重启后 worker_id **完全相同**，旧进程残留时身份校验直接通过。

   解法是单调递增的 ``lease_generation``：领取、回收各 +1，
   所有状态写入带 ``WHERE lease_generation = :gen``，旧代次的写入影响 0 行。

   边界：fencing 只能挡**数据库写入**。已经发给第三方的请求无法撤销，
   那需要业务层的幂等键（1b 的 ``idempotency_key = run_id:step_no``）。

2. 依赖初始化缺失（P0）
   ``promote_dependent_tasks`` 只在「上游变 SUCCEEDED」时触发，
   漏掉两条路径：

   a) 无依赖的任务创建后停在 PENDING，没有任何事件会推进它。
   b) 上游**早已** SUCCEEDED，之后才创建下游 —— 上游不会再成功一次。

   补一个 AFTER INSERT 触发器，与「上游完成」路径共用同一个判定函数
   ``app.dependencies_satisfied``，避免两处逻辑漂移。
"""

from alembic import op

revision: str = "0006_task_lease_fencing"
down_revision: str | None = "0005_agent_layer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ==============================================================
    # 1. 租约代次
    # ==============================================================
    op.execute(
        "ALTER TABLE tasks ADD COLUMN lease_generation bigint NOT NULL DEFAULT 0"
    )
    # 自依赖会让任务永远等自己。不加 CHECK 的话表现是「永久 PENDING、零报错」，
    # 属于最难查的一类；这里在写入时就拒掉。
    op.execute(
        """
        ALTER TABLE tasks
            ADD CONSTRAINT tasks_no_self_dependency
                CHECK (NOT (id = ANY (depends_on)))
        """
    )

    # ==============================================================
    # 2. 依赖判定（单一真相）
    # ==============================================================
    # 空依赖 → true；有依赖 → 全部 SUCCEEDED 才 true。
    # unnest(NULL) 返回空集，NOT EXISTS 得 true，所以 NULL 也不需要特判；
    # 保留 COALESCE 是为了让"空依赖恒成立"这件事在定义里可读。
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.dependencies_satisfied(p_deps uuid[])
        RETURNS boolean
        LANGUAGE sql
        STABLE
        AS $$
            SELECT COALESCE(
                NOT EXISTS (
                    SELECT 1 FROM unnest(p_deps) AS d(dep_id)
                     WHERE NOT EXISTS (
                         SELECT 1 FROM tasks p
                          WHERE p.id = d.dep_id
                            AND p.status = 'SUCCEEDED'
                     )
                ),
                true
            );
        $$;
        """
    )

    # ==============================================================
    # 3. 创建时推进
    # ==============================================================
    # BEFORE 还是 AFTER？必须 AFTER INSERT：BEFORE 阶段行还没落盘，
    # 依赖表里查不到同事务内刚插入的上游，判定会假阴性。
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.promote_task_if_ready()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.status <> 'PENDING' THEN
                RETURN NULL;
            END IF;

            IF NOT app.dependencies_satisfied(NEW.depends_on) THEN
                RETURN NULL;
            END IF;

            -- 带 status = 'PENDING' 条件：并发下若已被他人推进，这里是 0 行，
            -- 不会把 READY 覆盖回去。
            UPDATE tasks
               SET status = 'READY'
             WHERE id = NEW.id
               AND status = 'PENDING';

            RETURN NULL;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_tasks_promote_on_insert
            AFTER INSERT ON tasks
            FOR EACH ROW
            EXECUTE FUNCTION app.promote_task_if_ready()
        """
    )

    # 上游完成路径改为复用同一判定函数。
    # 原先内联的 NOT EXISTS 子查询与 dependencies_satisfied 是同一逻辑的两份拷贝，
    # 一旦要支持「SKIPPED 也算满足」这类变更，必然只改一处。
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.promote_dependent_tasks()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.status <> 'SUCCEEDED' THEN
                RETURN NEW;
            END IF;

            UPDATE tasks t
               SET status = 'READY'
             WHERE t.status = 'PENDING'
               AND t.workspace_id = NEW.workspace_id
               AND t.depends_on @> ARRAY[NEW.id]
               AND app.dependencies_satisfied(t.depends_on);

            RETURN NEW;
        END;
        $$;
        """
    )

    # ==============================================================
    # 4. 授权
    # ==============================================================
    op.execute(
        """
        DO $$
        DECLARE r text;
        BEGIN
            FOREACH r IN ARRAY ARRAY['xing_app', 'xing_worker'] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
                    EXECUTE format(
                        'GRANT EXECUTE ON FUNCTION app.dependencies_satisfied(uuid[]) TO %I', r
                    );
                    EXECUTE format(
                        'GRANT EXECUTE ON FUNCTION app.promote_task_if_ready() TO %I', r
                    );
                END IF;
            END LOOP;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_tasks_promote_on_insert ON tasks")
    op.execute("DROP FUNCTION IF EXISTS app.promote_task_if_ready()")

    # 还原 0005 的内联版本，让 downgrade 真正回到 0005 的行为
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.promote_dependent_tasks()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.status <> 'SUCCEEDED' THEN
                RETURN NEW;
            END IF;

            UPDATE tasks t
               SET status = 'READY'
             WHERE t.status = 'PENDING'
               AND t.workspace_id = NEW.workspace_id
               AND t.depends_on @> ARRAY[NEW.id]
               AND NOT EXISTS (
                   SELECT 1 FROM unnest(t.depends_on) AS d(dep_id)
                    WHERE NOT EXISTS (
                        SELECT 1 FROM tasks p
                         WHERE p.id = d.dep_id
                           AND p.status = 'SUCCEEDED'
                    )
               );

            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute("DROP FUNCTION IF EXISTS app.dependencies_satisfied(uuid[])")

    op.execute("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_no_self_dependency")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS lease_generation")
