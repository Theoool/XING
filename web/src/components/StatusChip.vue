<script setup lang="ts">
import { computed } from 'vue'

import {
  CONTENT_STATUS_LABEL,
  TASK_STATUS_LABEL,
  TOPIC_STATUS_LABEL,
  type TaskStatus,
} from '@/api/types'

const props = defineProps<{
  status: string
  kind?: 'topic' | 'task' | 'content'
}>()

const label = computed(() => {
  if (props.kind === 'task') {
    return TASK_STATUS_LABEL[props.status as TaskStatus] ?? props.status
  }
  if (props.kind === 'content') return CONTENT_STATUS_LABEL[props.status] ?? props.status
  return TOPIC_STATUS_LABEL[props.status] ?? props.status
})

/** 颜色只表达「需要你注意什么」：绿=完成，红=失败，黄=等待，灰=中性。 */
const tone = computed(() => {
  const s = props.status
  if (['SUCCEEDED', 'APPROVED', 'PUBLISHED', 'DONE'].includes(s)) return 'ok'
  if (['FAILED', 'REJECTED', 'CANCELLED'].includes(s)) return 'bad'
  if (['RUNNING', 'RETRYING', 'REVIEW', 'SELECTED', 'RECOMMENDED', 'PRODUCING'].includes(s))
    return 'accent'
  if (['READY', 'PENDING', 'DRAFT', 'CANDIDATE'].includes(s)) return 'muted'
  return 'muted'
})

const classes: Record<string, string> = {
  ok: 'border-[#c7ddd0] bg-ok-soft text-ok',
  bad: 'border-[#eccbc6] bg-bad-soft text-bad',
  accent: 'border-[#eed3c9] bg-accent-soft text-accent',
  muted: 'border-line bg-[#f1eee7] text-ink-2',
}
</script>

<template>
  <span class="chip" :class="classes[tone]">
    <span
      v-if="tone === 'accent'"
      class="pulse-dot inline-block h-1.5 w-1.5 rounded-full bg-current"
    />
    {{ label }}
  </span>
</template>
