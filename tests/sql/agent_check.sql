-- Agent 层数据库契约验证（0005 + 0006）
--
-- 验证四件"靠文档说不清、只能靠穷举证明"的事：
--   1. guard_task_status_transition 只放行 15 条迁移边（+ 8 条同态 no-op）
--   2. promote_dependent_tasks / promote_task_if_ready 在依赖满足时推进状态
--   3. rollup_llm_call_cost 累加正确，且超预算时被 CHECK 拦下
--   4. lease_generation 让旧代次的写入落空（fencing）
--
-- 用法：
--   docker compose exec -T db psql -U xing -d xing -v ON_ERROR_STOP=1 -f - < tests/sql/agent_check.sql
--
-- 全部用临时数据，结尾清理。可重复运行。

\set ON_ERROR_STOP on

DO $$
DECLARE
    v_ws       uuid;
    v_states   text[] := ARRAY[
        'PENDING', 'READY', 'RUNNING', 'WAITING',
        'RETRYING', 'SUCCEEDED', 'FAILED', 'CANCELLED'
    ];
    -- 与 docs/AGENT_DESIGN.md §3.1 逐条对应
    v_legal    text[] := ARRAY[
        'PENDING>READY',   'PENDING>CANCELLED',
        'READY>RUNNING',   'READY>CANCELLED',
        'RUNNING>SUCCEEDED', 'RUNNING>FAILED', 'RUNNING>WAITING',
        'RUNNING>RETRYING',  'RUNNING>CANCELLED',
        'WAITING>RUNNING',   'WAITING>FAILED', 'WAITING>CANCELLED',
        'RETRYING>READY',    'RETRYING>FAILED', 'RETRYING>CANCELLED'
    ];
    v_from     text;
    v_to       text;
    v_task     uuid;
    v_a        uuid;
    v_b        uuid;
    v_allowed  boolean;
    v_expected boolean;
    v_cases    int := 0;
