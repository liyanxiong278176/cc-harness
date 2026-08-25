# Durable Agent Loop 能力连续性增强规格

状态：已确认并完成核心实施（2026-08-18）

日期：2026-08-18

依赖：[Durable Agent Runtime 重建规格](./2026-08-17-durable-agent-runtime-rebuild.md)

实现说明：本规格对应的 Durable 核心代码、共享 capability builder、迁移、调度、Policy/L2/L5/provenance 安全链、Plan Discovery/mutation gate、能力连续性门禁和自动化回归已落地；旧 Runtime 入口/备份路径保留。当前配置下真实 Provider 流式请求、8 个 MCP server 连接、MCP 只读调用和隔离 Durable Supervisor E2E 已通过；真实业务项目登录注册 E2E 与一次性 live cutover 仍需部署环境验收，不以本地 mock 代替。

## 1. 目的

本次工作只改进 Durable Agent Loop 的执行、恢复、上下文和调度，不删除、旁路或简化 cc-harness 已接通的上下文压缩、工具结果卸载与检索、长期记忆、安全、权限、原生工具、MCP、Todo、Subagent 和评测能力。

完成后的 Loop 必须能在一个 PlanGraph 节点内持续执行多轮“模型 → 工具 → 已提交观察 → 模型”，并在 Segment 交接、进程崩溃、终端关闭和机器重启后，从同一权威交互记录恢复。

## 2. 当前事实与缺口

### 2.1 已有且必须复用

- `cc_harness/context.py`：四级上下文投影，包含 Snip、Prune、Summarize、保护区、版本与来源摘要。
- `cc_harness/memory/offload/*`：大型工具结果卸载、节点 manifest、`read_ref`、搜索和完整性检查。
- `cc_harness/memory/*`：layered recall、capture、pipeline、maintenance、reflection 和 drift detection。
- `cc_harness/native_tools.py`：稳定分页、内容哈希、条件 Edit/Write、原子替换和路径边界。
- `cc_harness/policy.py`、L2、L5、provenance/security：现有安全与权限链。
- `cc_harness/mcp_client.py`：MCP transport、来源与能力 metadata。
- 旧 `SessionRuntime` 的 activation manifest、计量、Todo/Subagent 和评测接线。

### 2.2 Durable 路径当前缺口

- `RunWorker` 一次 Segment 只调用模型一次，工具结果不会在同一 Segment 回到模型。
- 默认上下文只含 Goal、约束和最近二十个动作状态；工具观察正文和模型可见输出没有形成可重放历史。
- `cc_harness/context_projection.py` 是简化骨架，未调用生产 `ContextProjection`、offload、检索或 memory。
- 失败工具结果虽有 artifact，projection 未稳定保留所有结果引用。
- Supervisor 按空闲 worker 领取任意 queued Run，未执行 PlanGraph 依赖、Todo、项目根 Run 串行和文件所有权门禁。
- 新 DurableRuntime 自行装配 LLM/MCP/native tools，形成比 `SessionRuntime` 更少的平行能力栈。

## 3. 不可违反的设计原则

1. Run Event、ToolObservation 和内容寻址 artifact 是事实；摘要、Working State 和模型消息是可重建投影。
2. 任何工具观察在模型可见前必须先提交；任何可执行 tool call 在 dispatch 前必须先提交。
3. 同一 Run 连续性来自 Run 历史，不依赖长期记忆；长期记忆只提供 Advisory Memory Evidence。
4. 现有能力通过一个共享 `AgentCapabilityRuntime` 接入；禁止在 Durable 路径复制简化实现，也禁止嵌套旧 `run_turn()`。
5. Run 没有总 token、费用、时长或调用次数上限；Segment 由 PlanGraph 节点边界驱动，不按时间片或轮次让出。
6. 同一项目根 Run 默认串行；只有同一 PlanGraph 中依赖完成、文件范围不冲突且 worktree 隔离成立的 child 节点可并行。
7. 所有 Run 都有 PlanGraph；简单任务是单节点图，复杂任务可先只读探索再提交 DAG。
8. 压缩、卸载、检索、handoff 和 memory 不能改变 provenance、taint、权限或用户意图层级。

## 4. 目标架构

```text
RunCoordinator
├── RunStore / ArtifactStore
├── Project Scheduler
│   ├── Project Root Run Gate
│   ├── PlanGraph readiness
│   ├── Todo projection
│   └── child path/worktree gate
└── LocalSupervisor
    └── RunWorker
        ├── Durable Agent Kernel
        └── AgentCapabilityRuntime
            ├── ContextEngine
            ├── MemoryEngine
            ├── SafetyEngine
            ├── ToolRuntime
            ├── PlanningRuntime
            └── Activation/Evidence
```

`RunStore` 是执行事实权威；`AgentCapabilityRuntime` 只管理能力生命周期和适配器，不维护第二套消息、Run 状态或调度状态。

## 5. Execution Segment 与模型循环

一个 Segment 绑定一个当前 PlanGraph 节点/Todo，并允许多轮：

