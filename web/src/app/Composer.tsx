import { useEffect, useLayoutEffect, useRef } from 'react'
import {
  ArrowUp, ChevronDown, Cpu, FolderOpen, ShieldCheck, Square,
} from 'lucide-react'
import type { Settings } from './types'
import { permissionLabel, projectName } from './api'

type Props = {
  draft: string
  onDraft: (value: string) => void
  onSend: () => void
  onStop: () => void
  onPickProject: () => void
  onSettings: () => void
  projectRoot: string | null
  settings: Settings | null
  running: boolean
  sending: boolean
  mode: 'coding' | 'plan'
  onMode: (mode: 'coding' | 'plan') => void
  hero?: boolean
}

export function Composer({
  draft, onDraft, onSend, onStop, onPickProject, onSettings, projectRoot,
  settings, running, sending, mode, onMode, hero = false,
}: Props) {
  const textarea = useRef<HTMLTextAreaElement | null>(null)
  const hasProject = Boolean(projectRoot)

  useLayoutEffect(() => {
    const node = textarea.current
    if (!node) return
    node.style.height = 'auto'
    const height = Math.min(node.scrollHeight, hero ? 160 : 168)
    node.style.height = `${height}px`
    node.style.overflowY = node.scrollHeight > height ? 'auto' : 'hidden'
  }, [draft, hero])

  useEffect(() => {
    if (hasProject) textarea.current?.focus({ preventScroll: true })
  }, [hasProject])

  function submit() {
    if (!hasProject || sending) return
    if (!draft.trim()) return
    onSend()
  }

  return (
    <div className={`composer${hero ? ' composer-hero' : ''}`}>
      <div
        className={`composer-card${!hasProject ? ' composer-no-project' : ''}`}
        onClick={!hasProject ? onPickProject : undefined}
      >
        <textarea
          ref={textarea}
          className="composer-input"
          value={draft}
          rows={1}
          spellCheck={false}
          placeholder={hasProject ? '给 cc-harness 发送消息…' : '请选择一个工作区'}
          onChange={event => onDraft(event.target.value)}
          onKeyDown={event => {
            if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault()
              submit()
            }
          }}
          aria-label="消息输入"
        />
        <div className="composer-toolbar">
          <div className="composer-tools">
            <button
              type="button"
              className="composer-chip composer-folder"
              onClick={onPickProject}
              title={projectRoot || '选择工作区'}
            >
              <FolderOpen size={14} />
              <span>{projectName(projectRoot || undefined)}</span>
            </button>
            <button
              type="button"
              className="composer-chip"
              onClick={() => onMode(mode === 'plan' ? 'coding' : 'plan')}
              title="切换计划模式"
            >
              <span>{mode === 'plan' ? '计划模式' : '标准模式'}</span>
              <ChevronDown size={12} />
            </button>
            <button
              type="button"
              className="composer-chip"
              onClick={onSettings}
              title="权限设置"
            >
              <ShieldCheck size={14} />
              <span>{permissionLabel(settings?.permission_mode)}</span>
            </button>
          </div>
          <div className="composer-trailing">
            <button type="button" className="composer-chip model-chip" onClick={onSettings} title="模型设置">
              <Cpu size={14} />
              <span>{settings?.model || '未配置模型'}</span>
            </button>
            {running && (
              <button type="button" className="composer-stop" onClick={onStop} aria-label="停止运行">
                <Square size={13} fill="currentColor" />
              </button>
            )}
            <button
              type="button"
              className="composer-send"
              disabled={!hasProject || !draft.trim() || sending}
              onClick={submit}
              aria-label="发送"
            >
              <ArrowUp size={17} />
            </button>
          </div>
        </div>
      </div>
      <div className="composer-hint">
        <span>Enter 发送 · Shift+Enter 换行</span>
        {running && <span className="composer-running">正在生成，可继续排队消息</span>}
      </div>
    </div>
  )
}
