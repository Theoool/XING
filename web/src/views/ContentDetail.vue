<script setup lang="ts">
import DOMPurify from 'dompurify'
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  ChevronRight,
  Loader2,
  ShieldCheck,
  ShieldX,
  XCircle,
} from 'lucide-vue-next'
import { marked } from 'marked'
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'

import { chainApi, contentsApi, topicsApi } from '@/api'
import { errorMessage } from '@/api/client'
import type { Approval, Content, ContentVersion, ReviewVerdict, Topic } from '@/api/types'
import StatusChip from '@/components/StatusChip.vue'
import { formatDate } from '@/lib/format'
import { useToastStore } from '@/stores/toast'
import { useWorkspaceStore } from '@/stores/workspace'

const props = defineProps<{ id: string }>()

const ws = useWorkspaceStore()
const toast = useToastStore()

const content = ref<Content | null>(null)
const versions = ref<ContentVersion[]>([])
const approvals = ref<Approval[]>([])
const review = ref<ReviewVerdict | null>(null)
const topic = ref<Topic | null>(null)
const loading = ref(true)
const error = ref('')
const acting = ref(false)
const comment = ref('')

const TYPE_LABEL: Record<string, string> = {
  ARTICLE: '文章',
  VIDEO_SCRIPT: '口播稿',
  POST: '短帖',
  AUDIO: '音频稿',
  OTHER: '其他',
}

marked.setOptions({ gfm: true, breaks: true })

const body = computed(() => content.value?.current_version?.body ?? '')

const html = computed(() => {
  if (!body.value) return ''
  const rendered = marked.parse(body.value, { async: false }) as string
  // 正文来自模型与人工，一律当不可信输入处理：渲染前消毒。
  return DOMPurify.sanitize(rendered, { USE_PROFILES: { html: true } })
})

const wordCount = computed(() => body.value.replace(/\s/g, '').length)

async function load(): Promise<void> {
  const workspaceId = ws.currentId
  if (!workspaceId) return
  loading.value = true
  error.value = ''
  try {
    const detail = await contentsApi.get(workspaceId, props.id)
    content.value = detail

    // 审核结论来自链路的派生状态，不再去任务表里找 review_content：
    // 那条任务可能早就被翻页翻掉了，找不到就等于「没有审核」，这是会骗人的。
    const [versionRows, approvalRows, chain] = await Promise.all([
      contentsApi.versions(workspaceId, detail.id).catch(() => []),
      contentsApi.approvals(workspaceId, detail.id).catch(() => []),
      detail.topic_id
        ? chainApi.status(workspaceId, detail.topic_id).catch(() => null)
        : Promise.resolve(null),
    ])
    versions.value = versionRows
    approvals.value = approvalRows
    review.value = chain?.review ?? null

    if (detail.topic_id) {
      topic.value = await topicsApi.get(workspaceId, detail.topic_id).catch(() => null)
    }
  } catch (err) {
    error.value = errorMessage(err)
  } finally {
    loading.value = false
  }
}

async function decide(action: 'approve' | 'reject'): Promise<void> {
  const workspaceId = ws.currentId
  if (!workspaceId || !content.value) return
  acting.value = true
  try {
    content.value =
      action === 'approve'
        ? await contentsApi.approve(workspaceId, content.value.id, comment.value || undefined)
        : await contentsApi.reject(workspaceId, content.value.id, comment.value || undefined)
    approvals.value = await contentsApi.approvals(workspaceId, content.value.id)
    comment.value = ''
    toast.push(action === 'approve' ? '已通过审核' : '已驳回', 'success')
  } catch (err) {
    toast.push(errorMessage(err), 'error')
  } finally {
    acting.value = false
  }
}

onMounted(load)
watch(() => props.id, load)
</script>

