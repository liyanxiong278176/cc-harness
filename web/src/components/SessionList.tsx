import { useEffect } from 'react';
import { useSessionStore } from '../store/session';
import { listSessions, createSession, deleteSession } from '../api/client';

export function SessionList() {
  const { sessions, setSessions, setCurrent, currentSessionId } = useSessionStore();

  useEffect(() => {
    const refresh = () => listSessions().then(setSessions).catch(console.error);
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [setSessions]);

  const onCreate = async () => {
    const cwd = prompt('cwd:', '/tmp');
    if (!cwd) return;
    const meta = await createSession(cwd, 'coding');
    setSessions([...sessions, meta]);
    setCurrent(meta.session_id);
  };

  const onDelete = async (sid: string) => {
    if (!confirm('Delete session?')) return;
    await deleteSession(sid);
    setSessions(sessions.filter((s) => s.session_id !== sid));
    if (currentSessionId === sid) setCurrent(null);
  };

  return (
    <div className="p-2 flex flex-col gap-2">
      <button
        onClick={onCreate}
        className="px-3 py-2 bg-blue-500 text-white rounded hover:bg-blue-600"
      >
        + New Session
      </button>
      <ul className="flex flex-col gap-1">
        {sessions.map((s) => (
          <li
            key={s.session_id}
            className={`p-2 rounded cursor-pointer flex justify-between items-center ${
              currentSessionId === s.session_id ? 'bg-gray-200' : 'hover:bg-gray-100'
            }`}
            onClick={() => setCurrent(s.session_id)}
          >
            <span className="truncate text-sm">{s.cwd}</span>
            <button
              onClick={(e) => { e.stopPropagation(); onDelete(s.session_id); }}
              className="text-red-500 text-xs"
            >
              ✕
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
