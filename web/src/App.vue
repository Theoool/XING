<script setup lang="ts">
import { ArrowRight, Loader2, RefreshCw, WifiOff } from 'lucide-vue-next'
import { computed, onMounted, ref } from 'vue'
import { RouterView } from 'vue-router'

import AppSidebar from '@/components/AppSidebar.vue'
import ToastHost from '@/components/ToastHost.vue'
import { router } from '@/router'
import { useToastStore } from '@/stores/toast'
import { useWorkspaceStore } from '@/stores/workspace'

const ws = useWorkspaceStore()
const toast = useToastStore()

const name = ref('')
const creating = ref(false)

/** 列表还没读回来之前既不是「有工作区」也不是「没有」，先给一个安静的等待态。 */
const booting = computed(() => !ws.booted && !ws.error)
const needsOnboarding = computed(() => ws.booted && ws.list.length === 0)

async function create(): Promise<void> {
  const value = name.value.trim()
  if (!value) return
  creating.value = true
  try {
    const created = await ws.create(value)
    toast.push(`欢迎，${value}`, 'success')
    // 创建工作区 = 进入它的工作室。路由是租户的权威来源，所以这里必须导航。
    await router.replace(`/w/${created.id}/studio`)
  } catch (err) {
    toast.push(err instanceof Error ? err.message : '创建失败', 'error')
  } finally {
    creating.value = false
  }
}

/** 后端起来之后重试：列表读到了就补上工作区地址，否则守卫不会自己再跑一次。 */
async function retry(): Promise<void> {
  await ws.load()
  if (ws.currentId) await router.replace(`/w/${ws.currentId}/studio`)
}

onMounted(() => {
  void ws.ensureLoaded()
  router.afterEach((to) => {
    const title = to.meta.title as string | undefined
    document.title = title ? `XING · ${title}` : 'XING'
  })
})
</script>

<template>
  <!-- 工作区列表读回来之前，不判定任何事：既不说「没有工作区」，也不渲染空壳主界面。 -->
  <div v-if="booting" class="grid min-h-full place-items-center px-6 py-16">
    <div class="flex items-center gap-2 text-[12.5px] text-muted">
      <Loader2 :size="13" class="animate-spin" /> 正在读取工作区…
    </div>
  </div>

  <!-- 首次进入：没有工作区时不给看空壳界面，直接让人建一个。 -->
  <div v-else-if="needsOnboarding" class="grid min-h-full place-items-center px-6 py-16">
    <div class="w-full max-w-[440px] animate-rise">
      <div class="mb-8 flex items-center gap-2.5">
        <span class="display text-[26px] tracking-[0.2em]">XING</span>
        <span class="h-1.5 w-1.5 rounded-full bg-accent" />
      </div>

      <div v-if="ws.error" class="surface p-6">
        <WifiOff :size="18" class="mb-3 text-bad" />
        <h1 class="display text-[21px]">连不上后端</h1>
        <p class="mt-2 text-[13px] leading-relaxed text-ink-2">{{ ws.error }}</p>
        <p class="mt-1 text-[12px] text-muted">
          开发环境下请先执行 <code class="rounded bg-[#efece5] px-1 py-0.5">docker compose up -d</code>，
          前端通过 <code class="rounded bg-[#efece5] px-1 py-0.5">/api</code> 代理到 8000 端口。
        </p>
        <button class="btn-ghost mt-4" @click="retry">
          <RefreshCw :size="13" /> 重试
        </button>
      </div>

      <template v-else>
        <p class="eyebrow mb-3">Step 01 / 工作区</p>
        <h1 class="display text-[27px] leading-snug">先有一块自己的地方。</h1>
        <p class="mt-3 mb-7 text-[13.5px] leading-relaxed text-ink-2">
          选题、证据、内容都挂在工作区下。它是租户边界，也是数据隔离的单位 ——
          所以第一个问题不是「写什么」，而是「在哪写」。
        </p>

        <form class="surface p-1.5" @submit.prevent="create">
          <div class="flex items-center gap-1">
            <input
              v-model="name"
              autofocus
              maxlength="100"
              placeholder="例如：AI 与组织"
              class="min-w-0 flex-1 bg-transparent px-3 py-2.5 text-[14px] outline-none placeholder:text-[#b3ab9e]"
            />
            <button
              type="submit"
              class="btn-primary"
              :disabled="creating || !name.trim()"
            >
              <Loader2 v-if="creating" :size="14" class="animate-spin" />
              <template v-else>
                创建 <ArrowRight :size="14" />
              </template>
            </button>
          </div>
        </form>

        <button
          v-if="ws.loading"
          class="mt-3 flex items-center gap-1.5 text-[12px] text-muted"
          disabled
        >
          <Loader2 :size="12" class="animate-spin" /> 正在读取已有工作区…
        </button>
      </template>
    </div>
  </div>

  <div v-else class="flex h-full">
    <AppSidebar />
    <main class="min-w-0 flex-1 overflow-y-auto">
      <!-- key 绑工作区：切租户必须重挂视图，否则屏幕上还是上一个租户的数据。 -->
      <RouterView v-slot="{ Component }">
        <component :is="Component" :key="ws.currentId ?? 'none'" />
      </RouterView>
    </main>
  </div>

  <ToastHost />
</template>
