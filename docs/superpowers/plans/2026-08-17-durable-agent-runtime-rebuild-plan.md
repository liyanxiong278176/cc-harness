# Durable Agent Runtime 重建实施计划

> **执行约束**：这是一次 Clean Runtime Rebuild。实现期间可以在新目录中并行开发和测试，但不得在生产运行中让旧 runtime 与新 runtime 双写或共同拥有状态权威。未完成最后 cutover 前，旧运行时继续作为当前产品；cutover 时停止旧 runtime、备份并导入数据、启动新 runtime。

## 目标

以 [`docs/specs/2026-08-17-durable-agent-runtime-rebuild.md`](../../specs/2026-08-17-durable-agent-runtime-rebuild.md) 为唯一实现规格，交付一个本机 Durable Agent Runtime，并通过不可抵消 Runtime Rebuild Release Gate 后一次性替换现有运行时。

## 不变约束

- 不使用 `git reset --hard`、`git checkout --` 或递归删除工作区。
- 当前 dirty worktree 属于用户；所有实现任务先检查并保留已有修改。
- 不改写历史 benchmark evidence、冻结数据集、旧评测结果或其 result root。
- 新 runtime 的事件、状态和凭证数据不写入 child worktree 自己的独立 store。
- 不向 `run_turn()` 继续添加长期参数；它只作为迁移阶段的旧 implementation。
- 没有总 token、费用、调用次数和运行时长硬上限；只实现操作安全、停滞检测、动作超时和并发保护。

## 目标文件地图

### 新建 Module

| 文件 | 责任 | 外部接口 |
|---|---|---|
| `cc_harness/run_model.py` | Run、GoalContract、PlanGraph、Action、Lease、Approval、Projection 数据模型 | frozen dataclasses / enums |
| `cc_harness/run_events.py` | event envelope、事件类型、schema 校验、合法状态转换 | `EventCodec`, `EventValidator` |
| `cc_harness/run_store.py` | SQLite/WAL 运行存储、append、snapshot、事务和 object 引用 | `RunStore` |
| `cc_harness/artifacts.py` | 内容寻址对象、原子写、digest、引用和 GC | `ArtifactStore` |
| `cc_harness/run_projection.py` | 从事件重建 RunProjection、Todo/plan/working state 视图 | `ProjectionBuilder` |
| `cc_harness/lease.py` | worker lease、epoch、heartbeat、过期 fencing | `LeaseManager` |
| `cc_harness/action_contracts.py` | Tool Recovery Contract、retry/reconcile/cancel capability | `ToolRecoveryContract` |
| `cc_harness/run_kernel.py` | 无持久化副作用的 ReAct/segment 内核 | `AgentKernel.execute_segment()` |
| `cc_harness/supervisor.py` | 本机单写 supervisor、queue、worker 调度、恢复 | `LocalSupervisor` |
| `cc_harness/worker.py` | worker 进程、lease、segment 执行和 heartbeat | `RunWorker` |
| `cc_harness/ipc.py` | 本机客户端与 supervisor 的受认证命令/事件通道 | `CoordinatorTransport` |
| `cc_harness/coordinator.py` | REPL/headless 入口使用的深接口 | `RunCoordinator` |
| `cc_harness/approvals.py` | persistent approval 生命周期和参数 scope | `ApprovalService` |
| `cc_harness/followups.py` | Follow-up Queue、Predecessor Gate、interrupt | `FollowUpService` |
| `cc_harness/worktrees.py` | child worktree、candidate commit、父级集成 | `WorktreeManager` |
| `cc_harness/legacy_import.py` | 旧 session/Todo/memory/checkpoint/action journal 导入 | `LegacyImporter` |
| `cc_harness/runtime_contract.py` | pinned runtime/tool/model/policy digest | `RuntimeContract` |

### 重写或降级的既有 Module

