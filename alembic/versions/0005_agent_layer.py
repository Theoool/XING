"""Agent 层：tasks 补列 + agent_runs 语义修正

Revision ID: 0005_agent_layer
Revises: 0004_topic_uniqueness
Create Date: 2026-11-25

对应 docs/AGENT_DESIGN.md（冻结稿 v1）。三件事：

1. tasks 变成「可被 worker 领取的执行单元」
   补 assigned_agent / depends_on / retry_count / max_retries / available_at / 租约三件套，
   状态机新增 READY（依赖满足、可领）与 RETRYING（等待退避）。
   合法迁移由 guard_task_status_transition 守卫 —— 契约住数据库，不在 Python 里散落 if。

2. agent_runs 正名为 llm_calls
   原表存的是「一次模型调用」（model/tokens/cost/latency），却占用了 run 这个名字，
   导致真正的 run 级字段（goal/plan/预算）无处可放。表内 0 行，改名零成本。

3. 新建 run 级 agent_runs
   一次 Agent 执行的目标、计划、自治级别、三层预算与花费。

成本记账方向：llm_calls 是唯一明细源，agent_runs.total_* 由触发器累加。
预算超限靠 CHECK 抛 23514，而不是应用层 if —— 嵌套调用、漏检查的路径都能兜住。
"""

from alembic import op

