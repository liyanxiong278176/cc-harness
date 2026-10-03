# Claude 桌面风格 WebUI 改造验收报告

日期：2026-09-30  
分支：`feat/claude-style-webui`

## 结论

本次交付完成了 Claude Desktop 参照式双主题 WebUI、SSE 内存增量通道、Durable 对账、Thinking/工具卡片/审批、Artifact 文件面板、多标签/全屏/安全文件打开、日期分组、虚拟长列表、停止和受限安全重试。真实 DeepSeek provider 与 Chromium 浏览器实际运行了文本、Glob/Read、Write 审批允许/拒绝、停止/重试与产物自动打开；另用 5,000 条历史事件夹具验证虚拟窗口和滚动行为，并用 3 秒网络离线验证浏览器发送 `Last-Event-ID`、服务端接续后刷新不重复。

这仍不是原始需求的全量实现。真实 provider 未返回 reasoning delta，因此 Thinking 的真实流式外观无法端到端截图证明；`ContinueToolResult` 只读取当前 Run 的观察记录，已结束 Run 后的新消息属于子 Run，Runtime 不接受跨 Run 续读；源码目录拆分只完成渐进抽离；附件/麦克风、点赞/踩、分支/重新生成、分享/导出、完整账号区与实时 token 速率等也未完成。各缺项及建议见“未完成项、影响和建议”。

Raw reasoning 的新增持久化路径已关闭：worker/旧 agent 会话投影不再写入该字段，assistant-message 构造器也不再接受它；LLM 遇到 reasoning-only 响应改为一次有限的 thinking-disabled 重试。为保持不可变事件与老会话可读，既有 artifact 不会被原地改写，历史 artifact 可能仍含旧字段。此向前兼容策略记录在 [ADR-0090](../adr/0090-keep-raw-model-reasoning-transient.md)；真实 provider reasoning delta 尚未观测到，因此端到端可见性仍未验证。

## 任务矩阵

