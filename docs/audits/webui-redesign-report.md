# Claude 桌面风格 WebUI 改造验收报告

日期：2026-09-30  
分支：`feat/claude-style-webui`

## 结论

当前实现完成了双主题视觉、空状态建议卡片、内存态流式 reducer、独立 reasoning UI，以及现有 Runtime 到 SSE 的增量桥接。Windows 浏览器中已查看两种主题，并用真实 provider 完成了纯文本回合；直连 SSE 收到 3 个 content 增量、1 个 `done`、1 个 `AssistantMessageCommitted` 和 1 个 `RunOutcomeRecorded`。对应提交答案在 WebUI 时间线中可见。

这不是原始任务的全量验收通过：没有完成右侧自动 Artifact 面板、完整工具卡片交互、按日期分组、完整目录拆分、虚拟列表，也没有实际跑完工具、停止、审批、断线与长会话 8 条浏览器 E2E。真实 provider 这轮没有输出 reasoning delta，因此独立 thinking UI 由 reducer 单测覆盖，未由真实 reasoning 流截图证明。

另有一项硬约束冲突：现有 worker 会把 `reasoning_content` 写进 assistant-message artifact，工具回放依赖它；provider 无 content 时还会将 reasoning 用作最终正文兜底。此次只增加易失 live `reasoning` 包络，未更改该 Runtime 消息契约，因此“reasoning 原文在整个系统绝不持久化”仍未满足。证据见 [`worker.py`](../../cc_harness/worker.py#L1920)、[`interaction_history.py`](../../cc_harness/interaction_history.py#L144)、[`llm.py`](../../cc_harness/llm.py#L499)。

## 任务矩阵

| 编号 | 实现摘要 | 证据（文件:行） | 验证方式与结果 |
|---|---|---|---|
| 0 | 切换到 `feat/claude-style-webui`；本报告、决策记录和流式协议文档落在指定路径 | `docs/audits/webui-redesign-decisions.md`；本文；`docs/design/webui-streaming.md` | Windows/PowerShell 环境；任务未完成全量验收，见下方缺项。提交编号在最终交付摘要中列出。 |
| 1 | 对照本机 Claude 桌面端采集亮/暗、空状态、Artifacts、快捷键和外观参考截图；使用项目自有标记 | `docs/audits/screenshots/claude-ref/`；[`WelcomeSuggestions.tsx`](../../web/src/components/chat/WelcomeSuggestions.tsx#L31) | 8 张参考截图；没有复制 Claude 源码、字体、logo 或插画。 |
| 2 | 用 CSS 变量实现采样所得的亮/暗色板；聊天正文层级、圆形发送按钮和聚焦状态更新 | [`styles.css`](../../web/src/styles.css#L1)、[`styles.css`](../../web/src/styles.css#L27)、[`styles.css`](../../web/src/styles.css#L185)、[`styles.css`](../../web/src/styles.css#L281) | Windows Chrome 实际查看双主题；最终截图 `01-dark-welcome.png`、`02-light-welcome.png`。完整动效、焦点陷阱、对比度 AA 未逐项验收。 |
| 3a | 保留左侧项目/会话导航、搜索、状态点与项目折叠；分组维度仍是项目 | [`app.tsx`](../../web/src/cc/app.tsx#L2971)、[`app.tsx`](../../web/src/cc/app.tsx#L2980) | 浏览器可见；缺少按今天/过去 7 天/月的会话日期分组。公开 session projection 提供序号但无更新时间字段（[`webui.py`](../../cc_harness/webui.py#L1148)），不以序号伪造日期。 |
| 3b | 消息居中、用户气泡、assistant 无底色、Markdown 消毒、发送输入框固定在底部 | [`app.tsx`](../../web/src/cc/app.tsx#L1134)、[`StreamingMessage.tsx`](../../web/src/components/chat/StreamingMessage.tsx#L9)、[`styles.css`](../../web/src/styles.css#L246) | 最终实机回合截图 `03-real-model-stream.png`；复制可见。赞/踩、分支、重新生成操作未补齐。 |
| 3c | textarea 单行起步并自动增高；Enter/Shift+Enter 沿用现有快捷键；发送与停止操作走既有 API | [`app.tsx`](../../web/src/cc/app.tsx#L1739)、[`app.tsx`](../../web/src/cc/app.tsx#L2637)、[`app.tsx`](../../web/src/cc/app.tsx#L3016) | 代码和界面已查看；无麦克风和通用附件交互；停止状态没有独立实机 E2E。 |
| 3d | 首轮空状态包含通用问候和 2×2 中文建议；建议项调用现有 composer prompt 流程 | [`WelcomeSuggestions.tsx`](../../web/src/components/chat/WelcomeSuggestions.tsx#L4)、[`app.tsx`](../../web/src/cc/app.tsx#L3019) | 暗/亮空状态截图；卡片位于输入框下方，以匹配实机 Claude 的视觉顺序。 |
| 3e | 保留产物页、文件列表与预览能力 | [`app.tsx`](../../web/src/cc/app.tsx#L1559)、[`app.tsx`](../../web/src/cc/app.tsx#L2991) | 没有验证产物回合；当前是独立“产物”视图，未实现生成代码后右侧自动滑出、多 tab、全屏/在文件中打开的完整流程。 |
| 3f | 保留已有设置、权限、Toast 与操作确认入口 | [`app.tsx`](../../web/src/cc/app.tsx#L822)、[`app.tsx`](../../web/src/cc/app.tsx#L2771) | 未执行完整的键盘可达、Escape、焦点陷阱及 Ctrl+/ 快捷键 E2E。设置分页和快捷键弹窗没有按原规格全量验收。 |
| 4a | LLM provider 的 reasoning/content/tool-call delta 进入独立 `StreamEvent`；worker 透传为带 run/segment/chunk 的内存包络 | [`llm.py`](../../cc_harness/llm.py#L470)、[`worker.py`](../../cc_harness/worker.py#L1039)、[`durable_runtime.py`](../../cc_harness/durable_runtime.py#L293) | Python 单测覆盖分离与顺序；真实 SSE 收到 3 content + done，但本轮未见 reasoning/tool delta。 |
| 4b | SSE 先登记 live 订阅，再从 Durable 事件树对账；保留 `Last-Event-ID` 对 Durable cursor 续读 | [`webui.py`](../../cc_harness/webui.py#L2476)、[`webui.py`](../../cc_harness/webui.py#L2481)、[`webui.py`](../../cc_harness/webui.py#L2501)、[`webui.py`](../../cc_harness/webui.py#L2584) | `tests/test_web_sse.py` 覆盖 Durable 事件先回放；真实直连 SSE 用 query cursor 观察一次消息提交。浏览器断线 3 秒、header 续传与刷新去重未实测；live history ring 未接入恢复回放。 |
| 4c | 前端在 rAF 合帧、按 chunk 去重；commit 之后以权威持久正文替换临时正文；gap 冻结临时前缀 | [`app.tsx`](../../web/src/cc/app.tsx#L1991)、[`app.tsx`](../../web/src/cc/app.tsx#L2011)、[`app.tsx`](../../web/src/cc/app.tsx#L2164)、[`streaming.ts`](../../web/src/state/streaming.ts#L49) | `web/tests/streaming.test.ts` 3 项通过；无 5000+ 事件性能、虚拟列表或滚动锚点实测。 |
| 4d | 独立 thinking 展示、正文到达后自动折叠、显示本地观测耗时；轻量 live 工具阶段卡片 | [`StreamingMessage.tsx`](../../web/src/components/chat/StreamingMessage.tsx#L12)、[`streaming.ts`](../../web/src/state/streaming.ts#L107) | reducer 与组件状态有单测/构建覆盖；秒数从浏览器首次收到 reasoning 到正文/终态的间隔计算，不是 provider 精确计算时间。刷新后没有“已思考”持久占位字段；真实 provider 未发 reasoning；工具开始/结果/退出码/长输出展开均未实机验收。 |
| 4e | 保留现有审批 digest、停止、检查点继续 API 接线 | [`app.tsx`](../../web/src/cc/app.tsx#L2637)、[`app.tsx`](../../web/src/cc/app.tsx#L2771)、[`app.tsx`](../../web/src/cc/app.tsx#L2796) | 未执行允许/拒绝、stop/retry 全链路；没有单轮重生成按钮。只读模型 prompt 回合未触发工具调用，所以不能算作工具 E2E。 |
| 5 | 抽离 `state/streaming.ts`、`StreamingMessage`、`WelcomeSuggestions`；静态托管查找路径未变 | [`web/src/state/streaming.ts`](../../web/src/state/streaming.ts#L30)、[`StreamingMessage.tsx`](../../web/src/components/chat/StreamingMessage.tsx#L12)、[`WelcomeSuggestions.tsx`](../../web/src/components/chat/WelcomeSuggestions.tsx#L31)、[`webui.py`](../../cc_harness/webui.py#L2171) | `npm run build` 与 `python scripts/build_webui.py` 均通过；FastAPI 静态包已重新生成并保留既有 `mascot.png`。整个 `app.tsx`、`styles.css` 尚未按指定目录拆分。 |

## 真实模型与浏览器记录

- 在 Windows 本机用单独临时 Runtime 数据根启动本仓库 FastAPI WebUI（3081）和 Vite（5174）；项目的 provider 配置从工作区 `.env` 读取，UI 配置与其他 3080 进程隔离。截图存到 `docs/audits/screenshots/final/`。
- 第一条纯文本 REST 请求在客户端 20 秒读取超时后，Runtime 最终状态仍显示 `completed`；随后对同一会话发起 follow-up 并在提交前连接 SSE。第二条回合由 SSE 收到 `FollowUpQueued`、`RunCreated`、`ModelInvocationStarted`，之后 3 个 `stream_delta(kind=content)`、1 个 `stream_delta(kind=done)`、`AssistantMessageCommitted` 与 `RunOutcomeRecorded`。WebUI 刷新后展示两条提交答案，见 `03-real-model-stream.png`。
- 另一次只读 `README.md` 的 coding prompt 完成了消息提交，但事件计数没有工具 delta、Action/Approval 事件；模型未按提示调用工具。因此它没有验证工具卡片或审批。
- `01-dark-welcome.png`、`02-light-welcome.png` 是项目欢迎空状态；`03-real-model-stream.png` 是真实模型提交结果在浏览器中的显示。它不是增量渲染过程截图，也不包含 thinking 面板。

## 8 条浏览器 E2E 矩阵

| 场景 | 结果 | 证据/原因 |
|---|---|---|
| ① 亮/暗双主题 | 完成 | `01-dark-welcome.png`、`02-light-welcome.png`。 |
| ② 简单问答增量、reasoning 独立与自动折叠 | 部分 | 真实 SSE 观察到内容增量及权威 commit；provider 未返回 reasoning，所以 reasoning 显示/折叠只由单测覆盖。 |
| ③ 两步工具任务，卡片实时出现/折叠/展开 | 未完成 | 真实模型只读提示未触发工具；工具结果参数树、退出码和有界结果 UI 未实现完整。 |
| ④ 中途停止、保留半截并可重新生成 | 未完成 | 没有发送/停止中的浏览器 E2E；重新生成入口未实现。 |
| ⑤ 代码/文档输出自动打开 Artifact | 未完成 | 当前产物页可浏览，但右侧自动滑出与多视图流程缺失。 |
| ⑥ 审批允许和拒绝 | 未完成 | 没有产生真实待审批动作；未触碰 digest 路径。 |
| ⑦ 断网 3 秒恢复、刷新后 Last-Event-ID 无重/丢 | 未完成 | 服务端代码保留 Durable cursor，单测覆盖 Durable 回放；没有真实浏览器断网/刷新测试，临时 live delta 本身不可重放。 |
| ⑧ 5000+ 事件长会话、虚拟滚动与回到底部 | 未完成 | 当前没有虚拟列表，也未准备此 fixture。 |

## 未完成项、影响和建议

| 未完成项 | 根因/影响 | 建议 |
|---|---|---|
| 全部 reasoning 原文不持久化 | `worker.py`/`interaction_history.py` 的既有 artifact 存 provider 字段用于工具重放；LLM 还有 reasoning-only fallback。刷新/重放可能读取该数据，不能满足原硬约束。 | 先定 Runtime 兼容策略：例如将 provider replay payload 与可见 assistant artifact 分层存储/加密或删改保留策略，再实现迁移、重放回归和数据处理测试；这会超出“保持 Runtime 语义不变”。 |
| Artifact 自动面板/多 tab/全屏/在文件中打开 | 当前产物采用独立工作区，未和 assistant 增量建立自动动作。 | 新增右面板状态和 artifact 投影接口契约，先覆盖路径边界、Markdown 消毒和打开工作区文件的浏览器验收。 |
| 工具结果细节卡片 | live 工具事件目前仅名称/索引提示；审批投影保持参数 digest/安全摘要。 | 使用 Durable action/result 投影补齐结果，不从 live delta 暴露原始工具参数；定义有界输出预览与文件路径动作。 |
| 停止/重试/重新生成的 E2E | 当前没有浏览器自动化；重新生成 API 也未定义为无副作用动作。 | 先明确可安全重试条件与副作用边界，分别测试 stop、resume 和 retry；不要直接重放可能已执行工具的回合。 |
| 断线和长会话 E2E | 无 5000+ fixture/虚拟化；EventSource 当前采用 Durable 续读，临时 delta 不保证补发。 | 加 Playwright 场景和性能阈值；用既有 Durable fixture 测 run/segment/chunk 去重及回到底部按钮。 |
| 完整源码分层 | `app.tsx`/`styles.css` 仍较大，新增只拆了增量 reducer、Thinking 消息和欢迎建议。 | 后续按消息、composer、sidebar、right-panel、common、api 模块逐步抽离，每步独立构建/截图回归。 |
| 日期分组与模型自动标题 | 当前公开 session 条目无可信 `updated_at`；标题沿用任务文本。 | 添加只读 public projection 时间字段前，确认 Runtime 已有时间定义/时区；标题生成需定义稳定且不覆盖用户手工标题。 |

## 门禁输出摘要

以下命令在完成最终源码修改后执行；以最终终端退出码为准。

| 命令 | 结果 |
|---|---|
| `cd web; npm run build` | PASS，exit 0；`tsc -b` 成功，Vite 处理 1572 modules。 |
| `cd web; npm test` | PASS，3/3 Node 原生 reducer 测试通过。 |
| `python -m pytest tests/runtime_rebuild -q` | PASS，exit 0；收集到的测试全部通过。 |
| `python -m pytest tests/test_live_stream.py tests/test_web_sse.py tests/test_llm.py -q` | PASS，exit 0；相关测试全部通过。 |
| `python -m pytest tests/test_webui.py -q` | PASS，exit 0；WebUI 路由回归通过。 |
| `ruff check cc_harness/llm.py cc_harness/worker.py cc_harness/durable_runtime.py cc_harness/webui.py cc_harness/run_store.py tests/test_live_stream.py tests/test_llm.py` | PASS，`All checks passed!`。 |
| `python scripts/build_webui.py` | PASS，exit 0；重建 FastAPI 静态 bundle，既有 `mascot.png` 保留。 |

## 截图索引

| 文件 | 内容 |
|---|---|
| `docs/audits/screenshots/claude-ref/01-claude-desktop.png` | Claude 桌面端参考截图 |
| `docs/audits/screenshots/claude-ref/02-artifacts.png` | Claude Artifacts 参考 |
| `docs/audits/screenshots/claude-ref/03-new-chat.png` | Claude 暗色空状态参考 |
| `docs/audits/screenshots/claude-ref/04-shortcuts.png` | Claude 快捷键参考 |
| `docs/audits/screenshots/claude-ref/05-appearance.png`、`06-general-settings.png` | Claude 外观设置参考 |
| `docs/audits/screenshots/claude-ref/07-light-theme.png`、`08-light-chat.png` | Claude 亮色参考 |
| `docs/audits/screenshots/final/01-dark-welcome.png` | 当前实现暗色欢迎页 |
| `docs/audits/screenshots/final/02-light-welcome.png` | 当前实现亮色欢迎页 |
| `docs/audits/screenshots/final/03-real-model-stream.png` | 真实模型提交结果的 WebUI 页面 |
