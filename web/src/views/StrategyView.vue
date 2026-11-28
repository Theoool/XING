<script setup lang="ts">
import { AlertTriangle, ArrowDownToLine, Check, Loader2, Pencil, Plus, Save, Trash2 } from 'lucide-vue-next'
import { computed, onMounted, ref, watch } from 'vue'

import { strategiesApi } from '@/api'
import { errorMessage } from '@/api/client'
import type { Strategy, StrategyVersionHistoryItem } from '@/api/types'
import { formatDate, relativeTime } from '@/lib/format'
import {
  DEFAULT_STRATEGY,
  LEGACY_KEYS,
  PROMPT_BUDGET,
  STRATEGY_FIELDS,
  configChars,
  entryChars,
  fromRows,
  rowValue,
  toRows,
  type StrategyFieldSpec,
  type StrategyRow,
} from '@/lib/strategy'
import { useToastStore } from '@/stores/toast'
import { useWorkspaceStore } from '@/stores/workspace'

const wss = useWorkspaceStore()
const toast = useToastStore()

const loading = ref(true)
const loadError = ref<string | null>(null)
const strategies = ref<Strategy[]>([])
const selectedId = ref<string | null>(null)

const rows = ref<StrategyRow[]>([])
const summary = ref('')
const jsonMode = ref(false)
const jsonText = ref('')

const saving = ref(false)
const activating = ref(false)
const creating = ref(false)
const newOpen = ref(false)
const newName = ref('')
const renaming = ref(false)
const nameDraft = ref('')
const history = ref<StrategyVersionHistoryItem[]>([])

const selected = computed(
  () => strategies.value.find((item) => item.id === selectedId.value) ?? null,
)
const isActive = computed(() => selected.value?.status === 'ACTIVE')

/** 草稿的唯一数据源是 `rows`（JSON 模式下暂时是 `jsonText`）。
 *  没有第二份「表单副本」—— 两份状态迟早不一致，而这里的不一致会直接写进线上基准。 */
const draft = computed<Record<string, unknown> | null>(() => {
  try {
    return fromRows(rows.value)
  } catch {
    return null
  }
})
const used = computed(() => {
  // JSON 视图下 rows 是切过去之前的快照，不能拿它算长度 —— 否则改了半天数字不动。
  if (jsonMode.value) {
    try {
      return configChars(JSON.parse(jsonText.value) as Record<string, unknown>)
    } catch {
      return jsonText.value.length
    }
  }
  return draft.value ? configChars(draft.value) : 0
})
const overBudget = computed(() => used.value > PROMPT_BUDGET)

const duplicates = computed(() => {
  const seen = new Set<string>()
  const dup: string[] = []
  for (const row of rows.value) {
    const key = row.key.trim()
    if (!key) continue
    if (seen.has(key) && !dup.includes(key)) dup.push(key)
    seen.add(key)
  }
  return dup
})

/** 按当前返回顺序累计长度，标出「大概从这里开始被砍」。
 *  顺序是 jsonb 给的、不是用户排的，所以这只是一个估算 —— 但它比干巴巴的
 *  总数有用：用户能看到是哪几行把预算撑爆的。 */
const marks = computed(() => {
  let acc = 2
  return rows.value.map((row) => {
    let value: unknown = null
    try {
      value = rowValue(row)
    } catch {
      value = null
    }
    acc += entryChars(row.key.trim() || 'untitled', value)
    return acc > PROMPT_BUDGET
  })
})
const firstTruncated = computed(() => {
  const index = marks.value.findIndex(Boolean)
  return index < 0 ? 0 : index + 1
})
const budgetPct = computed(() => Math.min(100, Math.round((used.value / PROMPT_BUDGET) * 100)))

/** 用得上的建议字段：配置里还没有的那些。 */
const missingSpecs = computed<StrategyFieldSpec[]>(() =>
  STRATEGY_FIELDS.filter((spec) => !rows.value.some((row) => row.key.trim() === spec.key)),
)

