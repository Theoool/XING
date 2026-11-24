"""P0 修复：策略唯一性、审批单写入口、战略版本解析、邮箱大小写

Revision ID: 0003_p0_fixes
Revises: 0002_v01_schema
Create Date: 2026-11-25

这一版只做四件「不加就会让后续代码返工」的事，不新增业务表：

P0-2  app.current_strategy_version(uuid)
      决策必须绑定到「某一版战略」，但版本不该由客户端指定。
      给服务端一个唯一入口：按 strategies.current_version_number 解析出版本 id。
      decisions.strategy_version_id 保持 NOT NULL —— 它仍是唯一真源。

P0-3  strategies 一个 workspace 只能有一条 ACTIVE
      原 schema 只有普通索引，activate 接口无法保证唯一性，
      GET /workspaces/{id}/strategy（单数）也就没有确定语义。

P0-4  approvals 改为追加式历史 + contents.status 单写入口
      原来的 UNIQUE(content_id, stage) 让「驳回后重审」直接覆盖上一条，
      谁驳回、为什么，全部丢失。改成只约束 PENDING。
      同时用触发器把 contents.status 的唯一写入口收敛到 approvals，
      否则 approve 接口和 approvals 表必然分叉。

P0-5  users.email 改成大小写不敏感唯一
      原 UNIQUE(email) 是大小写敏感的，A@x.com 和 a@x.com 会变成两个账号。
"""

from alembic import op