| 文件 | 动作 |
|---|---|
| `cc_harness/fact_store.py` | 迁移实现到 `run_store.py`；迁移结束后不保留第二套 event-store implementation |
| `cc_harness/session_store.py` | 只保留读取/导入 adapter；新 runtime 不调用其 save path |
| `cc_harness/loop_control.py` | 拆出纯函数和控制策略，改为 Kernel/Projection 的内部 seam |
| `cc_harness/agent.py` | 把 ReAct 执行逻辑迁入 `run_kernel.py`，旧入口在 cutover 后移除或仅供 legacy tests |
| `cc_harness/runtime.py` | 改为 coordinator/client façade，不直接保存 messages 作为权威状态 |
| `cc_harness/entrypoint.py` | 启动 supervisor/client；增加 status/approve/interrupt/resume/rollback 命令 |
| `cc_harness/repl.py` | 改为 `RunCoordinator` client；输入默认入队，不直接运行 loop |
| `cc_harness/native_tools.py` | 为所有 first-party tool 补恢复契约和 action adapter |
| `cc_harness/mcp_client.py` | 将 MCP tool metadata 转成 contract；缺失 metadata 走最保守默认值 |
| `cc_harness/credential_broker.py` | 从 sandbox credential provisioning 扩展为 action-scoped capability broker |
| `cc_harness/project/service.py` | Todo 写入改成 command/event adapter，Todo 视图由 projection 产生 |
| `cc_harness/memory/*` | 保留存储能力，memory recall 输出带 provenance 的 Advisory Memory Evidence |

### 旧文件最终处理

只有在新 runtime 通过全部 release gates 后才允许删除/重命名旧生产入口。删除动作必须在 cutover checklist 中列出精确目标，并先完成只读备份。

## 阶段 0：冻结基线与迁移样本

### 任务 0.1：建立重建基线

**Files:**

- Create: `tests/runtime_rebuild/conftest.py`
- Create: `tests/runtime_rebuild/fixtures/legacy/`
- Create: `docs/runtime-rebuild-baseline.md`
- Do not modify: historical `eval/` evidence

步骤：

1. 记录当前 `git status --short`、Python、依赖、平台和模型配置摘要。
2. 为旧 session、Todo、memory、checkpoint、action journal 建立最小、冲突、损坏和大对象 fixture。
3. 记录每个 fixture 的 SHA-256 和预期导入事实。
4. 跑现有核心回归，保存结果为 baseline evidence；baseline 失败不被新 runtime 的结果覆盖。

