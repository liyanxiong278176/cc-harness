/** Versioned WebUI transport adapter.
 *
 * Commands are REST requests and the run tree is a replayable SSE stream. The
 * adapter deliberately contains no client-side Runtime state: projections and
 * event cursors remain authoritative on the Python side.
 */
export const WEB_API_BASE = '/api/web/v1'

export type ApiError = Error & { status?: number; code?: string }

// During a rolling update an already-running Python process may still serve
// only the legacy `/api` routes.  Remember that capability decision once the
// first request sees the SPA fallback, so the subsequent EventSource uses the
// matching legacy stream instead of treating an HTML 200 response as SSE.
let versionedApiAvailable = true

export async function webApi<T>(path: string, options?: RequestInit): Promise<T> {
  const normalizedPath = path.startsWith('/api/')
    ? path.slice('/api'.length)
    : (path.startsWith('/') ? path : '/' + path)
  const requestOptions: RequestInit = {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options?.headers ?? {}) },
  }
  let response = await fetch(WEB_API_BASE + normalizedPath, requestOptions)
  // A running WebUI may be serving a newly built bundle while its Python
  // process is still on the previous release. In that short rolling-update
  // window the SPA fallback returns HTML for the versioned path; use the
  // preserved legacy route instead of leaving the page unusable. Once the
  // process is restarted, all requests stay on `/api/web/v1`.
  const contentType = response.headers.get('content-type') ?? ''
  if (!contentType.includes('json') || response.status === 404 || response.status === 405) {
    versionedApiAvailable = false
    const legacyResponse = await fetch('/api' + normalizedPath, requestOptions)
    if (legacyResponse.ok || (legacyResponse.headers.get('content-type') ?? '').includes('json')) {
      response = legacyResponse
    }
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({})) as { detail?: unknown }
    const detail: { message?: string; code?: string } = typeof body.detail === 'object' && body.detail !== null
      ? (body.detail as { message?: string; code?: string })
      : { message: typeof body.detail === 'string' ? body.detail : undefined }
    const error = new Error(detail.message || `请求失败 (${response.status})`) as ApiError
    error.status = response.status
    if (detail.code) error.code = String(detail.code)
    throw error
  }
  return response.json() as Promise<T>
}

export function webEventsUrl(runId: string, after?: number, legacy = false): string {
  const query = after && after > 0 ? `?after=${encodeURIComponent(after)}` : ''
  const base = legacy || !versionedApiAvailable ? '/api' : WEB_API_BASE
  return `${base}/sessions/${encodeURIComponent(runId)}/events${query}`
}