function specFor(key: string): StrategyFieldSpec | undefined {
  return STRATEGY_FIELDS.find((spec) => spec.key === key.trim())
}

/** 旧字段名提示：只提示，不改数据 —— 迁移是用户的决定，不是编辑器的副作用。 */
function legacyHint(key: string): string | null {
  const next = LEGACY_KEYS[key.trim()]
  return next ? `早期用的是 ${key.trim()}，与 ${next} 等价；建议改名，别让同一件事占两次预算` : null
}

function statusLabel(status: Strategy['status']): string {
  return status === 'ACTIVE' ? '生效中' : status === 'DRAFT' ? '草稿' : status === 'PAUSED' ? '已暂停' : '已归档'
}

function statusClass(status: Strategy['status']): string {
  if (status === 'ACTIVE') return 'text-ok'
  if (status === 'PAUSED') return 'text-warn'
  if (status === 'ARCHIVED') return 'text-bad'
  return 'text-muted'
}

function applyEditor(strategy: Strategy): void {
  rows.value = toRows(strategy.current_version.strategy_config)
  summary.value = ''
  jsonMode.value = false
  jsonText.value = ''
}

async function load(): Promise<void> {
  const workspaceId = wss.currentId
  if (!workspaceId) return
  loading.value = true
  loadError.value = null
  try {
    const items = await strategiesApi.list(workspaceId)
    strategies.value = items
    // 优先停在生效中的那条：编辑页的第一诉求永远是「改现在用的」。
    const keep = items.find((item) => item.id === selectedId.value)
    const next = items.find((item) => item.status === 'ACTIVE') ?? keep ?? items[0] ?? null
    selectedId.value = next?.id ?? null
    if (next) applyEditor(next)
    else rows.value = []
    await loadHistory()
  } catch (err) {
    loadError.value = errorMessage(err)
  } finally {
    loading.value = false
  }
}

async function loadHistory(): Promise<void> {
  const workspaceId = wss.currentId
  const id = selectedId.value
  if (!workspaceId || !id) {
    history.value = []
    return
  }
  try {
    history.value = await strategiesApi.versions(workspaceId, id)
  } catch {
    // 历史读不到不该挡住编辑，静默降级成空列表。
    history.value = []
  }
}

function select(strategy: Strategy): void {
  if (strategy.id === selectedId.value) return
  selectedId.value = strategy.id
  applyEditor(strategy)
  void loadHistory()
}

function addRow(spec?: StrategyFieldSpec): void {
  rows.value.push({
    key: spec?.key ?? '',
    kind: spec?.sample?.startsWith('{') ? 'json' : spec?.sample?.includes('\n') ? 'lines' : 'text',
    text: spec?.sample ?? '',
  })
}

function removeRow(index: number): void {
  rows.value.splice(index, 1)
}

/** 放弃改动 = 回到当前版本。模板里不能直接传 `selected`（可能是 null）。 */
function discard(): void {
  if (selected.value) applyEditor(selected.value)
}

function useVersion(item: StrategyVersionHistoryItem): void {
  rows.value = toRows(item.strategy_config)
  jsonMode.value = false
  toast.push(`已载入 v${item.version_number} 的配置；保存会生成新版本，旧版本不会被改动`, 'success')
}

function enterJson(): void {
  if (!draft.value) {
    toast.push('先修好不是合法 JSON 的字段，再切到 JSON 视图', 'error')
    return
  }
  jsonText.value = JSON.stringify(draft.value, null, 2)
  jsonMode.value = true
}

function leaveJson(): void {
  try {
    const parsed: unknown = JSON.parse(jsonText.value)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new Error('配置顶层必须是一个对象')
    }
    rows.value = toRows(parsed as Record<string, unknown>)
    jsonMode.value = false
  } catch (err) {
    toast.push(err instanceof Error ? err.message : '不是合法 JSON', 'error')
  }
}

