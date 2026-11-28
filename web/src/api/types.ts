/** 与后端 pydantic 契约一一对应的类型。字段名不改写 —— 改名等于再维护一套映射。 */

export type TopicStatus =
  | 'CANDIDATE'
  | 'RECOMMENDED'
  | 'SELECTED'
  | 'REJECTED'
  | 'EXPIRED'
  | 'USED'

export type TaskStatus =
  | 'PENDING'
  | 'READY'
  | 'RUNNING'
  | 'RETRYING'
  | 'SUCCEEDED'
  | 'FAILED'
  | 'CANCELLED'

export interface Workspace {
  id: string
  name: string
  status: string
  created_by: string
  created_at: string
  updated_at: string
}

export interface SearchQuery {
  query?: string
  purpose?: string
}

export interface EvidenceRef {
  event_id: string
  role?: string
  weight?: number
}

/** 展开阶段只有前五个字段，深入后补齐后半部分。stage 是判别式。 */
export interface TopicBrief {
  stage?: 'EXPANDED' | 'DEEPENED'
  angle?: string
  audience?: string
  thesis?: string
  why_now?: string
  seed_grounded?: boolean
  run_id?: string
  differentiator?: string
  outline?: string[]
  counterpoints?: string[]
  evidence_refs?: EvidenceRef[]
  score_reason?: string
  deepened_at?: string
  search?: {
    provider?: string
    degraded?: boolean
    doc_count?: number
    evidence_count?: number
    rejected_quotes?: number
    queries?: SearchQuery[]
    run_id?: string
  }
  /** 这个切面建立在哪些真实舆论立场上（展开阶段写入）。 */
  stances?: {
    label: string
    camp: string
    claim: string
    audience: string
  }[]
  /** 舆论检测结论：未通过（UNGROUNDED）的候选已经是 REJECTED，不可选。 */
  opinion?: {
    checked?: boolean
    verdict?: 'GROUNDED' | 'WEAK' | 'UNGROUNDED'
    reason?: string
    audience_identified?: boolean
    overlap_with?: number[]
    checked_at?: string
    coverage?: {
      doc_count?: number
      source_count?: number
      stance_count?: number
      camp_counts?: Record<string, number>
      missing_camps?: string[]
    }
  }
  /** 采集留痕：补了几轮、补了什么、补完还缺什么（展开阶段写入）。 */
  sources?: {
    provider?: string
    doc_count?: number
    stance_count?: number
    supplement?: {
      rounds?: number
      queries?: string[]
      new_docs?: number
      new_stances?: number
      duplicate_stances?: number
      missing_before?: string[]
      missing_after?: string[]
    }
  }
}

export interface Topic {
  id: string
  /** 产出这条候选的展开任务：取证过程按它查。 */
  seed_task_id: string | null
  primary_event_id: string | null
  title: string
  description: string | null
  status: TopicStatus
  trend_score: number | null
  relevance_score: number | null
  potential_score: number | null
  risk_score: number | null
  discovered_at: string
  selected_at: string | null
  brief: TopicBrief | null
}

export interface Task {
  id: string
  task_type: string
  status: TaskStatus
  priority: number
  input: Record<string, unknown>
  output: Record<string, unknown> | null
  error: { code?: string; message?: string; reason?: string } | null
  depends_on: string[]
  dedup_key: string | null
  started_at: string | null
  completed_at: string | null
  created_at: string
}

export interface ContentVersion {
  id: string
  version_number: number
  title: string
  body: string
  metadata: Record<string, unknown>
  change_summary: string | null
  created_by: string | null
  created_at: string
}

export interface Content {
  id: string
  topic_id: string | null
  decision_id: string | null
  title: string
  content_type: string
  status: string
  current_version_number: number
  lock_version: number
  created_at: string
  updated_at: string
  current_version: ContentVersion | null
}

export interface ExternalEvent {
  id: string
  source_type: string
  external_id: string
  title: string
  summary: string | null
  url: string | null
  source_published_at: string | null
  discovered_at: string
}

export interface Approval {
  id: string
  content_id: string
  stage: string
  status: string
  reviewer_id: string | null
  comment: string | null
  decided_at: string | null
  created_at: string
}

