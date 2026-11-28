<script setup lang="ts">
import { ArrowUpRight, Quote, ScanSearch } from 'lucide-vue-next'
import { computed } from 'vue'

import type { Topic } from '@/api/types'
import ScoreMeter from '@/components/ScoreMeter.vue'
import StatusChip from '@/components/StatusChip.vue'

const props = defineProps<{
  topic: Topic
  index: number
  active?: boolean
  busy?: boolean
}>()

const emit = defineEmits<{
  select: [topic: Topic]
  inspect: [topic: Topic]
}>()

const rank = computed(() => String(props.index + 1).padStart(2, '0'))
const selected = computed(() => props.active || props.topic.status === 'SELECTED')
const recommended = computed(() => props.topic.status === 'RECOMMENDED')
const opinion = computed(() => props.topic.brief?.opinion ?? null)
// 检测还没跑完就不能选定：后端会以 409 OPINION_PENDING 拒绝，
// 界面上也不该给一个点下去必然失败的按钮。
const pending = computed(() => !opinion.value?.checked)
const blocked = computed(
  () =>
    props.topic.status === 'REJECTED' ||
    pending.value ||
    opinion.value?.verdict === 'UNGROUNDED',
)
const weak = computed(() => opinion.value?.verdict === 'WEAK')
</script>

<template>
  <article
    class="group relative flex flex-col rounded-[var(--radius-card)] border bg-paper-2 p-5 transition-all duration-200"
    :class="
      selected
        ? 'border-accent/45 shadow-[0_0_0_3px_rgba(191,67,39,0.06)]'
        : recommended
          ? 'border-[#d9d2c5] hover:border-[#c6bcab] hover:shadow-[0_6px_20px_rgba(23,19,15,0.06)]'
          : 'border-line hover:border-[#cfc6b6] hover:shadow-[0_6px_20px_rgba(23,19,15,0.05)]'
    "
  >
    <header class="mb-3 flex items-start justify-between gap-3">
      <span class="display text-[26px] leading-none text-[#d5ccbc]">{{ rank }}</span>
      <div class="flex items-center gap-1.5">
        <span
          v-if="weak"
          class="chip border-[#e7d6bd] bg-warn-soft text-warn"
          title="舆论检测：切面偏弱或与其它候选重叠"
        >
          切面偏弱
        </span>
        <StatusChip :status="topic.status" kind="topic" />
      </div>
    </header>

    <h3 class="display text-[18.5px] leading-[1.45] font-semibold">
      {{ topic.title }}
    </h3>

    <div v-if="topic.brief?.angle" class="mt-3 flex gap-2">
      <span class="mt-[2px] h-[13px] w-[2px] shrink-0 bg-accent/70" />
      <p class="text-[12.5px] leading-relaxed text-ink-2">
        <span class="text-muted">切面 ·</span> {{ topic.brief.angle }}
      </p>
    </div>

    <p
      v-if="topic.brief?.thesis"
      class="mt-3 line-clamp-3 text-[13px] leading-[1.7] text-[#5c554b]"
    >
      {{ topic.brief.thesis }}
    </p>

    <p
      v-if="topic.brief?.why_now"
      class="mt-2.5 flex items-start gap-1.5 text-[11.5px] leading-relaxed text-muted"
    >
      <Quote :size="11" class="mt-[3px] shrink-0" />
      <span class="line-clamp-2">{{ topic.brief.why_now }}</span>
    </p>

    <div class="mt-4 grid grid-cols-3 gap-3.5 border-t border-line-soft pt-3.5">
      <ScoreMeter label="相关" :value="topic.relevance_score" tone="ink" />
      <ScoreMeter label="潜力" :value="topic.potential_score" tone="accent" />
      <ScoreMeter label="时效" :value="topic.trend_score" tone="gold" />
    </div>

    <footer class="mt-4 flex items-center justify-between gap-2">
      <span class="min-w-0 truncate text-[11.5px] text-muted">
        {{ topic.brief?.audience ?? '未标注受众' }}
      </span>
      <div class="flex shrink-0 items-center gap-1">
        <button class="btn-ghost btn-sm" @click="emit('inspect', topic)">
          <ScanSearch :size="12" /> 详情
        </button>
        <button
          class="btn-sm"
          :class="selected ? 'btn-ghost text-accent' : 'btn-primary'"
          :disabled="busy || blocked"
          @click="emit('select', topic)"
        >
          <!-- 已选定的题不重新决策，但可以重新接上链路看进度：
               刷新页面后前端状态会丢，后端还在跑。 -->
          <template v-if="selected">看进度</template>
          <template v-else-if="pending">待检测</template>
          <template v-else-if="blocked">未通过检测</template>
          <template v-else>
            选定 <ArrowUpRight :size="12" />
          </template>
        </button>
      </div>
    </footer>
  </article>
</template>
