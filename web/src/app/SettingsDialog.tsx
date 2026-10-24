import { useEffect, useState } from 'react'
import { FolderOpen, Moon, Sun, X } from 'lucide-react'
import type { Settings } from './types'
import { projectName } from './api'

type Props = {
  open: boolean
  settings: Settings | null
  projectRoot: string | null
  theme: 'dark' | 'light'
  onClose: () => void
  onSaved: (settings: Settings) => void
  onPickProject: () => Promise<void>
  onTheme: () => void
}

const PERMISSIONS = [
  { value: 'default', label: '请求批准', detail: '执行命令或写文件前询问' },
  { value: 'auto-edit', label: '自动批准编辑', detail: '文件编辑自动通过，命令仍需批准' },
  { value: 'bypass-prompts', label: '完全访问权限', detail: '不再弹出批准请求' },
]

export function SettingsDialog({
  open, settings, projectRoot, theme, onClose, onSaved, onPickProject, onTheme,
}: Props) {
  const [baseUrl, setBaseUrl] = useState('')
  const [model, setModel] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [permission, setPermission] = useState('default')
  const [busy, setBusy] = useState(false)
  const [picking, setPicking] = useState(false)
  const [message, setMessage] = useState<{ tone: 'ok' | 'error'; text: string } | null>(null)

  useEffect(() => {
    if (!open) return
    setBaseUrl(settings?.base_url ?? '')
    setModel(settings?.model ?? '')
    setApiKey('')
    setPermission(settings?.permission_mode ?? 'default')
    setMessage(null)
  }, [open, settings])

  if (!open) return null

  async function save() {
    setBusy(true)
    setMessage(null)
    try {
      const payload: Record<string, string> = {
        base_url: baseUrl.trim(),
        model: model.trim(),
        permission_mode: permission,
      }
      if (apiKey.trim()) payload.api_key = apiKey.trim()
      const response = await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      const body = await response.json().catch(() => ({})) as { settings?: Settings; detail?: { message?: string } }
      if (!response.ok) throw new Error(body.detail?.message || `保存失败 (${response.status})`)
      const next = body.settings ?? { ...(settings as Settings), base_url: baseUrl, model, permission_mode: permission }
      onSaved(next)
      setMessage({ tone: 'ok', text: '设置已保存' })
    } catch (reason) {
      setMessage({ tone: 'error', text: reason instanceof Error ? reason.message : String(reason) })
    } finally {
      setBusy(false)
    }
  }

  async function pickProject() {
    setPicking(true)
    try {
      await onPickProject()
    } finally {
      setPicking(false)
    }
  }

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={event => {
      if (event.target === event.currentTarget) onClose()
    }}>
      <div className="modal-card settings-card" role="dialog" aria-modal="true" aria-label="设置">
        <div className="modal-head">
          <h2>设置</h2>
          <button type="button" className="icon-button" onClick={onClose} aria-label="关闭">
            <X size={17} />
          </button>
        </div>

        <div className="settings-body">
          <section className="settings-section">
            <div className="settings-section-title">工作区</div>
            <div className="settings-project">
              <div>
                <div className="settings-project-name">{projectName(projectRoot || undefined)}</div>
                <div className="settings-project-path">{projectRoot || '尚未选择本地项目文件夹'}</div>
              </div>
              <button type="button" className="button subtle" onClick={() => void pickProject()} disabled={picking}>
                <FolderOpen size={14} />
                {picking ? '选择中…' : '选择文件夹'}
              </button>
            </div>
          </section>

          <section className="settings-section">
            <div className="settings-section-title">模型</div>
            <label className="field">
              <span>Base URL</span>
              <input value={baseUrl} onChange={event => setBaseUrl(event.target.value)} placeholder="https://api.deepseek.com" spellCheck={false} />
            </label>
            <label className="field">
              <span>模型</span>
              <input value={model} onChange={event => setModel(event.target.value)} placeholder="deepseek-flash" spellCheck={false} />
            </label>
            <label className="field">
              <span>API Key</span>
              <input
                value={apiKey}
                onChange={event => setApiKey(event.target.value)}
                placeholder={settings?.has_api_key ? `已配置 ${settings.api_key_masked || ''}，留空则不修改` : '输入 API Key'}
                type="password"
                autoComplete="off"
                spellCheck={false}
              />
            </label>
          </section>

          <section className="settings-section">
            <div className="settings-section-title">权限</div>
            <div className="permission-list">
              {PERMISSIONS.map(item => (
                <label className={`permission-row${permission === item.value ? ' selected' : ''}`} key={item.value}>
                  <input
                    type="radio"
                    name="permission"
                    value={item.value}
                    checked={permission === item.value}
                    onChange={() => setPermission(item.value)}
                  />
                  <span>
                    <span className="permission-label">{item.label}</span>
                    <span className="permission-detail">{item.detail}</span>
                  </span>
                </label>
              ))}
            </div>
          </section>

          <section className="settings-section">
            <div className="settings-section-title">外观</div>
            <button type="button" className="button subtle theme-button" onClick={onTheme}>
              {theme === 'dark' ? <Sun size={15} /> : <Moon size={15} />}
              {theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'}
            </button>
          </section>
        </div>

        <div className="modal-foot">
          {message && <span className={`settings-message ${message.tone}`}>{message.text}</span>}
          <button type="button" className="button ghost" onClick={onClose}>取消</button>
          <button type="button" className="button primary" onClick={() => void save()} disabled={busy}>
            {busy ? '保存中…' : '保存设置'}
          </button>
        </div>
      </div>
    </div>
  )
}
