/** Thin HTTP/SSE transport for the cc-harness control plane. */

export type ApiError = Error & {
  status?: number
  code?: string
  phase?: string
  retryable?: boolean
  next_action?: string
}

export async function webApi<T>(path: string, options?: RequestInit): Promise<T> {
  const url = path.startsWith('/api/') ? path : `/api${path.startsWith('/') ? path : `/${path}`}`
  const response = await fetch(url, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options?.headers ?? {}) },
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as { detail?: unknown }
    const detail = typeof body.detail === 'object' && body.detail !== null
      ? body.detail as Record<string, unknown>
      : { message: typeof body.detail === 'string' ? body.detail : undefined }
    const message = typeof detail.message === 'string' && detail.message
      ? detail.message
      : `请求失败 (${response.status})`
    const error = new Error(message) as ApiError
    error.status = response.status
    if (typeof detail.code === 'string') error.code = detail.code
    if (typeof detail.phase === 'string') error.phase = detail.phase
    if (typeof detail.retryable === 'boolean') error.retryable = detail.retryable
    if (typeof detail.next_action === 'string') error.next_action = detail.next_action
    throw error
  }
  return response.json() as Promise<T>
}

export function describeError(reason: unknown): string {
  if (reason instanceof Error) {
    const api = reason as ApiError
    if (api.status === 409 && api.code === 'project_required') return '请先选择工作区'
    return api.message || '请求失败'
  }
  return String(reason ?? '请求失败')
}

export function sessionLabel(status: string | undefined): string {
  switch (status) {
    case 'queued':
    case 'waiting_on_predecessor': return '排队中'
    case 'running': return '运行中'
    case 'awaiting_approval': return '等待审批'
    case 'paused': return '已暂停'
    case 'blocked': return '已阻塞'
    case 'stalled': return '已停滞'
    case 'failed_recoverable':
    case 'failed': return '失败'
    case 'cancelled': return '已停止'
    case 'completed': return '已完成'
    default: return status || '未知'
  }
}

export function permissionLabel(mode: string | undefined): string {
  if (mode === 'bypass-prompts') return '完全访问权限'
  if (mode === 'auto-edit') return '自动批准编辑'
  return '请求批准'
}

export function projectName(root: string | undefined): string {
  if (!root) return '未分组'
  const parts = root.replace(/[\\/]+$/, '').split(/[\\/]/)
  return parts[parts.length - 1] || root
}

export function formatRelative(value: string | null | undefined): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const now = Date.now()
  const diff = now - date.getTime()
  const minutes = Math.floor(diff / 60_000)
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟`
  const hours = Math.floor(minutes / 60)
  if (hours < 24 && new Date().getDate() === date.getDate()) return `${hours} 小时`
  const days = Math.floor(diff / 86_400_000)
  if (days <= 1) return '昨天'
  if (days < 30) return `${days} 天`
  if (days < 365) return `${Math.floor(days / 30)} 个月`
  return `${Math.floor(days / 365)} 年`
}

export function timeLabel(value: string | null | undefined): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}
