import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { X } from 'lucide-react'
import type {
  Approval, Bootstrap, ChatSnapshot, LiveChunk, LiveSegment, Projection,
  RuntimeEvent, Session, Settings, TaskView,
} from './types'
import { describeError, formatRelative, permissionLabel, projectName, sessionLabel, webApi } from './api'
import {
  applyApprovalEvent, applyPlanEvent, applyTodoEvent, deriveTasks,
  mergeEvents, mergeTodoLists, projectionApprovals, projectionPlan, projectionQueue,
  projectionTodos, statusFromEvent,
} from './events'
import { applyLiveChunk, clearLiveSegments, pruneLiveSegments } from './live'
import { Sidebar } from './Sidebar'
import { Header } from './Header'
import { ChatPane, TraceView } from './Chat'
import { SettingsDialog } from './SettingsDialog'
import { TaskPanel } from './TaskPanel'

type PendingUser = {
  id: string
  content: string
  createdAt: number
  failed?: boolean
}

type Theme = 'dark' | 'light'
type Connection = 'connecting' | 'open' | 'reconnecting' | 'offline'

const ACTIVE_KEY = 'cc-harness.activeSession'
const PROJECT_KEY = 'cc-harness.projectRoot'
const THEME_KEY = 'cc-harness.theme'
const MODE_KEY = 'cc-harness.mode'
const SIDEBAR_KEY = 'cc-harness.sidebar.collapsed'

function readString(key: string): string | null {
  try { return localStorage.getItem(key) } catch { return null }
}

function writeString(key: string, value: string | null) {
  try {
    if (value === null) localStorage.removeItem(key)
    else localStorage.setItem(key, value)
  } catch { /* storage may be unavailable */ }
}

function readChat(id: string): ChatSnapshot | null {
  try {
    const raw = sessionStorage.getItem(`cc-harness.chat.${id}`)
    if (!raw) return null
    const value = JSON.parse(raw) as ChatSnapshot
    return value && Array.isArray(value.events) ? value : null
  } catch {
    return null
  }
}

function writeChat(snapshot: ChatSnapshot) {
  try {
    const slim: ChatSnapshot = {
      ...snapshot,
      events: snapshot.events.slice(-260),
    }
    sessionStorage.setItem(`cc-harness.chat.${snapshot.run_id}`, JSON.stringify(slim))
  } catch { /* quota is best-effort */ }
}

function initialTheme(): Theme {
  return readString(THEME_KEY) === 'light' ? 'light' : 'dark'
}

function initialActive(): string | null {
  try { return sessionStorage.getItem(ACTIVE_KEY) } catch { return null }
}

