import { ModeBadge } from './components/ModeBadge';
import { SessionList } from './components/SessionList';
import { Chat } from './components/Chat';

export default function App() {
  return (
    <div className="h-screen flex flex-col">
      <header className="border-b px-4 py-2 flex items-center gap-4">
        <h1 className="text-lg font-semibold">cc-harness</h1>
        <ModeBadge />
      </header>
      <main className="flex-1 flex">
        <aside className="w-64 border-r overflow-y-auto"><SessionList /></aside>
        <section className="flex-1"><Chat /></section>
        <aside className="w-96 border-l">{/* TODO: Task 21 + 22 */}</aside>
      </main>
    </div>
  );
}
