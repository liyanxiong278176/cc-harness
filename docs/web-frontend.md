# cc-harness Web 前端开发手册

## 开发模式

```bash
# Terminal 1:启 FastAPI 后端
PYTHONIOENCODING=utf-8 python main.py --serve --port 8765

# Terminal 2:启 Vite dev
cd web && npm install && npm run dev
```

浏览器:http://localhost:5173(Vite proxy `/api` + `/ws` → 8765)

### Smoke checklist (manual)

需要真 LLM + 浏览器。2 terminal:

- [ ] Terminal 1: `cd D:/agent_learning/cc-harness && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe main.py --serve --port 8765`
- [ ] Terminal 2: `cd web && npm run dev`
- [ ] 浏览器 `http://localhost:5173`
- [ ] SessionList 渲染(初次可能空)
- [ ] 点 "+ New Session" 输入 cwd=/tmp 创建 → 出现在 list
- [ ] 点 session → Chat 出现,WS 连上(`?v=1` 后端 OK 应见空 4-phase 输出)
- [ ] 发消息 → 收到 thought / action / observation / result 流
- [ ] 切 Files tab → 列 /tmp → 点 .py 文件 → Monaco 只读渲染
- [ ] 切 Terminal tab → 当前 MVP 占位 `test-pty-id` server 会 `WS_1008` 关,见 `[disconnected]`;生产 wiring 见 TODO

## 生产构建

```bash
cd web && npm run build
python main.py --serve --port 8765 --static-dir web/dist
```

FastAPI `StaticFiles` mount `/`,SPA 路由 fallback 到 `index.html`。

## 事件协议

见 `web/src/api/types.ts` ↔ `cc_harness/web/events.py`。

破坏性变更:升 `PROTOCOL_VERSION` major。前端硬编码最低版本,不匹配 → WS 拒绝。

## 防御层映射

| 层 | 触发 | 前端展示 |
|---|---|---|
| L2 命中 | `l2_refused` 事件 | 黄色 toast,模糊拒绝模板(沿 CLAUDE.md M2) |
| L4 ask | `l4_ask` 事件 | 黄底决策条(Yes / Always / No),30s 无响应 → no |
| L5 命中 | L5 内部统计,前端收脱敏版 | 红色 toast("已脱敏 N 项") |
| L8 沙箱 | 透传到 executor | 前端无感 |

## 多 session

- SessionList 5s polling(`/api/sessions`)
- 新建 → 后端落 `web_session` SQLite 行,FK cascade
- 删除 → cancel task + 等 5s + 落 checkpoint + 删行
- 重启进程 → `SessionManager.restore_from_checkpoint()` 重建

## PTY

后端 `PTYManager` spawn bash(可配 SHELL 环境变量);WS `/ws/pty/{pty_id}` 双向。前端 `TerminalPane` 用 xterm.js + FitAddon。Windows 上 `pywinpty` 留 TODO(spec §5.2 延后)。

## 故障排查

| 现象 | 检查 |
|---|---|
| Vite proxy 失败 | `8765` 后端是否起;`.env` 是否缺 key |
| WS 立即 close | 检查 URL 是否带 `?v=1`,或后端日志 /api/health 的 PROTOCOL_VERSION |
| `aiosqlite` teardown hang(Windows) | 用 `--junit-xml` + `pkill -9`(沿项目惯例) |
| Monaco 不显示 | 检查 `dist/monaco-editor/` 是否被 Vite 排除(默认包含) |
| xterm 输入乱码 | 检查 base64 编解码是否正确 |

## 已知限制 / 后续跟进(Known limitations / follow-ups)

- **PTY 是占位**:`TerminalPane` 当前用的是硬编码 `test-pty-id`,后端会立刻 `WS_1008` 关。
  生产 wiring 需要加 `POST /api/sessions/{sid}/pty` endpoint(指向 `cc_harness/web/pty.py:30 PTYManager.create()`),
  然后前端在切到 Terminal tab 时先调 POST 拿 `pty_id` 再连 WS。
- **L2 / L5 事件未透传到前端 store**:`app.state.l2` / `app.state.l5` 默认 `None`,
  故 `session_run_loop` 内部跳过这些层。follow-up 需扩展 `RuntimeContext`,
  把这两层接入 session 事件流(emit `l2_refused` / `l5_redacted`),从而让前端 toast / 状态条真正生效。
- **Monaco worker bundle 首次 build 慢**:`npm run build` 首次会拉 Monaco 的 worker 文件(~10s);
  Vite `split-chunks` 自动拆包,与主 chunk 不冲突,prod build 后续无影响。
- **Session 5s polling**:当前 `/api/sessions` 是 5s 轮询(简单可靠,无 WS push 复杂度)。
  后续如果 session 数多或对实时性有要求,可升级到 SSE / WS 推送。

## 后续 sub-project

| 名称 | 范围 | spec |
|---|---|---|
| web-auth | token + cookie + RBAC | 留待写 |
| web-deploy | Docker + nginx + systemd | 留待写 |
| web-sessions-advanced | session 间记忆共享 | 留待写 |
| web-mobile | 响应式 + 移动端 layout | 留待写 |

不在本 plan 范围,每个独立 brainstorm → spec → plan → 实施。
