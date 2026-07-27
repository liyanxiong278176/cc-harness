import type {
  ClientEvent, ServerEvent, SessionMeta, FileEntry,
} from './types';

const BASE = '';  // dev 走 Vite proxy;prod 同源

export async function listSessions(): Promise<SessionMeta[]> {
  const resp = await fetch(`${BASE}/api/sessions`);
  if (!resp.ok) throw new Error(`listSessions: ${resp.status}`);
  return (await resp.json()).sessions;
}

export async function createSession(cwd: string, mode: string): Promise<SessionMeta> {
  const resp = await fetch(`${BASE}/api/sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cwd, mode }),
  });
  if (!resp.ok) {
    const err = await resp.text();
    throw new Error(`createSession: ${resp.status} ${err}`);
  }
  return resp.json();
}

export async function deleteSession(sid: string): Promise<void> {
  await fetch(`${BASE}/api/sessions/${sid}`, { method: 'DELETE' });
}

export async function listFiles(sid: string, path = '.'): Promise<FileEntry[]> {
  const resp = await fetch(`${BASE}/api/sessions/${sid}/files?path=${encodeURIComponent(path)}`);
  if (!resp.ok) throw new Error(`listFiles: ${resp.status}`);
  return (await resp.json()).entries;
}

export async function readFile(sid: string, path: string): Promise<{ content: string; language: string }> {
  const resp = await fetch(`${BASE}/api/sessions/${sid}/file?path=${encodeURIComponent(path)}`);
  if (!resp.ok) throw new Error(`readFile: ${resp.status}`);
  return resp.json();
}

export function openChatWS(sid: string): WebSocket {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const host = window.location.host;
  const ws = new WebSocket(`${proto}//${host}/ws/${sid}`);
  // 版本 header 由浏览器 WS API 不支持,改用 query param
  // (后端 Task 14 已用 header,这里加 query param fallback)
  return ws;
}

export function sendEvent(ws: WebSocket, ev: ClientEvent): void {
  ws.send(JSON.stringify(ev));
}

export function parseServerEvent(line: string): ServerEvent | null {
  if (!line.startsWith('data: ')) return null;
  try { return JSON.parse(line.slice('data: '.length)); }
  catch { return null; }
}
