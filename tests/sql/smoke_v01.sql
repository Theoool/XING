\set ON_ERROR_STOP on

-- 全链路冒烟测试：从 strategy 一路走到 performance + feedback。
-- 整体在事务内执行，结尾 ROLLBACK，不污染开发库。
-- 任一断言失败会抛异常，psql 以非零码退出。

BEGIN;

DO $smoke$
DECLARE
    v_user      uuid;
    v_ws        uuid;
    v_strategy  uuid;
    v_sv        uuid;
    v_event     uuid;
    v_topic     uuid;
    v_decision  uuid;
    v_content   uuid;
    v_approval  uuid;
    v_cstatus   text;
    v_prod      uuid;
    v_pub1      uuid;
    v_pub2      uuid;
    v_run       uuid;
    v_after     timestamptz;
BEGIN
    INSERT INTO users (email, display_name)
    VALUES ('smoke_' || gen_random_uuid() || '@xing.dev', 'Smoke User')
    RETURNING id INTO v_user;

    INSERT INTO workspaces (name, created_by)
    VALUES ('Smoke WS', v_user)
    RETURNING id INTO v_ws;

    INSERT INTO workspace_members (workspace_id, user_id, role)
    VALUES (v_ws, v_user, 'OWNER');

    INSERT INTO strategies (workspace_id, name, status)
    VALUES (v_ws, 'Smoke Strategy', 'ACTIVE')
    RETURNING id INTO v_strategy;

    INSERT INTO strategy_versions
        (workspace_id, strategy_id, version_number, strategy_config, created_by)
    VALUES
        (v_ws, v_strategy, 1,
         jsonb_build_object('positioning', 'test', 'platforms', jsonb_build_array('YOUTUBE')),
         v_user)
    RETURNING id INTO v_sv;

    INSERT INTO external_events
        (workspace_id, source_type, external_id, title, summary)
    VALUES
        (v_ws, 'NEWS', 'evt-' || gen_random_uuid(), 'Something happened', 'Short summary')
    RETURNING id INTO v_event;

    INSERT INTO topics (workspace_id, primary_event_id, title, status)
    VALUES (v_ws, v_event, 'Smoke topic', 'SELECTED')
    RETURNING id INTO v_topic;

    INSERT INTO topic_events (topic_id, event_id, role)
    VALUES (v_topic, v_event, 'PRIMARY');

    INSERT INTO decisions
        (workspace_id, strategy_version_id, topic_id, decision_type, status, selected_option, decided_by, decided_at)
    VALUES
        (v_ws, v_sv, v_topic, 'TOPIC_SELECTION', 'APPROVED', 'SELECT', v_user, now())
    RETURNING id INTO v_decision;

    INSERT INTO decision_traces (workspace_id, decision_id, trace_type, data)
    VALUES (v_ws, v_decision, 'SCORE', '{"relevance": 0.91}'::jsonb);

    INSERT INTO contents
        (workspace_id, topic_id, decision_id, title, content_type, status, created_by)
    VALUES
        (v_ws, v_topic, v_decision, 'Smoke content', 'VIDEO_SCRIPT', 'DRAFT', v_user)
    RETURNING id INTO v_content;

    INSERT INTO content_versions
        (workspace_id, content_id, version_number, title, body, created_by)
    VALUES
        (v_ws, v_content, 1, 'Smoke content', 'Body text', v_user);

    -- 审批是 contents.status 的唯一写入口（P0-4）：
    -- 直接 UPDATE contents SET status='APPROVED' 会被守卫触发器拒绝，
    -- 必须由 approvals 的状态迁移驱动。
    INSERT INTO approvals (workspace_id, content_id, stage, status, comment)
    VALUES (v_ws, v_content, 'CONTENT', 'PENDING', 'first review')
    RETURNING id INTO v_approval;

    SELECT status INTO v_cstatus FROM contents WHERE id = v_content;
    IF v_cstatus <> 'DRAFT' THEN
        RAISE EXCEPTION 'PENDING approval should not change contents.status, got %', v_cstatus;
    END IF;

    UPDATE approvals
       SET status = 'APPROVED', reviewer_id = v_user, decided_at = now()
     WHERE id = v_approval;

    SELECT status INTO v_cstatus FROM contents WHERE id = v_content;
    IF v_cstatus <> 'APPROVED' THEN
        RAISE EXCEPTION 'approval did not drive contents.status, got %', v_cstatus;
    END IF;
    RAISE NOTICE 'ok: approvals drives contents.status';

    INSERT INTO productions
        (workspace_id, content_id, content_version_number, production_type, status)
    VALUES
        (v_ws, v_content, 1, 'VIDEO', 'SUCCEEDED')
    RETURNING id INTO v_prod;

    INSERT INTO publications (workspace_id, production_id, platform, status)
    VALUES (v_ws, v_prod, 'YOUTUBE', 'PENDING')
    RETURNING id INTO v_pub1;

    -- 修复验证：同一平台可以有多条尚未拿到 external_post_id 的记录
    INSERT INTO publications (workspace_id, production_id, platform, status)
    VALUES (v_ws, v_prod, 'YOUTUBE', 'PENDING')
    RETURNING id INTO v_pub2;

    INSERT INTO performance_snapshots
        (workspace_id, publication_id, age_bucket, views, likes, completion_rate)
    VALUES
        (v_ws, v_pub1, '24H', 12000, 340, 0.42);

    INSERT INTO feedback (workspace_id, content_id, feedback_type, rating, created_by)
    VALUES (v_ws, v_content, 'HUMAN_REVIEW', 4.5, v_user);

    -- 0005 起 agent_runs 是 run 级（goal/plan/预算），一次模型调用记 llm_calls。
    INSERT INTO agent_runs (workspace_id, agent_name, goal, status, completed_at)
    VALUES (v_ws, 'writer', 'smoke: produce one content', 'SUCCEEDED', now())
    RETURNING id INTO v_run;

    INSERT INTO llm_calls
        (workspace_id, run_id, model_provider, model_name, purpose, call_seq,
         input_tokens, output_tokens, cost, status)
    VALUES
        (v_ws, v_run, 'openai', 'gpt-4o-mini', 'generate', 1,
         1200, 800, 0.0012, 'SUCCEEDED');

    -- 成本由触发器累加到 run 上，这里顺带验证一次
    IF NOT EXISTS (
        SELECT 1 FROM agent_runs
         WHERE id = v_run AND total_tokens = 2000 AND total_cost = 0.0012
    ) THEN
        RAISE EXCEPTION 'llm_calls 成本未累加到 agent_runs';
    END IF;

    RAISE NOTICE 'ok: full core loop inserted';

    -- 触发器：updated_at 应被刷新。
    -- 注意 now() 返回的是事务开始时间，同一事务内两次调用值相同，
    -- 所以不能靠"先 insert 再 update"来观察变化。
    -- 这里先把 updated_at 压到过去（临时关掉触发器），再看它是否被覆盖。
    ALTER TABLE strategies DISABLE TRIGGER trg_strategies_updated_at;
    UPDATE strategies
       SET updated_at = timestamptz '2000-01-01 00:00:00+00'
     WHERE id = v_strategy;
    ALTER TABLE strategies ENABLE TRIGGER trg_strategies_updated_at;

    UPDATE strategies SET name = name || ' v2' WHERE id = v_strategy;
    SELECT updated_at INTO v_after FROM strategies WHERE id = v_strategy;
    IF v_after <= timestamptz '2000-01-01 00:00:00+00' THEN
        RAISE EXCEPTION 'updated_at trigger did not fire';
    END IF;
    RAISE NOTICE 'ok: updated_at trigger';

    -- 唯一约束：相同 external_post_id 必须冲突
    UPDATE publications SET external_post_id = 'POST-1', status = 'PUBLISHED' WHERE id = v_pub1;
    BEGIN
        UPDATE publications SET external_post_id = 'POST-1' WHERE id = v_pub2;
        RAISE EXCEPTION 'expected unique violation on (platform, external_post_id)';
    EXCEPTION WHEN unique_violation THEN
        RAISE NOTICE 'ok: external_post_id uniqueness enforced';
    END;

    -- 幂等键：同一 publication 的同一 age_bucket 只能有一条
    BEGIN
        INSERT INTO performance_snapshots (workspace_id, publication_id, age_bucket, views)
        VALUES (v_ws, v_pub1, '24H', 1);
        RAISE EXCEPTION 'expected unique violation on (publication_id, age_bucket)';
    EXCEPTION WHEN unique_violation THEN
        RAISE NOTICE 'ok: performance snapshot idempotency';
    END;

    -- feedback 必须恰好挂一个目标
    BEGIN
        INSERT INTO feedback (workspace_id, feedback_type)
        VALUES (v_ws, 'SYSTEM');
        RAISE EXCEPTION 'expected check violation on feedback target';
    EXCEPTION WHEN check_violation THEN
        RAISE NOTICE 'ok: feedback exactly-one-target';
    END;

    -- 状态值受 CHECK 约束
    BEGIN
        UPDATE contents SET status = 'NOT_A_STATUS' WHERE id = v_content;
        RAISE EXCEPTION 'expected check violation on contents.status';
    EXCEPTION WHEN check_violation THEN
        RAISE NOTICE 'ok: status CHECK enforced';
    END;

    RAISE NOTICE 'all smoke assertions passed';
END
$smoke$;

ROLLBACK;

\echo ''
\echo 'SMOKE TEST PASSED (rolled back)'
