/** HTTP 客户端。
 *
 * 后端错误是 RFC 9457 problem+json（``{code, detail, status, details}``），
 * 这里统一转成 ApiError，界面只处理 ``err.code`` 与 ``err.message``，
 * 不再各自判断 response.ok。
 */

const BASE = import.meta.env.VITE_API_BASE ?? '/api/v1'

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly details: unknown

  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details
  }
}

type Body = unknown

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: {
        Accept: 'application/json',
        ...(init.body ? { 'Content-Type': 'application/json' } : {}),
        ...init.headers,
      },
    })
  } catch (cause) {
    // 网络层失败（后端没起、代理断开）与业务错误必须能区分：
    // 前者提示「检查后端」，后者提示用户改输入。
    throw new ApiError(0, 'NETWORK_ERROR', '无法连接后端，请确认 API 已在 8000 端口运行', cause)
  }

  if (response.status === 204) return undefined as T

  const text = await response.text()
  let payload: unknown = null
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = text
    }
  }

  if (!response.ok) {
    const problem = (payload ?? {}) as {
      code?: string
      detail?: string
      title?: string
      details?: unknown
    }
    throw new ApiError(
      response.status,
      problem.code ?? `HTTP_${response.status}`,
      problem.detail ?? problem.title ?? `请求失败（${response.status}）`,
      problem.details,
    )
  }

  return payload as T
}

export const http = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: Body) =>
    request<T>(path, { method: 'POST', body: body ? JSON.stringify(body) : undefined }),
  patch: <T>(path: string, body?: Body) =>
    request<T>(path, { method: 'PATCH', body: body ? JSON.stringify(body) : undefined }),
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return '未知错误'
}