```text
构建 Model Context Projection
→ 提交 ModelInvocationStarted + ContextCallManifest
→ 调用模型
→ 完整组装并校验响应
→ 提交 AssistantMessageCommitted 和 tool calls
→ 执行工具
→ 提交 ToolObservation
→ 将已提交观察作为 tool message 回到模型
→ 继续下一轮
```

Segment 只在以下边界结束：

- 当前节点有验证证据并完成；
- 需要审批或用户输入；
- blocked、stalled 或 outcome_unknown；
- 等待 PlanGraph 依赖或 child；
- interrupt/cancel；
- 可恢复 provider/基础设施故障；
- Run 完成。

不得用模型轮次、经过时间、队列压力或资源计量强制 `RunYielded`。

## 6. 可持久交互与事件

新增或升级以下事实：

- `PlanDiscoveryStarted` / `PlanDiscoveryCompleted`
- `PlanNodeStarted` / `PlanNodeCompleted` / `PlanNodeBlocked`
- `ModelInvocationStarted`：模型、配置摘要、ContextCallManifest artifact。
- `AssistantMessageCommitted`：可见 assistant 内容、stop reason、usage 和稳定 tool-call IDs。
- `AssistantMessageInterrupted`：不可作为最终消息或工具授权。
- `ToolObservationChunkCommitted`：流式工具可见块。
- `ToolObservationCommitted`：最终结构化观察和 artifact refs。
- `ContextProjectionBuilt`：投影摘要、来源范围、token 分类和裁剪原因。
- `ContextCompacted`：版本、父摘要、覆盖事件、来源摘要和 artifact。
- `MemoryCandidateRecorded` / `MemoryCheckpointCommitted`。
- `PredecessorHandoffCommitted` / `ChildDelegationCommitted`。

现有 `ActionPlanned/Prepared/Started/terminal` 保持动作恢复权威，并与 ToolObservation ID 关联。所有 terminal 状态，包括 failed 和 outcome_unknown，都保留观察引用。

## 7. ToolObservation 协议

所有 Native、Command 和 MCP adapter 返回 provider-neutral `ToolObservation`：

```text
observation_id, tool_call_id, action_id, attempt
tool_name, status, effect, recovery contract
typed content blocks and artifact refs
complete, returned range, next cursor
read paths, modified paths, before/after digests
exit/process/remote request metadata
error kind, timing, provenance, taint, sanitization
```

### 7.1 读取和搜索

- 复用现有 Read 内容哈希、Glob/Grep 稳定分页。
- 小结果完整回灌；大结果返回有完整性标记的 preview 和 cursor。
- `ContinueToolResult` 适配现有 `read_ref/search_ref/inspect_node`，保持 Run、项目、路径和来源边界。
- `RecallRunContext` 搜索当前 Run 及明确授权的前序/child 事件和观察，再通过 `ContinueToolResult` 展开。

### 7.2 文件修改

- 复用现有 `expected_hash`、`create_only` 和原子替换。
- Edit/Write 必须引用读取观察或显式 expected absence。
- 前置摘要不匹配时不写入，提交 stale/conflict ToolObservation，要求重新读取。

### 7.3 run_command

- 默认是串行 workspace mutation，获得项目写锁。
- 执行前保存工作区基线，执行后记录新增、修改、删除路径和可恢复 diff。
- stdout/stderr 按块先提交后可见，最终观察含 cwd、规范化调用、exit、timeout、cancel 和进程身份。
- 只有 sandbox 强制 write-denied 的 profile 可声明只读并行。

### 7.4 重试与恢复

- read-only 且无已提交结果：安全重试。
- 条件文件修改：检查 before/after digest 后补记成功、重试或冲突。
- command：先对账 workspace change set，不盲目重放。
- external/MCP side effect：必须有幂等键或 reconciliation；否则 outcome_unknown。

### 7.5 并行

- 同一完整模型响应中的独立 Read/Glob/Grep 可并行，观察单独提交并按原 tool-call 顺序返回。
- mutation、command、unknown/external MCP 和有因果依赖的调用串行。

## 8. 生产 ContextEngine 接线

### 8.1 数据源

从 Run Event 重建 provider-neutral message transcript：user、assistant、tool call、ToolObservation。隐藏 reasoning、草稿和残缺流不进入 transcript。

### 8.2 复用而非重写

- 用 adapter 把 Run transcript 喂给现有 `cc_harness.context.ContextProjection`。
- 保留 Snip、Prune、Summarize、protect zone、tool pairing repair 和来源摘要校验。
- 将 summary artifact 接入 Run ArtifactStore，并追加 `ContextCompacted`；旧文件式 artifact 可作为 adapter，不能成为第二事实源。
- 复用 offload 节点、manifest、read/search/inspect 逻辑，并补 Run/observation identity 与 capability checks。

### 8.3 投影优先级

1. 系统/安全、Goal、当前用户决定、当前 Plan/Todo、审批和 outcome_unknown。
2. 当前节点、最近完整交互、最新文件观察和验证。
3. 与当前路径/错误/依赖相关的旧观察。
4. 版本化摘要、已完成历史和大型工具引用。

