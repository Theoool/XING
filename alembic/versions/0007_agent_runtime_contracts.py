"""Agent 层运行时契约收紧（Phase 1b 前置）。

0005 建了表，0006 补了租约 fencing。这一版只做「1b 编码前必须成立」的
六条契约 —— 全部是结构与约束，不新增表、不改业务语义：

1. **预算预留**（#1）。新增 ``agent_runs.reserved_cost``，CHECK 改成
   ``total_cost + reserved_cost <= max_cost``。

   原来的 ``total_cost <= max_cost`` 有个致命缺陷：LLM 调用发生在数据库
   事务之外，先花钱后记账，CHECK 只能拦「记账」拦不住「花钱」。更糟的是
   越界时触发器 UPDATE 失败会让整笔事务回滚，**``llm_calls`` 那行也一起
   消失** —— 供应商已经收了钱，库里查无此账。

   改成「先预留后结算」：调用前用一条条件 UPDATE 原子地占额度，调用后
   释放预留、写入明细。只要预留额是真实花费的**上界**，结算时永远不会
   再触发 CHECK，也就不存在「记账失败丢掉审计」。

2. **step_count 归 Runtime**（#2）。触发器不再碰它。原来触发器里写
   ``+ CASE WHEN purpose = 'step' THEN 1 ELSE 0``，而文档里 Runtime 也加 1 —
   —— 传 ``'plan'`` 加 0（预算失效），传默认值 ``'step'`` 加 2（提前耗尽）。
   现在只有一个写入方：``UPDATE ... SET step_count = step_count + 1
   WHERE step_count < max_steps``，0 行即超预算。CHECK 保留作兜底。

3. **``llm_calls`` 按租户绑定**（#5）。``(run_id, workspace_id)`` 复合外键
   指向 ``agent_runs(id, workspace_id)``。worker 是 BYPASSRLS，写错
   ``workspace_id`` 不会被策略挡住；这条外键是唯一能拦住「把 A 的调用记到
   B 的账上」的东西。

4. **attempt / call_seq 分离**（#6）。``attempt`` 是 run 级（= 任务第几次
   执行），``call_seq`` 是 run 内调用序号。原来的唯一键
   ``(run_id, step_no, purpose, attempt)`` 假设「一个 step 内每个 purpose
   只调一次」，同一 step 内重试两次就撞键。

5. **run 不可删**（#7）。``parent_run_id`` 由 CASCADE 改 RESTRICT，并撤销
   ``xing_app`` 对 ``agent_runs`` / ``llm_calls`` 的 DELETE 权限。
   级联删会把子 run 的审计一起带走，而审计的价值就在于事后能查。

6. **replay 键带租户**（#8）。唯一范围从 ``(replay_key)`` 改为
   ``(workspace_id, replay_key)``。全局唯一会让 B 租户的请求撞上 A 租户
   已录制的键，命中后读到别人的响应。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007_agent_runtime_contracts"
down_revision: str | None = "0006_task_lease_fencing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---------------- 1. 预算预留 ----------------
    op.execute(
        "ALTER TABLE agent_runs "
        "ADD COLUMN reserved_cost numeric(18,8) NOT NULL DEFAULT 0"
    )
    op.execute("ALTER TABLE agent_runs DROP CONSTRAINT agent_runs_cost_within_budget")
    op.execute(
        "ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_cost_within_budget "
        "CHECK (total_cost + reserved_cost <= max_cost)"
    )
    op.execute(
        "ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_reserved_sane "
        "CHECK (reserved_cost >= 0)"
    )

    # ---------------- 3. llm_calls 绑定租户 ----------------
    # 复合外键要求被引用列组唯一。id 已是主键，加上 workspace_id 只为满足
    # 「组合唯一」这条语法要求，但它顺带把「run 的租户」变成了可引用事实。
    op.execute(
        "ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_id_workspace_key "
        "UNIQUE (id, workspace_id)"
    )
    op.execute("ALTER TABLE llm_calls DROP CONSTRAINT llm_calls_run_id_fkey")
    op.execute(
        "ALTER TABLE llm_calls ADD CONSTRAINT llm_calls_run_workspace_fkey "
        "FOREIGN KEY (run_id, workspace_id) "
        "REFERENCES agent_runs (id, workspace_id) ON DELETE CASCADE"
    )

    # ---------------- 4. attempt / call_seq 分离 ----------------
    op.execute("ALTER TABLE llm_calls ADD COLUMN call_seq integer NOT NULL DEFAULT 1")
    # 去掉默认值：Runtime 必须显式给序号。留默认值等于允许静默重号。
    op.execute("ALTER TABLE llm_calls ALTER COLUMN call_seq DROP DEFAULT")
    op.execute("ALTER TABLE llm_calls ADD CONSTRAINT llm_calls_attempt_positive CHECK (attempt > 0)")

    op.execute("DROP INDEX llm_calls_step_unique")
    op.execute(
        "CREATE UNIQUE INDEX llm_calls_call_seq ON llm_calls (run_id, call_seq)"
    )

    # purpose 不再有兜底默认值 —— 归类是调用方的责任，不是数据库的猜测
    op.execute("ALTER TABLE llm_calls ALTER COLUMN purpose DROP DEFAULT")

    # ---------------- 5. 审计不可删 ----------------
    op.execute("ALTER TABLE agent_runs DROP CONSTRAINT agent_runs_parent_run_id_fkey")
    op.execute(
        "ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_parent_run_id_fkey "
        "FOREIGN KEY (parent_run_id) REFERENCES agent_runs (id) ON DELETE RESTRICT"
    )
    op.execute("REVOKE DELETE ON agent_runs FROM xing_app")
    op.execute("REVOKE DELETE ON llm_calls FROM xing_app")

    # ---------------- 6. replay 键带租户 ----------------
    # 唯一性只约束「录制」，不约束「回放」：一次回放命中会产生第二行，
    # 若把 REPLAYED 也纳入唯一范围，回放本身就会撞键。判据是
    # status='SUCCEEDED' —— 失败调用没有可用响应，重试应当重新录制。
    op.execute("DROP INDEX llm_calls_replay_key")
    op.execute(
        "CREATE UNIQUE INDEX llm_calls_replay_key "
        "ON llm_calls (workspace_id, replay_key) "
        "WHERE replay_key IS NOT NULL AND status = 'SUCCEEDED'"
    )

    # ---------------- 2. step_count 归 Runtime ----------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.rollup_llm_call_cost()
         RETURNS trigger
         LANGUAGE plpgsql
        AS $function$
                BEGIN
                    IF TG_OP = 'INSERT' THEN
                        -- 只累计 token 与金额。step_count 由 Runtime 用条件
                        -- UPDATE 原子递增，这里再动一次就是双重计数。
                        UPDATE agent_runs
                           SET total_tokens = total_tokens
                                            + NEW.input_tokens + NEW.output_tokens,
                               total_cost   = total_cost + NEW.cost
                         WHERE id = NEW.run_id;
                        RETURN NULL;
                    END IF;

                    -- 明细被删时回滚累计。生产路径不删（审计不可删），
                    -- 但测试与一次性排障会用到。
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
                $function$
        """
    )


def downgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.rollup_llm_call_cost()
         RETURNS trigger
         LANGUAGE plpgsql
        AS $function$
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
                $function$
        """
    )

    op.execute("DROP INDEX llm_calls_replay_key")
    op.execute(
        "CREATE UNIQUE INDEX llm_calls_replay_key "
        "ON llm_calls (replay_key) WHERE replay_key IS NOT NULL"
    )

    op.execute("GRANT DELETE ON llm_calls TO xing_app")
    op.execute("GRANT DELETE ON agent_runs TO xing_app")
    op.execute("ALTER TABLE agent_runs DROP CONSTRAINT agent_runs_parent_run_id_fkey")
    op.execute(
        "ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_parent_run_id_fkey "
        "FOREIGN KEY (parent_run_id) REFERENCES agent_runs (id) ON DELETE CASCADE"
    )

    op.execute("ALTER TABLE llm_calls ALTER COLUMN purpose SET DEFAULT 'step'")
    op.execute("DROP INDEX llm_calls_call_seq")
    op.execute(
        "CREATE UNIQUE INDEX llm_calls_step_unique "
        "ON llm_calls (run_id, step_no, purpose, attempt)"
    )
    op.execute("ALTER TABLE llm_calls DROP CONSTRAINT llm_calls_attempt_positive")
    op.execute("ALTER TABLE llm_calls DROP COLUMN call_seq")

    op.execute("ALTER TABLE llm_calls DROP CONSTRAINT llm_calls_run_workspace_fkey")
    op.execute(
        "ALTER TABLE llm_calls ADD CONSTRAINT llm_calls_run_id_fkey "
        "FOREIGN KEY (run_id) REFERENCES agent_runs (id) ON DELETE CASCADE"
    )
    op.execute("ALTER TABLE agent_runs DROP CONSTRAINT agent_runs_id_workspace_key")

    op.execute("ALTER TABLE agent_runs DROP CONSTRAINT agent_runs_reserved_sane")
    op.execute("ALTER TABLE agent_runs DROP CONSTRAINT agent_runs_cost_within_budget")
    op.execute(
        "ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_cost_within_budget "
        "CHECK (total_cost <= max_cost)"
    )
    op.execute("ALTER TABLE agent_runs DROP COLUMN reserved_cost")
