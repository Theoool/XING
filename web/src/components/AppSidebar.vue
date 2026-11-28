<script setup lang="ts">
import {
  Activity,
  Check,
  ChevronsUpDown,
  Compass,
  Feather,
  FolderOpen,
  Loader2,
  Plus,
} from 'lucide-vue-next'
import { computed, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import { useChainStore } from '@/stores/chain'
import { useToastStore } from '@/stores/toast'
import { useWorkspaceStore } from '@/stores/workspace'

const ws = useWorkspaceStore()
const chain = useChainStore()
const toast = useToastStore()
const route = useRoute()
const router = useRouter()

const open = ref(false)
const creating = ref(false)
const draftName = ref('')
const saving = ref(false)

/** 导航地址跟着工作区走。切换工作区就是换掉地址的第一段，不是改一个全局变量。 */
const nav = computed(() => {
  const base = ws.currentId ? `/w/${ws.currentId}` : ''
  return [
    { key: 'studio', to: `${base}/studio`, label: '选题台', icon: Feather, hint: '种子 → 候选 → 深入' },
    { key: 'contents', to: `${base}/contents`, label: '内容库', icon: FolderOpen, hint: '成稿与人工审核' },
    { key: 'tasks', to: `${base}/tasks`, label: '任务流', icon: Activity, hint: '每个 Agent 的执行记录' },
    { key: 'strategy', to: `${base}/strategy`, label: '内容战略', icon: Compass, hint: '定位、读者、语气与边界' },
  ]
})

const initial = computed(() => (ws.current?.name ?? 'X').slice(0, 1).toUpperCase())

function isActive(key: string): boolean {
  // 内容详情属于内容库：用路由名判断，不用路径前缀（/w/:id/contents/:cid）。
  if (key === 'contents') return route.name === 'contents' || route.name === 'content'
  return route.name === key
}

/** 切工作区时保留当前视图，只换租户段。 */
function switchTo(id: string): void {
  const suffix =
    route.name === 'content'
      ? `/contents/${String(route.params.id)}`
      : `/${typeof route.name === 'string' && !route.name.startsWith('legacy') ? route.name : 'studio'}`
  open.value = false
  if (id === ws.currentId) return
  void router.push(`/w/${id}${suffix}`)
}

async function createWorkspace(): Promise<void> {
  const name = draftName.value.trim()
  if (!name) return
  saving.value = true
  try {
    const created = await ws.create(name)
    toast.push(`工作区「${name}」已创建`, 'success')
    creating.value = false
    draftName.value = ''
    open.value = false
    await router.push(`/w/${created.id}/studio`)
  } catch (err) {
    toast.push(err instanceof Error ? err.message : '创建失败', 'error')
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <aside class="flex w-[236px] shrink-0 flex-col bg-ink text-[#ece6dc]">
    <div class="px-5 pt-6 pb-7">
      <div class="flex items-baseline gap-2">
        <span class="display text-[23px] leading-none tracking-[0.18em] text-[#f6f1e8]">XING</span>
        <span class="h-1.5 w-1.5 rounded-full bg-accent" />
      </div>
      <p class="mt-2 text-[10.5px] tracking-[0.16em] text-[#857b6d] uppercase">
        Content Studio
      </p>
    </div>

    <nav class="flex flex-col gap-0.5 px-2.5">
      <RouterLink
        v-for="item in nav"
        :key="item.key"
        :to="item.to"
        class="group relative flex items-center gap-2.5 rounded-md px-2.5 py-2.5 transition-colors"
        :class="
          isActive(item.key)
            ? 'bg-white/[0.07] text-[#f6f1e8]'
            : 'text-[#a99f90] hover:bg-white/[0.04] hover:text-[#e6dfd3]'
        "
      >
        <span
          v-if="isActive(item.key)"
          class="absolute top-1/2 left-0 h-4 w-[2px] -translate-y-1/2 bg-accent"
        />
        <component :is="item.icon" :size="15" :stroke-width="1.7" class="shrink-0" />
        <span class="flex-1 text-[13px]">{{ item.label }}</span>
        <Loader2
          v-if="item.key === 'studio' && chain.busy"
          :size="12"
          class="animate-spin text-accent"
        />
      </RouterLink>
    </nav>

    <div class="mt-auto px-2.5 pb-4">
      <div class="mb-2 px-1 text-[10px] tracking-[0.16em] text-[#6f665a] uppercase">工作区</div>

      <div class="relative">
        <button
          class="flex w-full items-center gap-2.5 rounded-md border border-white/[0.08] px-2 py-2 text-left transition hover:border-white/20 hover:bg-white/[0.04]"
          @click="open = !open"
        >
          <span
            class="grid h-7 w-7 shrink-0 place-items-center rounded bg-[#2b241c] text-[12px] font-medium text-[#e8dfd0]"
          >
            {{ initial }}
          </span>
          <span class="min-w-0 flex-1">
            <span class="block truncate text-[12.5px] text-[#e8e1d5]">
              {{ ws.current?.name ?? '未选择' }}
            </span>
            <span class="block text-[10.5px] text-[#7c7265]">
              {{ ws.list.length }} 个可访问
            </span>
          </span>
          <ChevronsUpDown :size="13" class="shrink-0 text-[#7c7265]" />
        </button>

        <div v-if="open" class="fixed inset-0 z-40" @click="open = false" />

        <div
          v-if="open"
          class="absolute bottom-full left-0 z-50 mb-2 w-full overflow-hidden rounded-lg border border-white/10 bg-[#211b15] shadow-[0_18px_40px_rgba(0,0,0,0.45)]"
        >
          <div class="max-h-56 overflow-y-auto p-1.5">
            <button
              v-for="item in ws.list"
              :key="item.id"
              class="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[12.5px] transition hover:bg-white/[0.06]"
              :class="item.id === ws.currentId ? 'text-[#f4eee2]' : 'text-[#a99f90]'"
              @click="switchTo(item.id)"
            >
              <span class="min-w-0 flex-1 truncate">{{ item.name }}</span>
              <Check v-if="item.id === ws.currentId" :size="13" class="text-accent" />
            </button>
          </div>

          <div class="border-t border-white/10 p-1.5">
            <div v-if="creating" class="flex items-center gap-1.5 px-1 py-1">
              <input
                v-model="draftName"
                autofocus
                placeholder="工作区名称"
                class="min-w-0 flex-1 rounded border border-white/15 bg-[#171310] px-2 py-1.5 text-[12.5px] text-[#eee7da] outline-none placeholder:text-[#6f665a] focus:border-accent/60"
                @keydown.enter="createWorkspace"
                @keydown.esc="creating = false"
              />
              <button
                class="rounded px-2 py-1.5 text-[12px] text-accent disabled:opacity-40"
                :disabled="saving || !draftName.trim()"
                @click="createWorkspace"
              >
                {{ saving ? '…' : '建' }}
              </button>
            </div>
            <button
              v-else
              class="flex w-full items-center gap-2 rounded px-2 py-1.5 text-[12.5px] text-[#a99f90] transition hover:bg-white/[0.06] hover:text-[#e8e1d5]"
              @click="creating = true"
            >
              <Plus :size="13" />
              新建工作区
            </button>
          </div>
        </div>
      </div>

      <p class="mt-4 px-1 text-[10px] leading-relaxed text-[#5f574c]">
        每次展开都会真实调用模型<br />并产生可核算的成本
      </p>
    </div>
  </aside>
</template>