| 编号 | 实现摘要 | 证据（文件:行） | 验证方式与结果 |
|---|---|---|---|
| 0 | 切换到 `feat/claude-style-webui`；本报告、决策记录和流式协议文档落在指定路径 | `docs/audits/webui-redesign-decisions.md`；本文；`docs/design/webui-streaming.md` | Windows/PowerShell 环境；任务未完成全量验收，见下方缺项。提交编号在最终交付摘要中列出。 |
| 1 | 对照本机 Claude 桌面端采集亮/暗、空状态、Artifacts、快捷键和外观参考截图；使用项目自有标记 | `docs/audits/screenshots/claude-ref/`；[`WelcomeSuggestions.tsx`](../../web/src/components/chat/WelcomeSuggestions.tsx#L31) | 8 张参考截图；没有复制 Claude 源码、字体、logo 或插画。 |
| 2 | 用 CSS 变量实现采样所得的亮/暗色板；聊天正文层级、圆形发送按钮和聚焦状态更新 | [`styles.css`](../../web/src/styles.css#L1)、[`styles.css`](../../web/src/styles.css#L27)、[`styles.css`](../../web/src/styles.css#L185)、[`styles.css`](../../web/src/styles.css#L281) | Windows Chrome 实际查看双主题；最终截图 `01-dark-welcome.png`、`02-light-welcome.png`。完整动效、焦点陷阱、对比度 AA 未逐项验收。 |
| 3a | 项目/会话搜索、状态点、项目折叠；以真实首末事件时间分组为今天/近 7 天/更早 | [`webui.py`](../../cc_harness/webui.py#L1264)、[`app.tsx`](../../web/src/cc/app.tsx#L470)、[`app.tsx`](../../web/src/cc/app.tsx#L3338) | `tests/test_webui.py::test_session_date_projection_reads_event_timestamps`；Windows 浏览器观察到日期组。日期以 Runtime 事件时间戳投影，不用 sequence 推断。 |
| 3b | 居中消息列、用户暖色气泡、assistant 无底色、自有 Sparkles 标记、marked + DOMPurify、复制操作 | [`app.tsx`](../../web/src/cc/app.tsx#L1326)、[`StreamingMessage.tsx`](../../web/src/components/chat/StreamingMessage.tsx#L12)、[`styles.css`](../../web/src/styles.css#L156) | 真实回答截图 `05-real-model-answer.png`；长列表截图 `09`/`10`。点赞/踩、分支和完整重新生成没有实现。 |
| 3c | 单行起步自动增高 composer、Enter/Shift+Enter、真实 stop API；无工具副作用时提供“重试本轮” | [`app.tsx`](../../web/src/cc/app.tsx#L2908)、[`app.tsx`](../../web/src/cc/app.tsx#L2993)、[`app.tsx`](../../web/src/cc/app.tsx#L3398) | Chromium + 真实 provider：看到部分输出→停止→保留前缀→同一请求安全重试并再次流式输出，截图 `11`–`13`。附件/麦克风仍未实现。 |
| 3d | 首轮空状态包含通用问候和 2×2 中文建议；建议项调用现有 composer prompt 流程 | [`WelcomeSuggestions.tsx`](../../web/src/components/chat/WelcomeSuggestions.tsx#L4)、[`app.tsx`](../../web/src/cc/app.tsx#L3019) | 暗/亮空状态截图；卡片位于输入框下方，以匹配实机 Claude 的视觉顺序。 |
| 3e | Artifact 项目文件列表、最多 8 个标签、全屏、复制、在本机默认程序打开；Markdown/HTML 消毒预览与安全 token 高亮代码视图 | [`app.tsx`](../../web/src/cc/app.tsx#L1679)、[`app.tsx`](../../web/src/cc/app.tsx#L1795)、[`SyntaxCode.tsx`](../../web/src/components/right-panel/SyntaxCode.tsx#L1)、[`webui.py`](../../cc_harness/webui.py#L2345) | 真实获批 Write 后右栏自动打开，截图 `16`；API 路径测试通过。历史观察回合的文件在截图后已删除。模拟文件列表验证多 tab/全屏；本机外部程序启动仅由 API 路径边界测试覆盖。 |
| 3f | 设置与快捷键弹窗支持 Ctrl+/、Ctrl+,、Escape、首焦点与 Tab 陷阱 | [`app.tsx`](../../web/src/cc/app.tsx#L849)、[`app.tsx`](../../web/src/cc/app.tsx#L990) | Windows Chromium 验证 Ctrl+/ 打开、焦点留在弹窗内、Shift+Tab 环绕、Escape 关闭；Ctrl+, 设置弹窗打开并可 Escape 关闭。设置页未覆盖原要求的完整分页。 |
| 4a | LLM provider 的 reasoning/content/tool-call delta 进入独立 `StreamEvent`；worker 透传为带 run/segment/chunk 的内存包络 | [`llm.py`](../../cc_harness/llm.py#L470)、[`worker.py`](../../cc_harness/worker.py#L1039)、[`durable_runtime.py`](../../cc_harness/durable_runtime.py#L293) | Python 单测覆盖分离与顺序；真实 SSE 收到 3 content + done，但本轮未见 reasoning/tool delta。 |
| 4b | SSE 先订阅 LiveStreamHub 再读 Durable，按 `Last-Event-ID` 游标对账并发出 runtime/stream 两种事件 | [`webui.py`](../../cc_harness/webui.py#L2677)、[`webui.py`](../../cc_harness/webui.py#L2731)、[`webui.py`](../../cc_harness/webui.py#L2786) | `tests/test_web_sse.py` 覆盖订阅/回放与 header cursor；Chromium 离线 3.2 秒后向真实 API 重连带 `Last-Event-ID: 7`，服务端恢复连接，刷新前后可见消息 2→2，截图 `17`。临时 delta 不补发。 |
| 4c | rAF 合帧、按 run/segment/chunk 去重、commit 权威替换、gap 冻结；按轮次虚拟化 10,000 条 timeline 上限 | [`streaming.ts`](../../web/src/state/streaming.ts#L49)、[`app.tsx`](../../web/src/cc/app.tsx#L2704)、[`app.tsx`](../../web/src/cc/app.tsx#L2620) | Node reducer 4/4；Playwright 5,000 事件夹具仅挂载 10 轮，顶部阅读新增事件不移动 scrollTop，跳转后距底部 54px，截图 `09`/`10`。大范围跳转使用立即滚动以避免长距离平滑滚动迟滞。 |
| 4d | 独立 thinking 展示、正文到达后自动折叠、显示浏览器本地观测耗时；reasoning 保持易失且 reasoning-only 响应不进入正文 | [`StreamingMessage.tsx`](../../web/src/components/chat/StreamingMessage.tsx#L12)、[`streaming.ts`](../../web/src/state/streaming.ts#L107)、[`llm.py`](../../cc_harness/llm.py#L338)、[`worker.py`](../../cc_harness/worker.py#L1933) | reducer/Python 回归覆盖 reasoning/content 分离与 reasoning-only 安全重试；真实 DeepSeek 未返回 reasoning delta，故缺真实 reasoning E2E。刷新不恢复 reasoning 原文。 |
| 4e | 工具实时/终态卡片展示参数摘要、耗时、结果行数/项数、有限预览；审批卡片提交现有 digest API；成功修改自动打开 Artifact | [`app.tsx`](../../web/src/cc/app.tsx#L1213)、[`app.tsx`](../../web/src/cc/app.tsx#L1360)、[`webui.py`](../../cc_harness/webui.py#L479) | 真实 Glob 活动态截图 `06-tool-call-live.png`，真实 Glob→Read 折叠/展开截图 `06`/`07`；真实 Write 审批拒绝/允许及 Artifact 自动打开截图 `14`–`16`。退出码、stderr/参数风险 diff 尚不完整。 |
| 4f | stop 走现有 API，保留部分输出；仅当停止后没有 ActionStarted/成功/失败/审批等副作用时，按原用户文本允许安全重试 | [`app.tsx`](../../web/src/cc/app.tsx#L2993)、[`app.tsx`](../../web/src/cc/app.tsx#L2708) | Chromium + 真实 provider 完成停止和重试。完整“重新生成上一条 assistant”未实现；不重放已开始工具的回合。 |
| 5 | 渐进抽离流 reducer、流式消息、欢迎建议和代码视图；FastAPI 静态托管路径保持原样 | [`streaming.ts`](../../web/src/state/streaming.ts#L49)、[`StreamingMessage.tsx`](../../web/src/components/chat/StreamingMessage.tsx#L12)、[`WelcomeSuggestions.tsx`](../../web/src/components/chat/WelcomeSuggestions.tsx#L4)、[`webui.py`](../../cc_harness/webui.py#L2171) | Vite + TS build、Node tests、FastAPI 静态打包均通过；TanStack Virtual 为长历史加最小依赖。`app.tsx` 与 `styles.css` 仍是大文件，composer/sidebar/API/common 样式目录没有完整拆分。 |

## 真实模型与浏览器记录

- Windows 本机启动 FastAPI WebUI，使用独立的临时 Runtime 数据根；provider 从项目 `.env` 读取，未将配置值或密钥写入日志/报告。最终截图保存在 `docs/audits/screenshots/final/`。
- 真实 DeepSeek 纯文本请求“只输出数字 42”收到实际 `stream_delta(kind=content)`、`done` 与 Durable commit，在浏览器逐块显示后呈现提交答案，见 `05-real-model-answer.png`。provider 未发 reasoning delta。
- 真实工具请求依次触发 Glob 和 Read，两个观察均以 Durable `ToolObservationCommitted` 投影展示；截图 `06` 折叠过程、`07` 展开结果和安全的续读边界。对已完成 Run 点击续读时，Runtime 拒绝了跨子 Run 的观察读取；该限制没有改变 Runtime 语义，完整页未伪称通过，失败尝试见 `08-tool-result-continuation.png`。
- 两次真实 Write 审批分别拒绝与允许。拒绝没有创建文件；允许后创建了精确内容并自动打开 Artifact 右栏。验收文件已删除；证据见 `14`–`16`。
- 真实 provider 回合流式输出后停止，再通过安全重试产生新增量，见 `11`–`13`。另有 5,000 条历史事件夹具和真实应用 UI 的 3.2 秒离线/Last-Event-ID 测试，见 `09`、`10`、`17`。快捷键弹窗键盘 E2E 截图为 `18`。

## 8 条浏览器 E2E 矩阵

| 场景 | 结果 | 证据/原因 |
|---|---|---|
| ① 亮/暗双主题 | 完成 | `01-dark-welcome.png`、`02-light-welcome.png`。 |
| ② 简单问答增量、reasoning 独立与自动折叠 | 部分 | 真实文本增量和 Durable commit 通过；真实 provider 未产出 reasoning，隔离思考 UI 由 reducer 回归覆盖。 |
| ③ 两步工具任务、卡片实时出现/折叠/展开 | 部分 | 真实 Glob 执行中的卡片、Glob→Read 折叠/展开、参数和结果通过（`06-tool-call-live`、`06`、`07`）；结束后跨 Run ContinueToolResult 被 Runtime 拒绝（`08`），无法声称完整续读通过。 |
| ④ 中途停止、保留半截并可安全重试 | 完成（安全重试范围） | 真实 provider + Chromium 停止后保留内容、显示“重试本轮”、再次流式输出，截图 `11`–`13`。不提供可重放已执行工具的无条件“重新生成”。 |
| ⑤ Write 后 Artifact 自动打开 | 完成 | 真实获批 Write 的 `ActionSucceeded.modified_paths` 自动打开 Artifact 并预览当前文件，截图 `16`；多标签/全屏由模拟 API 文件夹验证，当前默认程序打开路径由后端测试验证。 |
| ⑥ 审批允许与拒绝 | 完成 | 真实 Write 审批：拒绝不创建文件；允许创建精确内容；验收文件清理。截图 `14`–`16`。 |
| ⑦ 3 秒离线、Last-Event-ID 与刷新去重 | 完成（durable 事件） | 离线 3.2 秒，浏览器续连请求含 `Last-Event-ID: 7`，后续转发至真实 SSE API；刷新前后可见消息未增加/重复。临时 delta 不保证补发，见 `17`。 |
| ⑧ 5,000+ 事件虚拟滚动与回到底部 | 完成（历史夹具） | 5,000 条合成事件只渲染 10 个可见轮次；上翻后追加事件 scrollTop 0→0；“跳到最新”后距底部 54px，截图 `09`/`10`。 |

## 未完成项、影响和建议

| 未完成项 | 根因/影响 | 建议 |
|---|---|---|
| 历史 reasoning artifact 处理 | 新消息路径已不写 raw reasoning；历史不可变 artifact 仍可能含 ADR-0089 时期保存的字段。 | 已选向前兼容：不原地改写历史事件/artifact；如需清理既有运行数据，应另行设计有审计记录的显式迁移。新字段回归由 `tests/test_live_stream.py` 和 `tests/test_llm.py` 覆盖。 |
| reasoning 实际 provider 流 | 本次 DeepSeek 请求没有发 reasoning delta；不能用模拟页面截图冒充真实模型行为。 | 用确实配置 Extended Thinking 的 provider 单独运行同一浏览器场景；当前 reducer 与 raw reasoning 安全边界有单测。 |
| 已结束 Run 的完整工具结果续读 | `ContinueToolResult` 从当前 Run 的 Durable observations 中找 observation id；完成后新消息会进入子 Run，Runtime 不允许跨 Run 访问。已隐藏不支持的按钮并在卡片说明边界。 | 若产品必须支持完成后续读，需要单独设计 Runtime 授权的跨 Run 只读 API/事件契约；本次按“不改 Runtime 状态机/工具语义”不新增此能力。 |
| 源码目录完全拆分与 Claude 的所有周边操作 | `app.tsx`/`styles.css` 仍较大；附件/麦克风、点赞/踩、分支/完整重新生成、分享/导出、完整账号区和完整设置分页未实现。 | 后续拆分前端模块；只有在 cc-harness 提供真实 API/能力时再接入对应控制，不显示伪造功能。 |
| Thinking 的真实流式观感、tok/s | reasoning 未从真实 provider 到达；Runtime 没提供可用于精确速率的 token 计数流，因此未编造 tok/s。 | 配置/启用 provider reasoning 后再做端到端确认；仅在 Runtime 给出可信 usage 后显示速率。 |
| 历史 reasoning artifact | 新写入路径不再保存 raw reasoning；ADR-0089 期间的不可变 artifact 可能保留旧字段。 | 不原地改写历史；若需要清理，另做带审计的显式迁移。 |
| 完整视觉无障碍审计 | 已检查焦点环、弹窗初始焦点、键盘陷阱与 Escape；未完成每个状态的 WCAG AA 对比度/全键盘审计。 | 对主题 token 和全部控件再跑系统化的色彩与辅助技术审核。 |

## 门禁输出摘要

以下命令在完成最终源码修改后执行；以最终终端退出码为准。

| 命令 | 结果 |
|---|---|
| `cd web; npm run build` | PASS，TypeScript 零错误，Vite 1,577 modules 构建成功。 |
| `cd web; npm test` | PASS，Node 原生 reducer tests 4/4。 |
| `python -m pytest tests/runtime_rebuild -q` | PASS，exit 0。 |
| `python -m pytest tests/test_live_stream.py tests/test_web_sse.py tests/test_llm.py tests/test_webui.py tests/fault_injection/test_fi_runtime.py -q` | PASS，exit 0；真实 provider 探针测试按仓库默认策略 skip（需 `CC_HARNESS_RUN_REAL_LLM=1`），另有 Chromium/真实 DeepSeek E2E 已实际运行。 |
| `ruff check <本次修改的 Python 源码与测试>` | PASS，`All checks passed!`。未将 Ruff 用于 TSX。 |
| `python scripts/build_webui.py` | PASS，重建 FastAPI 静态 bundle，保留既有 `mascot.png`。 |
| `git diff --check` | PASS；仅提示工作区 LF→CRLF 的 Git 行尾转换提醒，无空白错误。 |

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
| `docs/audits/screenshots/final/03-real-model-stream.png` | 早期真实模型结果截图（不是增量过程证据） |
| `docs/audits/screenshots/final/05-real-model-answer.png` | DeepSeek 真实流式答案“42” |
| `docs/audits/screenshots/final/06-tool-call-live.png` | Glob 正在执行时的真实工具卡片 |
| `docs/audits/screenshots/final/06-tool-process-collapsed.png` | 真实 Glob/Read 工具过程折叠 |
| `docs/audits/screenshots/final/07-tool-results-expanded.png` | Read 结果展开、5/60 行和 Runtime 续读边界 |
| `docs/audits/screenshots/final/08-tool-result-continuation.png` | 点击续读后跨 Run 被 Runtime 拒绝的证据；作为缺项记录 |
| `docs/audits/screenshots/final/09-long-history-window.png` | 5,000 事件夹具上翻阅读且新增消息不拽底 |
| `docs/audits/screenshots/final/10-long-history-jump-latest.png` | 长历史点击跳转后回到末尾 |
| `docs/audits/screenshots/final/11-stop-live-response.png` | 真实模型输出中按停止前的部分答案 |
| `docs/audits/screenshots/final/12-stopped-safe-retry.png` | 停止后的保留内容与安全重试按钮 |
| `docs/audits/screenshots/final/13-retry-streaming.png` | 重试后的真实增量输出 |
| `docs/audits/screenshots/final/14-approval-pending-reject.png` | 拒绝路径的待审批卡片 |
| `docs/audits/screenshots/final/15-approval-pending-allow.png` | 允许路径的待审批卡片 |
| `docs/audits/screenshots/final/16-approval-allowed-artifact.png` | Write 获批后自动打开的 Artifact 文件面板 |
| `docs/audits/screenshots/final/17-sse-last-event-id-reconnect.png` | 3 秒离线恢复后的真实 SSE 页面 |
| `docs/audits/screenshots/final/18-shortcuts-modal.png` | 快捷键弹窗与键盘焦点测试 |
| `docs/audits/screenshots/final/19-artifact-tabs-fullscreen.png` | Artifact 两标签、词法高亮与全屏（文件 API 夹具） |
