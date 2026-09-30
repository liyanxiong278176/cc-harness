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
| reasoning 持久化边界 | 改写模型适配器和历史重放；维持现有 Runtime 重放语义并披露冲突 | 保留当前适配器语义，明确记录冲突 | `cc_harness/llm.py` 会把 provider 的 `reasoning_content` 放进 assistant 消息，以满足 DeepSeek 工具重放协议；当 provider 未返回正文时还会用 reasoning 文本兜底正文。因“不改 Runtime 重放/事件语义”的硬约束，本次不能保证所有 provider reasoning 原文绝不进入现有消息 artifact。新增 WebUI live envelope 本身不带 `reasoning_content` 字段，但它的 `kind=reasoning,text` 是临时展示数据。 |
| 断线恢复 | 缓存并重放临时 delta；只用 `Last-Event-ID` 重放 Durable；关闭流并重发模型请求 | 让 EventSource 按 Durable cursor 对账，禁止基于临时文本恢复事实 | SSE 首先订阅 live hub，再读取 Durable 事件树；reconnect 后有权威提交就显示提交正文，不能恢复的临时 reasoning 不再出现。这样避免重复模型调用，但断线期间可能看不到未提交的增量。 |
| React 结构 | 新增完整组件树并拆分整个 app/styles；沿用现有文件边界；渐进抽离 | 只抽离流式状态机、流式消息和建议卡片 | 已加入 `web/src/state/streaming.ts` 与 `web/src/components/chat/` 下两个组件；`web/src/cc/app.tsx` 及全局样式仍是大型文件。完整目录拆分需要覆盖用户已有的大量 UI 改动，未在本次实现中完成。 |
| CSS 隔离 | 引入 CSS Modules；沿用项目的全局 CSS 变量/选择器 | 沿用 `web/src/styles.css` | 现有页面依赖单一主题变量和全局类名；不新增样式依赖。主题 token 和聊天组件样式仍未拆成独立模块。 |
| 前端 reducer 测试 | 新增 Vitest/Jest；采用仓库已有测试设施；Node 原生测试 | 使用 Node `node:test` 与 TypeScript strip-types | 在 `web/package.json` 增加 `npm test`，不增加依赖。验证 reasoning/content 分离、重复 chunk、gap 冻结、segment 切换和工具阶段。 |
| 真实模型验证隔离 | 连接已有 3080 服务；使用本地 mock；单独启动临时 Runtime 数据根 | 单独启动本仓库 WebUI，指向隔离临时数据根，读取项目 `.env` 的 provider 配置 | 避免向其他进程/项目写入 session。真实请求使用简短的纯文本 prompt；SSE 直连观察增量和提交事件。工具、审批、停止等场景未因本次测试而伪称通过。 |

## 仍需产品/架构工作

- 完整重构 `app.tsx`、拆分 `styles.css`，并补齐指定的 composer、右侧 Artifact、消息操作和工具结果交互。
- 给公开 session projection 提供可信时间戳后再实现按日期分组；不能用 sequence 推断日期。
- 为真正的 reasoning-only provider 设计一个不破坏工具重放的持久化策略，然后再把“reasoning 原文不落库”作为整体保证。
- 增加浏览器端工具、审批允许/拒绝、停止、断线恢复和长会话自动化验证。

