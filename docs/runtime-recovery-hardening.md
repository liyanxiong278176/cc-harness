# Durable Runtime recovery hardening

这份说明对应 Runtime 的三类异常边界：进程/任务中断、工具结果未落盘、以及分层记忆读取。事件流仍是唯一事实源；checkpoint 只是可重建的加速器。

## 1. 中断、超时与 Supervisor 竞态

- `RunWorker.execute()` 为模型段和每个工具动作建立独立 watchdog。默认上限为 300 秒，可用 `CC_HARNESS_MODEL_TIMEOUT_SECONDS` 与 `CC_HARNESS_ACTION_TIMEOUT_SECONDS` 调整；设为 `0` 才表示显式关闭该层 watchdog。
- Ctrl+C 先追加 `InterruptRequested`。watchdog 轮询同一 Run 的权威 projection，取消模型/工具任务；已经越过 `ActionStarted` 但没有观察结果的动作写成 `ActionOutcomeUnknown`，然后才写 `RunCancelled`，所以取消不会被误报为外部副作用已撤销。
- Worker 被进程终止时，Run lease 的 fencing 和 Supervisor 的回收扫描会把无 lease 的 `RUNNING` Run 追加为 `WorkerLeaseExpired`，使它回到队列而不覆盖新 Worker 的事件。下一次 Supervisor tick 或应用重启即可从最后一个 checkpoint 继续。
- `project_supervisor_lease` 只做调度器选主；`run_lease` 只属于一个 Run。一个 Supervisor 可以并发调度多个同目录会话，文件/工作区冲突由 `run_resource_lease` 在动作边界处理。
- 启动 detached Supervisor 使用 `supervisor.start.lock` 的 `O_CREAT|O_EXCL` 原子占用，并以原子替换发布 `supervisor.pid`，避免多个 WebUI/TUI 客户端的 check-then-start 竞态。旧锁只在持有者进程已死亡时清理。

## 2. 工具动作幂等与对账

每个 `ActionRequest` 有稳定的 `idempotency_key`：优先使用 provider/API 提供的显式 key，否则由工具名、规范化参数摘要和 effect class 计算。`action_id` 只代表一次模型消息，崩溃重试可以变化；幂等键不会变化。规范化摘要排除内部 key 字段，参数顺序变化不会制造重复动作。

动作生命周期始终是：

```text
ActionPlanned → ActionPrepared → ActionStarted
  → ToolObservationCommitted → ActionSucceeded/ActionFailed
```

恢复时：

- 已有观察但缺终态：只补写终态，不再调用工具；
- 已开始却没有观察：只对声明为可重试且幂等的只读工具安全重试；写入、未知或外部副作用统一写 `ActionOutcomeUnknown` 并阻塞；
- 模型生成新 `action_id` 但语义相同：同一 Run 内按 `idempotency_key` 识别并阻止重复执行；同一模型批次内也做本地 key 去重；
- 外部系统的最终结果由 `RunCoordinator.reconcile_action()` 追加 `ReconciliationStarted/Resolved`，只能把 `outcome_unknown` 显式解析为 `succeeded` 或 `failed`，不会盲目重放；即使 Run 已进入 `cancelled`、`stalled` 或 `failed_recoverable`，仍可在这个恢复边界完成对账，之后再由用户决定是否继续。

观察 artifact 先写对象存储、后写事件是故意的。进程在两步之间退出时，`RunStore.collect_artifact_garbage()` 从不可变事件和 snapshot 重新计算根，只删除超过宽限期且未引用的对象，并清理中断留下的 `.tmp-*` 文件。

## 3. L3 优先的按需记忆

自动 `build_context()` 只注入 L3 persona 快照（并记录 `l3_snapshot` 和 fingerprint），不会把 L2/L1/L0 直接塞进每一轮上下文。

显式 `memory_recall` 使用渐进路径：

```text
L3 persona → L2 scenario → L1 semantic/FTS atom → L0 conversation
```

遇到第一个有用层就停止，并返回实际访问过的 `layers` 与 `next_layer`；没有命中才继续向下。L0 使用带参数绑定的 SQLite 查询，支持按 session 限定，查询文本永远不会拼进 SQL。

## 验证

`tests/runtime_rebuild/test_runtime_hardening.py` 覆盖：幂等键稳定性、同一批次去重、模型 watchdog、Ctrl+C 快速取消、最新 attempt 对账、L3→L0 渐进召回和事件引用感知的 artifact GC。
