import { memo, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertTriangle, Check, ChevronRight, Circle, Clipboard, FileText, Globe, ListTodo,
  Loader2, Pencil, Search, Terminal, Wrench, ArrowDown,
} from 'lucide-react'
import type {
  Approval, LiveSegment, RuntimeEvent, Settings, TaskView,
} from './types'
import { buildFlow, statusFromEvent, toolLabel, type FlowItem } from './events'
import { projectName, sessionLabel, timeLabel } from './api'
import { Markdown } from './Markdown'
import { Composer } from './Composer'
import { TaskPanel } from './TaskPanel'
import { ApprovalCard } from './ApprovalCard'
import { WhaleMark } from './Logo'

const INITIAL_VISIBLE = 160

function useCopy() {
  const [copied, setCopied] = useState(false)
  const timer = useRef<number | undefined>(undefined)
  useEffect(() => () => window.clearTimeout(timer.current), [])
  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      window.clearTimeout(timer.current)
      timer.current = window.setTimeout(() => setCopied(false), 1400)
    } catch {
      // Clipboard can be unavailable in non-secure contexts; leave the UI quiet.
    }
  }
  return { copied, copy }
}

function toolIcon(name: string | undefined) {
  const value = (name || '').toLowerCase()
  if (value.includes('run_command') || value.includes('bash') || value.includes('shell')) return Terminal
  if (value.includes('read')) return FileText
  if (value.includes('write') || value.includes('edit')) return Pencil
  if (value.includes('search') || value.includes('grep')) return Search
  if (value.includes('todo') || value.includes('plan')) return ListTodo
  if (value.includes('fetch') || value.includes('web')) return Globe
  return Wrench
}

