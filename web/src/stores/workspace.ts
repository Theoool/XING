import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { workspacesApi } from '@/api'
import { errorMessage } from '@/api/client'
import type { Workspace } from '@/api/types'

const STORAGE_KEY = 'xing.workspace_id'

export const useWorkspaceStore = defineStore('workspace', () => {
  const list = ref<Workspace[]>([])
  /** 当前工作区。它由路由里的 :wsId 决定（见 router.ts 的守卫），
   * 这里只是镜像 —— 组件不再靠它判断「该拉哪个租户」，路由参数才是权威。 */
  const currentId = ref<string | null>(localStorage.getItem(STORAGE_KEY))
  const loading = ref(false)
  const error = ref('')
  /** 列表至少成功读过一次。没读过之前不能判定「没有工作区」，
   * 否则首屏会闪一下「请创建工作区」。 */
  const booted = ref(false)

  let boot: Promise<void> | null = null

  const current = computed(() => list.value.find((w) => w.id === currentId.value) ?? null)
  const ready = computed(() => Boolean(current.value))

  async function load(): Promise<void> {
    loading.value = true
    error.value = ''
    try {
      list.value = await workspacesApi.list()
      // 兼容旧链接：?ws=<id> 只接受列表里的 id，不能让 URL 决定租户。
      const fromUrl = new URLSearchParams(window.location.search).get('ws')
      if (fromUrl && list.value.some((w) => w.id === fromUrl)) {
        currentId.value = fromUrl
        persist()
        return
      }
      // 本地记住的 id 可能已被删除或不属于当前用户：只有命中列表才认它。
      if (!list.value.some((w) => w.id === currentId.value)) {
        currentId.value = list.value[0]?.id ?? null
        persist()
      }
    } catch (err) {
      error.value = errorMessage(err)
    } finally {
      loading.value = false
      booted.value = true
    }
  }

  /** 首次需要工作区列表时调用。守卫可能在每次导航都进来，所以必须幂等。 */
  async function ensureLoaded(): Promise<void> {
    if (booted.value) return
    if (!boot) boot = load().finally(() => (boot = null))
    await boot
  }

  async function create(name: string): Promise<Workspace> {
    const ws = await workspacesApi.create(name)
    list.value = [ws, ...list.value]
    currentId.value = ws.id
    persist()
    return ws
  }

  function has(id: string): boolean {
    return list.value.some((w) => w.id === id)
  }

  /** 只改镜像，不做导航。导航由调用方（侧栏 / 守卫）负责。 */
  function select(id: string): void {
    if (!has(id)) return
    currentId.value = id
    persist()
  }

  function persist(): void {
    if (currentId.value) localStorage.setItem(STORAGE_KEY, currentId.value)
    else localStorage.removeItem(STORAGE_KEY)
  }

  return {
    list,
    currentId,
    current,
    ready,
    booted,
    loading,
    error,
    load,
    ensureLoaded,
    create,
    select,
    has,
  }
})