<template>
  <div class="mx-auto max-w-[1180px] px-8 py-8 lg:px-10">
    <RouterLink
      :to="`/w/${ws.currentId}/contents`"
      class="mb-6 inline-flex items-center gap-1.5 text-[12px] text-muted transition hover:text-ink"
    >
      <ArrowLeft :size="13" /> 内容库
    </RouterLink>

    <div v-if="loading" class="flex items-center gap-2 py-16 text-[12.5px] text-muted">
      <Loader2 :size="13" class="animate-spin" /> 读取中…
    </div>

    <div
      v-else-if="error"
      class="rounded-[var(--radius-card)] border border-[#eccbc6] bg-bad-soft px-5 py-4 text-[12.5px] text-bad"
    >
      {{ error }}
    </div>

    <template v-else-if="content">
      <header class="mb-7">
        <div class="mb-3 flex flex-wrap items-center gap-2">
          <StatusChip :status="content.status" kind="content" />
          <span class="text-[11.5px] text-muted">
            {{ TYPE_LABEL[content.content_type] ?? content.content_type }}
            · <span class="num">v{{ content.current_version_number }}</span>
            · {{ formatDate(content.updated_at) }}
            · <span class="num">{{ wordCount }}</span> 字
          </span>
        </div>
        <h1 class="display max-w-[820px] text-[27px] leading-[1.4] font-semibold">
          {{ content.title }}
        </h1>
        <RouterLink
          v-if="topic"
          :to="`/w/${ws.currentId}/studio`"
          class="mt-3 inline-flex items-center gap-1 text-[11.5px] text-muted transition hover:text-accent"
        >
          来自选题「{{ topic.title }}」 <ChevronRight :size="11" />
        </RouterLink>
      </header>

      <div class="grid gap-7 lg:grid-cols-[minmax(0,1fr)_318px]">
        <article class="surface px-8 py-7">
          <div v-if="html" class="prose-xing" v-html="html" />
          <p v-else class="py-16 text-center text-[13px] text-muted">
            这条内容还没有正文版本。
          </p>
        </article>

        <aside class="space-y-4 lg:sticky lg:top-7 lg:self-start">
          <!-- 审核结论：先给判断，再给理由 -->
          <section class="surface overflow-hidden">
            <div class="flex items-center gap-2 border-b border-line-soft px-4 py-3">
              <ShieldCheck v-if="review?.verdict === 'PASS'" :size="14" class="text-ok" />
              <ShieldX v-else-if="review" :size="14" class="text-bad" />
              <AlertTriangle v-else :size="14" class="text-muted" />
              <p class="text-[12px] font-medium">AI 审核</p>
              <span
                v-if="review"
                class="num ml-auto text-[12px] font-semibold"
                :class="review.verdict === 'PASS' ? 'text-ok' : 'text-bad'"
              >
                {{ review.verdict }} · {{ review.score }}
              </span>
            </div>

            <div class="px-4 py-3.5">
              <template v-if="review">
                <p v-if="!review.issues.length" class="flex items-start gap-1.5 text-[12px]"
                  :class="review.verdict === 'PASS' ? 'text-ok' : 'text-warn'"
                >
                  <component
                    :is="review.verdict === 'PASS' ? CheckCircle2 : AlertTriangle"
                    :size="13"
                    class="mt-0.5 shrink-0"
                  />
                  {{
                    review.verdict === 'PASS'
                      ? '没有发现事实或论证问题。'
                      : '审核判定为不通过，但没有返回具体条目 —— 请直接读正文判断。'
                  }}
                </p>
                <ul v-else class="space-y-2">
                  <li
                    v-for="(issue, i) in review.issues"
                    :key="i"
                    class="flex gap-2 text-[12px] leading-[1.65] text-ink-2"
                  >
                    <span class="num mt-[2px] shrink-0 text-[10.5px] text-[#b9b0a1]">
                      {{ String(i + 1).padStart(2, '0') }}
                    </span>
                    <span>{{ issue }}</span>
                  </li>
                </ul>
                <p class="mt-3 border-t border-line-soft pt-2.5 text-[10.5px] leading-relaxed text-muted">
                  审核只对「事实能否在证据里找到」负责，文风与取舍仍由你判断。
                </p>
              </template>
              <p v-else class="text-[12px] leading-relaxed text-muted">
                这条内容还没有审核记录。
              </p>
            </div>
          </section>

          <!-- 人工判断 -->
          <section class="surface p-4">
            <p class="eyebrow mb-2.5">人工判断</p>
            <textarea
              v-model="comment"
              rows="2"
              placeholder="可选：写一句判断理由"
              class="field resize-none !py-2 text-[12.5px]"
            />
            <div class="mt-2.5 grid grid-cols-2 gap-2">
              <button class="btn-primary btn-sm" :disabled="acting" @click="decide('approve')">
                <CheckCircle2 :size="12" /> 通过
              </button>
              <button class="btn-ghost btn-sm" :disabled="acting" @click="decide('reject')">
                <XCircle :size="12" /> 驳回
              </button>
            </div>
          </section>

          <section class="surface overflow-hidden">
            <div class="border-b border-line-soft px-4 py-3">
              <p class="text-[12px] font-medium">版本</p>
            </div>
            <ul class="divide-y divide-[#f0ece4]">
              <li
                v-for="version in versions"
                :key="version.id"
                class="flex items-baseline gap-2 px-4 py-2.5 text-[11.5px]"
              >
                <span class="num font-medium text-ink">v{{ version.version_number }}</span>
                <span class="min-w-0 flex-1 truncate text-muted">
                  {{ version.change_summary ?? '（无说明）' }}
                </span>
                <span class="shrink-0 text-[10.5px] text-[#b3ab9e]">
                  {{ formatDate(version.created_at) }}
                </span>
              </li>
              <li v-if="!versions.length" class="px-4 py-3 text-[11.5px] text-muted">暂无版本</li>
            </ul>
          </section>

          <section v-if="approvals.length" class="surface overflow-hidden">
            <div class="border-b border-line-soft px-4 py-3">
              <p class="text-[12px] font-medium">审批记录</p>
            </div>
            <ul class="divide-y divide-[#f0ece4]">
              <li v-for="item in approvals" :key="item.id" class="px-4 py-2.5 text-[11.5px]">
                <div class="flex items-center justify-between gap-2">
                  <span class="text-ink-2">{{ item.stage }}</span>
                  <StatusChip :status="item.status" />
                </div>
                <p v-if="item.comment" class="mt-1 leading-relaxed text-muted">
                  {{ item.comment }}
                </p>
                <p class="mt-0.5 text-[10.5px] text-[#b3ab9e]">
                  {{ formatDate(item.decided_at ?? item.created_at) }}
                </p>
              </li>
            </ul>
          </section>
        </aside>
      </div>
    </template>
  </div>
</template>
