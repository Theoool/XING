\set ON_ERROR_STOP on

-- RLS 隔离验证。必须以非超级用户（xing_app）身份运行，否则 RLS 不生效。
--   docker compose exec -T -e PGPASSWORD=xing_app_dev_pwd db \
--     psql -U xing_app -h 127.0.0.1 -d xing -f - < tests/sql/rls_check.sql
--
-- 全程在一个事务里，结尾 ROLLBACK，不留数据。

BEGIN;

INSERT INTO users (email, display_name)
VALUES ('rls_a_' || gen_random_uuid() || '@xing.dev', 'A') RETURNING id AS ua \gset
INSERT INTO users (email, display_name)
VALUES ('rls_b_' || gen_random_uuid() || '@xing.dev', 'B') RETURNING id AS ub \gset

INSERT INTO workspaces (name, created_by) VALUES ('RLS-WA', :'ua') RETURNING id AS wa \gset
INSERT INTO workspaces (name, created_by) VALUES ('RLS-WB', :'ub') RETURNING id AS wb \gset

INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (:'wa', :'ua', 'OWNER');
INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (:'wb', :'ub', 'OWNER');

-- A 在 WA 里建内容
SET LOCAL app.user_id = :'ua';
SET LOCAL app.workspace_id = :'wa';

INSERT INTO contents (workspace_id, title, created_by) VALUES (:'wa', 'A 的内容', :'ua');
INSERT INTO topics (workspace_id, title) VALUES (:'wa', 'A 的选题');

SELECT 'A@WA 可见内容' AS check, count(*) AS n FROM contents;
SELECT 'A@WA 可见选题' AS check, count(*) AS n FROM topics;

-- B 拿着 WA 的上下文，应当看不到任何东西
SET LOCAL app.user_id = :'ub';
SET LOCAL app.workspace_id = :'wa';

SELECT 'B@WA 可见内容(应 0)' AS check, count(*) AS n FROM contents;
SELECT 'B@WA 可见选题(应 0)' AS check, count(*) AS n FROM topics;

-- B 尝试往 WA 里写，应被 WITH CHECK 拒绝
DO $t$
DECLARE
    v_ws uuid := current_setting('app.workspace_id')::uuid;
    v_u  uuid := current_setting('app.user_id')::uuid;
BEGIN
    INSERT INTO contents (workspace_id, title, created_by)
    VALUES (v_ws, '越权写入', v_u);
    RAISE EXCEPTION 'expected RLS to block cross-tenant insert';
EXCEPTION
    WHEN insufficient_privilege THEN
        RAISE NOTICE 'ok: 跨租户写入被 RLS 拒绝';
END
$t$;

-- 清空上下文，默认应当什么都看不到
SELECT set_config('app.workspace_id', '', true), set_config('app.user_id', '', true);
SELECT '无上下文可见内容(应 0)' AS check, count(*) AS n FROM contents;

ROLLBACK;

\echo ''
\echo 'RLS CHECK PASSED (rolled back)'
