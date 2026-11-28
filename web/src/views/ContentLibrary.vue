<script setup lang="ts">
import { ArrowUpRight, FileText, Loader2, RefreshCw } from 'lucide-vue-next'
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { contentsApi } from '@/api'
import { errorMessage } from '@/api/client'
import type { Content } from '@/api/types'
import StatusChip from '@/components/StatusChip.vue'
import { relativeTime } from '@/lib/format'
import { useWorkspaceStore } from '@/stores/workspace'

const ws = useWorkspaceStore()

const rows = ref<Content[]>([])
const loading = ref(false)
const error = ref('')
type FilterKey = 'ALL' | 'REVIEW' | 'APPROVED'
const filter = ref<FilterKey>('ALL')

const TYPE_LABEL: Record<string, string> = {
  ARTICLE: '文章',
  VIDEO_SCRIPT: '口播稿',
  POST: '短帖',
  AUDIO: '音频稿',
  OTHER: '其他',
}

const visible = computed(() =>
  filter.value === 'ALL' ? rows.value : rows.value.filter((r) => r.status === filter.value),
)

const counts = computed(() => ({
  all: rows.value.length,
  review: rows.value.filter((r) => r.status === 'REVIEW').length,
  approved: rows.value.filter((r) => r.status === 'APPROVED').length,
}))

const filters = computed<Array<{ key: FilterKey; label: string; n: number }>>(() => [
  { key: 'ALL', label: '全部', n: counts.value.all },
  { key: 'REVIEW', label: '待审核', n: counts.value.review },
  { key: 'APPROVED', label: '已通过', n: counts.value.approved },
])

async function load(): Promise<void> {
  if (!ws.currentId) return
  loading.value = true
  error.value = ''
  try {
    rows.value = await contentsApi.list(ws.currentId, { limit: 100 })
  } catch (err) {
    error.value = errorMessage(err)
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="mx-auto max-w-[1080px] px-8 py-9 lg:px-10">
    <header class="mb-7 flex flex-wrap items-end justify-between gap-4">
      <div>
        <p class="eyebrow">Library · 内容库</p>
        <h1 class="display mt-2 text-[27px]">成稿、版本与人工判断。</h1>
        <p class="mt-2 text-[13px] leading-relaxed text-ink-2">
          每一次人工修改都会生成新版本 —— 版本差异本身就是「什么算好」的证据。
        </p>
      </div>
      <button class="btn-ghost btn-sm" :disabled="loading" @click="load">
        <RefreshCw :size="12" :class="{ 'animate-spin': loading }" /> 刷新
      </button>
    </header>

    <div class="mb-4 flex items-center gap-1.5">
      <button
        v-for="item in filters"
        :key="item.key"
        class="rounded-full border px-3 py-1 text-[12px] transition"
        :class="
          filter === item.key
            ? 'border-ink bg-ink text-paper-2'
            : 'border-line bg-transparent text-ink-2 hover:border-[#cfc6b6]'
        "
        @click="filter = item.key"
      >
        {{ item.label }}
        <span class="num ml-0.5 opacity-60">{{ item.n }}</span>
      </button>
    </div>

    <div v-if="error" class="rounded-[var(--radius-card)] border border-[#eccbc6] bg-bad-soft px-5 py-4 text-[12.5px] text-bad">
      {{ error }}
    </div>

    <div v-else-if="loading && !rows.length" class="surface flex items-center gap-2 px-5 py-6 text-[12.5px] text-muted">
      <Loader2 :size="13" class="animate-spin" /> 读取中…
    </div>

    <div v-else-if="visible.length" class="surface divide-y divide-[#ece8df] overflow-hidden">
      <RouterLink
        v-for="row in visible"
        :key="row.id"
        :to="`/w/${ws.currentId}/contents/${row.id}`"
        class="group flex items-center gap-4 px-5 py-4 transition hover:bg-[#f7f4ee]"
      >
        <div class="grid h-9 w-9 shrink-0 place-items-center rounded-md border border-line bg-white text-muted">
          <FileText :size="15" />
        </div>

        <div class="min-w-0 flex-1">
          <p class="truncate font-serif text-[15.5px] text-ink">{{ row.title }}</p>
          <p class="mt-1 flex items-center gap-2 text-[11.5px] text-muted">
            <span>{{ TYPE_LABEL[row.content_type] ?? row.content_type }}</span>
            <span class="text-[#d5cec2]">·</span>
            <span class="num">v{{ row.current_version_number }}</span>
            <span class="text-[#d5cec2]">·</span>
            <span>更新于 {{ relativeTime(row.updated_at) }}</span>
          </p>
        </div>

        <StatusChip :status="row.status" kind="content" />
        <ArrowUpRight
          :size="15"
          class="shrink-0 text-[#c9c1b3] transition group-hover:text-accent"
        />
      </RouterLink>
    </div>

    <div v-else class="rounded-[var(--radius-card)] border border-dashed border-line px-8 py-14 text-center">
      <p class="display text-[16px]">这里还是空的</p>
      <p class="mx-auto mt-2 max-w-[380px] text-[12.5px] leading-relaxed text-muted">
        内容来自选题台的「选定 → 深入 → 写作 → 审核」。先去挑一个选题，成稿会自动出现在这里。
      </p>
      <RouterLink :to="`/w/${ws.currentId}/studio`" class="btn-primary mt-5">
        去选题台 <ArrowUpRight :size="13" />
      </RouterLink>
    </div>
  </div>
</template>
