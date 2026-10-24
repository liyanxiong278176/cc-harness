# Runtime 全方面评测与修复（2026-10-24）

## 结论

本轮针对 Durable Runtime 的持久化、ReAct 循环、计划 DAG、工具执行、审批/取消、supervisor 租约、Provider 流式调用、WebUI REST/SSE 做了故障扫描、针对性复现和回归验证。

已修复 4 类会直接打断正常任务执行的确定性问题：

1. **计划已完成但仍被判定“无可用节点”并 RunBlocked**。模型调用 `task_plan` 把最后一步标记 done 后，下一段被 `no dependency-ready plan node` 阻断；真实的 `你是谁` 会话因此从 completed 变成 blocked。
2. **模型返回非法工具 JSON 时整轮 RunFailed**。`KernelProtocolError` 直接作为 worker 未处理异常落成 `RunFailed`，模型没有机会改正。
3. **supervisor 崩溃后旧租约阻塞 40–120 秒**。新进程只能等 TTL，期间新消息一直排队，表现为“发送后长时间没反应”。
4. **Windows `NO_PROXY` 包含 `[::1]` 导致 httpx/OpenAI 客户端初始化失败**。新建会话/续聊会 500，错误为 `InvalidURL: Invalid port: ':1]'`。

另外把 Provider 瞬时连接失败的重试从 3 次提升到 4 次（1s/2s/4s 退避），并保留“一旦已有流式输出就不再重放”的安全边界。

## 历史故障扫描

扫描本机 `%LOCALAPPDATA%\cc-harness\projects` 下 140 个项目、184 个 Run 的 Durable 事件：

| 事件 | 数量 |
| --- | ---: |
| `RunBlocked` | 24 |
| `RunFailed` | 19 |
| `RunStalled` | 30 |
| `RunCancelled` | 19 |
| `RunPaused` | 12 |
| `RunOutcomeRecorded` | 78 |

Top 失败原因：

| 原因 | 次数 | 性质 |
| --- | ---: | --- |
| `action outcome is unknown`（含 after worker recovery / approved action） | 18 | 安全闸门：崩溃时无法确认副作用，需人工核对 |
| `provider_transport` / `APIConnectionError` | 13 | 外部网络/Provider |
| `no_progress` | 25 | 完成门：模型未给出可验证完成 |
| `no dependency-ready plan node` | 2 | 确定性 Bug（本轮修复） |
| `reasoning_content` thinking 重放错误 | 3 | Provider 协议兼容（已有 disabled-thinking 回退） |
| `tool arguments are not JSON` | 2 | 确定性 Bug（本轮改为可恢复观察） |
| `database is locked` | 1 | 多进程/并发写（本轮改为单 WebUI 进程运行） |
| `goal_requires_decision` | 3 | 审批/决策闸门，预期行为 |

## 修复明细

### 1. 计划完成后的假阻塞

`cc_harness/worker.py`：

- 新增 `_completed_plan_node_ids()` 统一计算已完成节点（todos + `PlanNodeCompleted`）。
- 当 `_ensure_plan_node_started()` 返回 `None` 时，只有“仍有未完成节点”才追加 `RunBlocked`；全部完成则允许进入下一段，由完成门决定最终结果。
- 回归测试：`tests/runtime_rebuild/test_worker_recovery.py::test_worker_allows_final_segment_after_plan_marks_every_node_done`。
- 真实会话验证：原本 blocked 的 `a2b9def4...` 调用 `/resume` 后约 6 秒变为 `completed`，输出正常身份回答，事件流无 `RunBlocked`。
- 新会话验证：在隔离临时目录发送 `你是谁`，状态 `queued → running → completed`，无 `RunBlocked`，正常输出身份回答。

### 2. 非法工具 JSON 转为可恢复观察

`cc_harness/run_kernel.py`：

- `ActionRequest` 新增 `protocol_error`。
- 非法 JSON / 非对象参数不再抛 `KernelProtocolError` 终止 Run，而是生成带 `_invalid_json` / `_invalid_arguments` 的只读请求，并设置 `protocol_error`。
- `cc_harness/worker.py`：遇到 `protocol_error` 时不调用工具执行器，直接提交 `status=failed, error_kind=invalid_tool_arguments, recovery=replan` 的 ToolObservation；模型下一段可重新发正确的 JSON。
- 缺少工具名的调用仍视为硬协议错误。
- 回归测试：`test_worker_does_not_fail_run_on_malformed_tool_arguments`，以及更新后的 kernel/fault-injection 契约测试。

### 3. 失主 supervisor 租约快速回收

`cc_harness/supervisor.py` / `cc_harness/run_store.py`：