revision: str = "0005_agent_layer"
down_revision: str | None = "0004_topic_uniqueness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ==============================================================
    # 1. tasks：补列 + 状态机扩展
    # ==============================================================
    op.execute(
        """
        ALTER TABLE tasks
            ADD COLUMN assigned_agent   text,
            ADD COLUMN depends_on       uuid[]      NOT NULL DEFAULT '{}',
            ADD COLUMN retry_count      smallint    NOT NULL DEFAULT 0,
            ADD COLUMN max_retries      smallint    NOT NULL DEFAULT 3,
            ADD COLUMN available_at     timestamptz NOT NULL DEFAULT now(),
            ADD COLUMN locked_by        text,
            ADD COLUMN locked_at        timestamptz,
            ADD COLUMN lease_expires_at timestamptz
        """
    )
    op.execute(
        """
        ALTER TABLE tasks
            ADD CONSTRAINT tasks_retry_within_limit
                CHECK (retry_count <= max_retries),
            ADD CONSTRAINT tasks_lease_consistency
                CHECK ((locked_by IS NULL) = (lease_expires_at IS NULL))
        """
    )

    # 状态取值域：新增 READY / RETRYING。
    # 必须 DROP 后重建：ALTER CONSTRAINT 不能改 CHECK 表达式。
    op.execute("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_status_check")
    op.execute(
        """
        ALTER TABLE tasks
            ADD CONSTRAINT tasks_status_check
                CHECK (status IN (
                    'PENDING', 'READY', 'RUNNING', 'WAITING',
                    'RETRYING', 'SUCCEEDED', 'FAILED', 'CANCELLED'
                ))
        """
    )

    # 领取索引：从「扫 PENDING」改成「扫 READY」，并把排序键换成 available_at。
    # 部分索引体积只与可领任务量同阶，不受历史任务堆积影响。
    op.execute("DROP INDEX IF EXISTS idx_tasks_pending")
    op.execute(
        """
        CREATE INDEX idx_tasks_claimable
            ON tasks (workspace_id, priority DESC, available_at)
         WHERE status = 'READY'
        """
    )
    # 依赖推进要按 depends_on 反查（depends_on @> ARRAY[task_id]）
    op.execute("CREATE INDEX idx_tasks_depends_on ON tasks USING gin (depends_on)")

    # ==============================================================
    # 2. agent_runs → llm_calls（整表改名 + 对象改名）
    # ==============================================================
    op.execute("ALTER TABLE agent_runs RENAME TO llm_calls")
    op.execute(
        "ALTER POLICY agent_runs_workspace_policy ON llm_calls "
        "RENAME TO llm_calls_workspace_policy"
    )
    op.execute(
        "ALTER INDEX idx_agent_runs_workspace_started RENAME TO idx_llm_calls_run_started"
    )
    op.execute("ALTER INDEX idx_agent_runs_model RENAME TO idx_llm_calls_model")
    # idx_agent_runs_task 引用 task_id，而 task_id 要到第 4 步才删。
    # 索引名在 schema 内全局唯一，不先删掉会与第 3 步新表上的同名索引撞车。
    op.execute("DROP INDEX IF EXISTS idx_agent_runs_task")

    # CHECK / FK / PK 约束名同样含旧表名。跳过 contype='n'：
    # PG 18 把 NOT NULL 也登记为约束，改名它们没有收益，且会给 downgrade 添麻烦。
    op.execute(
        """
        DO $$
        DECLARE r record;
        BEGIN
            FOR r IN
                SELECT c.conname
                  FROM pg_constraint c
                  JOIN pg_class t ON t.oid = c.conrelid
                 WHERE t.relname = 'llm_calls'
                   AND c.conname LIKE 'agent_runs_%'
                   AND c.contype <> 'n'
            LOOP
                EXECUTE format(
                    'ALTER TABLE llm_calls RENAME CONSTRAINT %I TO %I',
                    r.conname,
                    replace(r.conname, 'agent_runs_', 'llm_calls_')
                );
            END LOOP;
        END
        $$;
        """
    )
    # 主键约束改名会连带重命名底层索引，不需要（也不能）再 ALTER INDEX

    # ==============================================================
    # 3. 新建 run 级 agent_runs
    # ==============================================================
    op.execute(
        """
        CREATE TABLE agent_runs (
            id                  uuid PRIMARY KEY DEFAULT uuidv7(),

            workspace_id        uuid NOT NULL
                                REFERENCES workspaces(id)
                                ON DELETE CASCADE,

            task_id             uuid
                                REFERENCES tasks(id)
                                ON DELETE SET NULL,

            parent_run_id       uuid
                                REFERENCES agent_runs(id)
                                ON DELETE CASCADE,

            agent_name          text NOT NULL,
            attempt             smallint NOT NULL DEFAULT 1,

            goal                text NOT NULL,
            plan                jsonb,

            autonomy_mode       text NOT NULL DEFAULT 'SEMI_AUTO'
                                CHECK (autonomy_mode IN ('SUGGEST', 'SEMI_AUTO', 'AUTO')),

            status              text NOT NULL DEFAULT 'RUNNING'
                                CHECK (status IN (
                                    'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED'
                                )),

            max_steps           integer       NOT NULL DEFAULT 20,
            step_count          integer       NOT NULL DEFAULT 0,
            max_tokens          bigint        NOT NULL DEFAULT 200000,
            total_tokens        bigint        NOT NULL DEFAULT 0,
            max_cost            numeric(18,8) NOT NULL DEFAULT 2.0,
            total_cost          numeric(18,8) NOT NULL DEFAULT 0,

            output              jsonb,
            error               jsonb,

            started_at          timestamptz NOT NULL DEFAULT now(),
            completed_at        timestamptz,

            CONSTRAINT agent_runs_attempt_positive     CHECK (attempt > 0),
            CONSTRAINT agent_runs_max_steps_positive   CHECK (max_steps > 0),
            CONSTRAINT agent_runs_step_count_sane      CHECK (step_count >= 0),
            CONSTRAINT agent_runs_token_count_sane     CHECK (total_tokens >= 0),
            CONSTRAINT agent_runs_cost_sane            CHECK (total_cost >= 0),
            CONSTRAINT agent_runs_no_self_parent       CHECK (parent_run_id IS DISTINCT FROM id),

            CONSTRAINT agent_runs_steps_within_budget
                CHECK (step_count <= max_steps),
            CONSTRAINT agent_runs_tokens_within_budget
                CHECK (total_tokens <= max_tokens),
            CONSTRAINT agent_runs_cost_within_budget
                CHECK (total_cost <= max_cost),

            CONSTRAINT agent_runs_completed_consistency
                CHECK ((status = 'RUNNING') = (completed_at IS NULL))
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX agent_runs_task_attempt
            ON agent_runs (task_id, attempt)
         WHERE task_id IS NOT NULL
        """
    )
    op.execute(
        "CREATE INDEX idx_agent_runs_workspace_started "
        "ON agent_runs (workspace_id, started_at DESC)"
    )
    op.execute("CREATE INDEX idx_agent_runs_task   ON agent_runs (task_id)")
    op.execute("CREATE INDEX idx_agent_runs_parent ON agent_runs (parent_run_id)")
    op.execute(
        "COMMENT ON TABLE agent_runs IS "
        "'One agent execution: goal, plan, autonomy mode, three-layer budget.'"
    )

    # ==============================================================
    # 4. llm_calls：挂到 run 上，补录制回放字段
    # ==============================================================
    op.execute(
        """
        ALTER TABLE llm_calls
            ADD COLUMN run_id     uuid NOT NULL
                                  REFERENCES agent_runs(id)
                                  ON DELETE CASCADE,
            ADD COLUMN purpose    text     NOT NULL DEFAULT 'step',
            ADD COLUMN attempt    smallint NOT NULL DEFAULT 1,
            ADD COLUMN step_no    integer  NOT NULL DEFAULT 0,
            ADD COLUMN request    jsonb,
            ADD COLUMN response   jsonb,
            ADD COLUMN replay_key text
        """
    )
    # task_id 由 run 上反查，冗余；agent_name 同一 run 内不换
    op.execute("ALTER TABLE llm_calls DROP COLUMN task_id")
    op.execute("ALTER TABLE llm_calls DROP COLUMN agent_name")

    # 回放模式记 0 成本，需要一个独立状态，不能混进 SUCCEEDED
    op.execute("ALTER TABLE llm_calls DROP CONSTRAINT llm_calls_status_check")
    op.execute(
        """
        ALTER TABLE llm_calls
            ADD CONSTRAINT llm_calls_status_check
                CHECK (status IN (
                    'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'REPLAYED'
                ))
        """
    )

    op.execute(
        """
        CREATE UNIQUE INDEX llm_calls_step_unique
            ON llm_calls (run_id, step_no, purpose, attempt)
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX llm_calls_replay_key
            ON llm_calls (replay_key)
         WHERE replay_key IS NOT NULL
        """
    )
    op.execute("CREATE INDEX idx_llm_calls_run ON llm_calls (run_id, started_at)")
    # 改名沿用旧注释，但语义已经变了：这张表现在只管「一次调用」，
    # 成本汇总在 agent_runs 上。注释必须跟着改，否则是误导。
    op.execute(
        "COMMENT ON TABLE llm_calls IS "
        "'One model call. Detail source of cost; agent_runs.total_* rolls it up.'"
    )

    # ==============================================================
    # 5. 新表 RLS
    # ==============================================================
    op.execute("ALTER TABLE agent_runs ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY agent_runs_workspace_policy
        ON agent_runs
        FOR ALL
        USING (
            workspace_id = app.current_workspace_id()
            AND app.is_workspace_member(workspace_id)
        )
        WITH CHECK (
            workspace_id = app.current_workspace_id()
            AND app.is_workspace_member(workspace_id)
        )
        """
    )

    # ==============================================================
    # 6. 状态机守卫与依赖推进
    # ==============================================================
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.guard_task_status_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF OLD.status = NEW.status THEN
                RETURN NEW;
            END IF;

            IF NOT EXISTS (
                SELECT 1 FROM (VALUES
                    ('PENDING',  'READY'),
                    ('PENDING',  'CANCELLED'),
                    ('READY',    'RUNNING'),
                    ('READY',    'CANCELLED'),
                    ('RUNNING',  'SUCCEEDED'),
                    ('RUNNING',  'FAILED'),
                    ('RUNNING',  'WAITING'),
                    ('RUNNING',  'RETRYING'),
                    ('RUNNING',  'CANCELLED'),
                    ('WAITING',  'RUNNING'),
                    ('WAITING',  'FAILED'),
                    ('WAITING',  'CANCELLED'),
                    ('RETRYING', 'READY'),
                    ('RETRYING', 'FAILED'),
                    ('RETRYING', 'CANCELLED')
                ) AS v(from_status, to_status)
                WHERE v.from_status = OLD.status
                  AND v.to_status   = NEW.status
            ) THEN
                RAISE EXCEPTION
                    'tasks.status 非法迁移: % -> % (task_id=%)',
                    OLD.status, NEW.status, NEW.id
                    USING ERRCODE = 'check_violation',
                          HINT = '合法迁移见 docs/AGENT_DESIGN.md §3.1';
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_tasks_status_guard
            BEFORE UPDATE OF status ON tasks
            FOR EACH ROW
            EXECUTE FUNCTION app.guard_task_status_transition()
        """
    )

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
    op.execute(
        """
        CREATE TRIGGER trg_tasks_promote_dependents
            AFTER UPDATE OF status ON tasks
            FOR EACH ROW
            EXECUTE FUNCTION app.promote_dependent_tasks()
        """
    )

    # 成本累加：llm_calls 是明细源，agent_runs.total_* 是事务内快照。
    # 累加越界时 CHECK 抛 23514，整笔事务回滚 —— 这就是预算拦截。
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.rollup_llm_call_cost()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                UPDATE agent_runs
                   SET total_tokens = total_tokens
                                    + NEW.input_tokens + NEW.output_tokens,
                       total_cost   = total_cost + NEW.cost,
                       step_count   = step_count
                                    + CASE WHEN NEW.purpose = 'step' THEN 1 ELSE 0 END
                 WHERE id = NEW.run_id;
                RETURN NULL;
            END IF;

            IF TG_OP = 'DELETE' THEN
                UPDATE agent_runs
                   SET total_tokens = total_tokens
                                    - OLD.input_tokens - OLD.output_tokens,
                       total_cost   = total_cost - OLD.cost
                 WHERE id = OLD.run_id;
                RETURN NULL;
            END IF;

            RETURN NULL;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_llm_calls_rollup
            AFTER INSERT OR DELETE ON llm_calls
            FOR EACH ROW
            EXECUTE FUNCTION app.rollup_llm_call_cost()
        """
    )

    # ==============================================================
    # 7. 授权（角色可能尚未创建，用 DO 块兜住）
    # ==============================================================
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'xing_app') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON agent_runs TO xing_app;
                GRANT SELECT, INSERT, UPDATE, DELETE ON llm_calls  TO xing_app;
                GRANT EXECUTE ON FUNCTION app.guard_task_status_transition() TO xing_app;
                GRANT EXECUTE ON FUNCTION app.promote_dependent_tasks()     TO xing_app;
                GRANT EXECUTE ON FUNCTION app.rollup_llm_call_cost()        TO xing_app;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_llm_calls_rollup ON llm_calls")
    op.execute("DROP TRIGGER IF EXISTS trg_tasks_promote_dependents ON tasks")
    op.execute("DROP TRIGGER IF EXISTS trg_tasks_status_guard ON tasks")
    op.execute("DROP FUNCTION IF EXISTS app.rollup_llm_call_cost()")
    op.execute("DROP FUNCTION IF EXISTS app.promote_dependent_tasks()")
    op.execute("DROP FUNCTION IF EXISTS app.guard_task_status_transition()")

    # llm_calls.run_id 引着 agent_runs，必须先断
    op.execute("DROP INDEX IF EXISTS llm_calls_step_unique")
    op.execute("DROP INDEX IF EXISTS llm_calls_replay_key")
    op.execute("DROP INDEX IF EXISTS idx_llm_calls_run")
    op.execute("ALTER TABLE llm_calls DROP COLUMN run_id")
    op.execute("ALTER TABLE llm_calls DROP COLUMN purpose")
    op.execute("ALTER TABLE llm_calls DROP COLUMN attempt")
    op.execute("ALTER TABLE llm_calls DROP COLUMN step_no")
    op.execute("ALTER TABLE llm_calls DROP COLUMN request")
    op.execute("ALTER TABLE llm_calls DROP COLUMN response")
    op.execute("ALTER TABLE llm_calls DROP COLUMN replay_key")

    op.execute("DROP TABLE IF EXISTS agent_runs CASCADE")

    # 还原 llm_calls 的列与被删对象
    op.execute("ALTER TABLE llm_calls ADD COLUMN task_id uuid")
    op.execute(
        "ALTER TABLE llm_calls ADD COLUMN agent_name text NOT NULL DEFAULT 'unknown'"
    )
    op.execute(
        "ALTER TABLE llm_calls ALTER COLUMN agent_name DROP DEFAULT"
    )
    op.execute(
        """
        ALTER TABLE llm_calls
            ADD CONSTRAINT agent_runs_task_id_fkey
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL
        """
    )
    op.execute("ALTER TABLE llm_calls DROP CONSTRAINT llm_calls_status_check")
    op.execute(
        """
        ALTER TABLE llm_calls
            ADD CONSTRAINT llm_calls_status_check
                CHECK (status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED'))
        """
    )
    op.execute(
        """
        DO $$
        DECLARE r record;
        BEGIN
            FOR r IN
                SELECT c.conname
                  FROM pg_constraint c
                  JOIN pg_class t ON t.oid = c.conrelid
                 WHERE t.relname = 'llm_calls'
                   AND c.conname LIKE 'llm_calls_%'
                   AND c.contype <> 'n'
            LOOP
                EXECUTE format(
                    'ALTER TABLE llm_calls RENAME CONSTRAINT %I TO %I',
                    r.conname,
                    replace(r.conname, 'llm_calls_', 'agent_runs_')
                );
            END LOOP;
        END
        $$;
        """
    )
    # 约束名改回 agent_runs_* 时，主键索引同样自动跟随
    op.execute(
        "ALTER INDEX idx_llm_calls_run_started RENAME TO idx_agent_runs_workspace_started"
    )
    op.execute("ALTER INDEX idx_llm_calls_model RENAME TO idx_agent_runs_model")
    op.execute(
        "ALTER POLICY llm_calls_workspace_policy ON llm_calls "
        "RENAME TO agent_runs_workspace_policy"
    )
    op.execute("ALTER TABLE llm_calls RENAME TO agent_runs")
    # 还原 0002 的原始表注释，让 downgrade 真正回到 0004 的状态
    op.execute(
        "COMMENT ON TABLE agent_runs IS "
        "'Minimal AI execution accounting: model, tokens, cost, latency, status. "
        "No chain-of-thought.'"
    )
    op.execute(
        "CREATE INDEX idx_agent_runs_task ON agent_runs (task_id)"
    )

    # 还原 tasks
    op.execute("DROP INDEX IF EXISTS idx_tasks_depends_on")
    op.execute("DROP INDEX IF EXISTS idx_tasks_claimable")
    # 状态先收敛回旧取值域，否则旧 CHECK 加不上
    op.execute("UPDATE tasks SET status = 'PENDING' WHERE status = 'READY'")
    op.execute("UPDATE tasks SET status = 'FAILED' WHERE status = 'RETRYING'")
    op.execute("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_status_check")
    op.execute(
        """
        ALTER TABLE tasks
            ADD CONSTRAINT tasks_status_check
                CHECK (status IN (
                    'PENDING', 'RUNNING', 'WAITING',
                    'SUCCEEDED', 'FAILED', 'CANCELLED'
                ))
        """
    )
    op.execute(
        """
        CREATE INDEX idx_tasks_pending
            ON tasks (workspace_id, priority DESC, created_at)
         WHERE status = 'PENDING'
        """
    )
    op.execute(
        """
        ALTER TABLE tasks
            DROP CONSTRAINT IF EXISTS tasks_retry_within_limit,
            DROP CONSTRAINT IF EXISTS tasks_lease_consistency,
            DROP COLUMN IF EXISTS assigned_agent,
            DROP COLUMN IF EXISTS depends_on,
            DROP COLUMN IF EXISTS retry_count,
            DROP COLUMN IF EXISTS max_retries,
            DROP COLUMN IF EXISTS available_at,
            DROP COLUMN IF EXISTS locked_by,
            DROP COLUMN IF EXISTS locked_at,
            DROP COLUMN IF EXISTS lease_expires_at
        """
    )
