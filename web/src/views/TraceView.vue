<script setup lang="ts">
/** 取证过程：一个任务到底怎么搜、怎么想、怎么判定的。
 *
 * 这一页之所以存在，是因为「选题好不好」没法只靠结果判断：同一批候选，
 * 可能是从 6 篇媒体文章里抽出来的，也可能是从 20 条真实发言里抽出来的。
 * 前者和后者在界面上长得一模一样，直到你把来源摊开。
 *
 * 四段按发生顺序排列，每段都可以单独读：
 *   检索 → 立场 → 模型步骤。规划在检索的第一条 query 之前就能看出来。
 */
import { ArrowLeft, ChevronDown, ChevronRight, ExternalLink, RefreshCw } from 'lucide-vue-next'
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'

import { tasksApi } from '@/api'
import { errorMessage } from '@/api/client'
import {
  DROP_REASON_LABEL,
  SOURCE_KIND_HINT,
  SOURCE_KIND_LABEL,
  TASK_STATUS_LABEL,
  TASK_TYPE_LABEL,
  TRACE_LAYER_LABEL,
  type SourceKind,
  type TaskTrace,
  type TraceQuery,
  type TraceStep,
} from '@/api/types'
import { formatDate, formatDuration } from '@/lib/format'
import { useWorkspaceStore } from '@/stores/workspace'

const props = defineProps<{ taskId: string }>()

const ws = useWorkspaceStore()

const data = ref<TaskTrace | null>(null)
const loading = ref(false)
const error = ref('')

/** 展开状态按 key 记，刷新后不保留：取证是「现在看一次」的动作，不是工作区。 */
const openQueries = ref<Set<string>>(new Set())
const openSteps = ref<Set<number>>(new Set())

const KIND_CHIP: Record<SourceKind, string> = {
  MEDIA: 'border-[#c9d9ea] bg-[#eaf1f8] text-[#2b5b8a]',
  UGC: 'border-[#cfe3d7] bg-[#e9f2ec] text-[#2c6b4a]',
  OFFICIAL: 'border-[#ddd7cc] bg-[#f1ede5] text-[#5b5347]',
  VESTED: 'border-[#ecd9b8] bg-[#f8eeda] text-[#8a5f14]',
  UNKNOWN: 'border-line bg-[#f4f1ea] text-muted',
}

const CAMP_LABEL: Record<string, string> = {
  SUPPORT: '支持',
  OPPOSE: '反对',
  CONDITIONAL: '有条件同意',
  EXPERIENCE: '亲历者',
  OBSERVER: '旁观分析',
}

const kindChart = computed(() => {
  const mix = data.value?.summary.source_mix ?? {}
  const total = Object.values(mix).reduce((sum, value) => sum + (value ?? 0), 0)
  return (Object.keys(mix) as SourceKind[])
    .filter((kind) => (mix[kind] ?? 0) > 0)
    .map((kind) => ({
      kind,
      count: mix[kind] ?? 0,
      pct: total ? Math.round(((mix[kind] ?? 0) / total) * 100) : 0,
    }))
})

const rows = computed(() => data.value?.rounds ?? [])

/** 进池材料里，按今天的分类表会换类的条数。
 *
 * 上面的百分比是那一趟的快照，不能拿现在的规则重算 —— 那样等于改写记录。
 * 但只摆旧数字又会误导：读的人会以为分类器认不出律所，跑去补一条已经补过的规则。
 * 所以数字不动，旁边标明「有几条会换类」。
 */
const staleCount = computed(() => {
  let count = 0
  for (const round of data.value?.rounds ?? []) {
    for (const query of round.queries) {
      for (const doc of query.docs) {
        if (doc.kept && doc.source_kind_now) count += 1
      }
    }
  }
  return count
})

async function load(): Promise<void> {
  const wsId = ws.currentId
  if (!wsId) return
  loading.value = true
  try {
    data.value = await tasksApi.trail(wsId, props.taskId)
    error.value = ''
    // 默认展开第一条检索的文档明细：进来就是要看「搜到了什么」。
    const first = data.value.rounds[0]?.queries[0]
    if (first) openQueries.value = new Set([keyOf(0, first.seq)])
  } catch (err) {
    error.value = errorMessage(err)
  } finally {
    loading.value = false
  }
}

