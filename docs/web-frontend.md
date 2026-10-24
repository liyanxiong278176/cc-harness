# cc-harness Web 前端

2026-10-24 起，前端以 DeepSeek Harness 的界面布局、样式与多轮对话流程为参考重新实现。旧的 `web/src/cc/app.tsx` 工作台已删除，新实现位于 `web/src/app/`，后端仍为 `cc_harness/webui.py`。

参考截图与逐项对比见 [`docs/design/screenshots/2026-10-24/`](design/screenshots/2026-10-24/)（`side-by-side.png` 为同视口 1650×894 CSS @1.25 DPR 的并排对比）。Runtime 故障扫描、修复与验证见 [`docs/design/2026-10-24-runtime-evaluation.md`](design/2026-10-24-runtime-evaluation.md)。

## 启动与构建

```powershell
# 后端（当前 main.py 的 TUI/CLI 入口不再提供 --serve，使用 WebUI 启动函数）
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -c "import asyncio; from pathlib import Path; from cc_harness.webui import run_web_server; asyncio.run(run_web_server(initial_cwd=Path.cwd(), port=3080, no_open=True))"

# 前端开发模式
cd web; npm run dev

# 构建并同步到 Python 包（发布前必须执行）
python scripts/build_webui.py
```

Vite 默认把 `/api` 代理到 `http://127.0.0.1:3080`，可用 `VITE_API_PROXY_TARGET` 修改。

## 页面结构

- 三列外层简化为「280px 侧栏 + 主列」，窄屏（≤1024px）自动收成 56px 图标栏；侧栏可手动收起。
- 主列头部 76px：会话标题、`标准模式`、运行状态、后台任务数，以及「对话 / 轨迹」两个 13px 选项卡。
- 对话内容列 `clamp(680px, 60%, 920px)`，用户气泡 22px 圆角、右对齐；助手正文 14/24px，Markdown 全宽。
- 每一轮用户消息把过程与最终回答分开：过程（工具、思考、通知、错误）折叠为「执行过程 · N 步」，最终助手回答始终可见；最新一轮运行中默认展开较短的执行过程。
- 输入卡 22px 圆角，底部工具行包含工作区、模式、权限、模型、发送/停止；Enter 发送、Shift+Enter 换行。
- 输入卡上方按需显示「任务列表」（只跟随 Runtime task-control 层 `task_projection.todos`，标题与状态随 `TodoCreated/Updated/Completed` 实时更新）和审批卡。
- 新建会话为居中 Hero：鱼形标识、标题、工作区选择与输入卡。
- 深/浅主题共享语义 token：暗色背景 `#151517`、侧栏 `#1b1b1c`、气泡/输入卡 `#2c2c2e`、主文字 `#f9fafb`。

## 数据与联调

- 初次加载只请求 `/api/bootstrap?include_sessions=false`，侧栏会话列表独立加载，不再阻塞输入卡。
- 会话正文使用 `GET /api/sessions/{run_id}/chat?limit=1200`：服务端只读取聊天相关事件（用户/助手/工具/计划/审批），并聚合 Run 树中的 `todos` 与 `plan.nodes` 用于对话历史；任务列表单独使用 `task_projection`（effective/current Run 的 Runtime task-control 层），不会把整棵会话树的历史 todo 混进当前任务。
- SSE 使用 `GET /api/sessions/{run_id}/events?after=<root_seq>&cursors=<json>`。`cursors` 按 Run 传入每个子 Run 的已知序列，避免切换会话或重连时从 0 重放整棵树。
- 临时增量走 `stream` 事件（`content` / `reasoning` / `tool_call_delta` / `done`）。durable `AssistantMessageCommitted` 到达后会清掉对应 Run 的临时段，并忽略迟到的增量，避免重复文本和完成协议 JSON 泄露。
- 发送消息先本地插入用户气泡，再 POST；POST 只写入 durable 队列即返回，supervisor 的启动放到后台任务，输入区不再等待模型/工具初始化。
- 运行中的普通消息进入会话根节点持久 FIFO；审批、停止、恢复仍走原有控制接口。

新增/变更接口同时提供 `/api` 与 `/api/web/v1` 别名：

| 接口 | 用途 |
| --- | --- |
| `GET /sessions/{run_id}/chat?limit=1200` | 聊天页专用的轻量快照 + 最近事件 + 每 Run cursors |
| `GET /sessions/{run_id}?view=chat` | 会话状态/投影，跳过 usage/context 的全量事件解码 |
| `GET /sessions/{run_id}/events?after=&cursors=` | 带每 Run 游标的 SSE，避免子树重放 |
| `GET /sessions/{run_id}/changes` | 文件变化与差异 |
| `PATCH /sessions/{run_id}/queue/{follow_up_id}` | 更新未认领消息 |
| `DELETE /sessions/{run_id}/queue/{follow_up_id}` | 撤回未认领消息 |

## 性能措施

- 项目发现扫描 30s 缓存；侧栏摘要 2.5s 缓存；聊天快照按 Run 序列签名缓存 5s。
- 工具观察/助手消息 artifact 使用有界 LRU 缓存（仅对真实 durable store 启用）。
- `RunWorker` 的 supervisor 不再在 POST 请求路径内同步初始化；失主进程的 supervisor lease 会通过 owner pid / 15s 心跳判定快速回收。
- 前端 `sessionStorage` 缓存最近一次会话快照，刷新时先渲染旧内容再后台对账；切换已加载过的会话直接命中内存缓存。
- Chat 只渲染最近 40 轮（可「加载更早的轮次」），每轮过程默认折叠；流式内容与 durable 事件分离，40ms 合并窗口内不会整页重渲染历史 Markdown。

## 验证

```powershell
# 前端 reducer 测试 + 构建
cd web; npm test; npm run build

# 后端 WebUI / store / lease / supervisor / SSE 测试
python -m pytest tests/test_webui.py tests/runtime_rebuild/test_run_store.py tests/runtime_rebuild/test_leases.py tests/runtime_rebuild/test_supervisor.py tests/test_live_stream.py tests/test_stream_errors.py -q
```

2026-10-24 实测（本机，1650×894 CSS）：

- 刷新：59ms 出现会话骨架，387–852ms 出现首条助手内容；sessionStorage 命中时 69ms。
- 切换会话：41ms 更新标题，680ms 出现内容。
- 发送：乐观气泡 16–148ms；POST 109–321ms；模型首字/完成取决于 provider。
- 多轮：2 用户 + 2 助手交替，无重复/原始完成 JSON。
- 计划：`task_plan` 运行中约 6s 出现 3 个步骤，随后 pending → in_progress → done；刷新后仍保留 3 个步骤及完成状态。

真实供应商的认证、网络和流式输出仍需在实际模型配置下验证。
