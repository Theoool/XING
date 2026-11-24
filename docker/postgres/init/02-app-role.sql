-- 首次初始化数据卷时创建应用角色。
--
-- 只有在空数据目录上才会执行（docker-entrypoint-initdb.d 的语义）。
-- 已有数据卷请手动执行：./dev.sh approle
--
-- 密码与 .env 里的 APP_DB_PASSWORD / DATABASE_URL 必须一致。
-- 生产环境请改掉这里的密码，并且不要把它写进镜像。

\set ON_ERROR_STOP on

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'xing_app') THEN
        CREATE ROLE xing_app
            LOGIN
            PASSWORD 'xing_app_dev_pwd'
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
        RAISE NOTICE 'role xing_app created';
    END IF;
END
$$;

DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO xing_app', current_database());
END
$$;

-- app schema 由迁移创建；先建出来，让下面的授权有落点（迁移里是 IF NOT EXISTS）
CREATE SCHEMA IF NOT EXISTS app;

GRANT USAGE ON SCHEMA public, app TO xing_app;

-- 迁移由 POSTGRES_USER（= 表 owner）执行，所以默认权限要挂在它名下，
-- 以后每张新表、每个新函数都会自动带上，不必每次迁移后补授权。
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO xing_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT EXECUTE ON FUNCTIONS TO xing_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA app
    GRANT EXECUTE ON FUNCTIONS TO xing_app;