命令：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_agent.py tests/test_runtime.py tests/test_session_store.py -q
.venv/Scripts/python.exe -m ruff check cc_harness tests
```

验收：baseline、fixture manifest 和环境摘要可重现；不修改既有 benchmark evidence。

## 阶段 1：Run Domain 与不可变事件

### 任务 1.1：模型和状态机

**Files:**

- Create: `cc_harness/run_model.py`
- Create: `tests/runtime_rebuild/test_run_model.py`
- Create: `tests/runtime_rebuild/test_run_state_machine.py`

实现：

- Run 状态、Action 状态、Child 状态、Approval 状态、Predecessor Gate 状态；
- GoalContract、PlanGraph、CandidateChangeSet、RuntimeContract；
- 结构化 `RunProgress` 和 `EvidenceRef`；
- 不允许 `completed` 绕过完成证据；
- `budget_exhausted` 不进入主状态枚举；
- `outcome_unknown` 只表示 action attempt 不明。

测试：覆盖所有合法/非法状态转换、取消、审批、前序门和 completed 拒绝条件。

### 任务 1.2：事件 codec 和合法转换

**Files:**

- Create: `cc_harness/run_events.py`
- Create: `tests/runtime_rebuild/test_run_events.py`

实现：

- 固定 envelope 字段；
- payload schema version；
- event ID、run sequence、causation/correlation；
- event type 到状态转换的白名单；
- 过期 lease、错误 sequence、旧 runtime contract 拒绝；
- secret redaction 不能替代 payload schema 验证。

测试：重复 event ID、序号跳跃、旧 schema、未知事件、非法状态变化、过期 lease 和参数篡改。

### 任务 1.3：Projection rebuild

**Files:**

- Create: `cc_harness/run_projection.py`
- Create: `tests/runtime_rebuild/test_projection_rebuild.py`

实现：

- 从空状态重放全部事件；
- 从 snapshot 重放增量事件；
- 生成 Run、Todo、Plan、WorkingState、Approval、Queue 视图；
- projection 不改变事件；
- 任意 snapshot 删除后可从事件重建相同 digest。

验收命令：

```powershell
.venv/Scripts/python.exe -m pytest tests/runtime_rebuild/test_run_model.py tests/runtime_rebuild/test_run_state_machine.py tests/runtime_rebuild/test_run_events.py tests/runtime_rebuild/test_projection_rebuild.py -q
```

## 阶段 2：Local Run Store 与 artifacts

### 任务 2.1：把 `fact_store` 提升为 RunStore

**Files:**

- Create: `cc_harness/run_store.py`
- Refactor: `cc_harness/fact_store.py`
- Create: `tests/runtime_rebuild/test_run_store.py`

实现：

- SQLite WAL、foreign keys、busy timeout；
- event append 的 expected sequence；
- immutable trigger；
- run metadata、lease、approval、queue、action attempt、snapshot 表；
- 单逻辑 writer；
- `append + projection cursor + snapshot metadata` 的事务；
- store location 按 project identity 稳定解析，不随 child worktree 变化；
- 保留现有 ADR 0019 的用户数据目录约束。

测试：并发 reader、单 writer、事务回滚、事件不可修改/删除、重启恢复、snapshot rebuild、不同 worktree 指向同一 project store。

### 任务 2.2：内容寻址对象库

**Files:**

- Create: `cc_harness/artifacts.py`
- Create: `tests/runtime_rebuild/test_artifacts.py`

实现：

1. 临时文件写入。
2. fsync/flush。
3. SHA-256 校验。
4. 原子 rename。
5. 事件提交 object reference。
6. orphan grace-period GC。

测试：崩溃模拟、重复内容去重、digest 不匹配、对象不存在、垃圾回收保护被引用对象。

### 任务 2.3：动作 journal 迁移

**Files:**

- Refactor: `cc_harness/loop_control.py`
- Create: `tests/runtime_rebuild/test_action_recovery.py`

将当前 JSONL ActionJournal 的语义转成 Run Event：

- started 前的 intent；
- result object first；
- success/failure/unknown terminal event；
- 不再有只写 journal、数据库不知道的第二事实源。

## 阶段 3：Kernel seam 与现有 ReAct 迁移

### 任务 3.1：建立 `AgentKernel` 小接口

**Files:**

- Create: `cc_harness/run_kernel.py`
- Create: `tests/runtime_rebuild/test_kernel_contract.py`
- Refactor: `cc_harness/agent.py`

把当前 `run_turn()` 内部逻辑拆为以下内部模块：

- prompt/context projection；
- one model segment；
- tool planning；
- action execution；
- event emission；
- completion candidate；
- progress/stall observation。

外部只保留：

```python
await AgentKernel.execute_segment(SegmentContext) -> SegmentOutcome
```

`SegmentContext` 使用一个 dataclass，不再暴露 30+ keyword 参数。

测试：fake LLM、fake tool adapter、fake event sink、fake projection；验证 Kernel 不直接写 SQLite、不会直接读取 REPL input、不会自行变更 Run 状态。

### 任务 3.2：Context 和 memory 接入

**Files:**

- Refactor: `cc_harness/context.py`
- Refactor: `cc_harness/memory/recall.py`, `cc_harness/memory/capture.py`
- Create: `tests/runtime_rebuild/test_context_projection_contract.py`

实现：

- Goal Contract 和 pinned facts 进入 projection 的受保护区；
- tool results 通过 artifact refs/offload 投影；
- memory 只注入 source/evidence/confidence 完整的 Advisory Memory Evidence；
- compaction 不改变原始事件；
- 恢复后 projection digest 可复现。

## 阶段 4：Tool Recovery Contract、审批与凭证

### 任务 4.1：工具契约和调度器

**Files:**

- Create: `cc_harness/action_contracts.py`
- Refactor: `cc_harness/native_tools.py`, `cc_harness/mcp_client.py`
- Create: `tests/runtime_rebuild/test_tool_contracts.py`
- Create: `tests/runtime_rebuild/test_action_scheduler.py`

为 `Read/Write/Edit/Glob/Grep/run_command` 补 first-party contract。MCP 工具没有可信 contract 时按 unknown 最保守策略。

调度器只依据 contract 判断：

- read-only batch；
- serial mutation；
- approval；
- retry；
- reconcile；
- cancel；
- child 可用性。

测试：工具名称伪装、schema 有但 contract 缺失、同一 action retry、read 并行、mutation 串行、unknown 工具默认拒绝自动重试。

### 任务 4.2：Action Attempt 与 `outcome_unknown`

**Files:**

- Refactor: `cc_harness/executor.py`, `cc_harness/sandbox.py`
- Create: `tests/runtime_rebuild/test_outcome_unknown.py`

实现：

- dispatch 前持久化 intent；
- dispatch 后记录 started；
- 进程崩溃恢复扫描未完成 attempt；
- read-only 安全重试；
- idempotency key 复用同一 key；
- 不可对账外部副作用进入 unknown；
- reconcile adapter 可把 unknown 变成 succeeded/failed；
- cancel 不等于 side effect reverted。

### 任务 4.3：Persistent Approval 和 capability broker

**Files:**

- Create: `cc_harness/approvals.py`
- Refactor: `cc_harness/credential_broker.py`, `cc_harness/policy.py`
- Create: `tests/runtime_rebuild/test_approvals.py`
- Create: `tests/runtime_rebuild/test_action_capabilities.py`

实现：

- approval 持久化具体参数和 scope；
- worker 释放 lease 等待审批；
- 参数变化必须重新审批；
- hard-deny 永不能覆盖；
- 模型请求优先 supervisor 代理；
- child 不继承父凭证；
- credential material 不进入 event/object/log。

## 阶段 5：Supervisor、Worker、IPC

### 任务 5.1：Lease manager

**Files:**

- Create: `cc_harness/lease.py`
- Create: `tests/runtime_rebuild/test_leases.py`

实现：claim、renew、expire、fence、reclaim。事件写入必须携带 lease epoch。

测试：双 worker claim、旧 epoch 写入、heartbeat 延迟、进程退出、机器重启后的 reclaim。

### 任务 5.2：Supervisor 和 worker

**Files:**

- Create: `cc_harness/supervisor.py`
- Create: `cc_harness/worker.py`
- Create: `tests/runtime_rebuild/test_supervisor.py`
- Create: `tests/runtime_rebuild/test_worker_recovery.py`

实现：

- 本机一个 supervisor；
- worker 只通过 coordinator/store seam 工作；
- worker 启动时读取 Pinned Run Contract；
- 每个 segment 后写 checkpoint/event；
- heartbeat 和 lease reclaim；
- 等待 approval 时释放 worker；
- worker 崩溃后只恢复可安全 segment；
- 并发 child 默认最多三个；
- 无硬总预算，但保留单动作 timeout、rate、storage、stall 和安全熔断。

### 任务 5.3：本机 IPC 和客户端 coordinator

**Files:**

- Create: `cc_harness/ipc.py`
- Create: `cc_harness/coordinator.py`
- Refactor: `cc_harness/entrypoint.py`, `cc_harness/runtime.py`, `cc_harness/repl.py`
- Create: `tests/runtime_rebuild/test_coordinator_ipc.py`

命令必须支持：

```text
run / submit
status
list
attach
approve
reject
interrupt
cancel
resume
follow-up
rollback
```

REPL 新输入默认走 `follow-up`，不直接调用 Kernel。`interrupt` 和 `cancel` 是显式控制命令。

## 阶段 6：Goal、Plan Graph、Follow-up 和 child integration

### 任务 6.1：Goal Contract 和计划图

**Files:**

- Create: `cc_harness/goals.py`
- Create: `cc_harness/plan_graph.py`
- Refactor: `cc_harness/project/service.py`, `cc_harness/project/tools.py`
- Create: `tests/runtime_rebuild/test_goal_contract.py`
- Create: `tests/runtime_rebuild/test_plan_graph.py`

实现：

- 明确任务自动 accept；
- 关键歧义/高风险阻塞；
- 依赖 DAG、文件 ownership、depth=2；
- Todo 是 projection/command adapter，不是第二权威；
- plan revision 追加事件；
- child done 只有候选完成语义。

### 任务 6.2：Follow-up Queue 和 Predecessor Gate

**Files:**

- Create: `cc_harness/followups.py`
- Create: `tests/runtime_rebuild/test_followups.py`

覆盖规格中的所有前序状态矩阵。普通消息永远排队；取消前序后自动启动并注入成果不完整事实；blocked/approval/stalled/failed 默认等待。

### 任务 6.3：Worktree 与 Candidate Change Set

**Files:**

- Create: `cc_harness/worktrees.py`
- Create: `tests/runtime_rebuild/test_worktrees.py`
- Create: `tests/runtime_rebuild/test_candidate_integration.py`

实现：

- 固定 base commit；
- child worktree/branch；
- file ownership 检查；
- child commit + diff digest + verification evidence；
- 父 integration worktree 显式 accept/reject；
- 冲突生成 integration task；
- 父集成后重新验证；
- 非 Git 降级串行并记录 isolation unavailable。

## 阶段 7：Legacy import 与一次性切换

### 任务 7.1：幂等导入器

**Files:**

- Create: `cc_harness/legacy_import.py`
- Create: `tests/runtime_rebuild/test_legacy_import.py`
- Create: `tests/runtime_rebuild/fixtures/legacy/expected.json`

导入来源：

- `SessionStore` sessions/messages/checkpoints；
- project Todo YAML/Markdown；
- memory store atoms and metadata；
- legacy action journal；
- context offload refs。

每个 source 记录 source digest、importer version、import status、unverified claims 和 artifact refs。重复执行不重复事件；中途失败可以从最后一个 source checkpoint 继续。

### 任务 7.2：旧/新数据对账

**Files:**

- Create: `scripts/check_runtime_rebuild_migration.py`
- Create: `tests/runtime_rebuild/test_migration_reconciliation.py`

对账：session identity、消息 digest、Todo status、checkpoint labels、artifact digest、memory provenance、action count。任何不可证明内容输出 `legacy_unverified`，不伪造验证事件。

### 任务 7.3：Pinned Run Contract 和 runtime migration

**Files:**

- Create: `cc_harness/runtime_contract.py`
- Create: `tests/runtime_rebuild/test_pinned_runtime_contract.py`

旧 worker、旧 schema、旧 tool contract digest 写入必须被拒绝。可恢复点迁移追加 `RunRuntimeMigrated`，失败保留旧备份和 run blocked 状态。

## 阶段 8：Kernel 迁移后的客户端切换

### 任务 8.1：新入口接入

**Files:**

- Refactor: `cc_harness/entrypoint.py`
- Refactor: `cc_harness/runtime.py`
- Refactor: `cc_harness/repl.py`
- Create: `tests/runtime_rebuild/test_new_entrypoint.py`

启动顺序：

1. 解析 project identity 和 user data store；
2. 连接或启动 supervisor；
3. 读取 active runs、approvals、follow-up queue；
4. 绑定客户端；
5. 只通过 `RunCoordinator` 操作；
6. 关闭终端不取消 run。

实现补充：每个 worker segment 完成后追加 `RunYielded` 并回到 `queued`，由
supervisor 领取下一段；`--command supervisor` 是可独立于终端运行的本机 worker
服务入口。

### 任务 8.2：旧入口封存

只有任务 9 全部通过后，才执行：

- 移除 `run_turn()` 生产调用；
- `SessionRuntime` 变成 coordinator facade；
- `SessionStore.save()` 不再由新 runtime 调用；
- 旧实现移动到明确的 `legacy` namespace 或删除；
- 更新 CLI help、release notes 和 migration guide。

## 阶段 9：非补偿发布门

### 任务 9.1：崩溃和恢复矩阵

**Files:**

- Create: `tests/runtime_rebuild/test_crash_matrix.py`
- Create: `scripts/run_runtime_rebuild_gate.py`

至少注入：intent 前、started 后、object 完整后、event commit 中、projection 更新后、snapshot 后、lease 过期、approval 创建后、child commit 后、父级接纳前。

每个样本检查：事件完整性、动作是否重复、对象 digest、最终 projection、worker fencing、run 状态。

### 任务 9.2：安全和工具契约门

**Files:**

- Create: `tests/runtime_rebuild/test_security_gate.py`
- Extend: `tests/test_policy.py`, `tests/test_agent_security.py`

覆盖：

- 未授权凭证不可达；
- child 不继承父凭证；
- approval scope 参数篡改；
- hard-deny 绕过；
- unknown MCP tool 不自动 replay；
- 外部副作用 unknown；
- secret 不进入 event/object/log/diff。

### 任务 9.3：跨平台和并行门

**Files:**

- Create: `tests/runtime_rebuild/test_cross_platform_contract.py`
- Extend: `.github/workflows/` 中的受控 runtime rebuild job

Windows、Linux、macOS 验证 supervisor lifecycle、IPC、SQLite/WAL、worktree、lease reclaim 和 graceful shutdown。平台差异通过 adapter 隔离，不在 Kernel 内散落。

### 任务 9.4：冻结对标与非退化

复用现有冻结 benchmark contract，创建新的 rebuild result root，不覆盖旧 evidence。比较：

- coding success；
- completion evidence validity；
- recovery correctness；
- duplicated side effects；
- child integration correctness；
- latency、calls、tokens、cost；
- safety and data-loss failures。

安全、数据损坏、恢复和权限门独立报告，不以成功率综合抵消。

## 阶段 10：Cutover / Rollback

### Cutover 顺序

1. 停止接受新的旧 runtime run。
2. 等待旧 runtime 到可恢复点，或记录未完成 action 为 legacy unknown。
3. 停止旧 supervisor/REPL writer。
4. 使用 SQLite backup API 备份旧 store。
5. 生成 object manifest 和完整 source digest。
6. 创建 new runtime store。
7. 运行一次性 legacy importer。
8. 执行 migration reconciliation。
9. 写入 `LegacyRunImported` 和 migration completion record。
10. 启动新 supervisor。
11. 只读验证 active run、approval、queue、memory 和 artifacts。
12. 开放新 coordinator 客户端。
13. 运行 post-cutover smoke 和 release gate。

### Rollback 条件

- import reconciliation 失败；
- 任一硬安全门失败；
- 旧 worker 能写新 store；
- 崩溃矩阵出现重复/遗漏副作用；
- approval 或 hard-deny 绕过；
- event/projection rebuild 不一致；
- 跨平台核心启动失败。

### Rollback 顺序

1. 停止新 supervisor 和新 writer。
2. 保存失败事件、日志、object manifest 和 gate report。
3. 恢复旧程序入口。
4. 恢复迁移前只读备份到新的临时恢复位置。
5. 不删除新 store；保留用于诊断。
6. 报告受影响 run 和人工需要确认的 external outcome unknown。

## 执行顺序与并行策略

严格依赖链：

```text
0 baseline
  ↓
