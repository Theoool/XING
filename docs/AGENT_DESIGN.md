# Agent 层设计（冻结稿 v1）

> 状态：**决策已冻结**，`0005` 迁移按本文执行。
> 适用范围：`src/xing/agents/`、`src/xing/worker/`、`tasks` / `agent_runs` / `llm_calls` 三张表。
> 基线：19 张表全 ORM 映射、服务层零手写 SQL、24 个测试全绿、六道防线（pytest / ruff+mypy / ormcheck / rlscheck / dbsmoke / agent_check）齐备。
> 本文落地后（`0005` + worker 骨架）：20 张表、20 个测试、六道防线（新增 agent_check 的状态机穷举与 worker 并发）。

本文只定「结构与契约」，不写实现细节。凡标注 **【冻结】** 的条目，后续阶段不得单方面变更；要改必须先改本文并给出迁移影响。

---

## 0. 一句话定位

XING 的 Agent 层是**内容生产线上的决策与执行引擎**，不是通用 Agent 框架。

用户是 Content Director，掌握三件事：选题批准、内容发布、预算上限。Agent 在授权边界内自主执行「选题 → 调研 → 写作 → 审核 → 生产」的链路，每一步的输入输出都落在数据库里，可审计、可回放、可刹车。

### 反目标（明确不做）

| 不做 | 理由 |
| --- | --- |
| 通用 Agent 编排框架（支持任意 DAG、任意工具、任意模型） | 业务只有一条主链路，通用性会换来三倍复杂度、零收益 |
| Agent 自由对话 / 聊天式交互 | 产品形态是生产线，不是 Chatbot。交互面是 API + 审批流 |
| 把思维链写进库 | 体积大、无审计语义、泄密面大。只存"决策依据的结构化摘要"（`decision_traces`） |
| 一次建齐 9 张 `agent_*` 表 | 无写入方的表等于猜测。表跟着代码长，不预先摊开 |
| 在 Phase 1 引入 MCP | 现有 7 个 service 已是全部所需能力，MCP 是外部工具接入通道，不是内部调用层 |

---

## 1. 三层执行模型

Agent 层涉及三个**不同粒度**的执行概念。混用它们是这类系统最常见的错误来源，因此三者各占一张表，职责互斥：

| 表 | 粒度 | 回答的问题 | 生命周期 | 权威写入方 |
| --- | --- | --- | --- | --- |
| `tasks` | 业务工作单元 | 「这件事该谁做、做完没」 | 分钟~小时 | Worker（状态） |
| `agent_runs` | 一次 Agent 执行 | 「这次执行的目标、计划、预算、花了多少」 | 秒~分钟 | Agent Runtime |
| `llm_calls` | 一次模型调用 | 「这一步用了哪个模型、多少 token、多少钱、返回什么」 | 毫秒~秒 | Agent Runtime |

关系：`tasks 1 ── N agent_runs 1 ── N llm_calls`。

一个 `task` 可以有多次 `run`（失败重试、人工重跑），一次 `run` 可以有多次 `llm_call`（规划、工具决策、生成、自检）。

### 【冻结】1.1 不建 `agent_tasks`

评审稿提出的 `agent_tasks` 与 `tasks` 语义重叠。同一个"待办"存两处，必然出现状态不一致，而修复成本远高于收益。

`tasks` 通过 `task_type` 区分业务类型（`WRITE_CONTENT` / `RESEARCH_TOPIC` / `PRODUCE_MEDIA` / `REVIEW_CONTENT`），通过 `assigned_agent` 指定执行者。Agent 侧不需要第二套任务表。

---

## 2. 表设计决策

### 2.1 【冻结】`agent_runs` 语义修正：现表改名 `llm_calls`

现有 `agent_runs` 实际记录的是**单次模型调用**（model_name / input_tokens / output_tokens / cost / latency_ms），却占用了 "run" 这个名字。这会导致真正的 run 级字段（goal / plan / 预算）无处可放。

处置（0 行数据，零迁移成本）：

1. `ALTER TABLE agent_runs RENAME TO llm_calls`，同时重命名索引、约束、RLS policy。
2. 新建 run 级 `agent_runs`。
3. ORM 侧 `AgentRun` → `LlmCall`，新增 `AgentRun`。

### 2.2 【冻结】`tasks` 补列

```sql
ALTER TABLE tasks
    ADD COLUMN assigned_agent  text,
    ADD COLUMN depends_on      uuid[]      NOT NULL DEFAULT '{}',
    ADD COLUMN retry_count     smallint    NOT NULL DEFAULT 0,
    ADD COLUMN max_retries     smallint    NOT NULL DEFAULT 3,
    ADD COLUMN available_at    timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN locked_by       text,
    ADD COLUMN locked_at       timestamptz,
    ADD COLUMN lease_expires_at timestamptz,
    ADD COLUMN lease_generation bigint     NOT NULL DEFAULT 0;

ALTER TABLE tasks
    ADD CONSTRAINT tasks_retry_within_limit CHECK (retry_count <= max_retries),
    ADD CONSTRAINT tasks_lease_consistency  CHECK (
        (locked_by IS NULL) = (lease_expires_at IS NULL)
    ),
    ADD CONSTRAINT tasks_no_self_dependency CHECK (NOT (id = ANY (depends_on)));
```

逐列理由：

- `assigned_agent`：`NULL` = 任意 worker 可领；非空 = 只有该 Agent 能领。这是"派发"与"抢占"两种模式的开关。
- `depends_on`：前置任务 id 数组。**不加 FK**——依赖是同一 workspace 内的软引用，且任务只被 `CANCELLED` 不被删除，悬空 id 的风险为零。用 GIN 索引支撑 `@>` 查询。若将来需要强一致，再升级为 `task_dependencies` 关联表，届时数据可无损搬过去。
- `retry_count` / `max_retries`：重试预算。用 CHECK 而非触发器，耗尽后写状态由应用层判断。
- `available_at`：退避时间。领取条件含 `available_at <= now()`，这样 `RETRYING` 任务不需要额外队列。
- `locked_by` / `locked_at` / `lease_expires_at`：租约。worker 崩溃时靠 `lease_expires_at` 过期回收，不依赖进程存活探测。
- `lease_generation`：**fencing token**。每次领取、每次回收各 +1，所有状态写入必须带 `WHERE lease_generation = :gen`。

**【0006 修正】为什么光有 `locked_by` 不够。** `locked_by` 回答"是谁"，不回答"是第几代"。它挡不住两个场景：

1. A 读到 `RUNNING` 后卡住 → 租约过期 → B 回收 → C 领取 → A 恢复并 commit。A 的 UPDATE 只带主键，会把 C 正在执行的任务标成 `SUCCEEDED` 并清空 C 的租约。`guard_task_status_transition` 看到 `RUNNING→SUCCEEDED` 是合法边，**会放行** —— 它校验边，不校验谁有权走这条边。
2. `make_worker_id()` 是 `hostname:pid`。容器里 hostname 是服务名、pid 常为 1，重启后 worker_id **完全相同**，旧进程残留时身份校验直接通过。

边界：fencing 只能挡**数据库写入**。已经发给第三方的请求无法撤销，那需要业务层的幂等键（1b 的 `idempotency_key = run_id:step_no`）。

**【冻结】不新增 `tasks.run_id`。** 评审稿有此提议，但方向反了：一个 task 对应多次 run，指针应挂在 `agent_runs.task_id`（已存在）。若在 `tasks` 上存 `run_id`，重试一次就要覆盖一次，历史 run 与 task 的关联反而丢失。

### 2.3 【冻结】新建 run 级 `agent_runs`

```sql
CREATE TABLE agent_runs (
    id                  uuid PRIMARY KEY DEFAULT uuidv7(),

    workspace_id        uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,

    task_id             uuid REFERENCES tasks(id) ON DELETE SET NULL,
    parent_run_id       uuid REFERENCES agent_runs(id) ON DELETE RESTRICT,

    agent_name          text NOT NULL,
    attempt             smallint NOT NULL DEFAULT 1 CHECK (attempt > 0),

    -- 目标与计划：Orchestrator 写入，Pydantic 校验后落库
    goal                text NOT NULL,
    plan                jsonb,

    -- 自治级别：SUGGEST 每步需批 / SEMI_AUTO 仅对外动作需批 / AUTO 内部自主
    autonomy_mode       text NOT NULL DEFAULT 'SEMI_AUTO'
                        CHECK (autonomy_mode IN ('SUGGEST', 'SEMI_AUTO', 'AUTO')),

    status              text NOT NULL DEFAULT 'RUNNING'
                        CHECK (status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')),

    -- 三层预算（硬约束，见 §4.4）
    max_steps           integer       NOT NULL DEFAULT 20    CHECK (max_steps > 0),
    step_count          integer       NOT NULL DEFAULT 0     CHECK (step_count >= 0),
    max_tokens          bigint        NOT NULL DEFAULT 200000,
    total_tokens        bigint        NOT NULL DEFAULT 0,
    max_cost            numeric(18,8) NOT NULL DEFAULT 2.0,
    total_cost          numeric(18,8) NOT NULL DEFAULT 0,
    reserved_cost       numeric(18,8) NOT NULL DEFAULT 0,  -- 0007：已预留未结算

    output              jsonb,
    error               jsonb,

    started_at          timestamptz NOT NULL DEFAULT now(),
    completed_at        timestamptz,

    CONSTRAINT agent_runs_steps_within_budget  CHECK (step_count   <= max_steps),
    CONSTRAINT agent_runs_tokens_within_budget CHECK (total_tokens <= max_tokens),
    CONSTRAINT agent_runs_cost_within_budget   CHECK (total_cost + reserved_cost <= max_cost),
    CONSTRAINT agent_runs_no_self_parent       CHECK (parent_run_id IS DISTINCT FROM id),
    CONSTRAINT agent_runs_completed_consistency CHECK (
        (status = 'RUNNING') = (completed_at IS NULL)
    )
);

CREATE UNIQUE INDEX agent_runs_task_attempt
    ON agent_runs (task_id, attempt)
 WHERE task_id IS NOT NULL;

CREATE INDEX idx_agent_runs_workspace_started
    ON agent_runs (workspace_id, started_at DESC);
CREATE INDEX idx_agent_runs_task       ON agent_runs (task_id);
CREATE INDEX idx_agent_runs_parent     ON agent_runs (parent_run_id);
```

