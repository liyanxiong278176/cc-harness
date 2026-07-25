# Codex 风格 Web UI — 设计文档

**Date:** 2026-07-25
**Status:** Draft (brainstorming → writing-plans → implementation)
**Author:** brainstorming session
**Target cc-harness branch:** master (post code-review-fixes, commit 06b6005)

---

## 0. 与真实 Codex 的关系

OpenAI 官方 Codex **CLI 是 Rust 二进制**,集成在 ChatGPT 里的 **Codex Web** 是闭源产品(没有开源前端栈可参考)。

**"Codex 风格 Web UI"** 这个描述,社区常见实现是 `codex-webui` 这类项目:Node 后端 spawn Codex CLI 子进程走 stdio JSON-RPC,前端用 React + Monaco + xterm + Socket.IO。

**本 spec 不复刻 OpenAI Codex 的实现细节,而是套用社区分层思路(React 前端 + WebSocket 流 + 桥接 agent runtime),但保留 cc-harness 的 Python in-process 架构**:

- **不用 stdio JSON-RPC 桥接**(避免子进程状态序列化、boot 3s 延迟、跨进程防御层断裂)—— 与方案 B 排除理由一致
- **后端选 FastAPI 而不是 NestJS/Express**(沿用 cc-harness 已有 Python 技术栈)
- **前端选 React + Vite + shadcn/ui**(社区最熟,资料最多)
- **PTY 桥放在 `tools.run_command` 内部**(`use_pty=True` 路径),不开新进程层

| 维度 | OpenAI Codex CLI | 社区 codex-webui | **本 spec(cc-harness Web UI)** |
|---|---|---|---|
| 核心 runtime | Rust 二进制 | Node 桥接 + Rust 子进程 | **Python in-process(沿 cc-harness)** |
| 前后端通信 | (Web UI 是闭源) | WebSocket + stdio JSON-RPC | **WebSocket SSE-style JSON** |
| 文件查看 | (Web UI 是闭源) | Monaco 可编辑 | **Monaco 只读(LLM 主写)** |
| 终端 | (Web UI 是闭源) | xterm.js 可选 | **xterm.js 双向 PTY** |
| 沙箱 | Linux 容器 | 透传到 CLI 沙箱 | **透传到 L8 沙箱(`policy.yaml.executor.backend`)** |
| Memory | AGENTS.md | 透传到 CLI | **沿 cc-harness 现有 MemoryService** |

---

## 1. 目标

构建一个类似 Codex 的 **Web 端交互界面**,让用户通过浏览器驱动 cc-harness 的 ReAct 编程 Agent。核心体验:

- 多 session 并存(同 cwd 多对话,跨 session 续接)
- 实时流式渲染 4 段输出(思考 / 行动 / 观察 / 结果)
- 文件树浏览 + Monaco 只读查看(LLM 改完自动同步)
- 嵌入 xterm.js 双向 PTY,可在浏览器里运行命令
- 防御层(L2/L4/L5/L8)**原样透传**,不做额外包装

不在范围:鉴权、多用户、协作编辑、复杂部署(留给后续 sub-project)。

---

## 2. 架构

### 2.1 进程边界

一个 Python 进程,启动模式二选一:

| 模式 | 命令 | 行为 |
|---|---|---|
| REPL(保留) | `python main.py` | 现有交互 REPL,完全不动 |
| Web Serve(新增) | `python main.py --serve --port 8765` | 启动 FastAPI + WebSocket,接管所有用户交互 |

`--serve` 与现有 `main.py:boot()` **共享**所有 wiring 逻辑(LLM / MCP / memory / scheduler / reflection / drift / checkpoint)。只在最外层 `run_repl` vs `run_serve` 分支。

### 2.2 组件图

