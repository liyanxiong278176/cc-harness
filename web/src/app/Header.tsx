import { ChevronDown, Moon, Settings, Sun } from 'lucide-react'
import { permissionLabel, sessionLabel } from './api'

type Props = {
  title: string
  status: string
  permissionMode: string
  tab: 'chat' | 'trace'
  theme: 'dark' | 'light'
  running: boolean
  backgroundCount: number
  onTab: (tab: 'chat' | 'trace') => void
  onTheme: () => void
  onSettings: () => void
}

export function Header({
  title, status, permissionMode, tab, theme, running, backgroundCount,
  onTab, onTheme, onSettings,
}: Props) {
  return (
    <header className="main-header">
      <div className="header-title-row">
        <div className="header-title-cluster">
          <h1 className="session-heading" title={title}>{title || '新对话'}</h1>
          <span className="mode-chip">标准模式</span>
          {status !== 'idle' && (
            <span className={`status-chip status-${status}`}>
              {running ? <span className="status-spinner" aria-hidden /> : <span className="status-dot" aria-hidden />}
              {running ? '运行中' : sessionLabel(status)}
            </span>
          )}
          {backgroundCount > 0 && (
            <span className="runtime-chip">
              <span className="runtime-dot" aria-hidden />
              {backgroundCount} 个后台任务运行中
              <ChevronDown size={12} />
            </span>
          )}
        </div>
        <div className="header-actions">
          <button type="button" className="icon-button" onClick={onTheme} aria-label={theme === 'dark' ? '切换浅色主题' : '切换深色主题'}>
            {theme === 'dark' ? <Sun size={17} /> : <Moon size={17} />}
          </button>
          <button type="button" className="icon-button" onClick={onSettings} aria-label="设置">
            <Settings size={17} />
          </button>
        </div>
      </div>
      <div className="header-tabs" role="tablist">
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'chat'}
          className={`header-tab${tab === 'chat' ? ' active' : ''}`}
          onClick={() => onTab('chat')}
        >
          对话
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'trace'}
          className={`header-tab${tab === 'trace' ? ' active' : ''}`}
          onClick={() => onTab('trace')}
        >
          轨迹
        </button>
      </div>
      <span className="sr-only">{permissionLabel(permissionMode)}</span>
    </header>
  )
}
