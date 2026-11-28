<script setup lang="ts">
import {
  AlertTriangle,
  ChevronDown,
  Loader2,
  Pause,
  Play,
  RefreshCw,
  Route as RouteIcon,
} from 'lucide-vue-next'
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'

import { queueApi, tasksApi } from '@/api'
import { errorMessage } from '@/api/client'
import {
  TASK_STATUS_LABEL,
  TASK_TYPE_LABEL,
  type QueueStatus,
  type Task,
  type TaskStatus,
} from '@/api/types'
import StatusChip from '@/components/StatusChip.vue'
import { formatDate, formatDuration, queueWait, taskDuration } from '@/lib/format'
import { useWorkspaceStore } from '@/stores/workspace'

const ws = useWorkspaceStore()

const rows = ref<Task[]>([])
const queue = ref<QueueStatus | null>(null)
const loading = ref(false)
const error = ref('')
const auto = ref(true)
const expanded = ref<string | null>(null)
type StatusFilter = 'ALL' | 'QUEUED' | 'ACTIVE' | 'FAILED'
const statusFilter = ref<StatusFilter>('ALL')

let timer: number | undefined

/** 「排队」和「执行中」必须分开：READY 的任务没有任何进程在跑它。
 * 以前两者都算进「执行中」，于是 worker 挂了两小时，界面还在说执行中。 */
const QUEUED: TaskStatus[] = ['PENDING', 'READY']
const RUNNING: TaskStatus[] = ['RUNNING', 'RETRYING']

const visible = computed(() => {
  if (statusFilter.value === 'QUEUED') return rows.value.filter((t) => QUEUED.includes(t.status))
  if (statusFilter.value === 'ACTIVE') return rows.value.filter((t) => RUNNING.includes(t.status))
  if (statusFilter.value === 'FAILED') return rows.value.filter((t) => t.status === 'FAILED')
  return rows.value
})

const summary = computed(() => ({
  queued: rows.value.filter((t) => QUEUED.includes(t.status)).length,
  active: rows.value.filter((t) => RUNNING.includes(t.status)).length,
  failed: rows.value.filter((t) => t.status === 'FAILED').length,
}))

const filters = computed<Array<{ key: StatusFilter; label: string }>>(() => [
  { key: 'ALL', label: '全部' },
  { key: 'QUEUED', label: `排队 ${summary.value.queued}` },
  { key: 'ACTIVE', label: `执行中 ${summary.value.active}` },
  { key: 'FAILED', label: `失败 ${summary.value.failed}` },
])

async function load(): Promise<void> {
  if (!ws.currentId) return
  loading.value = true
  try {
    const [list, status] = await Promise.all([
      tasksApi.list(ws.currentId, 80),
      queueApi.status(ws.currentId).catch(() => null),
    ])
    rows.value = list
    queue.value = status
    error.value = ''
  } catch (err) {
    error.value = errorMessage(err)
  } finally {
    loading.value = false
  }
}

function startAuto(): void {
  stopAuto()
  if (!auto.value) return
  timer = window.setInterval(() => {
    // 页面在后台时不刷：任务不会因为看不见而停下，回来再拉一次即可。
    if (document.visibilityState === 'visible') void load()
  }, 4000)
}

function stopAuto(): void {
  if (timer) window.clearInterval(timer)
  timer = undefined
}