```
┌──────────────────────────────────────────────────────────────┐
│ Browser (React + Vite + shadcn/ui + Monaco + xterm.js)       │
│   ├── Chat 组件       (4-段流式渲染)                          │
│   ├── FileTree 组件   (懒加载文件树)                          │
│   ├── CodeViewer 组件 (Monaco 只读 + 高亮 diff)               │
│   ├── TerminalPane 组件 (xterm.js 双向 PTY)                   │
│   └── SessionList 组件 (多 session 切换)                      │
└──────────────────────────────────────────────────────────────┘
          │ fetch              ▲ WebSocket (chat + PTY)
          ▼                    │
┌──────────────────────────────────────────────────────────────┐
│ FastAPI 单进程 (cc_harness/web/app.py)                       │
│                                                              │
│   ├── /api/sessions              GET/POST/DELETE             │
│   ├── /api/sessions/{sid}/files  GET (文件树,fs MCP)         │
│   ├── /api/sessions/{sid}/file   GET (Monaco 只读)           │
│   ├── /ws/{session_id}           WebSocket (chat 流)         │
│   └── /ws/pty/{session_id}       WebSocket (PTY 双向)        │
│                                                              │
│   SessionManager (in-memory dict + asyncio.Lock)             │
│     ├── per-session ReplState                                  │
│     ├── per-session asyncio.Task (run_turn loop)              │
│     └── 全局 _llm_lock (LLM 串行,避免 cc-harness 单进程撑爆)  │
└──────────────────────────────────────────────────────────────┘
          │
          ▼
┌──────────────────────────────────────────────────────────────┐
│ cc_harness 库(沿用,不改动防御层)                             │
│   ├── LLMClient + MCPClient + run_command (PTY 路径)         │
│   ├── agent.run_turn(增加 event_emitter 形参)                 │
│   ├── L2 输入扫毒 / L4 权限闸门 / L5 输出 DLP / L8 沙箱      │
│   ├── MemoryService / ReflectionEngine / DriftDetector       │
│   └── CheckpointService(扩展支持 Web 多 session 持久化)      │
└──────────────────────────────────────────────────────────────┘
```

---

## 3. 事件协议(ReAct → WebSocket JSON)

每个 user_message 触发的 ReAct 循环被 emit 为一系列 JSON 事件,通过 WebSocket 以 SSE-style 推送:

```
data: {"type":"thought","text":"...","ts":...,"iteration":1}\n\n
data: {"type":"action","name":"mcp__fs__read_file","args":{...},"ts":...,"iteration":1}\n\n
data: {"type":"observation","text":"...","is_error":false,"duration_ms":12,"iteration":1}\n\n
data: {"type":"action","name":"run_command","args":{"command":"pytest"},"ts":...,"iteration":2}\n\n
data: {"type":"observation","text":"...","duration_ms":1240,"is_error":false,"iteration":2}\n\n
data: {"type":"result","text":"...","ts":...}\n\n
data: {"type":"done","session_id":"...","turn_idx":3}\n\n
```

### 3.1 事件类型(后端 → 前端)

| 事件 | 字段 | 触发点 |
|---|---|---|
| `thought` | `text, ts, iteration` | agent 每次 LLM 迭代(LLM 流式文本完整缓冲后 emit) |
| `action` | `name, args, ts, iteration` | tool_call 派发前 |
| `observation` | `text, is_error, duration_ms, iteration` | tool_call 完成后 |
| `result` | `text, ts` | ReAct 循环结束(无 tool_call)的 LLM 最终回复 |
| `compaction` | `before, after, summary, tier` | Tier1/2/3 上下文压缩完成 |
| `l4_ask` | `ask_id, question, tool_name, args` | L4 闸门 ask 拦截 |
| `l5_redacted` | `count, types` | L5 命中(只记类型计数,不暴露明文) |
| `l2_refused` | `template` | L2 命中(沿用 REFUSAL_TEMPLATE) |
| `mode` | `value` | /mode 切换 |
| `slash_ack` | `command` | slash 命令已处理 |
| `file_changed` | `path, content` | LLM 改了 cwd 内文件(从 fs MCP / git diff 侦测) |
| `error` | `message, fatal` | 软/硬错误 |
| `done` | `session_id, turn_idx, duration_ms` | turn 结束 |

### 3.2 反向事件(前端 → 后端)

| 事件 | 字段 | 说明 |
|---|---|---|
| `user_input` | `text` | 用户发送消息 |
| `slash` | `command` | `/plan` / `/design` / `/coding` / `/chat` / `/clear` / `/mode` |
| `l4_response` | `ask_id, decision` | `yes` / `always` / `no`,回应 l4_ask |
| `interrupt` | (空) | 中断当前 turn(`asyncio.Task.cancel`) |

### 3.3 版本协商

- HTTP header `X-CC-Harness-Web-Version: 1`(当前为 `1`)
- WS 握手时校验,版本不匹配 → 403
- 协议破坏性变更 → 升 major,前端硬编码最低支持版本