设计要点：

- `goal` 是自然语言但**必填**：它是审计时唯一能解释"为什么跑这一趟"的字段。没有 goal 的 run 不允许存在。
- `plan` 由 Orchestrator 产出（`{"steps": [...]}`），存 JSONB 是因为它只是过程快照，不参与查询与约束。
- `output` 存**结构化结果**（内容 id、引用 id、判定结论），不存正文。正文归 `contents` / `content_versions`。这条界线要守死，否则同一份文本会散落三处。
- `parent_run_id`：支持 Review 这类子 Agent 的嵌套执行。**RESTRICT 而非 CASCADE**（0007 修正）：级联删会把子 run 的审计一起带走，而审计的全部价值就在事后能查。要清理就先显式删子 run。
- `attempt` + 唯一索引：同一 task 的多次执行有稳定编号，与 `tasks.retry_count` 对得上。
- `reserved_cost`：为「先花钱后记账」准备的占位额度，见 §4.4。财务上它是已承诺未发生，必须和 `total_cost` 一起参与预算判断。

### 2.4 【冻结】`llm_calls`（改名后的原 `agent_runs`）

保留原表全部计量列，做以下增删：

```sql
-- 第一步：整表改名。表改名不会连带改索引/约束/策略的名字，
-- 而模型侧写的是 llm_calls_*，不重命名会被 alembic check 判成漂移。
ALTER TABLE agent_runs RENAME TO llm_calls;
ALTER INDEX agent_runs_pkey                RENAME TO llm_calls_pkey;
ALTER INDEX idx_agent_runs_workspace_started RENAME TO idx_llm_calls_run_started;
ALTER INDEX idx_agent_runs_model           RENAME TO idx_llm_calls_model;
ALTER POLICY agent_runs_workspace_policy ON llm_calls RENAME TO llm_calls_workspace_policy;

-- 其余 CHECK 约束名同样含旧表名，用 DO 块批量改。
-- 不逐个手写：漏一个就是一次 alembic check 漂移（见 scripts/app_role.sql 同样的取舍）。
DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT c.conname
          FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
         WHERE t.relname = 'llm_calls' AND c.conname LIKE 'agent_runs_%'
    LOOP
        EXECUTE format('ALTER TABLE llm_calls RENAME CONSTRAINT %I TO %I',
                       r.conname, replace(r.conname, 'agent_runs_', 'llm_calls_'));
    END LOOP;
END
$$;

-- 第二步：调整列（0005 建，0007 收紧为下面这版最终形态）
ALTER TABLE llm_calls
    ADD COLUMN run_id     uuid NOT NULL,
    ADD COLUMN purpose    text NOT NULL,          -- 无默认值：归类是调用方的责任
    ADD COLUMN attempt    smallint NOT NULL DEFAULT 1,
    ADD COLUMN step_no    integer  NOT NULL DEFAULT 0,
    ADD COLUMN call_seq   integer  NOT NULL,      -- run 内调用序号，从 1 起
    ADD COLUMN request    jsonb,
    ADD COLUMN response   jsonb,
    ADD COLUMN replay_key text;

-- 原 task_id 冗余（run 上已有），删除
ALTER TABLE llm_calls DROP COLUMN task_id;
-- agent_name 移到 run 上（同一 run 内不换 Agent）
ALTER TABLE llm_calls DROP COLUMN agent_name;

-- 租户绑定：run 必须属于这个 workspace。worker 是 BYPASSRLS，
-- 写错 workspace_id 不会被策略挡住，只有这条复合外键能拦。
ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_id_workspace_key UNIQUE (id, workspace_id);
ALTER TABLE llm_calls ADD CONSTRAINT llm_calls_run_workspace_fkey
    FOREIGN KEY (run_id, workspace_id)
    REFERENCES agent_runs (id, workspace_id) ON DELETE CASCADE;

-- 调用序号按 run 唯一。旧键 (run_id, step_no, purpose, attempt) 的错误假设是
-- 「一个 step 内每个 purpose 只调一次」—— 同一 step 重试两次就撞键。
CREATE UNIQUE INDEX llm_calls_call_seq ON llm_calls (run_id, call_seq);

-- 只约束「录制」，不约束「回放」：回放命中会插入第二行同键记录。
CREATE UNIQUE INDEX llm_calls_replay_key
    ON llm_calls (workspace_id, replay_key)
 WHERE replay_key IS NOT NULL AND status = 'SUCCEEDED';
```

- `purpose` 取值收敛为 `plan` / `tool_decision` / `generate` / `self_check` / `repair`。用于按用途拆成本。**没有 `step`**：这个兜底常量曾掩盖「一次调用算不算一个 step」的歧义（触发器按它 +1、Runtime 也 +1，结果要么 0 要么 2 倍）。
- `attempt` 是 **run 级**（= 任务第几次执行），`call_seq` 是 **run 内调用序号**。两者分开，同一 step 内重试就不会撞唯一键。
- `request` / `response` 存**脱敏后的**完整往返。这是录制回放的燃料，也是 debug 时唯一的一手证据。体积靠 `purpose <> 'generate'` 的采样策略控制（见 §4.5）。
- `replay_key` = `sha256(provider ‖ model ‖ system ‖ prompt ‖ 版本前缀)`，唯一范围是 `(workspace_id, replay_key)` 且只约束 `SUCCEEDED` 行。全局唯一会让 B 租户撞上 A 已录制的键、命中后读到别人的响应 —— 那是数据泄漏，不只是键冲突。

### 2.5 【冻结】不建的 6 张表

评审稿建议 9 张 `agent_*`。逐张处置：

| 提议表 | 处置 | 替代 |
| --- | --- | --- |
| `agent_tasks` | 不建 | 用 `tasks`（§1.1） |
| `agent_tools` | 不建 | 工具注册表是**代码常量**（`tools/registry.py`），不是数据。工具定义随代码版本走，进库会失去版本控制 |
| `agent_tool_calls` | 不建 | 工具调用的输入输出已在 `llm_calls.request/response` 里 |
| `agent_memory` | 推迟到 Phase 3 | Phase 1 只做只读聚合（§6.2），读的是已有的 `decisions` / `decision_traces` / `feedback` / `performance_snapshots` |
| `agent_reflections` | 不建 | 「反思」的产物若是决策依据，进 `decision_traces`；若是改进项，进 `feedback`。不另开表 |
| `agent_evaluations` | 推迟到 Phase 3 | 先用 `performance_snapshots` 承载结果指标。内容质量评分等有稳定口径后再建 |

**判据**：一张表必须有明确的写入方、明确的读取方、明确的查询模式，三者缺一不建。

### 2.6 最终表清单

| # | 表 | 变化 |
| --- | --- | --- |
| 1-18 | 其余 18 张 | 不变 |
| 19 | `llm_calls` | 由 `agent_runs` 改名 + 调整（§2.4） |
| 20 | `agent_runs` | 新建，run 级（§2.3） |

净增 1 张表，`alembic check` 的 diff 应只有 `agent_runs` 的 create 与 `llm_calls` 的列变更。

---

## 3. 状态机与控制流归属

### 3.1 【冻结】`tasks` 状态机

```
                 ┌──────────────────────────────┐
                 │                              │
  PENDING ──depends satisfied──> READY ──claim──> RUNNING ──ok──> SUCCEEDED
     │                            │                 │
     │                            │                 ├──fail & retry left──> RETRYING ──backoff──> READY
     │                            │                 │                            │
     │                            │                 ├──fail & exhausted──────────┴──> FAILED
     │                            │                 │
     │                            │                 └──needs human──> WAITING ──resume──> RUNNING
     │                            │
     └────────────┬───────────────┴──────────────┬──────────────────────────────────────> CANCELLED
                  │                              │
              (任意非终态均可取消)          SUCCEEDED / FAILED / CANCELLED 为终态，不可再变
```

新增状态 `READY`（依赖满足、可被领取）与 `RETRYING`（等待退避）。原 `PENDING` 语义收窄为"已创建但依赖未满足或未就绪"。

