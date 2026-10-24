import { useEffect, useMemo, useState } from 'react'
import {
  AlertCircle, CheckCircle2, ChevronDown, ChevronUp, CircleDashed, ListTodo, Loader2, XCircle,
} from 'lucide-react'
import type { TaskView } from './types'

function Glyph({ status }: { status: TaskView['status'] }) {
  if (status === 'done') return <CheckCircle2 size={14} className="task-done" />
  if (status === 'running') return <Loader2 size={14} className="task-running spin" />
  if (status === 'blocked') return <AlertCircle size={14} className="task-blocked" />
  if (status === 'failed') return <XCircle size={14} className="task-failed" />
  return <CircleDashed size={14} className="task-pending" />
}

export function TaskPanel({
  tasks, running,
}: {
  tasks: TaskView[]
  running: boolean
}) {
  // A plan is the answer's shape, not an optional detail: show the checklist
  // by default and let the user fold it when they want more transcript space.
  const [collapsed, setCollapsed] = useState(false)
  const progress = useMemo(() => {
    const done = tasks.filter(task => task.status === 'done').length
    const active = tasks.filter(task => task.status === 'running').length
    const pending = tasks.length - done - active
    return [
      done > 0 ? `${done} 已完成` : '',
      active > 0 ? `${active} 进行中` : '',
      pending > 0 ? `${pending} 待执行` : '',
    ].filter(Boolean).join(' · ')
  }, [tasks])

  useEffect(() => {
    if (running) setCollapsed(false)
  }, [running])

  if (tasks.length === 0) return null

  return (
    <section className="task-panel" aria-label="任务列表">
      <button
        type="button"
        className="task-panel-header"
        aria-expanded={!collapsed}
        onClick={() => setCollapsed(value => !value)}
      >
        <ListTodo size={15} className="task-panel-lead" />
        <span className="task-panel-title">任务列表</span>
        <span className="task-panel-progress">{progress}</span>
        <span className="task-panel-chevron">
          {collapsed ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
        </span>
      </button>
      {!collapsed && (
        <ol className="task-list">
          {tasks.map(task => (
            <li key={task.id} className="task-item" data-status={task.status}>
              <span className="task-glyph"><Glyph status={task.status} /></span>
              <span className="task-title">{task.title}</span>
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}