---

## 4. 多 Session + 持久化

### 4.1 SessionManager(内存)

```python
class SessionManager:
    def __init__(self, llm, mcp_factory, checkpoint_service, max_sessions=8):
        self._sessions: dict[str, SessionRecord] = {}
        self._llm_lock = asyncio.Lock()  # 全局,LLM 串行
        self._max = max_sessions

    async def create(self, cwd: Path, mode: str) -> SessionRecord: ...
    async def delete(self, session_id: str) -> None: ...
    async def list(self) -> list[SessionMeta]: ...
    async def get(self, session_id: str) -> SessionRecord | None: ...
    async def push_event(self, session_id: str, event: dict) -> None: ...
    async def restore_from_checkpoint(self) -> None: ...
```

`SessionRecord`:
```python
@dataclass
class SessionRecord:
    session_id: str
    state: ReplState                  # 沿用现有 dataclass
    task: asyncio.Task                # run_turn loop
    event_queue: asyncio.Queue        # WS 推流用
    pty_sessions: dict[str, PTYRecord]
    created_at: float
    last_active_at: float
```

每个 session 内部用 **per-session lock**(`asyncio.Lock`),所有 `run_turn` 调用走 `_llm_lock + session_lock`(避免单 session 把 LLM 阻塞时其他 session 假死)。

### 4.2 持久化(SQLite 全状态)

沿用现有 `CheckpointService`(表 `session_checkpoint` / `session_message`),**扩展**加一张新表 `web_session`:

```sql
CREATE TABLE web_session (
    id            TEXT PRIMARY KEY,       -- UUID hex,前端用
    cwd           TEXT NOT NULL,
    mode          TEXT NOT NULL,
    created_at    REAL NOT NULL,
    last_active_at REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'closed' | 'errored'
    extra_json    TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (id) REFERENCES session_checkpoint(session_id) ON DELETE CASCADE
);
```

每 turn 末 `_persist_turn(session_id, turn_idx, messages)`:
- 调 `CheckpointService.persist_turn(...)`(已有,落 `session_checkpoint` + `session_message`)
- 更新 `web_session.last_active_at`

启动时 `_restore_sessions()`:
- 读所有 `web_session.status='active'`
- 对每个 session:`rebuild ReplState` + `restore messages from session_message` + spawn task

**关键不变量**:Web UI DELETE session → `DELETE FROM web_session WHERE id=?` → FK cascade 清理 `session_checkpoint` + `session_message`。

### 4.3 资源上限

- `MAX_SESSIONS = 8`(环境变量 `CC_HARNESS_WEB_MAX_SESSIONS` 可配,默认 8)
- 超过 → 创建返回 422 + 当前活跃列表
- 单 session `messages` 上限沿用现有 `ContextConfig.compaction_trigger_tokens`,Web 不额外限

---

## 5. 改造点(对 cc-harness 现有代码)

### 5.1 `agent.run_turn` 加 `event_emitter`

```python
async def run_turn(
    ...,
    event_emitter: Callable[[dict], Awaitable[None]] | None = None,
) -> tuple[list[dict], ...]:
    ...
    if event_emitter:
        await event_emitter({"type":"thought","text":llm_text,"ts":...,"iteration":i})
    for tool_call in tool_calls:
        if event_emitter:
            await event_emitter({"type":"action","name":..., "args":...})
        result = await dispatch(...)
        if event_emitter:
            await event_emitter({"type":"observation","text":..., "is_error":..., "duration_ms":...})
    ...
```

`event_emitter=None` 时保持现有 REPL 行为完全不变(沿用 `rich.console.print` 输出)。

### 5.2 `tools.run_command` 加 PTY 路径

新增形参 `use_pty: bool = False`:

```python
async def run_command(command: str, *, use_pty: bool = False, ...):
    if use_pty:
        # Linux/macOS:pty.openpty() + os.read/write
        # Windows:winpty (pip dependency,可选)
        ...
    else:
        # 现有 asyncio subprocess 路径,完全不变
        ...
```

PTY 路径下不返回完整 stdout,而是**持续 push** 到 `pty_writer: Callable[[bytes], Awaitable[None]]`。Web PTY WS 走这条路径。现有 REPL 调用 `use_pty=False`,完全不变。

### 5.3 `repl.run_repl` 抽出公共 boot

