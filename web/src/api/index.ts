/** 按域分组的接口。路径参数一律走 workspace_id —— 后端从路径取租户，
 * 不从 header 取，前端照抄这个约定就不会造出「header 与路径不一致」的越权。 */

import { http } from './client'
import type {
  Approval,
  ChainStatus,
  Content,
  ContentVersion,
  ExternalEvent,
  Page,
  QueueStatus,
  Strategy,
  StrategyVersionHistoryItem,
  Task,
  TaskTrace,
  Topic,
  TopicStatus,
  Workspace,
} from './types'

export const workspacesApi = {
  list: () => http.get<Workspace[]>('/workspaces'),
  create: (name: string) => http.post<Workspace>('/workspaces', { name }),
}

/** 战略是版本化对象：没有「PUT 整条战略」，改配置只能新增一个版本。
 *
 * 所以这里刻意不提供 update —— 界面上「保存」= `addVersion`。 */
export const strategiesApi = {
  list: (ws: string) => http.get<Strategy[]>(`/workspaces/${ws}/strategies`),
  active: (ws: string) => http.get<Strategy>(`/workspaces/${ws}/strategy`),
  get: (ws: string, id: string) => http.get<Strategy>(`/workspaces/${ws}/strategies/${id}`),
  create: (ws: string, name: string, config: Record<string, unknown> = {}) =>
    http.post<Strategy>(`/workspaces/${ws}/strategies`, {
      name,
      strategy_config: config,
      change_summary: '界面创建',
    }),
  /** 只改名字。配置必须走 addVersion —— 否则历史版本会指向被改过的内容。 */
  rename: (ws: string, id: string, name: string) =>
    http.patch<Strategy>(`/workspaces/${ws}/strategies/${id}`, { name }),
  addVersion: (ws: string, id: string, config: Record<string, unknown>, summary: string) =>
    http.post<Strategy>(`/workspaces/${ws}/strategies/${id}/versions`, {
      strategy_config: config,
      change_summary: summary,
    }),
  versions: (ws: string, id: string, limit = 20) =>
    http.get<StrategyVersionHistoryItem[]>(
      `/workspaces/${ws}/strategies/${id}/versions?limit=${limit}`,
    ),
  activate: (ws: string, id: string) =>
    http.post<Strategy>(`/workspaces/${ws}/strategies/${id}/activate`),
}

export const topicsApi = {
  list: (ws: string, status?: TopicStatus, limit = 100) => {
    const query = new URLSearchParams({ limit: String(limit) })
    if (status) query.set('status', status)
    return http.get<Topic[]>(`/workspaces/${ws}/topics?${query}`)
  },
  get: (ws: string, id: string) => http.get<Topic>(`/workspaces/${ws}/topics/${id}`),
  select: (ws: string, id: string, rationale?: string) =>
    http.post<Topic>(`/workspaces/${ws}/topics/${id}/select`, {
      selected_option: null,
      rationale: rationale ?? null,
    }),
  reject: (ws: string, id: string, reason?: string) =>
    http.post<Topic>(`/workspaces/${ws}/topics/${id}/reject`, { reason: reason ?? null }),
}

export interface TaskCreateBody {
  task_type: string
  input?: Record<string, unknown>
  depends_on?: string[]
  assigned_agent?: string | null
  dedup_key?: string | null
  priority?: number
}

/** 链路状态由后端派生，前端只渲染。
 * 以前是拉任务列表再按 task_type + input.topic_id 找，任务一多就漏。 */
export const chainApi = {
  status: (ws: string, topicId: string) =>
    http.get<ChainStatus>(`/workspaces/${ws}/topics/${topicId}/chain`),
  retry: (ws: string, topicId: string, step: 'deepen' | 'write' | 'review') =>
    http.post<ChainStatus>(`/workspaces/${ws}/topics/${topicId}/chain/retry`, { step }),
}

export const tasksApi = {
  create: (ws: string, body: TaskCreateBody) => http.post<Task>(`/workspaces/${ws}/tasks`, body),
  list: (ws: string, limit = 50) =>
    http.get<Task[]>(`/workspaces/${ws}/tasks?limit=${limit}`),
  get: (ws: string, id: string) => http.get<Task>(`/workspaces/${ws}/tasks/${id}`),
  cancel: (ws: string, id: string, reason?: string) =>
    http.post<Task>(
      `/workspaces/${ws}/tasks/${id}/cancel${reason ? `?reason=${encodeURIComponent(reason)}` : ''}`,
    ),
  /** 取证轨迹：检索参数、回来的材料、模型每一步的原文。按需拉，不进轮询。 */
  trail: (ws: string, id: string) => http.get<TaskTrace>(`/workspaces/${ws}/tasks/${id}/trail`),
}

/** 队列状态：任务列表答不了「为什么它一直不动」，这条能。 */
export const queueApi = {
  status: (ws: string) => http.get<QueueStatus>(`/workspaces/${ws}/queue/status`),
}

export const contentsApi = {
  list: (ws: string, params: { topicId?: string; status?: string; limit?: number } = {}) => {
    const query = new URLSearchParams({ limit: String(params.limit ?? 50) })
    if (params.topicId) query.set('topic_id', params.topicId)
    if (params.status) query.set('status', params.status)
    return http.get<Content[]>(`/workspaces/${ws}/contents?${query}`)
  },
  get: (ws: string, id: string) => http.get<Content>(`/workspaces/${ws}/contents/${id}`),
  versions: (ws: string, id: string) =>
    http.get<ContentVersion[]>(`/workspaces/${ws}/contents/${id}/versions`),
  addVersion: (ws: string, id: string, body: string, changeSummary?: string) =>
    http.post<Content>(`/workspaces/${ws}/contents/${id}/versions`, {
      body,
      change_summary: changeSummary ?? null,
    }),
  approve: (ws: string, id: string, comment?: string) =>
    http.post<Content>(`/workspaces/${ws}/contents/${id}/approve`, { comment: comment ?? null }),
  reject: (ws: string, id: string, comment?: string) =>
    http.post<Content>(`/workspaces/${ws}/contents/${id}/reject`, { comment: comment ?? null }),
  approvals: (ws: string, id: string) =>
    http.get<Approval[]>(`/workspaces/${ws}/contents/${id}/approvals`),
}

export const eventsApi = {
  get: (ws: string, id: string) => http.get<ExternalEvent>(`/workspaces/${ws}/events/${id}`),
}

/** 选题展开：真正的入口是一次任务创建。任务体自带评分排产，
 * 所以前端只提交这一个请求，后面的环节由后端接力。 */
export function expandTopics(
  ws: string,
  seed: { direction: string; count: number },
): Promise<Task> {
  return tasksApi.create(ws, {
    task_type: 'expand_topics',
    input: { seed_type: 'direction', direction: seed.direction, count: seed.count },
  })
}

export { http }
export type { Page }
