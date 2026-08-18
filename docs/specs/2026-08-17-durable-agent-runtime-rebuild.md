# Durable Agent Runtime 重建规格

状态：已确认设计，待实现

日期：2026-08-17

## 1. 目的

把 cc-harness 从“由终端进程驱动的增强型 ReAct loop”重建为一个本机可持久、可恢复、可审计、可并行的 Durable Agent Runtime。

重建后的系统应能让一个 coding 任务：

- 脱离 REPL 继续运行；
- 在进程崩溃、终端关闭和机器重启后恢复；
- 在副作用结果不明时避免盲目重放；
- 通过隔离 worktree 并行执行 child run；
- 由父运行显式接纳子代理候选变更；
- 在没有总 token、费用或时长硬上限的情况下持续推进；
- 没有可证明进展时停滞并释放 worker；
- 只凭结构化目标、变更、验证和审批证据进入 completed。

## 2. 范围

### 2.1 本次重建包含

- provider-neutral 的 Durable Agent Run 内核；
- append-only Run Event 与可重建 projection；
- 本机 supervisor、worker、heartbeat 和 lease fencing；
- Goal Contract、Run Plan Graph、Follow-up Queue；
- persistent approval 与 explicit interrupt；
- Tool Recovery Contract、Outcome Unknown 和 reconciliation；
- Git worktree child run、candidate commit 和父级接纳；
- action-scoped credential broker；
- advisory memory evidence；
- legacy session/Todo/memory/checkpoint 的一次性幂等导入；
- 崩溃注入、迁移、恢复、安全和跨平台发布门。

### 2.2 明确不包含

- Web、移动端、云端 supervisor 或跨机器 worker；
- 复制 Claude Code 或 Codex 的品牌、界面和产品平台；
- 为旧 `run_turn()` 内部调用者保留长期兼容层；
- 用 benchmark 分数抵消恢复、安全或数据完整性失败；
- 将 memory 命中、模型最终文本或 Todo `done` 单独作为完成证据。

## 3. 现有代码基线与重建边界

当前项目已经有以下可复用实现：

| 现有模块 | 重建中的处理 |
|---|---|
| `cc_harness/fact_store.py` | 提升为 Run Store 的实现基础；补齐 run、lease、approval、queue、action 和 projection 数据模型 |
| `cc_harness/session_store.py` | 降为 legacy import adapter；不再作为新运行权威 |
| `cc_harness/loop_control.py` | 拆出并保留完成门、WorkingState、RecoveryPolicy、StallController 的纯逻辑；ActionJournal 接入 Run Event |
| `cc_harness/agent.py` | 不再作为持久化和调度入口；其 ReAct 细节下沉为 Agent Kernel 内部实现 |
| `cc_harness/runtime.py` | 重建为客户端 façade + supervisor 生命周期适配，不再直接拥有执行状态 |
| `cc_harness/native_tools.py` | 继续作为 first-party tool adapter，并补 Tool Recovery Contract |
| `cc_harness/mcp_client.py` | 继续作为 MCP transport adapter；没有可信恢复契约的 MCP 工具按最保守策略处理 |
| `cc_harness/credential_broker.py` | 扩展为 action-scoped capability broker；sandbox vault 仍是一个 adapter |
| `cc_harness/context.py` | 保留为 Context Projection implementation；原始事件和 artifacts 永不被压缩覆盖 |
| `cc_harness/memory/*` | 继续提供 recall/capture/maintenance，但只能输出 Advisory Memory Evidence |
| `cc_harness/project/*` | 保留 Todo/HTN 领域能力，但 Todo 状态改为 Run Event 的 projection |

重建不是在 `run_turn()` 上继续加参数。新内核必须有一个小的外部接口，复杂性藏在内部 implementation 和 adapters 中。

## 4. 领域模型

```text
Run
├── GoalContract
├── PlanGraph
│   └── ChildRun[]
├── RunEvents[]
├── CurrentProjection
├── ActionAttempts[]
├── ApprovalRequests[]
├── FollowUpQueue[]
├── WorkingState
├── Evidence[]
└── PinnedRunContract
```

