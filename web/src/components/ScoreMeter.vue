<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  label: string
  value: number | null | undefined
  tone?: 'ink' | 'accent' | 'gold'
}>()

const pct = computed(() => {
  if (props.value === null || props.value === undefined) return null
  return Math.round(props.value * 100)
})

const colors = {
  ink: 'bg-ink',
  accent: 'bg-accent',
  gold: 'bg-[#a98435]',
} as const
</script>

<template>
  <div class="min-w-0">
    <div class="mb-1.5 flex items-baseline justify-between gap-2">
      <span class="text-[10.5px] tracking-[0.06em] text-muted">{{ label }}</span>
      <span
        class="num text-[12px] font-medium"
        :class="pct === null ? 'text-[#b6ae9f]' : 'text-ink'"
      >
        {{ pct === null ? '—' : pct }}
      </span>
    </div>
    <div class="h-[3px] overflow-hidden rounded-full bg-[#e7e1d6]">
      <div
        v-if="pct !== null"
        class="h-full rounded-full transition-[width] duration-500"
        :class="colors[props.tone ?? 'ink']"
        :style="{ width: `${Math.max(2, Math.min(100, pct))}%` }"
      />
    </div>
  </div>
</template>
