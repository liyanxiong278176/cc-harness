# WebUI 重设计决策记录

日期：2026-09-30  
分支：`feat/claude-style-webui`

本记录按原需求给出的优先级作决定：Runtime 契约与既有执行语义优先，其次是实时反馈，再是 Claude 桌面端的视觉和交互相似度，最后控制改动范围。它也记录尚未完成的取舍，不代表需求矩阵全部通过。

## 决策

| 主题 | 备选方案 | 选择 | 原因与影响 |
|---|---|---|---|
| 参考材料 | 复制 Claude 资源；仅观察布局；使用全新视觉方向 | 在本机 Claude 桌面端观察并截图，重新实现结构与配色 | 参考截图放在 `docs/audits/screenshots/claude-ref/`；没有复制 Claude 的字体文件、商标、插画或源码。产品标记使用项目自有符号。 |
| 色彩 | 使用原 cc 配色；照抄截图取样；只定义一套主题 | 以截图采样为参考，设 CSS 变量并提供亮/暗主题 | 暗色主要表面取样约为页面 `#151515`、侧栏 `#111111`、输入区 `#20201F`，陶土色强调色约 `#D97757`；亮色页面约 `#FCFCFB`、侧栏 `#FBFBF9`、输入区 `#FFFFFF`。最终值在 `web/src/styles.css`，截图和显示器采样存在约 ±2 RGB 误差。 |
| 流式数据路径 | 把 delta 写进 SQLite/run_events；进程内推送并以 Durable 对账；只轮询 Durable | 使用现有 `LiveStreamHub` 进程内广播，保留 Durable 事件对账 | delta、reasoning 与工具阶段不写入新增的 Durable 流式事件；`AssistantMessageCommitted` 仍是正文权威来源。代价是跨进程/断线时临时 delta 不可恢复，前端冻结并等待 Durable 投影。 |
| reasoning | 混入 assistant 正文；浏览器临时独立展示；新增脱敏后的持久摘要 | 新增只在 WebUI 内存通道消费的独立 `reasoning` 增量 | 与正文分开、按 run/segment/chunk 去重；有正文到达后折叠并显示思考时长。没有新增长期摘要字段，因为这会改变 Runtime 事件/投影语义。 |
| reasoning 持久化边界 | 保留 provider 字段供工具回放；让 reasoning 仅内存可见，并为 provider 缺字段做一次有限降级 | 新写 artifact/session 不记录 raw reasoning；reasoning-only 响应不会作为答案，工具回放必要时切到 thinking-disabled 一次 | 新持久化路径不含 reasoning。为遵守 `run_events` 不可变与“不改状态机”约束，不原地改写 ADR-0089 时期的历史 artifact；历史数据仍可能包含该字段。策略和回退边界见 ADR-0090。 |
| 断线恢复 | 缓存并重放临时 delta；只用 `Last-Event-ID` 重放 Durable；关闭流并重发模型请求 | 让 EventSource 按 Durable cursor 对账，禁止基于临时文本恢复事实 | SSE 首先订阅 live hub，再读取 Durable 事件树；reconnect 后有权威提交就显示提交正文，不能恢复的临时 reasoning 不再出现。这样避免重复模型调用，但断线期间可能看不到未提交的增量。 |
| React 结构 | 一次拆完整组件树；维持单文件；渐进抽离低耦合逻辑 | 增量 reducer、流式消息、建议卡片、代码视图独立；主页面/样式渐进抽离 | 已加入 `web/src/state/streaming.ts`、`components/chat/StreamingMessage.tsx`、`WelcomeSuggestions.tsx` 与 `components/right-panel/SyntaxCode.tsx`。`app.tsx`、`styles.css` 仍是大型文件；不宣称完成了 composer/sidebar/API/common 全目录拆分。 |
| CSS 隔离 | 引入 CSS Modules；沿用项目的全局 CSS 变量/选择器 | 沿用 `web/src/styles.css` | 现有页面依赖单一主题变量和全局类名；不新增样式依赖。主题 token 和聊天组件样式仍未拆成独立模块。 |
| 前端 reducer 测试 | 新增 Vitest/Jest；采用仓库已有测试设施；Node 原生测试 | 使用 Node `node:test` 与 TypeScript strip-types | 在 `web/package.json` 增加 `npm test`，不增加依赖。验证 reasoning/content 分离、重复 chunk、gap 冻结、segment 切换和工具阶段。 |
| 真实模型验证隔离 | 连接已有 3080 服务；使用本地 mock；单独启动临时 Runtime 数据根 | 单独启动本仓库 WebUI，指向隔离临时数据根，读取项目 `.env` 的 provider 配置 | 避免向其他进程/项目写入 session。Chromium + 真实 provider 实际运行纯文本流、Glob→Read、stop→retry、Write 审批允许/拒绝与 Artifact 自动打开；不会把 provider 未发出的 reasoning delta 伪称通过。跨 Run 续读被 Runtime 拒绝并记录为缺项。 |
| 长历史渲染 | 直接保留所有轮次 DOM；自行实现估算虚拟列表；引入成熟的小型虚拟化库 | 使用 `@tanstack/react-virtual` 虚拟化 conversation turn | 这是为 5,000+ 历史体验引入的唯一运行时依赖；按 turn 动态测量、overscan 6 行、最多读取 10,000 个 timeline event。小距离平滑，大距离立即跳转，避免超长平滑滚动看似卡住。Chromium 5,000 事件夹具实际挂载 10 个 turn。 |
| 日期分组来源 | 按 sequence 猜日期；为 session API 增加时间投影；放弃时间分组 | 从 Durable 根/树事件首末 `occurred_at` 生成只读 `created_at`/`updated_at` | 时间字段由真实事件产生；会话列表内部按 Run 读取首末记录，测试覆盖 projection，前端分为今天/近 7 天/更早。没有从 event count 推测时间。 |
| 工具结果续读 | 完成后由新 child Run 调用原 Run 的 `ContinueToolResult`；新增跨 Run Runtime 能力；不展示失效 CTA | 保留 Runtime 的 observation 归属规则；仅当前 Run 可恢复时显示继续按钮，跨 Run 显示限制提示 | 真实 E2E 证实新消息进入 child Run 后，`ContinueToolResult` 只能读取当前 Run observations，跨 Run 请求被拒绝。没有为 UI 偷开 Runtime 权限；需要跨 Run 完整结果时须单独设计 Runtime 合约。 |
| 安全重试 | 任意失败/停止都复用原始 prompt；仅 Runtime resume；检查后续工具副作用 | 只允许最后一条用户回合之后不存在 ActionStarted/ActionSucceeded/ActionFailed/ToolObservationCommitted/ApprovalDecided 的场景调用重试 | 避免重新执行已经开始的工具副作用；有后续动作时继续使用既有检查点“继续”语义。真实 provider stop→retry E2E 通过。完整 assistant regenerate 功能不实现。 |
| Artifact 源码预览 | HTML 字符串模板高亮；引入重量级 code editor；React token 节点 | 用正则词法 token + React 节点输出；延续 dompurify Markdown/HTML 消毒 | 不插入未经信任的 HTML、不引入编辑器依赖。支持多 tab/全屏/本机默认程序打开；syntax highlighting 是轻量词法着色，不等同 IDE parser。 |

## 仍需产品/架构工作

- 已结束 Run 的只读工具结果跨 Run 续读：需要 Runtime 明确授权/事件契约，不能由前端跨 Run 查 artifact。
- 真实 provider reasoning delta：本次 DeepSeek 没有发出 reasoning；需要支持 Extended Thinking 的配置作额外端到端验收。
- 完整拆分 `app.tsx`/`styles.css`，补齐附件/麦克风、点赞/踩、分支/完整重新生成、分享/导出和完整设置分页。
- token 速率仅在 Runtime 提供可信计数/时间字段后显示；目前不估算虚构 usage。
- 如需清理 ADR-0089 期间产生的历史 reasoning artifact，另行设计可审计的显式迁移；新写入路径已不再保存该原文。