revision: str = "0003_p0_fixes"
down_revision: str | None = "0002_v01_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------------------------------------------------------------
    # P0-3 一个 workspace 只能有一条 ACTIVE 战略
    # ---------------------------------------------------------------
    # 先收敛历史数据：同一 workspace 里保留最新创建的那条 ACTIVE，其余降级。
    # 不先做这一步，后面的唯一索引在有数据的库上会直接建不起来。
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY workspace_id
                       ORDER BY created_at DESC, id DESC
                   ) AS rn
              FROM strategies
             WHERE status = 'ACTIVE'
        )
        UPDATE strategies s
           SET status = 'PAUSED'
          FROM ranked r
         WHERE s.id = r.id
           AND r.rn > 1
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX strategies_one_active_per_workspace
            ON strategies (workspace_id)
         WHERE status = 'ACTIVE'
        """
    )

    # ---------------------------------------------------------------
    # P0-2 战略版本解析入口
    # ---------------------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.current_strategy_version(p_strategy_id uuid)
        RETURNS uuid
        LANGUAGE sql
        STABLE
        AS $$
            SELECT sv.id
              FROM strategies s
              JOIN strategy_versions sv
                ON sv.strategy_id = s.id
               AND sv.version_number = s.current_version_number
             WHERE s.id = p_strategy_id
        $$;
        """
    )
    op.execute(
        """
        COMMENT ON FUNCTION app.current_strategy_version(uuid) IS
        '按 strategies.current_version_number 解析当前战略版本。'
        'decisions.strategy_version_id 必须由服务端经此函数得到，不接受客户端直接传。';
        """
    )

    # ---------------------------------------------------------------
    # P0-4a approvals 改为追加式
    # ---------------------------------------------------------------
    op.execute("ALTER TABLE approvals DROP CONSTRAINT IF EXISTS approvals_unique_stage")
    op.execute(
        """
        CREATE UNIQUE INDEX approvals_one_pending_per_stage
            ON approvals (content_id, stage)
         WHERE status = 'PENDING'
        """
    )

    # ---------------------------------------------------------------
    # P0-4b approvals -> contents.status 单向同步
    # ---------------------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.sync_content_status_from_approval()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_target text;
        BEGIN
            -- 只有内容审核这一阶段会驱动 contents.status；
            -- PRODUCTION / PUBLICATION 两个阶段各自驱动 productions / publications。
            IF NEW.stage <> 'CONTENT' THEN
                RETURN NEW;
            END IF;

            v_target := CASE NEW.status
                            WHEN 'APPROVED'  THEN 'APPROVED'
                            WHEN 'REJECTED'  THEN 'REVIEW'
                            WHEN 'CANCELLED' THEN 'REVIEW'
                        END;

            IF v_target IS NULL THEN
                RETURN NEW;  -- PENDING 不改变内容状态
            END IF;

            -- 只允许在「审核态」之间迁移，避免把已经 PRODUCING/READY/PUBLISHED
            -- 的内容退回 REVIEW。
            UPDATE contents
               SET status = v_target
             WHERE id = NEW.content_id
               AND status IN ('DRAFT', 'REVIEW', 'APPROVED')
               AND status <> v_target;

            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_approvals_sync_content_status
        AFTER INSERT OR UPDATE ON approvals
        FOR EACH ROW
        EXECUTE FUNCTION app.sync_content_status_from_approval()
        """
    )

    # ---------------------------------------------------------------
    # P0-4c 反向守卫：没有已通过的审批，不允许把内容置为 APPROVED
    # ---------------------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.guard_content_approval_status()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.status <> 'APPROVED' THEN
                RETURN NEW;
            END IF;

            -- 已经是 APPROVED 的行再更新别的字段，不重复校验
            IF TG_OP = 'UPDATE' AND OLD.status = 'APPROVED' THEN
                RETURN NEW;
            END IF;

            IF NOT EXISTS (
                SELECT 1
                  FROM approvals a
                 WHERE a.content_id = NEW.id
                   AND a.stage = 'CONTENT'
                   AND a.status = 'APPROVED'
            ) THEN
                RAISE EXCEPTION
                    'contents.status=APPROVED 前必须有 stage=CONTENT、status=APPROVED 的审批记录 (content_id=%)',
                    NEW.id
                    USING ERRCODE = 'check_violation',
                          HINT = '调用 POST /contents/{id}/approve，不要直接改 contents.status';
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_contents_approval_guard
        BEFORE INSERT OR UPDATE ON contents
        FOR EACH ROW
        EXECUTE FUNCTION app.guard_content_approval_status()
        """
    )

    # ---------------------------------------------------------------
    # P0-5 邮箱唯一性改成大小写不敏感
    # ---------------------------------------------------------------
    op.execute("UPDATE users SET email = lower(email) WHERE email <> lower(email)")
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_email_unique")
    op.execute("CREATE UNIQUE INDEX users_email_lower_unique ON users (lower(email))")

    # ---------------------------------------------------------------
    # 新函数授权给应用角色（角色可能尚未创建，用 DO 块兜住）
    # ---------------------------------------------------------------
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'xing_app') THEN
                GRANT EXECUTE ON FUNCTION app.current_strategy_version(uuid) TO xing_app;
                GRANT EXECUTE ON FUNCTION app.sync_content_status_from_approval() TO xing_app;
                GRANT EXECUTE ON FUNCTION app.guard_content_approval_status() TO xing_app;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_contents_approval_guard ON contents")
    op.execute("DROP TRIGGER IF EXISTS trg_approvals_sync_content_status ON approvals")
    op.execute("DROP FUNCTION IF EXISTS app.guard_content_approval_status()")
    op.execute("DROP FUNCTION IF EXISTS app.sync_content_status_from_approval()")
    op.execute("DROP FUNCTION IF EXISTS app.current_strategy_version(uuid)")

    op.execute("DROP INDEX IF EXISTS approvals_one_pending_per_stage")
    # 历史行存在时这一步会失败——这是刻意的：降级不应该静默丢审批历史。
    op.execute("ALTER TABLE approvals ADD CONSTRAINT approvals_unique_stage UNIQUE (content_id, stage)")

    op.execute("DROP INDEX IF EXISTS strategies_one_active_per_workspace")

    op.execute("DROP INDEX IF EXISTS users_email_lower_unique")
    op.execute("ALTER TABLE users ADD CONSTRAINT users_email_unique UNIQUE (email)")