### 4.1 Durable Agent Run

长程任务从创建到终止的唯一权威状态。它拥有目标、生命周期、事件、子运行、审批和完成证据的归属。

Run 的核心状态：

```text
draft
queued
running
awaiting_approval
waiting_on_predecessor
stalled
blocked
cancel_requested
cancelled
failed_recoverable
failed_terminal
completed
```

`outcome_unknown` 是 Action Attempt 状态，不是简单的工具失败；它可以使 Run 进入 `blocked` 或 `awaiting_approval`。

`budget_exhausted` 不属于重建后的终止状态。总 token、费用、调用次数和运行时长只做计量，不构成完成上限。

### 4.2 Goal Contract

写入型长程运行在第一次 mutation 前必须拥有 Goal Contract：

```json
{
  "objective": "string",
  "acceptance_criteria": ["verifiable criterion"],
  "constraints": ["must hold"],
  "allowed_scope": ["path or capability"],
  "excluded_scope": ["path or capability"],
  "required_evidence": ["test/build/review/artifact"],
  "human_review": ["approval requirement"],
  "contract_version": 1
}
```

明确、低风险任务可以自动生成并接受。会改变结果的歧义或高风险目标必须阻塞，向用户提出一个决策问题。普通后续消息不能静默修改现有 Goal Contract。

### 4.3 Run Plan Graph

父运行拥有一张可修订 DAG：节点是 child run 或父级动作，边表达依赖、文件所有权和接纳顺序。

约束：

- 默认最多同时运行三个 child run；
- 最大 child depth 为 2；
- 累计 child 数量不限制；
- 超过深度的分解回到上级，改建为 sibling；
- 有文件所有权冲突的写入节点不能并行；
- 计划修订必须追加事件并保留旧版本；
- child `done` 表示候选成果完成，不表示父 Run 完成。

### 4.4 Follow-up Run

当前 Run 活跃时用户提交的普通新消息进入 Follow-up Queue，成为独立的后续 Run。它引用前序 Run 的结果和 artifact，但拥有独立 Goal Contract、事件、预算计量和完成证据。

前序门：

| 前序状态 | 后续 Run 行为 |
|---|---|
| `completed` | 自动创建并运行 |
| `cancelled` | 自动创建并运行，但注入“前序成果可能不完整”事实 |
| `blocked` | `waiting_on_predecessor` |
| `awaiting_approval` | `waiting_on_predecessor` |
| `stalled` | `waiting_on_predecessor` |
| `failed_recoverable` | `waiting_on_predecessor` |
| `failed_terminal` | `waiting_on_predecessor` |
| 用户明确允许跳过 | 创建 `PredecessorBypassed` 事件后运行 |

只有显式 `/interrupt` 或等价控制动作影响当前 Run。普通消息不注入正在运行的模型上下文。

### 4.5 Action Attempt

每个工具动作拥有稳定 action ID 和尝试序号：

```text
planned → prepared → started → succeeded
                         ├── failed
                         ├── cancelled
                         └── outcome_unknown
```

动作至少记录：tool name、规范化参数摘要、effect class、contract digest、actor、run/worker、attempt、开始时间、结果 artifact、错误类别和 lease epoch。

## 5. 外部接口与 seam

### 5.1 RunCoordinator：用户和入口的唯一 seam

```python
class RunCoordinator(Protocol):
    async def submit(self, request: RunRequest) -> RunHandle: ...
    async def inspect(self, run_id: str) -> RunView: ...
    async def send(self, run_id: str, message: str) -> QueueReceipt: ...
    async def interrupt(self, run_id: str, reason: str) -> ControlReceipt: ...
    async def approve(self, approval_id: str, decision: ApprovalDecision) -> ControlReceipt: ...
    async def cancel(self, run_id: str, reason: str) -> ControlReceipt: ...
```

REPL、headless CLI、JSONL 和未来其他客户端只依赖这个 seam。它们不能直接调用 `run_turn()`、写 Todo 状态或修改 worker 数据库。

### 5.2 AgentKernel：无持久化副作用的深 module