function currentConfig(): Record<string, unknown> {
  if (!jsonMode.value) return fromRows(rows.value)
  const parsed: unknown = JSON.parse(jsonText.value)
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('配置顶层必须是一个对象')
  }
  return parsed as Record<string, unknown>
}

async function save(): Promise<void> {
  const workspaceId = wss.currentId
  const strategy = selected.value
  if (!workspaceId || !strategy) return

  if (duplicates.value.length) {
    toast.push(`字段名重复：${duplicates.value.join('、')}；后一个会覆盖前一个`, 'error')
    return
  }
  if (!jsonMode.value && rows.value.some((row) => !row.key.trim())) {
    toast.push('有字段还没命名 —— 没有字段名的内容不会写进配置', 'error')
    return
  }
  if (!summary.value.trim()) {
    toast.push('写一句变更说明吧 —— 版本不可修改，这句话是以后唯一能解释「这版为什么存在」的东西', 'error')
    return
  }

  let config: Record<string, unknown>
  try {
    config = currentConfig()
  } catch (err) {
    toast.push(err instanceof Error ? err.message : '配置不合法', 'error')
    return
  }

  saving.value = true
  try {
    const updated = await strategiesApi.addVersion(
      workspaceId,
      strategy.id,
      config,
      summary.value.trim(),
    )
    strategies.value = strategies.value.map((item) => (item.id === updated.id ? updated : item))
    applyEditor(updated)
    toast.push(
      isActive.value ? `已保存为 v${updated.current_version_number}，下一次展开即按它来` : `已保存为 v${updated.current_version_number}`,
      'success',
    )
    await loadHistory()
  } catch (err) {
    toast.push(errorMessage(err), 'error')
  } finally {
    saving.value = false
  }
}

async function rename(): Promise<void> {
  const workspaceId = wss.currentId
  const strategy = selected.value
  const name = nameDraft.value.trim()
  if (!workspaceId || !strategy || !name) return
  try {
    const updated = await strategiesApi.rename(workspaceId, strategy.id, name)
    strategies.value = strategies.value.map((item) => (item.id === updated.id ? updated : item))
    renaming.value = false
    toast.push('名称已更新', 'success')
  } catch (err) {
    toast.push(errorMessage(err), 'error')
  }
}

function startRename(): void {
  if (!selected.value) return
  nameDraft.value = selected.value.name
  renaming.value = true
}

async function activate(): Promise<void> {
  const workspaceId = wss.currentId
  const strategy = selected.value
  if (!workspaceId || !strategy) return
  activating.value = true
  try {
    const updated = await strategiesApi.activate(workspaceId, strategy.id)
    // 不整页重载：重载会顺手把编辑器重置，用户没保存的改动就没了。
    // 服务端的规则是「其余 ACTIVE 一律降级为 PAUSED」，本地照这个规则同步即可。
    strategies.value = strategies.value.map((item) => {
      if (item.id === updated.id) return updated
      return item.status === 'ACTIVE' ? { ...item, status: 'PAUSED' as const } : item
    })
    toast.push('已切换生效战略', 'success')
  } catch (err) {
    toast.push(errorMessage(err), 'error')
  } finally {
    activating.value = false
  }
}

async function create(): Promise<void> {
  const workspaceId = wss.currentId
  const name = newName.value.trim()
  if (!workspaceId || !name) return
  creating.value = true
  try {
    const created = await strategiesApi.create(workspaceId, name, DEFAULT_STRATEGY)
    strategies.value = [created, ...strategies.value]
    selectedId.value = created.id
    applyEditor(created)
    newName.value = ''
    newOpen.value = false
    toast.push('已创建为草稿；激活后才会成为选定与评分的基准', 'success')
    await loadHistory()
  } catch (err) {
    toast.push(errorMessage(err), 'error')
  } finally {
    creating.value = false
  }
}

onMounted(() => void load())
watch(
  () => wss.currentId,
  () => {
    selectedId.value = null
    void load()
  },
)
</script>

