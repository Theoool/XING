# XING

AIXING 后端服务。本地开发环境基于 Docker Compose 一键拉起，生产镜像与开发镜像共用同一份 Dockerfile。

## 技术栈

| 层 | 选型 | 说明 |
|---|---|---|
| 运行时 | Python 3.12 + uv | 依赖由 `uv.lock` 锁定，构建可复现 |
| Web | FastAPI + Uvicorn | 开发态热重载，生产多 worker |
| 数据 | PostgreSQL 18 + pgvector 0.8 | 原生 `uuidv7()`，无需 uuid-ossp |
| 缓存 | Redis 7.4 | AOF 持久化 + LRU 淘汰，可兼做队列 |
| ORM | SQLAlchemy 2.x (async) + asyncpg | 全异步链路 |
| 迁移 | Alembic | 异步 env，启动时自动 upgrade |
| 日志 | structlog | 开发彩色，生产 JSON |

## 快速开始

```bash
cp .env.example .env          # 首次
docker compose up -d          # 首次会构建镜像，约 1-3 分钟
```

启动后：

- API 文档 <http://localhost:8000/docs>
- 就绪检查 <http://localhost:8000/readyz>
- 数据库客户端：`./dev.sh tools` → <http://localhost:8080>（服务器填 `db`，用户名 `xing`，密码 `xing_dev_pwd`，库 `xing`）

首次启动会自动执行 `alembic upgrade head`，建好 `set_updated_at()` 触发器函数。

## 服务与端口

| 服务 | 容器内 | 宿主机 | 说明 |
|---|---|---|---|
| api | 8000 | `${API_PORT}` | FastAPI |
| db | 5432 | `${POSTGRES_PORT}` | PostgreSQL 18 |
| redis | 6379 | `${REDIS_PORT}` | Redis 7.4 |
| adminer | 8080 | `${ADMINER_PORT}` | 仅 `--profile tools` |

容器之间走服务名互访：`db:5432`、`redis:6379`。`.env` 里的端口变量只影响宿主机映射。

## 目录结构

```
.
├── docker-compose.yml
├── Makefile / dev.sh           # 命令入口，二选一
├── docker/
│   ├── api/Dockerfile          # dev / prod 两个 target
│   ├── api/entrypoint.sh       # 迁移 + 启动
│   └── postgres/init/          # 首次建库执行的 SQL
├── src/xing/
│   ├── main.py                 # 应用工厂与 lifespan
│   ├── core/                   # 配置、日志、错误契约、dev 身份
│   ├── db/                     # 引擎、会话（含 RLS 上下文注入）
│   ├── cache/                  # Redis 连接池
│   ├── api/
│   │   ├── deps.py             # 当前用户 + 带 RLS 上下文的会话
│   │   ├── pagination.py       # 键集分页
│   │   ├── routes/health.py    # 探针
│   │   └── v1/                 # 业务路由（workspaces/strategies/...）
│   ├── schemas/                # Pydantic 请求/响应模型
│   ├── models/                 # 19 张表的 ORM 映射（唯一 schema 真相）
│   └── services/               # 业务逻辑（全部 ORM，无手写 SQL）
├── alembic/
│   ├── sql/v01_schema.sql      # v0.1 DDL 正文
│   └── versions/               # 迁移（0003 = P0 修复，0004 = 选题唯一性）
├── scripts/app_role.sql        # 应用角色与授权（RLS 前提）
├── docker/postgres/init/       # 首次建库：扩展、参数、应用角色
└── tests/
    ├── sql/                    # smoke_v01 / p0_check / rls_check
    ├── test_health.py
    ├── test_api_flow.py        # 业务链 + 租户隔离 + 错误契约
    └── test_rls_context.py     # RLS 上下文注入回归
```

## 常用命令

有两种等价入口，按环境选一种：

```bash
make help          # 装了 make（macOS / Linux / WSL）
./dev.sh help      # Windows + Git Bash，或不装 make
```