`TaskStatus.CANCELLABLE` 更新为 `(PENDING, READY, RUNNING, WAITING, RETRYING)`。

### 3.2 【冻结】合法迁移由数据库守卫

沿用 `app.guard_content_approval_status` 的既有模式：**契约住触发器，不在 Python 里散落 if**。

```sql
CREATE OR REPLACE FUNCTION app.guard_task_status_transition()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status = NEW.status THEN
        RETURN NEW;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM (VALUES
            ('PENDING',   'READY'),   ('PENDING',   'CANCELLED'),
            ('READY',     'RUNNING'), ('READY',     'CANCELLED'),
            ('RUNNING',   'SUCCEEDED'), ('RUNNING', 'FAILED'),
            ('RUNNING',   'WAITING'), ('RUNNING',   'RETRYING'),
            ('RUNNING',   'CANCELLED'),
            ('WAITING',   'RUNNING'), ('WAITING',   'CANCELLED'),
            ('WAITING',   'FAILED'),
            ('RETRYING',  'READY'),   ('RETRYING',  'CANCELLED'),
            ('RETRYING',  'FAILED')
        ) AS v(from_status, to_status)
        WHERE v.from_status = OLD.status AND v.to_status = NEW.status
    ) THEN
        RAISE EXCEPTION 'tasks.status 非法迁移: % -> % (task_id=%)',
            OLD.status, NEW.status, NEW.id
            USING ERRCODE = 'check_violation',
                  HINT = '合法迁移见 docs/AGENT_DESIGN.md §3.1';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_tasks_status_guard
    BEFORE UPDATE OF status ON tasks
    FOR EACH ROW EXECUTE FUNCTION app.guard_task_status_transition();
```

注意：`PENDING` 不再直接跳 `RUNNING`。这一步强制引入"依赖评估"，顺手堵掉了"任务还没就绪就被 worker 抢走"的整类 bug。

### 3.3 【冻结】迁移的**发起方**归属

状态合法性由数据库保证；**谁有权发起**由下表规定，不通过（越权的写入会被 guard 拒绝或留下错误痕迹）。

| 目标状态 | 唯一发起方 | 时机 |
| --- | --- | --- |
| `READY` | 触发器 `promote_task_if_ready`（创建时判定）/ `promote_dependent_tasks`（上游完成）；worker 推进循环（`RETRYING` 退避到期） | 插入时依赖已满足、上游任务 `SUCCEEDED`、`available_at` 到期 |
| `RUNNING` | Worker | 领取任务，写入租约，`lease_generation + 1` |
| `SUCCEEDED` / `FAILED` | Worker（依据 `agent_runs.status`） | run 终止；**必须带当前 `lease_generation`** |
| `RETRYING` | Worker | run 失败且 `retry_count < max_retries`；回收也走这条路 |
| `WAITING` | Agent Runtime | 需要人工审批，写 `output.reason` |
| `CANCELLED` | API（用户） | `POST /tasks/{id}/cancel` |

**Agent 永远不写 `tasks.status`。** Agent 只写 `agent_runs` 与业务数据（`contents` / `decisions`）。Worker 是 `tasks` 状态的唯一写入者，因为只有它持有租约、知道自己是合法的执行者。

这条规则是并发安全的基础：如果 Agent 也能改 `tasks.status`，租约过期回收后新旧两个执行者会互相覆盖。

**【0006 修正】"唯一写入者"要落成"唯一**有效**写入者"。** 仅靠"只有 worker 写"不够——旧 worker 恢复后依然是 worker。所以收尾一律走 `queue.finalize()`，它把 `lease_generation` 写进 WHERE：失效代次的 UPDATE 影响 0 行，调用方据此**丢弃结果**（不是抛异常，这是正常的并发结果）。

`READY` 的创建时判定同样不能省：`promote_dependent_tasks` 只在"上游变 SUCCEEDED"时触发，漏掉「无依赖」和「上游早已成功」两条路径，任务会永久 `PENDING` 且零报错。

### 3.4 三层各自的权威源

| 层 | 权威源 | 派生/缓存 |
| --- | --- | --- |
| 内容状态 | `approvals`（触发器推导 `contents.status`） | — |
| 任务状态 | `tasks.status` | — |
| 执行状态 | `agent_runs.status` | `tasks.status`（由 worker 依据它推进） |
| 成本 | `llm_calls`（明细） | `agent_runs.total_*`（触发器累加的快照） |

成本的方向必须记清楚：**`llm_calls` 是唯一记账源**，`agent_runs.total_cost` 是它的事务内累加值，不独立写入。累加用触发器，避免应用层漏写：

```sql
CREATE OR REPLACE FUNCTION app.rollup_llm_call_cost()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        -- 只累计 token 与金额。step_count 由 Runtime 用条件 UPDATE 原子递增，
        -- 触发器再动一次就是双重计数（0007 修正）。
        UPDATE agent_runs
           SET total_tokens = total_tokens + NEW.input_tokens + NEW.output_tokens,
               total_cost   = total_cost   + NEW.cost
         WHERE id = NEW.run_id;
    ELSIF TG_OP = 'DELETE' THEN
        UPDATE agent_runs
           SET total_tokens = total_tokens - OLD.input_tokens - OLD.output_tokens,
               total_cost   = total_cost   - OLD.cost
         WHERE id = OLD.run_id;
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER trg_llm_calls_rollup
    AFTER INSERT OR DELETE ON llm_calls
    FOR EACH ROW EXECUTE FUNCTION app.rollup_llm_call_cost();
```

### 3.5 依赖推进触发器

```sql
CREATE OR REPLACE FUNCTION app.promote_dependent_tasks()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status <> 'SUCCEEDED' THEN
        RETURN NEW;
    END IF;

    UPDATE tasks t
       SET status = 'READY'
     WHERE t.status = 'PENDING'
       AND t.workspace_id = NEW.workspace_id
       AND t.depends_on @> ARRAY[NEW.id]
       AND NOT EXISTS (
           SELECT 1 FROM unnest(t.depends_on) AS d(dep_id)
            WHERE NOT EXISTS (
                SELECT 1 FROM tasks p
                 WHERE p.id = d.dep_id AND p.status = 'SUCCEEDED'
            )
       );

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_tasks_promote_dependents
    AFTER UPDATE OF status ON tasks
    FOR EACH ROW EXECUTE FUNCTION app.promote_dependent_tasks();

CREATE INDEX idx_tasks_depends_on ON tasks USING gin (depends_on);
```

---

## 4. Agent 运行时

### 4.1 角色与职责边界

`Orchestrator` 是**派发者**，不是超级 Agent。它的职责只有三件：把 goal 拆成 DAG、写 `tasks`、汇聚结果。它不生成内容、不调用外部工具。

| 角色 | 输入 | 输出 | 允许写的表 |
| --- | --- | --- | --- |
| `Orchestrator` | `contents.goal` 或用户指令 | `tasks` 行 + `agent_runs.plan` | `tasks`(insert)、`agent_runs` |
| `Planner` | goal + `strategy` 约束 | 计划 `{steps}` | `agent_runs.plan` |
| `Researcher` | 选题 / 任务 | `external_events` 引用 + 摘要 | `external_events` |
| `Writer` | 选题 + 调研 + 策略 | `contents` 草稿 + `content_versions` | `contents`、`content_versions` |
| `Reviewer` | 内容 + 证据集 | 判定 `{verdict, issues}` | `approvals`（仅建议）、`decision_traces` |
| `Producer` | 已批准内容 | `productions` 行 | `productions` |

强制约束：每个 Agent 只能写表中列出的表。这条靠**代码结构**保证（每个 Agent 只拿到对应 service 的实例），不靠运行时检查。

### 4.2 单步循环（所有 Agent 共用）

```
loop:
    step_no = begin_step(run_id)        # 条件 UPDATE 原子递增；0 行 -> FAILED(BUDGET_STEPS)
    decision = call_model(purpose="tool_decision", step_no=step_no)   # 结构化输出
    if decision.kind == "final":        -> SUCCEEDED(decision.output)
    tool = registry.get(decision.tool)
    if tool.requires_approval and autonomy_mode != "AUTO":
        -> WAITING  (写 output.reason, 等审批)
    result = tool.invoke(decision.args)                # 幂等键 = run_id + step_no
    call_model(purpose="self_check", ...)              # 可选轻量自检
```

**步数**由 `begin_step` 用一条条件 UPDATE 判定，不在循环里写 if：

```sql
UPDATE agent_runs SET step_count = step_count + 1
 WHERE id = :run AND status = 'RUNNING' AND step_count < max_steps
RETURNING step_count;          -- 0 行 = 超预算
```

没有「先读后写」的窗口，也不需要应用层配合。`CHECK` 保留作兜底。

**金额**不是靠「先用后查」，而是 **预留 + 结算**（0007）：

```
预留  UPDATE agent_runs SET reserved_cost = reserved_cost + :上界
        WHERE total_cost + reserved_cost + :上界 <= max_cost     -- 0 行 = 超预算
调用  在事务之外，不持有任何连接
结算  释放预留 -> 插入 llm_calls（触发器累加 total_cost）
```