export function App() {
  const storedChat = useMemo(() => {
    const id = initialActive()
    return id ? readChat(id) : null
  }, [])
  const [theme, setTheme] = useState<Theme>(initialTheme)
  const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(() => {
    const stored = readString(SIDEBAR_KEY)
    if (stored !== null) return stored === '1'
    return window.innerWidth < 1100
  })
  const [settings, setSettings] = useState<Settings | null>(null)
  const [projectRoot, setProjectRoot] = useState<string | null>(() => readString(PROJECT_KEY))
  const [sessions, setSessions] = useState<Session[]>([])
  const [sessionsLoading, setSessionsLoading] = useState(true)
  const [activeId, setActiveId] = useState<string | null>(() => initialActive())
  const [events, setEvents] = useState<RuntimeEvent[]>(storedChat?.events ?? [])
  const [projection, setProjection] = useState<Projection>(storedChat?.projection ?? {})
  const [taskProjection, setTaskProjection] = useState<Projection>(
    storedChat?.task_projection ?? {},
  )
  const [status, setStatus] = useState(storedChat?.status ?? 'paused')
  const [liveSegments, setLiveSegments] = useState<LiveSegment[]>([])
  const [pendingUsers, setPendingUsers] = useState<PendingUser[]>([])
  const [chatLoading, setChatLoading] = useState(Boolean(initialActive()) && !storedChat)
  const [connection, setConnection] = useState<Connection>('connecting')
  const [draft, setDraft] = useState('')
  const [mode, setMode] = useState<'coding' | 'plan'>(() => readString(MODE_KEY) === 'plan' ? 'plan' : 'coding')
  const [sending, setSending] = useState(false)
  const [tab, setTab] = useState<'chat' | 'trace'>('chat')
  const [query, setQuery] = useState('')
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [tasksOpen, setTasksOpen] = useState(false)
  const [toast, setToast] = useState<string | null>(null)
  const [traceEvents, setTraceEvents] = useState<RuntimeEvent[]>([])
  const [traceLoading, setTraceLoading] = useState(false)

  const activeIdRef = useRef(activeId)
  activeIdRef.current = activeId
  const sessionsRef = useRef<Session[]>([])
  sessionsRef.current = sessions
  const cursorsRef = useRef<Record<string, number>>({})
  const taskRunIdRef = useRef(storedChat?.task_run_id ?? '')
  const chatCacheRef = useRef<Map<string, ChatSnapshot>>(new Map())
  const streamRef = useRef<EventSource | null>(null)
  const reconnectRef = useRef<number | undefined>(undefined)
  const streamGeneration = useRef(0)
  const refreshTimer = useRef<number | undefined>(undefined)
  const startedRef = useRef(false)
  const traceForRef = useRef<string | null>(null)
  const finalizedLiveRuns = useRef<Set<string>>(new Set())
  const eagerRefreshRef = useRef(false)

  const activeSession = sessions.find(session => session.run_id === activeId)
  const title = activeSession?.title || '新对话'
  const running = ['running', 'queued', 'waiting_on_predecessor', 'cancel_requested'].includes(status)
  const backgroundCount = useMemo(() => {
    const activeStatuses = new Set(['running', 'queued', 'awaiting_approval', 'waiting_on_predecessor', 'cancel_requested'])
    const count = sessions.filter(session => activeStatuses.has(session.status)).length
    return count > 0 ? count : (running ? 1 : 0)
  }, [sessions, running])
  const tasks = useMemo(() => {
    // ``projection`` is the root conversation projection (its todos span the
    // whole Run tree).  The task dock must mirror the Runtime task-control
    // layer only; never fall back to the aggregated tree checklist.
    return deriveTasks(
      projectionTodos(taskProjection),
      projectionPlan(taskProjection),
      status,
      taskProjection.run_id || projection.effective_run_id,
    )
  }, [projection, taskProjection, status])
  const approvals = useMemo(
    () => (status === 'awaiting_approval' || projectionApprovals(projection).length > 0)
      ? projectionApprovals(projection)
      : [],
    [projection, status],
  )

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    writeString(THEME_KEY, theme)
  }, [theme])

  useEffect(() => {
    writeString(MODE_KEY, mode)
  }, [mode])

  useEffect(() => {
    writeString(SIDEBAR_KEY, sidebarCollapsed ? '1' : '0')
  }, [sidebarCollapsed])

  useEffect(() => {
    if (!toast) return undefined
    const timer = window.setTimeout(() => setToast(null), 4200)
    return () => window.clearTimeout(timer)
  }, [toast])

  const refreshSessions = useCallback(async () => {
    try {
      const list = await webApi<{ sessions: Session[] }>('/api/sessions')
      setSessions(list.sessions)
      if (!projectRoot && !activeIdRef.current) {
        const preferred = [...list.sessions].sort(byUpdatedDesc)[0]?.project_root
        if (preferred) {
          setProjectRoot(preferred)
          writeString(PROJECT_KEY, preferred)
        }
      }
      return list.sessions
    } catch {
      return []
    }
  }, [projectRoot])

  const adoptTaskSnapshot = useCallback((snapshot: ChatSnapshot) => {
    // ``task_projection`` is the Runtime task-control layer.  Older cached
    // payloads did not carry it; clear the dock rather than showing the
    // aggregated conversation tree until the fresh snapshot arrives.
    if (!snapshot.task_projection) {
      taskRunIdRef.current = ''
      setTaskProjection({})
      return
    }
    taskRunIdRef.current = snapshot.task_run_id
      || snapshot.task_projection.run_id
      || ''
    setTaskProjection(snapshot.task_projection)
  }, [])

  const refreshChat = useCallback(async (id: string) => {
    try {
      const snapshot = await webApi<ChatSnapshot>(`/api/sessions/${encodeURIComponent(id)}/chat?limit=1200`)
      if (activeIdRef.current !== id) return
      chatCacheRef.current.set(id, snapshot)
      writeChat(snapshot)
      setStatus(snapshot.status)
      setChatLoading(false)
      setProjectRoot(snapshot.project_root)
      writeString(PROJECT_KEY, snapshot.project_root)
      if (snapshot.cursors) cursorsRef.current = { ...snapshot.cursors }
      setEvents(previous => mergeEvents(previous, snapshot.events))
      setProjection(previous => ({
        ...snapshot.projection,
        todos: mergeTodoLists(projectionTodos(snapshot.projection), projectionTodos(previous)),
      }))
      adoptTaskSnapshot(snapshot)
      setSessions(previous => previous.map(session => session.run_id === id
        ? { ...session, status: snapshot.status, updated_at: new Date().toISOString() }
        : session))
    } catch {
      // A background refresh is best-effort; the visible snapshot stays.
    }
  }, [adoptTaskSnapshot])

  const scheduleRefresh = useCallback((id: string, delay = 650) => {
    window.clearTimeout(refreshTimer.current)
    refreshTimer.current = window.setTimeout(() => { void refreshChat(id) }, delay)
  }, [refreshChat])

  const closeStream = useCallback(() => {
    streamGeneration.current += 1
    streamRef.current?.close()
    streamRef.current = null
    window.clearTimeout(reconnectRef.current)
    reconnectRef.current = undefined
  }, [])

  const openStream = useCallback((id: string) => {
    closeStream()
    const generation = streamGeneration.current
    setConnection('connecting')
    const cursors = { ...cursorsRef.current }
    const rootAfter = cursors[id] ?? 0
    const url = `/api/sessions/${encodeURIComponent(id)}/events?after=${rootAfter}`
      + `&cursors=${encodeURIComponent(JSON.stringify(cursors))}`
    const source = new EventSource(url)
    streamRef.current = source

    source.onopen = () => {
      if (streamGeneration.current !== generation) return
      setConnection('open')
    }
    source.addEventListener('runtime', event => {
      if (streamGeneration.current !== generation) return
      try {
        const payload = JSON.parse((event as MessageEvent).data) as RuntimeEvent
        handleRuntimeEvent(id, payload, generation)
      } catch {
        // Malformed frame is ignored; the durable refresh remains the fallback.
      }
    })
    source.addEventListener('stream', event => {
      if (streamGeneration.current !== generation) return
      try {
        const chunk = JSON.parse((event as MessageEvent).data) as LiveChunk
        if (chunk.type === 'stream_error') {
          const message = chunk.error?.message
          if (message) setToast(message)
          return
        }
        // Durable assistant commits can overtake the last coalesced live
        // chunks.  Once a run's text is durable, late deltas would duplicate
        // it (and could leak the internal completion envelope), so they are
        // discarded until the next tool observation opens a new segment.
        if (finalizedLiveRuns.current.has(chunk.run_id)) return
        setLiveSegments(previous => pruneLiveSegments(applyLiveChunk(previous, chunk)))
        if (chunk.kind === 'done') scheduleRefresh(id, 320)
      } catch {
        // Ignore malformed ephemeral chunks.
      }
    })
    source.addEventListener('stream_error', event => {
      if (streamGeneration.current !== generation) return
      try {
        const payload = JSON.parse((event as MessageEvent).data) as { message?: string }
        if (payload.message) setToast(payload.message)
      } catch { /* noop */ }
    })
    source.addEventListener('heartbeat', () => {
      if (streamGeneration.current === generation) setConnection('open')
    })
    source.onerror = () => {
      if (streamGeneration.current !== generation || streamRef.current !== source) return
      source.close()
      streamRef.current = null
      setConnection('reconnecting')
      reconnectRef.current = window.setTimeout(() => {
        if (activeIdRef.current === id) openStreamRef.current(id)
      }, 900)
    }
  }, [closeStream, scheduleRefresh])

  const openStreamRef = useRef(openStream)
  openStreamRef.current = openStream

  const acknowledgePending = useCallback((incoming: RuntimeEvent[]) => {
    if (incoming.length === 0) return
    const userTexts = incoming
      .filter(event => event.kind === 'user' && event.content)
      .map(event => event.content!.trim())
    if (userTexts.length === 0) return
    setPendingUsers(previous => previous.filter(item => !userTexts.includes(item.content.trim())))
  }, [])

  const handleRuntimeEvent = useCallback((id: string, event: RuntimeEvent, generation: number) => {
    if (activeIdRef.current !== id || streamGeneration.current !== generation) return
    cursorsRef.current[event.run_id] = Math.max(cursorsRef.current[event.run_id] ?? 0, event.sequence)
    setEvents(previous => {
      const next = mergeEvents(previous, [event])
      return next.length > 2400 ? next.slice(-2000) : next
    })
    setStatus(previous => statusFromEvent(previous, event))
    setSessions(previous => previous.map(session => session.run_id === id
      ? { ...session, status: statusFromEvent(session.status, event), updated_at: event.occurred_at }
      : session))
    // The task dock follows the Runtime task-control projection, not the
    // root conversation projection (whose todos span every Run in the tree).
    // A new root Run starts a fresh checklist; a child/follow-up becomes
    // effective asynchronously, so its authoritative todo list is adopted by
    // the scheduled chat snapshot instead of guessed from the event stream.
    if (
      event.event_type === 'RunCreated'
      && event.run_id === activeIdRef.current
      && event.run_id !== taskRunIdRef.current
    ) {
      taskRunIdRef.current = event.run_id
      setTaskProjection({ run_id: event.run_id, status: 'queued', todos: [], plan: undefined })
    }
    if (event.event_type.startsWith('Todo') || event.event_type.startsWith('Plan')) {
      if (!taskRunIdRef.current) taskRunIdRef.current = event.run_id
      if (event.run_id === taskRunIdRef.current) {
        setTaskProjection(previous => {
          if (event.event_type.startsWith('Todo')) {
            return {
              ...previous,
              run_id: previous.run_id ?? event.run_id,
              todos: applyTodoEvent(projectionTodos(previous), event),
            }
          }
          const plan = applyPlanEvent(previous.plan, event)
          return plan === previous.plan
            ? previous
            : { ...previous, run_id: previous.run_id ?? event.run_id, plan }
        })
      }
    }
    const relevant = ['TodoCreated', 'TodoUpdated', 'TodoCompleted', 'PlanCreated', 'PlanRevised', 'PlanNodeStarted', 'PlanNodeCompleted', 'ApprovalRequested', 'ApprovalGranted', 'ApprovalRejected', 'FollowUpQueued', 'RunCreated', 'RunOutcomeRecorded', 'RunFailed', 'RunBlocked', 'RunCancelled']
    setProjection(previous => {
      let next: Projection | null = null
      if (event.event_type.startsWith('Todo')) {
        const todos = applyTodoEvent(projectionTodos(previous), event)
        if (todos !== previous.todos) next = { ...previous, todos }
      } else if (event.event_type.startsWith('Plan')) {
        const plan = applyPlanEvent(previous.plan, event)
        if (plan !== previous.plan) next = { ...previous, plan }
      } else if (event.event_type.startsWith('Approval')) {
        const list = applyApprovalEvent(projectionApprovals(previous), event)
        if (list !== previous.approvals) next = { ...previous, approvals: list }
      }
      return next ?? previous
    })
    if (event.event_type === 'AssistantMessageCommitted') {
      finalizedLiveRuns.current.add(event.run_id)
      setLiveSegments(previous => clearLiveSegments(previous, event.run_id))
    } else if (
      event.event_type === 'ToolObservationCommitted'
      || event.event_type === 'FollowUpQueued'
      || event.event_type === 'RunCreated'
      || event.event_type === 'TaskGuidanceReceived'
      || event.event_type === 'RunResumed'
    ) {
      // The next model segment is starting (or a new user turn arrived);
      // allow its live deltas through.
      finalizedLiveRuns.current.delete(event.run_id)
    }
    acknowledgePending([event])
    if (relevant.includes(event.event_type)) {
      scheduleRefresh(id, event.event_type.startsWith('Plan') ? 900 : 550)
      if (event.event_type === 'RunOutcomeRecorded' || event.event_type === 'RunFailed') {
        void refreshSessions()
      }
    }
  }, [acknowledgePending, refreshSessions, scheduleRefresh])

  // Initial shell: bootstrap is fast and intentionally excludes the session
  // scan; the sidebar hydrates independently and never blocks the composer.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const boot = await webApi<Bootstrap>('/api/bootstrap?include_sessions=false')
        if (cancelled) return
        setSettings(boot.settings)
        if (!projectRoot && boot.project?.root) {
          setProjectRoot(boot.project.root)
          writeString(PROJECT_KEY, boot.project.root)
        }
        if (boot.initial_prompt && !draft) setDraft(boot.initial_prompt)
      } catch (reason) {
        if (!cancelled) setToast(describeError(reason))
      } finally {
        if (!cancelled) setSessionsLoading(true)
      }
      const list = await refreshSessions()
      if (cancelled) return
      setSessionsLoading(false)
      if (!activeIdRef.current && !startedRef.current) {
        startedRef.current = true
        const ordered = [...list].sort(byUpdatedDesc)
        // A chosen workspace with no history must land on the hero, not jump
        // into an unrelated project's most recent session.
        const latest = projectRoot
          ? ordered.find(session => session.project_root === projectRoot)
          : ordered[0]
        if (latest) {
          setProjectRoot(latest.project_root ?? projectRoot)
          setActiveId(latest.run_id)
        }
      }
    })()
    return () => { cancelled = true }
    // The shell boot runs once; active session changes own their own effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Load one conversation: cached state paints immediately, then the bounded
  // chat endpoint reconciles without blocking the rest of the shell.
  useEffect(() => {
    if (!activeId) {
      closeStream()
      setEvents([])
      setProjection({})
      setTaskProjection({})
      taskRunIdRef.current = ''
      setStatus('paused')
      setLiveSegments([])
      setPendingUsers([])
      setChatLoading(false)
      setConnection('connecting')
      setTraceEvents([])
      traceForRef.current = null
      return undefined
    }
    writeString(ACTIVE_KEY, activeId)
    try { sessionStorage.setItem(ACTIVE_KEY, activeId) } catch { /* noop */ }
    closeStream()
    setTab('chat')
    setTraceEvents([])
    traceForRef.current = null
    setLiveSegments([])
    setPendingUsers([])
    finalizedLiveRuns.current.clear()
    const cached = chatCacheRef.current.get(activeId) ?? readChat(activeId)
    if (cached) {
      setEvents(cached.events)
      setProjection(cached.projection)
      adoptTaskSnapshot(cached)
      setStatus(cached.status)
      setProjectRoot(cached.project_root)
      cursorsRef.current = cached.cursors ?? cursorsFrom(cached.events)
      setChatLoading(false)
    } else {
      setEvents([])
      setProjection({})
      setTaskProjection({})
      taskRunIdRef.current = ''
      setStatus('paused')
      cursorsRef.current = {}
      setChatLoading(true)
    }
    let cancelled = false
    const request = webApi<ChatSnapshot>(`/api/sessions/${encodeURIComponent(activeId)}/chat?limit=1200`)
    request.then(snapshot => {
      if (cancelled || activeIdRef.current !== activeId) return
      chatCacheRef.current.set(activeId, snapshot)
      writeChat(snapshot)
      cursorsRef.current = snapshot.cursors ?? cursorsFrom(snapshot.events)
      setEvents(snapshot.events)
      setProjection(snapshot.projection)
      adoptTaskSnapshot(snapshot)
      setStatus(snapshot.status)
      setProjectRoot(snapshot.project_root)
      writeString(PROJECT_KEY, snapshot.project_root)
      setChatLoading(false)
      setSessions(previous => {
        const exists = previous.some(session => session.run_id === activeId)
        if (exists) return previous.map(session => session.run_id === activeId
          ? { ...session, status: snapshot.status } : session)
        return previous
      })
      setPendingUsers(previous => previous.filter(item => !snapshot.events.some(event =>
        event.kind === 'user' && event.content?.trim() === item.content.trim())))
      openStreamRef.current(activeId)
    }).catch(reason => {
      if (cancelled) return
      setChatLoading(false)
      setToast(describeError(reason))
      if (!cached) {
        const latest = sessionsRef.current[0]
        if (latest && latest.run_id !== activeId) setActiveId(latest.run_id)
        else setActiveId(null)
      }
    })
    return () => {
      cancelled = true
      closeStream()
    }
    // activeId is the only trigger; callbacks are refs or stable setters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId])

  useEffect(() => () => {
    closeStream()
    window.clearTimeout(refreshTimer.current)
  }, [closeStream])

  useEffect(() => {
    if (tab !== 'trace' || !activeId) return
    if (traceForRef.current === activeId) return
    let cancelled = false
    setTraceLoading(true)
    webApi<{ events: RuntimeEvent[] }>(`/api/sessions/${encodeURIComponent(activeId)}/timeline?limit=3000`)
      .then(response => {
        if (cancelled) return
        setTraceEvents(response.events)
        traceForRef.current = activeId
      })
      .catch(reason => {
        if (!cancelled) setToast(describeError(reason))
      })
      .finally(() => {
        if (!cancelled) setTraceLoading(false)
      })
    return () => { cancelled = true }
  }, [tab, activeId])

  useEffect(() => {
    if (!settingsOpen) return
    let cancelled = false
    webApi<Settings>('/api/settings')
      .then(value => { if (!cancelled) setSettings(value) })
      .catch(() => { /* the bootstrap value remains usable */ })
    return () => { cancelled = true }
  }, [settingsOpen])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        document.getElementById('session-search')?.focus()
      }
      if (event.key === 'Escape') {
        setSettingsOpen(false)
        setTasksOpen(false)
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])

  const send = useCallback(async () => {
    const text = draft.trim()
    if (!text || sending) return
    if (!activeId && !projectRoot) {
      setSettingsOpen(true)
      return
    }
    const pending: PendingUser = { id: `pending-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`, content: text, createdAt: Date.now() }
    setPendingUsers(previous => [...previous, pending])
    setDraft('')
    setSending(true)
    try {
      if (activeId) {
        await webApi<Record<string, unknown>>(`/api/sessions/${encodeURIComponent(activeId)}/messages`, {
          method: 'POST',
          body: JSON.stringify({ text, session_id: activeId, interaction_mode: 'auto', plan_mode: mode === 'plan' }),
        })
        chatCacheRef.current.delete(activeId)
        setStatus('queued')
        scheduleRefresh(activeId, 500)
      } else {
        const response = await webApi<{ session_id?: string; run_id?: string }>('/api/sessions', {
          method: 'POST',
          body: JSON.stringify({ text, project_root: projectRoot, interaction_mode: 'auto', plan_mode: mode === 'plan' }),
        })
        const created = response.session_id || response.run_id
        if (created) {
          startedRef.current = true
          taskRunIdRef.current = ''
          setTaskProjection({})
          setActiveId(created)
          setStatus('queued')
        }
      }
      window.setTimeout(() => { void refreshSessions() }, 800)
    } catch (reason) {
      setPendingUsers(previous => previous.map(item => item.id === pending.id ? { ...item, failed: true } : item))
      setToast(describeError(reason))
    } finally {
      setSending(false)
    }
  }, [activeId, draft, mode, projectRoot, refreshSessions, scheduleRefresh, sending])

  const stop = useCallback(async () => {
    if (!activeId) return
    try {
      await webApi(`/api/sessions/${encodeURIComponent(activeId)}/stop`, { method: 'POST' })
      setStatus('cancel_requested')
    } catch (reason) {
      setToast(describeError(reason))
    }
  }, [activeId])

  const approve = useCallback(async (approval: Approval) => {
    if (!activeId) return
    try {
      await webApi(`/api/sessions/${encodeURIComponent(activeId)}/approvals/${encodeURIComponent(approval.approval_id)}/approve`, {
        method: 'POST',
        body: JSON.stringify({ action_args_digest: approval.action_args_digest ?? '' }),
      })
      setProjection(previous => ({ ...previous, approvals: (previous.approvals ?? []).filter(item => item.approval_id !== approval.approval_id) }))
      scheduleRefresh(activeId, 250)
    } catch (reason) {
      setToast(describeError(reason))
    }
  }, [activeId, scheduleRefresh])

  const reject = useCallback(async (approval: Approval) => {
    if (!activeId) return
    try {
      await webApi(`/api/sessions/${encodeURIComponent(activeId)}/approvals/${encodeURIComponent(approval.approval_id)}/reject`, {
        method: 'POST',
        body: JSON.stringify({ reason: 'WebUI 用户拒绝' }),
      })
      setProjection(previous => ({ ...previous, approvals: (previous.approvals ?? []).filter(item => item.approval_id !== approval.approval_id) }))
      scheduleRefresh(activeId, 250)
    } catch (reason) {
      setToast(describeError(reason))
    }
  }, [activeId, scheduleRefresh])

  const pickProject = useCallback(async () => {
    try {
      const response = await webApi<{ selected: boolean; project?: { root: string }; settings?: Settings; sessions?: Session[] }>('/api/projects/select', {
        method: 'POST',
        body: JSON.stringify({ picker: true }),
      })
      if (!response.selected) return
      if (response.settings) setSettings(response.settings)
      if (response.project?.root) {
        setProjectRoot(response.project.root)
        writeString(PROJECT_KEY, response.project.root)
      }
      if (response.sessions) setSessions(response.sessions)
      startedRef.current = true
      setActiveId(null)
    } catch (reason) {
      setToast(describeError(reason))
    }
  }, [])

  const newChat = useCallback(() => {
    startedRef.current = true
    closeStream()
    setActiveId(null)
    try { sessionStorage.removeItem(ACTIVE_KEY) } catch { /* noop */ }
    setTab('chat')
    setDraft('')
    setPendingUsers([])
    setLiveSegments([])
    finalizedLiveRuns.current.clear()
  }, [closeStream])

  const deleteSession = useCallback(async (id: string) => {
    if (id === activeId && running) {
      setToast('运行中的会话不能直接删除，请先停止')
      return
    }
    if (!window.confirm('删除这个会话？')) return
    try {
      await webApi(`/api/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' })
      setSessions(previous => previous.filter(session => session.run_id !== id))
      if (activeIdRef.current === id) newChat()
    } catch (reason) {
      setToast(describeError(reason))
    }
  }, [activeId, newChat, running])

  const openSettings = useCallback(() => setSettingsOpen(true), [])
  const toggleTheme = useCallback(() => setTheme(value => value === 'dark' ? 'light' : 'dark'), [])

  const queueCount = projectionQueue(projection).length
  const activeTitle = activeSession?.title || title

  return (
    <div className="app-shell" data-sidebar={sidebarCollapsed ? 'collapsed' : 'expanded'}>
      <Sidebar
        sessions={sessions}
        activeId={activeId}
        loading={sessionsLoading}
        projectRoot={projectRoot}
        collapsed={sidebarCollapsed}
        query={query}
        permissionMode={settings?.permission_mode ?? 'default'}
        onQuery={setQuery}
        onToggle={() => setSidebarCollapsed(value => !value)}
        onNew={newChat}
        onSelect={setActiveId}
        onDelete={id => void deleteSession(id)}
        onSettings={openSettings}
        onTasks={() => {
          if (!activeId) setToast('先选择一个会话')
          else setTasksOpen(true)
        }}
        onPickProject={() => void pickProject()}
      />
      <main className="main-column">
        <Header
          title={activeTitle}
          status={activeId ? status : 'idle'}
          permissionMode={settings?.permission_mode ?? 'default'}
          tab={tab}
          theme={theme}
          running={running}
          backgroundCount={backgroundCount}
          onTab={setTab}
          onTheme={toggleTheme}
          onSettings={openSettings}
        />
        {tab === 'chat' ? (
          <ChatPane
            activeId={activeId}
            title={activeTitle}
            status={status}
            events={events}
            liveSegments={liveSegments}
            pendingUsers={pendingUsers}
            tasks={tasks}
            approvals={approvals}
            loadingChat={chatLoading}
            running={running}
            connection={connection}
            draft={draft}
            onDraft={setDraft}
            onSend={() => void send()}
            onStop={() => void stop()}
            onNew={newChat}
            onPickProject={() => void pickProject()}
            onSettings={openSettings}
            onApprove={approve}
            onReject={reject}
            projectRoot={projectRoot}
            settings={settings}
            mode={mode}
            onMode={setMode}
            sending={sending}
            onRetry={(text) => setDraft(text)}
          />
        ) : (
          <TraceView events={traceEvents} loading={traceLoading} runId={activeId} />
        )}
      </main>

      <SettingsDialog
        open={settingsOpen}
        settings={settings}
        projectRoot={projectRoot}
        theme={theme}
        onClose={() => setSettingsOpen(false)}
        onSaved={next => { setSettings(next); setSettingsOpen(false); void refreshSessions() }}
        onPickProject={pickProject}
        onTheme={toggleTheme}
      />

      {tasksOpen && (
        <div className="modal-backdrop" role="presentation" onMouseDown={event => {
          if (event.target === event.currentTarget) setTasksOpen(false)
        }}>
          <div className="modal-card tasks-card" role="dialog" aria-modal="true" aria-label="任务">
            <div className="modal-head">
              <h2>任务</h2>
              <button type="button" className="icon-button" onClick={() => setTasksOpen(false)} aria-label="关闭">
                <X size={17} />
              </button>
            </div>
            <div className="tasks-card-body">
              <div className="tasks-summary">
                <span>{activeTitle}</span>
                <span className="tasks-state">{sessionLabel(status)}</span>
              </div>
              {tasks.length > 0
                ? <TaskPanel tasks={tasks} running={running} />
                : <div className="tasks-empty">当前会话还没有可展示的计划步骤</div>}
              {queueCount > 0 && (
                <div className="tasks-queue">还有 {queueCount} 条消息在队列中</div>
              )}
              <div className="tasks-meta">
                <span>权限 · {permissionLabel(settings?.permission_mode)}</span>
                <span>工作区 · {projectName(projectRoot || undefined)}</span>
                <span>更新时间 · {formatRelative(activeSession?.updated_at)}</span>
              </div>
            </div>
          </div>
        </div>
      )}

      {toast && (
        <div className="toast" role="status">
          {toast}
          <button type="button" onClick={() => setToast(null)} aria-label="关闭提示"><X size={13} /></button>
        </div>
      )}

    </div>
  )
}

function cursorsFrom(events: RuntimeEvent[]): Record<string, number> {
  const cursors: Record<string, number> = {}
  for (const event of events) {
    cursors[event.run_id] = Math.max(cursors[event.run_id] ?? 0, event.sequence)
  }
  return cursors
}

function byUpdatedDesc(left: Session, right: Session): number {
  const a = Date.parse(left.updated_at || left.created_at || '') || 0
  const b = Date.parse(right.updated_at || right.created_at || '') || 0
  return b - a
}
