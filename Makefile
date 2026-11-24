SHELL := /bin/bash
COMPOSE := docker compose
.DEFAULT_GOAL := help

.PHONY: help up build down stop restart logs logs-all ps sh db redis \
        migrate approle dbsmoke rlscheck ormcheck revision downgrade test lint fmt typecheck \
        tools health reset

help: ## 显示所有可用命令
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

up: ## 启动全部服务（后台）
	$(COMPOSE) up -d
	@echo "API   -> http://localhost:$${API_PORT:-8000}/docs"
	@echo "Admin -> make tools  (http://localhost:8080)"

build: ## 重新构建 api 镜像（改了 pyproject.toml 后必须执行）
	$(COMPOSE) build --no-cache api

down: ## 停止并移除容器（保留数据卷）
	$(COMPOSE) down

stop: ## 仅停止容器
	$(COMPOSE) stop

restart: down up ## 重启

logs: ## 跟踪 api 日志
	$(COMPOSE) logs -f api

logs-all: ## 跟踪全部服务日志
	$(COMPOSE) logs -f

ps: ## 查看服务状态
	$(COMPOSE) ps

sh: ## 进入 api 容器
	$(COMPOSE) exec api bash

db: ## 进入 psql
	$(COMPOSE) exec db psql -U $${POSTGRES_USER:-xing} -d $${POSTGRES_DB:-xing}

redis: ## 进入 redis-cli
	$(COMPOSE) exec redis redis-cli

migrate: ## 应用迁移到最新
	$(COMPOSE) exec api alembic upgrade head

approle: ## 创建/修复 xing_app 应用角色并授权（RLS 前提）
	$(COMPOSE) exec -T db psql -U $${POSTGRES_USER:-xing} -d $${POSTGRES_DB:-xing} \
	  -v ON_ERROR_STOP=1 -f - < scripts/app_role.sql

dbsmoke: ## 数据库回归：全链路冒烟 + P0 约束（自动回滚）
	@for f in tests/sql/smoke_v01.sql tests/sql/p0_check.sql; do \
	  echo "== $$f =="; \
	  $(COMPOSE) exec -T db psql -U $${POSTGRES_USER:-xing} -d $${POSTGRES_DB:-xing} \
	    -v ON_ERROR_STOP=1 -f - < $$f || exit 1; \
	done

rlscheck: ## 以 xing_app 角色验证 RLS 隔离（自动回滚）
	$(COMPOSE) exec -T -e PGPASSWORD=$${APP_DB_PASSWORD:-xing_app_dev_pwd} db psql \
	  -U xing_app -h 127.0.0.1 -d $${POSTGRES_DB:-xing} \
	  -v ON_ERROR_STOP=1 -f - < tests/sql/rls_check.sql

ormcheck: ## 校验 ORM 模型与数据库是否一致（空 diff 才算通过）
	$(COMPOSE) exec -T api alembic check

revision: ## 生成迁移，用法：make revision m="add topics table"
	$(COMPOSE) exec api alembic revision --autogenerate -m "$(m)"

downgrade: ## 回滚一个版本
	$(COMPOSE) exec api alembic downgrade -1

test: ## 运行测试
	$(COMPOSE) exec api pytest -q

lint: ## 静态检查
	$(COMPOSE) exec api ruff check src tests

fmt: ## 格式化
	$(COMPOSE) exec api ruff format src tests

typecheck: ## 类型检查
	$(COMPOSE) exec api mypy src

tools: ## 启动 Adminer 数据库客户端
	$(COMPOSE) --profile tools up -d adminer
	@echo "Adminer -> http://localhost:$${ADMINER_PORT:-8080}"

health: ## 检查就绪状态
	@curl -sS http://localhost:$${API_PORT:-8000}/readyz

reset: ## 清空数据库与缓存（危险，需输入 yes 确认）
	@echo "即将删除数据卷 xing-pgdata / xing-redisdata，数据不可恢复。"
	@read -r -p "输入 yes 继续: " ans; [ "$$ans" = "yes" ] || { echo "已取消"; exit 1; }
	$(COMPOSE) down -v
	@echo "已清空。下次 make up 会重新初始化。"
