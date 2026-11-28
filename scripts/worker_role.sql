-- 后台执行角色
--
-- 为什么 worker 不能复用 xing_app：
--   worker 的领取查询必须**跨租户**（SELECT ... FROM tasks WHERE status='READY'
--   ... FOR UPDATE SKIP LOCKED）。执行那一刻还不知道会领到哪个 workspace，
--   而 xing_app 的每条 policy 都要求 app.workspace_id 等于行上的 workspace_id。
--   用 xing_app 连接，worker 只能看到「当前上下文租户」的任务，队列直接失效。
--
-- 所以这是本项目唯一一处有意绕过 RLS 的角色。三条硬规则配套：
--   1. 不给 users / approvals / feedback 的权限 —— 跑任务不需要读审批与用户资料
--   2. worker 拿到任务后，task.workspace_id 是后续所有查询的唯一租户上下文，
--      必须显式传参（现有 service 签名都要求 workspace_id）
--   3. worker 不提供 HTTP 接口，只接受队列驱动，绕过 RLS 的能力不外露给请求侧
--
-- 详见 docs/AGENT_DESIGN.md §8.0。
--
-- 执行一次即可，可重复运行：
--   ./dev.sh workerrole
--
-- 生产环境请改掉密码，并把它写进 worker 进程的 DATABASE_URL。

\set ON_ERROR_STOP on

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'xing_worker') THEN
        CREATE ROLE xing_worker
            LOGIN
            PASSWORD 'xing_worker_dev_pwd'
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT BYPASSRLS;
        RAISE NOTICE 'role xing_worker created';
    ELSE
        -- 已存在则确保属性正确（幂等）
        ALTER ROLE xing_worker BYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE;
        RAISE NOTICE 'role xing_worker already exists (attributes enforced)';
    END IF;
END
$$;

DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO xing_worker', current_database());
END
$$;

GRANT USAGE ON SCHEMA public, app TO xing_worker;

-- 只给运行期真正需要的表。逐表列出而不是 GRANT ALL：
-- 「把权限列出来」这件事本身就是文档，漏一张会被下一行注释暴露。
GRANT SELECT, INSERT, UPDATE           ON tasks              TO xing_worker;
GRANT SELECT, INSERT, UPDATE           ON agent_runs         TO xing_worker;
GRANT SELECT, INSERT, UPDATE           ON llm_calls          TO xing_worker;
GRANT SELECT, INSERT, UPDATE           ON productions        TO xing_worker;
GRANT SELECT, INSERT, UPDATE           ON contents           TO xing_worker;
GRANT SELECT, INSERT, UPDATE           ON content_versions   TO xing_worker;
GRANT SELECT, INSERT, UPDATE           ON decisions          TO xing_worker;
GRANT SELECT, INSERT                   ON decision_traces    TO xing_worker;
GRANT SELECT, INSERT, UPDATE           ON external_events    TO xing_worker;
GRANT SELECT                           ON workspaces         TO xing_worker;
GRANT SELECT                           ON workspace_members  TO xing_worker;
GRANT SELECT                           ON strategies         TO xing_worker;
GRANT SELECT                           ON strategy_versions  TO xing_worker;
-- 选题需要写，不只是读：
--   expander 产出候选（INSERT CANDIDATE），scorer 回填四个分数并把前三个
--   标成 RECOMMENDED（UPDATE）。但没有 DELETE —— 选题只被 REJECTED/EXPIRED，
--   不删除，删了「这个题当年被否掉过」这条信息就没了。
GRANT SELECT, INSERT, UPDATE           ON topics             TO xing_worker;
-- 证据关系：深入阶段要把「哪条材料支撑哪个论点、原文是哪一句」写进来。
-- 仍然没有 DELETE —— 证据只增不删：一次深入用过的材料是审计的一部分，
-- 删掉之后「当时凭什么这么写」就答不出来了。重跑靠 (topic_id, event_id)
-- 主键做 upsert，不会因为不给 DELETE 而堆积重复行。
GRANT SELECT, INSERT, UPDATE           ON topic_events       TO xing_worker;
-- 舆论立场：展开阶段写。同样没有 DELETE —— 一次展开采集到哪些声音，
-- 是「这个切面凭什么成立」的原始依据，删掉就再也回答不了。
GRANT SELECT, INSERT, UPDATE           ON opinion_stances    TO xing_worker;
-- 候选 → 立场：有引用才有候选。只有 INSERT 与 SELECT，没有 UPDATE：
-- 引用关系是对「这个题建立在什么之上」的断言，改它等于改写历史，
-- 需要改就重新展开一批。
GRANT SELECT, INSERT                   ON topic_stances      TO xing_worker;
-- 审批：只能「举手」，不能「决定」。
--
-- Reviewer 执行完要留下一条 PENDING，否则「这篇内容在等谁」在库里没有表示，
-- 人也不知道该看什么。所以给 INSERT。但 select/insert 之外**一条都不给**：
-- 没有 UPDATE 就无法把 PENDING 改成 APPROVED，没有 DELETE 就无法抹掉待办。
-- 这比「靠代码不写 UPDATE」强，因为它是数据库层面的事实。
GRANT SELECT, INSERT                   ON approvals          TO xing_worker;

-- 刻意不授权：users（用户资料）、feedback、publications。
-- 需要时单独加，并在这里写清理由。

DO $$
DECLARE
    fn record;
BEGIN
    FOR fn IN
        SELECT p.oid::regprocedure AS signature
          FROM pg_proc p
          JOIN pg_namespace n ON n.oid = p.pronamespace
         WHERE n.nspname = 'app'
    LOOP
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO xing_worker', fn.signature);
    END LOOP;
END
$$;

\echo 'worker role ready (BYPASSRLS; 仅运行期表权限)'
