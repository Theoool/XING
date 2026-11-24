-- 只在数据卷首次初始化时执行。
-- PG18 已原生提供 uuidv7()，不再需要 uuid-ossp 扩展。

CREATE EXTENSION IF NOT EXISTS vector;            -- 向量检索（document_chunks.embedding）
CREATE EXTENSION IF NOT EXISTS pg_trgm;           -- 模糊匹配 / 中文标题检索
CREATE EXTENSION IF NOT EXISTS btree_gin;         -- JSONB 与标量组合索引
CREATE EXTENSION IF NOT EXISTS pg_stat_statements; -- 慢查询归因，需 shared_preload_libraries
