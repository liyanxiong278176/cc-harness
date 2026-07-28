import { useEffect, useRef } from 'react';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';

export function TerminalPane({ sessionId }: { sessionId: string }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const term = new Terminal({ cols: 80, rows: 24, fontSize: 13 });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(containerRef.current);
    fit.fit();
    termRef.current = term;

    // WS 连 PTY(后端 Task 16 完整实现;MVP 这里先用 echo 桥占位)
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = window.location.host;
    // TODO: replace test-pty-id with session-scoped PTY mint (requires POST /api/sessions/{sid}/pty endpoint; cc_harness/web/pty.py:30 already has PTYManager.create()). Until then, the WS connection will close immediately on the server (test-pty-id not registered) and the user will see only "[disconnected]" in the terminal.
    const ws = new WebSocket(`${proto}//${host}/ws/pty/test-pty-id`);
    wsRef.current = ws;
    ws.onopen = () => term.writeln('\r\n[connected]\r\n');
    ws.onclose = () => term.writeln('\r\n[disconnected]\r\n');
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.type === 'stdout') {
          term.write(atob(msg.data));
        }
      } catch {}
    };
    term.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'stdin', data: btoa(data) }));
      }
    });

    return () => {
      term.dispose();
      ws.close();
    };
  }, [sessionId]);

  return <div ref={containerRef} className="h-full w-full bg-black" />;
}