| 操作 | make | dev.sh |
|---|---|---|
| 启动 | `make up` | `./dev.sh up` |
| 跟踪日志 | `make logs` | `./dev.sh logs` |
| 进容器 | `make sh` | `./dev.sh sh` |
| 进 psql | `make db` | `./dev.sh db` |
| 跑测试 | `make test` | `./dev.sh test` |
| 格式化 | `make fmt` | `./dev.sh fmt` |
| 静态检查 | `make lint` | `./dev.sh lint` |
| 生成迁移 | `make revision m="..."` | `./dev.sh revision "..."` |
| 修复应用角色 | `make approle` | `./dev.sh approle` |
| 数据库回归 | `make dbsmoke` | `./dev.sh dbsmoke` |
| RLS 隔离检查 | `make rlscheck` | `./dev.sh rlscheck` |
| ORM 模型校验 | `make ormcheck` | `./dev.sh ormcheck` |
| 起 Adminer | `make tools` | `./dev.sh tools` |
| 清空数据卷 | `make reset` | `./dev.sh reset` |

两者都只是 `docker compose` 的薄封装，任何命令都可以直接手敲，例如 `docker compose exec api pytest -q`。

## 开发流程

**改业务代码**：直接改 `src/`，Uvicorn 热重载，无需重启容器。

**加依赖**：改 `pyproject.toml` → 本机 `uv lock` → `./dev.sh build`。锁文件不一致时镜像构建会主动失败（`--frozen`），这是刻意的。

**加表**：

```bash
# 1. 先改模型（src/xing/models/），再生成迁移
./dev.sh revision "add xxx table"        # 或 make revision m="add xxx table"
# 2. 检查生成的 diff 是否只包含你这次的改动
# 3. 应用
./dev.sh migrate
# 4. 校验模型与库已一致（空 diff 才算通过）
./dev.sh ormcheck
```

`alembic/` 目录是挂载进容器的，容器内生成的迁移文件会直接出现在宿主机上。

> v0.1 的建表语句初始化在 `alembic/sql/v01_schema.sql`（raw SQL），但**模型已经与库完全对齐**（`./dev.sh ormcheck` 空 diff）。所以从现在起 `alembic revision --autogenerate` 可以用：它只会生成你真正改动的那部分。
>
> 前提是模型里保留了几处刻意的「迁就库侧」写法 —— 约束名/索引名写死、`Computed` 生成列、部分唯一索引的 `postgresql_where`。改这些之前先跑一次 `ormcheck`，确认改完仍为空 diff。

## API

开发阶段**不做登录**，但租户隔离是真的在跑。当前用户由 `api/deps.py::get_current_user_id` 给出：

| 场景 | 行为 |
|---|---|
| 不带任何 header | 使用固定 dev 用户（`dev@xing.local`，自动创建，并拥有一个 `XING Dev` 工作区） |
| 带 `X-User-ID: <uuid>` | 切换到该用户，用于验证隔离（用户需先用 `POST /api/v1/dev/users` 建出来） |
| `APP_ENV` 非 development | 直接 401，不会静默放行 |

接认证时只替换这一个函数（改成解 JWT + 校验成员关系），业务路由、服务层、RLS 注入都不用动。

**租户来自路径，不来自 header**：所有业务接口都是 `/api/v1/workspaces/{workspace_id}/...`。这样「header 说是 A、路径是 B」这类越权问题在结构上就不存在。

### 已实现（v0.1 第一条业务链）

```
workspace → strategy(+v1) → 新增版本 → activate
      → events/ingest（幂等）
      → topics/recommend（打分排序）
      → topics/{id}/select（自动落一条绑定当前战略版本的决策）
      → contents（+versions）→ approve / reject
      → tasks（PENDING，等 worker）
```

共 39 个端点，全部可在 <http://localhost:8000/docs> 直接试。
逐条说明（参数、响应字段、错误码、状态机、端到端示例）见 **[docs/API.md](docs/API.md)**。

### 五个刻意的设计决定

1. **错误用真实状态码 + `application/problem+json`**。不返回 200 + `{"error": ...}`，否则网关、重试、缓存、生成的前端客户端全部失效。数据库约束冲突映射成 409/422/403，不是 500。
2. **列表用键集分页**（`created_at, id`），没有 `total`。大表上 `COUNT(*)` 是全表扫，需要总数请单独出统计接口。
3. **决策的 `strategy_version_id` 不接受客户端传**，由 `app.current_strategy_version()` 解析当前生效版本。否则客户端可以永远传 v1，审计链就断了。
4. **审批只写 `approvals`**，`contents.status` 由触发器推导。已通过的内容新增版本会自动回到 `REVIEW`——审批是针对某一版的。
5. **`recommend` 是幂等的**：重复调用不会重复建选题，只重算分数。靠 `topics(workspace_id, primary_event_id)` 的部分唯一索引兜底并发。