```python
class AgentKernel(Protocol):
    async def execute_segment(self, context: SegmentContext) -> SegmentOutcome: ...
```

`SegmentContext` 由 runtime 组装，包含 run snapshot、projection、Goal Contract、available tools、working state、lease 和 event sink。Kernel 返回事件意图、模型输出、action requests 和候选完成，不直接决定权威状态。

### 5.3 RunStore：唯一事实写入口

```python
class RunStore(Protocol):
    async def append(self, command: AppendEvent) -> StoredEvent: ...
    async def read(self, run_id: str, after: int = 0) -> EventPage: ...
    async def load_projection(self, run_id: str) -> RunProjection: ...
    async def save_snapshot(self, snapshot: RunSnapshot) -> None: ...
    async def import_legacy(self, source: LegacySource) -> LegacyImportReport: ...
```

调用者不提交“当前状态覆盖”，只提交合法事件。`expected_sequence` 和 `lease_epoch` 必须匹配，否则写入失败。

### 5.4 Supervisor / Worker seam

Supervisor 接口：

```python
class LocalSupervisor(Protocol):
    async def start(self) -> None: ...
    async def tick(self) -> None: ...
    async def stop(self, drain: bool = True) -> None: ...
```

Worker 接口：

```python
class RunWorker(Protocol):
    async def claim(self, run_id: str) -> Lease: ...
    async def execute(self, lease: Lease) -> None: ...
    async def heartbeat(self, lease: Lease) -> None: ...
    async def release(self, lease: Lease) -> None: ...
```

Supervisor 是逻辑单写者。Worker 和客户端通过本地 IPC 或 coordinator 提交命令，不直接改权威 SQLite。

## 6. Run Event 规范

事件 envelope：

```json
{
  "event_id": "uuid",
  "run_id": "uuid",
  "sequence": 42,
  "event_type": "ActionStarted",
  "schema_version": 1,
  "occurred_at": "RFC3339",
  "actor": {"kind": "worker", "id": "worker-1"},
  "causation_id": "uuid|null",
  "correlation_id": "uuid",
  "lease_epoch": 7,
  "runtime_contract_digest": "sha256:...",
  "payload": {},
  "artifact_refs": []
}
```

第一版事件集合：

```text
RunCreated
GoalContractAccepted
GoalContractRevised
RunQueued
RunClaimed
RunYielded
WorkerHeartbeat
RunSegmentStarted
RunSegmentFinished
PlanCreated
PlanRevised
ChildRunCreated
ChildRunClaimed
ChildCandidateSubmitted
ChildCandidateAccepted
ChildCandidateRejected
IntegrationConflictRaised
ActionPlanned
ActionPrepared
ActionStarted
ActionProgressRecorded
ActionSucceeded
ActionFailed
ActionCancelled
ActionOutcomeUnknown
ReconciliationStarted
ReconciliationResolved
ApprovalRequested
ApprovalGranted
ApprovalRejected
InterruptRequested
FollowUpQueued
PredecessorBypassed
ProgressRecorded
StallDiagnosisRecorded
RunStalled
VerificationRecorded
CompletionCandidateSubmitted
CompletionAccepted
RunBlocked
RunCancelled
RunFailed
RunResumed
RunRuntimeMigrated
LegacyRunImported
RunSnapshotCreated
```

事件不可更新、不可删除。rewind、branch 和 cancel 都追加事件，不篡改历史。

## 7. 存储与对象

逻辑布局：

```text
cc-harness project data/
├── runtime.db
├── objects/<sha256-prefix>/<sha256>
├── snapshots/<run-id>/<sequence>.json
├── worktrees/<run-id>/<child-id>/
├── backups/<cutover-id>/
└── migration/<migration-id>.json
```

物理位置必须按 project identity 稳定解析，不能随 worktree 或当前 shell CWD 漂移；符合现有 ADR 0019 的用户数据目录约束。项目 `.cc-harness/` 可保存 activation、上下文和项目控制文件，但不应因每个 child worktree 创建独立权威 store。

