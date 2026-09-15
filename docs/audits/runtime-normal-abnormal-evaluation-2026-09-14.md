# Runtime 正常与异常路径评测

日期：2026-09-14

本次评测只使用仓库和临时项目目录，没有读取、修改或运行 `D:\agent_learning\外卖` 中的业务代码。

## 权威 UI 参考

对照了 [DeepSeek Harness 官方仓库](https://github.com/deepseek-ai/deepseek-harness) 和其 Web UI 指南。官方流程是启动后输出本地 URL，在 Settings 配置模型，选择 workspace 后才能发送任务，并在权限策略要求时进行审批。

当前 WebUI 对应实现：

- 左侧按项目路径分组，可展开/收起会话历史；
- 中间消息区独立滚动，底部 composer 固定；
- 用户消息保留在右侧，助手回复、工具卡片和实时流分开显示；
- 右侧显示 Durable Runtime、调度器、审批和实时事件时间线；
- 上下文用量保留在底部模型状态入口，完成协议 JSON 不直接展示给用户。

## 正常路径

### 真实 Provider 冒烟

使用已配置的 DeepSeek `deepseek-v4-flash`，在临时项目运行一次最小任务。没有把 API key 或响应原文写入仓库。

| 指标 | 结果 |
| --- | --- |
| Provider 响应 | `true` |
| Runtime 根 Run | 1 |
| 最终状态 | `completed` |
| 事件数量 | 17 |
| 事件链 | `RunCreated → PlanCreated → TodoCreated → GoalContractAccepted → RunQueued → RunClaimed → RunSegmentStarted → PlanNodeStarted → TodoUpdated → ModelInvocationStarted → ModelInvocationFinished → AssistantMessageCommitted → CompletionCandidateSubmitted → PlanNodeCompleted → TodoCompleted → CompletionAccepted → RunOutcomeRecorded` |

真实模型没有主动返回合法 `CompletionCandidate`。测试 wrapper 仅在缺失时补入固定测试 candidate，以隔离验证 Runtime 的持久化/完成门；Provider 调用本身是真实的。这个结果仍是重要的编码任务观察：自然语言回答不能直接让 coding Run 完成。

### 本轮 Runtime 修复后的真实模型验证

在同一临时目录追加了两次真实 DeepSeek 请求（没有进入业务项目）：

- `你好` 被 WebUI 的保守分类器持久化为 `interaction_mode=conversation`。模型回复落盘后，Runtime 以 `ASSISTANT_RESPONSE` 证据生成 `CompletionCandidate`，最终 `completed`；没有把模型最后一句话当作完成证明。
- 第二轮 `你能做什么？` 作为同一根会话的 follow-up child Run 执行并完成，根会话仍保持独立的事件/检查点链。
- 含有“工具/项目/测试”等编码意图的短句仍分类为 `coding`，缺少验证证据时保持 `stalled`，不会因为修复对话体验而放宽编码完成门。

这使正常对话和工程任务有明确的 Runtime 合约，而不是用一个“模型说完成了”分支覆盖两种语义。

### 页面与数据

验证服务使用临时端口（本轮为 `http://localhost:3082/`），端口不是运行时契约的一部分。

健康检查返回 `200`；根页面引用由 `scripts/build_webui.py` 生成的最新构建资源。一次已持久化会话的 API 数据显示：

- 状态和事件序号来自 Durable Projection；
- 上下文 `5367 / 128000`，约 `4.2%`；
- context、memory、safety capability 均已初始化；
- 短任务未触发压缩，`compaction.tier=none` 是按阈值工作的预期结果；
- `/timeline` 展示的事件与 SQLite 事件流一致；
- 会话切换期间先显示“正在载入会话”，并禁用发送；只有最新请求完成后才解除，避免大历史读取时出现空白欢迎页或把消息发到旧会话；
- 右侧运行面板增加 Runtime-owned 诊断卡：投影重建、无完成证据、未知工具结果、租约冲突和环境降级都有中文原因与下一步；原始 traceback、completion JSON 和密钥不会进入聊天区。

## 异常矩阵

| 场景 | 验证结果 | Runtime 行为 |
| --- | --- | --- |
| Provider 返回后、助手结果落盘前中断 | 真实 DeepSeek 测试通过 | `running → cancel_requested → cancelled`；继续后复用同一 `run_id`/checkpoint，最终 `completed`，根 Run 仍为 1 |
| 阻塞/停滞后继续 | 回归测试通过 | `blocked/stalled/cancelled/failed_recoverable` 接收用户继续消息，追加 `RunResumed` 并回到 `queued`；已在 `queued` 的重复 resume 是幂等 no-op |
| 等待审批时拒绝工具 | 回归测试通过 | 仅把当前动作记录为 `rejected`/`user_rejected`，写入工具观察结果并回到队列；模型循环继续，拒绝的工具不会执行 |
| 执行到一半停止，再输入新信息 | 回归测试通过 | 若当前 head 已停止，消息作为同一 Run 的恢复原因进入下一次模型上下文；若有活动子 Run，则排队为 follow-up，避免并发执行两个会话头 |
| 工具重复调用 | 回归测试通过 | 依据语义 action/idempotency key 去重；同一批重复动作在执行前被拦截，不产生第二次副作用 |
| 工具成功但状态/结果未落盘后崩溃 | 回归测试通过 | 恢复时标为 `outcome_unknown`，外部副作用不自动重放；只有幂等读取允许重试，写操作需要显式 reconciliation |
| 投影派生游标过期 | 全局扫描通过 | 从不可变事件重放并原子修复 `projection_digest`；真实事件序号错位、快照字节被篡改仍 fail-closed |
| 多窗口争抢项目 supervisor | 回归测试通过 | 竞争窗口进入 control-plane，消息/审批/恢复仍可落盘；右侧显示“其他窗口运行中”，原持有者退出或 TTL 过期后有界接管 |
| 浏览器保留失效会话 ID | HTTP 验证通过 | 返回 `404 session_not_found`，前端清理选择并停止重复轮询，不再产生连续 400 |

## 测试结果

- `tests/runtime_rebuild` + WebUI/SSE 回归：199 项通过；
- Fault-injection、投影兼容、租约、中断和 WebUI 重点回归：87 项通过，1 项真实模型门控测试按默认策略跳过；
- 上下文、记忆并发、记忆检查点、安全与沙箱相关选定测试：167 项通过；
- `ruff check cc_harness tests`、`compileall`、`git diff --check`：通过；
- `npm run build` 和 `scripts/build_webui.py`：通过；
- 非业务项目运行记录扫描：包含真实对话根 Run 及 follow-up child Run；投影重放失败 0 个；旧的派生 digest 仍按事件流自动修复。

## 仍需关注

1. 编码任务的真实模型有时只给自然语言而不提交完成 candidate；Runtime 正确拒绝“口头完成”，WebUI 已将纯对话单独建模，编码任务仍需通过验证门。
2. 本次正常冒烟输入很短，没有触发真实压缩；压缩卸载应另做大上下文压力测试，并核对前端 ring 与 `ContextCompacted` 事件。
3. 被强制终止的旧进程可能在租约 TTL 内显示“其他窗口运行中”，这是防止双调度的保护，不是新的 500 错误。
