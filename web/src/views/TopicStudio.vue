<script setup lang="ts">
import {
  AlertTriangle,
  CheckCircle2,
  FileText,
  Loader2,
  RotateCcw,
  Sparkles,
} from 'lucide-vue-next'
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'

import { strategiesApi } from '@/api'
import { errorMessage } from '@/api/client'
import type { Topic } from '@/api/types'
import ChainProgress from '@/components/ChainProgress.vue'
import TopicCard from '@/components/TopicCard.vue'
import TopicDrawer from '@/components/TopicDrawer.vue'
import { DEFAULT_STRATEGY } from '@/lib/strategy'
import { useChainStore } from '@/stores/chain'
import { useToastStore } from '@/stores/toast'
import { useWorkspaceStore } from '@/stores/workspace'

const chain = useChainStore()
const toast = useToastStore()
const wss = useWorkspaceStore()

const direction = ref('')
const count = ref(6)
const inspecting = ref<Topic | null>(null)
const drawerOpen = ref(false)
const strategyReady = ref(true)
const strategyBusy = ref(false)

const suggestions = [
  '企业给 AI 审批签字这件事，为什么没人愿意做第一个',
  'AI 让「经验」贬值之后，中层管理者还剩什么价值',
  '大模型价格战之后，做 AI 应用的公司靠什么活',
]

const canStart = computed(() => direction.value.trim().length >= 6 && !chain.busy)
const hasResults = computed(() => chain.topics.length > 0)
const contentPath = computed(() =>
  chain.content ? `/w/${wss.currentId}/contents/${chain.content.id}` : '/',
)

onMounted(() => {
  if (!chain.topics.length) void chain.loadRecent()
  void checkStrategy()
})

watch(
  () => wss.currentId,
  () => void checkStrategy(),
)

// 「选定」失败最常见的原因不是网络，而是这个工作区还没启用战略 ——
// 决策必须绑定战略版本，所以这里把它当成一个可修复的前置条件提示出来。
watch(
  () => chain.error?.code,
  (code) => {
    if (code === 'NO_ACTIVE_STRATEGY') strategyReady.value = false
  },
)

async function checkStrategy(): Promise<void> {
  const workspaceId = wss.currentId
  if (!workspaceId) return
  try {
    await strategiesApi.active(workspaceId)
    strategyReady.value = true
  } catch {
    strategyReady.value = false
  }
}

async function enableStrategy(): Promise<void> {
  const workspaceId = wss.currentId
  if (!workspaceId) return
  strategyBusy.value = true
  try {
    const created = await strategiesApi.create(workspaceId, '默认内容战略', DEFAULT_STRATEGY)
    await strategiesApi.activate(workspaceId, created.id)
    strategyReady.value = true
    toast.push('已启用默认战略，现在可以选定了；定位与读者可在「内容战略」里改', 'success')
  } catch (err) {
    toast.push(errorMessage(err), 'error')
  } finally {
    strategyBusy.value = false
  }
}

async function start(): Promise<void> {
  if (!canStart.value) return
  drawerOpen.value = false
  await chain.start(direction.value, count.value)
  if (chain.phase === 'failed' && chain.error) {
    toast.push(`${chain.error.message}（${chain.error.code}）`, 'error', 7000)
  }
}

function useSuggestion(text: string): void {
  direction.value = text
}

function pick(topic: Topic): void {
  drawerOpen.value = false
  void chain.choose(topic)
}

function inspect(topic: Topic): void {
  inspecting.value = topic
  drawerOpen.value = true
}
</script>

