import { useMemo, useState } from 'react'
import {
  ChevronDown, ChevronRight, FolderOpen, MessagesSquare, PanelLeft, Plus,
  Search, Settings, Trash2, X,
} from 'lucide-react'
import type { Session } from './types'
import { formatRelative, permissionLabel, projectName } from './api'
import { WhaleMark } from './Logo'

type Props = {
  sessions: Session[]
  activeId: string | null
  loading: boolean
  projectRoot: string | null
  collapsed: boolean
  query: string
  permissionMode: string
  onQuery: (value: string) => void
  onToggle: () => void
  onNew: () => void
  onSelect: (id: string) => void
  onDelete: (id: string) => void
  onSettings: () => void
  onTasks: () => void
  onPickProject: () => void
}

type Group = {
  root: string
  label: string
  sessions: Session[]
  updated: number
}

const COLLAPSE_KEY = 'cc-harness.sidebar.groups'

function readGroups(): Record<string, boolean> {
  try {
    const raw = localStorage.getItem(COLLAPSE_KEY)
    return raw ? JSON.parse(raw) as Record<string, boolean> : {}
  } catch {
    return {}
  }
}

export function Sidebar({
  sessions, activeId, loading, projectRoot, collapsed, query, permissionMode,
  onQuery, onToggle, onNew, onSelect, onDelete, onSettings, onTasks, onPickProject,
}: Props) {
  const [closedGroups, setClosedGroups] = useState<Record<string, boolean>>(readGroups)
  const groups = useMemo<Group[]>(() => {
    const byRoot = new Map<string, Session[]>()
    const term = query.trim().toLowerCase()
    for (const session of sessions) {
      const root = session.project_root || ''
      if (term && !`${session.title} ${root}`.toLowerCase().includes(term)) continue
      const list = byRoot.get(root) ?? []
      list.push(session)
      byRoot.set(root, list)
    }
    const result: Group[] = []
    for (const [root, list] of byRoot) {
      list.sort((left, right) => {
        const a = Date.parse(left.updated_at || left.created_at || '') || 0
        const b = Date.parse(right.updated_at || right.created_at || '') || 0
        return b - a
      })
      result.push({
        root,
        label: projectName(root),
        sessions: list,
        updated: Date.parse(list[0]?.updated_at || list[0]?.created_at || '') || 0,
      })
    }
    const counts = new Map<string, number>()
    for (const group of result) counts.set(group.label, (counts.get(group.label) ?? 0) + 1)
    for (const group of result) {
      if ((counts.get(group.label) ?? 0) < 2) continue
      const parts = group.root.replace(/[\\/]+$/, '').split(/[\\/]/)
      const parent = parts[parts.length - 2]
      if (parent && parent !== group.label) group.label = `${parent} / ${group.label}`
    }
    result.sort((left, right) => right.updated - left.updated)
    return result
  }, [sessions, query])

  function toggleGroup(root: string) {
    const next = { ...closedGroups, [root]: !closedGroups[root] }
    setClosedGroups(next)
    try { localStorage.setItem(COLLAPSE_KEY, JSON.stringify(next)) } catch { /* storage may be unavailable */ }
  }

  return (
    <aside className="sidebar" data-collapsed={collapsed || undefined}>
      <div className="sidebar-logo-row">
        <button type="button" className="brand-button" onClick={onNew} aria-label="新会话">
          <span className="brand-mark"><WhaleMark size={24} /></span>
          <span className="brand-word">cc-harness</span>
          <span className="brand-badge">LOCAL</span>
        </button>
        <button type="button" className="icon-button sidebar-toggle" onClick={onToggle} aria-label={collapsed ? '展开侧栏' : '收起侧栏'}>
          <PanelLeft size={17} />
        </button>
      </div>

      <button type="button" className="new-session" onClick={onNew}>
        <Plus size={15} />
        <span className="new-session-label">新会话</span>
      </button>

      <nav className="panel-list" aria-label="主导航">
        <button type="button" className="panel-row" onClick={onNew}>
          <MessagesSquare size={16} />
          <span className="panel-title">对话</span>
        </button>
        <button type="button" className="panel-row" onClick={onTasks}>
          <FolderOpen size={16} />
          <span className="panel-title">任务</span>
        </button>
      </nav>

      <div className="workspace-region">
        <div className="workspace-heading">
          <span className="workspace-heading-label">工作区</span>
          <div className="workspace-heading-actions">
            <button type="button" className="icon-button small" onClick={() => document.getElementById('session-search')?.focus()} aria-label="搜索会话">
              <Search size={15} />
            </button>
            <button type="button" className="icon-button small" onClick={onNew} aria-label="新建会话">
              <Plus size={15} />
            </button>
            <button type="button" className="icon-button small" onClick={onPickProject} aria-label="选择工作区">
              <FolderOpen size={15} />
            </button>
          </div>
        </div>

        <div className="session-search">
          <Search size={14} />
          <input
            id="session-search"
            value={query}
            onChange={event => onQuery(event.target.value)}
            placeholder="搜索会话"
            spellCheck={false}
          />
          {query ? (
            <button type="button" className="search-clear" onClick={() => onQuery('')} aria-label="清除搜索">
              <X size={13} />
            </button>
          ) : <kbd>Ctrl K</kbd>}
        </div>

        <div className="session-groups">
          {loading && sessions.length === 0 && (
            <div className="sidebar-skeleton" aria-label="正在加载会话">
              <span /><span /><span />
            </div>
          )}
          {!loading && groups.length === 0 && (
            <div className="sidebar-empty">还没有会话<br />发送第一条消息开始</div>
          )}
          {groups.map(group => {
            const closed = closedGroups[group.root] === true
            return (
              <section className="session-group" key={group.root || 'ungrouped'}>
                <button type="button" className="group-heading" onClick={() => toggleGroup(group.root)}>
                  {closed ? <ChevronRight size={13} /> : <ChevronDown size={13} />}
                  <FolderOpen size={15} className="group-folder" />
                  <span className="group-label">{group.label}</span>
                  <span className="group-count">{group.sessions.length}</span>
                </button>
                {!closed && (
                  <div className="group-sessions">
                    {group.sessions.map(session => (
                      <div
                        key={session.run_id}
                        className={`session-row${activeId === session.run_id ? ' active' : ''}`}
                        role="button"
                        tabIndex={0}
                        onClick={() => onSelect(session.run_id)}
                        onKeyDown={event => {
                          if (event.key === 'Enter' || event.key === ' ') onSelect(session.run_id)
                        }}
                      >
                        <span className={`session-dot status-${session.status}`} aria-hidden />
                        <span className="session-copy">
                          <span className="session-title">{session.title || '新会话'}</span>
                          <span className="session-time">{formatRelative(session.updated_at || session.created_at)}</span>
                        </span>
                        <button
                          type="button"
                          className="session-delete"
                          aria-label="删除会话"
                          onClick={event => {
                            event.stopPropagation()
                            onDelete(session.run_id)
                          }}
                        >
                          <Trash2 size={13} />
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </section>
            )
          })}
        </div>
      </div>

      <div className="sidebar-footer">
        <button type="button" className="footer-row" onClick={onSettings}>
          <Settings size={16} />
          <span>设置</span>
          <span className="footer-meta">{permissionLabel(permissionMode)}</span>
        </button>
        {projectRoot && (
          <div className="sidebar-project" title={projectRoot}>
            <FolderOpen size={13} />
            <span>{projectName(projectRoot)}</span>
          </div>
        )}
      </div>
    </aside>
  )
}
