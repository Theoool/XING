/** 选题链的状态机。
 *
 * 放在 Pinia 而不是组件里，是为了让「展开 → 深入 → 成稿」这条几分钟的链路
 * 不随路由切换而中断：用户点完「选定」去内容库转一圈，回来时进度还在。
 *
 * **状态不在这里推导**。七环走到哪一步由后端 ``GET /topics/{id}/chain`` 回答，
 * 这里只做三件事：轮询、把后端相位映射成界面相位、把失败翻译成一句人话。
 * 以前前端自己拉任务列表、按 task_type + input.topic_id 猜进度，任务一多就漏，
 * 而漏掉的表现是「一直转圈」—— 猜错比不知道更糟。
 */

import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'

import { chainApi, contentsApi, expandTopics, tasksApi, topicsApi } from '@/api'
import { ApiError } from '@/api/client'
import { formatDuration } from '@/lib/format'
import type {
  ChainStatus,
  ChainStep,
  ChainStepStatus,
  Content,
  ReviewVerdict,
  ServerChainPhase,
  Task,
  Topic,
} from '@/api/types'
import { TERMINAL_TASK_STATUS } from '@/api/types'

import { useWorkspaceStore } from './workspace'

export type ChainPhase =
  | 'idle'
  | 'expanding'
  | 'checking'
  | 'scoring'
  | 'ready'
  | 'deepening'
  | 'writing'
  | 'reviewing'
  | 'done'
  | 'failed'
  | 'rejected'

export interface ChainError {
  code: string
  message: string
}

export interface UiStep extends ChainStep {
  hint: string
}

const PHASE_MAP: Record<ServerChainPhase, ChainPhase> = {
  EXPANDING: 'expanding',
  CHECKING: 'checking',
  SCORING: 'scoring',
  AWAITING_SELECTION: 'ready',
  DEEPENING: 'deepening',
  WRITING: 'writing',
  REVIEWING: 'reviewing',
  DONE: 'done',
  FAILED: 'failed',
  REJECTED: 'rejected',
}

/** 七环的展示顺序与说明。顺序必须与后端 chain_service.STEP_LABELS 一致；
 * 说明只在这里，因为它属于界面语言，后端不需要知道。 */
const STEP_META: Array<{ key: string; label: string; hint: string }> = [
  { key: 'expand', label: '展开', hint: '采舆论、抽立场，切成互不重复的切面' },
  { key: 'opinion', label: '舆论检测', hint: '独立 run 判定每个切面在舆论里站不站得住' },
  { key: 'score', label: '评分', hint: '独立 run 打分，避免自评虚高' },
  { key: 'select', label: '选定', hint: '由你决定写哪一个' },
  { key: 'deepen', label: '深入', hint: '检索材料、抽取证据、补全论点' },
  { key: 'write', label: '成稿', hint: '按内容类型与篇幅档写作' },
  { key: 'review', label: '审核', hint: '事实出处与字数机检' },
]

const HINT_BY_KEY: Record<string, string> = Object.fromEntries(
  STEP_META.map((item) => [item.key, item.hint]),
)

const RETRY_LABEL: Record<string, string> = {
  deepen: '重跑深入',
  write: '重跑写作',
  review: '重跑审核',
}

const TIMEOUT = {
  expand: 10 * 60_000,
  pipeline: 15 * 60_000,
}

const TICK = 1500

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

function taskFailure(task: Task): ChainError {
  return {
    code: task.error?.code ?? `TASK_${task.status}`,
    message:
      task.error?.message ??
      task.error?.reason ??
      `任务${task.status === 'CANCELLED' ? '被取消' : '失败'}（${task.task_type}）`,
  }
}

function sortCandidates(rows: Topic[]): Topic[] {
  const weight = (t: Topic) =>
    (t.relevance_score ?? 0) + (t.potential_score ?? 0) + (t.trend_score ?? 0)
  return [...rows].sort((a, b) => {
    const rank = Number(b.status === 'RECOMMENDED') - Number(a.status === 'RECOMMENDED')
    return rank !== 0 ? rank : weight(b) - weight(a)
  })
}

