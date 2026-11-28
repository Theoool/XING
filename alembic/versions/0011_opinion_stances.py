"""舆论立场层：把「模型想到的角度」换成「语料里真实存在的立场」。

展开这一步原来只有两次调用：一次背景检索、一次让模型想六个角度。检索结果
只用来校准 ``why_now``，六个切面全部来自模型先验 —— 于是「同一个事件，不同
人群怎么吵」这件事从来没有被真正采集过。

这一版加两张表，把因果关系倒过来：

    opinions_stances（下称 opinion_stances）  一次展开采集到的立场池
        label     一句话标签，如「责任在签字人」
        camp      SUPPORT / OPPOSE / CONDITIONAL / EXPERIENCE / OBSERVER
        claim     这个立场主张什么
        audience  谁持有它（人群 + 处境）
        quote     支撑它的原文（落库前已机检为材料子串）
        event_id  指回 external_events 的那篇材料

    topic_stances  候选引用立场（多对多）
        有引用才有候选。展开时「引用不到立场」的候选直接被丢掉，
        这条规则由外键与代码共同保证：``stance_id`` 必须真实存在、
        且属于同一个 workspace。

为什么是两张表而不是把立场塞进 ``topics.brief``：一份立场会被多个候选引用
（同一阵营的两个不同受众），而且它需要保留到原文的溯源。塞进 jsonb 之后
「这个候选凭什么这么切」就只能靠人读 JSON，也没法用外键挡住跨租户引用。

``topics`` 上一版只有主键，这一版补 ``(id, workspace_id)`` 唯一约束，
作为 ``topic_stances`` 复合外键的被引用侧 —— worker 是 BYPASSRLS，
RLS 挡不住把两个租户的行接在一起，只有复合外键能挡（0007/0009 同样处理）。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011_opinion_stances"
down_revision: str | None = "0010_topic_evidence_quote"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ==============================================================
    # 1. 复合外键的被引用侧
    # ==============================================================
    op.execute(
        "ALTER TABLE topics ADD CONSTRAINT topics_id_workspace_key UNIQUE (id, workspace_id)"
    )

    # ==============================================================
    # 2. 立场池
    # ==============================================================
    op.execute(
        """
        CREATE TABLE opinion_stances (
            id              uuid PRIMARY KEY DEFAULT uuidv7(),
            workspace_id    uuid NOT NULL
                            REFERENCES workspaces(id) ON DELETE CASCADE,
            seed_task_id    uuid NOT NULL,
            event_id        uuid NOT NULL,
            label           text NOT NULL,
            camp            text NOT NULL,
            claim           text NOT NULL,
            audience        text NOT NULL,
            quote           text NOT NULL,
            created_at      timestamptz NOT NULL DEFAULT now(),

            CONSTRAINT opinion_stances_id_workspace_key UNIQUE (id, workspace_id),
            CONSTRAINT opinion_stances_camp_check CHECK (
                camp IN ('SUPPORT', 'OPPOSE', 'CONDITIONAL', 'EXPERIENCE', 'OBSERVER')
            ),
            CONSTRAINT opinion_stances_seed_task_workspace_fkey
                FOREIGN KEY (seed_task_id, workspace_id)
                REFERENCES tasks (id, workspace_id) ON DELETE CASCADE,
            CONSTRAINT opinion_stances_event_workspace_fkey
                FOREIGN KEY (event_id, workspace_id)
                REFERENCES external_events (id, workspace_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE opinion_stances IS "
        "'Public-opinion stances extracted from retrieved material for one expand task. "
        "Every candidate topic must cite at least one of these.'"
    )
    # 同一篇材料上的同一个标签只算一个立场。展开重跑靠它做 upsert，
    # 而不是「先删再插」—— worker 没有 DELETE 权限。
    op.execute(
        "CREATE UNIQUE INDEX opinion_stances_seed_event_label "
        "ON opinion_stances (seed_task_id, event_id, label)"
    )
    op.execute(
        "CREATE INDEX idx_opinion_stances_seed ON opinion_stances (workspace_id, seed_task_id)"
    )
    op.execute("CREATE INDEX idx_opinion_stances_event ON opinion_stances (event_id)")

    # ==============================================================
    # 3. 候选 → 立场
    # ==============================================================
    op.execute(
        """
        CREATE TABLE topic_stances (
            workspace_id    uuid NOT NULL,
            topic_id        uuid NOT NULL,
            stance_id       uuid NOT NULL,
            is_primary      boolean NOT NULL DEFAULT false,
            created_at      timestamptz NOT NULL DEFAULT now(),

            PRIMARY KEY (topic_id, stance_id),
            CONSTRAINT topic_stances_topic_workspace_fkey
                FOREIGN KEY (topic_id, workspace_id)
                REFERENCES topics (id, workspace_id) ON DELETE CASCADE,
            CONSTRAINT topic_stances_stance_workspace_fkey
                FOREIGN KEY (stance_id, workspace_id)
                REFERENCES opinion_stances (id, workspace_id) ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "COMMENT ON TABLE topic_stances IS "
        "'Which public-opinion stances a candidate topic is built on. "
        "A candidate with no rows here is a model prior, not a grounded topic.'"
    )
    op.execute("CREATE INDEX idx_topic_stances_stance ON topic_stances (stance_id)")

    # ==============================================================
    # 4. RLS
    # ==============================================================
    op.execute("ALTER TABLE opinion_stances ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY opinion_stances_workspace_policy
        ON opinion_stances
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
    op.execute("ALTER TABLE topic_stances ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY topic_stances_workspace_policy
        ON topic_stances
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
    # 5. 授权
    # ==============================================================
    # 新表不继承 0002 的 "ALL TABLES" 授权 —— 那时它们还不存在。
    # worker 只给到 UPDATE：立场与引用都是证据，没有 DELETE。
    op.execute(
        """
        DO $$
        DECLARE r text;
        BEGIN
            FOREACH r IN ARRAY ARRAY['xing_app', 'xing_worker'] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
                    EXECUTE format(
                        'GRANT SELECT, INSERT, UPDATE ON opinion_stances TO %I', r
                    );
                    EXECUTE format(
                        'GRANT SELECT, INSERT, UPDATE ON topic_stances TO %I', r
                    );
                END IF;
            END LOOP;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS topic_stances CASCADE")
    op.execute("DROP TABLE IF EXISTS opinion_stances CASCADE")
    op.execute("ALTER TABLE topics DROP CONSTRAINT IF EXISTS topics_id_workspace_key")
