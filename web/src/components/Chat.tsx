import { useEffect, useRef, useState } from 'react';
import { useSessionStore } from '../store/session';
import { openChatWS, sendEvent, parseServerEvent } from '../api/client';
import type { ServerEvent } from '../api/types';

export function Chat() {
  const sid = useSessionStore((s) => s.currentSessionId);
  const messages = useSessionStore((s) => (sid ? s.messages[sid] ?? [] : []));
  const append = useSessionStore((s) => s.appendMessage);
  const setPendingAsk = useSessionStore((s) => s.setPendingAsk);
  const pendingAsk = useSessionStore((s) => s.pendingAsk);
  const [input, setInput] = useState('');
  const wsRef = useRef<WebSocket | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!sid) return;
    const ws = openChatWS(sid);
    wsRef.current = ws;
    ws.onmessage = (e) => {
      const data = parseServerEvent(e.data);
      if (!data) return;
      const ev = data as ServerEvent;
      if (ev.type === 'thought' || ev.type === 'action' || ev.type === 'observation'
          || ev.type === 'result' || ev.type === 'error') {
        append(sid, { type: ev.type, data: ev });
      }
      if (ev.type === 'l4_ask') {
        setPendingAsk({ ask_id: ev.ask_id, question: ev.question });
      }
    };
    return () => ws.close();
  }, [sid, append, setPendingAsk]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const send = () => {
    if (!wsRef.current || !input.trim()) return;
    sendEvent(wsRef.current, { type: 'user_input', text: input });
    setInput('');
  };

  const respondAsk = (decision: 'yes' | 'always' | 'no') => {
    if (!wsRef.current || !pendingAsk) return;
    sendEvent(wsRef.current, {
      type: 'l4_response',
      ask_id: pendingAsk.ask_id,
      decision,
    });
    setPendingAsk(null);
  };

  return (
    <div className="flex flex-col h-full">
      <div className="flex-1 overflow-y-auto p-4 space-y-2 font-mono text-sm">
        {messages.map((m, i) => (
          <div key={i} className="border-l-2 border-gray-300 pl-3">
            {m.data.type === 'thought' && (
              <p className="text-gray-600">思考: {m.data.text}</p>
            )}
            {m.data.type === 'action' && (
              <p className="text-blue-700">
                行动: {m.data.name}({JSON.stringify(m.data.args)})
              </p>
            )}
            {m.data.type === 'observation' && (
              <pre className={`whitespace-pre-wrap ${m.data.is_error ? 'text-red-700' : 'text-green-700'}`}>
                观察: {m.data.text}
              </pre>
            )}
            {m.data.type === 'result' && (
              <p className="text-purple-700 font-semibold">结果: {m.data.text}</p>
            )}
            {m.data.type === 'error' && (
              <p className={m.data.fatal ? 'text-red-900 font-bold' : 'text-orange-700'}>
                错误: {m.data.message}
              </p>
            )}
          </div>
        ))}
        <div ref={endRef} />
      </div>

      {pendingAsk && (
        <div className="border-t p-3 bg-yellow-50 flex items-center gap-2">
          <span className="text-sm">{pendingAsk.question}</span>
          <button onClick={() => respondAsk('yes')} className="px-3 py-1 bg-green-500 text-white rounded">Yes</button>
          <button onClick={() => respondAsk('always')} className="px-3 py-1 bg-blue-500 text-white rounded">Always</button>
          <button onClick={() => respondAsk('no')} className="px-3 py-1 bg-red-500 text-white rounded">No</button>
        </div>
      )}

      <div className="border-t p-3 flex gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && send()}
          className="flex-1 border rounded px-3 py-2"
          placeholder="输入消息..."
        />
        <button onClick={send} className="px-4 py-2 bg-blue-500 text-white rounded">发送</button>
      </div>
    </div>
  );
}
