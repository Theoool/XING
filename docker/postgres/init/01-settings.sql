-- 数据库级默认参数。挂在 database 上而非实例上，避免影响同实例的其他库。
DO $$
DECLARE
    db name := current_database();
BEGIN
    EXECUTE format('ALTER DATABASE %I SET timezone = %L', db, 'Asia/Shanghai');
    EXECUTE format('ALTER DATABASE %I SET statement_timeout = %L', db, '30s');
    -- 异步框架最容易出的问题是连接拿到事务后忘了提交，把锁一直占着
    EXECUTE format('ALTER DATABASE %I SET idle_in_transaction_session_timeout = %L', db, '60s');
    EXECUTE format('ALTER DATABASE %I SET lock_timeout = %L', db, '10s');
END
$$;
