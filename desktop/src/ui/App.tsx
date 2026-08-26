import { useEffect, useMemo, useRef, useState } from "react";
import { getVersion } from "@tauri-apps/api/app";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { invoke } from "@tauri-apps/api/core";
import { listen, UnlistenFn } from "@tauri-apps/api/event";
import { relaunch } from "@tauri-apps/plugin-process";
import { check, DownloadEvent } from "@tauri-apps/plugin-updater";
import { DesktopBridge, BridgeMessage } from "../bridge";

type RunSummary = { run_id: string; status: string; sequence: number; storage_error?: boolean };
type Usage = { input_tokens: number; output_tokens: number; cache_read_input_tokens: number; cache_hit_ratio: number; reported_cost: number | null; reported_cost_currency: string | null; cost_status: string };
type WorkspaceConfig = { workspace: string; env_path: string; base_url: string; model: string; has_api_key: boolean; api_key_masked?: string | null; configured: boolean; runtime_started?: boolean };
type UpdateState = "idle" | "checking" | "installing" | "latest" | "error";

const bridge = new DesktopBridge();
const suggestions = ["检查当前项目的工作区状态", "分析最近一次运行并给出下一步", "运行测试并修复失败项"];

function App() {
  // Never silently connect to the installation directory. It is rarely the
  // project the user wants and was the main source of the “cannot use it” UX.
  const [cwd, setCwd] = useState("");
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [activeRun, setActiveRun] = useState<string | null>(null);
  const [composer, setComposer] = useState("");
  const [events, setEvents] = useState<BridgeMessage[]>([]);
  const [runtimeStarted, setRuntimeStarted] = useState(false);
  const [bridgeConnected, setBridgeConnected] = useState(false);
  const [model, setModel] = useState("未配置");
  const [connection, setConnection] = useState("未连接");
  const [error, setError] = useState<string | null>(null);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [appVersion, setAppVersion] = useState("未知");
  const [updateState, setUpdateState] = useState<UpdateState>("idle");
  const [updateMessage, setUpdateMessage] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsLoading, setSettingsLoading] = useState(false);
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [settings, setSettings] = useState({ workspace: "", base_url: "", api_key: "", model: "", has_api_key: false, api_key_masked: null as string | null });
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const current = useMemo(() => runs.find((run) => run.run_id === activeRun) ?? null, [activeRun, runs]);

  async function syncTrayStatus(nextRuns: RunSummary[]) {
    const activeCount = nextRuns.filter((run) => ["queued", "running", "awaiting_approval", "cancel_requested", "stalled"].includes(run.status)).length;
    const approvalCount = nextRuns.filter((run) => run.status === "awaiting_approval").length;
    const state = nextRuns.some((run) => run.status === "stalled" || run.status === "failed_terminal") ? "attention" : approvalCount > 0 ? "approval" : activeCount > 0 ? "active" : nextRuns.some((run) => run.status === "completed") ? "completed" : "idle";
    try { await invoke("update_tray_status", { state, activeCount, approvalCount }); } catch { /* browser preview has no native tray */ }
  }

  async function refreshRuns() {
    try {
      const result = await bridge.request("list");
      const next = (result.data?.runs ?? []) as RunSummary[];
      setRuns(next);
      if (!activeRun && next[0]) setActiveRun(next[0].run_id);
      await syncTrayStatus(next);
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
  }

  async function refreshUsage(runId: string | null = activeRun) {
    if (!runId) { setUsage(null); return; }
    try { setUsage((await bridge.request("usage", { run_id: runId })).data as unknown as Usage); } catch { /* no usage before first model event */ }
  }

  async function connectBridge(workspace = cwd): Promise<boolean> {
    const target = workspace.trim();
    if (!target) {
      setConnection("等待选择工作区"); setError("请先在左上角“设置”中填写项目目录"); setSettingsOpen(true); return false;
    }
    if (bridgeConnected && target === cwd.trim()) { await refreshRuns(); return true; }
    if (runtimeStarted) { setError("运行进行中不能切换工作区；请先完成或停止当前运行"); return false; }
    setError(null); setConnection("正在连接本地 sidecar");
    try {
      const hello = await bridge.restart(target);
      setCwd(target); setConnection(`本地 sidecar 已连接 · ${String(hello.data?.connection ?? "ready")}`); setBridgeConnected(true);
      setModel(String(hello.data?.model ?? "未启动")); setRuntimeStarted(Boolean(hello.data?.runtime_started)); await refreshRuns(); return true;
    } catch (reason) {
      setConnection("sidecar 未连接"); setBridgeConnected(false); setError(reason instanceof Error ? reason.message : String(reason)); return false;
    }
  }

  async function openSettings() {
    setSettingsOpen(true); setSettingsError(null); setSettings((previous) => ({ ...previous, workspace: cwd }));
    if (!cwd.trim()) return;
    setSettingsLoading(true);
    try {
      if (!bridgeConnected && !(await connectBridge(cwd))) return;
      const config = (await bridge.request("config", { action: "get" })).data as unknown as WorkspaceConfig;
      setSettings({ workspace: config.workspace || cwd, base_url: config.base_url || "", api_key: "", model: config.model || "", has_api_key: Boolean(config.has_api_key), api_key_masked: config.api_key_masked ?? null });
    } catch (reason) { setSettingsError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setSettingsLoading(false); }
  }

  async function saveSettings() {
    const workspace = settings.workspace.trim();
    if (!workspace || !settings.base_url.trim() || !settings.model.trim()) { setSettingsError("项目目录、base_url 和模型名称都不能为空"); return; }
    if (runtimeStarted) { setSettingsError("当前运行已启动，不能在运行中切换模型配置"); return; }
    setSettingsSaving(true); setSettingsError(null);
    try {
      if (!bridgeConnected || workspace !== cwd.trim()) { if (!(await connectBridge(workspace))) return; }
      const saved = (await bridge.request("config", { action: "save", base_url: settings.base_url.trim(), api_key: settings.api_key, model: settings.model.trim() })).data as unknown as WorkspaceConfig;
      setCwd(workspace); setModel(saved.model || settings.model); setConnection("配置已保存，等待启动");
      setSettings((previous) => ({ ...previous, workspace, api_key: "", has_api_key: Boolean(saved.has_api_key), api_key_masked: saved.api_key_masked ?? null }));
      setSettingsOpen(false); setError(null);
    } catch (reason) { setSettingsError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setSettingsSaving(false); }
  }

  async function checkForUpdates() {
    if (updateState === "checking" || updateState === "installing") return;
    setError(null); setUpdateState("checking"); setUpdateMessage("正在检查更新…");
    try {
      const update = await check({ timeout: 30_000 });
      if (!update) { setUpdateState("latest"); setUpdateMessage(`当前已是最新版本（v${appVersion}）`); return; }
      setUpdateState("installing"); setUpdateMessage(`发现 v${update.version}，正在下载并安装…`);
      let downloaded = 0; let contentLength: number | undefined;
      await update.downloadAndInstall((event: DownloadEvent) => {
        if (event.event === "Started") { contentLength = event.data.contentLength; downloaded = 0; }
        else if (event.event === "Progress") downloaded += event.data.chunkLength;
        if (contentLength) setUpdateMessage(`正在更新… ${Math.min(100, Math.round((downloaded / contentLength) * 100))}%`);
      });
      setUpdateMessage("更新已安装，正在重启应用…"); await relaunch();
    } catch (reason) { const message = reason instanceof Error ? reason.message : String(reason); setUpdateState("error"); setUpdateMessage("更新失败"); setError(`更新失败：${message}`); }
  }

  useEffect(() => {
    let unlistenQuit: UnlistenFn | undefined; let unlistenOpen: UnlistenFn | undefined; let unlistenUpdate: UnlistenFn | undefined;
    void getVersion().then(setAppVersion).catch(() => setAppVersion("未知"));
    // Deliberately wait for a project path; connecting to the app directory is misleading.
    void (async () => {
      unlistenOpen = await listen("tray://open", () => void getCurrentWindow().show());
      unlistenUpdate = await listen("tray://update", () => void checkForUpdates());
      unlistenQuit = await listen("tray://quit", async () => {
        try {
          const result = await bridge.request("shutdown", { confirm: false }); const active = (result.data?.active_runs ?? []) as Array<{ run_id: string }>;
          if (result.data?.requires_confirmation && active.length > 0 && !window.confirm(`仍有 ${active.length} 个任务运行，确认退出并停止它们吗？`)) return;
          await bridge.request("shutdown", { confirm: true }); await invoke("exit_desktop");
        } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
      });
    })();
    const dispose = bridge.onMessage((message) => { if (message.message_type === "event") { setEvents((previous) => [...previous.slice(-199), message]); void refreshRuns(); void refreshUsage(message.run_id ?? null); } });
    return () => { dispose(); void unlistenOpen?.(); void unlistenUpdate?.(); void unlistenQuit?.(); };
  }, []);

  useEffect(() => {
    if (!activeRun || !bridgeConnected) return;
    setEvents([]); void refreshUsage(activeRun); void bridge.request("watch", { run_id: activeRun, watch_id: "foreground" });
  }, [activeRun, bridgeConnected]);

  async function submit() {
    const objective = composer.trim(); if (!objective) return; setError(null);
    if (!cwd.trim()) { setSettingsOpen(true); setSettingsError("请先配置项目目录和模型连接"); return; }
    setComposer("");
    try {
      if (!bridgeConnected && !(await connectBridge(cwd))) return;
      const result = await bridge.request("submit", { objective, auto_start: true }); const runId = String(result.data?.run_id ?? "");
      setRuntimeStarted(Boolean(result.data?.runtime_started)); setModel(String(result.data?.model ?? model)); setConnection(String(result.data?.connection ?? connection)); setActiveRun(runId); await refreshRuns();
    } catch (reason) { setComposer(objective); setError(reason instanceof Error ? reason.message : String(reason)); }
  }

  async function stop() { if (!activeRun) return; try { await bridge.request("interrupt", { run_id: activeRun, reason: "desktop user interrupt" }); await refreshRuns(); } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); } }
  async function continueRun() { if (!activeRun) return; try { await bridge.request("resume", { run_id: activeRun, auto_start: true }); setRuntimeStarted(true); await refreshRuns(); } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); } }
  async function hideWindow() { await getCurrentWindow().hide(); }

  const activeCount = runs.filter((run) => ["queued", "running", "awaiting_approval"].includes(run.status)).length;
  return (
    <main className="shell">
      <header className="titlebar"><div className="brand-group"><div className="brand"><span className="brand-mark">✦</span><span>cc-harness</span></div><button className="icon-button" aria-label="打开设置" title="打开设置" onClick={() => void openSettings()}>⚙</button></div><div className="title-actions">{updateMessage && <span className={`update-message update-${updateState}`}>{updateMessage}</span>}<button className="quiet-button" disabled={updateState === "checking" || updateState === "installing"} onClick={() => void checkForUpdates()}>{updateState === "checking" ? "检查中…" : updateState === "installing" ? "更新中…" : "检查更新"}</button><button className="quiet-button" onClick={hideWindow}>隐藏到托盘</button></div></header>
      <section className="workspace">
        <aside className="sidebar panel"><div className="sidebar-topline"><span className="eyebrow">工作区</span><span className={`connection-chip ${bridgeConnected ? "is-live" : ""}`}>{bridgeConnected ? "已连接" : "未连接"}</span></div><button className="new-session" onClick={() => inputRef.current?.focus()}><span>＋</span> 新会话 <kbd>⌘ K</kbd></button><div className="workspace-card"><div className="workspace-card-head"><span className="workspace-icon">⌂</span><strong>{cwd ? cwd.split(/[\\/]/).pop() : "未选择项目"}</strong><button className="mini-button" onClick={() => void openSettings()} aria-label="编辑工作区">···</button></div><div className="workspace-path" title={cwd}>{cwd || "点击左上角设置开始"}</div></div><div className="panel-heading runs-heading"><span>会话 / 运行</span><span className="count">{runs.length}</span></div><div className="run-list">{runs.length === 0 && <div className="empty">还没有运行。发送第一条任务开始。</div>}{runs.map((run) => <button className={`run-item ${run.run_id === activeRun ? "selected" : ""}`} key={run.run_id} onClick={() => setActiveRun(run.run_id)}><span className={`status-dot status-${run.status}`} /><span className="run-copy"><strong>{run.run_id.slice(0, 8)}</strong><small>{run.status} · seq {run.sequence}{run.storage_error ? " · 存储需修复" : ""}</small></span></button>)}</div><div className="sidebar-footer"><button onClick={() => void openSettings()}>⚙ 工作区设置</button><span>v{appVersion}</span></div></aside>
        <section className="transcript panel"><div className="conversation-head"><div><span className="eyebrow">{current ? "当前运行" : "本地 Agent 工作台"}</span><strong>{current ? `运行 ${current.run_id.slice(0, 8)}` : "开始一个新任务"}</strong></div><span className="conversation-state">{current?.status ?? "等待输入"}</span></div><div className="stream">{!activeRun && <div className="welcome"><div className="welcome-orbit"><span>✦</span></div><span className="eyebrow">CC-HARNESS RUNTIME</span><h1>把复杂工作交给一个<br /><em>可追踪的本地 Agent。</em></h1><p>任务、审批、事件和真实 API 用量都在同一个工作区里持续记录。关闭窗口不会停止任务。</p><div className="suggestions">{suggestions.map((item) => <button key={item} onClick={() => { setComposer(item); inputRef.current?.focus(); }}>{item}<span>↗</span></button>)}</div></div>}{events.map((item, index) => <article className="event-card" key={`${item.run_id}-${item.sequence}-${index}`}><div className="event-meta"><span className="event-badge">{item.event_type}</span><span>#{item.sequence}</span></div><pre>{JSON.stringify(item.payload ?? {}, null, 2)}</pre></article>)}</div><div className="composer-wrap"><div className="composer-shell"><textarea ref={inputRef} value={composer} onChange={(event) => setComposer(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); void submit(); } }} placeholder="描述要完成的任务…" /><div className="composer-actions"><span className="hint">内部提示词和凭据不会在界面展示 · Ctrl/⌘ + Enter 发送</span><button className="primary" onClick={() => void submit()}>发送 <span>↗</span></button></div></div></div></section>
        <aside className="context panel"><div className="context-head"><div><span className="eyebrow">实时投影</span><strong>上下文</strong></div><span className="readonly">只读</span></div><div className="context-section"><h3>审批</h3><p>待审批动作会显示在这里，并沿用现有策略。</p></div><div className="context-section"><h3>任务状态</h3><p className="value-line"><span className={`status-dot status-${current?.status ?? "idle"}`} />{current ? `${current.status}${current.storage_error ? "（持久化投影不一致）" : ""}` : "暂无活动任务"}</p></div><div className="context-section"><h3>实时用量</h3><p>{usage ? `${usage.input_tokens} 输入 · ${usage.output_tokens} 输出` : "等待运行事件"}</p><p>{usage ? `缓存命中 ${(usage.cache_hit_ratio * 100).toFixed(1)}%` : "缓存命中 —"}</p><p className="cost-line">{usage?.cost_status === "reported" ? `API 直接费用：${usage.reported_cost} ${usage.reported_cost_currency ?? ""}` : "API 直接费用：unavailable"}</p></div>{error && <div className="error-box"><strong>需要处理</strong><span>{error}</span><button onClick={() => void connectBridge(cwd)}>重试连接</button></div>}</aside>
      </section>
      <footer className="statusbar"><span><i className="status-dot status-running" />模型：{model}</span><span>运行：{runtimeStarted ? "已启动" : "未启动"}</span><span>连接：{connection}</span><span>权限：沿用 policy.yaml</span><span>活动：{activeCount}</span><span className="status-spacer" /><span>Token：{usage ? `${usage.input_tokens}/${usage.output_tokens}` : "—"}</span><span>费用：{usage?.cost_status === "reported" ? `${usage.reported_cost} ${usage.reported_cost_currency ?? ""}` : "unavailable"}</span>{activeRun && <button onClick={() => void continueRun()}>继续</button>}{activeRun && <button className="danger" onClick={() => void stop()}>停止</button>}</footer>
      {settingsOpen && <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !settingsSaving) setSettingsOpen(false); }}><section className="settings-modal" role="dialog" aria-modal="true" aria-labelledby="settings-title"><header className="modal-head"><div><span className="eyebrow">工作区设置</span><h2 id="settings-title">连接你的模型</h2><p>配置保存在项目目录的 .env 中，API key 不会回显到界面。</p></div><button className="icon-button" aria-label="关闭设置" onClick={() => setSettingsOpen(false)}>×</button></header><form onSubmit={(event) => { event.preventDefault(); void saveSettings(); }}><label>项目目录<input value={settings.workspace} onChange={(event) => setSettings((previous) => ({ ...previous, workspace: event.target.value }))} placeholder="D:\path\to\your-project" /></label><label>base_url<input value={settings.base_url} onChange={(event) => setSettings((previous) => ({ ...previous, base_url: event.target.value }))} placeholder="https://api.example.com/v1" /></label><label>模型名称<input value={settings.model} onChange={(event) => setSettings((previous) => ({ ...previous, model: event.target.value }))} placeholder="deepseek-v4-flash" /></label><label>API key<input type="password" autoComplete="off" value={settings.api_key} onChange={(event) => setSettings((previous) => ({ ...previous, api_key: event.target.value }))} placeholder={settings.has_api_key ? `已保存 ${settings.api_key_masked ?? "（留空保留）"}` : "sk-…"} /></label>{settingsLoading && <div className="form-note">正在读取当前工作区配置…</div>}{settingsError && <div className="form-error">{settingsError}</div>}<div className="modal-actions"><button type="button" className="quiet-button" disabled={settingsSaving} onClick={() => setSettingsOpen(false)}>取消</button><button type="submit" className="primary" disabled={settingsSaving || settingsLoading}>{settingsSaving ? "保存中…" : "保存配置"}</button></div></form></section></div>}
    </main>
  );
}

export { App };