把 `main.py:boot()` 里 `cfg + llm + mcp + memory + scheduler + reflection + drift + checkpoint` 的 wiring 抽到 `cc_harness/web/boot.py:build_runtime()`,`run_repl` 和新的 `run_serve` 都调用它。

### 5.4 `l4_ask` 推前端

`PolicyEngine` 现有 ask 路径走 `_read_user()`(input())。**不直接改 PolicyEngine**,而是在 `SessionManager` 这一层包装:把 `_read_user` 替换为"发 `l4_ask` 事件 + 等 WS 回 `l4_response`"。REPL 路径不变(仍走 input())。

### 5.6 lifespan 启动序列

`FastAPI lifespan` 顺序(沿用 `main.py:boot()` 现有顺序,只为 `--serve` 加收尾):

1. `build_runtime()` → cfg + llm + mcp + memory deps + scheduler + reflection + drift + checkpoint
2. `SessionManager.restore_from_checkpoint()`(遍历 `web_session.status='active'`,rebuild ReplState + restore messages + spawn task)
3. yield(应用可用)
4. shutdown:遍历所有 session → cancel task + 等 5s → 落最终 checkpoint → close 所有 WS → mcp.shutdown()

### 5.5 `l2` / `l5` 透传

- L2:`SessionManager.create_user_message(text)` 先调 `l2.scan_user_input`,命中 → 发 `l2_refused` 事件,**不入 messages**(沿 CLAUDE.md M2 设计)
- L5:`agent.run_turn` 在 emit `thought` / `result` 之前过 `l5.scan`(沿 CLAUDE.md M3 设计),前端收到的也是脱敏版;`observation` 不扫(沿 M3)

---

## 6. WebSocket / HTTP 路由

### 6.1 HTTP

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/sessions` | 列出所有 session 元数据(无 messages) |
| POST | `/api/sessions` | body `{cwd, mode}`,创建并返回 `session_id` |
| GET | `/api/sessions/{sid}` | 单 session 元数据 |
| DELETE | `/api/sessions/{sid}` | cancel task + 落 checkpoint + 从 dict 删 |
| POST | `/api/sessions/{sid}/mode` | body `{mode}`,切换 sticky mode |
| GET | `/api/sessions/{sid}/files?path=.` | 文件树,fs MCP `list_directory` |
| GET | `/api/sessions/{sid}/file?path=...` | 单文件内容,>200KB 拒绝 |
| GET | `/api/health` | `{status:"ok", version, session_count}` |

### 6.2 WebSocket

- `WS /ws/{session_id}` — chat 流(双向 §3.2 / §3.1)
- `WS /ws/pty/{pty_id}` — PTY 双向(只 `stdin` / `stdout` / `exit`,**不**走 §3.2 的 `user_input` / `slash` / `interrupt` / `l4_response` —— PTY 是独立子协议,不经 agent.run_turn)

WS 帧格式:JSON object,失败帧 → 服务端发 `{"type":"error","message":"...","fatal":true}` 后 close。

---

## 7. 前端

### 7.1 技术栈

- **Vite + React 18 + TypeScript**
- **shadcn/ui**(Radix + Tailwind)组件库
- **Zustand** 全局 session 状态
- **Monaco Editor** 文件查看(`@monaco-editor/react`)
- **xterm.js + addon-fit** 终端模拟(`@xterm/xterm`)

### 7.2 路由

- `/` → SessionList(默认进首个 active session)
- `/s/:sessionId` → Chat 主界面(三栏:文件树 / chat / 终端)
- `/s/:sessionId/files` → 单独文件树视图
- `/s/:sessionId/files/:path` → 单独 Monaco 视图

### 7.3 关键组件

| 组件 | 职责 | 关键 props |
|---|---|---|
| `<Chat>` | 渲染 4 段流式输出 | `sessionId`, `messages`, `onSend`, `onSlash` |
| `<SessionList>` | 列 sessions + 新建/删除 | `sessions`, `onCreate`, `onDelete` |
| `<FileTree>` | 懒加载文件树 | `sessionId`, `onSelect(path)` |
| `<CodeViewer>` | Monaco 只读 | `path`, `content`, `language` |
| `<TerminalPane>` | xterm.js 双向 | `sessionId`, `ptyId` |

### 7.4 状态管理(Zustand)

```ts
// store/session.ts
interface SessionStore {
  currentSessionId: string | null;
  sessions: Record<string, SessionMeta>;
  messages: Record<string, Message[]>;  // session_id → 4-段渲染条目
  pendingAsk: L4Ask | null;             // 当前等用户决策的 l4_ask
  // actions: setCurrent, addMessage, appendStream, setAsk, resolveAsk, ...
}
```

### 7.5 开发代理

Vite `vite.config.ts`:
```ts
server: {
  proxy: {
    "/api": "http://localhost:8765",
    "/ws": { target: "ws://localhost:8765", ws: true },
  }
}
```

---

## 8. PTY 桥(双向)

### 8.1 后端

```python
# cc_harness/web/pty.py
class PTYSession:
    pty_id: str
    session_id: str       # 关联的 chat session
    master_fd: int
    proc: asyncio.subprocess.Process
    writer: Callable[[bytes], Awaitable[None]]  # 推到 WS

