<script setup lang="ts">
import { AlertTriangle, Check, Clock, Loader2, RotateCcw } from 'lucide-vue-next'
import { computed } from 'vue'
import { RouterLink } from 'vue-router'

import type { ChainStepStatus } from '@/api/types'
import { formatDuration } from '@/lib/format'
import { useChainStore } from '@/stores/chain'
import { useWorkspaceStore } from '@/stores/workspace'

const chain = useChainStore()
const ws = useWorkspaceStore()

/** 执行事实 -> 视觉状态。后端给的是任务状态，界面不该再自己猜。 */
function visual(status: ChainStepStatus): 'done' | 'active' | 'queued' | 'failed' | 'todo' {
  switch (status) {
    case 'DONE':
      return 'done'
    case 'RUNNING':
      return 'active'
    case 'WAITING':
    case 'QUEUED':
    case 'RETRYING':
      return 'queued'
    case 'FAILED':
    case 'CANCELLED':
      return 'failed'
    default:
      return 'todo'
  }
}

const view = computed(() =>
  chain.steps.map((step) => ({ ...step, state: visual(step.status) })),
)
</script>

<template>
  <section class="surface overflow-hidden">
    <div class="relative flex items-center gap-0 overflow-x-auto px-5 py-4">
      <div
        v-if="chain.busy && !chain.stalled"
        class="sweep-bar absolute inset-x-0 top-0 h-[1.5px] overflow-hidden bg-[#efe9df]"
      />

      <template v-for="(step, i) in view" :key="step.key">
        <div class="flex shrink-0 items-center gap-2">
          <span
            class="grid h-[19px] w-[19px] place-items-center rounded-full border text-[10px] font-medium transition-colors"
            :class="{
              'border-ink bg-ink text-paper': step.state === 'done',
              'border-accent bg-accent-soft text-accent': step.state === 'active',
              'border-accent/50 bg-transparent text-accent': step.state === 'queued',
              'border-bad bg-bad-soft text-bad': step.state === 'failed',
              'border-line bg-transparent text-[#b3ab9e]': step.state === 'todo',
            }"
          >
            <Check v-if="step.state === 'done'" :size="11" :stroke-width="2.6" />
            <Loader2 v-else-if="step.state === 'active'" :size="10" class="animate-spin" />
            <template v-else>{{ i + 1 }}</template>
          </span>
          <span
            class="text-[12.5px] whitespace-nowrap transition-colors"
            :class="{
              'text-ink': step.state === 'done',
              'font-medium text-accent': step.state === 'active' || step.state === 'queued',
              'font-medium text-bad': step.state === 'failed',
              'text-[#a9a194]': step.state === 'todo',
            }"
          >
            {{ step.label }}
          </span>
        </div>

        <div
          v-if="i < view.length - 1"
          class="mx-2 h-px min-w-2 flex-1"
          :class="
            step.state === 'done'
              ? 'bg-[#c9c0b0]'
              : step.state === 'failed'
                ? 'bg-[#e3bdb6]'
                : 'bg-line-soft'
          "
        />
      </template>

      <div class="ml-4 flex shrink-0 items-center gap-1.5 text-[11.5px] text-muted">
        <Clock :size="12" />
        <span class="num">{{ formatDuration(chain.elapsed) }}</span>
      </div>
    </div>

    <div
      class="flex items-start gap-2 border-t px-5 py-3 text-[12.5px]"
      :class="
        chain.phase === 'failed' || chain.phase === 'rejected'
          ? 'border-[#efd6d1] bg-bad-soft/60 text-bad'
          : chain.stalled
            ? 'border-[#ece0c4] bg-warn-soft/70 text-warn'
            : 'border-line-soft bg-[#f8f6f1] text-ink-2'
      "
    >
      <template v-if="(chain.phase === 'failed' || chain.phase === 'rejected') && chain.error">
        <AlertTriangle :size="14" class="mt-0.5 shrink-0" />
        <span class="min-w-0 flex-1">
          {{ chain.error.message }}
          <code class="ml-1.5 rounded bg-white/60 px-1 py-0.5 text-[11px]">
            {{ chain.error.code }}
          </code>
        </span>
        <div class="flex shrink-0 items-center gap-2">
          <button
            v-for="item in chain.retryableSteps"
            :key="item.key"
            class="btn-ghost btn-sm"
            :disabled="chain.busy"
            @click="chain.retryStep(item.key)"
          >
            <RotateCcw :size="11" /> {{ item.label }}
          </button>
          <button class="text-[12px] underline underline-offset-2" @click="chain.reset()">
            清空
          </button>
        </div>
      </template>
      <template v-else>
        <AlertTriangle v-if="chain.stalled" :size="14" class="mt-0.5 shrink-0" />
        <span v-else class="text-[#b0a89b]">当前</span>
        <span class="flex-1">{{ chain.currentHint }}</span>
        <RouterLink
          v-if="chain.stalled"
          :to="`/w/${ws.currentId}/tasks`"
          class="shrink-0 text-[12px] underline underline-offset-2"
        >
          去任务流
        </RouterLink>
        <span v-if="chain.review" class="shrink-0 text-[12px]">
          审核
          <span class="font-medium" :class="chain.review.verdict === 'PASS' ? 'text-ok' : 'text-bad'">
            {{ chain.review.verdict }} {{ chain.review.score }}
          </span>
        </span>
      </template>
    </div>
  </section>
</template>