### 几个已知缺口

- `tasks` 只会停在 `PENDING`：没有 worker，`cancel` 也只改数据库状态。
- `recommend` 的打分是规则式（战略关键词命中率 / 24h 衰减 / 素材完备度），不含 LLM；`risk_score` 恒为 NULL，留给 AI Runtime。
- `dev/users` 接口只在 development 下可用，它没有鉴权，不能带进生产。
- **ORM 已全覆盖**：19 张表全部有映射（`src/xing/models/`），服务层与路由里**没有手写 SQL**，
  全部走 `select()` / `update()` / `pg_insert()`。唯一保留的 `text()` 是 RLS 上下文注入
  （`set_config`）和健康检查的 `select 1`。
  `./dev.sh ormcheck`（`alembic check`）是漂移防线：模型改了但没写迁移时它会报出待执行的 upgrade。

## 数据库

v0.1 schema 共 **19 张表**，迁移 `0002_v01_schema`，DDL 正文在 `alembic/sql/v01_schema.sql`，可直接审阅和 diff。

`0003_p0_fixes` 是它的修正补丁，只加约束不加表：

| 修复 | 内容 |
|---|---|
| 战略唯一 | `strategies` 一个 workspace 只允许一条 `ACTIVE`（部分唯一索引） |
| 版本解析 | `app.current_strategy_version(strategy_id)` 按 `current_version_number` 返回版本 id |
| 审批留痕 | `approvals` 的唯一键从「每阶段一条」改为「每阶段一条 `PENDING`」，驳回后重审不再覆盖历史 |
| 单写入口 | `approvals` 驱动 `contents.status` 单向同步，未审批直接写 `APPROVED` 会被拒绝 |
| 邮箱唯一 | `UNIQUE(email)` 改为 `UNIQUE(lower(email))`，`A@x.com` 与 `a@x.com` 不再算两个账号 |

| 域 | 表 |
|---|---|
| 身份 | `users` `workspaces` `workspace_members` |
| 战略 | `strategies` `strategy_versions` |
| 事件与选题 | `external_events` `topics` `topic_events` |
| 决策 | `decisions` `decision_traces` |
| 内容 | `contents` `content_versions` |
| 生产与发布 | `tasks` `productions` `publications` |
| 效果与反馈 | `performance_snapshots` `feedback` `approvals` |
| AI 运行时 | `agent_runs` |

### 验证

```bash
./dev.sh dbsmoke     # 全链路插入 + 约束/触发器断言 + P0 回归，自动回滚
./dev.sh rlscheck    # 以 xing_app 角色验证租户隔离，自动回滚
./dev.sh test        # pytest，含 RLS 上下文注入回归
```

### RLS 是怎么真正生效的

16 张业务表开了 RLS，但 RLS 对**超级用户和表 owner 一律不生效** —— 「开了」不等于「生效」。三层保证缺一不可：

**1. 连接身份。** `DATABASE_URL` 指向 `xing_app`（NOSUPERUSER / NOBYPASSRLS / 非表 owner），`DATABASE_ADMIN_URL` 只在迁移时使用。新建数据卷时 `docker/postgres/init/02-app-role.sql` 自动建角色；已有数据卷执行 `./dev.sh approle`。

**2. 启动守卫。** entrypoint 在迁移后实际连一次库，读 `current_user / rolsuper / rolbypassrls`，任一为真直接拒绝启动。带着假隔离跑，比起不来更危险。确需跳过设 `RLS_GUARD=0`。

**3. 事务级注入。** 上下文不能在路由里 `SET LOCAL` 一次就完事：它的生命周期是当前事务，service 里 `commit` 之后，同一请求的下一条查询落在新事务，GUC 为空，RLS 把结果全部过滤掉。现象是「创建返回 201，紧接着列表返回空」，而且只在 commit 之后复现。正确做法是挂在 `SessionEvents.after_begin`（`src/xing/db/session.py`），每个事务自动重设；`is_local=true` 保证连接归还池子时不串租户。

