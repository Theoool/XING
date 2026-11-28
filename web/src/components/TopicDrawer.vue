<script setup lang="ts">
import {
  ArrowUpRight,
  ExternalLink,
  FileSearch,
  Layers,
  Route as RouteIcon,
  ShieldQuestion,
  X,
} from 'lucide-vue-next'
import { computed, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'

import { eventsApi } from '@/api'
import type { EvidenceRef, ExternalEvent, Topic } from '@/api/types'
import ScoreMeter from '@/components/ScoreMeter.vue'
import StatusChip from '@/components/StatusChip.vue'
import { formatDate } from '@/lib/format'
import { useWorkspaceStore } from '@/stores/workspace'

const props = defineProps<{
  topic: Topic | null
  open: boolean
  busy?: boolean
}>()

const emit = defineEmits<{
  close: []
  select: [topic: Topic]
  reject: [topic: Topic]
}>()

// 证据原文按租户取：worker 是 BYPASSRLS，接口这一层必须带对 workspace。
const ws = useWorkspaceStore()
const resolved = ref<Array<{ ref: EvidenceRef; event: ExternalEvent | null }>>([])
const loadingEvidence = ref(false)

const brief = computed(() => props.topic?.brief ?? {})
const deepened = computed(() => brief.value.stage === 'DEEPENED')
const selected = computed(() => props.topic?.status === 'SELECTED')

const ROLE_LABEL: Record<string, string> = {
  PRIMARY: '主证',
  SUPPORTING: '旁证',
  CONTRADICTING: '反证',
  BACKGROUND: '背景',
}

const CAMP_LABEL: Record<string, string> = {
  SUPPORT: '支持',
  OPPOSE: '反对',
  CONDITIONAL: '有条件',
  EXPERIENCE: '亲历者',
  OBSERVER: '旁观',
}

const opinion = computed(() => brief.value.opinion ?? null)
// 补采留痕：缺口是「缺了哪个阵营」，补采是「为它又跑了一轮，补到什么」。
const supplement = computed(() => brief.value.sources?.supplement ?? null)
// 检测没跑完就不能选定（后端 409 OPINION_PENDING），界面不提供注定失败的按钮。
const pending = computed(() => !opinion.value?.checked)
const blocked = computed(
  () => props.topic?.status === 'REJECTED' || pending.value || opinion.value?.verdict === 'UNGROUNDED',
)
const opinionMeta = computed(() => {
  switch (opinion.value?.verdict) {
    case 'GROUNDED':
      return { label: '舆论成立', tone: 'border-[#cfe0d5] bg-ok-soft text-ok' }
    case 'WEAK':
      return { label: '切面偏弱', tone: 'border-[#e7d6bd] bg-warn-soft text-warn' }
    case 'UNGROUNDED':
      return { label: '未通过', tone: 'border-[#e8c4bd] bg-bad-soft text-bad' }
    default:
      return { label: '待检测', tone: 'border-line bg-transparent text-muted' }
  }
})

const PURPOSE_LABEL: Record<string, string> = {
  FACT: '事实',
  DATA: '数据',
  COUNTER: '反例',
  CONTEXT: '背景',
}

watch(
  () => [props.open, props.topic?.id] as const,
  async ([open, id]) => {
    document.body.style.overflow = open ? 'hidden' : ''
    if (!open || !id) return

    resolved.value = []
    const refs = props.topic?.brief?.evidence_refs ?? []
    if (!refs.length || !ws.currentId) return

    loadingEvidence.value = true
    try {
      const rows = await Promise.allSettled(
        refs.map((item) => eventsApi.get(ws.currentId as string, item.event_id)),
      )
      resolved.value = refs.map((item, i) => {
        const result = rows[i]
        return { ref: item, event: result?.status === 'fulfilled' ? result.value : null }
      })
    } finally {
      loadingEvidence.value = false
    }
  },
  { immediate: true },
)
</script>

<template>
  <Teleport to="body">
    <Transition
      enter-from-class="opacity-0"
      enter-active-class="transition duration-200"
      leave-to-class="opacity-0"
      leave-active-class="transition duration-150"
    >
      <div v-if="open && topic" class="fixed inset-0 z-90 flex justify-end">
        <div class="absolute inset-0 bg-[#17130f]/35 backdrop-blur-[2px]" @click="emit('close')" />

        <aside
          class="relative flex h-full w-full max-w-[560px] flex-col border-l border-line bg-paper shadow-[-24px_0_60px_rgba(23,19,15,0.16)]"
        >
          <header class="flex items-start gap-3 border-b border-line-soft px-6 py-5">
            <div class="min-w-0 flex-1">
              <div class="mb-2 flex items-center gap-2">
                <p class="eyebrow">Candidate / 候选</p>
                <StatusChip :status="topic.status" kind="topic" />
              </div>
              <h2 class="display text-[20px] leading-snug font-semibold">{{ topic.title }}</h2>
            </div>
            <button class="btn-ghost btn-sm !px-2" @click="emit('close')">
              <X :size="15" />
            </button>
          </header>

          <div class="flex-1 overflow-y-auto px-6 py-5">
            <div class="grid grid-cols-3 gap-4">
              <ScoreMeter label="相关性" :value="topic.relevance_score" />
              <ScoreMeter label="潜力" :value="topic.potential_score" tone="accent" />
              <ScoreMeter label="时效" :value="topic.trend_score" tone="gold" />
            </div>

            <section v-if="opinion?.checked" class="mt-5 rounded-[10px] border border-line bg-paper-2 px-4 py-3">
              <div class="flex items-center justify-between gap-3">
                <div class="flex items-center gap-1.5">
                  <ShieldQuestion :size="13" class="text-muted" />
                  <p class="eyebrow">舆论检测</p>
                </div>
                <span class="chip" :class="opinionMeta.tone">{{ opinionMeta.label }}</span>
              </div>
              <p
                v-if="opinion.reason && opinion.verdict !== 'GROUNDED'"
                class="mt-2 text-[12px] leading-relaxed text-ink-2"
              >
                {{ opinion.reason }}
              </p>
              <p v-if="opinion.coverage" class="mt-2 text-[11px] leading-relaxed text-muted">
                材料 {{ opinion.coverage.doc_count ?? 0 }} 篇 · 来源
                {{ opinion.coverage.source_count ?? 0 }} 个 · 立场
                {{ opinion.coverage.stance_count ?? 0 }} 条
                <span v-if="opinion.coverage.missing_camps?.length">
                  · 缺
                  {{
                    opinion.coverage.missing_camps
                      .map((camp) => CAMP_LABEL[camp] ?? camp)
                      .join('、')
                  }}
                </span>
              </p>
              <p v-if="supplement?.rounds" class="mt-1 text-[11px] leading-relaxed text-muted">
                为缺口补采 1 轮：新收材料 {{ supplement.new_docs ?? 0 }} 篇 · 从中抽到立场
                {{ supplement.new_stances ?? 0 }} 条
                <span v-if="supplement.missing_after?.length">
                  · 仍缺
                  {{
                    supplement.missing_after.map((camp) => CAMP_LABEL[camp] ?? camp).join('、')
                  }}
                </span>
              </p>
              <!-- 「这几个数字是怎么来的」不该靠猜：每条 query 的参数、每篇材料的
                   去留都在取证页里。没有 seed_task_id 的老数据不显示入口，而不是给个死链。 -->
              <RouterLink
                v-if="topic?.seed_task_id"
                :to="`/w/${ws.currentId}/trails/${topic.seed_task_id}`"
                class="mt-2 inline-flex items-center gap-1 text-[11px] text-accent transition hover:underline"
                @click="emit('close')"
              >
                <RouteIcon :size="11" /> 看这次检索的取证过程
              </RouterLink>
            </section>

            <section v-if="brief.stances?.length" class="mt-7">
              <div class="mb-3 flex items-center gap-1.5">
                <ShieldQuestion :size="13" class="text-muted" />
                <p class="eyebrow">舆论基座</p>
              </div>
              <ul class="space-y-2">
                <li
                  v-for="(stance, i) in brief.stances"
                  :key="i"
                  class="rounded-[10px] border border-line px-3.5 py-2.5"
                >
                  <div class="flex items-center gap-2">
                    <span class="chip border-line text-muted">
                      {{ CAMP_LABEL[stance.camp] ?? stance.camp }}
                    </span>
                    <span class="text-[13px] font-medium text-ink">{{ stance.label }}</span>
                  </div>
                  <p class="mt-1.5 text-[12px] leading-relaxed text-ink-2">{{ stance.claim }}</p>
                  <p class="mt-1 text-[11px] leading-relaxed text-muted">
                    谁在说：{{ stance.audience }}
                  </p>
                </li>
              </ul>
            </section>

            <section v-if="brief.thesis" class="mt-6">
              <p class="eyebrow mb-2">核心论点</p>
              <p
                class="border-l-2 border-accent pl-3.5 font-serif text-[16px] leading-[1.8] text-ink"
              >
                {{ brief.thesis }}
              </p>
            </section>

            <section class="mt-5 grid grid-cols-1 gap-4">
              <div>
                <p class="eyebrow mb-1">目标读者</p>
                <p class="text-[13px] leading-relaxed text-ink-2">
                  {{ brief.audience || '—' }}
                </p>
              </div>
              <div v-if="brief.angle">
                <p class="eyebrow mb-1">切入角度</p>
                <p class="text-[13px] leading-relaxed text-ink-2">{{ brief.angle }}</p>
              </div>
              <div v-if="brief.why_now">
                <p class="eyebrow mb-1">为什么是现在</p>
                <p class="text-[13px] leading-relaxed text-ink-2">{{ brief.why_now }}</p>
              </div>
              <div v-if="brief.differentiator">
                <p class="eyebrow mb-1">与同类内容的不同</p>
                <p class="text-[13px] leading-relaxed text-ink-2">{{ brief.differentiator }}</p>
              </div>
            </section>

            <section v-if="brief.outline?.length" class="mt-7">
              <div class="mb-3 flex items-center gap-1.5">
                <Layers :size="13" class="text-muted" />
                <p class="eyebrow">论证结构</p>
              </div>
              <ol class="space-y-2.5">
                <li
                  v-for="(line, i) in brief.outline"
                  :key="i"
                  class="flex gap-2.5 text-[13px] leading-[1.75] text-ink-2"
                >
                  <span class="num mt-[3px] shrink-0 font-serif text-[12px] text-[#b9b0a1]">
                    {{ String(i + 1).padStart(2, '0') }}
                  </span>
                  <span>{{ line }}</span>
                </li>
              </ol>
            </section>

            <section v-if="brief.counterpoints?.length" class="mt-7">
              <div class="mb-3 flex items-center gap-1.5">
                <ShieldQuestion :size="13" class="text-muted" />
                <p class="eyebrow">反例与回应</p>
              </div>
              <div class="space-y-2">
                <p
                  v-for="(line, i) in brief.counterpoints"
                  :key="i"
                  class="rounded-md border border-[#e8e0d0] bg-[#faf6ec] px-3 py-2.5 text-[12.5px] leading-[1.75] text-[#6b5a34]"
                >
                  {{ line }}
                </p>
              </div>
            </section>

            <section class="mt-7">
              <div class="mb-3 flex items-center justify-between">
                <div class="flex items-center gap-1.5">
                  <FileSearch :size="13" class="text-muted" />
                  <p class="eyebrow">证据 · {{ resolved.length }}</p>
                </div>
                <span v-if="loadingEvidence" class="pulse-dot text-[11px] text-muted">
                  读取来源…
                </span>
              </div>

              <p
                v-if="!resolved.length"
                class="rounded-md border border-dashed border-line px-3 py-4 text-[12px] leading-relaxed text-muted"
              >
                {{
                  deepened
                    ? '这一步没有留下可用的证据引用。'
                    : '还没有深入过。选定之后才会检索材料、抽取原文并挂上证据。'
                }}
              </p>

              <ul v-else class="space-y-2.5">
                <li
                  v-for="row in resolved"
                  :key="row.ref.event_id"
                  class="rounded-md border border-line-soft bg-white/60 px-3 py-2.5"
                >
                  <div class="mb-1 flex items-center gap-1.5">
                    <span
                      class="chip border-[#e6ded0] bg-[#f4efe4] px-1.5 py-0 text-[10px] text-[#7a6a4d]"
                    >
                      {{ ROLE_LABEL[row.ref.role ?? 'SUPPORTING'] ?? row.ref.role }}
                    </span>
                    <span class="truncate text-[12.5px] font-medium text-ink">
                      {{ row.event?.title ?? '来源不可用' }}
                    </span>
                  </div>
                  <p class="line-clamp-3 text-[12px] leading-[1.7] text-[#6a6257]">
                    {{ row.event?.summary ?? '这条证据指向的事件已经被删除，或不属于当前工作区。' }}
                  </p>
                  <div class="mt-1.5 flex items-center gap-2 text-[10.5px] text-muted">
                    <span v-if="row.event?.source_published_at">
                      {{ formatDate(row.event.source_published_at) }}
                    </span>
                    <a
                      v-if="row.event?.url"
                      :href="row.event.url"
                      target="_blank"
                      rel="noreferrer noopener"
                      class="ml-auto inline-flex items-center gap-1 text-accent hover:underline"
                    >
                      原文 <ExternalLink :size="10" />
                    </a>
                  </div>
                </li>
              </ul>
            </section>

            <section v-if="brief.search?.queries?.length" class="mt-7">
              <p class="eyebrow mb-2">检索记录</p>
              <div class="space-y-1.5">
                <div
                  v-for="(item, i) in brief.search.queries"
                  :key="i"
                  class="flex items-start gap-2 text-[11.5px] text-[#6a6257]"
                >
                  <span
                    class="chip shrink-0 border-line bg-[#f1eee7] px-1.5 py-0 text-[10px] text-muted"
                  >
                    {{ PURPOSE_LABEL[item.purpose ?? ''] ?? item.purpose ?? '检索' }}
                  </span>
                  <span class="leading-relaxed">{{ item.query }}</span>
                </div>
              </div>
              <p class="mt-2 text-[10.5px] text-muted">
                {{ brief.search.provider }} · 材料 {{ brief.search.doc_count ?? 0 }} 条 ·
                入库证据 {{ brief.search.evidence_count ?? 0 }} 条
                <span v-if="brief.search.rejected_quotes">
                  · 引文机检丢弃 {{ brief.search.rejected_quotes }} 条
                </span>
                <span v-if="brief.search.degraded" class="text-warn"> · 本次检索已降级</span>
              </p>
            </section>
          </div>

          <footer class="flex items-center gap-3 border-t border-line-soft bg-[#f8f5ef] px-6 py-4">
            <p class="flex-1 text-[11.5px] leading-relaxed text-muted">
              {{
                selected
                  ? '已选定，链条会自动深入并生成初稿。'
                  : '选定后会检索材料、抽取证据，再交给写作与审核。'
              }}
            </p>
            <button
              v-if="!selected"
              class="btn-ghost btn-sm"
              :disabled="busy"
              @click="emit('reject', topic)"
            >
              不要这个
            </button>
            <button
              v-if="!selected"
              class="btn-accent"
              :disabled="busy || blocked"
              @click="emit('select', topic)"
            >
              <template v-if="pending">检测还没完成</template>
              <template v-else-if="blocked">舆论检测未通过</template>
              <template v-else>选定这篇 <ArrowUpRight :size="14" /></template>
            </button>
            <!-- 已选定的题：重新接上链路看进度（刷新后前端状态会丢，后端还在跑）。 -->
            <button v-else class="btn-ghost" :disabled="busy" @click="emit('select', topic)">
              看进度
            </button>
          </footer>
        </aside>
      </div>
    </Transition>
  </Teleport>
</template>