为什么必须这样：LLM 调用发生在数据库事务之外，先花钱后记账，`CHECK` 只能拦「记账」拦不住「花钱」。更糟的是越界时触发器 UPDATE 失败会连 `llm_calls` 那行一起回滚 —— 供应商已经收了钱，库里查无此账。

预留额必须是真实花费的**上界**（字符数当输入上界、`max_output_tokens` 当输出上界，见 `agents/pricing.py`）。这条成立，结算就永远不会触发 `CHECK`，也就不存在「记账失败丢审计」。

### 4.3 【冻结】输出契约

所有 Agent 的输出走 Pydantic 校验，**不使用自由文本解析**。

```python
class ToolDecision(BaseModel):
    kind: Literal["tool", "final"]
    tool: str | None = None
    args: dict[str, Any] = {}
    output: dict[str, Any] | None = None
    reasoning: str                                   # 一句话，不是思维链
```

校验失败的处理：把 `ValidationError` 摘要作为 `purpose="repair"` 的输入重试一次，仍失败则 run 置 `FAILED(INVALID_OUTPUT)`。不允许"尽力解析"，那会把错误推迟到更贵的地方。

每个 Agent 的输出 schema 定义在 `agents/schemas.py`，与 §4.1 的输出列一一对应。schema 变更与迁移同批提交。

### 4.4 【冻结】三层预算

| 层 | 字段 | 默认 | 拦截方式 |
| --- | --- | --- | --- |
| 步数 | `step_count` / `max_steps` | 12 | Runtime 条件 `UPDATE ... WHERE step_count < max_steps`（0 行即超）；`CHECK` 兜底 |
| token | `total_tokens` / `max_tokens` | 200k | `CHECK agent_runs_tokens_within_budget`（软：事后拦，不阻断已经发生的调用） |
| 金额 | `total_cost` + `reserved_cost` / `max_cost` | 2.0 | 预留阶段的条件 `UPDATE`（硬：花不出去）+ `CHECK` 兜底 |

三层里只有金额是**事前硬拦**，因为只有它对应真实的外部付款。token 超限只能事后发现 —— 供应商已经生成完了 —— 所以它的作用是「及时发现异常 run」，不是「阻止花钱」。这个差别必须写清，否则会有人以为 token 预算能省钱。

用**独立数值列 + CHECK**，不用 `budget jsonb`。理由：JSONB 里的数值无法加 CHECK，等于放弃数据库层拦截；而这两列恰好是需要硬拦截的东西。评审稿的 `budget/spent jsonb` 方案在此修正。

`max_*` 的取值由 `autonomy_mode` 决定默认档，用户可在 `POST /tasks` 里覆盖，但受 workspace 级配额封顶（Phase 4 引入）。

### 4.5 【冻结】录制回放

- 每个 `llm_call` 存 `request` / `response`（脱敏后），`replay_key` 唯一。
- 回放模式（`XING_LLM_MODE=replay`）：命中 `replay_key` 直接返回存档 response，不发起真实调用，`cost` 记 0，`status='REPLAYED'`。
- 未命中的 `generate` 调用**允许降级为真实调用**；未命中的 `tool_decision` 调用**必须失败**——工具决策依赖真实模型行为，用假数据回放会掩盖 bug。

这套机制让「同一份计划能否复现」成为可测命题，也是后续所有 Agent 回归测试的基础设施。

**解析不过的输出不进回放。** 回放只证明「那次 HTTP 成功了」，不证明「那段文本能用」。
把一段连 JSON 都解析不了的回答留在缓存里，等于把所有重试固定成同一个失败 ——
报错位置连字符数都一样，看起来像模型固执，实际是在复读。所以 ``call_structured``
一旦解析失败（语法或 schema 都算），就先 ``forget_replay`` 把键摘掉；如果命中的
本来就是回放（``replayed=True``），还会立刻真问一次，且不消耗那唯一一次修复机会。
修复提示也换了口径：不再只说「第 20 行第 1 列解析失败」，而是把出错处附近的原文
一起给模型 —— 位置它改不动，原文它改得动。

