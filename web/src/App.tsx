// App.tsx — 三栏 layout,路由留空,后续 Task 19-22 填充
// 注:Routes/Route 当前未使用(noUnusedLocals 会报错),Task 19 接入路由时再 import
export default function App() {
  return (
    <div className="h-screen flex flex-col">
      <header className="border-b px-4 py-2 flex items-center gap-4">
        <h1 className="text-lg font-semibold">cc-harness</h1>
      </header>
      <main className="flex-1 flex">
        <aside className="w-64 border-r">{/* TODO: SessionList */}</aside>
        <section className="flex-1 flex flex-col">{/* TODO: Chat */}</section>
        <aside className="w-96 border-l">{/* TODO: FileTree + TerminalPane */}</aside>
      </main>
    </div>
  );
}