SQLite/WAL 负责结构化事件、租约、审批、队列和 projection。大型工具输出、diff、日志、snapshot 和附件先写临时对象、校验 SHA-256、原子改名，再提交引用事件。未被引用的对象由带 grace period 的垃圾回收处理。

## 8. Lease、heartbeat 和恢复

Lease 字段：run_id、worker_id、lease_epoch、acquired_at、expires_at、last_heartbeat、runtime_contract_digest。

规则：

1. 同一个 run 只能有一个有效 lease。
2. 每次事件写入都校验 lease epoch。
3. 过期 worker 的事件拒绝写入。
4. Supervisor 发现 lease 过期后先记录 `WorkerLeaseExpired`，再回收 run。
5. 未完成 action 的恢复分类由 Tool Recovery Contract 决定。
6. 无 terminal result 的 mutation 一律先成为 `outcome_unknown`。
7. 只有 read-only 或同一幂等键的 action 可以自动重试。
8. 无法 reconciliation 的外部副作用进入 `blocked` 或 `awaiting_approval`。

崩溃恢复点至少覆盖：

- ActionPlanned 前；
- ActionPrepared 后、dispatch 前；
- ActionStarted 后、结果前；
- object 已完整、引用事件未提交；
- 事件提交后、projection 更新前；
- snapshot 创建后；
- worker heartbeat 过期后；
- approval 已创建但客户端断开后。

## 9. Tool Recovery Contract

每个工具 adapter 必须声明：

```python
@dataclass(frozen=True)
class ToolRecoveryContract:
    effect: Literal["read_only", "local_mutation", "external_side_effect", "unknown"]
    retry: Literal["safe", "idempotency_key", "unsafe"]
    reconcile: Literal["supported", "unsupported"]
    cancel: Literal["supported", "best_effort", "unsupported"]
    requires_approval: bool
    parallel: Literal["read_batch", "serial"]
```

没有可信 metadata 的 MCP 工具默认为：`external_side_effect / unsafe / unsupported / unsupported / approval / serial`。

权限允许与恢复安全是两个独立判断。用户批准 `git push origin feature/auth` 不意味着 timeout 后可以无条件重复 push。

## 10. Worker、worktree 和凭证

### 10.1 Child run

写入型 child 从固定 base commit 创建独立 worktree 和分支。完成后提交 Candidate Change Set：commit hash、diff digest、验收和验证证据。父运行在集成 worktree 中按依赖顺序审查、应用、处理冲突并重新验证。

child commit 不等于父完成。冲突生成独立 integration task，不能由两个 child 互相覆盖解决。

非 Git 项目或 worktree 创建失败时，写入型 child 降级为串行，明确记录 `isolation_unavailable`，不能静默并行写共享目录。

### 10.2 凭证

Worker 默认不继承长期 API key、token、password、cookie、数据库连接串或云凭证。模型请求优先由 supervisor 代理；外部集成按 action-scoped capability 授权，绑定 run、action、工具、目标、参数范围和有效期。

child 不自动继承父凭证。凭证材料不能进入 event、message、object、log、diff 或 commit。

## 11. 审批、中断和队列

Approval Request 必须持久化具体 tool、参数摘要、风险、scope 和 expiry。没有客户端时 Run 进入 `awaiting_approval`，释放 worker，不自动允许。

普通新消息只写 `FollowUpQueued`。明确 `/interrupt` 才写 `InterruptRequested`。取消无法确认时保留 `outcome_unknown`，不能声称外部副作用已撤销。

## 12. Context 和 memory

原始事件、工具对象和用户目标永不被 compaction 删除。Context Projection 是每次模型调用前重建的临时视图。

Memory 只能返回 Advisory Memory Evidence：必须包含 source、project scope、timestamp、confidence 和 evidence ID。它不得覆盖 Goal Contract、授予能力、完成 Todo 或证明完成。冲突时以当前代码、当前用户目标和结构化事件为准。

## 13. 进展、停滞和完成

Run Progress 只能由结构化事实证明：验收项满足、错误减少、有效变更、修改后验证通过、child 接纳、阻塞解除、关键证据取得或已淘汰失败路径。

