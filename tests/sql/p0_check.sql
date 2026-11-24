\set ON_ERROR_STOP on

-- P0 修复的回归测试（0003_p0_fixes）。
-- 整体在事务内执行，结尾 ROLLBACK，不污染开发库。
-- 每个断言都对应一个具体故障：这里失败说明某个 P0 又退回去了。

BEGIN;

DO $p0$
DECLARE
    v_user      uuid;
    v_ws        uuid;
    v_strategy  uuid;
    v_sv1       uuid;
    v_sv2       uuid;
    v_resolved  uuid;
    v_content   uuid;
    v_content2  uuid;
    v_a1        uuid;
    v_a2        uuid;
    v_status    text;
    v_count     integer;
    v_email     text;
BEGIN
    v_email := 'Case' || replace(gen_random_uuid()::text, '-', '') || '@X.DEV';

    INSERT INTO users (email, display_name)
    VALUES (v_email, 'P0 User')
    RETURNING id INTO v_user;

    INSERT INTO workspaces (name, created_by) VALUES ('P0 WS', v_user) RETURNING id INTO v_ws;
    INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (v_ws, v_user, 'OWNER');

    INSERT INTO strategies (workspace_id, name, status)
    VALUES (v_ws, 'P0 Strategy', 'ACTIVE')
    RETURNING id INTO v_strategy;

    INSERT INTO strategy_versions
        (workspace_id, strategy_id, version_number, strategy_config, created_by)
    VALUES (v_ws, v_strategy, 1, '{"positioning":"v1"}'::jsonb, v_user)
    RETURNING id INTO v_sv1;

    -- ---------------------------------------------------------------
    -- P0-3：一个 workspace 只能有一条 ACTIVE 战略
    -- ---------------------------------------------------------------
    BEGIN
        INSERT INTO strategies (workspace_id, name, status)
        VALUES (v_ws, 'Second ACTIVE', 'ACTIVE');
        RAISE EXCEPTION 'P0-3 回归：同一 workspace 出现了两条 ACTIVE 战略';
    EXCEPTION WHEN unique_violation THEN
        RAISE NOTICE 'ok: P0-3 一个 workspace 只能有一条 ACTIVE 战略';
    END;

    -- ---------------------------------------------------------------
    -- P0-2：战略版本由服务端解析
    -- ---------------------------------------------------------------
    v_resolved := app.current_strategy_version(v_strategy);
    IF v_resolved IS DISTINCT FROM v_sv1 THEN
        RAISE EXCEPTION 'P0-2 回归：current_strategy_version 未解析到 v1';
    END IF;

    INSERT INTO strategy_versions
        (workspace_id, strategy_id, version_number, strategy_config, created_by)
    VALUES (v_ws, v_strategy, 2, '{"positioning":"v2"}'::jsonb, v_user)
    RETURNING id INTO v_sv2;

    UPDATE strategies SET current_version_number = 2 WHERE id = v_strategy;

    v_resolved := app.current_strategy_version(v_strategy);
    IF v_resolved IS DISTINCT FROM v_sv2 THEN
        RAISE EXCEPTION 'P0-2 回归：current_version_number=2 时未解析到 v2';
    END IF;

    IF app.current_strategy_version(gen_random_uuid()) IS NOT NULL THEN
        RAISE EXCEPTION 'P0-2 回归：不存在的战略应返回 NULL';
    END IF;
    RAISE NOTICE 'ok: P0-2 战略版本按 current_version_number 解析';

    -- ---------------------------------------------------------------
    -- P0-4：审批是追加式历史，contents.status 只有一个写入口
    -- ---------------------------------------------------------------
    INSERT INTO contents (workspace_id, title, status, created_by)
    VALUES (v_ws, 'P0 content', 'DRAFT', v_user)
    RETURNING id INTO v_content;

    INSERT INTO approvals (workspace_id, content_id, stage, status, comment)
    VALUES (v_ws, v_content, 'CONTENT', 'PENDING', 'r1')
    RETURNING id INTO v_a1;

    -- 同一阶段同时只能有一条 PENDING
    BEGIN
        INSERT INTO approvals (workspace_id, content_id, stage, status, comment)
        VALUES (v_ws, v_content, 'CONTENT', 'PENDING', 'r2');
        RAISE EXCEPTION 'P0-4 回归：同一阶段出现了两条 PENDING 审批';
    EXCEPTION WHEN unique_violation THEN
        RAISE NOTICE 'ok: P0-4 同一阶段只允许一条 PENDING';
    END;

    -- 驳回后内容退回 REVIEW，且历史不被覆盖
    UPDATE approvals SET status = 'REJECTED', decided_at = now() WHERE id = v_a1;

    SELECT status INTO v_status FROM contents WHERE id = v_content;
    IF v_status <> 'REVIEW' THEN
        RAISE EXCEPTION 'P0-4 回归：驳回后 contents.status 应为 REVIEW，实际 %', v_status;
    END IF;

    INSERT INTO approvals (workspace_id, content_id, stage, status, comment)
    VALUES (v_ws, v_content, 'CONTENT', 'PENDING', 'r2 after rejection')
    RETURNING id INTO v_a2;

    SELECT count(*) INTO v_count FROM approvals WHERE content_id = v_content;
    IF v_count <> 2 THEN
        RAISE EXCEPTION 'P0-4 回归：审批历史被覆盖，期望 2 条实际 % 条', v_count;
    END IF;

    UPDATE approvals SET status = 'APPROVED', decided_at = now() WHERE id = v_a2;

    SELECT status INTO v_status FROM contents WHERE id = v_content;
    IF v_status <> 'APPROVED' THEN
        RAISE EXCEPTION 'P0-4 回归：通过后 contents.status 应为 APPROVED，实际 %', v_status;
    END IF;

    -- 没有审批记录时不许直接写 APPROVED
    INSERT INTO contents (workspace_id, title, status, created_by)
    VALUES (v_ws, 'P0 content bypass', 'DRAFT', v_user)
    RETURNING id INTO v_content2;

    BEGIN
        UPDATE contents SET status = 'APPROVED' WHERE id = v_content2;
        RAISE EXCEPTION 'P0-4 回归：未经审批就把内容置为 APPROVED';
    EXCEPTION WHEN check_violation THEN
        RAISE NOTICE 'ok: P0-4 contents.status=APPROVED 必须由审批驱动';
    END;

    -- ---------------------------------------------------------------
    -- P0-5：邮箱大小写不敏感唯一
    -- ---------------------------------------------------------------
    BEGIN
        INSERT INTO users (email, display_name) VALUES (lower(v_email), 'Dup');
        RAISE EXCEPTION 'P0-5 回归：同一邮箱的大小写变体被当成两个账号';
    EXCEPTION WHEN unique_violation THEN
        RAISE NOTICE 'ok: P0-5 邮箱大小写不敏感唯一';
    END;

    RAISE NOTICE 'all P0 assertions passed';
END
$p0$;

ROLLBACK;

\echo ''
\echo 'P0 CHECK PASSED (rolled back)'
