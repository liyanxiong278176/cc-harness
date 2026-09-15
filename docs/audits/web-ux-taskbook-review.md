# WebUI 自主任务书评审

评审对象：`codex-frontend-upgrade-autonomous-prompt.md`（来自用户粘贴的版本）  
评审日期：2026-09-14  
评审范围：当前 `D:\agent_learning\cc-harness`，不修改 `D:\agent_learning\外卖`

## 结论

任务书的交互目标与当前项目方向一致，但不能原样执行。它把旧的非 Durable `run_turn` 流式接口当成了生产路径，并且要求展示原始 reasoning、引入不存在的文件树/队列 API，以及把整个仓库测试标成必须全绿。直接照做会破坏“不可变事件流是唯一事实源”、泄露 provider 私有字段，或制造不可用的假入口。

本次采用以下适配原则：

1. Durable Runtime 的 `run_events`、Checkpoint、审批、权限和压缩语义不改；流式增量只驻内存，`AssistantMessageCommitted` 仍是正文权威版本。
2. provider 的 `reasoning_content` 仅用于模型协议恢复和运行态统计，WebUI 只显示“正在思考/已思考”状态，不展示原始思考文本。
3. 只实现当前后端已有 API 能兑现的 UI；没有真实 API 的文件树、队列、分叉和完整工具参数面板不放假入口。
4. 验收以本次改动相关的后端测试、前端构建、流式协议测试和 HTTP/SSE 烟测为准；可选依赖缺失或历史无关失败单独记录，不把它们伪装成“全绿”。

## 事实复核与差距

| 任务书假设 | 当前事实（代码证据） | 处理 |
|---|---|---|
| `/api/sessions/{run_id}/events` 已接入实时 token 流 | `cc_harness/webui.py:1402` 当前每 0.5 秒读取 Durable 事件；没有进程内增量通道 | 实现易失 `LiveStreamHub`，SSE 同时消费增量和 Durable 对账 |
| `llm.py` 的 chunk 可直接由 worker 转发 | `cc_harness/llm.py:244` 产生 `StreamEvent`，但 `DurableModelAdapter.complete` 在 `cc_harness/durable_runtime.py:179` 缓冲后才返回 `ModelSegment` | 给 `ModelAdapter` 增加可选回调，worker 负责添加 run/segment/chunk 元数据 |
| `run_turn(event_emitter)` 是 Durable WebUI 路径 | `cc_harness/runtime.py:875` 的 `run_turn` 属于旧 Runtime；Durable supervisor 使用 `RunWorker` + `ReActKernel` | 不修改旧 `run_turn` 语义，新增 Durable 专用回调 seam |
| 原始 reasoning 可在 UI 中展示 | 当前安全边界明确不把 provider 私有 reasoning 发送到浏览器；`reasoning_content` 还用于 DeepSeek 协议恢复 | 只广播非敏感状态和长度/阶段，不广播原文 |
| 前端从单体空白页开始 | `web/src/main.tsx` 已有三栏、按项目分组会话、权限选择、设置、上下文环、审批卡和状态面板 | 以增量修复为主，不重写已稳定的布局 |
| 完整文件树、@ 芯片、队列、轨迹 tab 都有后端 API | 当前公开 API 只有 project/session/timeline/context/events/settings/approval/stop/resume | 任务书改为“有 API 才接入”；其余记录为延期，不渲染假数据 |
| 所有 `pytest` 必须全绿 | `tests/test_agentdojo_adapter.py` 需要可选 `agentdojo`；仓库还存在与本次无关的历史失败 | 使用相关测试集 + `tests/runtime_rebuild`，附完整套件阻塞原因 |

## 风险分级

| 风险 | 影响 | 决策 |
|---|---|---|
| 流式增量被误写入 SQLite | 破坏不可变事件流、增加恢复重复 | 禁止持久化；断线后以 timeline 重建 |
| 多 worker 共用回调 | A 会话的 token 串入 B 会话 | 回调在 worker/segment 作用域创建，所有消息携带 run_id |
| SSE 只读增量不读权威事件 | 刷新或丢包后显示不一致 | 每个流保留 Durable 轮询对账，提交事件覆盖临时气泡 |
| 展示原始 reasoning | 可能泄露 provider 私有思考和敏感数据 | 浏览器只收阶段状态，不收 reasoning 文本 |
| supervisor 进程与 WebUI 分离 | 进程内 hub 收不到 detached worker 的增量 | 继续保留 0.5 秒 Durable 对账；文档标注 detached 模式无 token 级实时流 |
| 大会话渲染卡顿 | 用户无法输入/滚动 | 先做 bounded event window、合帧和滚动契约；虚拟列表作为后续独立变更 |

## 原任务项处置

| 原任务项 | 处置 | 原因 |
|---|---|---|
| P0-1～P0-4 | 本次实现 | 是当前体验差距最大且不改变 Durable 事实源 |
| P0-5 | 改写为安全的思考状态 | 不向浏览器发送原始 reasoning |
| P0-6 | 本次保留现有停止 API，并补流式冻结状态 | 当前接口已存在 |
| P1-1～P1-5 | 部分实现/保留 | 工具参数脱敏和结果已有后端边界；UI 只消费公开投影 |
| P1-6 | 保留现有空状态和首句标题 | 当前 UI 已具备，不重复重写 |
| P2-1、P2-3～P2-6 | 保留并校准 | 当前三栏、权限、状态条、降级和恢复入口已存在 |
| P2-2 | 仅实现现有 `/` 命令 | `@` 文件芯片没有后端引用 API，延期 |
| P3-1～P3-4 | 延期/缩小 | 轨迹、文件树和完整审批 diff 缺少公开 API；禁止假数据 |
| P4-1 | 延期 | 需要专门的长列表基准和新增依赖评估 |
| P4-2～P4-4 | 本次验证 | 当前已有滚动/快捷键/中文 UI 基础 |
| P4-5 | 不做大拆分 | 单文件重构会扩大风险；先通过协议 seam 和组件级小改动验证 |

## 适配后的验收口径

- 前端构建通过，TypeScript 无新增错误。
- `LiveStreamHub` 覆盖多订阅者、顺序、取消订阅和有界缓存。
- Durable worker 的真实 provider chunk 能按 `run_id → segment → chunk` 顺序广播；广播失败不得改变运行结果。
- SSE 断线/重连后先以 Durable timeline 对账，临时增量不重复、不伪造已提交正文。
- 停止、审批拒绝、provider 失败仍由现有 Durable API 和事件状态机处理。
- 运行面板、上下文压缩、记忆/安全能力状态继续来自后端真实字段；缺字段隐藏，不编造。
- 真实模型烟测只在已有用户配置和明确授权下执行；默认测试使用可控假 provider，避免意外费用。

