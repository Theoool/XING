#!/usr/bin/env bash
# 不依赖 make 的命令入口。Windows 用户在 Git Bash 中执行 ./dev.sh <命令>
set -euo pipefail
cd "$(dirname "$0")"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

COMPOSE=(docker compose)

usage() {
  cat <<'EOF'
用法: ./dev.sh <命令>

  up          启动全部服务
  worker      启动后台执行进程（profile: worker，默认不跑）
  workerlog   跟踪 worker 日志
  workerrestart 重启 worker（改过 src/ 后必须执行：它不热重载）
  build       重建 api 镜像（改了 pyproject.toml 后必须执行）
  down        停止并移除容器（保留数据卷）
  stop        仅停止容器
  restart     重启
  logs        跟踪 api 日志
  logs-all    跟踪全部服务日志
  ps          查看服务状态
  sh          进入 api 容器
  db          进入 psql
  redis       进入 redis-cli
  migrate     应用迁移到最新
  approle     创建/修复 xing_app 应用角色并授权（RLS 依赖它）
  workerrole  创建/修复 xing_worker 后台执行角色（唯一 BYPASSRLS 角色）
  dbsmoke     数据库回归：全链路冒烟 + P0 约束 + Agent 契约（均自动回滚）
  rlscheck    以 xing_app 角色验证 RLS 隔离（自动回滚）
  ormcheck    校验 ORM 模型与数据库是否一致（空 diff 才算通过）
  revision    生成迁移，例: ./dev.sh revision "add topics table"
  downgrade   回滚一个版本
  test        运行测试
  lint        静态检查
  fmt         格式化
  typecheck   类型检查
  tools       启动 Adminer 数据库客户端
  health      检查就绪状态
  reset       清空数据卷（需输入 yes 确认）
  help        显示本帮助
EOF
}

cmd="${1:-help}"
shift || true

case "$cmd" in
  up)        "${COMPOSE[@]}" up -d && echo "API -> http://localhost:${API_PORT:-8000}/docs" ;;
  build)     "${COMPOSE[@]}" build --no-cache api ;;
  down)      "${COMPOSE[@]}" down ;;
  stop)      "${COMPOSE[@]}" stop ;;
  restart)   "${COMPOSE[@]}" down && "${COMPOSE[@]}" up -d ;;
  logs)      "${COMPOSE[@]}" logs -f api ;;
  logs-all)  "${COMPOSE[@]}" logs -f ;;
  ps)        "${COMPOSE[@]}" ps ;;
  worker)    "${COMPOSE[@]}" --profile worker up -d worker ;;
  workerlog) "${COMPOSE[@]}" --profile worker logs -f worker ;;
  # worker 跑的是 `python -m xing.worker.main`，启动时把模块导入内存，改 src/ 对它
  # 无效。API 那边有 --reload，很容易让人以为「都热重载了」—— 这个子命令就是
  # 为了让那条假设不成立时有一个不需要记住的命令。
  workerrestart) "${COMPOSE[@]}" --profile worker restart worker ;;
  sh)        "${COMPOSE[@]}" exec api bash ;;
  db)        "${COMPOSE[@]}" exec db psql -U "${POSTGRES_USER:-xing}" -d "${POSTGRES_DB:-xing}" ;;
  redis)     "${COMPOSE[@]}" exec redis redis-cli ;;
  migrate)   "${COMPOSE[@]}" exec api alembic upgrade head ;;
  dbsmoke)
    for f in tests/sql/smoke_v01.sql tests/sql/p0_check.sql tests/sql/agent_check.sql; do
      echo "== ${f} =="
      "${COMPOSE[@]}" exec -T db psql \
        -U "${POSTGRES_USER:-xing}" -d "${POSTGRES_DB:-xing}" \
        -v ON_ERROR_STOP=1 -f - < "$f"
    done
    ;;
  approle)
    "${COMPOSE[@]}" exec -T db psql \
      -U "${POSTGRES_USER:-xing}" -d "${POSTGRES_DB:-xing}" \
      -v ON_ERROR_STOP=1 -f - < scripts/app_role.sql
    ;;
  workerrole)
    "${COMPOSE[@]}" exec -T db psql \
      -U "${POSTGRES_USER:-xing}" -d "${POSTGRES_DB:-xing}" \
      -v ON_ERROR_STOP=1 -f - < scripts/worker_role.sql
    ;;
  rlscheck)
    "${COMPOSE[@]}" exec -T -e PGPASSWORD="${APP_DB_PASSWORD:-xing_app_dev_pwd}" db psql \
      -U xing_app -h 127.0.0.1 -d "${POSTGRES_DB:-xing}" \
      -v ON_ERROR_STOP=1 -f - < tests/sql/rls_check.sql
    ;;
  ormcheck)
    # 模型与库不一致时 alembic check 会打印待执行的 upgrade 操作并非零退出。
    # 这是 ORM 化过程中唯一的漂移防线：模型改了但没写迁移，这里立刻会红。
    "${COMPOSE[@]}" exec -T api alembic check
    ;;
  revision)
    if [ $# -lt 1 ]; then
      echo '缺少迁移说明，例: ./dev.sh revision "add topics table"'
      exit 1
    fi
    "${COMPOSE[@]}" exec api alembic revision --autogenerate -m "$1"
    ;;
  downgrade) "${COMPOSE[@]}" exec api alembic downgrade -1 ;;
  test)      "${COMPOSE[@]}" exec api pytest -q ;;
  lint)      "${COMPOSE[@]}" exec api ruff check src tests ;;
  fmt)       "${COMPOSE[@]}" exec api ruff format src tests ;;
  typecheck) "${COMPOSE[@]}" exec api mypy src ;;
  tools)     "${COMPOSE[@]}" --profile tools up -d adminer && echo "Adminer -> http://localhost:${ADMINER_PORT:-8080}" ;;
  health)    curl -sS "http://localhost:${API_PORT:-8000}/readyz" && echo ;;
  reset)
    echo "即将删除数据卷 xing-pgdata / xing-redisdata，数据不可恢复。"
    read -r -p "输入 yes 继续: " ans
    if [ "$ans" != "yes" ]; then echo "已取消"; exit 1; fi
    "${COMPOSE[@]}" down -v
    ;;
  help|-h|--help) usage ;;
  *)
    echo "未知命令: $cmd"
    echo
    usage
    exit 1
    ;;
esac
