/** 战略配置的编辑约定。
 *
 * `strategy_config` 是自由 JSONB —— 后端不校验字段名，也不做 schema 迁移。
 * 它只有两个真实消费方：
 *   1. `expander` / `scorer` 把整份配置 `json.dumps(..., ensure_ascii=False)`
 *      后截断 1500 字符塞进 prompt；
 *   2. `topic_service` 把里面所有字符串当关键词，算事件相关度。
 *
 * 所以字段名只有「人读」的意义，但**长度**有硬后果：超出 1500 字符的部分
 * 模型根本看不到，而且截断点落在 JSON 中间，模型拿到的是半截对象。
 * 编辑器必须把这个预算显式画出来，否则用户改的是后半段、生效的是前半段。
 */

/** 与 `agents/expander.py`、`agents/scorer.py` 里的 `[:1500]` 保持一致。 */
export const PROMPT_BUDGET = 1500

export interface StrategyFieldSpec {
  key: string
  label: string
  hint: string
  /** 新增字段时的默认形状，只用于占位提示，不写入配置。 */
  sample?: string
}

/** 建议顺序只影响界面呈现。
 *
 * 注意：写入 prompt 的键顺序由 PostgreSQL 的 jsonb 决定（不保留插入顺序），
 * 所以「把重要字段放前面」在这里是做不到的 —— 能控的只有总长度。 */
export const STRATEGY_FIELDS: StrategyFieldSpec[] = [
  {
    key: 'positioning',
    label: '定位',
    hint: '一句话说清「写给谁、解决什么判断」',
    sample: '关注 AI 与组织变革，写给需要做判断的人',
  },
  {
    key: 'target_audience',
    label: '目标读者',
    hint: '每行一个；越具体，候选选题越不容易发散',
    sample: '企业中高层\n技术决策者',
  },
  { key: 'content_directions', label: '内容方向', hint: '每行一个', sample: 'AI 落地的组织阻力' },
  { key: 'platforms', label: '发布平台', hint: '每行一个', sample: '公众号' },
  { key: 'tone', label: '语气', hint: '写「要什么」，也写「不要什么」', sample: '冷静、具体、反常识' },
  { key: 'boundaries', label: '边界', hint: '每行一条；明确不写什么', sample: '不写行业通稿' },
  { key: 'goals', label: '目标', hint: '键值对象，例如每月篇数', sample: '{"monthly_publish": 8}' },
]

/** 一键启用时写入的最小可用配置。刻意只放三条有内容的字段：
 *  空模板也会被序列化进 prompt，那是在拿预算换占位符。 */
export const DEFAULT_STRATEGY: Record<string, unknown> = {
  positioning: '关注 AI 与组织变革，写给需要做判断的人',
  target_audience: ['企业中高层、技术决策者与一线管理者'],
  tone: '冷静、具体、反常识；不写行业通稿，不堆形容词',
}

/** 早期由「一键启用」写进去的字段名。配置没有 schema，所以库里两种都存在；
 *  编辑器不自动迁移（那是替用户改内容），只提示一下，避免同一件事写两遍占预算。 */
export const LEGACY_KEYS: Record<string, string> = {
  audience: 'target_audience',
}

/** 前端能结构化编辑的三种形状。其余（数字、布尔、混合数组）统一走 JSON。 */
export type FieldKind = 'text' | 'lines' | 'json'

export interface StrategyRow {
  key: string
  kind: FieldKind
  text: string
}

export function kindOf(value: unknown): FieldKind {
  if (typeof value === 'string') return 'text'
  if (Array.isArray(value) && value.every((item) => typeof item === 'string')) return 'lines'
  return 'json'
}

/** 配置 -> 可编辑行。字符串用多行文本，字符串数组按行拆，其余原样 JSON。 */
export function toRows(config: Record<string, unknown> | null | undefined): StrategyRow[] {
  if (!config) return []
  return Object.entries(config).map(([key, value]) => {
    const kind = kindOf(value)
    if (kind === 'lines') {
      return { key, kind, text: (value as string[]).join('\n') }
    }
    if (kind === 'json') {
      return { key, kind, text: JSON.stringify(value, null, 2) }
    }
    return { key, kind, text: String(value) }
  })
}

/** 单行 -> JSON 值。非法 JSON 抛错，错误信息带字段名。 */
export function rowValue(row: StrategyRow): unknown {
  if (row.kind === 'text') return row.text.trim()
  if (row.kind === 'lines') {
    return row.text
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean)
  }
  try {
    return JSON.parse(row.text)
  } catch {
    throw new Error(`字段「${row.key || '未命名'}」不是合法 JSON`)
  }
}

/** 可编辑行 -> 配置。非法 JSON 时抛出的错误带字段名，界面能指到具体那一行。 */
export function fromRows(rows: StrategyRow[]): Record<string, unknown> {
  const config: Record<string, unknown> = {}
  for (const row of rows) {
    const key = row.key.trim()
    if (!key) continue
    config[key] = rowValue(row)
  }
  return config
}

function pyJson(value: unknown): string {
  if (typeof value === 'string') return JSON.stringify(value)
  if (Array.isArray(value)) return `[${value.map(pyJson).join(', ')}]`
  if (value && typeof value === 'object') {
    const body = Object.entries(value as Record<string, unknown>)
      .map(([key, item]) => `${JSON.stringify(key)}: ${pyJson(item)}`)
      .join(', ')
    return `{${body}}`
  }
  return JSON.stringify(value) ?? 'null'
}

/** 近似 `len(json.dumps(config, ensure_ascii=False))`。
 *
 * 不追求字节级一致，只要求「和后端截断的口径同量级」—— 用于把预算画出来，
 * 而不是用于断言。差别只可能出现在浮点数格式上。 */
export function configChars(config: Record<string, unknown>): number {
  return pyJson(config).length
}

/** 某一行在整份 JSON 里占多少字符（含前置的 `, `）。 */
export function entryChars(key: string, value: unknown): number {
  return `${JSON.stringify(key)}: ${pyJson(value)}`.length + 2
}
