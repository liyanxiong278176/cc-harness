import { useEffect, useRef, useState } from 'react'
import { RefreshCw, X } from 'lucide-react'
import { webApi } from './api'

type Source = { source_id?: string; source_type?: string; selected_reason?: string; omitted_reason?: string; transform?: string; content_hash?: string }
type Assets = {
  project_root: string
  memory: { available: boolean; items: { id: string; text: string; source: string; version: number; provenance?: { verified_at?: number; kind?: string } }[] }
  skills: { id: string; name: string; scope: string; description: string; enabled: boolean; conflict: boolean; digest: string }[]
  context: { run_id: string; manifest: { sources?: Source[]; context_policy?: { version?: string }; coverage?: { integrity_errors?: string[] }; skill_snapshot?: string } | null; continuation: { source_sequence: number; next_step: string; uncertain_actions: unknown[] }; workspace?: { items: { path: string; status: string }[] } } | null
  cleanup: { items: { digest: string; size_bytes: number }[]; token: string; protected_count: number }
}

export function LocalAssetsModal({ runId, projectRoot, onClose }: { runId: string | null; projectRoot: string; onClose: () => void }) {
  const dialog = useRef<HTMLElement>(null)
  const [data, setData] = useState<Assets | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [tab, setTab] = useState('memory')
  const [nodeId, setNodeId] = useState('')
  const [path, setPath] = useState('')
  const [notice, setNotice] = useState<string | null>(null)
  const generation = useRef(0)
  const close = useRef(onClose)
  close.current = onClose

  async function load() {
    const current = ++generation.current
    setBusy(true)
    setError(null)
    try {
      const query = new URLSearchParams({ project_root: projectRoot })
      if (runId) query.set('run_id', runId)
      const result = await webApi<Assets>('/api/local-assets?' + query)
      if (current === generation.current) setData(result)
    } catch (e) { if (current === generation.current) setError(e instanceof Error ? e.message : String(e)) }
    finally { if (current === generation.current) setBusy(false) }
  }
  useEffect(() => { void load(); return () => { generation.current++ } }, [runId])
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    dialog.current?.focus()
    function keys(event: KeyboardEvent) {
      if (event.key === 'Escape') { event.preventDefault(); close.current() }
      if (event.key !== 'Tab') return
      const controls = Array.from(dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), [tabindex="0"]') ?? [])
      const first = controls[0], last = controls[controls.length - 1]
      if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog.current)) { event.preventDefault(); last?.focus() }
      else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialog.current)) { event.preventDefault(); first?.focus() }
    }
    document.addEventListener('keydown', keys)
    return () => { document.removeEventListener('keydown', keys); previous?.focus() }
  }, [])

  async function act(action: string, extra: Record<string, unknown> = {}) {
    setBusy(true); setError(null); setNotice(null)
    try {
      await webApi('/api/local-assets', { method: 'POST', body: JSON.stringify({ action, run_id: data?.context?.run_id ?? runId, project_root: projectRoot, ...extra }) })
      setNotice(action === 'forget' ? '已遗忘相关长期记忆及派生内容，原始聊天和执行记录保留。' : '操作已完成。')
      await load()
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); setBusy(false) }
  }

  return <div className="modal-backdrop" onMouseDown={e => { if (e.target === e.currentTarget) onClose() }}>
    <section className="settings-modal local-assets-modal" ref={dialog} role="dialog" aria-modal="true" aria-label="本地能力与记忆" tabIndex={-1}>
      <div className="modal-heading"><h2>本地能力与记忆</h2><div><button className="icon-button" onClick={() => void load()} disabled={busy} aria-label="刷新"><RefreshCw size={17} /></button><button className="icon-button" onClick={onClose} aria-label="关闭"><X size={19} /></button></div></div>
      <p className="modal-intro">管理当前项目的长期记忆、Skill 和恢复资料。</p>
      <nav className="asset-tabs" aria-label="本地资产分类">{[['memory', '记忆'], ['skills', 'Skill'], ['context', '上下文来源'], ['storage', '引用与清理']].map(([id, label]) => <button key={id} aria-pressed={tab === id} onClick={() => setTab(id)}>{label}</button>)}</nav>
      {busy && <p role="status">正在处理…</p>}{error && <p role="alert" className="asset-error">{error}</p>}{notice && <p role="status">{notice}</p>}
      {data && tab === 'memory' && <div><p>记忆作为历史参考，不能覆盖当前要求。遗忘不会删除原始聊天。</p>{!data.memory.items.length && <p>当前项目暂无长期记忆。</p>}{data.memory.items.map(m => <article className="asset-item" key={m.id}><p>{m.text}</p><small>来源：{m.source} · 版本 {m.version}{m.provenance?.verified_at ? ` · 核验于 ${new Date(m.provenance.verified_at * 1000).toLocaleString()}` : ' · 未记录核验时间'}</small><button disabled={busy} onClick={() => void act('forget', { id: m.id })}>遗忘这条记忆</button></article>)}</div>}
      {data && tab === 'skills' && <div><p>启停默认对下一个任务生效。当前任务使用固定版本；暂停后可显式重新加载。</p>{!data.skills.length && <p>暂无 Skill。可放入项目的 .cc-harness/skills 或用户目录中的 skills 文件夹。</p>}{data.skills.map(s => <article className="asset-item" key={s.id}><strong>{s.name}</strong><small>{s.scope === 'project' ? '项目' : '用户'} · {s.id}{s.conflict ? ' · 存在同名来源，启用即选择此来源' : ''}</small><p>{s.description}</p><button disabled={busy} onClick={() => void act(s.enabled ? 'disable' : 'enable', { id: s.id })}>{s.enabled ? '停用' : s.conflict ? '选择并启用此来源' : '启用'}</button></article>)}{runId && <button disabled={busy} onClick={() => void act('reload')}>为当前已暂停任务重新加载 Skill</button>}</div>}
      {data && tab === 'context' && <div>{!data.context ? <p>选择一个任务后查看来源。</p> : <><p>续行资料来自第 {data.context.continuation.source_sequence} 个已提交事件；来源清单对应最近一次模型请求。</p>{data.context.continuation.uncertain_actions.length > 0 && <p className="asset-error">存在结果未确认的动作，需要先核查，不能直接重试。</p>}<p>策略：{data.context.manifest?.context_policy?.version ?? '尚无调用记录'}</p>{data.context.manifest?.coverage?.integrity_errors?.map(e => <p role="alert" key={e}>{e}</p>)}{data.context.manifest?.sources?.map((s, i) => <article className="asset-item" key={i}><strong>{s.source_type} · {s.source_id}</strong><small>{s.transform} · {s.selected_reason}</small>{s.omitted_reason && <p>{s.omitted_reason}</p>}<code>{s.content_hash}</code></article>)}</>}</div>}
      {data && tab === 'storage' && <div><h3>恢复失效引用</h3><p>指定当前项目内的原始文件，内容一致才会恢复历史引用。</p><label>引用节点 ID<input value={nodeId} onChange={e => setNodeId(e.target.value)} /></label><label>项目内文件路径<input value={path} onChange={e => setPath(e.target.value)} /></label><button disabled={busy || !runId || !nodeId || !path} onClick={() => void act('bind', { id: nodeId, path })}>校验并重新绑定</button><h3>清理预览</h3><p>发现 {data.cleanup.items.length} 个超过宽限期的未提交孤立对象，共 {data.cleanup.items.reduce((n, x) => n + x.size_bytes, 0).toLocaleString()} 字节。任务历史、检查点和有效证据保留。</p><details><summary>查看候选对象</summary>{data.cleanup.items.map(x => <p key={x.digest}><code>{x.digest}</code> · {x.size_bytes} 字节</p>)}</details><button disabled={busy || !data.cleanup.items.length} onClick={() => void act('cleanup', { token: data.cleanup.token })}>清理预览中的孤立对象</button></div>}
    </section>
  </div>
}
