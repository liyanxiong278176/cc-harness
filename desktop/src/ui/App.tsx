import { useEffect, useMemo, useRef, useState } from "react";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { invoke } from "@tauri-apps/api/core";
import { listen, UnlistenFn } from "@tauri-apps/api/event";
import { DesktopBridge, BridgeMessage } from "../bridge";

type RunSummary = {
  run_id: string;
  status: string;
  sequence: number;
  storage_error?: boolean;
};
type Usage = {
  input_tokens: number;
  output_tokens: number;
  cache_read_input_tokens: number;
  cache_hit_ratio: number;
  reported_cost: number | null;
  reported_cost_currency: string | null;
  cost_status: string;
};

const bridge = new DesktopBridge();

function App() {
  const [cwd, setCwd] = useState(".");
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [activeRun, setActiveRun] = useState<string | null>(null);
  const [composer, setComposer] = useState("");
  const [events, setEvents] = useState<BridgeMessage[]>([]);
  const [runtimeStarted, setRuntimeStarted] = useState(false);
  const [model, setModel] = useState("未启动");
  const [connection, setConnection] = useState("未连接");
  const [error, setError] = useState<string | null>(null);
  const [usage, setUsage] = useState<Usage | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const current = useMemo(
    () => runs.find((run) => run.run_id === activeRun) ?? null,
    [activeRun, runs],
  );

  async function refreshRuns() {
    try {
      const result = await bridge.request("list");
      const next = (result.data?.runs ?? []) as RunSummary[];
      setRuns(next);
      if (!activeRun && next[0]) setActiveRun(next[0].run_id);
      await syncTrayStatus(next);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  async function syncTrayStatus(nextRuns: RunSummary[]) {
    const activeCount = nextRuns.filter((run) =>
      ["queued", "running", "awaiting_approval", "cancel_requested", "stalled"].includes(run.status),
    ).length;
    const approvalCount = nextRuns.filter((run) => run.status === "awaiting_approval").length;
    const state = nextRuns.some((run) => run.status === "stalled" || run.status === "failed_terminal")
      ? "attention"
      : approvalCount > 0
        ? "approval"
        : activeCount > 0
          ? "active"
          : nextRuns.some((run) => run.status === "completed")
            ? "completed"
            : "idle";
    try {
      await invoke("update_tray_status", { state, activeCount, approvalCount });
    } catch {
      // Browser/Vite preview has no native tray; the desktop build does.
    }
  }

  async function refreshUsage(runId: string | null = activeRun) {
    if (!runId) {
      setUsage(null);
      return;
    }
    try {
      const result = await bridge.request("usage", { run_id: runId });
      setUsage(result.data as unknown as Usage);
    } catch {
      // A new run has no usage events yet; keep the last safe display.
    }
  }

  useEffect(() => {
    let unlistenQuit: UnlistenFn | undefined;
    let unlistenOpen: UnlistenFn | undefined;
    void (async () => {
      try {
        const hello = await bridge.start(cwd);
        setConnection(String(hello.data?.connection ?? "本地 sidecar 已连接"));
        setModel(String(hello.data?.model ?? "未启动"));
        setRuntimeStarted(Boolean(hello.data?.runtime_started));
        await refreshRuns();
      } catch (reason) {
        setConnection("等待本地 sidecar");
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    })();
    void (async () => {
      unlistenOpen = await listen("tray://open", () => void getCurrentWindow().show());
      unlistenQuit = await listen("tray://quit", async () => {
        try {
          const result = await bridge.request("shutdown", { confirm: false });
          const active = (result.data?.active_runs ?? []) as Array<{ run_id: string }>;
          if (result.data?.requires_confirmation && active.length > 0) {
            const shouldExit = window.confirm(`仍有 ${active.length} 个任务运行，确认退出并停止它们吗？`);
            if (!shouldExit) return;
          }
          await bridge.request("shutdown", { confirm: true });
          await invoke("exit_desktop");
        } catch (reason) {
          setError(reason instanceof Error ? reason.message : String(reason));
        }
      });
    })();
    const dispose = bridge.onMessage((message) => {
      if (message.message_type === "event") {
        setEvents((previous) => [...previous.slice(-199), message]);
        void refreshRuns();
        void refreshUsage(message.run_id ?? null);
      }
    });
    return () => {
      dispose();
      void unlistenOpen?.();
      void unlistenQuit?.();
    };
  }, []);

  useEffect(() => {
    if (!activeRun) return;
    setEvents([]);
    void refreshUsage(activeRun);
    void bridge.request("watch", { run_id: activeRun, watch_id: "foreground" });
  }, [activeRun]);

  async function submit() {
    const objective = composer.trim();
    if (!objective) return;
    setComposer("");
    setError(null);
    try {
      const result = await bridge.request("submit", {
        objective,
        auto_start: true,
      });
      const runId = String(result.data?.run_id ?? "");
      setRuntimeStarted(Boolean(result.data?.runtime_started));
      setModel(String(result.data?.model ?? "未启动"));
      setConnection(String(result.data?.connection ?? connection));
      setActiveRun(runId);
      await refreshRuns();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  async function stop() {
    if (!activeRun) return;
    try {
      await bridge.request("interrupt", { run_id: activeRun, reason: "desktop user interrupt" });
      await refreshRuns();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  async function continueRun() {
    if (!activeRun) return;
    try {
      await bridge.request("resume", { run_id: activeRun, auto_start: true });
      setRuntimeStarted(true);
      await refreshRuns();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }

  async function hideWindow() {
    await getCurrentWindow().hide();
  }

  return (
    <main className="shell">
      <header className="titlebar">
        <div className="brand"><span className="brand-dot" />cc-harness</div>
        <div className="title-actions"><button onClick={hideWindow}>隐藏到托盘</button></div>
      </header>
      <section className="workspace">
        <aside className="sidebar panel">
          <div className="panel-heading"><span>工作区</span><button>＋</button></div>
          <input className="workspace-input" value={cwd} onChange={(event) => setCwd(event.target.value)} />
          <div className="panel-heading runs-heading"><span>会话 / 运行</span><span className="count">{runs.length}</span></div>
          <div className="run-list">
            {runs.length === 0 && <div className="empty">还没有运行。发送第一条任务开始。</div>}
            {runs.map((run) => (
              <button
                className={`run-item ${run.run_id === activeRun ? "selected" : ""}`}
                key={run.run_id}
                onClick={() => setActiveRun(run.run_id)}
              >
                <span className={`status-dot status-${run.status}`} />
                <span className="run-copy"><strong>{run.run_id.slice(0, 8)}</strong><small>{run.status} · seq {run.sequence}{run.storage_error ? " · 存储需修复" : ""}</small></span>
              </button>
            ))}
          </div>
        </aside>
        <section className="transcript panel">
          <div className="panel-heading"><span>{current ? `运行 ${current.run_id.slice(0, 8)}` : "新会话"}</span><span className="muted">{current?.status ?? "等待输入"}</span></div>
          <div className="stream">
            {!activeRun && <div className="welcome"><h1>本地 Agent 工作台</h1><p>共享现有 cc-harness Runtime。关闭窗口不会停止任务，托盘退出才会退出应用。</p></div>}
            {events.map((item, index) => (
              <article className="event-card" key={`${item.run_id}-${item.sequence}-${index}`}>
                <div className="event-meta">#{item.sequence} · {item.event_type}</div>
                <pre>{JSON.stringify(item.payload ?? {}, null, 2)}</pre>
              </article>
            ))}
          </div>
          <div className="composer-wrap">
            <textarea
              ref={inputRef}
              value={composer}
              onChange={(event) => setComposer(event.target.value)}
              onKeyDown={(event) => { if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); void submit(); } }}
              placeholder="描述要完成的任务，Ctrl/⌘ + Enter 发送"
            />
            <div className="composer-actions"><span className="hint">内部提示词和凭据不会在界面展示</span><button className="primary" onClick={() => void submit()}>发送</button></div>
          </div>
        </section>
        <aside className="context panel">
          <div className="panel-heading"><span>上下文</span><span className="muted">只读投影</span></div>
          <div className="context-section"><h3>审批</h3><p>待审批动作会显示在这里，并沿用现有策略。</p></div>
          <div className="context-section"><h3>任务状态</h3><p>{current ? `${current.status}${current.storage_error ? "（持久化投影不一致，未自动修改）" : ""}` : "暂无活动任务"}</p></div>
          <div className="context-section"><h3>实时用量</h3><p>{usage ? `${usage.input_tokens} 输入 · ${usage.output_tokens} 输出 · 缓存命中 ${(usage.cache_hit_ratio * 100).toFixed(1)}%` : "等待运行事件"}</p><p>{usage?.cost_status === "reported" ? `API 直接费用：${usage.reported_cost} ${usage.reported_cost_currency ?? ""}` : "API 直接费用：unavailable"}</p></div>
          {error && <div className="error-box">{error}</div>}
        </aside>
      </section>
      <footer className="statusbar">
        <span><i className="status-dot status-running" />模型：{model}</span>
        <span>运行：{runtimeStarted ? "已启动" : "未启动"}</span>
        <span>连接：{connection}</span>
        <span>权限：沿用 policy.yaml</span>
        <span>活动：{runs.filter((run) => ["queued", "running", "awaiting_approval"].includes(run.status)).length}</span>
        <span>Token：{usage ? `${usage.input_tokens}/${usage.output_tokens}` : "—"}</span>
        <span>费用：{usage?.cost_status === "reported" ? `${usage.reported_cost} ${usage.reported_cost_currency ?? ""}` : "unavailable"}</span>
        <span className="status-spacer" />
        {activeRun && <button onClick={() => void continueRun()}>继续</button>}
        {activeRun && <button className="danger" onClick={() => void stop()}>停止</button>}
      </footer>
    </main>
  );
}

export { App };