- `supervisor_id` 写入 host + pid + 随机后缀。
- 新 supervisor 申请租约时，若 owner 是同机且 pid 已不存在，立即接管，不再等 120s TTL。
- 兼容旧格式：如果 owner 无 pid，但 `heartbeat_at` 超过 15s 未更新，也判定为失主。
- 回归测试：`tests/runtime_rebuild/test_supervisor.py`、`test_three_layer_leases.py`。
- 实测续聊 POST 从约 3s（启动 supervisor）降到 109ms 级；失主恢复从 40s 级降到立即接管。

### 4. `NO_PROXY=[::1]` 导致客户端初始化失败

新增 `cc_harness/net_env.py`：

- `sanitize_no_proxy_env()` 去掉 httpx 无法解析的方括号 IPv6 token。
- `make_async_openai()` 统一构造客户端，`llm.py`、`capability_services.py`、`repl.py` 全部改用该入口，避免再出现“某个入口能跑、另一个入口 500”。
- 回归测试：`test_runtime_compatibility_smoke`、`test_discovery_and_shared_capabilities`。

### 5. Provider 瞬时失败重试

`cc_harness/llm.py`：

- 连接类错误最多 4 次尝试，退避 1s / 2s / 4s。
- 已有 content/tool delta 后仍不重放，避免重复文本/重复副作用。

## 验证结果

Runtime 聚焦测试（全部通过）：

```powershell
python -m pytest tests/runtime_rebuild tests/test_webui.py tests/test_llm.py tests/test_live_stream.py tests/test_stream_errors.py -q
python -m pytest tests/fault_injection -q
```

包含：Run store / projection、lease（run / resource / supervisor）、supervisor、worker recovery、plan graph、审批、follow-up、WebUI REST/SSE、LLM retry、live stream、fault injection（真实 provider probe 默认跳过）。

全量测试基线：

```powershell
python -m pytest tests/ --ignore=tests/test_agentdojo_adapter.py -q
```

全量收集仍会命中 legacy in-process `dispatch_subagent` 与 REPL 完成门相关的旧用例；这些用例断言的是已主动关闭的 legacy 路径（报错为 `legacy in-process dispatch is disabled`），与 WebUI Durable Runtime 无关。`tests/test_agentdojo_adapter.py` 需要可选的 `agentdojo` 依赖，未安装时在收集阶段即报 `ModuleNotFoundError`，因此从 runtime 评测命令中排除。

前端：

```powershell
cd web; npm test; npm run build
```

10 个 reducer/live 测试通过，生产构建通过。

真实链路：

- 新会话首轮：POST 约 300ms，首条 assistant 约 4–6s（取决于 provider）。
- 多轮追问：POST 109ms，乐观气泡 16–148ms。
- 计划：`task_plan` 运行中约 6s 出现 3 个步骤，状态实时更新；刷新后步骤与完成状态保留。
- 会话切换：标题约 41ms，内容约 680ms。
- 页面刷新：骨架 59–180ms，首条 assistant 413–852ms（有 `sessionStorage` 缓存时 69ms）。

## 剩余风险（不是本轮可静默修复的确定性 Bug）

1. **Provider 网络/余额**：`APIConnectionError`、402 Insufficient Balance 等外部失败无法在 Runtime 内消除；已增加有界重试，最终失败会保留为 recoverable。
2. **`action outcome is unknown`**：进程在写文件/执行命令期间崩溃时，Runtime 无法证明副作用是否发生，因此安全地 Blocked。正确恢复需要操作者确认“已执行”或“未执行”；底层已有 `RunCoordinator.reconcile_action`，但 WebUI 尚未提供按钮，当前需 CLI/API 处理。
3. **`no_progress`**：模型没有给出可验证完成时的完成门行为，会话进入 stalled/paused 而不是崩溃；用户可以继续对话。
4. **历史遗留测试**：`tests/test_d1_*` / `tests/test_repl.py` 等仍在断言已主动关闭的 legacy in-process `dispatch_subagent` 行为，和 WebUI Durable Runtime 路径无关；本轮没有改动这些语义。
5. **`database is locked`**：历史上多 WebUI/supervisor 进程并存时会触发；现在按单 WebUI 进程运行，并保留 30s busy_timeout。多进程部署仍需避免共享同一项目库的写竞争。

## 复现命令

```powershell
# 聚焦 runtime 回归
python -m pytest tests/runtime_rebuild tests/test_webui.py tests/test_llm.py tests/test_live_stream.py tests/test_stream_errors.py -q

# 计划完成 / 非法 JSON 两个新增回归
python -m pytest tests/runtime_rebuild/test_worker_recovery.py -q

# 历史失败扫描（只读本机 durable store）
python scripts/runtime_failure_scan.py
```

`scripts/runtime_failure_scan.py` 只读扫描，不修改任何 Run。
