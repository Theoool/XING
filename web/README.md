# XING Web

XING 的前端：把「种子 → 候选 → 评分 → 选定 → 深入 → 成稿」这条链做成可以点的界面。

技术栈：Vue 3.5 + Vite 6 + TypeScript + Pinia + Vue Router + Tailwind CSS 4。

## 跑起来

```bash
# 后端（另开一个终端）
docker compose up -d

# 前端
cd web
npm install
npm run dev          # http://localhost:5173
```

开发服务器把 `/api` 代理到 `http://localhost:8000`，因此前端永远同源，
不依赖后端 CORS，也不会把后端地址写死进代码。改后端地址：

```bash
VITE_API_PROXY=http://192.168.1.10:8000 npm run dev
```

生产构建默认打同源的 `/api/v1`；要指向别的地址就设 `VITE_API_BASE`。

```bash
npm run build         # 类型检查 + 打包到 dist/
npm run typecheck
```

## 真实模型

界面不自带模型开关，它跟随后端环境：

```bash
# .env
LLM_PROVIDER=openai
LLM_MODEL=qwen3.7-flash
LLM_ENABLE_THINKING=false      # 思考链会吃掉 max_output_tokens
SEARCH_PROVIDER=tavily
SEARCH_API_KEY=tvly-xxx
```

改完执行 `docker compose up -d --force-recreate worker api`。

不配也能点：默认 `stub` 会返回确定性假数据，用来验证链路是否打通，
但候选标题会一样、分数固定 —— 那不是产品行为，只是管线在跑。

注意：跑 `pytest` 前先 `docker compose stop worker`，否则后台 worker 会抢走
测试创建的任务；测试依赖 stub provider，不要在跑测试时把 `.env` 切成真实模型。

## 界面与接口的对应

| 页面 | 读 | 写 |
|---|---|---|
| 选题台 | `GET /tasks/{id}`（展开任务）、`GET /topics/{id}/chain`（七环轮询）、`GET /events/{id}`（证据原文） | `POST /tasks`（`expand_topics`）、`POST /topics/{id}/select`、`POST /topics/{id}/reject`、`POST /topics/{id}/chain/retry` |
| 内容库 | `GET /contents` | — |
| 内容详情 | `GET /contents/{id}`、`/versions`、`/approvals`、`GET /topics/{id}/chain`（审核结论） | `POST /contents/{id}/approve`、`/reject`、`POST /versions` |
| 任务流 | `GET /tasks`、`GET /queue/status`（排队 / 执行中 / worker 在线） | `POST /tasks/{id}/cancel` |
| 内容战略 | `GET /strategies`、`GET /strategies/{id}/versions` | `POST /strategies`、`PATCH /strategies/{id}`（改名）、`POST /strategies/{id}/versions`、`POST /strategies/{id}/activate` |
| 取证过程 | `GET /tasks/{id}/trail`（检索 → 立场 → 模型步骤） | — |

链路的关键约定：**前端只提交一次展开任务，之后只读状态**。展开收尾排检测，
检测通过排评分；`select` 在服务层排深入，深入完成后再排「写作 + 审核」。
七环走到哪一步由 `GET /topics/{id}/chain` 回答，界面不再去任务列表里
按 `task_type + input.topic_id` 找 —— 列表有 limit，任务一多那一步就「消失」，
表现是一直转圈。

**战略页的「保存」= 新增版本**：`strategy_config` 存在 `strategy_versions` 里，一条版本
不可修改。所以页面没有「保存修改」，只有「保存为 vN」；历史里的「以此为底稿」也只是把
旧配置载入编辑器，不改历史。配置编辑器上的 1500 字符预算条不是装饰：后端把整份配置
序列化后截断到 1500 字符塞进 expander / scorer 的提示词，超出部分模型看不到，而且截断点
落在 JSON 中间。`jsonb` 不保留插入顺序，所以能控的只有总量，不是「把重要的放前面」。

**工作区在路由里**：`/w/:wsId/studio|contents|contents/:id|tasks|strategy|trails/:taskId`。
切换工作区是一次导航，`RouterView` 的 key 绑 `wsId`，所以切租户必然重挂视图 ——
以前切完还显示上一个租户的正文，而且不报错。旧地址（`/studio`、`/contents/:id`）
和 `?ws=<id>` 会被守卫补成带工作区的地址，历史链接仍然可用。

**「执行中」只有一种含义**：`RUNNING` / `RETRYING`。`READY` 的任务没有进程在跑它，
所以它算「排队」。判断队列有没有人消费靠后端心跳（`workers_online`），不靠界面猜：
worker 每 5 秒写一次 Redis 心跳，30 秒不写就视为不在运行。没有 worker 时，选题台
显示「已排队 2 分 10 秒仍没人领取」，任务流页给出启动命令 —— 而不是转圈到十分钟超时。

## 目录

```
src/
  api/          客户端、problem+json 错误映射、类型、按域分组的接口
  stores/       workspace（租户镜像）/ chain（链路状态机）/ toast
  components/   侧栏、候选卡、详情抽屉、链路进度、状态徽标、评分条
  views/        选题台、内容库、内容详情、任务流、内容战略、取证过程
  router.ts     工作区路由与守卫（旧地址、?ws= 的统一入口）
  lib/format.ts 时间、分数、耗时的展示格式
  lib/strategy.ts 战略配置的字段约定、形状转换与提示词预算
  scripts/      shot.mjs —— 用本机 Chrome 截图并回收控制台错误
```

## 看一眼页面而不靠手点

`typecheck` 和 `build` 只证明「能编译」，证不了「能渲染」：取证页全是数据展示，
最容易死的地方在运行时（字段缺失、空数组取 `[0]`），这些在构建阶段都是绿的。

```bash
node scripts/shot.mjs "http://localhost:5173/w/<wsId>/trails/<taskId>" out.png

# 脚本跑出来的工作区属于另一个用户，浏览器默认的 dev 身份看不见它
# （RLS 正常工作，返回 404）。要验证渲染，显式换成那个身份：
node scripts/shot.mjs "http://localhost:5173/w/<wsId>/trails/<taskId>" out.png <X-User-ID>
```

第三个参数走后端 `deps.py` 的开发期 `X-User-ID` 覆盖。输出是 JSON：
`status` / `title` / 首屏文本 / **`problems`（console error、pageerror、
requestfailed）**。`problems` 为空才算渲染干净。

## 已知边界

- **种子只支持方向型**。后端 `expand_topics` 也支持 `seed_type=event`，
  但需要先有事件；界面还没做事件选择器。
- **内容类型还不能选**。`write_content` 支持 `ARTICLE` / `VIDEO_SCRIPT` 等篇幅档，
  但界面不传 `content_type`，一律按默认文章写。
- 刷新页面会丢掉前端链路状态（后端还在跑）。已选定的卡片可以直接点「看进度」
  重新接上，不需要重新决策。
- 轮询有超时（链路整体 15 分钟）。超时只表示「这次没等到」，
  任务本身可能还在跑，去任务流看真实状态。