<template>
  <div class="mx-auto max-w-[1180px] px-8 py-9 lg:px-10">
    <header class="mb-7 flex flex-wrap items-end justify-between gap-4">
      <div>
        <p class="eyebrow">Topic Studio · 选题台</p>
        <h1 class="display mt-2 text-[29px] leading-tight">先有一个站得住的判断，再谈写得好。</h1>
        <p class="mt-2.5 max-w-[640px] text-[13px] leading-relaxed text-ink-2">
          给一个事件或一个方向，展开成若干互不重复的切面；评分后由你选一个，
          再检索材料、抽取证据，交给写作与审核。
        </p>
      </div>
      <div class="text-right">
        <p class="eyebrow mb-1.5">当前链路</p>
        <p class="text-[12.5px] text-ink-2">
          <template v-if="chain.stalled">
            <span class="inline-flex items-center gap-1.5 text-warn">
              <AlertTriangle :size="12" /> 已停住 · worker 不在
            </span>
          </template>
          <template v-else-if="chain.busy">
            <span class="inline-flex items-center gap-1.5 text-accent">
              <Loader2 :size="12" class="animate-spin" /> 正在执行
            </span>
          </template>
          <template v-else-if="chain.phase === 'done'">成稿已生成</template>
          <template v-else-if="chain.phase === 'failed'">链路中断</template>
          <template v-else-if="chain.phase === 'rejected'">没有候选通过检测</template>
          <template v-else-if="chain.phase === 'ready'">检测与评分完成</template>
          <template v-else>空闲</template>
        </p>
      </div>
    </header>

    <!-- 选定前的前置条件：决策必须绑定一条生效战略 -->
    <section
      v-if="!strategyReady"
      class="mb-4 flex flex-wrap items-center gap-3 rounded-[var(--radius-card)] border border-[#e8d9b8] bg-warn-soft px-4 py-3"
    >
      <AlertTriangle :size="15" class="shrink-0 text-warn" />
      <p class="min-w-[240px] flex-1 text-[12.5px] leading-relaxed text-[#7a5a1e]">
        <span class="font-medium">这个工作区还没有生效的内容战略。</span>
        展开不受影响；但「选定」会写一条决策，必须绑定战略版本。
      </p>
      <button class="btn-ghost btn-sm" :disabled="strategyBusy" @click="enableStrategy">
        <Loader2 v-if="strategyBusy" :size="12" class="animate-spin" />
        启用默认战略
      </button>
    </section>

    <!-- 种子输入 -->
    <section class="surface p-5">
      <div class="mb-3 flex items-center justify-between">
        <p class="eyebrow">Seed / 种子</p>
        <span class="text-[11px] text-muted">展开时附带一次背景检索，用于锚定时效理由</span>
      </div>

      <textarea
        v-model="direction"
        rows="3"
        :disabled="chain.busy"
        maxlength="600"
        placeholder="写一个事件，或者一个方向。越具体，切面越锋利。"
        class="field resize-none leading-relaxed disabled:bg-[#f4f1ea] disabled:text-muted"
        @keydown.ctrl.enter="start"
        @keydown.meta.enter="start"
      />

      <div class="mt-2 flex flex-wrap items-center gap-1.5">
        <span class="text-[11px] text-muted">试试</span>
        <button
          v-for="text in suggestions"
          :key="text"
          class="max-w-[280px] truncate rounded-full border border-line bg-white/70 px-2.5 py-1 text-[11.5px] text-ink-2 transition hover:border-[#cfc6b6] hover:text-ink"
          :disabled="chain.busy"
          @click="useSuggestion(text)"
        >
          {{ text }}
        </button>
      </div>

      <div class="mt-5 flex flex-wrap items-end justify-between gap-4 border-t border-line-soft pt-4">
        <div>
          <p class="eyebrow mb-2">候选数量</p>
          <div class="inline-flex rounded-md border border-line bg-[#f1eee7] p-0.5">
            <button
              v-for="n in [4, 6, 8]"
              :key="n"
              class="num rounded px-3 py-1 text-[12px] transition"
              :class="
                count === n ? 'bg-white text-ink shadow-sm' : 'text-muted hover:text-ink-2'
              "
              :disabled="chain.busy"
              @click="count = n"
            >
              {{ n }}
            </button>
          </div>
        </div>

        <div class="flex items-center gap-4">
          <p class="hidden max-w-[210px] text-[11px] leading-relaxed text-muted sm:block">
            展开只负责「切开」，不负责找事实。<br />证据在选定之后才检索。
          </p>
          <button class="btn-accent h-[38px] px-5" :disabled="!canStart" @click="start">
            <Loader2 v-if="chain.busy" :size="14" class="animate-spin" />
            <Sparkles v-else :size="14" />
            {{ chain.busy ? '执行中…' : '展开候选' }}
          </button>
        </div>
      </div>
    </section>

    <!-- 链路进度 -->
    <div v-if="chain.phase !== 'idle'" class="mt-4">
      <ChainProgress />
    </div>

    <!-- 成稿结果 -->
    <section
      v-if="chain.phase === 'done' && chain.content"
      class="mt-4 flex flex-wrap items-center gap-4 rounded-[var(--radius-card)] border border-[#cfe0d5] bg-ok-soft/70 px-5 py-4"
    >
      <CheckCircle2 :size="18" class="shrink-0 text-ok" />
      <div class="min-w-0 flex-1">
        <p class="text-[13.5px] font-medium text-ink">{{ chain.content.title }}</p>
        <p class="mt-0.5 text-[11.5px] text-ink-2">
          初稿已落库，共 {{ chain.content.current_version_number }} 个版本
          <span v-if="chain.review">
            · 审核
            <span :class="chain.review.verdict === 'PASS' ? 'text-ok' : 'text-bad'">
              {{ chain.review.verdict }} {{ chain.review.score }}
            </span>
            <span v-if="chain.review.issues.length">
              · {{ chain.review.issues.length }} 条问题待处理
            </span>
          </span>
        </p>
      </div>
      <RouterLink :to="contentPath" class="btn-primary">
        <FileText :size="14" /> 阅读全文
      </RouterLink>
    </section>

    <!-- 候选列表 -->
    <section class="mt-8">
      <div class="mb-4 flex items-end justify-between gap-4">
        <div class="flex items-baseline gap-3">
          <h2 class="display text-[17px] font-semibold">候选</h2>
          <span class="num text-[12px] text-muted">
            {{ hasResults ? `${chain.topics.length} 个` : '空' }}
          </span>
          <span v-if="hasResults" class="text-[11px] text-[#b0a89b]">
            推荐在前，其次按综合分
          </span>
        </div>
        <button
          v-if="hasResults && !chain.busy"
          class="btn-ghost btn-sm"
          title="重新读取最近候选"
          @click="chain.loadRecent()"
        >
          <RotateCcw :size="12" /> 刷新
        </button>
      </div>

      <div v-if="hasResults" class="grid gap-4 md:grid-cols-2">
        <TopicCard
          v-for="(topic, i) in chain.topics"
          :key="topic.id"
          :topic="topic"
          :index="i"
          :active="chain.selectedId === topic.id"
          :busy="chain.busy"
          class="animate-rise"
          :style="{ animationDelay: `${Math.min(i, 8) * 45}ms` }"
          @select="pick"
          @inspect="inspect"
        />
      </div>

      <!-- 首次进入：解释这条链在做什么，而不是只放一句「暂无数据」 -->
      <div v-else class="rounded-[var(--radius-card)] border border-dashed border-line px-8 py-12">
        <div class="mx-auto max-w-[560px] text-center">
          <div class="mx-auto mb-4 grid h-10 w-10 place-items-center rounded-full border border-line">
            <Sparkles :size="16" class="text-muted" />
          </div>
          <p class="display text-[16px]">还没有候选</p>
          <p class="mt-2 text-[12.5px] leading-relaxed text-muted">
            输入一个种子开始。展开会真实调用模型并计费，通常在 30~90 秒内返回
            {{ count }} 个不同切面。
          </p>
        </div>
      </div>
    </section>

    <p v-if="hasResults" class="mt-6 text-[11px] leading-relaxed text-[#b0a89b]">
      提示：选定后不可撤销，链条会自动跑完「深入 → 写作 → 审核」。
      中途可以在任务流里看到每一步的实际状态与耗时。
    </p>
  </div>

  <TopicDrawer
    :topic="inspecting"
    :open="drawerOpen"
    :busy="chain.busy"
    @close="drawerOpen = false"
    @select="pick"
    @reject="
      (topic) => {
        drawerOpen = false
        void chain.reject(topic, '选题台不感兴趣')
      }
    "
  />
</template>