1 domain/events
  ↓
2 store/artifacts
  ↓
3 kernel seam
  ↓
4 tool/recovery/security
  ↓
5 supervisor/worker/IPC
  ↓
6 goals/queue/worktree
  ↓
7 migration/contracts
  ↓
8 client cutover
  ↓
9 release gates
  ↓
10 one-time cutover
```

可以并行的实现工作：

- 1.1 与 1.2；
- 2.1 与 2.2；
- 4.1 与 4.3；
- 6.1、6.2 与 7.1（只要依赖模型已固定）；
- 9.1、9.2、9.3 可并行执行，但 cutover 必须等待全部通过。

不可并行：

- store schema 与 projection 未定前不能写 supervisor；
- kernel seam 未定前不能改 entrypoint；
- worktree integration 未通过不能启用 child 写入；
- legacy import 未通过不能 cutover；
- release gate 未通过不能删除旧入口。

## 每个任务的完成格式

每个实现任务必须提交：

1. failing test 或明确的 contract test；
2. implementation；
3. focused test pass；
4. 邻近回归结果；
5. 事件/schema 变更说明；
6. 若影响迁移，更新 importer/reconciliation fixture；
7. 若影响安全，更新对应 gate；
8. 运行进展和剩余风险记录。

## 最终交付清单

- [x] 新规格中的所有 public seam 有实现和 contract test。
- [x] 新 runtime 不以 messages/Todo/checkpoint 之一作为权威状态。
- [x] `run_turn()` 不再是产品执行入口。
- [x] 本机 supervisor/worker 可在终端关闭后继续。
- [x] worker lease 过期后旧 worker 无法写入。
- [x] ordinary follow-up 自动排队，explicit interrupt 可取消。
- [x] child worktree candidate commit 可父级接纳和重新验证。
- [x] outcome_unknown 不被盲重放。
- [x] 无总预算硬停止，停滞和操作安全限制仍有效。
- [x] legacy 数据可幂等导入并可审计。
- [x] release gates 全部通过。
- [x] cutover 和 rollback rehearsal 完成。
- [x] 用户可用简历陈述中的性能/可靠性数字都有保留证据。

## 完成验证记录（2026-08-18）

- `tests/runtime_rebuild`：全部通过；`ruff check cc_harness tests scripts`：全部通过。
- 全量 `tests`（排除既有 `tests/test_generate_attacks.py` 环境格式基线失败）：全部通过；该基线失败与重建代码无关。
- 真实项目迁移 dry-run：全部旧来源无错误；10 个 legacy run 已导入且默认 `blocked`，等待显式 `resume`。
- 真实 cutover：完成 SQLite 备份、对象清单、幂等导入和对账；旧来源文件保留。
- 数字简历表述仅使用已保存的 gate/test/recovery 证据；未将未执行的 live benchmark 性能或费用当作结论。

## 推荐执行命令

```powershell
# 每阶段 focused tests
.venv/Scripts/python.exe -m pytest tests/runtime_rebuild -q

# 全量回归（cutover 前）
.venv/Scripts/python.exe -m pytest tests -q

# 静态检查
.venv/Scripts/python.exe -m ruff check cc_harness tests scripts

# 运行重建发布门
.venv/Scripts/python.exe scripts/run_runtime_rebuild_gate.py --all

# 迁移 dry-run（不改 production store）
.venv/Scripts/python.exe scripts/check_runtime_rebuild_migration.py --dry-run --fixture tests/runtime_rebuild/fixtures/legacy
```

实现者每完成一个阶段都必须保存 focused test、gate evidence 和未解决风险；不能用“代码已经合并”替代验证证据。
