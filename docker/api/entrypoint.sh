#!/usr/bin/env bash
set -euo pipefail

if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
  echo "[entrypoint] applying database migrations -> head"
  # 迁移走 DATABASE_ADMIN_URL（表 owner），应用运行时走 DATABASE_URL（受限角色）
  alembic upgrade head
  echo "[entrypoint] migrations applied"
fi

# RLS 前置检查
#
# 这一步不是锦上添花：superuser 和表 owner 会整体绕过行级安全策略，
# 也就是说连接用错角色时，16 条 policy 全部静默失效 —— 应用看起来正常，
# 隔离实际为零。宁可起不来，也不要带着假隔离跑。
if [ "${RLS_GUARD:-1}" = "1" ]; then
  python - <<'PY'
import asyncio
import os
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

url = os.environ["DATABASE_URL"]


async def verify() -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "select current_user as role, r.rolsuper, r.rolbypassrls "
                        "  from pg_roles r where r.rolname = current_user"
                    )
                )
            ).one()
    finally:
        await engine.dispose()

    print(
        f"[entrypoint] app db role={row.role} "
        f"superuser={row.rolsuper} bypassrls={row.rolbypassrls}"
    )
    if row.rolsuper or row.rolbypassrls:
        print(
            "[entrypoint] 拒绝启动：应用连接会绕过 RLS，租户隔离只剩应用层。",
            file=sys.stderr,
        )
        print(
            "[entrypoint] 修复：./dev.sh approle 创建 xing_app，并确认 DATABASE_URL 指向它。",
            file=sys.stderr,
        )
        print(
            "[entrypoint] 确需关闭此检查：设置 RLS_GUARD=0",
            file=sys.stderr,
        )
        sys.exit(1)


try:
    asyncio.run(verify())
except Exception as exc:  # noqa: BLE001
    print(f"[entrypoint] 无法用 DATABASE_URL 连接数据库：{exc}", file=sys.stderr)
    print(
        "[entrypoint] 若是新数据卷，应用角色应已由 init/02-app-role.sql 创建；"
        "老数据卷请执行 ./dev.sh approle",
        file=sys.stderr,
    )
    sys.exit(1)
PY
fi

exec "$@"
