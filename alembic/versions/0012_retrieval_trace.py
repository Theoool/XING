"""取证轨迹：把「怎么搜的、搜到什么、为什么留/丢」变成可查的数据。

展开这一步原来是黑盒：一次检索计划、一次立场抽取，最后只剩一批候选。中间
发生了什么只有日志，重启就没了 —— 于是「为什么这题缺 CONDITIONAL」「这条
立场凭什么算 UGC」只能靠猜。

这一版加两样东西：

    external_events.source_kind
        这条材料是「谁在说话」：MEDIA / UGC / OFFICIAL / VESTED / UNKNOWN。
        由 ``search/sources.py`` 按域名判定，判定命中的规则一并写进轨迹，
        因为「用了哪条规则」比结果本身更需要被核对。

    retrieval_traces
        一次检索一行：query、层级（媒体/讨论/补采）、真正下发的参数、
        耗时、返回条数、以及**每一条文档**的域名/来源类型/是否采用/丢弃原因。
        文档明细存 jsonb：一条 query 的结果天然是一个数组，拆表只会让
        读取变成 N+1，而这里唯一的用法就是「整段读出来给人看」。

为什么不塞进 ``tasks.output``：补采会追加第二轮，任务失败重试又会覆盖，
而轨迹的价值恰恰在于「失败时也还在」。它是诊断数据，必须独立于业务结果。

没有 URL 的材料（模型端检索）也会有一行，``domain`` 为空、``kind`` 按
标签判定 —— 认不出来就写 UNKNOWN，不猜。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012_retrieval_trace"
down_revision: str | None = "0011_opinion_stances"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ==============================================================
    # 1. 材料的来源类型
    # ==============================================================
    op.execute(
        "ALTER TABLE external_events ADD COLUMN source_kind text NOT NULL DEFAULT 'UNKNOWN'"
    )
    op.execute(
        "ALTER TABLE external_events ADD CONSTRAINT external_events_source_kind_check "
        "CHECK (source_kind IN ('MEDIA', 'UGC', 'OFFICIAL', 'VESTED', 'UNKNOWN'))"
    )
    op.execute(
        "COMMENT ON COLUMN external_events.source_kind IS "
        "'Who is speaking: MEDIA / UGC / OFFICIAL / VESTED / UNKNOWN. "
        "Classified from the URL by search/sources.py; UNKNOWN means unclassified, not unimportant.'"
    )
    op.execute("CREATE INDEX idx_external_events_kind ON external_events (workspace_id, source_kind)")

    # ==============================================================
    # 2. 取证轨迹
    # ==============================================================
    op.execute(
        """
        CREATE TABLE retrieval_traces (
            id              uuid PRIMARY KEY DEFAULT uuidv7(),
            workspace_id    uuid NOT NULL
                            REFERENCES workspaces(id) ON DELETE CASCADE,
            task_id         uuid NOT NULL,
            run_id          uuid,
            attempt         smallint NOT NULL DEFAULT 1,
            round           smallint NOT NULL DEFAULT 0,
            seq             smallint NOT NULL DEFAULT 1,
            layer           text NOT NULL,
            query           text NOT NULL,
            target          text,
            provider        text NOT NULL,
            options         jsonb NOT NULL DEFAULT '{}'::jsonb,
            status          text NOT NULL,
            latency_ms      integer NOT NULL DEFAULT 0,
            result_count    integer NOT NULL DEFAULT 0,
            new_count       integer NOT NULL DEFAULT 0,
            docs            jsonb NOT NULL DEFAULT '[]'::jsonb,
            error           text,
            created_at      timestamptz NOT NULL DEFAULT now(),

            CONSTRAINT retrieval_traces_layer_check CHECK (
                layer IN ('MEDIA', 'DISCUSSION', 'SUPPLEMENT')
            ),
            CONSTRAINT retrieval_traces_status_check CHECK (
                status IN ('OK', 'EMPTY', 'ERROR')
            ),
            CONSTRAINT retrieval_traces_task_workspace_fkey
                FOREIGN KEY (task_id, workspace_id)
                REFERENCES tasks (id, workspace_id) ON DELETE CASCADE,
            CONSTRAINT retrieval_traces_attempt_seq_key
                UNIQUE (task_id, attempt, round, seq)
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE retrieval_traces IS "
        "'One row per executed search: the params actually sent, what came back, "
        "and why each document was kept or dropped. Diagnostic data, not business state.'"
    )
    op.execute(
        "CREATE INDEX idx_retrieval_traces_task ON retrieval_traces (task_id, round, seq)"
    )

    # ==============================================================
    # 3. RLS
    # ==============================================================
    op.execute("ALTER TABLE retrieval_traces ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY retrieval_traces_workspace_policy
        ON retrieval_traces
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
    # 4. 授权
    # ==============================================================
    # 轨迹只增不改：重试会写新一轮（attempt 不同），但不该被回头修饰。
    # 只写 GRANT 是不够的 —— scripts/app_role.sql 里对**未来**的表设了
    # ALTER DEFAULT PRIVILEGES（SELECT, INSERT, UPDATE, DELETE），
    # 所以新表建出来时应用角色就已经有 UPDATE/DELETE 了。
    # 不 REVOKE 的话「轨迹不可篡改」只是注释里的说法，不是数据库的事实：
    # 任何一段走漏的代码都能把昨天那次检索改写掉。
    op.execute(
        """
        DO $$
        DECLARE r text;
        BEGIN
            FOREACH r IN ARRAY ARRAY['xing_app', 'xing_worker'] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
                    EXECUTE format(
                        'GRANT SELECT, INSERT ON retrieval_traces TO %I', r
                    );
                    EXECUTE format(
                        'REVOKE UPDATE, DELETE, TRUNCATE ON retrieval_traces FROM %I', r
                    );
                END IF;
            END LOOP;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS retrieval_traces CASCADE")
    op.execute("DROP INDEX IF EXISTS idx_external_events_kind")
    op.execute(
        "ALTER TABLE external_events DROP CONSTRAINT IF EXISTS external_events_source_kind_check"
    )
    op.execute("ALTER TABLE external_events DROP COLUMN IF EXISTS source_kind")