**「合法 JSON」由解码保证，不是靠 prompt 求出来的。** 结构化调用（``call_structured``）
现在带 ``response_format={"type":"json_object"}``，把 ``temperature`` 压到 0.2。
起因是一次真实的双重失败：同一份 ``DeepenResult``，第一次把 ``evidence`` 数组
写进了 ``counterpoints`` 里（``"证据": [`` 这种数组里写键的写法根本不是 JSON），
第二次（修复调用）在 ``"claim":`` 换行之后直接写了一整句中文，没有引号 ——
报错是 ``Expecting value: line 20 column 1``。两次 ``finish_reason`` 都是 ``stop``，
输出是完整的，所以**不是**被 ``max_output_tokens`` 截断（这一点查了用量才确认：
1510 / 1049 tokens，上限 2000）。0.7 的温度对长嵌套 JSON 太松。

``response_format`` 是否被网关支持因服务商而异，所以 provider 里留了一条退化路径：
若报错信息里出现 ``response_format``，去掉该字段重发一次，退回「prompt 约束」的老办法。

还有一处**收得很窄**的容错：``runtime._quote_bare_values``。它只处理「键独占一行、
值裸写在下一行」这一种坏法，给值补上引号，而且补完必须重新通过 ``json.loads``
与 schema 校验才会被采用 —— 所以最坏情况是回到原来的报错，不会把能用的输出判死，
也不会把坏的拼成看起来合法的东西。值里已经带引号、或值本来是对象/数组中括号的，
一律不动：那些加引号会改错语义，宁可交回模型重答。补了就写 ``agent.json_repaired``
日志，不静默。

---

## 5. 工具层

### 5.1 【冻结】工具声明契约

工具是**代码常量**，不是数据库行：

```python
@dataclass(frozen=True)
class ToolSpec:
    name: str                       # 全局唯一，snake_case
    description: str
    args_schema: type[BaseModel]
    idempotent: bool                # 是否可安全重放
    requires_approval: bool         # 是否属"对外动作"
    cost_weight: int                # 预算权重，用于步数折算
    handler: Callable[..., Awaitable[Any]]
```

`requires_approval=True` 的判定标准只有一条：**该动作一旦执行，外部世界会看到**（发布、发邮件、调用第三方写接口）。内部写库一律不批。

### 5.2 幂等键

`idempotent=True` 的工具，调用时传 `idempotency_key = f"{run_id}:{step_no}"`，由 handler 自己保证幂等（通常是 `INSERT ... ON CONFLICT DO NOTHING` + 查回）。`idempotent=False` 的工具在重试时**必须**先人工确认，不进自动重试路径。

### 5.3 MCP 的位置

Phase 4 才考虑。届时 MCP 只是 `ToolSpec.handler` 的一种实现来源（`MCPServerToolProvider`），**不改变** `ToolSpec` 的形状，也不改变调用方的代码。当前不在接口里预留 MCP 抽象——没有第二个实现时，抽象只会变成负担。

---

## 6. Memory

### 6.1 【冻结】读写分离

| 方向 | Phase 1 | Phase 3 |
| --- | --- | --- |
| 读 | 必须有（只读聚合） | 加语义检索 |
| 写 | 不做 | 显式 `remember()`，带 TTL 与来源 |

只做写不做读，等于造了个没人用的仓库；只做读不做写，恰好是当前最需要的（Agent 需要历史上下文，但还没能力判断什么值得记）。

### 6.2 【冻结】Phase 1 不建 memory 表

`MemoryReader` 是一个**查询聚合器**，从已有表里按主题取历史：

| 读什么 | 从哪读 | 用途 |
| --- | --- | --- |
| 该选题的历史决策 | `decisions` + `decision_traces` | 避免重复选题、复用有效论据 |
| 该账号的历史表现 | `performance_snapshots` | 调整内容形式与发布时间 |
| 用户的历史反馈 | `feedback` | 修正风格与偏好 |
| 同策略下的既往内容 | `contents` + `content_versions` | 保持一致性、避免自我重复 |

接口固定为 `async def recall(workspace_id, topic_id, kinds, limit) -> MemoryBundle`，返回结构化 bundle 直接进 prompt。Phase 3 引入向量检索时，只换实现，不换签名。

`decision_traces` 就是 Memory 的写入位。Agent 做的每个非平凡判断，写一条 trace，这就是"记住"。

---

## 7. Review 降级

评审稿把 Review 列为五个专业 Agent 之一。实际拆开看，它由两类完全不同的工作组成：

| 工作 | 性质 | 处置 |
| --- | --- | --- |
| 格式、字数、必填项、敏感词、引用完整性 | 确定性规则 | `ReviewService`，纯 Python，无 LLM |
| 「这句话是否被给定证据支持」 | 需要判断 | 窄 Agent，输入 `(claim, evidence[])`，输出 `{supported: bool, evidence_ids: [], confidence: float}` |

【冻结】Review 不设"综合评审 Agent"。宽口径的"帮我看看这稿子好不好"无法验收、无法回归、成本高且结论不可用。拆成「确定性规则 + 窄事实核查」后，两者都能写测试。

---

## 8. Worker（Phase 1a）

### 8.0 【冻结】角色与租户上下文

`xing_app` 是 `NOBYPASSRLS`，而 worker 的**领取查询必须跨租户**（它要扫所有 workspace 的 `READY` 任务，此时还不知道会领到哪个租户）。因此 worker 不能复用应用角色。

新增 `scripts/worker_role.sql`，建 `xing_worker`：

```sql
CREATE ROLE xing_worker LOGIN PASSWORD '...' NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS;
GRANT USAGE ON SCHEMA public, app TO xing_worker;
GRANT SELECT, INSERT, UPDATE ON tasks, agent_runs, llm_calls, productions TO xing_worker;
GRANT SELECT ON workspaces, workspace_members TO xing_worker;
GRANT EXECUTE ON FUNCTION app.is_workspace_member(uuid) TO xing_worker;
```

这是本项目唯一一处有意绕过 RLS 的地方，因此配套三条硬规则：

1. `xing_worker` **不给** `users` / `approvals` / `feedback` 的权限。跑任务不需要读审批与用户资料。
2. worker 拿到任务后，`task.workspace_id` 是它后续所有查询的**唯一租户上下文**，必须显式传参。现有 7 个 service 的签名都要求 `workspace_id`，天然满足。
3. worker **不提供 HTTP 接口**，只接受队列驱动。绕过 RLS 的能力不外露给请求侧。

`rlscheck` 需同时验证：`xing_worker` 读 `users` 被拒；`xing_app` 读 `agent_runs` 有租户隔离。

### 8.1 领取

```sql
WITH claimed AS (
    SELECT id FROM tasks
     WHERE status = 'READY'
       AND available_at <= now()
       AND (assigned_agent IS NULL OR assigned_agent = :agent)
     ORDER BY priority DESC, available_at
     LIMIT 1
       FOR UPDATE SKIP LOCKED
)
UPDATE tasks t
   SET status = 'RUNNING',
       locked_by = :worker_id,
       locked_at = now(),
       lease_expires_at = now() + interval '90 seconds',
       lease_generation = t.lease_generation + 1,
       started_at = COALESCE(t.started_at, now())
  FROM claimed
 WHERE t.id = claimed.id
RETURNING t.*, t.lease_generation AS my_generation;
```

领取只扫 `READY`。`RETRYING` 不参与领取——它由推进循环转成 `READY` 后才可被抢（§8.2 第 1 条）。这样「哪个状态可领」只有一个答案，不会出现两处判断不一致。

`SKIP LOCKED` 让多 worker 天然不抢同一行；租约让 worker 崩溃后 90 秒自动可回收。二者组合不需要任何分布式锁。

`RETURNING` 里的 `my_generation` 必须由 worker 保存下来，之后每次写入都要带它（§8.4）。**忘记保存 = fencing 全部失效**，而且不会报错，只是偶尔丢一次结果。

### 8.2 三个周期性动作

实现上是**一个循环 + 时间戳节流**（`worker/main.py`），不是三个独立协程：三者都要写
同一张 `tasks` 表，分开跑反而要处理互相等待，单循环更容易在测试里驱动。

1. **推进**（每 5 秒）：`UPDATE tasks SET status='READY' WHERE status='RETRYING' AND available_at <= now()`。
2. **续租**（执行期间每 30 秒）：`UPDATE tasks SET lease_expires_at = now() + 90s WHERE id = :id AND locked_by = :worker_id AND lease_generation = :gen AND status = 'RUNNING'`。返回 0 行说明租约已被他人接管，**立即中止执行**，不再写任何业务数据。
3. **回收**（每 30 秒）：`status='RUNNING' AND lease_expires_at < now()` 按重试额度分两条语句 —— 额度耗尽直接 `FAILED`，否则 `RETRYING` + `retry_count += 1`。两者都清空 `locked_by`/`locked_at`/`lease_expires_at`，并 `lease_generation = lease_generation + 1`。

三条都是单条 UPDATE，不需要锁表，也不需要外部调度器。

### 8.3 收尾：fencing 的唯一执行点

`queue.finalize()` 是**唯一**能把任务带到终态的入口。它同时做三件事：写状态、清租约、校验代次。

```python
UPDATE tasks
   SET status = :status,
       locked_by = NULL, locked_at = NULL, lease_expires_at = NULL, ...
 WHERE id = :task_id
   AND locked_by = :worker_id
   AND status = 'RUNNING'
   AND lease_generation = :gen;      -- ← 少了这一行，前三个条件都挡不住旧代次
```

返回 `rowcount == 1` 才算成功。`0` 行不是异常，是正常的并发结果：执行期间租约被回收、任务已被他人接管，本次结果必须**丢弃**。

为什么不能"读出来改属性再 commit"：ORM 那种写法生成的 UPDATE 只带主键。§2.2 里第 1 个场景（A 卡住 → B 接管 → A 恢复）就是被这个写法放过去的，而状态机守卫无法发现 —— 它看到的是合法的 `RUNNING→SUCCEEDED`。

### 8.4 Phase 1a 的里程碑（已完成）

先不接 LLM。用一个 `EchoHandler`（读 `input`，写 `output`，睡 200ms）把「领取 → 执行 → 续租 → 完成 → 依赖推进」全链路跑通。**LLM 接进来之后，这条链路的问题会被误判成模型问题**，所以必须先单独验证。

交付物：

| 项 | 位置 |
| --- | --- |
| 迁移 | `0005_agent_layer`（表与状态机）+ `0006_task_lease_fencing`（fencing 与依赖初始化） |
| 队列 | `worker/queue.py`：`claim_one` / `heartbeat` / `reclaim_expired` / `promote_retrying` / `finalize` |
| 执行 | `worker/runner.py`：handler + 心跳 + 租约竞速 |
| 角色 | `scripts/worker_role.sql`：`xing_worker`（BYPASSRLS） |
| 契约 | `tests/sql/agent_check.sql`：状态机 64 组合 + 三种创建顺序 + fencing |
| 并发 | `tests/test_worker.py`：12 worker × 20 任务 + 旧代次写入被拒 |

### 8.5 进程心跳：谁在消费队列

队列是**拉**模型，所以「worker 挂了」和「worker 在忙」在数据上完全一样：任务都是
`READY`，没有 `started_at`，没有任何错误。没有外部观察者时，这个状态会一直持续到
界面超时，而超时只说「没等到」，不说原因。

worker 每 5 秒往 Redis 写一个带 TTL 的键（`cache/heartbeat.py`）：

```
SET xing:worker:<worker_id> '{"worker_id":...,"agent":...,"beat":...}' EX 30
```

**心跳由独立协程发，不跟着主循环**。主循环跑一个 expand 要 30~40 秒、跑一趟带补采的
检测更久，跟着它写心跳就会在「正在干活」的时候过期 —— 那是把正确状态报成故障，
比不报更糟。退出时（SIGTERM 走到 `finally`）主动 `DEL`，SIGKILL 则等 TTL 自然过期：
最坏 30 秒的误报方向是「还有人活着」，保守但不危险。

读侧有两个出口，都只是把心跳和队列放在一起看：

| 出口 | 给出什么 |
| --- | --- |
| `GET /workspaces/{ws}/queue/status` | `workers_online` / `queued` / `running` / `oldest_wait_s` / `stalled` |
| `GET /topics/{id}/chain` | 每环 `waiting_s` + 全局 `workers_online` |

`stalled = queued > 0 and workers_online == 0` 是派生结论，不是新状态：它不过是把
「有东西等着被消费」和「没有人在消费」两句话连起来。界面据此把「执行中」改成
「排队 2 分 10 秒，没人领取」，这也修正了一个长期的语义错误 —— `READY` 从来不是
「执行中」，它只是「等着被执行」。

> 心跳解决的是**可见性**，不是吞吐。当前仍是单进程单任务串行：一条 expand 排在
> 一条 deepen 后面就得等。要提吞吐得开并发池或按 agent 拆进程，那时
> `worker_id` 天然是多值的，接口不需要改。

---

## 9. 代码结构

评审稿提出 `src/xing/agents/` 下 13 个子目录。实际按当前代码量，13 个文件、零子目录：

```
src/xing/agents/
    __init__.py
    pricing.py        # 价格表 + 预留额上界计算
    provider.py       # LLM 供应商适配（stub / openai 兼容）
    runtime.py        # 预算记账、录制回放、llm_calls 写入、call_model/call_structured
    upstream.py       # 读 depends_on 各任务 output，只取指针
    schemas.py        # 全部输出 Pydantic + 对应 shape

    # 一条 goal → 内容（§13）
    planner.py        # 计划分解
    researcher.py     # 子任务研究（窄检索）
    writer.py
    reviewer.py       # 窄事实核查，非综合评审

    # 一个事件/方向 → 选题（§14）
    expander.py       # 采舆论 → 抽立场 → 生成引用立场的候选
    opinion.py        # 舆论检测闸门
    scorer.py         # 打分排序（先 tier 再 overall）
    deepener.py       # 深入取证

src/xing/search/
    __init__.py
    base.py           # Searcher 协议 + SearchDoc / SearchHit
    stub.py
    tavily.py
    qwen.py           # 走 runtime.call_model(web_search=True)

src/xing/worker/
    __init__.py
    main.py           # 领取循环
    runner.py         # 执行、心跳、租约
```

【冻结】不预建 `memory/`、`evaluation/`、`policy/`、`observability/` 等空目录，也不建 `tools/`
（没有第二实现时的工具抽象是负担）。目录跟着代码长；先摊开架构图再填代码，
最后产出的是长得像架构的空壳。

---

## 10. Roadmap

| 阶段 | 交付物 | 验收标准 | 明确不做 |
| --- | --- | --- | --- |
| **0**（已完成） | 19 表 ORM、服务层零 SQL | `ruff` + `mypy` + `ormcheck` 空 diff | — |
| **1a**（已完成） | `0005`/`0006` 迁移、`worker/`（queue/runner/handlers/main）、`xing_worker` 角色 | 12 worker × 20 任务无重复执行；租约过期可回收；旧代次写入被 fence；三种创建顺序都推进正确 | 不接 LLM |
| **1b**（已完成） | `0007` 迁移、`agents/`（pricing / provider / runtime / writer）、`write_content` handler、录制回放 | 单 Agent 产出 `contents` 草稿且审计可对账；超预算 `BUDGET_EXCEEDED` 且无残留；同 prompt 回放 `cost=0` | 不做多 Agent 协作 |
| **2**（已完成） | `0008` 迁移、`worker/reaper.py`、`agents/`（schemas / upstream / planner / researcher / reviewer）+ writer 改造 | 一条 goal 全链路产出待审内容（4 个任务、4 个 run）；子 run 成本按 `parent_run_id` 可聚合；计划重跑不重复建任务；上游失败级联取消 | 不做工具注册表、不做 Memory 写入（见 §13.6） |
| **3** | `MemoryReader`、`ReviewService`、反馈闭环 | Agent 能引用历史决策；规则评审可单测 | 不建 memory 表 |
| **4** | 工具扩展、workspace 配额、MCP（可选） | 配额超限可拒绝新 run | 不做通用编排 |

> 1b 的「`agent_runs` / `llm_calls` 拆分」与「预算三层列」已在 `0005` 一并落地（纯结构变更，
> 与 worker 无耦合），所以 1b 的工作收敛为运行时接线 + 第一个真实 Agent（已完成，见 §12）。

阶段间的硬门禁：每个阶段结束时 `ormcheck` 必须空 diff、`rlscheck` 必须覆盖全部新表、`pytest` 全绿。任一不满足不进下一阶段。

---

## 11. 验收口径的扩展

四道防线新增两道：

| 防线 | 覆盖什么 | 落点 |
| --- | --- | --- |
| `pytest` | 行为 | `tests/test_worker.py`：领取、并发不重复、重试分流、租约回收、非法迁移被拒 |
| `ruff` + `mypy` | 静态 | 不变 |
| `ormcheck` | 模型/库一致 | 新表必须同步映射，否则报 drop |
| `rlscheck` | 租户隔离 | `agent_runs` / `llm_calls` 已进策略清单；另验证无上下文时读不到 |
| `guardcheck` | 状态机 | `tests/sql/agent_check.sql`：穷举 64 组合 |
| `workercheck` | 并发 | `tests/test_worker.py::test_concurrent_workers_execute_each_task_once` |
| `fencecheck` | 租约代次 | `test_stale_generation_write_is_fenced_out` + `agent_check.sql` §4 |
| `dagcheck` | 任务图 | `agent_check.sql` §5/§6（`dedup_key` 唯一、失败级联取消）+ `tests/test_agent_pipeline.py` |

`agent_check.sql` 已挂进 `./dev.sh dbsmoke`，和 P0 约束一起跑。`guardcheck` 值得单独做的理由：状态机的正确性靠人眼审 15 条边不现实，而穷举 8×8=64 种组合只需要几秒钟（放行 15 条迁移边 + 8 条 `from = to` 的 no-op，其余 41 条必须被拒）。

`fencecheck` 单独列出来的理由：fencing 失效的表现是「偶尔丢一次结果」或「偶尔重复一次执行」，不报错、不留痕，靠人工测试几乎发现不了。它只能通过「显式构造旧代次」验证 —— 所以必须有一个专门走这条路径的用例。

---

## 12. 待决问题

以下 4 项**不阻塞** `0005`，但需在 Phase 2 前定：

1. **`contents.goal` 的来源**：是用户输入的一句话，还是从 `decisions` 推导？影响 Orchestrator 的入口签名。
2. **`WAITING` 的恢复协议**：审批通过后由谁把任务推回 `RUNNING`——API 直接改，还是写一条 `approvals` 让触发器推？倾向后者，与内容审批保持一致，但需要 `tasks` 上有对应的审批关联。
3. **`autonomy_mode` 的默认值**：目前定 `SEMI_AUTO`。若首批用户是单人创作者，`AUTO` 更省事，但发布动作仍需批。取决于产品定位。
4. **子 Agent 的成本归属**：`Reviewer` 作为子 run 执行时，成本记在子 run 还是父 run？当前设计记子 run（`parent_run_id` 可聚合），但 `tasks` 的成本统计需要递归，可能影响查询性能。

---

## 12. v1.1 实施修订（`0007` 落地）

冻结稿 v1 在编码中发现 6 处「会导致实际实现失败或审计失真」的问题，全部由
`0007_agent_runtime_contracts` 一次性收紧。**没有一条推翻架构决策**，都是契约修正。

| # | 问题 | 修订 | 落点 |
| --- | --- | --- | --- |
| 1 | 预算 CHECK 拦不住「事务外花钱」，越界还会把 `llm_calls` 明细一起回滚 | `reserved_cost` + 先预留后结算，预留额取真实花费上界 | `agents/pricing.py`、`runtime.call_model` |
| 2 | `step_count` 两处各加一次（传 `plan` 加 0、传默认 `step` 加 2） | 触发器不再碰；Runtime 条件 UPDATE 原子递增；删掉 `LlmPurpose.STEP` 与列默认值 | `runtime.begin_step`、触发器函数 |
| 3 | 无依赖任务、以及「上游早已成功」后创建的下游会永久 `PENDING` | `AFTER INSERT` 触发器与上游完成路径共用 `app.dependencies_satisfied()` | `0006` |
| 4 | 租约只有身份没有代次，回收后旧 worker 仍能写成功 | `lease_generation` + 所有写入带 `WHERE lease_generation = :gen` | `0006`、`queue.finalize` |
| 5 | worker 是 BYPASSRLS，写错 `workspace_id` 不会被策略挡住 | `(run_id, workspace_id)` 复合外键指向 `agent_runs(id, workspace_id)` | `0007` |
| 6 | `attempt` 同时被当 run 级和调用级，同一 step 重试就撞唯一键 | 拆成 run 级 `attempt` + 调用级 `call_seq`；唯一键改 `(run_id, call_seq)` | `0007` |
| 7 | `parent_run_id` 级联删除会带走子 run 的审计 | CASCADE → RESTRICT，并撤销 `xing_app` 对两张运行表的 DELETE | `0007` |
| 8 | `replay_key` 全局唯一，B 租户会撞上 A 已录制的键并读到别人响应 | 唯一范围改 `(workspace_id, replay_key)`，且只约束 `SUCCEEDED` | `0007` |

### 12.1 编码时才暴露的两件事

**`INSERT ... RETURNING` 看不到 `AFTER` 触发器改的值。** 它在触发器之前求值，
所以 `create_task` 必须 `flush` + `refresh` 才拿得到触发器推进后的 `READY`。

**回放命中的插入会撞自己。** `replay_key` 若对所有行唯一，「回放」这条路径
天然要写第二行同键记录，必撞。唯一性只能约束「录制」（`status='SUCCEEDED'`），
不能约束「回放」—— 这个 bug 是测试跑出来的，不是审出来的。

### 12.2 原遗留项（Phase 2 已处置）

| 项 | 处置 |
| --- | --- |
| 被取消的 run 停在 `RUNNING` | `worker/reaper.py::reap_stale_runs`，`main` 每 120 秒跑一次；只释放 `reserved_cost`，`total_cost` 不动 |
| 内容幂等靠 `metadata->>'task_id'` 顺序扫描 | 改 `contents.source_task_id` + 部分唯一索引，见 §13 |
| 工具调用的外部副作用无幂等键 | 仍未解决，但 Phase 2 不产生外部副作用（没有工具），推迟到第一个对外动作出现时 |

---

## 13. Phase 2：任务图编排（已落地）

Phase 2 的核心不是「多加几个 Agent」，而是把 §4.1 那句「Orchestrator 只派发」
兑现成机制：**编排走数据库任务图，不走进程内 agent 调 agent**。

```
plan_content ──▶ research_topic ──▶ write_content ──▶ review_content
   (派发)          (external_events)   (contents+v1)     (approvals=PENDING)
```

四个节点各自独立领取、独立租约、独立 run、独立预算。好处是断点续跑、并发、
审计、预算切分全部复用 Phase 1 已有机制；代价是任务图本身必须有正确性保证，
这就是 §13.1 那三条。

### 13.1 `0008` 的三条契约

| # | 缺口 | 契约 |
| --- | --- | --- |
| 1 | `plan_content` 重跑会长出第二条生产线 | `tasks.dedup_key` + 唯一索引 `(workspace_id, dedup_key) WHERE dedup_key IS NOT NULL`；子任务键 `{plan_task_id}:{step}`，插入 `ON CONFLICT DO NOTHING` 再查回 |
| 2 | 上游永久 `FAILED` 后下游永远 `PENDING` | `app.cancel_blocked_dependents()`：`AFTER UPDATE OF status WHEN NEW.status IN ('FAILED','CANCELLED')`，一条 `WITH RECURSIVE` 把全部后代置 `CANCELLED` + `error.code='UPSTREAM_FAILED'` |
| 3 | 「这篇内容由哪个任务产出」无法反查 | `contents.source_task_id` + 部分唯一索引，同时把写任务的幂等判定从 JSONB 顺序扫描换成索引查找 |

三个实现细节值得记下来：

- **递归取消用一条语句，不用触发器一跳一跳。** 跳一次等一次触发器，深度不可控，
  中间态（A 已取消、B 还没）会被并发查询看到。
- **`UNION` 而非 `UNION ALL`。** `depends_on` 没有成环约束，`A→B→A` 只能靠调用方
  自觉；`UNION` 让环退化成一个点，递归必然终止。
- **取消时一并写 `completed_at`**，否则下游会留下「终态但没完成时间」的行。

### 13.2 四个 Agent 的边界

| Agent | task_type | 模型调用 | 写的表 |
| --- | --- | --- | --- |
| Orchestrator | `plan_content` | 1×`plan` | `tasks`(insert)、`agent_runs.plan` |
| Researcher | `research_topic` | 1×`generate` | `external_events` |
| Writer | `write_content` | 1×`generate` | `contents`、`content_versions` |
| Reviewer | `review_content` | 1×`self_check` | `approvals`（仅 INSERT） |

三个刻意的收敛：

1. **Orchestrator 与 Planner 合成一个 handler。** 拆成两个 run 只多一次模型调用
   和一层失败点，没有审计收益 —— 计划本来就在那个 run 的 `plan` 列里。
2. **图的结构是代码常量，模型只填标题与三段 brief。** 通用 DAG 换来的只有两类
   新失败（漏步、成环），本就零收益。`schemas.Plan` 因此只有
   `content_title + research_brief / write_brief / review_focus`。
3. **Reviewer 不写 `decision_traces`。** 那张表的 `decision_id` 是 `NOT NULL`，
   审核不挂在任何 decision 上；硬塞假 decision 会污染整条决策链。

`xing_worker` 因此新增一条权限：`approvals` 的 `SELECT, INSERT`。**只给 INSERT
不给 UPDATE/DELETE** —— Agent 可以举手要求人审，但无法把 `PENDING` 改成
`APPROVED`，也无法删掉待办。这比「靠代码不写 UPDATE」强，是数据库层面的事实。

### 13.3 上游结果怎么传

`agents/upstream.py`：读 `depends_on` 各任务的 `output`，**只取指针**
（`content_id` / `event_id` / `summary`），正文永远从业务表回查。

正文进 `tasks.output` 会在 `tasks` / `contents` / `content_versions` 留三份副本，
而 `tasks.output` 会被任务列表接口直接返回给前端。

### 13.4 输出契约

`agents/schemas.py` 三个 Pydantic 模型 + `runtime.call_structured`：
解析失败给**一次** `purpose='repair'`，再失败 `FAILED(INVALID_OUTPUT)`。
不允许「尽力解析」—— 那只会把错误推迟到写进内容库之后。

`*_SHAPE` 是同一份契约给 stub provider 用的镜像（合成合法响应，让无 key 环境
也能跑通整条链路）。两份手写就会漂，所以 `tests/test_agent_contracts.py` 逐对
校验 `model_cls.model_validate(fill(shape))`，漂了就是测试红。

### 13.5 预算

不建共享账本。`plan_content` 按固定比例把预算分给四个 run，
合计只用 80%（plan 20 / research 15 / write 35 / review 10），留 20% 给重试：

| 情况 | 行为 |
| --- | --- |
| 单个 run 超预算 | 该 run `FAILED(BUDGET_EXCEEDED)`，任务不进重试 |
| 子任务重试 | 重新从自己的额度里花，可能累计超过父任务的分配额 |
| 成本聚合 | 按 `agent_runs.parent_run_id` 递归加总，子 run 挂在 plan run 下 |

**残余风险**：分配是软的，重试多了会超总预算。兜底是每个 run 的 `max_cost`
是硬上限 + `max_retries=3`。真正的总闸（workspace 配额）在 Phase 4。

### 13.6 Phase 2 明确不做

| 不做 | 理由 |
| --- | --- |
| 工具注册表（`ToolSpec`） | 四个 Agent 都是「一次调用 + 结构化输出 + 写库」，没有 `tool_decision` 循环；现在建就是 0 个真实工具的空壳 |
| `tool_decision` 步进循环 | 同上。每 Agent `max_steps=2`（主调用 + repair），失败就终态 |
| 审核不通过后的自动返修闭环 | DAG 表达不了环。停在 `approvals=REJECTED`，由人建 `revise_content` 任务 |
| 任意 DAG / 动态加节点 | 业务只有一条链路；动态图要先有环检测、拓扑校验、可视化，全是为不存在的需求付成本 |

### 13.7 验收

| 用例 | 断言 |
| --- | --- |
| `test_goal_produces_a_reviewable_draft_end_to_end` | 4 个任务全 `SUCCEEDED`；1 篇内容进 `REVIEW`；1 条 `approvals(PENDING)`；4 个 run 成本之和 == `llm_calls` 明细之和 |
| `test_replanning_the_same_task_does_not_duplicate_the_chain` | 计划重跑后子任务数不变、id 不变；两次 run 的 `attempt` 为 1 和 2 |
| `test_upstream_failure_cancels_dependents` | 上游 `BUDGET_EXCEEDED` 后下游 `CANCELLED` + `UPSTREAM_FAILED` |
| `test_reaper_closes_orphaned_runs_without_touching_spend` | 僵尸 run 转 `FAILED(ORPHANED_RUN)`，`reserved_cost` 清零，`total_cost` 保留 |
| `agent_check.sql` §5/§6 | `dedup_key` 同键插入被拒；失败级联取消两层后代 |

### 13.8 接真实模型后修正的三处

`stub` 只证明链路能跑通，证明不了「模型会不会用」。首次用真实模型
（`qwen3.7-flash`，compatible-mode）跑同一条 goal，暴露三个只有联网才会出现的
问题：

| 现象 | 根因 | 处置 |
| --- | --- | --- |
| 回复两个字也消耗 196 `completion_tokens`，正文被截断 | 思考型模型的思维链计入 completion 额度 | `LLM_ENABLE_THINKING=false` → `extra_body.enable_thinking`；默认 `None` 表示**不发送**该字段（OpenAI 不认，发了会 400） |
| 标题是 `面向CTO及企业AI项目负责人，撰写60秒短视频脚本。结构采` | 写作 Agent 把执行 brief 硬截 30 字当标题 | `Plan` 增 `content_title`，由 Planner 单独产出；写作侧兜底改成「取第一个短句」 |
| 首跑把编造的「某银行千万级坏账」当真实案例，审核仍给 85 PASS | Reviewer 只看到正文和审核要点，没有事实基线，无法判断「是否超出材料」 | 审核任务带 `research_task_id`，回查 `external_events.summary` 作事实基线；评分口径加「编造」「结论强度失真」两条硬规则 |

第二条尤其值得记下来：**同一份 brief，截断当标题这种缺陷在 stub 下永远看不见**，
因为 stub 的文本是哈希生成的，没有语义可以违反。工程正确性和内容质量是两条
独立的验收线，前者靠 stub + 并发测试，后者只能靠真模型跑。

修完之后同一条 goal 的判定从 `PASS 85` 变成 `FAIL 30`，两条 issue 精确指出了
编造案例与语气加强 —— 说明「事实基线 + 硬规则」这一层是必要的，不是锦上添花。

配置入口（`.env`，默认仍是 stub，`pytest` 不联网）：

```
LLM_PROVIDER=openai
LLM_MODEL=qwen3.7-flash
LLM_ENABLE_THINKING=false
```

---

## 附：本文对评审稿的修正清单

| # | 评审稿 | 本文 | 理由 |
| --- | --- | --- | --- |
| 1 | 新增 `agent_tasks` 表 | 不建 | 与 `tasks` 语义重叠，双写必不一致 |
| 2 | `tasks` 补 `run_id` | 不加，用 `agent_runs.task_id` | 一对多方向反了 |
| 3 | 9 张 `agent_*` 新表 | 净增 1 张 | 无写入方的表是猜测 |
| 4 | `budget` / `spent` 用 jsonb | 6 个数值列 + CHECK | JSONB 内数值无法硬拦截 |
| 5 | `Review Agent` | `ReviewService` + 窄核查 Agent | 宽口径评审无法验收 |
| 6 | `agents/` 13 个子目录 | 13 文件 + 零子目录 | 目录跟代码长，不预摊 |
| 7 | MCP 可选层 | Phase 4，且不改 `ToolSpec` | 没有第二实现时的抽象是负担 |
| 8 | Memory 读写同时建 | Phase 1 只读，不建表 | 先解决"读得到"，再解决"记得住" |

---

## 14. 选题链与检索层（v0.4，已落地）

§13 那条图是从「一句 goal」出发的。产品主干是另一条：从「一个事件/方向」出发，
先选题再写。两条链共用同一套 Runtime，差别只在图的形状与**有没有检索**。

```
expand_topics ──▶ check_opinion ──▶ score_topics ──▶ (人选定) ──▶ deepen_topic ──▶ write_content ──▶ review_content
   ↑ 3~5 次检索（必须）   ↑ 闸门，不通则不排产        ↑ 自动排产        ↑ 2~5 次检索（必须）
```

| Agent | task_type | 模型调用 | 检索 | 写的表 |
| --- | --- | --- | --- | --- |
| Expander | `expand_topics` | 1×`plan` + 1×`analyze` + 1×`generate`（缺阵营时 +1×`analyze`，补采的 query 查表生成） | 3~5 次（必须，并发），补采再 +≤3 次（并发） | `opinion_stances`、`topic_stances`、`topics`(CANDIDATE) |
| OpinionChecker | `check_opinion` | 1×`self_check` | — | `topics.brief.opinion`、`topics`(REJECTED) |
| Scorer | `score_topics` | 1×`self_check` | — | `topics`(分数/状态) |
| Deepener | `deepen_topic` | 1×`plan` + 1×`generate` | 2~5 次（严格） | `external_events`、`topic_events`、`topics.brief` |
| Writer | `write_content` | 1×`generate` | — | `contents`、`content_versions` |
| Reviewer | `review_content` | 1×`self_check` | — | `approvals`(INSERT) |

写作的篇幅按内容类型定档（v0.4）：`write_content` 原来固定 `max_output_tokens=1600`，
prompt 里又写着「不强制固定字数」，模型只看到一句「类型：VIDEO_SCRIPT」——于是
成稿普遍 700~1100 字。现在 `LENGTH_GUIDES` 给每种类型一组 `floor / target /
ceiling / max_tokens`（文章 1400 / 1800 / 2600 / 4000），写进 prompt，也写进
`content_versions.metadata`（`char_count`、`length_floor`、`short`）。

审核侧对同一个下限做**机检**：字数算得出来，不该让 judge 估。低于下限的 80%
直接 FAIL（没写完），80%~下限之间记一条 issue 并扣 15 分（收尾仓促）—— 两者
返工方式不同。下限优先取 metadata 里写作时记的值，改了档位不会让旧稿追溯性变短。

### 14.0 展开为什么是三次调用而不是一次

一次调用里「想角度 + 顺手编个理由」是模型最省力的路径，也是这一环最早出问题的地方：
切面看起来合理，但没有任何人这么说过。v0.3 把它拆成**先采舆论、再抽立场、最后才生成**，
三次调用之间各有一道机检，模型没有捷径可走。

```
plan(3~5 条 query，必须含 OPPOSE/VOICE/SEGMENT) ──▶ 真实检索（并发）
  ──▶ analyze(抽立场，每条必须带原文 quote) ──▶ 机检：quote 必须是材料子串
  ──▶ [缺声音阵营?] 查表生成 ≤3 条 query ──▶ 换一批材料 ──▶ analyze
  ──▶ generate(候选，每条必须给 stance_indexes) ──▶ 机检：引用落空即丢弃
```

第三段是可选的补采：第一轮抽完只覆盖了部分阵营时，针对缺失的
`SUPPORT` / `OPPOSE` / `CONDITIONAL` / `EXPERIENCE` 再采一轮。`OBSERVER` 不补
—— 旁观评论不是分歧的一方，为它多跑一轮只会带回跑题材料（真实跑踩过一次）。
补采用满时一个 run 是 4 次调用，所以 `max_steps=9`（每步留一次 repair）。

**补采的 query 不调模型**（v0.4）：缺口是确定的（缺哪一类声音），措辞也是确定的，
再问一次模型只换来 4~5 秒的空等。改成 `_SUPPLEMENT_QUERIES` 模板 + 种子核心词，
代价是 query 比模型写的窄 —— 实测模型规划能补回 3 条立场、模板补回 1 条，
但补采本来只是「换一组词再搜一次」，不是重新规划。核心词必须先剥掉种子的
`方向：`/`事件：` 标签，否则「方向」两个字会被当成主题。

禁止先验不靠提示词里的「不要编」，靠结构：

- `GroundedCandidate.stance_indexes` 是 `min_length=1` 的必填字段 —— 模型不引用就出不了 schema；
- `_ground()` 把引用为空或全部越界的候选丢掉，全丢则 `NO_GROUNDED_CANDIDATE` 终态；
- `topic_stances` 两侧都是 `(id, workspace_id)` 复合外键 —— 引用必须指向本租户真实存在的立场行。

检索为空（`NO_OPINION_SOURCE`）、立场全部机检不过（`NO_STANCE_EXTRACTED`）、
真实检索下立场不足 3 个或阵营不足 2 个（`OPINION_TOO_THIN`）都是不可重试终态：
没有舆论就不该出题，重试只会让模型开始编。

### 14.1 舆论检测是闸门，不是打分

`check_opinion` 是独立 task、独立 run，只看得到「立场池 + 候选」，看不到生成过程 ——
与打分独立同源：同一上下文自评必然偏松。判定三档：

| verdict | 含义 | 后果 |
| --- | --- | --- |
| `GROUNDED` | 切面确实对应池里的具体立场，且能指名受众 | 进入打分 |
| `WEAK` | 立场沾边但受众含糊，或与其他候选重复 | 进入打分，但排在 `GROUNDED` 之后 |
| `UNGROUNDED` | 切面不来自任何立场（模型先验 / 换皮） | `topics.status = REJECTED`，不排打分 |

**换皮判定不交给模型**。两个候选的 `(frozenset(stance_ids), audience)` 相同是结构事实，
机检算出来就降 `WEAK`，模型判 `GROUNDED` 也不改。判 `GROUNDED` 而 `audience_identified`
为假，同样降 `WEAK`。

它**不判真假**。prompt 里明写「不要判断事实真假、不要因为观点不正确就判 UNGROUNDED」，
真假是 `deepen_topic` 的事 —— 展开阶段能保证的只有「有人这么说过」，不能保证「说得对」。

覆盖度（材料数、来源数、阵营分布、`missing_camps`）由 SQL 聚合，不经模型，
写进 `brief.opinion.coverage`。真实跑出 `missing_camps=[CONDITIONAL, EXPERIENCE]`
时不补假数据，如实记录 —— 缺口要能被看见。

排序分两层：先按 `tier`（`GROUNDED`=0 / `WEAK`=1），再按 `overall`。
`REJECTED` 的候选不进打分集合，前端按钮直接禁用。

### 14.2 检索为什么是一个独立目录

`src/xing/search/`：`SearchDoc` 的字段直接对齐 `external_events` 的列，
所以「搜索结果 → 证据」不需要任何映射层。三个后端（stub / tavily / qwen）
前提不同，但调用方只看到 `Searcher.search(query) -> SearchHit`。

`qwen`（模型端联网）**走 `runtime.call_model(web_search=True)`**，不自己发 HTTP。
这不是洁癖：一次联网检索约 7000 input tokens，绕开 Runtime 就等于绕开预算。
两个配套代价都写进了 `runtime`：

- `make_replay_key` 升到 v2，带/不带联网是两个键（否则回放会把「没联网的回答」
  当成检索结果）；
- 预留额度加 `settings.search_context_chars`，因为检索正文是供应商注入的，
  不在我们发的 prompt 字符数里 —— 不加就违反「预留是花费上界」。

### 14.3 证据的可核性靠机检，不靠 judge

`topic_events.quote`、`opinion_stances.quote` 落库前都必须是 `external_events.summary`
的子串（折叠空白后）。模型可以改写 `claim`，但不能改写引文。这一步没有模型参与，
所以结论可复现 —— 交给 LLM judge 判断「意思是否相近」，恰恰最容易被相似措辞骗过。

两层对引文的处理不同，这是有意的：

- **立场层**（展开）：引文不过就丢这条立场，`dropped_stances` 计数。立场是选题的地基，
  地基里混进改写过的引文，后面所有「有据可依」都成了空话。
- **证据层**（深入）：引文不过不失败，用材料原文兜底成 BACKGROUND，并在
  `brief.search.degraded / rejected_quotes` 留痕。深入阶段已经有真材料在手，
  降级比中止更有用，但降级必须可见。

### 14.4 手工跑一遍

`scripts/run_topic_chain.py`（不进 CI）：建临时 workspace，跑完整七环，
打印 brief / 立场池 / 证据 / 成稿 / 成本。stub 只能证明管道接对了，真实跑的失败模式
（结构化输出不合格、检索无结果、引文全是改写、立场凑不够 3 个）只有在这里才会出现。

---

*修订记录：v1 2026-11-25 首版冻结；v1.1 同日，Phase 1b 落地后按实现回填（§11）；
v1.2 2026-11-26，选题链补上深入与检索层（§14）；
v1.3 同日晚，展开改为舆论驱动 + 新增舆论检测闸门（§14.0 / §14.1）；
v1.4 同日，展开补上缺失阵营补采（§14.0 第三段）；
v1.5 同日晚，补采改模板 query、检索并发、写作按类型定篇幅 + 审核机检（§14）。*
