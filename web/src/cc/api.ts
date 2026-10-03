/** Thin transport for the existing cc-harness REST/SSE contract. */
export type StreamErrorDetail = {
  message?: string
  code?: string
  phase?: 'connection' | 'model' | 'tool' | 'approval' | 'persistence' | 'sse'
  retryable?: boolean
  partial_output?: boolean
  attempt?: number | null
  retry_after?: number | null
  next_action?: string
  request_id?: string
}

export type ApiError = Error & StreamErrorDetail & { status?: number }

export async function webApi<T>(path: string, options?: RequestInit): Promise<T> {
  const url = path.startsWith('/api/') ? path : `/api${path.startsWith('/') ? path : `/${path}`}`
  const response = await fetch(url, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options?.headers ?? {}) },
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as { detail?: unknown }
    const detail: StreamErrorDetail = typeof body.detail === 'object' && body.detail !== null
      ? body.detail as StreamErrorDetail
      : { message: typeof body.detail === 'string' ? body.detail : undefined }
    const error = new Error(detail.message || `请求失败 (${response.status})`) as ApiError
    error.status = response.status
    if (detail.code) error.code = String(detail.code)
    if (detail.phase) error.phase = detail.phase
    if (typeof detail.retryable === 'boolean') error.retryable = detail.retryable
    if (typeof detail.partial_output === 'boolean') error.partial_output = detail.partial_output
    if (detail.attempt !== undefined) error.attempt = detail.attempt
    if (detail.retry_after !== undefined) error.retry_after = detail.retry_after
    if (detail.next_action) error.next_action = String(detail.next_action)
    if (detail.request_id) error.request_id = String(detail.request_id)
    throw error
  }
  return response.json() as Promise<T>
}

export function webEventsUrl(runId: string, after?: number, _legacy = false): string {
  const query = after && after > 0 ? `?after=${encodeURIComponent(after)}` : ''
  return `/api/sessions/${encodeURIComponent(runId)}/events${query}`
}
