import { useEffect, useState } from 'react';
import { ModeBadge } from './components/ModeBadge';
import { SessionList } from './components/SessionList';
import { Chat } from './components/Chat';
import { FileTree } from './components/FileTree';
import { CodeViewer } from './components/CodeViewer';
import { useSessionStore } from './store/session';

export default function App() {
  const [filePath, setFilePath] = useState<string | null>(null);
  const sid = useSessionStore((s) => s.currentSessionId);
  // session 切换 → 清 filePath,避免 CodeViewer 显示前 session 的文件内容
  useEffect(() => {
    setFilePath(null);
  }, [sid]);
  return (
    <div className="h-screen flex flex-col">
      <header className="border-b px-4 py-2 flex items-center gap-4">
        <h1 className="text-lg font-semibold">cc-harness</h1>
        <ModeBadge />
      </header>
      <main className="flex-1 flex">
        <aside className="w-64 border-r overflow-y-auto"><SessionList /></aside>
        <section className="flex-1"><Chat /></section>
        <aside className="w-96 border-l flex flex-col">
          <div className="h-1/3 border-b overflow-y-auto">
            {sid && <FileTree sessionId={sid} onSelect={setFilePath} />}
          </div>
          <div className="h-2/3">
            {sid && filePath && <CodeViewer sessionId={sid} path={filePath} />}
          </div>
        </aside>
      </main>
    </div>
  );
}