BEGIN
    SELECT id INTO v_ws FROM workspaces ORDER BY created_at LIMIT 1;

    -- ---------- 1. 状态机穷举 ----------
    FOREACH v_from IN ARRAY v_states LOOP
        FOREACH v_to IN ARRAY v_states LOOP
            -- 直接 INSERT 目标起始态：guard 是 BEFORE UPDATE OF status，INSERT 不触发。
            --
            -- 但 AFTER INSERT 的 promote_task_if_ready 会把「无依赖的 PENDING」
            -- 推成 READY，那样就构造不出 PENDING 起点了。这里给一个永不存在的
            -- 依赖 id —— 不是测试后门，而是用真实语义（依赖未满足）构造合法状态，
            -- 顺便也验证了「依赖未满足 → 保持 PENDING」。
            INSERT INTO tasks (workspace_id, task_type, status, depends_on)
            VALUES (
                v_ws, 'AGENTCHECK', v_from,
                CASE WHEN v_from = 'PENDING'
                     THEN ARRAY[gen_random_uuid()]
                     ELSE '{}'::uuid[]
                END
            )
            RETURNING id INTO v_task;

            v_allowed := true;
            BEGIN
                UPDATE tasks SET status = v_to WHERE id = v_task;
            EXCEPTION WHEN check_violation THEN
                v_allowed := false;
            END;

            v_expected := (v_from = v_to) OR ((v_from || '>' || v_to) = ANY (v_legal));

            IF v_allowed <> v_expected THEN
                RAISE EXCEPTION
                    '状态机守卫不符: % -> %，期望放行=%，实际放行=%',
                    v_from, v_to, v_expected, v_allowed;
            END IF;

            v_cases := v_cases + 1;
            DELETE FROM tasks WHERE id = v_task;
        END LOOP;
    END LOOP;

    RAISE NOTICE 'ok  状态机守卫：% 种组合全部符合预期', v_cases;

    -- ---------- 2. 上游完成推进下游 ----------
    INSERT INTO tasks (workspace_id, task_type, status)
    VALUES (v_ws, 'AGENTCHECK', 'READY')
    RETURNING id INTO v_task;

    INSERT INTO tasks (workspace_id, task_type, status, depends_on)
    VALUES (v_ws, 'AGENTCHECK', 'PENDING', ARRAY[v_task]);

    UPDATE tasks SET status = 'RUNNING'   WHERE id = v_task;
    UPDATE tasks SET status = 'SUCCEEDED' WHERE id = v_task;

    IF NOT EXISTS (
        SELECT 1 FROM tasks
         WHERE task_type = 'AGENTCHECK' AND status = 'READY'
           AND depends_on @> ARRAY[v_task]
    ) THEN
        RAISE EXCEPTION '依赖推进失败：上游 SUCCEEDED 后下游仍非 READY';
    END IF;

    RAISE NOTICE 'ok  上游完成：下游自动转 READY';

    DELETE FROM tasks WHERE task_type = 'AGENTCHECK';

    -- ---------- 2b. 创建时推进（三种顺序）----------
    -- (a) 无依赖 → 创建即 READY。
    --     缺这条的话，队列里没有事件会推进它，任务永久 PENDING 且零报错。
    INSERT INTO tasks (workspace_id, task_type, status)
    VALUES (v_ws, 'AGENTCHECK', 'PENDING')
    RETURNING id INTO v_a;

    IF (SELECT status FROM tasks WHERE id = v_a) <> 'READY' THEN
        RAISE EXCEPTION '(a) 无依赖任务创建后未转 READY，实际=%',
            (SELECT status FROM tasks WHERE id = v_a);
    END IF;

    -- 让上游先跑完
    INSERT INTO tasks (workspace_id, task_type, status)
    VALUES (v_ws, 'AGENTCHECK', 'READY')
    RETURNING id INTO v_b;
    UPDATE tasks SET status = 'RUNNING'   WHERE id = v_b;
    UPDATE tasks SET status = 'SUCCEEDED' WHERE id = v_b;

    -- (b) 上游**早已** SUCCEEDED，之后才创建下游。
    --     这是原实现漏掉的核心路径：上游不会再成功一次，没有事件能推进下游。
    INSERT INTO tasks (workspace_id, task_type, status, depends_on)
    VALUES (v_ws, 'AGENTCHECK', 'PENDING', ARRAY[v_b])
    RETURNING id INTO v_a;

    IF (SELECT status FROM tasks WHERE id = v_a) <> 'READY' THEN
        RAISE EXCEPTION '(b) 依赖已完成任务的新任务未转 READY，实际=%',
            (SELECT status FROM tasks WHERE id = v_a);
    END IF;

    -- (c) 上游未完成 → 必须留在 PENDING，不能抢跑
    INSERT INTO tasks (workspace_id, task_type, status)
    VALUES (v_ws, 'AGENTCHECK', 'READY')
    RETURNING id INTO v_b;

    INSERT INTO tasks (workspace_id, task_type, status, depends_on)
    VALUES (v_ws, 'AGENTCHECK', 'PENDING', ARRAY[v_b])
    RETURNING id INTO v_a;

    IF (SELECT status FROM tasks WHERE id = v_a) <> 'PENDING' THEN
        RAISE EXCEPTION '(c) 依赖未满足的任务不应转 READY，实际=%',
            (SELECT status FROM tasks WHERE id = v_a);
    END IF;

    RAISE NOTICE 'ok  创建时推进：无依赖 / 依赖已完成 / 依赖未完成，三种顺序都对';

    DELETE FROM tasks WHERE task_type = 'AGENTCHECK';

    -- ---------- 2c. 自依赖必须被拒 ----------
    BEGIN
        INSERT INTO tasks (workspace_id, task_type, status, depends_on)
        VALUES (v_ws, 'AGENTCHECK', 'PENDING', ARRAY[gen_random_uuid()]);
        -- 上面是合法行（依赖不存在）；自依赖需要真实的 id，用 UPDATE 构造
        SELECT id INTO v_a FROM tasks WHERE task_type = 'AGENTCHECK' LIMIT 1;
        UPDATE tasks SET depends_on = ARRAY[v_a] WHERE id = v_a;
        RAISE EXCEPTION '自依赖未被拒绝：constraint tasks_no_self_dependency 失效';
    EXCEPTION
        WHEN check_violation THEN
            NULL;  -- 预期路径
    END;

    DELETE FROM tasks WHERE task_type = 'AGENTCHECK';
    RAISE NOTICE 'ok  自依赖：被 CHECK 拒绝';

    -- ---------- 3. 成本累加、step 归属、预算拦截 ----------
    INSERT INTO agent_runs
        (workspace_id, agent_name, goal, max_steps, max_tokens, max_cost)
    VALUES
        (v_ws, 'checker', 'verify budget rollup', 3, 1000, 1.0)
    RETURNING id INTO v_task;

    INSERT INTO llm_calls
        (workspace_id, run_id, model_provider, model_name, purpose, call_seq,
         input_tokens, output_tokens, cost)
    VALUES
        (v_ws, v_task, 'openai', 'gpt-x', 'generate', 1, 100, 50, 0.4);

    -- 触发器只累加 token 与金额；step_count 必须还是 0。
    -- 它是「决策循环数」，由 Runtime 原子递增，不等于调用次数。
    IF NOT EXISTS (
        SELECT 1 FROM agent_runs
         WHERE id = v_task AND step_count = 0
           AND total_tokens = 150 AND total_cost = 0.4
    ) THEN
        RAISE EXCEPTION '成本累加或 step 归属错误：期望 step=0 tokens=150 cost=0.4，实际=%',
            (SELECT format('step=%s tokens=%s cost=%s', step_count, total_tokens, total_cost)
               FROM agent_runs WHERE id = v_task);
    END IF;

    -- Runtime 的步数递增是条件 UPDATE：到上限之后必须 0 行影响
    UPDATE agent_runs SET step_count = step_count + 1
     WHERE id = v_task AND step_count < max_steps;
    IF (SELECT step_count FROM agent_runs WHERE id = v_task) <> 1 THEN
        RAISE EXCEPTION 'step_count 条件递增失效';
    END IF;

    -- 0.4 + 0.7 = 1.1 > 1.0，必须被 CHECK 拦下
    BEGIN
        INSERT INTO llm_calls
            (workspace_id, run_id, model_provider, model_name, purpose, call_seq,
             input_tokens, output_tokens, cost)
        VALUES
            (v_ws, v_task, 'openai', 'gpt-x', 'generate', 2, 10, 10, 0.7);
        RAISE EXCEPTION '预算拦截失效：超 max_cost 的调用被允许写入';
    EXCEPTION WHEN check_violation THEN
        NULL;  -- 预期路径
    END;

    -- 预留也占额度：0.4 已花 + 0.7 预留 = 1.1 > 1.0。
    -- 缺这条的话「先占后花」只是文档里的说法，运行时照样能把预算撑破。
    BEGIN
        UPDATE agent_runs SET reserved_cost = 0.7 WHERE id = v_task;
        RAISE EXCEPTION '预留未纳入预算：total_cost + reserved_cost 超限仍被允许';
    EXCEPTION WHEN check_violation THEN
        NULL;  -- 预期路径
    END;

    -- 租户绑定：把这次调用记到别的 workspace 上必须被拒。
    -- worker 是 BYPASSRLS，策略挡不住写错租户，只有复合外键能。
    BEGIN
        INSERT INTO llm_calls
            (workspace_id, run_id, model_provider, model_name, purpose, call_seq,
             input_tokens, output_tokens, cost)
        VALUES
            (gen_random_uuid(), v_task, 'openai', 'gpt-x', 'generate', 3, 1, 1, 0);
        RAISE EXCEPTION '跨租户写入未被拒绝：(run_id, workspace_id) 外键失效';
    EXCEPTION WHEN foreign_key_violation THEN
        NULL;  -- 预期路径
    END;

    RAISE NOTICE 'ok  预算：累加正确、step 不被触发器计数、金额与预留都拦得住、跨租户写入被拒';

    DELETE FROM agent_runs WHERE id = v_task;  -- llm_calls 级联删除

    -- ---------- 4. 租约 fencing ----------
    INSERT INTO tasks (workspace_id, task_type, status)
    VALUES (v_ws, 'AGENTCHECK', 'PENDING')
    RETURNING id INTO v_a;

    -- A 领取：代次 0 → 1
    UPDATE tasks
       SET status = 'RUNNING', locked_by = 'worker-A',
           lease_expires_at = now() + interval '90 seconds',
           lease_generation = lease_generation + 1
     WHERE id = v_a AND status = 'READY' AND lease_generation = 0;
    IF NOT FOUND THEN
        RAISE EXCEPTION '领取失败：代次未从 0 递增';
    END IF;

    -- 租约过期，被回收：代次 1 → 2，状态回 RETRYING
    UPDATE tasks
       SET status = 'RETRYING', locked_by = NULL, lease_expires_at = NULL,
           lease_generation = lease_generation + 1
     WHERE id = v_a AND status = 'RUNNING' AND lease_generation = 1;

    -- 退避到期后 B 接管：代次 2 → 3
    UPDATE tasks SET status = 'READY' WHERE id = v_a AND status = 'RETRYING';
    UPDATE tasks
       SET status = 'RUNNING', locked_by = 'worker-B',
           lease_expires_at = now() + interval '90 seconds',
           lease_generation = lease_generation + 1
     WHERE id = v_a AND status = 'READY' AND lease_generation = 2;

    -- A 恢复后用旧代次（1）写结果：必须 0 行
    UPDATE tasks
       SET status = 'SUCCEEDED', locked_by = NULL, lease_expires_at = NULL,
           completed_at = now()
     WHERE id = v_a
       AND lease_generation = 1
       AND locked_by = 'worker-A'
       AND status = 'RUNNING';
    IF FOUND THEN
        RAISE EXCEPTION 'fencing 失效：旧代次(1)覆盖了 B 正在执行的任务';
    END IF;

    -- B 用当前代次（3）写结果：必须成功
    UPDATE tasks
       SET status = 'SUCCEEDED', locked_by = NULL, lease_expires_at = NULL,
           completed_at = now()
     WHERE id = v_a
       AND lease_generation = 3
       AND locked_by = 'worker-B'
       AND status = 'RUNNING';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'fencing 误伤：当前代次(3)的合法写入被拒绝';
    END IF;

    RAISE NOTICE 'ok  租约 fencing：旧代次写入落空，当前代次可写';

    DELETE FROM tasks WHERE task_type = 'AGENTCHECK';

    IF EXISTS (SELECT 1 FROM tasks WHERE task_type = 'AGENTCHECK') THEN
        RAISE EXCEPTION '清理不彻底，仍残留 AGENTCHECK 任务';
    END IF;
END
$$;

\echo 'agent_check: all passed'