<template>
  <div class="mx-auto max-w-[1180px] px-8 py-9 lg:px-10">
    <header class="mb-7 flex flex-wrap items-end justify-between gap-4">
      <div>
        <p class="eyebrow">Strategy · 内容战略</p>
        <h1 class="display mt-2 text-[29px] leading-tight">战略不是设置项，是判断的基准。</h1>
        <p class="mt-2.5 max-w-[680px] text-[13px] leading-relaxed text-ink-2">
          这份配置会随候选生成与评分进入提示词，也被当成事件相关度的关键词；每一条决策都绑定到
          当时那个具体版本。所以改配置不是「保存设置」，而是新增一版快照 —— 旧版本原样留在那里。
        </p>
      </div>
      <div class="text-right">
        <p class="eyebrow mb-1.5">版本</p>
        <p class="text-[12.5px] text-ink-2">
          <template v-if="selected">
            v{{ selected.current_version_number }} · {{ relativeTime(selected.current_version.created_at) }}
          </template>
          <template v-else>还没有战略</template>
        </p>
      </div>
    </header>

    <div v-if="loading" class="flex items-center gap-2 py-10 text-[12.5px] text-muted">
      <Loader2 :size="13" class="animate-spin" /> 正在读取战略…
    </div>

    <div v-else-if="loadError" class="surface p-6">
      <p class="text-[13px] text-bad">{{ loadError }}</p>
      <button class="btn-ghost btn-sm mt-3" @click="load">重试</button>
    </div>

    <div v-else class="grid gap-5 lg:grid-cols-[minmax(0,1fr)_296px]">
      <div class="min-w-0">
        <!-- 没有任何战略：这是唯一需要「新建」而不是「编辑」的时刻 -->
        <section v-if="!strategies.length" class="surface p-6">
          <p class="eyebrow">还没有战略</p>
          <p class="mt-2 max-w-[560px] text-[13px] leading-relaxed text-ink-2">
            展开选题不需要战略，但「选定」会写一条决策并绑定战略版本，所以没有生效战略时会直接
            409。先建一条，改好再激活。
          </p>
          <div class="mt-4 flex flex-wrap items-center gap-2">
            <input
              v-model="newName"
              placeholder="战略名称，例如「2026 Q4 内容战略」"
              class="field max-w-[320px]"
              @keydown.enter="create"
            />
            <button class="btn-accent btn-sm" :disabled="creating || !newName.trim()" @click="create">
              <Loader2 v-if="creating" :size="12" class="animate-spin" />
              用默认配置创建
            </button>
          </div>
          <p class="mt-2 text-[11px] text-muted">
            默认配置只有定位、读者、语气三条；建完可以直接在这里改。
          </p>
        </section>

        <template v-else>
          <!-- 同一工作区可以有多条战略，但只有一条生效。切换不等于编辑目标。 -->
          <div class="mb-4 flex flex-wrap items-center gap-1.5">
            <template v-if="strategies.length > 1">
              <button
                v-for="item in strategies"
                :key="item.id"
                class="chip border-line bg-white/70"
                :class="item.id === selectedId ? 'border-accent text-accent' : 'text-ink-2'"
                @click="select(item)"
              >
                {{ item.name }}
                <span class="ml-1" :class="statusClass(item.status)">{{ statusLabel(item.status) }}</span>
              </button>
            </template>
            <!-- 想改的东西还没定稿时，正确做法是新建一条 DRAFT 改好再激活，
                 而不是直接改正在生效的那条。 -->
            <template v-if="newOpen">
              <input
                v-model="newName"
                autofocus
                placeholder="新战略名称"
                class="w-[180px] rounded-md border border-line bg-white px-2 py-1 text-[12px] text-ink outline-none placeholder:text-[#b0a89b] focus:border-accent/50"
                @keydown.enter="create"
                @keydown.esc="newOpen = false"
              />
              <button class="btn-ghost btn-sm" :disabled="creating || !newName.trim()" @click="create">
                <Loader2 v-if="creating" :size="12" class="animate-spin" />
                建
              </button>
              <button class="btn-ghost btn-sm" @click="newOpen = false">取消</button>
            </template>
            <button v-else class="btn-ghost btn-sm" @click="newOpen = true">
              <Plus :size="12" /> 新建战略
            </button>
          </div>

          <section v-if="selected" class="surface p-5">
            <div class="mb-4 flex flex-wrap items-start justify-between gap-3 border-b border-line-soft pb-3">
              <div class="min-w-0">
                <div v-if="renaming" class="flex flex-wrap items-center gap-1.5">
                  <input
                    v-model="nameDraft"
                    autofocus
                    maxlength="100"
                    class="w-[240px] rounded-md border border-line bg-white px-2 py-1 text-[14px] text-ink outline-none focus:border-accent/50"
                    @keydown.enter="rename"
                    @keydown.esc="renaming = false"
                  />
                  <button class="btn-ghost btn-sm" :disabled="!nameDraft.trim()" @click="rename">
                    <Check :size="12" /> 保存
                  </button>
                  <button class="btn-ghost btn-sm" @click="renaming = false">取消</button>
                </div>
                <div v-else class="flex items-center gap-2">
                  <h2 class="text-[15px] font-medium">{{ selected.name }}</h2>
                  <button
                    class="rounded p-0.5 text-muted transition hover:bg-line-soft hover:text-ink"
                    title="重命名（不影响版本）"
                    @click="startRename"
                  >
                    <Pencil :size="11" />
                  </button>
                  <span class="text-[11px]" :class="statusClass(selected.status)">
                    {{ statusLabel(selected.status) }}
                  </span>
                </div>
                <p class="mt-1 text-[11.5px] text-muted">
                  v{{ selected.current_version.version_number }} ·
                  {{ selected.current_version.change_summary || '（未写说明）' }} ·
                  {{ formatDate(selected.current_version.created_at) }}
                </p>
              </div>
              <div class="flex items-center gap-2">
                <button
                  v-if="!isActive"
                  class="btn-ghost btn-sm"
                  :disabled="activating || selected.status === 'ARCHIVED'"
                  @click="activate"
                >
                  <Loader2 v-if="activating" :size="12" class="animate-spin" />
                  <Check v-else :size="12" />
                  设为生效
                </button>
                <span
                  v-else
                  class="inline-flex items-center gap-1 text-[11px] text-ok"
                >
                  <Check :size="12" /> 正在生效
                </span>
              </div>
            </div>

            <!-- 编辑生效战略等于改线上基准，这一点必须在动手前说清 -->
            <p
              v-if="isActive"
              class="mb-4 flex items-start gap-2 rounded-md border border-[#e8d9b8] bg-warn-soft px-3 py-2 text-[11.5px] leading-relaxed text-[#7a5a1e]"
            >
              <AlertTriangle :size="13" class="mt-0.5 shrink-0" />
              <span>
                这是当前生效的战略。保存会立刻改变后续展开与评分的基准（正在跑的任务读到的也是新版本），
                已发出的决策不受影响 —— 它们钉在旧版本上。
              </span>
            </p>

            <div class="mb-3 flex items-center justify-between">
              <p class="eyebrow">配置</p>
              <div class="flex items-center gap-1">
                <button class="btn-ghost btn-sm" @click="jsonMode ? leaveJson() : enterJson()">
                  {{ jsonMode ? '回到字段视图' : 'JSON 视图' }}
                </button>
              </div>
            </div>

            <!-- JSON 视图：字段形状超出三种可编辑形态时（数字、嵌套对象、混合数组）的出口 -->
            <textarea
              v-if="jsonMode"
              v-model="jsonText"
              rows="18"
              spellcheck="false"
              class="field w-full resize-y font-mono text-[12px] leading-relaxed"
            />

            <template v-else>
              <div class="space-y-2.5">
                <div
                  v-for="(row, index) in rows"
                  :key="index"
                  class="rounded-md border border-line-soft bg-paper/60 p-3"
                  :class="marks[index] ? 'border-[#e6c9c3]' : ''"
                >
                  <div class="mb-1.5 flex items-center gap-2">
                    <input
                      v-model="row.key"
                      spellcheck="false"
                      placeholder="字段名"
                      class="w-[190px] rounded border border-line/70 bg-white px-1.5 py-0.5 text-[12px] font-medium text-ink outline-none hover:border-line focus:border-accent/50"
                    />
                    <span class="rounded bg-line-soft px-1.5 py-0.5 text-[10px] text-muted">
                      {{ row.kind === 'lines' ? '每行一项' : row.kind === 'json' ? 'JSON' : '文本' }}
                    </span>
                    <span
                      v-if="marks[index]"
                      class="text-[10px] text-bad"
                      title="按当前显示顺序估算：超出预算后，排在后面的字段会被截断"
                    >
                      不进提示词
                    </span>
                    <button
                      class="ml-auto rounded p-1 text-muted transition hover:bg-bad-soft hover:text-bad"
                      title="删除字段"
                      @click="removeRow(index)"
                    >
                      <Trash2 :size="12" />
                    </button>
                  </div>
                  <textarea
                    v-model="row.text"
                    :rows="row.kind === 'text' ? 2 : 3"
                    spellcheck="false"
                    class="field w-full resize-y text-[12.5px] leading-relaxed"
                    :class="row.kind === 'json' ? 'font-mono text-[12px]' : ''"
                  />
                  <p v-if="specFor(row.key)" class="mt-1 text-[10.5px] text-muted">
                    {{ specFor(row.key)?.hint }}
                  </p>
                  <p v-else-if="legacyHint(row.key)" class="mt-1 text-[10.5px] text-warn">
                    {{ legacyHint(row.key) }}
                  </p>
                </div>
              </div>

              <div v-if="!rows.length" class="rounded-md border border-dashed border-line px-3 py-6 text-center text-[12px] text-muted">
                还没有任何字段。空配置不会让流程失败，但评分就失去了依据。
              </div>

              <div class="mt-3 flex flex-wrap items-center gap-1.5">
                <button class="btn-ghost btn-sm" @click="addRow()">
                  <Plus :size="12" /> 加一个字段
                </button>
                <template v-if="missingSpecs.length">
                  <span class="ml-1 text-[11px] text-muted">常用：</span>
                  <button
                    v-for="spec in missingSpecs"
                    :key="spec.key"
                    class="rounded-full border border-line bg-white/70 px-2.5 py-1 text-[11px] text-ink-2 transition hover:border-[#cfc6b6] hover:text-ink"
                    @click="addRow(spec)"
                  >
                    {{ spec.label }}
                  </button>
                </template>
              </div>
            </template>

            <!-- 预算条：真正会进提示词的长度。两种视图共用一份，否则 JSON 视图是盲写 -->
            <div class="mt-4 rounded-md bg-paper px-3 py-2.5">
              <div class="flex items-baseline justify-between text-[11px]">
                <span class="text-ink-2">进入提示词的长度</span>
                <span :class="overBudget ? 'text-bad' : 'text-muted'">
                  {{ used }} / {{ PROMPT_BUDGET }} 字符
                </span>
              </div>
              <div class="mt-1.5 h-1 overflow-hidden rounded-full bg-line">
                <div
                  class="h-full rounded-full transition-all"
                  :class="overBudget ? 'bg-bad' : 'bg-accent'"
                  :style="{ width: `${budgetPct}%` }"
                />
              </div>
              <p v-if="overBudget" class="mt-1.5 text-[10.5px] leading-relaxed text-bad">
                超出 {{ used - PROMPT_BUDGET }} 字符。后端只取前 {{ PROMPT_BUDGET }} 字符，
                而且截断点落在 JSON 中间 —— 模型拿到的是残缺配置。删掉不重要的字段或把内容压短；
                调顺序没用，键的顺序由数据库决定、保存后还会重排。
                <template v-if="!jsonMode">
                  按当前顺序估算，第 {{ firstTruncated }} 个字段之后的内容进不去。
                </template>
              </p>
              <p v-else class="mt-1.5 text-[10.5px] leading-relaxed text-muted">
                后端把整份配置序列化后截断到 {{ PROMPT_BUDGET }} 字符塞进提示词。存储是 jsonb，
                键的顺序由数据库决定、保存后可能重排，所以能控制的只有总量。
              </p>
            </div>

            <div class="mt-4 border-t border-line-soft pt-3">
              <label class="mb-1.5 block text-[11px] text-ink-2">
                变更说明 <span class="text-accent">*</span>
              </label>
              <input
                v-model="summary"
                maxlength="200"
                placeholder="这次改了什么，为什么改。例如：补充独立开发者方向"
                class="field"
              />
              <div class="mt-3 flex flex-wrap items-center gap-2">
                <button class="btn-accent btn-sm" :disabled="saving" @click="save">
                  <Loader2 v-if="saving" :size="12" class="animate-spin" />
                  <Save v-else :size="12" />
                  保存为 v{{ selected.current_version_number + 1 }}
                  <template v-if="isActive">（立即生效）</template>
                </button>
                <button class="btn-ghost btn-sm" @click="discard">放弃改动</button>
                <span v-if="!isActive" class="text-[11px] text-muted">
                  这条战略没有生效，保存不会影响线上
                </span>
              </div>
            </div>
          </section>
        </template>
      </div>

      <!-- 右栏：版本历史。没有它，编辑等于覆盖 —— 用户看不到上一版长什么样 -->
      <aside class="min-w-0">
        <section class="surface p-4">
          <div class="mb-2.5 flex items-center justify-between">
            <p class="eyebrow">版本历史</p>
            <span class="text-[10.5px] text-muted">不可修改</span>
          </div>
          <p v-if="!history.length" class="text-[11.5px] leading-relaxed text-muted">
            还没有版本记录。
          </p>
          <ol v-else class="space-y-2">
            <li
              v-for="item in history"
              :key="item.id"
              class="rounded-md border border-line-soft px-2.5 py-2"
              :class="item.is_current ? 'bg-ok-soft/50' : ''"
            >
              <div class="flex items-center gap-1.5">
                <span class="num text-[11.5px] text-ink">v{{ item.version_number }}</span>
                <span v-if="item.is_current" class="text-[10px] text-ok">生效中</span>
                <span class="ml-auto text-[10px] text-muted">{{ relativeTime(item.created_at) }}</span>
              </div>
              <p class="mt-1 text-[11.5px] leading-relaxed text-ink-2">
                {{ item.change_summary || '（未写说明）' }}
              </p>
              <button
                v-if="!item.is_current"
                class="mt-1.5 inline-flex items-center gap-1 text-[10.5px] text-accent hover:underline"
                @click="useVersion(item)"
              >
                <ArrowDownToLine :size="10" /> 以此为底稿
              </button>
            </li>
          </ol>
          <p class="mt-3 border-t border-line-soft pt-2.5 text-[10.5px] leading-relaxed text-muted">
            「以此为底稿」只是把旧配置载入编辑器，不会改动历史；保存后才会生成新版本。
          </p>
        </section>

        <section class="surface mt-4 p-4">
          <p class="eyebrow mb-2">这份配置被谁读</p>
          <ul class="space-y-1.5 text-[11.5px] leading-relaxed text-ink-2">
            <li>· 展开：候选选题要符合定位与读者</li>
            <li>· 评分：账号定位进入评分提示词</li>
            <li>· 选题池：所有字符串被当作事件相关度关键词</li>
            <li>· 决策：选定那一刻钉住当时的版本 id</li>
          </ul>
        </section>
      </aside>
    </div>
  </div>
</template>