function visibleAssistantText(text: string): string {
  let value = text.replace(/<cc-harness-complete>[\s\S]*?<\/cc-harness-complete>/gi, '')
  value = value.replace(/```(?:json)?\s*([\s\S]*?)```/g, (match, body: string) => (
    /"acceptance_criteria"|"evidence"\s*:/.test(body) ? '' : match
  ))
  value = value.trim()
  // The completion gate's envelope is an internal protocol exchange.  It is
  // stored for audit but must never appear as prose, including in the live
  // prefix before the durable (and already filtered) message lands.
  if (/^\{\s*"acceptance_criteria"/.test(value)) return ''
  return value
}

const UserMessage = memo(function UserMessage({ item }: { item: Extract<FlowItem, { kind: 'user' }> }) {
  const { copied, copy } = useCopy()
  return (
    <div className="message-row user-row">
      <div className={`user-bubble${item.pending ? ' pending' : ''}${item.failed ? ' failed' : ''}`}>
        {item.content}
      </div>
      <div className="message-meta user-meta">
        {item.pending && <span className="meta-pending">发送中…</span>}
        {item.failed && <span className="meta-failed">发送失败</span>}
        {!item.pending && !item.failed && item.time && <span>{timeLabel(item.time)}</span>}
        <button type="button" className="message-action" aria-label="复制消息" onClick={() => void copy(item.content)}>
          {copied ? <Check size={13} /> : <Clipboard size={13} />}
        </button>
      </div>
    </div>
  )
})

const AssistantMessage = memo(function AssistantMessage({
  item,
}: {
  item: Extract<FlowItem, { kind: 'assistant' }>
}) {
  const { copied, copy } = useCopy()
  const content = visibleAssistantText(item.content)
  if (!content) return null
  return (
    <div className="message-row assistant-row">
      <div className="assistant-body">
        <Markdown text={content} />
        {item.streaming && <span className="stream-caret" aria-hidden />}
      </div>
      <div className="message-meta assistant-meta">
        <button type="button" className="message-action" aria-label="复制回复" onClick={() => void copy(content)}>
          {copied ? <Check size={13} /> : <Clipboard size={13} />}
        </button>
      </div>
    </div>
  )
})

function ReasoningMessage({ item }: { item: Extract<FlowItem, { kind: 'reasoning' }> }) {
  const [open, setOpen] = useState(false)
  const firstLine = item.content.replace(/\s+/g, ' ').slice(0, 120)
  return (
    <details className="reasoning-row" data-running={item.streaming || undefined} open={open} onToggle={event => setOpen((event.currentTarget as HTMLDetailsElement).open)}>
      <summary className="reasoning-summary">
        <span className="reasoning-spark" aria-hidden>✦</span>
        <span className="reasoning-title">{item.streaming ? '思考中' : '思考'}</span>
        <span className="reasoning-preview">{firstLine}</span>
        <ChevronRight size={13} className="reasoning-chevron" />
      </summary>
      <div className="reasoning-body">{item.content}</div>
    </details>
  )
}

function ToolMessage({ item }: { item: Extract<FlowItem, { kind: 'tool' }> }) {
  const Icon = toolIcon(item.name)
  const [open, setOpen] = useState(false)
  return (
    <details className="tool-row" data-status={item.status} open={open} onToggle={event => setOpen((event.currentTarget as HTMLDetailsElement).open)}>
      <summary className="tool-summary">
        <span className="tool-icon">
          {item.status === 'running'
            ? <Loader2 size={14} className="spin" />
            : <Icon size={14} />}
        </span>
        <span className="tool-label">{item.label}</span>
        {item.summary && <span className="tool-detail-label">·</span>}
        {item.summary && <span className="tool-detail">{item.summary}</span>}
        {item.status === 'error' && <span className="tool-state error">失败</span>}
        {item.status === 'rejected' && <span className="tool-state error">已拒绝</span>}
        <ChevronRight size={13} className="tool-chevron" />
      </summary>
      <div className="tool-body">
        {item.arguments !== undefined && (
          <div className="tool-block">
            <span className="tool-block-title">参数</span>
            <pre>{JSON.stringify(item.arguments, null, 2)}</pre>
          </div>
        )}
        {item.result && (
          <div className="tool-block">
            <span className="tool-block-title">输出</span>
            <pre>{item.result}</pre>
          </div>
        )}
        {!item.arguments && !item.result && <div className="tool-empty">暂无更多详情</div>}
      </div>
    </details>
  )
}

function NoticeMessage({ item }: { item: Extract<FlowItem, { kind: 'notice' | 'error' }> }) {
  const error = item.kind === 'error'
  return (
    <div className={`notice-row${error ? ' notice-error' : ''}`}>
      {error ? <AlertTriangle size={14} /> : <Circle size={8} fill="currentColor" />}
      <span>{item.content}</span>
    </div>
  )
}

function FlowRow({ item }: { item: FlowItem }) {
  switch (item.kind) {
    case 'user': return <UserMessage item={item} />
    case 'assistant': return <AssistantMessage item={item} />
    case 'reasoning': return <ReasoningMessage item={item} />
    case 'tool': return <ToolMessage item={item} />
    case 'error':
    case 'notice': return <NoticeMessage item={item} />
    default: return null
  }
}

type Turn = {
  id: string
  user?: Extract<FlowItem, { kind: 'user' }>
  items: FlowItem[]
}

function groupTurns(flow: FlowItem[]): Turn[] {
  const turns: Turn[] = []
  let current: Turn | null = null
  for (const item of flow) {
    if (item.kind === 'user') {
      if (current && (current.user || current.items.length > 0)) turns.push(current)
      current = { id: item.id, user: item, items: [] }
      continue
    }
    if (!current) current = { id: `turn-${turns.length}`, items: [] }
    current.items.push(item)
  }
  if (current) turns.push(current)
  return turns
}

function TurnProcess({ items, defaultOpen, running }: {
  items: FlowItem[]
  defaultOpen: boolean
  running: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  const errors = items.filter(item => item.kind === 'error' || (item.kind === 'tool' && item.status === 'error')).length
  return (
    <details
      className="turn-process"
      open={open}
      onToggle={event => setOpen((event.currentTarget as HTMLDetailsElement).open)}
    >
      <summary className="turn-process-summary">
        <span className="turn-process-title">执行过程</span>
        <span className="turn-process-count">{items.length} 步</span>
        {errors > 0 && <span className="turn-process-error">{errors} 失败</span>}
        {running && <Loader2 size={13} className="spin turn-process-spinner" />}
        <ChevronRight size={13} className="turn-process-chevron" />
      </summary>
      {open && (
        <div className="turn-process-body">
          {items.map(item => <FlowRow key={item.id} item={item} />)}
        </div>
      )}
    </details>
  )
}

function TurnView({ turn, last, running }: { turn: Turn; last: boolean; running: boolean }) {
  const finalIndex = (() => {
    for (let index = turn.items.length - 1; index >= 0; index -= 1) {
      if (turn.items[index]?.kind === 'assistant') return index
    }
    return -1
  })()
  const before = finalIndex >= 0 ? turn.items.slice(0, finalIndex) : turn.items
  const final = finalIndex >= 0 ? turn.items[finalIndex] : undefined
  const after = finalIndex >= 0 ? turn.items.slice(finalIndex + 1) : []
  const process = before
  return (
    <div className="turn-view" data-running={last && running ? true : undefined}>
      {turn.user && <UserMessage item={turn.user} />}
      {process.length > 2 && (
        <TurnProcess
          items={process}
          defaultOpen={last && running ? process.length <= 10 : false}
          running={last && running}
        />
      )}
      {process.length <= 2 && process.map(item => <FlowRow key={item.id} item={item} />)}
      {final && final.kind === 'assistant' && <AssistantMessage item={final} />}
      {after.map(item => <FlowRow key={item.id} item={item} />)}
    </div>
  )
}

function RunStatus({ running, status }: { running: boolean; status: string }) {
  const start = useRef(Date.now())
  const [elapsed, setElapsed] = useState(0)
  useEffect(() => {
    if (!running) return undefined
    // The component stays mounted across turns; reset the clock when a new
    // run starts instead of measuring from the first page paint.
    start.current = Date.now()
    setElapsed(0)
    const timer = window.setInterval(
      () => setElapsed(Math.floor((Date.now() - start.current) / 1000)),
      1000,
    )
    return () => window.clearInterval(timer)
  }, [running])
  if (!running) return null
  return (
    <div className="run-status" role="status">
      <span className="run-status-text">运行中</span>
      {elapsed >= 15 && <span className="run-status-clock">用时 {formatSeconds(elapsed)}</span>}
      <span className="sr-only">{sessionLabel(status)}</span>
    </div>
  )
}

function formatSeconds(seconds: number): string {
  if (seconds < 60) return `${seconds} 秒`
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
}

export type ChatPaneProps = {
  activeId: string | null
  title: string
  status: string
  events: RuntimeEvent[]
  liveSegments: LiveSegment[]
  pendingUsers: Array<{ id: string; content: string; failed?: boolean }>
  tasks: TaskView[]
  approvals: Approval[]
  loadingChat: boolean
  running: boolean
  connection: 'connecting' | 'open' | 'reconnecting' | 'offline'
  draft: string
  onDraft: (value: string) => void
  onSend: () => void
  onStop: () => void
  onNew: () => void
  onPickProject: () => void
  onSettings: () => void
  onApprove: (approval: Approval) => Promise<void>
  onReject: (approval: Approval) => Promise<void>
  projectRoot: string | null
  settings: Settings | null
  mode: 'coding' | 'plan'
  onMode: (mode: 'coding' | 'plan') => void
  sending: boolean
  onRetry: (text: string) => void
}

export function ChatPane(props: ChatPaneProps) {
  const {
    activeId, events, liveSegments, pendingUsers, tasks, approvals, loadingChat,
    running, connection, projectRoot, settings, draft, onDraft, onSend, onStop,
    onNew, onPickProject, onSettings, onApprove, onReject, mode, onMode, sending,
  } = props
  const scroller = useRef<HTMLDivElement | null>(null)
  const stick = useRef(true)
  const [atBottom, setAtBottom] = useState(true)
  const [visible, setVisible] = useState(INITIAL_VISIBLE)
  const [elapsedTick, setElapsedTick] = useState(0)

  const flow = useMemo(() => {
    const durable = buildFlow(events)
    const live: FlowItem[] = []
    for (const segment of liveSegments) {
      if (segment.reasoning) {
        live.push({
          kind: 'reasoning',
          id: `live:${segment.key}:reasoning`,
          content: segment.reasoning,
          streaming: segment.phase === 'reasoning',
        })
      }
      if (segment.content) {
        live.push({
          kind: 'assistant',
          id: `live:${segment.key}:content`,
          content: segment.content,
          streaming: segment.phase !== 'done',
        })
      }
    }
    const pending: FlowItem[] = pendingUsers.map(item => ({
      kind: 'user',
      id: item.id,
      content: item.content,
      pending: !item.failed,
      failed: item.failed,
    }))
    return [...durable, ...live, ...pending]
  }, [events, liveSegments, pendingUsers])

  const turns = useMemo(() => groupTurns(flow), [flow])
  const shownTurns = useMemo(
    () => turns.slice(Math.max(0, turns.length - visible)),
    [turns, visible],
  )
  const signature = useMemo(() => {
    const last = flow[flow.length - 1]
    if (!last) return `${flow.length}`
    if (last.kind === 'assistant' || last.kind === 'reasoning' || last.kind === 'user') {
      return `${flow.length}:${last.content.length}`
    }
    return `${flow.length}`
  }, [flow])

  useEffect(() => {
    setVisible(INITIAL_VISIBLE)
    stick.current = true
    setAtBottom(true)
    const node = scroller.current
    if (node) node.scrollTop = node.scrollHeight
  }, [activeId])

  useEffect(() => {
    if (!running) return undefined
    const timer = window.setInterval(() => setElapsedTick(value => value + 1), 1000)
    return () => window.clearInterval(timer)
  }, [running])

  useEffect(() => {
    if (!stick.current) return
    const node = scroller.current
    if (!node) return
    node.scrollTop = node.scrollHeight
  }, [signature, turns.length, running, elapsedTick])

  function onScroll() {
    const node = scroller.current
    if (!node) return
    const near = node.scrollHeight - node.scrollTop - node.clientHeight < 32
    stick.current = near
    setAtBottom(near)
  }

  const hero = !activeId
  const projectChip = (
    <button type="button" className="hero-project" onClick={onPickProject}>
      <span className="hero-project-folder" aria-hidden>▱</span>
      <span>{projectRoot ? projectName(projectRoot) : '选择工作区'}</span>
      <ChevronRight size={13} />
    </button>
  )

  return (
    <div className="chat-root">
      <div className="chat-scroll" ref={scroller} onScroll={onScroll} data-active={activeId || undefined}>
        {hero ? (
          <div className="hero">
            <div className="hero-inner">
              <div className="hero-mark"><WhaleMark size={42} /></div>
              <h2 className="hero-title">有什么可以帮忙的？</h2>
              <p className="hero-subtitle">写代码、改文件、跑命令，或者只是聊聊你手上的问题。</p>
              <div className="hero-project-row">{projectChip}</div>
              <div className="hero-composer">
                <Composer
                  draft={draft}
                  onDraft={onDraft}
                  onSend={onSend}
                  onStop={onStop}
                  onPickProject={onPickProject}
                  onSettings={onSettings}
                  projectRoot={projectRoot}
                  settings={settings}
                  running={false}
                  sending={sending}
                  mode={mode}
                  onMode={onMode}
                  hero
                />
              </div>
            </div>
          </div>
        ) : (
          <div className={`chat-column${loadingChat ? ' is-loading' : ''}`}>
            {loadingChat && events.length === 0 && (
              <div className="chat-loading">
                <span className="loading-line" />
                <span className="loading-line short" />
                <span className="loading-line" />
                <span className="loading-caption">正在加载会话…</span>
              </div>
            )}
            {!loadingChat && flow.length === 0 && (
              <div className="chat-empty">
                <span className="empty-spinner" aria-hidden />
                <span>{running ? '正在准备上下文…' : '这个会话还没有消息'}</span>
              </div>
            )}
            {turns.length > visible && (
              <button type="button" className="load-older" onClick={() => setVisible(value => value + 40)}>
                加载更早的轮次
              </button>
            )}
            {shownTurns.map((turn, index) => (
              <TurnView
                key={turn.id}
                turn={turn}
                last={index === shownTurns.length - 1}
                running={running}
              />
            ))}
            <RunStatus running={running} status={props.status} />
            {connection === 'reconnecting' && (
              <div className="connection-hint">实时连接正在重连，已显示的最后状态仍然有效</div>
            )}
          </div>
        )}
      </div>

      {!hero && (
        <div className="composer-dock">
          <div className="composer-dock-inner">
            <ApprovalCard
              approvals={approvals}
              toolLabel={toolLabel}
              onApprove={onApprove}
              onReject={onReject}
            />
            <TaskPanel tasks={tasks} running={running} />
            <Composer
              draft={draft}
              onDraft={onDraft}
              onSend={onSend}
              onStop={onStop}
              onPickProject={onPickProject}
              onSettings={onSettings}
              projectRoot={projectRoot}
              settings={settings}
              running={running}
              sending={sending}
              mode={mode}
              onMode={onMode}
            />
          </div>
        </div>
      )}

      {!hero && !atBottom && (
        <button
          type="button"
          className="to-bottom"
          aria-label="回到底部"
          onClick={() => {
            stick.current = true
            setAtBottom(true)
            const node = scroller.current
            if (node) node.scrollTop = node.scrollHeight
          }}
        >
          <ArrowDown size={15} />
        </button>
      )}
    </div>
  )
}

export function TraceView({
  events, loading, runId,
}: {
  events: RuntimeEvent[]
  loading: boolean
  runId: string | null
}) {
  const rows = useMemo(() => [...events].reverse().slice(0, 800), [events])
  if (!runId) {
    return <div className="trace-empty">选择一个会话后查看完整轨迹</div>
  }
  return (
    <div className="trace-root">
      <div className="trace-column">
        <div className="trace-heading">
          <span>事件轨迹</span>
          <span className="trace-meta">{loading ? '加载中…' : `${events.length} 条事件`}</span>
        </div>
        {loading && events.length === 0 && (
          <div className="trace-loading"><span className="empty-spinner" /> 正在读取 Durable 事件…</div>
        )}
        {rows.map(event => (
          <div className="trace-row" key={event.id}>
            <span className="trace-seq">#{event.sequence}</span>
            <span className="trace-type">{event.event_type}</span>
            <span className="trace-time">{timeLabel(event.occurred_at)}</span>
            <span className="trace-content">
              {event.content || event.tool_name || event.status || ''}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

export function statusOfEvent(current: string, event: RuntimeEvent): string {
  return statusFromEvent(current, event)
}