/** 审核 Agent 写进 tasks.output 的判定。 */
export interface ReviewVerdict {
  verdict: 'PASS' | 'FAIL'
  score: number
  issues: string[]
}

/** 后端派生的链路相位。与 docs/TOPIC_PIPELINE.md §1 的七环对应。 */
export type ServerChainPhase =
  | 'EXPANDING'
  | 'CHECKING'
  | 'SCORING'
  | 'AWAITING_SELECTION'
  | 'DEEPENING'
  | 'WRITING'
  | 'REVIEWING'
  | 'DONE'
  | 'FAILED'
  | 'REJECTED'

export type ChainStepStatus =
  | 'PENDING'
  | 'WAITING'
  | 'QUEUED'
  | 'RUNNING'
  | 'RETRYING'
  | 'DONE'
  | 'FAILED'
  | 'CANCELLED'
  | 'SKIPPED'

/** 一步的状态。status 是执行事实，retryable 是「能不能点重跑」。 */
export interface ChainStep {
  key: string
  label: string
  status: ChainStepStatus
  task_id: string | null
  error: { code?: string; message?: string } | null
  retryable: boolean
  /** WAITING 时已等了多久（秒）。后端算，因为「排队」和「没人来领」只能靠时长区分。 */
  waiting_s: number | null
}

/** GET /topics/{id}/chain：链路的唯一真相。 */
export interface ChainStatus {
  topic_id: string
  seed_task_id: string | null
  phase: ServerChainPhase
  steps: ChainStep[]
  content_id: string | null
  review: ReviewVerdict | null
  can_select: boolean
  select_blocked_reason: string | null
  failed_step: string | null
  /** 存活 worker 数。0 表示没人在消费队列：WAITING 不会自己往前走。null = 未知。 */
  workers_online: number | null
}

/** GET /workspaces/{ws}/queue/status：队列堆了多少，以及有没有人在消费。 */
export interface QueueStatus {
  /** 存活 worker 数。null = 心跳读不到（Redis 不可用），此时 stalled 不下结论。 */
  workers_online: number | null
  queued: number
  running: number
  oldest_wait_s: number | null
  stalled: boolean
}

export interface Strategy {
  id: string
  workspace_id: string
  name: string
  status: 'DRAFT' | 'ACTIVE' | 'PAUSED' | 'ARCHIVED'
  current_version_number: number
  lock_version: number
  created_at: string
  updated_at: string
  current_version: StrategyVersion
}

/** 不可变快照。改配置 = 新增一条，不是原地修改。 */
export interface StrategyVersion {
  id: string
  version_number: number
  strategy_config: Record<string, unknown>
  change_summary: string | null
  created_by: string
  created_at: string
}

/** 历史列表条目多一个 is_current：哪一版正在生效由服务端判定，前端不自己比。 */
export interface StrategyVersionHistoryItem extends StrategyVersion {
  is_current: boolean
}

export interface Page<T> {
  items: T[]
  next_cursor: string | null
}

export const TOPIC_STATUS_LABEL: Record<string, string> = {
  CANDIDATE: '候选',
  RECOMMENDED: '推荐',
  SELECTED: '已选定',
  REJECTED: '已否决',
  EXPIRED: '已过期',
  USED: '已使用',
}

export const TASK_STATUS_LABEL: Record<TaskStatus, string> = {
  PENDING: '等待依赖',
  READY: '待领取',
  RUNNING: '执行中',
  RETRYING: '重试中',
  SUCCEEDED: '成功',
  FAILED: '失败',
  CANCELLED: '已取消',
}

export const TASK_TYPE_LABEL: Record<string, string> = {
  expand_topics: '展开选题',
  check_opinion: '舆论检测',
  score_topics: '选题评分',
  deepen_topic: '深入选题',
  write_content: '撰写正文',
  review_content: '审核内容',
  plan_content: '规划内容',
  research_topic: '调研',
}

export const CONTENT_STATUS_LABEL: Record<string, string> = {
  DRAFT: '草稿',
  REVIEW: '待审核',
  APPROVED: '已通过',
  PRODUCING: '生成中',
  READY: '就绪',
  PUBLISHED: '已发布',
  ARCHIVED: '已归档',
}

