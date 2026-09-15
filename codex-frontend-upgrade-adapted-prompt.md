# cc-harness WebUI 自主执行任务书（适配版）

你是一名资深前端/交互工程师，兼通 Python 后端。工作目录是 `D:\agent_learning\cc-harness`，只读参考目录是 `D:\agent_learning\deepseek-harness-reference`。目标是改善 cc-harness WebUI 的实时交互，同时保持唯一的 Durable Runtime、上下文/记忆/安全能力和审计语义不变。

## 自主执行规则

1. 不等待确认；遇到取舍时按“runtime 契约 > 安全与隐私 > 可恢复性 > 实时体验 > 视觉细节 > 改动面最小”自行决策，并写入 `docs/audits/web-ux-decisions.md`。
2. 不修改 `D:\agent_learning\外卖`，不改 `run_events` schema、状态机白名单、Checkpoint 语义或用户 `.env`。
3. 流式增量是易失 UI 数据，禁止写入 SQLite 或 `run_events`；`AssistantMessageCommitted` 是唯一权威正文。
4. provider 原始 `reasoning_content` 不发送到浏览器，只发送非敏感的思考阶段状态。
5. 只调用当前已有 REST/SSE API；没有后端来源的控件不渲染为可操作入口。
6. 使用 Windows/PowerShell 实测；不复制参考项目源码，不引入 Redux/Ant Design/MUI 等重框架。

## 当前架构事实

- Durable 路径是 `DurableRuntimeClient → LocalSupervisor → RunWorker → ReActKernel → DurableModelAdapter → LLMClient`。
- `LLMClient.chat()` 已逐 chunk 产生 `StreamEvent`，但 `DurableModelAdapter.complete()` 目前在返回 `ModelSegment` 前缓冲完整响应。
- `cc_harness/webui.py` 的 `/api/sessions/{run_id}/events` 负责 Durable 事件轮询和重放；它必须继续保留低频对账和 `Last-Event-ID`。
- WebUI 已有三栏布局、按项目分组会话、设置、权限策略、上下文环、审批卡、停止/继续和执行降级状态。

## 执行顺序

### 1. 评审与设计

完成并落盘：

- `docs/audits/web-ux-taskbook-review.md`
- `docs/audits/web-ux-gap.md`
- `docs/audits/web-ux-decisions.md`
- `docs/design/web-streaming.md`

记录当前事实、参考界面差异、未接入能力及每项取舍。

### 2. P0：Durable 流式广播

- 新增进程内 `LiveStreamHub`：按 run 订阅、异步队列扇出、有界最后值缓存、取消订阅；发布异常不得影响 worker。
- 为 `ModelAdapter`/`ReActKernel` 增加向后兼容的可选流式回调；旧测试 double 不支持回调时仍可执行。
- `DurableModelAdapter` 转发 content、tool-call 阶段和终态元数据；不转发原始 reasoning 文本。
- `RunWorker` 为每个 segment 生成单调 chunk 序号，附 `run_id`、segment、chunk、phase；worker 结束发送终态并清理。
- `/events` 先发送 Durable 对账事件，再发送易失增量；断线后以 timeline 为准，不把增量当作已提交事件。
- 前端维护临时 streaming 节点，收到 `AssistantMessageCommitted` 后用权威正文替换；停止/失败时冻结临时内容并显示真实状态。
- 增量 markdown 至少按 rAF/30ms 合帧，避免每个 token 触发全量重排。

### 3. P1：状态和错误体验

- 保留现有三档权限、停止/继续、审批、沙箱降级和上下文真实遥测。
- 只显示后端提供的状态/用量；没有字段就隐藏。
- provider 重试、SSE 重连和 Durable 对账使用明确中文状态。
- `/` 命令只映射真实 API；没有文件引用 API 的 `@` 芯片不放假入口。

### 4. P2：验证

- 新增 hub、回调兼容、SSE 对账和前端 streaming 替换测试。
- `web/npm run build`。
- `pytest` 运行相关测试以及 `tests/runtime_rebuild`。
- `ruff check` 运行改动 Python 文件。
- 启动 WebUI，使用 HTTP/SSE 验证健康、会话列表、事件重放和断线对账；若没有明确授权，不发起真实 provider 请求。
- 输出 `docs/audits/web-ux-final-report.md`，列出完成/延期矩阵、测试结果、已知限制和用户可见变化。

## 完成标准

完成不是模型一句“已完成”，而是代码、测试、事件顺序、前端构建和 HTTP/SSE 证据均满足上述验收。任何失败都记录根因和影响，不编造“全绿”。

