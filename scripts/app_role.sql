-- 应用连接角色
--
-- 为什么必须单独建角色：
--   PostgreSQL 的 RLS 对超级用户和表 owner 一律不生效。
--   用 docker-compose 里的 POSTGRES_USER（= 超级用户 + 所有表 owner）连接，
--   前面那 16 条 policy 等于没写。应用必须换成这个角色。
--
-- 执行一次即可，可重复运行：
--   docker compose exec -T db psql -U xing -d xing -f - < scripts/app_role.sql
--
-- 生产环境请改掉下面的密码，并同步更新 .env 里的 DATABASE_URL。

\set ON_ERROR_STOP on

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'xing_app') THEN
        CREATE ROLE xing_app
            LOGIN
            PASSWORD 'xing_app_dev_pwd'
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
        RAISE NOTICE 'role xing_app created';
    ELSE
        RAISE NOTICE 'role xing_app already exists';
    END IF;
END
$$;

DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO xing_app', current_database());
END
$$;

GRANT USAGE ON SCHEMA public, app TO xing_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO xing_app;

-- 没有这些 EXECUTE，所有带 RLS 的查询都会报
-- permission denied for function is_workspace_member
--
-- 不逐个列函数名：app schema 里以后还会加函数，漏一个就是一次线上故障。
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
        EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO xing_app', fn.signature);
    END LOOP;
END
$$;

-- 以后新建的表/函数自动带上权限，省得每次迁移后补授权
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO xing_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT EXECUTE ON FUNCTIONS TO xing_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA app
    GRANT EXECUTE ON FUNCTIONS TO xing_app;

-- 上面那条 ALL TABLES 是按「所有表都要能改」写的，但对**只增不改**的表
-- 它恰好把约束抹掉了。这个脚本是手动 bootstrap、可重复运行的，而迁移是
-- 另一个时机跑的：只要先跑过迁移再跑本脚本（开发环境里很常见），
-- 0012 里的 REVOKE 就会被这里的 GRANT 无声覆盖 —— append-only 退化成注释。
-- 所以在这里再收一次，让「谁都不能改轨迹」不依赖两个脚本的先后顺序。
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
         WHERE table_schema = 'public' AND table_name = 'retrieval_traces'
    ) THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON retrieval_traces FROM xing_app;
        RAISE NOTICE 'retrieval_traces: append-only 已强制（去掉 UPDATE/DELETE）';
    END IF;
END
$$;

\echo 'app role ready'