export const TERMINAL_TASK_STATUS: TaskStatus[] = ['SUCCEEDED', 'FAILED', 'CANCELLED']

/* ── 取证轨迹 ──────────────────────────────────────────────────────
   一个任务「怎么做的」。四段各自独立：规划、检索、立场、模型步骤。 */

export type SourceKind = 'MEDIA' | 'UGC' | 'OFFICIAL' | 'VESTED' | 'UNKNOWN'

export type TraceLayer = 'MEDIA' | 'DISCUSSION' | 'SUPPLEMENT'

export interface TraceDoc {
  rank: number
  title: string
  url: string | null
  domain: string | null
  source_kind: SourceKind
  /** 命中的分类规则：`domain:zhihu.com` / `suffix:.gov.cn` / `unclassified` */
  kind_rule: string
  published_at: string | null
  kept: boolean
  drop_reason: 'EXCLUDED_PREVIOUS_ROUND' | 'DUPLICATE' | 'OVER_LIMIT' | null
  /** 按今天的分类表重算，仅在它与 `source_kind` 不一致时给出。
   *  轨迹是当时的快照，这个字段回答「规则变过之后会怎么判」。 */
  source_kind_now: SourceKind | null
  /** 现在会命中的规则（如 `pattern:lawyer`）。结论不告诉你该改哪一行，规则才告诉。 */
  kind_rule_now: string | null
}

export interface TraceQuery {
  seq: number
  layer: TraceLayer
  query: string
  target: string | null
  provider: string
  options: {
    topic?: string | null
    time_range?: string | null
    include_domains?: string[]
    exclude_domains?: string[]
  }
  status: 'OK' | 'EMPTY' | 'ERROR'
  latency_ms: number
  result_count: number
  new_count: number
  error: string | null
  docs: TraceDoc[]
}

export interface TraceRound {
  round: number
  queries: TraceQuery[]
  query_count: number
  result_count: number
  new_count: number
  source_mix: Partial<Record<SourceKind, number>>
}

export interface TraceStep {
  step_no: number
  call_seq: number
  purpose: string
  status: string
  model: string
  latency_ms: number | null
  input_tokens: number
  output_tokens: number
  cost: number
  system: string | null
  prompt: string | null
  response: string | null
  error: string | null
  truncated: boolean
}

export interface TraceStance {
  id: string
  label: string
  camp: string
  claim: string
  audience: string
  quote: string
  source_kind: SourceKind
  domain: string | null
  url: string | null
  title: string | null
}

export interface TaskTrace {
  task_id: string
  task_type: string
  status: TaskStatus
  attempt: number
  dedup_key: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
  error: { code?: string; message?: string } | null
  input: Record<string, unknown>
  output: Record<string, unknown> | null
  run_id: string | null
  agent: string | null
  goal: string | null
  run_status: string | null
  rounds: TraceRound[]
  steps: TraceStep[]
  stances: TraceStance[]
  summary: {
    query_count: number
    doc_count: number
    kept_count: number
    stance_count: number
    source_mix: Partial<Record<SourceKind, number>>
    ugc_ratio: number
  }
}

export const SOURCE_KIND_LABEL: Record<SourceKind, string> = {
  MEDIA: '媒体',
  UGC: '用户发言',
  OFFICIAL: '官方',
  VESTED: '利益相关',
  UNKNOWN: '未分类',
}

export const SOURCE_KIND_HINT: Record<SourceKind, string> = {
  MEDIA: '有人在编辑，但不是在表达自己的处境',
  UGC: '一个个具体的人在说话，带互动量',
  OFFICIAL: '政府、法院、统计、高校等机构口径',
  VESTED: '律所、厂商、咨询、公关稿，立场与生意绑定',
  UNKNOWN: '没认出域名，或能认出但不属于上面任何一类',
}

export const TRACE_LAYER_LABEL: Record<TraceLayer, string> = {
  MEDIA: '媒体层',
  DISCUSSION: '讨论层',
  SUPPLEMENT: '补采',
}

export const DROP_REASON_LABEL: Record<string, string> = {
  EXCLUDED_PREVIOUS_ROUND: '上一轮已有',
  DUPLICATE: '与本轮重复',
  OVER_LIMIT: '超出材料上限',
}