function pretty(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

watch(auto, startAuto)

onMounted(() => {
  void load()
  startAuto()
})

onUnmounted(stopAuto)
</script>

<template>
  <div class="mx-auto max-w-[1080px] px-8 py-9 lg:px-10">
    <header class="mb-7 flex flex-wrap items-end justify-between gap-4">
      <div>
        <p class="eyebrow">Pipeline · 任务流</p>
        <h1 class="display mt-2 text-[27px]">每个 Agent 都留下了执行记录。</h1>
        <p class="mt-2 text-[13px] leading-relaxed text-ink-2">
          任务图是这条产品线的骨架：展开、评分、深入、写作、审核各是一次独立领取的执行。
          失败会向下传播，不会静默跳过。
        </p>
      </div>
      <div class="flex items-center gap-2">
        <button
          class="btn-ghost btn-sm"
          :class="auto ? 'text-accent' : ''"
          @click="auto = !auto"
        >
          <Pause v-if="auto" :size="12" />
          <Play v-else :size="12" />
          {{ auto ? '自动刷新' : '已暂停' }}
        </button>
        <button class="btn-ghost btn-sm" :disabled="loading" @click="load">
          <RefreshCw :size="12" :class="{ 'animate-spin': loading }" /> 刷新
        </button>
      </div>
    </header>

    <div class="mb-4 flex items-center gap-1.5">
      <button
        v-for="item in filters"
        :key="item.key"
        class="rounded-full border px-3 py-1 text-[12px] transition"
        :class="
          statusFilter === item.key
            ? 'border-ink bg-ink text-paper-2'
            : 'border-line text-ink-2 hover:border-[#cfc6b6]'
        "
        @click="statusFilter = item.key"
      >
        {{ item.label }}
      </button>
    </div>

    <div
      v-if="error"
      class="rounded-[var(--radius-card)] border border-[#eccbc6] bg-bad-soft px-5 py-4 text-[12.5px] text-bad"
    >
      {{ error }}
    </div>

    <!-- 队列最要紧的一条事实：在排队，但没人来领。不说出来就只是「一直在转圈」。 -->
    <div
      v-else-if="queue && queue.stalled"
      class="mb-4 flex items-start gap-2.5 rounded-[var(--radius-card)] border border-[#ece0c4] bg-warn-soft px-5 py-4 text-warn"
    >
      <AlertTriangle :size="15" class="mt-0.5 shrink-0" />
      <div class="min-w-0 text-[12.5px] leading-relaxed">
        <p class="font-medium">
          <template v-if="queue.queued > 0">
            有 {{ queue.queued }} 条任务在排队，但没有 worker 在运行 —— 它们不会被执行。
          </template>
          <template v-else>
            有 {{ queue.running }} 条任务停在执行中，但 worker 心跳已经消失 —— 它们不会自己结束。
          </template>
        </p>
        <p class="mt-1 text-warn/85">
          <template v-if="queue.queued > 0">
            最早一条已等
            <span class="num">{{ formatDuration(queue.oldest_wait_s ?? 0) }}</span>
            。
          </template>
          启动消费进程即可继续：
          <code class="rounded bg-white/70 px-1.5 py-0.5 font-mono text-[11px]">
            docker compose --profile worker up -d worker
          </code>
        </p>
        <p v-if="queue.running > 0" class="mt-1 text-warn/85">
          重启后，租约已过期的执行会被回收重跑，不需要手工改状态。
        </p>
      </div>
    </div>

    <div v-else-if="loading && !rows.length" class="flex items-center gap-2 py-10 text-[12.5px] text-muted">
      <Loader2 :size="13" class="animate-spin" /> 读取中…
    </div>

    <div v-else-if="visible.length" class="surface overflow-hidden">
      <div class="grid grid-cols-[minmax(0,1.5fr)_120px_92px_110px_28px] items-center gap-3 border-b border-line-soft bg-[#f8f5ef] px-4 py-2.5 text-[10.5px] tracking-[0.08em] text-muted uppercase">
        <span>任务</span>
        <span>状态</span>
        <span class="text-right">耗时 / 排队</span>
        <span class="text-right">创建</span>
        <span />
      </div>

      <div v-for="task in visible" :key="task.id" class="border-b border-[#f0ece4] last:border-0">
        <button
          class="grid w-full grid-cols-[minmax(0,1.5fr)_120px_92px_110px_28px] items-center gap-3 px-4 py-3 text-left transition hover:bg-[#f8f5ef]"
          @click="expanded = expanded === task.id ? null : task.id"
        >
          <span class="min-w-0">
            <span class="block truncate text-[13px] text-ink">
              {{ TASK_TYPE_LABEL[task.task_type] ?? task.task_type }}
            </span>
            <span class="mt-0.5 block truncate font-mono text-[10.5px] text-[#b0a89b]">
              {{ task.dedup_key ?? task.id }}
            </span>
          </span>
          <StatusChip :status="task.status" kind="task" />
          <span
            class="num text-right text-[11.5px]"
            :class="QUEUED.includes(task.status) ? 'text-warn' : 'text-ink-2'"
          >
            <template v-if="QUEUED.includes(task.status)">
              等 {{ queueWait(task.created_at) }}
            </template>
            <template v-else>{{ taskDuration(task.started_at, task.completed_at) }}</template>
          </span>
          <span class="num text-right text-[11px] text-muted">
            {{ formatDate(task.created_at).slice(5) }}
          </span>
          <ChevronDown
            :size="14"
            class="text-[#b0a89b] transition-transform"
            :class="{ 'rotate-180': expanded === task.id }"
          />
        </button>

        <div v-if="expanded === task.id" class="bg-[#fbf9f4] px-4 pt-1 pb-4">
          <div v-if="task.error" class="mb-3 rounded-md border border-[#eccbc6] bg-bad-soft px-3 py-2">
            <p class="text-[11.5px] font-medium text-bad">
              {{ task.error.code ?? 'ERROR' }}
            </p>
            <p class="mt-0.5 text-[11.5px] leading-relaxed text-bad/90">
              {{ task.error.message ?? task.error.reason ?? '没有更多信息' }}
            </p>
          </div>

          <div class="grid gap-3 md:grid-cols-2">
            <div>
              <p class="eyebrow mb-1.5">输入</p>
              <pre class="max-h-56 overflow-auto rounded-md border border-line bg-white p-2.5 font-mono text-[10.5px] leading-relaxed text-ink-2">{{ pretty(task.input) }}</pre>
            </div>
            <div>
              <p class="eyebrow mb-1.5">输出 / 指针</p>
              <pre class="max-h-56 overflow-auto rounded-md border border-line bg-white p-2.5 font-mono text-[10.5px] leading-relaxed text-ink-2">{{ task.output ? pretty(task.output) : '—' }}</pre>
            </div>
          </div>

          <p class="mt-2 font-mono text-[10px] text-[#b0a89b]">
            id {{ task.id }}
            <span v-if="task.depends_on.length"> · 依赖 {{ task.depends_on.length }} 个前置任务</span>
          </p>
          <!-- 输入/输出只说明「收到什么、交出什么」，说明不了「中间怎么做的」。
               那部分在取证页：检索参数、材料去留、每一步的 prompt 原文。 -->
          <RouterLink
            :to="`/w/${ws.currentId}/trails/${task.id}`"
            class="btn-ghost btn-sm mt-3"
          >
            <RouteIcon :size="12" /> 取证过程
          </RouterLink>
        </div>
      </div>
    </div>

    <div v-else class="rounded-[var(--radius-card)] border border-dashed border-line px-8 py-12 text-center">
      <p class="display text-[15px]">{{ statusFilter === 'ALL' ? '还没有任务' : '这一类是空的' }}</p>
      <p class="mt-2 text-[12px] text-muted">
        {{
          statusFilter === 'ALL'
            ? '去选题台开始一次展开，任务会实时出现在这里。'
            : '切到「全部」看完整执行记录。'
        }}
      </p>
    </div>

    <p class="mt-4 text-[10.5px] text-[#b0a89b]">
      {{ TASK_STATUS_LABEL.READY }} = 已排产，但没有任何进程在跑它；
      {{ TASK_STATUS_LABEL.RUNNING }} 的判定来自租约，停留过久的执行会被回收成
      FAILED(ORPHANED)，不会一直占着队列。
      <span v-if="queue">
        当前在线 worker
        <span class="num" :class="queue.workers_online ? 'text-ok' : 'text-warn'">
          {{ queue.workers_online ?? '—' }}
        </span>
        个（每 5 秒一次心跳，30 秒未上报视为不在运行）。
      </span>
    </p>
  </div>
</template>