连续三个 execution segment 没有 Progress 后：

1. 写入 StallDiagnosisRecorded；
2. 生成一次替代策略/计划版本；
3. 再无 Progress 则写 RunStalled；
4. 释放 worker；
5. 等待用户或新的策略版本；
6. 不原样重放上一段。

CompletionVerifier 必须同时检查：

- Goal Contract acceptance criteria；
- 修改路径和 Candidate Change Set；
- 修改后的匹配测试/构建/检查证据；
- 未解决 error 和 `outcome_unknown`；
- child 是否已被父运行接纳；
- 必要人工审批；
- 禁止范围没有被修改。

## 14. Pinned Run Contract 与升级

Run 创建时绑定：runtime version、event schema、tool contract digest、model/provider config digest、policy/safety profile 和 capability profile。

升级只能在可恢复点进行，追加 `RunRuntimeMigrated`；旧 worker 在迁移后不能继续写。若新版本无法读取 run，继续使用旧 worker 或进入 blocked，并保留迁移前备份。

## 15. Legacy import

一次性导入旧 session、Todo、memory、checkpoint 和 action journal：

- 幂等：相同源 digest 不重复导入；
- 可审计：记录 source path、source digest、importer version 和结果；
- 不编造：旧数据无法证明的 outcome、permission 和 verification 标记 `legacy_unverified`；
- 不双写：新 runtime 启动后旧 store 只读；
- 保留备份：切换前使用 SQLite backup API 和 object manifest；
- 失败可重试：单个 source 失败不损坏已成功导入数据；
- 迁移后验证：事件数量、消息摘要、Todo、artifact refs 和关键 checkpoint 对账。

## 16. Runtime Rebuild Release Gate

以下门禁不可相互抵消，任意硬门失败都不能 cutover：

1. 迁移：代表性旧数据可幂等导入并可读回。
2. 事件：事件不可变、顺序、schema、expected sequence 和 projection rebuild 正确。
3. 恢复：所有崩溃注入点没有重复或遗漏的安全动作。
4. Outcome Unknown：不可对账副作用不会盲重试。
5. Lease：过期/旧版本 worker 无法写入。
6. Worktree：child 隔离、candidate commit、冲突和父级重验证有效。
7. Approval：离线不放行，参数变更重新审批，hard-deny 永不绕过。
8. Credential：未授权长期凭证不可达，child 不继承父凭证。
9. Queue：Follow-up 和 Predecessor Gate 规则正确。
10. Stall：重复无进展最终停滞并释放 worker。
11. Completion：无证据不能 completed。
12. Cross-platform：Windows、Linux、macOS 核心 supervisor/worker/IPC/恢复通过。
13. Rollback：cutover 失败可回到旧 runtime 和迁移前备份。
14. Frozen parity：固定模型、环境、任务集下核心 coding 能力无未经批准的显著退化。

## 17. 关键失败语义

| 事件 | 状态 | 自动动作 |
|---|---|---|
| provider transient error | running | 按 Tool Recovery Contract 重试 |
| permission denied | blocked/failed_terminal | 不绕过，等待用户或改计划 |
| action started but no result | outcome_unknown | 对账，不盲重放 |
| worker lease expired | queued/recovery | 回收并恢复可安全 segment |
| approval offline | awaiting_approval | 保存、释放 worker |
| repeated identical trajectory | stalled | 一次替代计划后仍无进展则停滞 |
| child conflict | blocked/integration | 建 integration task |
| predecessor failed | waiting_on_predecessor | 不自动运行 follow-up |
| old worker after migration | rejected | 丢弃写入，保留证据 |

## 18. 设计完成标准

该规格视为实现完成的设计基线，当且仅当：

- 新内核的外部 seam 不再暴露 `run_turn()` 的长参数表；
- Run Event、projection、lease、action、approval、queue 和 child run 有明确 schema；
- 每条状态转换都有事件和测试；
- 旧数据导入和 cutover 回滚路径可执行；
- 计划中的发布门都有对应测试或可复现证据命令。
