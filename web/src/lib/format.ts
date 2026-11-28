export function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const diff = Date.now() - new Date(iso).getTime()
  if (Number.isNaN(diff)) return '—'
  const minutes = Math.floor(diff / 60_000)
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days} 天前`
  return formatDate(iso)
}

export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds} 秒`
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  return s ? `${m} 分 ${s} 秒` : `${m} 分`
}

/** 分数是 0~1 的浮点。统一在展示层转成 0~100 的整数，
 * 不在后端四舍五入 —— 排序用的还是原始精度。 */
export function score(v: number | null | undefined): number | null {
  if (v === null || v === undefined) return null
  return Math.round(v * 100)
}

export function taskDuration(startedAt: string | null, completedAt: string | null): string {
  if (!startedAt) return '—'
  const start = new Date(startedAt).getTime()
  const end = completedAt ? new Date(completedAt).getTime() : Date.now()
  const s = Math.max(0, Math.round((end - start) / 1000))
  return formatDuration(s)
}

/** 还没被领取的任务：它没有开始时间，只有「等了多久」。
 * 这个数字是判断「排队」还是「没人来领」的唯一依据，所以必须显示。 */
export function queueWait(createdAt: string | null | undefined): string {
  if (!createdAt) return '—'
  const s = Math.max(0, Math.round((Date.now() - new Date(createdAt).getTime()) / 1000))
  return formatDuration(s)
}

export function truncate(text: string, max: number): string {
  const flat = text.replace(/\s+/g, ' ').trim()
  return flat.length > max ? `${flat.slice(0, max)}…` : flat
}
