# XING API 参考（v0.1）

面向接入方的完整接口说明。**共 39 个端点**，全部可在 <http://localhost:8000/docs> 交互式试用，
机器可读定义在 <http://localhost:8000/openapi.json>。

本文档与代码同源，字段、状态码、错误码均按当前实现逐条核对。

---

## 目录

- [1. 基础约定](#1-基础约定)
- [2. 端点总览](#2-端点总览)
- [3. 系统与健康](#3-系统与健康)
- [4. 工作区 Workspaces](#4-工作区-workspaces)
- [5. 战略 Strategies](#5-战略-strategies)
- [6. 外部事件 Events](#6-外部事件-events)
- [7. 选题 Topics](#7-选题-topics)
- [8. 决策 Decisions](#8-决策-decisions)
- [9. 内容 Contents](#9-内容-contents)
- [10. 任务 Tasks](#10-任务-tasks)
- [11. 开发辅助 Dev](#11-开发辅助-dev)
- [12. 状态机总表](#12-状态机总表)
- [13. 端到端最小流程](#13-端到端最小流程)
- [14. 当前缺口](#14-当前缺口)

---

## 1. 基础约定

### 1.1 地址

| 环境 | Base URL |
|---|---|
| 本地开发 | `http://localhost:8000` |
| 容器内互访 | `http://api:8000` |

业务接口统一前缀 `/api/v1`。健康检查不在该前缀下。

### 1.2 认证（开发期）

当前**没有登录体系**，身份由 `api/deps.py::get_current_user_id` 决定：

| 请求情况 | 得到的身份 |
|---|---|
| 不带任何头 | 固定 dev 用户 `dev@xing.local`（首次请求时自动创建，并附带一个 `XING Dev` 工作区） |
| 带 `X-User-ID: <uuid>` | 该用户，用于验证租户隔离（需先通过 `POST /api/v1/dev/users` 创建） |
| `APP_ENV` 不是 `development` | 一律 `401 AUTH_REQUIRED`，不会静默放行 |

```
X-User-ID: 0193f2a1-7c4e-7a3b-9d21-0f5c8b6e4a12
```

接认证时只改这一个函数（换成解 JWT + 校验成员关系），业务路由、服务层、RLS 注入都不用动。

> **注意**：`X-User-ID` 不做任何校验，客户端可以声称是任何人。它只是开发期调试开关，
> 生产环境必须关掉（代码里已经用 `settings.is_dev` 硬拦）。

### 1.3 请求与响应

- 请求体 `Content-Type: application/json`，字段名 `snake_case`。
- 响应体也是 JSON，字段名 `snake_case`。
- **主键一律 UUIDv7**（时间有序，可直接当排序键）。
- **时间一律 ISO-8601 带时区**，例如 `2026-11-25T06:30:12.481903+00:00`。库内全部 `timestamptz`。
- 金额/分数字段：`numeric` 列在 JSON 里是数字（服务层已把 `Decimal` 转成 `float`）。
- 空值统一用 `null`，不用空字符串占位。

### 1.4 错误契约（RFC 9457）

所有错误返回 `Content-Type: application/problem+json`，结构固定：

```json
{
  "type": "https://xing.local/errors/topic_not_selectable",
  "title": "TOPIC_NOT_SELECTABLE",
  "status": 409,
  "detail": "选题当前状态为 SELECTED，不能再次决策",
  "code": "TOPIC_NOT_SELECTABLE"
}
```

校验类错误额外带 `details.fields`：

```json
{
  "type": "https://xing.local/errors/validation_error",
  "title": "VALIDATION_ERROR",
  "status": 422,
  "detail": "请求参数校验失败",
  "code": "VALIDATION_ERROR",
  "details": {
    "fields": [
      { "field": "priority", "message": "Input should be less than or equal to 100" }
    ]
  }
}
```

**设计原则：错误必须用真实 HTTP 状态码**，不返回 `200 + {"error": ...}`。否则网关重试、
监控告警、缓存、OpenAPI 生成的前端客户端全部失效。

#### 全部错误码

| HTTP | code | 触发场景 |
|---|---|---|
| 401 | `AUTH_REQUIRED` | 非开发环境未实现认证 |
| 403 | `WORKSPACE_FORBIDDEN` | RLS 拒绝写入（SQLSTATE 42501），当前用户不是该工作区成员 |
| 404 | `WORKSPACE_NOT_FOUND` | 工作区不存在**或**不是成员（刻意不区分） |
| 404 | `STRATEGY_NOT_FOUND` | 战略不存在或不属于该工作区 |
| 404 | `NO_ACTIVE_STRATEGY` | 查询当前生效战略时，一条 ACTIVE 都没有 |
| 404 | `EVENT_NOT_FOUND` | 事件不存在 |
| 404 | `TOPIC_NOT_FOUND` | 选题不存在 |
| 404 | `DECISION_NOT_FOUND` | 决策不存在 |
| 404 | `CONTENT_NOT_FOUND` | 内容不存在 |
| 404 | `TASK_NOT_FOUND` | 任务不存在 |
| 404 | `NOT_FOUND` | 非开发环境访问 `/dev/*` |
| 409 | `NO_ACTIVE_STRATEGY` | 写决策 / 选选题时需要解析战略版本，但没有 ACTIVE |
| 409 | `STRATEGY_ARCHIVED` | 对已归档战略新增版本或激活 |
| 409 | `TOPIC_NOT_SELECTABLE` | 选题状态不是 `CANDIDATE` / `RECOMMENDED` |
| 409 | `DECISION_NOT_PROPOSED` | 重复审批已结束的决策 |
| 409 | `CONTENT_ARCHIVED` | 对已归档内容新增版本或审核 |
| 409 | `CONTENT_ALREADY_APPROVED` | 重复通过同一条内容 |
| 409 | `TASK_NOT_CANCELLABLE` | 取消已结束的任务 |
| 409 | `CONFLICT` | 任意唯一约束冲突（SQLSTATE 23505），`details.constraint` 给出索引名 |
| 422 | `VALIDATION_ERROR` | 请求体 / 查询参数校验失败 |
| 422 | `INVALID_CURSOR` | 分页游标无法解码 |
| 422 | `FOREIGN_KEY_VIOLATION` | 引用了不存在的对象（SQLSTATE 23503） |
| 422 | `NOT_NULL_VIOLATION` | 缺必填字段（23502） |
| 422 | `CONSTRAINT_VIOLATION` | CHECK 约束失败（23514） |
| 422 | `INVALID_EMAIL` | `/dev/users` 邮箱格式不对 |
| 500 | `DATABASE_ERROR` | 未预期的数据库错误（`details.sqlstate` 便于排查，不泄露 SQL） |

同一个 code 在不同接口可能是不同状态码（`NO_ACTIVE_STRATEGY` 出现在 404 和 409），
因为语义不同：读「当前战略」时是资源不存在，写决策时是前置条件不满足。

### 1.5 分页

只有 **`GET /events`** 用键集分页，返回：

```json
{ "items": [ ... ], "next_cursor": "MjAyNi0xMS0yNVQwNjozMDoxMi4" }
```

| 参数 | 说明 |
|---|---|
| `limit` | 1–100，默认 20 |
| `cursor` | 上一页返回的 `next_cursor`，不透明字符串 |

规则：

- `next_cursor` 为 `null` 表示没有下一页。
- 游标编码的是 `(created_at, id)`，按 `created_at DESC, id DESC` 排序。
- **游标不要自己拼**，也不要长期缓存——它包含时间戳，跨数据集无意义。
- 没有 `total`。大表上 `COUNT(*)` 是全表扫，需要总数请单独出统计接口。

其余列表接口（topics / decisions / contents / tasks / strategies / versions / approvals）
用 `limit` 截断，`order by ... desc`，没有游标。

### 1.6 租户隔离

所有业务接口路径都带工作区：`/api/v1/workspaces/{workspace_id}/...`。

**租户 ID 只从路径取，不从 header 取**，这样「header 说 A、路径是 B」的越权在结构上不存在。

服务端每个请求都会把 `(user_id, workspace_id)` 写进数据库会话变量，由 RLS 策略强制过滤：

```sql
workspace_id = app.current_workspace_id() AND app.is_workspace_member(workspace_id)
```

实际表现：

| 操作 | 非成员的结果 |
|---|---|
| 列表查询 | `200` + 空数组（RLS 让 SELECT 返回 0 行） |
| 单条查询 | `404 xxx_NOT_FOUND` |
| 写入 | `403 WORKSPACE_FORBIDDEN` |

> 列表返回空数组而不是 403 是一致性上的已知取舍：RLS 在 SQL 层过滤，路由层不额外判权。
> 若前端需要「无权 vs 空」的区分，应在进入页面前调 `GET /workspaces/{id}` 探测。

**workspaces 与 workspace_members 两张表没有开 RLS**（它们本身是租户的源头），
所以工作区接口靠 service 层每条 SQL 显式带 `user_id` 成员过滤。

---

## 2. 端点总览

### 系统

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 存活探针 |
| GET | `/readyz` | 就绪探针（DB + Redis） |
| GET | `/api/v1/ping` | 版本回显 |

### 工作区（4）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/workspaces` | 我参与的工作区 |
| POST | `/api/v1/workspaces` | 创建工作区 |
| GET | `/api/v1/workspaces/{workspace_id}` | 工作区详情 |
| PATCH | `/api/v1/workspaces/{workspace_id}` | 更新工作区 |

### 战略（6）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/workspaces/{workspace_id}/strategies` | 战略列表 |
| POST | `/workspaces/{workspace_id}/strategies` | 创建战略（自动落 v1） |
| GET | `/workspaces/{workspace_id}/strategy` | 当前生效战略（**单数**） |
| GET | `/workspaces/{workspace_id}/strategies/{strategy_id}` | 战略详情 |
| POST | `/workspaces/{workspace_id}/strategies/{strategy_id}/versions` | 新增版本 |
| POST | `/workspaces/{workspace_id}/strategies/{strategy_id}/activate` | 激活 |

### 事件（3）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/workspaces/{workspace_id}/events/ingest` | 采集（幂等） |
| GET | `/workspaces/{workspace_id}/events` | 列表（键集分页） |
| GET | `/workspaces/{workspace_id}/events/{event_id}` | 详情 |

### 选题（5）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/workspaces/{workspace_id}/topics/recommend` | 推荐（打分排序，幂等） |
| GET | `/workspaces/{workspace_id}/topics` | 列表 |
| GET | `/workspaces/{workspace_id}/topics/{topic_id}` | 详情 |
| POST | `/workspaces/{workspace_id}/topics/{topic_id}/select` | 选定（自动写决策） |
| POST | `/workspaces/{workspace_id}/topics/{topic_id}/reject` | 拒绝（自动写决策） |

### 决策（5）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/workspaces/{workspace_id}/decisions` | 记录决策 |
| GET | `/workspaces/{workspace_id}/decisions` | 列表（含 traces） |
| GET | `/workspaces/{workspace_id}/decisions/{decision_id}` | 详情（含 traces） |
| POST | `/workspaces/{workspace_id}/decisions/{decision_id}/approve` | 批准 |
| POST | `/workspaces/{workspace_id}/decisions/{decision_id}/reject` | 否决 |

### 内容（8）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/workspaces/{workspace_id}/contents` | 创建内容（可同时落 v1） |
| GET | `/workspaces/{workspace_id}/contents` | 列表 |
| GET | `/workspaces/{workspace_id}/contents/{content_id}` | 详情（含当前版本） |
| GET | `/workspaces/{workspace_id}/contents/{content_id}/versions` | 版本历史 |
| POST | `/workspaces/{workspace_id}/contents/{content_id}/versions` | 新增版本 |
| POST | `/workspaces/{workspace_id}/contents/{content_id}/approve` | 审核通过 |
| POST | `/workspaces/{workspace_id}/contents/{content_id}/reject` | 审核驳回 |
| GET | `/workspaces/{workspace_id}/contents/{content_id}/approvals` | 审批历史 |

### 任务（4）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/workspaces/{workspace_id}/tasks` | 创建任务 |
| GET | `/workspaces/{workspace_id}/tasks` | 列表 |
| GET | `/workspaces/{workspace_id}/tasks/{task_id}` | 详情（轮询用） |
| POST | `/workspaces/{workspace_id}/tasks/{task_id}/cancel` | 取消（`?reason=`） |

### 开发辅助（1）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/dev/users` | 创建测试用户（仅 development） |

---

## 3. 系统与健康

### GET /healthz

存活探针，**不触碰任何外部依赖**，只证明进程还在。

```json
{ "status": "ok" }
```

用于容器的 liveness probe。数据库挂了它也应该返回 200，否则编排层会误杀容器。

### GET /readyz

就绪探针，逐个检查下游依赖。任一不可用返回 **503**，便于编排层摘流。

```json
// 200
{
  "status": "ok",
  "version": "0.1.0",
  "checks": { "postgres": "ok", "redis": "ok" }
}
```

```json
// 503
{
  "status": "degraded",
  "version": "0.1.0",
  "checks": {
    "postgres": "ok",
    "redis": "error: Error 111 connecting to redis:6379. Connection refused."
  }
}
```

### GET /api/v1/ping

```json
{ "message": "pong", "version": "0.1.0" }
```

---

## 4. 工作区 Workspaces

工作区是顶层租户边界。创建者自动成为 `OWNER`。

### GET /api/v1/workspaces

我参与的工作区列表，按创建时间倒序。**无分页**（一个人参与的工作区量级很小）。

**查询参数**

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `include_archived` | bool | `false` | 是否包含 `ARCHIVED` 状态 |

**响应 200**

```json
[
  {
    "id": "0193f2a1-7c4e-7a3b-9d21-0f5c8b6e4a12",
    "name": "XING Dev",
    "status": "ACTIVE",
    "created_by": "0193f2a0-1111-7c4e-8a3b-0f5c8b6e4a12",
    "created_at": "2026-11-25T06:30:12.481903+00:00",
    "updated_at": "2026-11-25T06:30:12.481903+00:00"
  }
]
```

`WorkspaceOut` 字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | uuid | 工作区 ID |
| `name` | string | 1–100 字符 |
| `status` | `ACTIVE` \| `ARCHIVED` | |
| `created_by` | uuid | 创建者 |
| `created_at` / `updated_at` | timestamp | |

### POST /api/v1/workspaces

创建工作区，并在同一事务里写入 `workspace_members(role='OWNER')`。

**请求体**

```json
{ "name": "品牌内容中心" }
```

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| `name` | string | 是 | 1–100 |

**响应 201**：`WorkspaceOut`

**错误**

| 状态 | code | 场景 |
|---|---|---|
| 422 | `VALIDATION_ERROR` | name 为空或超长 |

### GET /api/v1/workspaces/{workspace_id}

**响应 200**：`WorkspaceOut`

**错误**：`404 WORKSPACE_NOT_FOUND` —— 不存在与不是成员**共用同一个错误**，避免探测他人工作区是否存在。

### PATCH /api/v1/workspaces/{workspace_id}

部分更新。两个字段都可选；**都不传时原样返回，不报错**。

```json
{ "name": "改名后的工作区", "status": "ARCHIVED" }
```

| 字段 | 类型 | 约束 |
|---|---|---|
| `name` | string \| null | 1–100 |
| `status` | `ACTIVE` \| `ARCHIVED` \| null | |

**响应 200**：`WorkspaceOut`

**错误**：`404 WORKSPACE_NOT_FOUND`、`422 VALIDATION_ERROR`

---

## 5. 战略 Strategies

战略是**版本化对象**，所以没有「PUT 整条战略」：

- `strategies` 主表只存 `name / status / current_version_number / lock_version`。
- 真正的配置在 `strategy_versions.strategy_config`（JSONB）。
- 一个工作区**最多一条 `ACTIVE`**（部分唯一索引 `strategies_one_active_per_workspace` 保证）。

`workspace_id` 出现在响应里，因为战略可以被跨工作区引用比对（未来 Pattern 层会用到）。

### GET /workspaces/{workspace_id}/strategies

按 `created_at DESC` 返回该工作区全部战略，每个都带当前版本。无分页。

**响应 200**：`StrategyOut[]`

```json
[
  {
    "id": "0193f2b0-2222-7c4e-8a3b-0f5c8b6e4a12",
    "workspace_id": "0193f2a1-7c4e-7a3b-9d21-0f5c8b6e4a12",
    "name": "2026 Q4 内容战略",
    "status": "ACTIVE",
    "current_version_number": 2,
    "lock_version": 1,
    "created_at": "2026-11-20T02:11:00+00:00",
    "updated_at": "2026-11-24T09:03:00+00:00",
    "current_version": {
      "id": "0193f2b5-3333-7c4e-8a3b-0f5c8b6e4a12",
      "version_number": 2,
      "strategy_config": {
        "content_directions": ["AI 编程工具", "独立开发者"],
        "target_audience": "一线研发工程师",
        "tone": "克制、具体、有数据"
      },
      "change_summary": "补充独立开发者方向",
      "created_by": "0193f2a0-1111-7c4e-8a3b-0f5c8b6e4a12",
      "created_at": "2026-11-24T09:03:00+00:00"
    }
  }
]
```

`StrategyOut` / `StrategyVersionOut` 字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `status` | `DRAFT` \| `ACTIVE` \| `PAUSED` \| `ARCHIVED` | 新建默认 `DRAFT` |
| `current_version_number` | int | ≥1，指向 `strategy_versions.version_number` |
| `lock_version` | int | 每次新增版本 +1，供乐观锁 |
| `current_version` | object | 当前版本的完整配置 |
| `strategy_config` | object | 自由 JSONB，`recommend` 会把里面所有字符串当关键词 |
| `change_summary` | string \| null | 这一版改了什么 |

### POST /workspaces/{workspace_id}/strategies

创建战略，**同一事务内自动落 v1**，不会出现「有战略没版本」的中间态。

```json
{
  "name": "2026 Q4 内容战略",
  "strategy_config": {
    "content_directions": ["AI 编程工具"],
    "target_audience": "一线研发工程师"
  },
  "change_summary": "初版"
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | string | 是 | 1–100 |
| `strategy_config` | object | 否 | 默认 `{}` |
| `change_summary` | string | 否 | |

**响应 201**：`StrategyOut`（`status = DRAFT`，`current_version_number = 1`）

**错误**：`403 WORKSPACE_FORBIDDEN`、`422 VALIDATION_ERROR`

> 创建后**不会自动激活**。必须显式调 `activate`，否则 `recommend` 和写决策都会因
> `NO_ACTIVE_STRATEGY` 失败。这是刻意的——战略生效应该是一个有意识的动作。

### GET /workspaces/{workspace_id}/strategy

当前生效战略（注意路径是**单数** `strategy`）。

**响应 200**：`StrategyOut`

**错误**：`404 NO_ACTIVE_STRATEGY`

### GET /workspaces/{workspace_id}/strategies/{strategy_id}

**响应 200**：`StrategyOut`
**错误**：`404 STRATEGY_NOT_FOUND`

### POST /workspaces/{workspace_id}/strategies/{strategy_id}/versions

新增一个版本，并把 `current_version_number` 指向它、`lock_version + 1`。

```json
{
  "strategy_config": { "content_directions": ["AI 编程工具", "独立开发者"] },
  "change_summary": "补充独立开发者方向"
}
```

| 字段 | 类型 | 必填 |
|---|---|---|
| `strategy_config` | object | 是（整份覆盖，不是 merge） |
| `change_summary` | string | 否 |

**响应 201**：`StrategyOut`（`current_version_number` 已递增）

> **语义警告**：如果这条战略本来就是 `ACTIVE`，新增版本会**立即改变线上基准**——
> 不需要再调一次 `activate`，后续新决策马上绑定到新版本。系统没有「草稿版本」的概念，
> 也无法在保持 v2 生效的同时把 v3 先存着。需要 staged rollout 时，正确做法是
> 新建一条独立的战略（`DRAFT`），改好再 `activate`。

**错误**

| 状态 | code | 场景 |
|---|---|---|
| 404 | `STRATEGY_NOT_FOUND` | |
| 409 | `STRATEGY_ARCHIVED` | 已归档战略不能再加版本 |
| 403 | `WORKSPACE_FORBIDDEN` | |

**并发**：服务层先 `SELECT ... FOR UPDATE` 锁住 `strategies` 行再算 `next_version`，
两个并发请求不会拿到同一个版本号。

**重要**：`strategy_config` 是**整份替换**。想改一个字段，要先把现有配置读出来改完再整体提交。

### POST /workspaces/{workspace_id}/strategies/{strategy_id}/activate

把目标战略置为 `ACTIVE`，**同时把该工作区其他 `ACTIVE` 降级为 `PAUSED`**。

无请求体。

**响应 200**：`StrategyOut`

**错误**：`404 STRATEGY_NOT_FOUND`、`409 STRATEGY_ARCHIVED`、`403 WORKSPACE_FORBIDDEN`

**实现细节**：先降级其他、再激活目标。顺序反了会撞部分唯一索引。

---

## 6. 外部事件 Events

外部世界发生了什么。**事件不等于选题**：一个选题通常由多条事件支撑，关系在 `topic_events`。

`ingest` 是整个系统数据进入的**唯一入口**，也必须是幂等的——爬虫重试、上游重复推送都会打到这里。

### POST /workspaces/{workspace_id}/events/ingest

按 `(workspace_id, source_type, external_id)` upsert。

```json
{
  "source_type": "NEWS",
  "external_id": "reuters-2026-11-24-ai-coding-001",
  "title": "某厂商发布新一代 AI 编程助手",
  "summary": "支持跨文件重构，官方称补全准确率提升 40%",
  "url": "https://example.com/news/001",
  "source_published_at": "2026-11-24T10:00:00+00:00",
  "raw_payload": { "author": "Jane Doe", "section": "tech" }
}
```

| 字段 | 类型 | 必填 | 约束 / 说明 |
|---|---|---|---|
| `source_type` | enum | 是 | `NEWS` `SEARCH` `SOCIAL` `RSS` `API` `MANUAL` `OTHER` |
| `external_id` | string | 是 | 1–255，上游的稳定 ID（不要用自增序号） |
| `title` | string | 是 | ≥1 字符，无上限；转成选题时才截断到 500 |
| `summary` | string | 否 | |
| `url` | string | 否 | |
| `source_published_at` | timestamp | 否 | 上游发布时间；缺失时用 `discovered_at` 参与打分 |
| `raw_payload` | object | 否 | 原始报文，便于回溯 |

**响应**

| 状态 | 含义 |
|---|---|
| **201** | 新建（`created: true`） |
| **200** | 命中幂等键，更新了已有记录（`created: false`） |

```json
{
  "created": false,
  "event": {
    "id": "0193f2c0-4444-7c4e-8a3b-0f5c8b6e4a12",
    "source_type": "NEWS",
    "external_id": "reuters-2026-11-24-ai-coding-001",
    "title": "某厂商发布新一代 AI 编程助手",
    "summary": "支持跨文件重构，官方称补全准确率提升 40%",
    "url": "https://example.com/news/001",
    "source_published_at": "2026-11-24T10:00:00+00:00",
    "discovered_at": "2026-11-25T06:31:02.114+00:00"
  }
}
```

**更新语义（重要）**

| 字段 | 冲突时的行为 |
|---|---|
| `title` | **强制覆盖**（标题以最新一次投递为准） |
| `summary` / `url` / `source_published_at` / `raw_payload` | `COALESCE(新值, 旧值)` —— **只补空，不覆盖已有内容** |

也就是说：上游后续把 summary 改成空，库里原来的 summary 不会被清空。这是为了防
「上游偶发返回半成品报文」把已有数据打坏。

**调用方判断逻辑**：看 HTTP 状态码（201 新增 / 200 已存在），不要看 body 里的 `created`
也能判断，但两者一致。

**错误**：`403 WORKSPACE_FORBIDDEN`、`422 VALIDATION_ERROR`

### GET /workspaces/{workspace_id}/events

按 `created_at DESC, id DESC` 的**键集分页**。

**查询参数**

| 参数 | 类型 | 默认 | 约束 |
|---|---|---|---|
| `limit` | int | 20 | 1–100 |
| `cursor` | string | null | 上一页的 `next_cursor` |
| `source_type` | enum | null | 同 ingest 的枚举 |
| `q` | string | null | 对 `title` 和 `summary` 做 `ILIKE %q%` |

**响应 200**

```json
{
  "items": [ { "id": "...", "title": "...", "...": "..." } ],
  "next_cursor": "MjAyNi0xMS0yNVQwNjozMTowMi4xMTQrMDA6MDB8MDE5M2YyYzAtNDQ0NC03YzRlLTg"
}
```

`EventOut` 字段：`id, source_type, external_id, title, summary, url, source_published_at, discovered_at`

> 列表项**不含** `raw_payload`（可能很大）和 `created_at`（只用于游标）。

**错误**：`422 INVALID_CURSOR`（游标被篡改或来自别的环境）

**用法**

```
GET /events?limit=20
GET /events?limit=20&cursor=<next_cursor>
GET /events?source_type=NEWS&q=AI
```

### GET /workspaces/{workspace_id}/events/{event_id}

**响应 200**：`EventOut`
**错误**：`404 EVENT_NOT_FOUND`

---

## 7. 选题 Topics

选题是**事件 → 内容**的中间层。`recommend` 是当前唯一带算法的接口。

### POST /workspaces/{workspace_id}/topics/recommend

把近期未被覆盖的事件转成候选选题，打分排序后返回。**幂等**：重复调用不会重复建选题，
只重算分数。

**查询参数**

| 参数 | 类型 | 默认 | 约束 |
|---|---|---|---|
| `limit` | int | 10 | 1–50 |
| `lookback_days` | int | 14 | 1–90 |

**响应 200**：`TopicRecommendation[]`（按 `overall_score` 倒序）

```json
[
  {
    "id": "0193f2d0-5555-7c4e-8a3b-0f5c8b6e4a12",
    "primary_event_id": "0193f2c0-4444-7c4e-8a3b-0f5c8b6e4a12",
    "title": "某厂商发布新一代 AI 编程助手",
    "description": "支持跨文件重构，官方称补全准确率提升 40%",
    "status": "CANDIDATE",
    "trend_score": 0.8824,
    "relevance_score": 0.6667,
    "potential_score": 1.0,
    "risk_score": null,
    "discovered_at": "2026-11-25T06:31:02.114+00:00",
    "selected_at": null,
    "overall_score": 0.7813
  }
]
```

#### 打分规则（当前是纯规则，不含 LLM）

| 分数 | 计算 | 含义 |
|---|---|---|
| `trend_score` | `1/(1+age_hours/24) × 平台权重` | 事件多新；24 小时衰减一半 |
| `relevance_score` | `min(1, 命中战略关键词数 / 3)` | 命中 3 个即满分；战略无关键词时为 0 |
| `potential_score` | 有摘要 +0.4、有链接 +0.3、有发布时间 +0.3 | **素材完备度**，是「能不能立刻开工」的代理指标，不是传播预测 |
| `overall_score` | `0.55×relevance + 0.30×trend + 0.15×potential` | 只在本次响应里出现，**不落库** |

平台权重：`SOCIAL 1.0` > `NEWS 0.9` > `SEARCH 0.85` > `RSS/API 0.7` > `MANUAL/OTHER 0.5`。

关键词提取：把 `strategy_config` 里所有长度 ≥2 的字符串（递归遍历数组和对象）当作短语，
在 `title + summary` 上做**子串匹配**（小写归一化）。不用分词器，中英文都能工作。

> `overall_score` 不落库是刻意的：它依赖当时的权重，存下来会在权重调整后变成假历史。

**写入行为**

1. 先 `INSERT ... SELECT ... ON CONFLICT DO NOTHING`：把 `lookback_days` 内、
   还没有任何选题覆盖的事件转成 `CANDIDATE`。并发调用时靠
   `topics(workspace_id, primary_event_id)` 部分唯一索引兜底。
2. 再对该工作区**所有** `CANDIDATE`（最多 200 条）重算三个分数并批量 UPDATE。
3. 返回按 `overall_score` 排序的前 `limit` 条。

**错误**

| 状态 | code | 场景 |
|---|---|---|
| 409 | `NO_ACTIVE_STRATEGY` | 没有生效战略，算不出 relevance |

**注意**：`risk_score` 当前恒为 `null`，留给 AI Runtime。`status` 在 recommend 之后
仍然是 `CANDIDATE`——接口不会自动把候选改成 `RECOMMENDED`。

### GET /workspaces/{workspace_id}/topics

**查询参数**

| 参数 | 类型 | 默认 | 约束 |
|---|---|---|---|
| `status` | enum | null | `CANDIDATE` `RECOMMENDED` `SELECTED` `REJECTED` `EXPIRED` `USED` |
| `limit` | int | 20 | 1–100 |

**响应 200**：`TopicOut[]`（按 `discovered_at DESC`）

### GET /workspaces/{workspace_id}/topics/{topic_id}

**响应 200**：`TopicOut`
**错误**：`404 TOPIC_NOT_FOUND`

`TopicOut` 字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | uuid | |
| `primary_event_id` | uuid \| null | 主事件，选题的主证据 |
| `title` | string | ≤500（从事件标题截断） |
| `description` | string \| null | 事件摘要 |
| `status` | enum | 见上 |
| `trend_score` / `relevance_score` / `potential_score` | float \| null | 0–1 |
| `risk_score` | float \| null | 当前恒 null |
| `discovered_at` | timestamp | |
| `selected_at` | timestamp \| null | |

### POST /workspaces/{workspace_id}/topics/{topic_id}/select

选定选题，**并在同一事务里自动写一条 `TOPIC_SELECTION` 决策**（`status = APPROVED`，
`strategy_version_id` 由服务端解析，evidence trace 记录四个分数）。

```json
{ "selected_option": "从这个角度写：补全准确率的可信度", "rationale": "目标读者最关心实效" }
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `selected_option` | string | 否 | ≤200；**不传时默认用选题标题** |
| `rationale` | string | 否 | 决策理由，进 `decisions.rationale` |

**响应 200**：`TopicOut`（`status = SELECTED`，`selected_at` 已填）

> 响应里**不返回**自动生成的 `decision_id`。需要它请调 `GET /decisions?decision_type=TOPIC_SELECTION`。

**错误**

| 状态 | code | 场景 |
|---|---|---|
| 404 | `TOPIC_NOT_FOUND` | |
| 409 | `TOPIC_NOT_SELECTABLE` | 状态不是 `CANDIDATE`/`RECOMMENDED`（已选、已拒、已过期、已用过） |
| 409 | `NO_ACTIVE_STRATEGY` | 决策必须绑定战略版本，没有 ACTIVE 就写不了 |

**并发**：先 `SELECT ... FOR UPDATE` 锁选题行，双击不会产生两条决策。

### POST /workspaces/{workspace_id}/topics/{topic_id}/reject

拒绝选题，同样自动写一条 `TOPIC_SELECTION` 决策（`status = REJECTED`）。

```json
{ "reason": "与 Q4 方向不符" }
```

| 字段 | 类型 | 必填 |
|---|---|---|
| `reason` | string | 否（进 `decisions.rationale`） |

**响应 200**：`TopicOut`（`status = REJECTED`）

**错误**：同 select（`404` / `409 TOPIC_NOT_SELECTABLE` / `409 NO_ACTIVE_STRATEGY`）

---

## 8. 决策 Decisions

AIXING 与普通 CMS 的分界线：这里记录**「为什么」**。

两条硬规则：

1. `strategy_version_id` **不接受客户端传**，由 `app.current_strategy_version()` 解析当前生效版本。
   否则客户端可以永远传 v1，三个月后审计会发现所有决策都声称基于初版战略。
2. `decision_traces` 存的是**可审计的证据与打分**，不是模型的思维链原文。

### POST /workspaces/{workspace_id}/decisions

创建决策，初始状态 `PROPOSED`。

```json
{
  "decision_type": "ANGLE_SELECTION",
  "topic_id": "0193f2d0-5555-7c4e-8a3b-0f5c8b6e4a12",
  "selected_option": "对比评测角度",
  "rationale": "同类内容多为发布会通稿，实测对比有差异化",
  "score": 0.82,
  "traces": [
    {
      "trace_type": "EVIDENCE",
      "source_type": "EVENT",
      "source_id": "0193f2c0-4444-7c4e-8a3b-0f5c8b6e4a12",
      "data": { "url": "https://example.com/news/001", "quote": "提升 40%" }
    },
    {
      "trace_type": "SCORE",
      "data": { "relevance": 0.6667, "risk": 0.1 }
    }
  ]
}
```

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| `decision_type` | enum | 是 | `TOPIC_SELECTION` `ANGLE_SELECTION` `CONTENT_DIRECTION` `PUBLISHING` `STRATEGY` |
| `topic_id` | uuid | 否 | 必须属于同一工作区（否则 422 外键） |
| `selected_option` | string | 否 | ≤200 |
| `rationale` | string | 否 | |
| `score` | float | 否 | **0–1**，越界 422 |
| `traces` | array | 否 | 默认 `[]` |

`traces[]` 元素：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `trace_type` | enum | 是 | `EVIDENCE` `SCORE` `RULE` `ALTERNATIVE` `CONSTRAINT` |
| `source_type` | string | 否 | 自由文本，如 `EVENT` / `TOPIC` / `CONTENT` / `RULE_SET` |
| `source_id` | uuid | 否 | 指向来源对象 |
| `data` | object | 否 | 默认 `{}`，证据原文 / 分数 / 规则命中情况 |

**响应 201**：`DecisionOut`（含全部 traces）

```json
{
  "id": "0193f2e0-6666-7c4e-8a3b-0f5c8b6e4a12",
  "strategy_version_id": "0193f2b5-3333-7c4e-8a3b-0f5c8b6e4a12",
  "topic_id": "0193f2d0-5555-7c4e-8a3b-0f5c8b6e4a12",
  "decision_type": "ANGLE_SELECTION",
  "status": "PROPOSED",
  "selected_option": "对比评测角度",
  "rationale": "同类内容多为发布会通稿，实测对比有差异化",
  "score": 0.82,
  "decided_by": "0193f2a0-1111-7c4e-8a3b-0f5c8b6e4a12",
  "decided_at": null,
  "created_at": "2026-11-25T06:35:10.002+00:00",
  "traces": [
    {
      "trace_type": "EVIDENCE",
      "source_type": "EVENT",
      "source_id": "0193f2c0-4444-7c4e-8a3b-0f5c8b6e4a12",
      "data": { "url": "https://example.com/news/001", "quote": "提升 40%" }
    }
  ]
}
```

**错误**

| 状态 | code | 场景 |
|---|---|---|
| 409 | `NO_ACTIVE_STRATEGY` | 没有生效战略或其当前版本不存在 |
| 422 | `FOREIGN_KEY_VIOLATION` | `topic_id` 不存在或属于别的租户 |
| 422 | `VALIDATION_ERROR` | `score` 越界、枚举非法 |

### GET /workspaces/{workspace_id}/decisions

**查询参数**

| 参数 | 类型 | 默认 | 约束 |
|---|---|---|---|
| `decision_type` | enum | null | 5 种类型之一 |
| `limit` | int | 20 | 1–100 |

**响应 200**：`DecisionOut[]`，按 `created_at DESC`，**每条都带 `traces`**
（`json_agg` 聚合，不会有 N+1）。

### GET /workspaces/{workspace_id}/decisions/{decision_id}

**响应 200**：`DecisionOut`
**错误**：`404 DECISION_NOT_FOUND`

`DecisionOut` 字段：`id, strategy_version_id, topic_id, decision_type, status,
selected_option, rationale, score, decided_by, decided_at, created_at, traces[]`

### POST /workspaces/{workspace_id}/decisions/{decision_id}/approve

把 `PROPOSED` 决策置为 `APPROVED`，写入 `decided_by` / `decided_at`。

```json
{ "comment": "同意，按这个角度推进" }
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `comment` | string | 否 | 非空时**覆盖** `rationale` |

**响应 200**：`DecisionOut`

**错误**

| 状态 | code | 场景 |
|---|---|---|
| 404 | `DECISION_NOT_FOUND` | |
| 409 | `DECISION_NOT_PROPOSED` | 已经是 APPROVED / REJECTED / EXECUTED / SUPERSEDED |

**只允许 `PROPOSED → APPROVED`**。重复审批返回 409 而不是静默覆盖——静默成功会让
「谁在什么时候批的」这条审计线失真。

### POST /workspaces/{workspace_id}/decisions/{decision_id}/reject

同上，置为 `REJECTED`。请求体与错误码同 approve。

---

## 9. 内容 Contents

内容资产与版本。**改正文用 `POST /versions`，不提供 `PATCH body`**：
每一次人工修改都是一个版本，这样 `v1(AI) → v2(人工改) → v3(发布)` 的差异
将来可以反推成稳定偏好与 Pattern。

审批的唯一写入口是 `approvals` 表：`contents.status` **由数据库触发器推导**，
服务层不双写。

### POST /workspaces/{workspace_id}/contents

```json
{
  "title": "AI 编程助手实测：补全准确率到底有多少",
  "content_type": "VIDEO_SCRIPT",
  "topic_id": "0193f2d0-5555-7c4e-8a3b-0f5c8b6e4a12",
  "decision_id": "0193f2e0-6666-7c4e-8a3b-0f5c8b6e4a12",
  "body": "开场：30 秒内抛出结论……"
}
```

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| `title` | string | 是 | ≥1 |
| `content_type` | enum | 否 | `VIDEO_SCRIPT`(默认) `ARTICLE` `POST` `AUDIO` `OTHER` |
| `topic_id` | uuid | 否 | |
| `decision_id` | uuid | 否 | |
| `body` | string | 否 | **传了就同时落 v1**（`change_summary = 'initial'`） |

**响应 201**：`ContentOut`（`status = DRAFT`，`current_version_number = 1` 或 0）

> 不传 `body` 时内容只有标题、没有版本，`current_version_number` 仍是默认值 1 但
> `current_version` 为 `null`。这是合法的中间态（先建壳、后填内容）。

**错误**：`422 FOREIGN_KEY_VIOLATION`（topic/decision 不存在）、`422 VALIDATION_ERROR`、`403`

### GET /workspaces/{workspace_id}/contents

**查询参数**

| 参数 | 类型 | 默认 | 约束 |
|---|---|---|---|
| `status` | string | null | 自由字符串，不校验（见下方状态机） |
| `topic_id` | uuid | null | |
| `limit` | int | 20 | 1–100 |

**响应 200**：`ContentOut[]`，按 `updated_at DESC`。

> **注意**：列表接口**不返回 `current_version`**（恒为 `null`），因为列表 SQL 没 join
> 版本表——避免把正文（可能几十 KB）塞进列表响应。要正文请调详情。

### GET /workspaces/{workspace_id}/contents/{content_id}

**响应 200**：`ContentOut`（**含 `current_version` 完整正文**）

```json
{
  "id": "0193f2f0-7777-7c4e-8a3b-0f5c8b6e4a12",
  "topic_id": "0193f2d0-5555-7c4e-8a3b-0f5c8b6e4a12",
  "decision_id": "0193f2e0-6666-7c4e-8a3b-0f5c8b6e4a12",
  "title": "AI 编程助手实测：补全准确率到底有多少",
  "content_type": "VIDEO_SCRIPT",
  "status": "REVIEW",
  "current_version_number": 2,
  "lock_version": 1,
  "created_at": "2026-11-25T06:40:00+00:00",
  "updated_at": "2026-11-25T06:45:00+00:00",
  "current_version": {
    "id": "0193f2f5-8888-7c4e-8a3b-0f5c8b6e4a12",
    "version_number": 2,
    "title": "AI 编程助手实测：补全准确率到底有多少",
    "body": "开场：30 秒内抛出结论……",
    "metadata": {},
    "change_summary": "补充实测数据",
    "created_by": "0193f2a0-1111-7c4e-8a3b-0f5c8b6e4a12",
    "created_at": "2026-11-25T06:45:00+00:00"
  }
}
```

**错误**：`404 CONTENT_NOT_FOUND`

### GET /workspaces/{workspace_id}/contents/{content_id}/versions

**响应 200**：`ContentVersionOut[]`，按 `version_number DESC`（最新在前）。无分页。

`ContentVersionOut`：`id, version_number, title, body, metadata, change_summary, created_by, created_at`

### POST /workspaces/{workspace_id}/contents/{content_id}/versions

新增版本。正文整份提交，不做 diff——版本本身就是差异的载体。

```json
{
  "body": "开场：30 秒内抛出结论……（补充实测数据后的新正文）",
  "title": "可选：同时改标题",
  "change_summary": "补充实测数据"
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `body` | string | 是 | ≥1 |
| `title` | string | 否 | 不传则沿用当前标题 |
| `change_summary` | string | 否 | |

**响应 201**：`ContentOut`（新的 `current_version`）

**副作用**

1. `current_version_number + 1`，`lock_version + 1`。
2. **如果原状态是 `APPROVED`，自动回到 `REVIEW`** —— 审批是针对某一版的，换了版本原通过就失效。

**错误**

| 状态 | code | 场景 |
|---|---|---|
| 404 | `CONTENT_NOT_FOUND` | |
| 409 | `CONTENT_ARCHIVED` | 已归档内容不能再加版本 |

### POST /workspaces/{workspace_id}/contents/{content_id}/approve

审核通过。**只写 `approvals`**，`contents.status` 由触发器
`trg_approvals_sync_content_status` 推导。

```json
{ "comment": "可以发" }
```

**响应 200**：`ContentOut`（`status = APPROVED`）

**错误**

| 状态 | code | 场景 |
|---|---|---|
| 404 | `CONTENT_NOT_FOUND` | |
| 409 | `CONTENT_ARCHIVED` | |
| 409 | `CONTENT_ALREADY_APPROVED` | 已经是 APPROVED |

**写入行为**

1. 先把该内容所有 `stage='CONTENT' AND status='PENDING'` 的审批单置为 `CANCELLED`
   （避免待办与新结论并存）。
2. 插入一条 `APPROVED` 审批记录。
3. 触发器把 `contents.status` 从 `DRAFT`/`REVIEW`/`APPROVED` 改为 `APPROVED`。

> 触发器只在 `DRAFT / REVIEW / APPROVED` 之间迁移，**不会**把已经 `PRODUCING`/`READY`/
> `PUBLISHED` 的内容退回 `REVIEW`。
>
> 另有反向守卫 `guard_content_approval_status`：没有 `APPROVED` 审批记录时，
> 任何把 `contents.status` 直接置为 `APPROVED` 的 UPDATE 都会被拒（防止绕过审批写状态）。

### POST /workspaces/{workspace_id}/contents/{content_id}/reject

审核驳回。写一条 `REJECTED` 审批记录，触发器把内容置为 **`REVIEW`**（不是 `DRAFT`）。

```json
{ "comment": "第二段的数据来源需要补链接" }
```

**响应 200**：`ContentOut`（`status = REVIEW`）
**错误**：`404 CONTENT_NOT_FOUND`、`409 CONTENT_ARCHIVED`

> 驳回不报「已驳回」，因为 `REVIEW` 是「等人再改」的可用状态，可以直接再提交版本后重新审。

### GET /workspaces/{workspace_id}/contents/{content_id}/approvals

**响应 200**：`ApprovalOut[]`，按 `created_at DESC`（最新在前）

```json
[
  {
    "id": "0193f300-9999-7c4e-8a3b-0f5c8b6e4a12",
    "content_id": "0193f2f0-7777-7c4e-8a3b-0f5c8b6e4a12",
    "stage": "CONTENT",
    "status": "APPROVED",
    "reviewer_id": "0193f2a0-1111-7c4e-8a3b-0f5c8b6e4a12",
    "comment": "可以发",
    "decided_at": "2026-11-25T06:50:00+00:00",
    "created_at": "2026-11-25T06:50:00+00:00"
  }
]
```

`stage` 当前只产生 `CONTENT`；`PRODUCTION` / `PUBLICATION` 两个阶段将各自驱动
`productions` / `publications`（尚未接入）。

---

## 10. 任务 Tasks

后端异步执行的统一入口。**当前没有 worker，任务只会停在 `PENDING`。**

接口先立起来，是因为「生产/发布」这类长任务从第一天就该走
`202 + task_id + 轮询`，而不是让 HTTP 请求等 FFmpeg 跑完。

### POST /workspaces/{workspace_id}/tasks

```json
{
  "task_type": "RENDER_VIDEO",
  "priority": 10,
  "input": { "content_id": "0193f2f0-...", "resolution": "1080p" }
}
```

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| `task_type` | string | 是 | 1–50，**自由文本**，不做枚举校验 |
| `priority` | int | 否 | -100 ~ 100，默认 0（越大越优先） |
| `input` | object | 否 | 默认 `{}` |

**响应 201**：`TaskOut`（`status = PENDING`）

```json
{
  "id": "0193f310-aaaa-7c4e-8a3b-0f5c8b6e4a12",
  "task_type": "RENDER_VIDEO",
  "status": "PENDING",
  "priority": 10,
  "input": { "content_id": "0193f2f0-...", "resolution": "1080p" },
  "output": null,
  "error": null,
  "started_at": null,
  "completed_at": null,
  "created_at": "2026-11-25T06:55:00+00:00"
}
```

**错误**：`422 VALIDATION_ERROR`、`403`

> 注意这里返回的是 **201** 而不是 202。接 worker 之后应改成 202 + `Location` 头，
> 当前实现先保持「资源已创建」的语义。

### GET /workspaces/{workspace_id}/tasks

| 参数 | 类型 | 默认 | 约束 |
|---|---|---|---|
| `status` | string | null | 自由字符串 |
| `limit` | int | 20 | 1–100 |

**响应 200**：`TaskOut[]`，按 `created_at DESC`

### GET /workspaces/{workspace_id}/tasks/{task_id}

**轮询用**。前端拿到 `task_id` 后按 1–3 秒间隔轮询，直到 `status` 进入终态
（`SUCCEEDED` / `FAILED` / `CANCELLED`）。

**响应 200**：`TaskOut`
**错误**：`404 TASK_NOT_FOUND`

`TaskOut`：`id, task_type, status, priority, input, output, error, started_at, completed_at, created_at`

- `output`：成功时的产物（自由 JSON）
- `error`：失败原因（自由 JSON），取消时形如 `{"reason": "...", "cancelled_by": "user"}`

### POST /workspaces/{workspace_id}/tasks/{task_id}/cancel

**参数在 query string，不是 body**：

```
POST /workspaces/{workspace_id}/tasks/{task_id}/cancel?reason=用户放弃
```

| 参数 | 位置 | 必填 |
|---|---|---|
| `reason` | query | 否 |

**响应 200**：`TaskOut`（`status = CANCELLED`，`completed_at` 已填）

**错误**

| 状态 | code | 场景 |
|---|---|---|
| 404 | `TASK_NOT_FOUND` | |
| 409 | `TASK_NOT_CANCELLABLE` | 任务已进入 `SUCCEEDED` / `FAILED` / `CANCELLED` |

**只有 `PENDING` / `RUNNING` / `WAITING` 可以取消。**

> 当前 `cancel` 只改数据库状态。真正停掉一个正在跑的进程需要 worker 侧配合
> （检查取消标记 / 终止子进程），接 worker 时必须补齐。

---

## 11. 开发辅助 Dev

### POST /api/v1/dev/users

只在 `APP_ENV=development` 下可用。存在理由很具体：开发阶段不做登录，但验证租户隔离
需要**第二个真实用户**，而 `users.id` 是外键，随便编一个 UUID 会违反约束。

```json
{ "email": "alice@example.com", "display_name": "Alice" }
```

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| `email` | string | 是 | 3–255，必须含 `@`，服务端转小写 |
| `display_name` | string | 是 | 1–100 |

**响应 201**

```json
{
  "id": "0193f320-bbbb-7c4e-8a3b-0f5c8b6e4a12",
  "email": "alice@example.com",
  "display_name": "Alice"
}
```

**幂等**：同一邮箱（大小写不敏感）重复调用会**更新 `display_name` 并返回原 ID**，
不会报冲突。所以可以当「确保用户存在」用。

**错误**

| 状态 | code | 场景 |
|---|---|---|
| 404 | `NOT_FOUND` | 非开发环境（伪装成接口不存在，不暴露调试入口） |
| 422 | `INVALID_EMAIL` | 没有 `@` |
| 422 | `VALIDATION_ERROR` | 长度不合规 |

**这个接口没有鉴权，不能带进生产。**

---

## 12. 状态机总表

### 战略 `strategies.status`

```
DRAFT ──activate──> ACTIVE ──(被别的战略激活)──> PAUSED ──activate──> ACTIVE
  │                   │
  └───────────────────┴──> ARCHIVED（当前无接口，直接改库）
```

- 一个工作区同时最多一条 `ACTIVE`。
- `ARCHIVED` 不能新增版本、不能激活（409）。
- `PAUSED` 是「曾经生效、被别人顶掉」，可以再激活。

### 选题 `topics.status`

```
        recommend 建档
              ↓
CANDIDATE ──select──> SELECTED ──(内容产出后)──> USED
    │
    ├──reject──> REJECTED
    │
    └──(超期)──> EXPIRED          RECOMMENDED 也可 select/reject
```

`select` / `reject` 只接受 `CANDIDATE` 与 `RECOMMENDED`，其余状态一律
`409 TOPIC_NOT_SELECTABLE`。`EXPIRED` / `USED` 当前没有接口写入。

### 决策 `decisions.status`

```
PROPOSED ──approve──> APPROVED ──(执行)──> EXECUTED
    │
    └──reject──> REJECTED

（被新决策替代 → SUPERSEDED，无接口）
```

- `POST /decisions` 建的是 `PROPOSED`。
- `topics/select`、`topics/reject` 自动建的是 `APPROVED` / `REJECTED`（已决，不需二审）。

### 内容 `contents.status`

```
DRAFT ──approve──> APPROVED ──(生产)──> PRODUCING ──> READY ──(发布)──> PUBLISHED
  │                   │
  │                   └──add_version──> REVIEW
  ├──approve──> APPROVED
  └──reject──> REVIEW ──add_version──> REVIEW ──approve──> APPROVED

任意状态 ──> ARCHIVED（无接口）
```

关键规则：**新增版本会让 `APPROVED` 回到 `REVIEW`**；驳回也是回到 `REVIEW`（不是 `DRAFT`）。

### 任务 `tasks.status`

```
PENDING ──(worker 领取)──> RUNNING ──> SUCCEEDED
   │                          │
   │                          └──> FAILED
   └──cancel──> CANCELLED <──cancel── RUNNING / WAITING
```

当前只有 `PENDING` 与 `CANCELLED` 会被真正写出来。

### 审批 `approvals.status`

```
PENDING ──> APPROVED / REJECTED
   └──(被新结论作废)──> CANCELLED
```

---

## 13. 端到端最小流程

从零到一条可发布内容，最短路径（全程 `http://localhost:8000`）：

```bash
# 0) 拿到 dev 工作区（首次调用会自动创建 dev 用户 + XING Dev 工作区）
WS=$(curl -s localhost:8000/api/v1/workspaces | python -c "import json,sys;print(json.load(sys.stdin)[0]['id'])")
echo $WS

# 1) 建战略
SID=$(curl -s -X POST localhost:8000/api/v1/workspaces/$WS/strategies \
  -H 'Content-Type: application/json' \
  -d '{"name":"Q4 战略","strategy_config":{"content_directions":["AI 编程工具","独立开发者"]}}' \
  | python -c "import json,sys;print(json.load(sys.stdin)['id'])")

# 2) 激活（不激活后面全部 409 NO_ACTIVE_STRATEGY）
curl -s -X POST localhost:8000/api/v1/workspaces/$WS/strategies/$SID/activate > /dev/null

# 3) 采集事件（幂等：第二次返回 200）
curl -s -X POST localhost:8000/api/v1/workspaces/$WS/events/ingest \
  -H 'Content-Type: application/json' \
  -d '{"source_type":"NEWS","external_id":"demo-001","title":"AI 编程工具发布新版",
       "summary":"支持跨文件重构","url":"https://example.com/1",
       "source_published_at":"2026-11-25T00:00:00Z"}'

# 4) 推荐选题
curl -s -X POST "localhost:8000/api/v1/workspaces/$WS/topics/recommend?limit=5"

# 5) 选定（自动写一条决策）
TID=<上一步返回的 id>
curl -s -X POST localhost:8000/api/v1/workspaces/$WS/topics/$TID/select \
  -H 'Content-Type: application/json' -d '{"rationale":"时效性强"}'

# 6) 建内容并落 v1
CID=$(curl -s -X POST localhost:8000/api/v1/workspaces/$WS/contents \
  -H 'Content-Type: application/json' \
  -d "{\"title\":\"实测\",\"topic_id\":\"$TID\",\"body\":\"正文初稿\"}" \
  | python -c "import json,sys;print(json.load(sys.stdin)['id'])")

# 7) 审核通过（触发器把 status 推到 APPROVED）
curl -s -X POST localhost:8000/api/v1/workspaces/$WS/contents/$CID/approve \
  -H 'Content-Type: application/json' -d '{"comment":"可以发"}'

# 8) 建生产任务（会停在 PENDING，等 worker）
curl -s -X POST localhost:8000/api/v1/workspaces/$WS/tasks \
  -H 'Content-Type: application/json' \
  -d "{\"task_type\":\"RENDER_VIDEO\",\"input\":{\"content_id\":\"$CID\"}}"
```

验证租户隔离（需要第二个用户）：

```bash
# 造一个 alice，用她的身份访问 dev 工作区
AID=$(curl -s -X POST localhost:8000/api/v1/dev/users \
  -H 'Content-Type: application/json' \
  -d '{"email":"alice@example.com","display_name":"Alice"}' \
  | python -c "import json,sys;print(json.load(sys.stdin)['id'])")

# 列表返回空数组（RLS 过滤），写操作返回 403
curl -s -H "X-User-ID: $AID" localhost:8000/api/v1/workspaces/$WS/contents
curl -s -X POST -H "X-User-ID: $AID" -H 'Content-Type: application/json' \
  localhost:8000/api/v1/workspaces/$WS/tasks -d '{"task_type":"X"}'
```

---

## 14. 当前缺口

按影响排序，接入前需要知道：

| 缺口 | 影响 | 位置 |
|---|---|---|
| **没有 worker** | `tasks` 永远停在 `PENDING`；`cancel` 只改库不改进程 | `services/task_service.py` |
| **没有认证** | `X-User-ID` 可任意伪造成任何人 | `api/deps.py::get_current_user_id` |
| **`recommend` 不含 LLM** | 打分是规则式，`risk_score` 恒 `null`；无去重/语义聚类 | `services/topic_service.py` |
| **列表不返回 `current_version`** | 内容列表拿不到正文，需逐个调详情 | `services/content_service.py::list_contents` |
| **非成员访问列表返回 200 + `[]`** | 前端无法区分「无权」与「没有数据」 | RLS 在 SQL 层过滤的直接结果 |
| **战略/内容无归档接口** | 只有工作区能用 `PATCH status=ARCHIVED`；战略与内容的归档要直接改库 | 无 |
| **`strategy_config` 整份覆盖** | 改一个字段必须回传全量，容易误删 | `services/strategy_service.py::add_version` |
| **`POST /tasks` 返回 201 而非 202** | 与「异步任务」语义略有出入，接 worker 时应对齐 | `api/v1/tasks.py` |
| **`select` 不返回 `decision_id`** | 前端要回头查决策列表才能拿到关联决策 | `services/topic_service.py::select_topic` |
| **游标格式会随字段变化失效** | 已发出的 `next_cursor` 在部署新版本后可能解不开 | `api/pagination.py` |
| **`q` 用 `ILIKE %...%`** | 大数据量下走不了索引；`pg_trgm` 已装但未建 GIN 索引 | `services/event_service.py::list_events` |
