import { useSessionStore } from '../store/session';

export function ModeBadge() {
  const sid = useSessionStore((s) => s.currentSessionId);
  const sessions = useSessionStore((s) => s.sessions);
  const mode = sessions.find((m) => m.session_id === sid)?.mode ?? 'coding';
  return (
    <span className="px-2 py-1 rounded bg-blue-100 text-blue-800 text-xs font-mono">
      [{mode}]
    </span>
  );
}
