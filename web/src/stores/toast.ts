import { defineStore } from 'pinia'
import { ref } from 'vue'

export type ToastKind = 'info' | 'success' | 'error'

export interface Toast {
  id: number
  kind: ToastKind
  text: string
}

export const useToastStore = defineStore('toast', () => {
  const items = ref<Toast[]>([])
  let seq = 0

  function push(text: string, kind: ToastKind = 'info', ttl = 4200): void {
    const id = ++seq
    items.value = [...items.value, { id, kind, text }]
    window.setTimeout(() => dismiss(id), ttl)
  }

  function dismiss(id: number): void {
    items.value = items.value.filter((t) => t.id !== id)
  }

  return { items, push, dismiss }
})