function keyOf(round: number, seq: number): string {
  return `${round}:${seq}`
}

function toggleQuery(round: number, query: TraceQuery): void {
  const key = keyOf(round, query.seq)
  const next = new Set(openQueries.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  openQueries.value = next
}

function toggleStep(step: TraceStep): void {
  const next = new Set(openSteps.value)
  if (next.has(step.step_no)) next.delete(step.step_no)
  else next.add(step.step_no)
  openSteps.value = next
}

function optionsText(query: TraceQuery): string {
  const parts: string[] = []
  if (query.options.topic) parts.push(`topic=${query.options.topic}`)
  if (query.options.time_range) parts.push(`time_range=${query.options.time_range}`)
  const domains = query.options.include_domains ?? []
  if (domains.length) parts.push(`限定 ${domains.length} 个站：${domains.slice(0, 3).join('、')}${domains.length > 3 ? '…' : ''}`)
  return parts.join(' · ') || '无附加参数（通用检索）'
}

function stepLabel(purpose: string): string {
  const map: Record<string, string> = {
    plan: '规划',
    analyze: '分析',
    generate: '生成',
    self_check: '自检',
    repair: '修复',
    tool_decision: '工具决策',
  }
  return map[purpose] ?? purpose
}

onMounted(load)
watch(() => [ws.currentId, props.taskId], load)
</script>

<template>
  <section class="mx-auto max-w-[1040px] px-8 py-8">
    <RouterLink
      :to="`/w/${ws.currentId}/tasks`"
      class="mb-5 inline-flex items-center gap-1.5 text-[12px] text-muted transition hover:text-ink"
    >
      <ArrowLeft :size="13" /> 任务流
    </RouterLink>

    <div v-if="error" class="surface border-bad/30 bg-bad-soft px-5 py-4 text-[12.5px] text-bad">
      {{ error }}
      <button class="btn-ghost btn-sm ml-3" @click="load"><RefreshCw :size="12" /> 重试</button>
    </div>

    <div v-else-if="loading && !data" class="py-16 text-center text-[12.5px] text-muted">
      正在读取取证记录…
    </div>

    <template v-else-if="data">
      <!-- 头部：这是什么任务，跑得怎么样 -->
      <header class="border-b border-line pb-5">
        <p class="eyebrow">取证过程</p>
        <div class="mt-1.5 flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h1 class="display text-[22px]">
            {{ TASK_TYPE_LABEL[data.task_type] ?? data.task_type }}
          </h1>
          <span class="text-[12px] text-muted">
            {{ TASK_STATUS_LABEL[data.status] ?? data.status }} · 第 {{ data.attempt }} 次执行
            <template v-if="data.agent"> · {{ data.agent }}</template>
          </span>
        </div>
        <p v-if="data.goal" class="mt-2 max-w-[70ch] text-[12.5px] leading-relaxed text-ink-2">
          {{ data.goal }}
        </p>
        <p class="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-muted">
          <span>创建 {{ formatDate(data.created_at) }}</span>
          <span>耗时 {{ formatDuration(Math.max(0, Math.round(((data.completed_at ? new Date(data.completed_at).getTime() : Date.now()) - new Date(data.started_at ?? data.created_at).getTime()) / 1000))) }}</span>
          <span v-if="data.dedup_key" class="num">{{ data.dedup_key }}</span>
        </p>
        <div
          v-if="data.error"
          class="mt-3 rounded-md border border-[#eccbc6] bg-bad-soft px-3.5 py-2.5 text-[12px] text-bad"
        >
          {{ data.error.code }} · {{ data.error.message }}
        </div>
      </header>

      <!-- 结论条：素材构成是这一页最该先看的东西 -->
      <div class="mt-5 grid grid-cols-2 gap-3 sm:grid-cols-5">
        <div class="surface px-4 py-3">
          <p class="text-[10.5px] text-muted">检索条数</p>
          <p class="num mt-1 text-[20px]">{{ data.summary.query_count }}</p>
        </div>
        <div class="surface px-4 py-3">
          <p class="text-[10.5px] text-muted">返回材料</p>
          <p class="num mt-1 text-[20px]">{{ data.summary.doc_count }}</p>
        </div>
        <div class="surface px-4 py-3">
          <p class="text-[10.5px] text-muted">进入材料池</p>
          <p class="num mt-1 text-[20px]">{{ data.summary.kept_count }}</p>
        </div>
        <div class="surface px-4 py-3">
          <p class="text-[10.5px] text-muted">立场</p>
          <p class="num mt-1 text-[20px]">{{ data.summary.stance_count }}</p>
        </div>
        <div class="surface px-4 py-3">
          <p class="text-[10.5px] text-muted">用户发言占比</p>
          <p
            class="num mt-1 text-[20px]"
            :class="data.summary.ugc_ratio > 0 ? 'text-ok' : 'text-warn'"
          >
            {{ Math.round(data.summary.ugc_ratio * 100) }}%
          </p>
        </div>
      </div>

      <div v-if="kindChart.length" class="mt-3 flex flex-wrap items-center gap-2">
        <span class="text-[11px] text-muted">材料构成</span>
        <span
          v-for="item in kindChart"
          :key="item.kind"
          class="chip"
          :class="KIND_CHIP[item.kind]"
          :title="SOURCE_KIND_HINT[item.kind]"
        >
          {{ SOURCE_KIND_LABEL[item.kind] }} {{ item.count }} · {{ item.pct }}%
        </span>
        <span v-if="staleCount" class="text-[10.5px] text-muted">
          · 其中 {{ staleCount }} 条按今天的规则会换类（见每条材料上的「现判」）
        </span>
      </div>

      <!-- ① 检索 -->
      <section class="mt-8">
        <div class="flex items-baseline justify-between">
          <h2 class="text-[14px] font-medium">一 · 检索</h2>
          <p class="text-[11px] text-muted">每条 query 下发的参数、回来的材料、以及为什么留下或丢掉</p>
        </div>

        <div v-if="!rows.length" class="mt-3 rounded-md border border-dashed border-line px-5 py-6 text-[12px] text-muted">
          这次执行没有检索记录。可能是任务早于取证功能上线，或者失败发生在检索之前。
        </div>

        <div v-for="round in rows" :key="round.round" class="mt-4">
          <p class="mb-2 text-[11px] text-muted">
            第 {{ round.round + 1 }} 轮（{{ round.round === 0 ? '首轮' : '补采' }}） ·
            {{ round.query_count }} 条 query · 回来 {{ round.result_count }} 篇 · 进池
            {{ round.new_count }} 篇
          </p>

          <div
            v-for="query in round.queries"
            :key="query.seq"
            class="surface mb-2 overflow-hidden"
          >
            <button
              class="flex w-full items-start gap-3 px-4 py-3 text-left transition hover:bg-[#f8f5ee]"
              @click="toggleQuery(round.round, query)"
            >
              <component
                :is="openQueries.has(keyOf(round.round, query.seq)) ? ChevronDown : ChevronRight"
                :size="14"
                class="mt-0.5 shrink-0 text-muted"
              />
              <div class="min-w-0 flex-1">
                <div class="flex flex-wrap items-center gap-2">
                  <span
                    class="chip"
                    :class="
                      query.layer === 'DISCUSSION'
                        ? 'border-[#cfe3d7] bg-[#e9f2ec] text-[#2c6b4a]'
                        : query.layer === 'SUPPLEMENT'
                          ? 'border-[#ecd9b8] bg-[#f8eeda] text-[#8a5f14]'
                          : 'border-line bg-[#f4f1ea] text-ink-2'
                    "
                  >
                    {{ TRACE_LAYER_LABEL[query.layer] }}
                  </span>
                  <span v-if="query.target" class="text-[10.5px] tracking-wide text-muted">
                    {{ query.target }}
                  </span>
                  <span class="text-[13px] text-ink">{{ query.query }}</span>
                </div>
                <p class="mt-1 text-[10.5px] text-muted">{{ optionsText(query) }}</p>
              </div>
              <div class="shrink-0 text-right">
                <p class="num text-[11.5px]" :class="query.status === 'ERROR' ? 'text-bad' : 'text-ink-2'">
                  {{ query.result_count }} → {{ query.new_count }}
                </p>
                <p class="num text-[10px] text-muted">{{ query.latency_ms }} ms</p>
              </div>
            </button>

            <div v-if="query.error" class="border-t border-line-soft bg-bad-soft/60 px-4 py-2 text-[11.5px] text-bad">
              {{ query.error }}
            </div>

            <ul
              v-if="openQueries.has(keyOf(round.round, query.seq))"
              class="divide-y divide-[#f0ece3] border-t border-line-soft"
            >
              <li v-if="!query.docs.length" class="px-4 py-3 text-[11.5px] text-muted">
                {{ query.status === 'EMPTY' ? '这条 query 没有返回任何结果。' : '没有可展示的材料。' }}
              </li>
              <li
                v-for="doc in query.docs"
                :key="`${query.seq}-${doc.rank}-${doc.url}`"
                class="flex items-start gap-3 px-4 py-2.5"
                :class="doc.kept ? '' : 'opacity-55'"
              >
                <span class="num w-4 shrink-0 pt-0.5 text-[10.5px] text-muted">{{ doc.rank }}</span>
                <div class="min-w-0 flex-1">
                  <div class="flex flex-wrap items-center gap-2">
                    <span class="chip" :class="KIND_CHIP[doc.source_kind]" :title="SOURCE_KIND_HINT[doc.source_kind]">
                      {{ SOURCE_KIND_LABEL[doc.source_kind] }}
                    </span>
                    <!-- 分类表在检索之后变过。摆出两个答案而不是覆盖旧的：
                         轨迹是当时的记录，改了就是篡改；只留旧的又会让人以为
                         分类器认不出来，跑去改一条其实已经修好的规则。 -->
                    <span
                      v-if="doc.source_kind_now"
                      class="chip border-dashed bg-transparent"
                      :class="KIND_CHIP[doc.source_kind_now]"
                      title="分类规则在这次检索之后更新过。左边是当时的判定（原样保留），这里是按今天的规则会得到的判定。"
                    >
                      现判 {{ SOURCE_KIND_LABEL[doc.source_kind_now] }}
                    </span>
                    <span class="text-[10.5px] text-muted">{{ doc.domain ?? '无链接' }}</span>
                    <span v-if="!doc.kept" class="chip border-line bg-[#f4f1ea] text-muted">
                      未采用 · {{ DROP_REASON_LABEL[doc.drop_reason ?? ''] ?? doc.drop_reason }}
                    </span>
                  </div>
                  <a
                    v-if="doc.url"
                    :href="doc.url"
                    target="_blank"
                    rel="noreferrer"
                    class="mt-1 flex items-start gap-1 text-[12px] leading-snug text-ink-2 hover:text-accent"
                  >
                    <span class="line-clamp-2">{{ doc.title }}</span>
                    <ExternalLink :size="11" class="mt-1 shrink-0 opacity-60" />
                  </a>
                  <p v-else class="mt-1 text-[12px] text-ink-2">{{ doc.title }}</p>
                  <p class="mt-0.5 text-[10px] text-muted">
                    规则 {{ doc.kind_rule }}
                    <template v-if="doc.kind_rule_now">
                      <span class="text-muted">→ 现规则</span>
                      {{ doc.kind_rule_now }}
                    </template>
                    <template v-if="doc.published_at"> · 发布于 {{ formatDate(doc.published_at) }}</template>
                  </p>
                </div>
              </li>
            </ul>
          </div>
        </div>
      </section>

      <!-- ② 立场 -->
      <section class="mt-8">
        <div class="flex items-baseline justify-between">
          <h2 class="text-[14px] font-medium">二 · 立场</h2>
          <p class="text-[11px] text-muted">从哪篇材料的哪句话里抽出来的；引文已机检为原文子串</p>
        </div>

        <div v-if="!data.stances.length" class="mt-3 rounded-md border border-dashed border-line px-5 py-6 text-[12px] text-muted">
          这次执行没有落任何立场。
        </div>

        <div v-else class="mt-3 grid gap-3 md:grid-cols-2">
          <article v-for="stance in data.stances" :key="stance.id" class="surface px-4 py-3.5">
            <div class="flex flex-wrap items-center gap-2">
              <span class="chip border-line bg-[#f4f1ea] text-ink-2">
                {{ CAMP_LABEL[stance.camp] ?? stance.camp }}
              </span>
              <span class="text-[12.5px] font-medium">{{ stance.label }}</span>
            </div>
            <blockquote class="mt-2 border-l-2 border-[#e2ddd3] pl-3 text-[12px] leading-relaxed text-ink-2">
              {{ stance.quote }}
            </blockquote>
            <p class="mt-2 text-[11px] text-muted">持有者：{{ stance.audience }}</p>
            <div class="mt-2 flex flex-wrap items-center gap-2">
              <span class="chip" :class="KIND_CHIP[stance.source_kind]">
                {{ SOURCE_KIND_LABEL[stance.source_kind] }}
              </span>
              <a
                v-if="stance.url"
                :href="stance.url"
                target="_blank"
                rel="noreferrer"
                class="inline-flex items-center gap-1 text-[11px] text-muted hover:text-accent"
              >
                {{ stance.domain ?? '原文' }} <ExternalLink :size="10" />
              </a>
              <span v-else class="text-[11px] text-muted">{{ stance.domain ?? '无链接' }}</span>
            </div>
          </article>
        </div>
      </section>

      <!-- ③ 模型步骤 -->
      <section class="mt-8 pb-10">
        <div class="flex items-baseline justify-between">
          <h2 class="text-[14px] font-medium">三 · 模型步骤</h2>
          <p class="text-[11px] text-muted">每一步的 system / 输入 / 输出原文，按调用顺序</p>
        </div>

        <div v-if="!data.steps.length" class="mt-3 rounded-md border border-dashed border-line px-5 py-6 text-[12px] text-muted">
          这次执行没有模型调用记录。
        </div>

        <div v-for="step in data.steps" :key="step.call_seq" class="surface mt-2 overflow-hidden">
          <button
            class="flex w-full items-center gap-3 px-4 py-2.5 text-left transition hover:bg-[#f8f5ee]"
            @click="toggleStep(step)"
          >
            <component
              :is="openSteps.has(step.step_no) ? ChevronDown : ChevronRight"
              :size="14"
              class="shrink-0 text-muted"
            />
            <span class="num w-5 text-[10.5px] text-muted">{{ step.step_no }}</span>
            <span class="text-[12.5px]">{{ stepLabel(step.purpose) }}</span>
            <span class="text-[10.5px] text-muted">{{ step.model }}</span>
            <span class="ml-auto flex items-center gap-3 text-[10.5px] text-muted">
              <span class="num">{{ step.input_tokens }} → {{ step.output_tokens }} tok</span>
              <span class="num">{{ step.latency_ms ?? 0 }} ms</span>
              <span class="num">${{ step.cost.toFixed(4) }}</span>
            </span>
          </button>

          <div v-if="openSteps.has(step.step_no)" class="border-t border-line-soft px-4 py-3">
            <p v-if="step.error" class="mb-2 text-[11.5px] text-bad">{{ step.error }}</p>
            <div v-if="step.system" class="mb-3">
              <p class="eyebrow">system</p>
              <pre class="mt-1 max-h-56 overflow-auto rounded-md bg-[#f8f5ee] p-3 text-[11px] leading-relaxed whitespace-pre-wrap text-ink-2">{{ step.system }}</pre>
            </div>
            <div v-if="step.prompt" class="mb-3">
              <p class="eyebrow">输入</p>
              <pre class="mt-1 max-h-72 overflow-auto rounded-md bg-[#f8f5ee] p-3 text-[11px] leading-relaxed whitespace-pre-wrap text-ink-2">{{ step.prompt }}</pre>
            </div>
            <div v-if="step.response">
              <p class="eyebrow">输出</p>
              <pre class="mt-1 max-h-72 overflow-auto rounded-md bg-[#f8f5ee] p-3 text-[11px] leading-relaxed whitespace-pre-wrap text-ink-2">{{ step.response }}</pre>
            </div>
            <p v-if="step.truncated" class="mt-2 text-[10.5px] text-warn">
              原文过长，已截断到 20000 字符。
            </p>
          </div>
        </div>
      </section>
    </template>
  </section>
</template>