function blankSteps(activeKey: string): UiStep[] {
  return STEP_META.map((meta) => ({
    key: meta.key,
    label: meta.label,
    status: (meta.key === activeKey ? 'RUNNING' : 'PENDING') as ChainStepStatus,
    task_id: null,
    error: null,
    retryable: false,
    waiting_s: null,
    hint: meta.hint,
  }))
}

export const useChainStore = defineStore('chain', () => {
  const ws = useWorkspaceStore()

  const phase = ref<ChainPhase>('idle')
  const steps = ref<UiStep[]>([])
  const topics = ref<Topic[]>([])
  const selectedId = ref<string | null>(null)
  const selectedTopic = ref<Topic | null>(null)
  const content = ref<Content | null>(null)
  const review = ref<ReviewVerdict | null>(null)
  const error = ref<ChainError | null>(null)
  const elapsed = ref(0)
  const seed = ref('')
  /** 后端对「能不能选定」的判定。界面用它解释为什么按钮是灰的，
   * 真正的拒绝在 select 的 409 上（界面不是防线）。 */
  const canSelect = ref(false)
  const selectBlockedReason = ref<string | null>(null)
  /** 存活 worker 数。0 意味着队列没人消费，WAITING 会一直 WAITING。 */
  const workersOnline = ref<number | null>(null)

  let token = 0
  let timer: number | undefined
  let startedAt = 0

  const busy = computed(() =>
    ['expanding', 'checking', 'scoring', 'deepening', 'writing', 'reviewing'].includes(phase.value),
  )

  const selectedCandidate = computed(
    () => topics.value.find((t) => t.id === selectedId.value) ?? selectedTopic.value,
  )

  /** 失败时可以点的重跑项。只有失败的任务才会被后端标成 retryable。 */
  const retryableSteps = computed(() =>
    steps.value
      .filter((step) => step.retryable)
      .map((step) => ({ key: step.key, label: RETRY_LABEL[step.key] ?? '重跑' })),
  )

  const failedStep = computed(() => steps.value.find((step) => step.status === 'FAILED') ?? null)

  /** 已排产但还没被领取的那一步。它是「还在跑」和「根本没人跑」的分界点。 */
  const waitingStep = computed(
    () => steps.value.find((step) => step.status === 'WAITING') ?? null,
  )

  /** 正在执行的那一步。心跳没了的时候，它只是一块墓碑，不是进度。 */
  const runningStep = computed(
    () => steps.value.find((step) => step.status === 'RUNNING') ?? null,
  )

  /** 退避重试中的那一步。转回 READY 靠 worker 的 promote_retrying，
   * 没有 worker 就一直躺在这里。 */
  const retryingStep = computed(
    () => steps.value.find((step) => step.status === 'RETRYING') ?? null,
  )

  /** 有人排队、有人卡在执行中、或者有人等着重试，却没有 worker 在跑：
   * 这条链路不会自己往前走。排队那批没人领；执行中那批是执行者已经死了，
   * 而租约回收也长在 worker 的主循环里 —— 同样没人收尸。
   * 以前这些情况在界面上都和「正在执行」一模一样，一直转圈，直到十五分钟超时。 */
  const stalled = computed(
    () =>
      workersOnline.value === 0 &&
      (waitingStep.value !== null || runningStep.value !== null || retryingStep.value !== null),
  )

  const currentHint = computed(() => {
    if (phase.value === 'idle') return '输入一个种子开始'
    if (phase.value === 'ready' && selectBlockedReason.value === 'OPINION_PENDING') {
      return '这批候选没有检测记录（早于检测环节），重新展开一次会补上'
    }
    if (phase.value === 'ready') return '检测与评分已完成，选一个开始写'
    if (phase.value === 'done') return '链路跑完，成稿已进入内容库'
    if (phase.value === 'rejected') return '没有一个候选通过舆论检测，换个更具体的种子'

    // 「卡住」必须排在「在跑」前面：没有心跳时，RUNNING 不是进度而是残留。
    if (stalled.value) {
      const queuedStep = waitingStep.value
      if (queuedStep) {
        const waited = formatDuration(queuedStep.waiting_s ?? 0)
        return `${queuedStep.label}已排队 ${waited} 仍没人领取 —— worker 没在运行，任务不会自己开始`
      }
      const dead = runningStep.value
      if (dead) {
        return `${dead.label}停在执行中，但 worker 心跳已消失 —— 它不会自己结束，重启 worker 后由租约回收接手`
      }
      const retrying = retryingStep.value
      if (retrying) {
        return `${retrying.label}已失败过一次，正等退避重试，但 worker 心跳已消失 —— 它不会自己重来`
      }
    }

    const active = runningStep.value
    if (active) return active.hint
    const retryingNow = retryingStep.value
    if (retryingNow) return `${retryingNow.label}这次失败，正在退避重试`
    const queued = waitingStep.value
    if (queued) {
      const waited = formatDuration(queued.waiting_s ?? 0)
      return `${queued.label}已排队 ${waited}，等待 worker 领取`
    }
    return '等待下一步'
  })

  function startClock(): void {
    startedAt = Date.now()
    elapsed.value = 0
    if (timer) window.clearInterval(timer)
    timer = window.setInterval(() => {
      elapsed.value = Math.round((Date.now() - startedAt) / 1000)
    }, 1000)
  }

  function stopClock(): void {
    if (timer) window.clearInterval(timer)
    timer = undefined
  }

  function reset(): void {
    token += 1
    stopClock()
    phase.value = 'idle'
    steps.value = []
    topics.value = []
    selectedId.value = null
    selectedTopic.value = null
    content.value = null
    review.value = null
    error.value = null
    elapsed.value = 0
    canSelect.value = false
    selectBlockedReason.value = null
    workersOnline.value = null
  }

  /** 切换工作区必须丢弃当前链路：否则深入完成的结果会写回上一个租户的视角。
   * 路由切换已经会重挂视图，这里再兜一层（同一组件内改 wsId 时也能生效）。 */
  watch(
    () => ws.currentId,
    () => reset(),
  )

  function applyStatus(status: ChainStatus): void {
    steps.value = status.steps.map((step) => ({
      ...step,
      hint: HINT_BY_KEY[step.key] ?? '',
    }))
    review.value = status.review
    canSelect.value = status.can_select
    selectBlockedReason.value = status.select_blocked_reason
    workersOnline.value = status.workers_online
    phase.value = PHASE_MAP[status.phase]
  }

  async function fetchTopics(workspaceId: string, ids: string[]): Promise<Topic[]> {
    if (!ids.length) return []
    const all = await topicsApi.list(workspaceId, undefined, 100)
    const wanted = new Set(ids)
    return sortCandidates(all.filter((t) => wanted.has(t.id)))
  }

  async function refreshTopic(workspaceId: string, topicId: string): Promise<void> {
    const fresh = await topicsApi.get(workspaceId, topicId).catch(() => null)
    if (!fresh) return
    selectedTopic.value = fresh
    topics.value = topics.value.map((t) => (t.id === fresh.id ? fresh : t))
  }

  async function loadContent(
    workspaceId: string,
    contentId: string | null,
    topicId: string,
  ): Promise<Content | null> {
    if (contentId) {
      const detail = await contentsApi.get(workspaceId, contentId).catch(() => null)
      if (detail) return detail
    }
    const rows = await contentsApi.list(workspaceId, { topicId, limit: 1 })
    return rows[0] ?? null
  }

  async function loadRecent(): Promise<void> {
    const workspaceId = ws.currentId
    if (!workspaceId || busy.value) return
    try {
      const rows = await topicsApi.list(workspaceId, undefined, 24)
      topics.value = sortCandidates(
        rows.filter((t) => t.status !== 'REJECTED' && t.status !== 'EXPIRED'),
      )
    } catch {
      // 最近的候选只是「接着看」，取不到不该弹错挡住主流程。
    }
  }

  function assertAlive(run: number): void {
    if (run !== token) throw new ApiError(0, 'ABORTED', '已开始新的链路')
  }

  async function waitTask(
    run: number,
    workspaceId: string,
    taskId: string,
    timeout: number,
  ): Promise<Task> {
    const begun = Date.now()
    for (;;) {
      assertAlive(run)
      const task = await tasksApi.get(workspaceId, taskId)
      if (TERMINAL_TASK_STATUS.includes(task.status)) return task
      if (Date.now() - begun > timeout) {
        throw new ApiError(0, 'TIMEOUT', '任务长时间没有结束，请到任务流里查看')
      }
      await sleep(TICK)
    }
  }

  /** 轮询链路状态，每一次都先渲染再判断 —— 进度是后端给的，不是本地推的。 */
  async function waitChain(
    run: number,
    workspaceId: string,
    topicId: string,
    stopWhen: (status: ChainStatus) => boolean,
  ): Promise<ChainStatus> {
    const begun = Date.now()
    for (;;) {
      assertAlive(run)
      const status = await chainApi.status(workspaceId, topicId)
      applyStatus(status)
      if (stopWhen(status)) return status
      if (Date.now() - begun > TIMEOUT.pipeline) {
        throw new ApiError(0, 'TIMEOUT', '链路长时间没有推进，请到任务流里查看')
      }
      await sleep(TICK)
    }
  }

  async function finish(
    run: number,
    workspaceId: string,
    topicId: string,
    status: ChainStatus,
  ): Promise<void> {
    if (status.phase === 'DONE') {
      content.value = await loadContent(workspaceId, status.content_id, topicId)
      await refreshTopic(workspaceId, topicId)
      assertAlive(run)
      phase.value = 'done'
      stopClock()
      return
    }
    if (status.phase === 'REJECTED') {
      throw new ApiError(0, 'CHAIN_REJECTED', '这个切面被舆论检测判为不成立')
    }
    const step = status.steps.find((item) => item.key === status.failed_step)
    const detail = step?.error ?? {}
    throw new ApiError(
      0,
      detail.code ?? 'CHAIN_FAILED',
      `${step?.label ?? '链路'}失败：${detail.message ?? '没有更多信息'}`,
    )
  }

  function fail(run: number, cause: unknown): void {
    if (run !== token) return
    phase.value = 'failed'
    stopClock()
    error.value =
      cause instanceof ApiError
        ? { code: cause.code, message: cause.message }
        : { code: 'UNKNOWN', message: cause instanceof Error ? cause.message : '未知错误' }
  }

  async function start(direction: string, count: number): Promise<void> {
    const workspaceId = ws.currentId
    if (!workspaceId || !direction.trim()) return

    const run = ++token
    stopClock()
    topics.value = []
    selectedId.value = null
    selectedTopic.value = null
    content.value = null
    review.value = null
    error.value = null
    seed.value = direction.trim()
    phase.value = 'expanding'
    steps.value = blankSteps('expand')
    startClock()

    try {
      const task = await expandTopics(workspaceId, { direction: seed.value, count })
      const done = await waitTask(run, workspaceId, task.id, TIMEOUT.expand)
      if (done.status !== 'SUCCEEDED') {
        throw new ApiError(0, taskFailure(done).code, taskFailure(done).message)
      }

      const ids = (done.output?.topic_ids as string[] | undefined) ?? []
      if (!ids.length) {
        throw new ApiError(0, 'EMPTY_EXPANSION', '这次展开没有产出候选，检查模型与检索配置')
      }

      // 展开只负责切面。检测与评分是它排下来的两条独立任务，
      // 必须由链路状态确认走完才能把候选交给人 —— 否则闸门只是个摆设。
      phase.value = 'checking'
      const status = await waitChain(
        run,
        workspaceId,
        ids[0],
        (s) => ['AWAITING_SELECTION', 'REJECTED', 'FAILED'].includes(s.phase),
      )

      topics.value = await fetchTopics(workspaceId, ids)
      if (!topics.value.length) {
        throw new ApiError(0, 'EMPTY_EXPANSION', '候选都在检测里被拒了，换个更具体的种子')
      }
      // 单个候选被拒不代表整批失败：看一眼还有没有能选的。
      const selectable = topics.value.filter((topic) => topic.status !== 'REJECTED')
      if (!selectable.length) {
        phase.value = 'rejected'
        error.value = {
          code: 'ALL_REJECTED',
          message: '这批候选都没通过舆论检测，换一个更具体的种子再试',
        }
        stopClock()
        return
      }
      if (status.phase === 'FAILED') {
        await finish(run, workspaceId, ids[0], status)
      }
      // 第一个候选被拒不代表整批被拒：只要还有能选的，就回到「等你挑」。
      phase.value = 'ready'
      error.value = null
      assertAlive(run)
      stopClock()
    } catch (cause) {
      fail(run, cause)
    }
  }

  /** 选定并一路等到成稿。已选定的选题可直接续跑（例如刷新页面后回来）。 */
  async function choose(topic: Topic): Promise<void> {
    const workspaceId = ws.currentId
    if (!workspaceId) return

    const run = ++token
    stopClock()
    error.value = null
    review.value = null
    content.value = null
    selectedId.value = topic.id
    selectedTopic.value = topic
    phase.value = 'deepening'
    startClock()

    // 已经选定过的选题不再调 select：状态机会以 409 挡住，
    // 而「从列表点进来接着看」是正常路径，不该报错。
    const needsSelect = topic.status !== 'SELECTED'

    try {
      if (needsSelect) {
        const selected = await topicsApi.select(workspaceId, topic.id, '选题台选定')
        assertAlive(run)
        selectedTopic.value = selected
        topics.value = topics.value.map((t) => (t.id === selected.id ? selected : t))
      }

      const status = await waitChain(run, workspaceId, topic.id, (s) =>
        ['DONE', 'FAILED', 'REJECTED'].includes(s.phase),
      )
      await finish(run, workspaceId, topic.id, status)
    } catch (cause) {
      fail(run, cause)
    }
  }

  /** 重跑失败的环节。链路的其余部分不动，只把这一步重新排进队列。 */
  async function retryStep(step: string): Promise<void> {
    const workspaceId = ws.currentId
    const topicId = selectedId.value
    if (!workspaceId || !topicId) return

    const run = ++token
    stopClock()
    error.value = null
    phase.value = step === 'deepen' ? 'deepening' : step === 'write' ? 'writing' : 'reviewing'
    startClock()

    try {
      await chainApi.retry(workspaceId, topicId, step as 'deepen' | 'write' | 'review')
      const status = await waitChain(run, workspaceId, topicId, (s) =>
        ['DONE', 'FAILED', 'REJECTED'].includes(s.phase),
      )
      await finish(run, workspaceId, topicId, status)
    } catch (cause) {
      fail(run, cause)
    }
  }

  async function reject(topic: Topic, reason: string): Promise<void> {
    const workspaceId = ws.currentId
    if (!workspaceId) return
    const updated = await topicsApi.reject(workspaceId, topic.id, reason)
    topics.value = topics.value
      .map((t) => (t.id === updated.id ? updated : t))
      .filter((t) => t.status !== 'REJECTED')
  }

  return {
    phase,
    steps,
    topics,
    selectedId,
    selectedTopic,
    selectedCandidate,
    content,
    review,
    error,
    elapsed,
    seed,
    busy,
    canSelect,
    selectBlockedReason,
    workersOnline,
    currentHint,
    retryableSteps,
    failedStep,
    waitingStep,
    runningStep,
    retryingStep,
    stalled,
    start,
    choose,
    retryStep,
    reject,
    reset,
    loadRecent,
  }
})