async def create_pty(session_id: str, cwd: Path) -> PTYSession: ...
async def write_stdin(pty_id: str, data: bytes) -> None: ...
async def close_pty(pty_id: str) -> None: ...
```

- **Linux/macOS**:`pty.openpty()` + `os.read(master_fd, N)` + `asyncio.create_subprocess_exec(..., stdin=master_fd, stdout=master_fd)`
- **Windows**:`winpty` 包(`pip install pywinpty`),fork `cmd.exe` with cwd

### 8.2 前端

`<TerminalPane>` 持 `term: Terminal` + `ws: WebSocket`:

```ts
term.onData((data) => ws.send(JSON.stringify({type:"stdin", data: dataBase64(data)})));
ws.onmessage = (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.type === "stdout") term.write(base64Decode(msg.data));
  else if (msg.type === "exit") term.write(`\r\n[process exited ${msg.code}]\r\n`);
};
```

PTY 不入 agent `messages`(沿 CLAUDE.md M3:"工具观察段不扫"),纯附属功能。

---

## 9. 错误处理

### 9.1 错误分级

| 级别 | 触发 | 处理 |
|---|---|---|
| **fatal** | MCP 启动失败 / LLM 4xx auth / OOM | `error{fatal:true}` → 关闭 WS + `DELETE /api/sessions/{id}` + 前端跳回 SessionList |
| **soft** | 单 tool 失败 / LLM 5xx 重试 3 次仍失败 | `error{fatal:false}` → session 继续,前端黄色 toast |
| **L2 命中** | `scan_user_input` 触发 | `l2_refused` 事件,不入 messages,前端显示 REFUSAL_TEMPLATE |
| **L4 ask** | PolicyEngine ask 拦截 | `l4_ask` 事件,30s 无前端响应 → 视为 `no`(沿现有 fail-soft) |
| **PTY 断开** | 子进程退出 / WS 关闭 | `pty_closed` 事件,前端 term 显示 `[closed]`,后端清理 PTYSession |

### 9.2 进程生命周期

- `CTRL+C` / SIGTERM:`app.lifespan` shutdown 钩子 → 遍历所有 session → cancel task + 等 5s → 落最终 checkpoint → close WS → exit
- WS 异常断开:`SessionManager._watch_ws` task 检 connection_lost,5s 内未重连 → cancel session task + 标记 `status='closed'`(保留可恢复)

---

## 10. 测试

| 文件 | 类型 | 说明 |
|---|---|---|
| `tests/web/test_events.py` | 单测 | 事件 pydantic schema + 序列化 round-trip |
| `tests/web/test_session_manager.py` | 单测 | FakeLLM + asyncio,create/delete/list/restore |
| `tests/web/test_routes.py` | 单测 | FastAPI `TestClient`,HTTP 路由 |
| `tests/web/test_ws.py` | 单测 | `TestClient.websocket_connect`,事件流 round-trip |
| `tests/web/test_pty.py` | 单测(macOS/Linux only) | spawn 真 PTY,echo 测试 |
| `tests/test_web_integration.py` | `_test_*.py` 前缀 | 真 LLM + 全链路 |

新依赖(加 `pyproject.toml`):
- `fastapi>=0.110`
- `uvicorn[standard]>=0.27`
- `websockets>=12`
- `pywinpty>=2.0; sys_platform == 'win32'`(可选)

新依赖(前端 `web/package.json`):
- `react`, `react-dom`, `react-router-dom`
- `@monaco-editor/react`
- `@xterm/xterm`, `@xterm/addon-fit`
- `zustand`
- shadcn/ui 全套(`tailwindcss` + `@radix-ui/react-*`)
- `vite`, `typescript`

---

## 11. 部署

### 11.1 开发

```bash
# Terminal 1:后端
python main.py --serve --port 8765