同一份数据、commit 前后各查一次的实测对比：

```
旧写法（依赖里 SET LOCAL 一次）: 1 行 -> 0 行
新写法（after_begin 注入）      : 1 行 -> 1 行
```

业务代码只需要：

```python
await bind_rls(session, user_id=..., workspace_id=...)
```

### 已知取舍

- `contents.status` 到 `APPROVED` 只能由 `approvals` 驱动（守卫触发器强制）。代价是导入历史数据时必须先补审批记录，不能直接写状态。
- `workspaces` / `workspace_members` 未开 RLS（开了会自引用）。隔离完全依赖 `workspace_service` 里每条 SQL 都带 `user_id` 成员过滤——改这几个接口时不能去掉这个条件。
- `updated_at` 触发器用 `now()`（事务时间），同一事务内多次更新不会刷新；跨事务正常。
- 邮箱唯一性由 `lower(email)` 索引保证，但**不会自动改写写入值**：应用层注册/登录时仍需统一转小写，否则查不到自己刚建的账号。

## 环境要点

**PG18 数据目录变了。** 从 18 起 `PGDATA` 是 `/var/lib/postgresql/18/docker`，所以卷挂在父目录 `/var/lib/postgresql`。这样以后用 `pg_upgrade --link` 升大版本时，新旧目录能在同一个卷里共存。挂 `/var/lib/postgresql/data` 会初始化出一个空库。

**uv 的 venv 在 `/opt/venv`。** 源码目录 `./src` 挂载进容器时不会覆盖依赖。若把 venv 放在 `/app/.venv`，挂载宿主目录后依赖就丢了。

**主键用 `uuidv7()`。** PG18 原生支持，时间有序，比 uuidv4 的随机插入对 B-tree 友好：

```sql
create table demo (
  id uuid primary key default uuidv7(),
  created_at timestamptz not null default now()
);
```

**向量列要带维度。** 维度必须与 embedding 模型一致，且换模型时不能直接改列类型：

```sql
-- text-embedding-3-small = 1536 维
alter table document_chunks add column embedding vector(1536);
create index on document_chunks using hnsw (embedding vector_cosine_ops);
```

注意 `ivfflat` 索引上限 2000 维，3072 维的模型（如 `text-embedding-3-large`）只能用 HNSW 或 halfvec。

## 排错

| 现象 | 原因 / 处理 |
|---|---|
| 改代码不热重载 | Windows 下 bind mount 的文件事件可能丢失。本项目已装 `watchfiles` 并指定 `--reload-dir /app/src`；仍无效时 `./dev.sh restart` |
| 构建卡在拉取镜像 | registry 网关抖动，重试即可；也可在 Docker Desktop 里配置镜像加速 |
| 依赖下载慢 | 在 Dockerfile 的 `pip install uv` 前加 `ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`，或用 `uv sync --index-url` |
| 端口被占用 | 改 `.env` 里的 `*_PORT`，不要改容器内端口 |
| 启动报 `拒绝启动：应用连接会绕过 RLS` | `DATABASE_URL` 指向了超级用户。执行 `./dev.sh approle` 并确认 `DATABASE_URL` 用的是 `xing_app` |
| 启动报 `无法用 DATABASE_URL 连接数据库` | 老数据卷没有 `xing_app` 角色。`./dev.sh approle` 创建后重启 |
| 接口返回 201 但列表查不到刚建的数据 | RLS 上下文在 commit 后丢了。上下文必须由 `after_begin` 注入，不能只在依赖里 `SET LOCAL` 一次 |
| 想彻底重来 | `./dev.sh reset` 后 `./dev.sh up` |

## 上线

```bash
docker build -f docker/api/Dockerfile --target prod -t xing-api:0.1.0 .
```

prod target 使用非 root 用户、不装开发依赖、不带热重载。别忘了把 `APP_ENV=production`、`APP_DEBUG=false`、`APP_SECRET_KEY` 换成真实值，并且数据库密码不要沿用默认值。
