<script setup lang="ts">
import { CheckCircle2, Info, X, XCircle } from 'lucide-vue-next'

import { useToastStore } from '@/stores/toast'

const toast = useToastStore()

const styleOf = {
  info: 'border-line bg-paper-2 text-ink',
  success: 'border-[#c7ddd0] bg-ok-soft text-ok',
  error: 'border-[#eccbc6] bg-bad-soft text-bad',
} as const
</script>

<template>
  <div class="pointer-events-none fixed right-6 bottom-6 z-100 flex w-[360px] flex-col gap-2">
    <TransitionGroup
      enter-from-class="translate-y-2 opacity-0"
      enter-active-class="transition duration-200"
      leave-to-class="translate-y-1 opacity-0"
      leave-active-class="transition duration-150"
    >
      <div
        v-for="item in toast.items"
        :key="item.id"
        class="pointer-events-auto flex items-start gap-2.5 rounded-lg border px-3.5 py-3 text-[13px] shadow-[0_8px_28px_rgba(23,19,15,0.12)]"
        :class="styleOf[item.kind]"
      >
        <CheckCircle2 v-if="item.kind === 'success'" :size="16" class="mt-0.5 shrink-0" />
        <XCircle v-else-if="item.kind === 'error'" :size="16" class="mt-0.5 shrink-0" />
        <Info v-else :size="16" class="mt-0.5 shrink-0 opacity-70" />
        <p class="min-w-0 flex-1 leading-relaxed">{{ item.text }}</p>
        <button class="opacity-50 transition hover:opacity-100" @click="toast.dismiss(item.id)">
          <X :size="14" />
        </button>
      </div>
    </TransitionGroup>
  </div>
</template>