机器 reducer 保护不可丢失状态；模型只生成带来源引用的语义工作摘要。必选状态本身超窗时 fail-closed 为 `context_overflow`，不静默删除。

## 9. MemoryEngine 接线

- 每次 context build 可调用现有 layered recall，结果作为带来源、项目范围、时间和置信度的 Advisory Memory Evidence。
- 同一 Run 的历史查找走 `RecallRunContext`，不把 memory 当 transcript。
- 在已提交 Progress/Verification/Plan 节点边界生成 memory candidate。
- Run terminal 时执行巩固；临时错误、未验证猜测和废弃方案不进入正式项目记忆。
- 复用现有 capture、pipeline、maintenance、reflection 和 drift；后台结果只有提交后才能进入后续上下文。
- memory 不修改 Goal、权限、Todo 完成或 completion evidence。

## 10. 调度、Todo 与 Subagent

- 每个 Run 都有 PlanGraph，Todo 是节点进度投影。
- 复杂任务先进入只读 Plan Discovery Phase；第一版可执行图提交前禁止 mutation/command/unknown external tool。
- 同项目一次只推进一个 root Run；后续 root Run 通过 predecessor gate 等待。
- child ready 必须满足 dependencies completed、path ownership disjoint、worktree isolated、并发深度规则。
- child 只接收 delegation manifest：节点目标、验收、决定、owned/forbidden paths、依赖结果、相关观察、工具/能力和可召回父历史范围。
- 父 Run 默认只接收 Candidate Change Set、修改范围、验证、未解决问题和 artifact refs；应用并重验后才成为父事实。

## 11. Follow-up

前序完成或取消后提交 `PredecessorHandoffCommitted`，包含终态、已接纳路径/基线、验证、用户决定、未解决错误/outcome_unknown 和授权召回范围。Follow-up 不复制 transcript；取消前序明确标记不完整，其他非终态继续 gate，除非用户 bypass。

## 12. 安全与来源

- 每个 ToolObservation、offload chunk、summary、memory evidence、recall result 和 handoff 保留 provenance/taint/sanitization。
- 文件、命令、MCP、Web 和 memory 内容始终以 tool/advisory 数据进入，不能升级为 user/system/approval。
- 凭证材料在持久化前移除，事件和 artifact 只保留允许的脱敏表示与摘要。
- Durable CapabilityRuntime 必须复用现有 Policy、L2、L5、field/span provenance 和 capability broker。

## 13. 遗留数据连续性

迁移除 session、Todo、action journal 和 memory 外，还必须处理：

- 有效 compaction summary 及其 source digest；
- offload refs、node manifest、canvas 和 read_ref 可达性；
- tool-call/result 配对与可恢复 transcript；
- memory project scope、checkpoint 和 maintenance 状态。

导入保持幂等、原数据只读、未知副作用不重放、历史 Run 默认 blocked，且导入报告逐项对账。

## 14. 能力连续性发布门

以下门禁不可相互抵消：

1. Multi-round：同一 Segment 的 Read 结果真实影响后续 Edit，test error 真实影响修复。
2. Interaction recovery：每个 model/tool commit 边界崩溃后恢复无重复、无遗漏。
3. Context：生产四级压缩真实触发，摘要可验证、原记录可恢复。
4. Offload/retrieval：大结果卸载、续读、搜索和完整性校验真实执行。
5. Memory：recall/capture/pipeline/maintenance/reflection/drift 各有 activation 和证据。
6. ToolObservation：Native、Command、MCP 统一协议，失败结果也保留正文引用。
7. Mutation/command：stale edit 被拒绝，command 实际文件变更可对账。
8. Safety：Policy/L2/L5/provenance/approval/capability broker 没有旁路。
9. Scheduling：Plan/Todo readiness、项目根串行、child 文件冲突和 worktree 门有效。
10. Handoff：child 和 Follow-up 只继承授权结构化上下文。
11. Migration：旧摘要、offload、检索和 memory 数据可读且不重复。
12. Existing regression：旧能力测试与 runtime rebuild gate 全部通过。

旧 Runtime 只在以上门禁全部通过且 rollback rehearsal 成功后退为兼容入口。

## 15. 端到端验收场景

固定“实现登录注册模块”夹具：Agent 必须读取现有 FastAPI/SQLAlchemy 项目，建立 PlanGraph，条件修改模型/路由，运行测试，读取失败日志，修复 JWT 错误，再凭测试证据完成。测试注入大日志、Segment 重启、stale file change、command 产生文件、用户 follow-up 和两个独立 child 候选，以证明整条链而非单独单元存在。

## 16. 非目标

- 不重写成熟的 compaction、offload、memory 或安全算法。
- 不保留两个权威 message/session 状态。
- 不以任意预算、轮次或时间片终止长任务。
- 不允许普通 root Run 借空闲 worker 并行写同一项目。
- 不保存或展示隐藏 chain-of-thought。
