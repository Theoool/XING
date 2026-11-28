import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

import { useWorkspaceStore } from '@/stores/workspace'

/** 工作区是租户边界，也是 URL 的一部分。
 *
 * 放在 localStorage 里的时候，同一个浏览器开两个标签页会互相改租户，
 * `/contents/:id` 也只能在「当前租户」里解释 —— 链接不可分享，刷新即丢上下文。
 * 现在它是路径的第一段：/:wsId/...，切换工作区 = 一次导航 = 组件自然重挂。
 *
 * 旧路径（/studio、/contents/:id、/tasks）保留为同名路由，守卫会把它们补成带
 * 工作区的地址，所以历史书签仍然可用。
 */
const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/studio' },
  {
    path: '/w/:wsId',
    redirect: (to) => ({ path: `/w/${String(to.params.wsId)}/studio` }),
  },
  {
    path: '/w/:wsId/studio',
    name: 'studio',
    component: () => import('@/views/TopicStudio.vue'),
    meta: { title: '选题台' },
  },
  {
    path: '/w/:wsId/contents',
    name: 'contents',
    component: () => import('@/views/ContentLibrary.vue'),
    meta: { title: '内容库' },
  },
  {
    path: '/w/:wsId/contents/:id',
    name: 'content',
    component: () => import('@/views/ContentDetail.vue'),
    props: true,
    meta: { title: '内容详情' },
  },
  {
    path: '/w/:wsId/tasks',
    name: 'tasks',
    component: () => import('@/views/TaskMonitor.vue'),
    meta: { title: '任务流' },
  },
  {
    // 战略是整条流水线的输入：展开/评分的提示词、事件相关度的关键词都读它。
    // 放在导航里而不是藏进设置，因为「改配置 = 新增一版」这件事本身需要被看见。
    path: '/w/:wsId/strategy',
    name: 'strategy',
    component: () => import('@/views/StrategyView.vue'),
    meta: { title: '内容战略' },
  },
  {
    // 取证：一个任务具体怎么搜、怎么想、怎么判定。按任务 id 而不是选题 id，
    // 因为它对「展开 / 深入 / 写作」都成立 —— 任何一步都能查它自己的过程。
    path: '/w/:wsId/trails/:taskId',
    name: 'trail',
    component: () => import('@/views/TraceView.vue'),
    props: true,
    meta: { title: '取证过程' },
  },

  // 旧地址：守卫会在渲染前补上 /w/:wsId，这里只需要一条能匹配的规则。
  { path: '/studio', name: 'legacy-studio', component: () => import('@/views/TopicStudio.vue') },
  {
    path: '/contents',
    name: 'legacy-contents',
    component: () => import('@/views/ContentLibrary.vue'),
  },
  {
    path: '/contents/:id',
    name: 'legacy-content',
    component: () => import('@/views/ContentDetail.vue'),
    props: true,
  },
  { path: '/tasks', name: 'legacy-tasks', component: () => import('@/views/TaskMonitor.vue') },

  { path: '/:pathMatch(.*)*', redirect: '/studio' },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

/** 目标地址里的工作区是否可用；不可用时补成当前工作区，保留原视图。 */
function withWorkspace(to: { path: string; fullPath: string }, wsId: string) {
  // /w/<错的 id>/contents/x → /contents/x；旧路径原样保留（含 query）。
  const suffix = to.path.startsWith('/w/')
    ? to.path.replace(/^\/w\/[^/]+/, '') || '/studio'
    : to.fullPath
  return `/w/${wsId}${suffix}`
}

router.beforeEach(async (to) => {
  const ws = useWorkspaceStore()
  await ws.ensureLoaded()

  // 一条工作区都没有：让 App 渲染引导页，先把「在哪写」解决掉。
  if (!ws.list.length) return true

  const param = to.params.wsId
  const wsId = typeof param === 'string' ? param : null
  if (wsId && ws.has(wsId)) {
    ws.select(wsId)
    return true
  }

  // 没有工作区段（旧链接），或者 id 不属于当前用户：换成合法地址，别让用户停在错租户上。
  const fallback = ws.currentId && ws.has(ws.currentId) ? ws.currentId : ws.list[0].id
  return withWorkspace(to, fallback)
})

export function workspacePath(wsId: string | null, suffix: string): string {
  return wsId ? `/w/${wsId}${suffix}` : suffix
}