# Terminal 2:前端
cd web && npm install && npm run dev   # http://localhost:5173
```

Vite dev server 代理 `/api` + `/ws` 到 `8765`。

### 11.2 生产(单进程)

```bash
cd web && npm run build  # 出 web/dist/
python main.py --serve --port 8765 --static-dir web/dist
```

FastAPI `StaticFiles` mount `/`,所有非 `/api` `/ws` 请求 fallback 到 `index.html`(SPA)。

### 11.3 不在范围

- ❌ Docker / k8s / TLS 终止 / 反向代理(nginx / caddy)
- ❌ systemd / Windows Service
- ❌ 性能压测 / 负载均衡

(留给后续 `web-deploy` sub-project。)

---

## 12. YAGNI 边界

**不做**(明确留给后续):
- 鉴权 / 多用户 / RBAC / SSO
- 实时协作(多用户编辑同一 session)
- 移动端响应式(只做桌面 ≥1280px)
- 会话间记忆共享差异化(沿用 cc-harness 现有 memory 装配,Web 不额外干预)
- 多 LLM 后端切换(沿 OpenAI-compatible)

**最小但必须做**:
- ✅ CORS(dev 模式允许 5173)
- ✅ 单进程优雅关闭
- ✅ WebSocket 心跳(ping/pong 30s)
- ✅ session 列表 polling(`GET /api/sessions` 每 5s 拉一次,不搞 SSE)

---

## 13. 与现有 CLAUDE.md 防御层的对应

| 层 | 现有机制 | Web 边界处理 |
|---|---|---|
| **L2 输入扫毒** | `cc_harness.l2.scan_user_input`(repl.py:进 run_turn 前) | 沿用:`SessionManager.create_user_message` 入口过 L2,命中 → 发 `l2_refused` 不入 messages |
| **L4 权限闸门** | `cc_harness.policy.PolicyEngine`(allow/ask) | `PolicyEngine._read_user` 在 Web 路径下替换为"发 l4_ask + 等 l4_response"(REPL 路径不变) |
| **L5 输出 DLP** | `cc_harness.l5` 扫 LLM 输出 | 沿用:`agent.run_turn` emit thought/result 前过 L5,前端收脱敏版 |
| **L8 沙箱** | `cc_harness.executor.SandboxExecutor` + `policy.yaml.executor.backend` | 完全透传,Web 不感知 |
| **审计** | `<root>/logs/{policy,l2,l5,drift,cross_session}.jsonl` | 沿用,Web 不额外加 |

---

## 14. 后续 sub-project 候选

| Sub-project | 范围 |
|---|---|
| `web-auth` | token + cookie + RBAC(单用户扩展) |
| `web-collaboration` | 多用户编辑同一 session(CRDT / OT) |
| `web-deploy` | Docker / nginx / systemd / TLS |
| `web-mobile` | 响应式 + 移动端 layout |
| `web-sessions-advanced` | session 间记忆共享 / 模板化启动 |

每个都是独立 spec → plan → implement 循环,不在本 spec 范围。

---

## 15. 实施步骤概要(给 writing-plans skill 用)

1. 改造 `agent.run_turn` 加 `event_emitter` 形参(保留现有 REPL 行为)
2. 改造 `tools.run_command` 加 PTY 路径(Linux/macOS 优先,Windows 延后)
3. 抽出 `cc_harness/web/boot.py:build_runtime()`
4. 实现 `SessionManager` + 内存 dict + asyncio.Lock
5. 实现 FastAPI app + lifespan + 路由
6. 实现 WS 路由(chat + PTY)
7. 改造 `CheckpointService` 加 `web_session` 表 + restore
8. 前端 Vite 工程骨架 + 路由 + shadcn/ui 集成
9. 前端 `<Chat>` / `<FileTree>` / `<CodeViewer>` / `<TerminalPane>` 组件
10. 单测 + 集成测试 + `_test_*.py` E2E

预计 ~10-14 commit,符合 sub-project 历史节奏(参考 D-subagent 单层 fan-out 23 commit / +43 测试)。