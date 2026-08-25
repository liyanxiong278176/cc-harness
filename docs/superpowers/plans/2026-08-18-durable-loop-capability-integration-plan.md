# Durable Agent Loop 能力连续性实施计划

状态：核心实现与当前配置下的 Provider/MCP/Supervisor 验收通过；真实业务项目与 live cutover 单独保留

规格：[2026-08-18-durable-loop-capability-integration.md](../../specs/2026-08-18-durable-loop-capability-integration.md)

实施结论（2026-08-18）：Durable 入口已经接入共享 `AgentCapabilityRuntime` 与 `SharedCapabilityServices`，完成事件/Artifact 权威交互历史、同 Segment 多轮模型循环、ToolObservation/续读、上下文压缩与 offload、授权回忆、记忆生命周期、Policy/L2/L5/provenance/output guard、动作级 capability broker、只读 Plan Discovery/mutation gate、PlanGraph 调度、child/follow-up handoff、旧数据迁移和 capability continuity gate。`tests/runtime_rebuild`、全仓 pytest（排除既有攻击生成脚本）和 ruff 均通过；当前配置下的真实 Provider、MCP 只读调用和隔离 Durable Supervisor E2E 已记录在[验收报告](../../specs/2026-08-18-durable-loop-acceptance-report.md)。

当前仍需目标部署环境执行、且本次不冒充完成的真实验收只有：真实业务项目登录注册 E2E 和一次性 live cutover。当前配置下的真实 Provider/MCP、Supervisor smoke、provider-neutral 登录注册夹具和自动 gate report 已完成。

## 0. 工作原则

- 保留工作区现有未相关改动；按文件/区块选择性提交。
- 每个阶段先写失败测试，再接线，再运行相关旧回归。
- 不调用旧 `run_turn()` 作为 Durable 内核，不复制简化能力实现。
- 每个能力必须有真实 activation/artifact 证据，不能只 mock “已调用”。

## 1. 冻结能力基线与夹具

- [x] 1.1 生成旧 SessionRuntime 能力矩阵：context、offload、read_ref、memory、security、tools、Todo/Subagent、activation。
- [x] 1.2 冻结 provider-neutral 登录注册项目夹具及机器验收；当前配置下真实第三方 provider 验收已通过，真实业务项目仍保留部署验收。
- [x] 1.3 保存现有 context/offload/memory 代表性数据夹具和摘要。
- [x] 1.4 增加 Durable 能力连续性 gate runner，初始状态必须因未接线而失败。

验收：报告明确显示 Durable 当前缺少 context/offload/memory 等能力，不允许假绿。

## 2. 扩展领域模型、事件和 projection

- [x] 2.1 增加 Plan discovery/node、model invocation/message、ToolObservation、context、memory、handoff 事件 schema。
- [x] 2.2 定义 provider-neutral `ToolObservation`、content block、cursor、provenance 和 context manifest 数据类。
- [x] 2.3 让 Action terminal 的 succeeded/failed/outcome_unknown 全部关联 observation artifact。
- [x] 2.4 更新 projection、snapshot、SQLite derived tables 和 additive migration。
- [x] 2.5 加入事件 codec、非法顺序、projection rebuild 和旧 snapshot 兼容测试。

验收：完整交互只凭 events/artifacts 重建，projection digest 确定。

## 3. 抽取 AgentCapabilityRuntime

- [x] 3.1 从 `SessionRuntime._initialize/_build_context_services/_build_memory_services` 提取共享 capability configuration/lifecycle boundary。
- [x] 3.2 建立 ContextEngine、MemoryEngine、SafetyEngine、ToolRuntime、PlanningRuntime adapter interfaces。
- [x] 3.3 让 SessionRuntime 在迁移期使用共享 capability builder，保持旧行为测试。
- [x] 3.4 让 DurableRuntimeClient 使用同一 builder，移除自行简化装配。
- [x] 3.5 统一 close/drain/error degradation 和 activation manifest。

验收：两条入口共享同一能力实例构造路径；Durable 不 import/call `run_turn()`。

## 4. 统一 ToolObservation 与工具 adapter

- [x] 4.1 将现有 `ToolResult` 适配为结构化 ToolObservation，不破坏旧调用者。
- [x] 4.2 Native Read/Glob/Grep 映射稳定 cursor、hash、范围和完整性。
- [x] 4.3 Native Edit/Write 强制 observation/hash precondition，冲突返回结构化 stale observation。
- [x] 4.4 为 run_command 增加 workspace lock、前后基线、stdout/stderr chunk、退出状态和 change set。
- [x] 4.5 MCP 保留 server/content block/request identity/capability metadata，未知工具采用保守恢复契约。
- [x] 4.6 实现独立只读 batch；mutation/command/external 串行。

验收：Native、Command、MCP 使用同一 observation projection，失败正文可恢复。

## 5. 提交后可见与多轮 Kernel

- [x] 5.1 模型调用前提交 ContextCallManifest/ModelInvocationStarted。
- [x] 5.2 完整组装并校验 assistant 响应，提交后才 dispatch tools。
- [x] 5.3 tool call/action 先提交；每个观察块和最终观察提交后才回到模型。
- [x] 5.4 将 Kernel 改为同一 Segment 内多轮 model↔tool，保留取消、审批和 lease heartbeat。
- [x] 5.5 Segment 绑定一个 Plan node，不使用总预算、max_iter、时间片或队列压力终止。
- [x] 5.6 覆盖模型/工具各提交点崩溃恢复和旧 lease fencing。

验收：登录夹具中 Read 内容影响 Edit，pytest stderr 影响下一次修复；重启后不丢观察、不重放副作用。

## 6. 接入生产 ContextEngine 与 offload

- [x] 6.1 从 Run events 物化标准 user/assistant/tool transcript，保持 tool-call pairing。
- [x] 6.2 用 adapter 调用现有 `ContextProjection.compact` 和 TokenCounter/ContextConfig。
- [x] 6.3 将 summary artifact、source digest、coverage 和 stats 写入 Run ArtifactStore/Event。
- [x] 6.4 将现有 offload/read_ref/search/inspect 绑定 run_id、observation_id 和 capability scope。
- [x] 6.5 实现 `ContinueToolResult`，小结果内联、大结果显式续读。
- [x] 6.6 实现 `RecallRunContext` 的事件/路径/类型/序号检索及授权关系过滤。
- [x] 6.7 生成确定性的 ContextCallManifest 和 protected-state reducer；mandatory overflow fail-closed。

验收：真实触发 Snip/Prune/Summarize/offload/continuation/recall，来源和原始记录可校验。

## 7. 接入 MemoryEngine 与安全链

- [x] 7.1 在 context build 调用现有 layered recall，输出 Advisory Memory Evidence。
- [x] 7.2 在 Progress/Verification/node terminal 提交 memory candidate，Run terminal 执行巩固。
- [x] 7.3 接回 capture、pipeline、maintenance、reflection 和 drift lifecycle/activation。
- [x] 7.4 确保 memory 不替代 same-Run history、Goal、权限、Todo 或 completion evidence。
- [x] 7.5 接回 Policy、L2、L5、provenance、output guard 和 action-scoped capability broker。
- [x] 7.6 验证 offload/summary/recall/memory 变换全程保留 taint 和来源。

验收：每项能力有真实 activation 和 artifact；注入文本经检索后仍是 tool/advisory 数据。

## 8. 重做项目级调度

- [x] 8.1 所有 Run 创建单节点或 discovery PlanGraph，并生成 Plan-Backed Todo。
- [x] 8.2 实现只读 Plan Discovery Phase 和 mutation gate。
- [x] 8.3 Supervisor 只领取 PlanGraph ready node，不再按 `records[:capacity]` 直接领取。
- [x] 8.4 实现 Project Root Run Gate 和 predecessor queue。
- [x] 8.5 将 completed/active nodes、owned paths、workspace locks 和 child depth 纳入原子 claim。
- [x] 8.6 仅对依赖完成、路径不交叉、worktree 隔离 child 建立并行 cohort。

验收：同项目独立 root Run 不并行；依赖或路径冲突 child 不启动；不同项目可独立运行。

## 9. Child 和 Follow-up 上下文

- [x] 9.1 持久化 ChildDelegation manifest 和 parent recall authorization range。
- [x] 9.2 child 只获得节点/路径/能力/依赖相关上下文，不继承 transcript/credentials。
- [x] 9.3 Candidate Change Set 回流结构化结果；父应用、重验后才更新事实。
- [x] 9.4 持久化 PredecessorHandoff，Follow-up 使用摘要和受控 recall。
- [x] 9.5 cancelled 前序标记 incomplete；其他非终态保持 gate，bypass 必须显式事件。

验收：child/follow-up 无跨范围读取，拒绝候选不成为完成事实。

## 10. 遗留能力数据迁移

- [x] 10.1 扩展 LegacyProjectImporter 扫描 context summary、offload refs/manifests/canvas 和 memory checkpoints。
- [x] 10.2 按 source digest 幂等导入或建立只读 artifact link，保留来源和完整性。
- [x] 10.3 对账 message/tool pairing、summary coverage、offload reachability 和 memory project scope。
- [x] 10.4 旧历史 Run 默认 blocked，迁移数据不能自动执行或授予权限。
- [x] 10.5 扩展 rehearsal/rollback 报告和真实项目 dry-run。

验收：旧压缩、卸载、检索和记忆数据可读；重复执行无重复事件/对象。

## 11. 非补偿性能力连续性门禁

- [x] 11.1 multi-round/context/offload/retrieval/memory/tool/safety/scheduler/handoff/migration gate 分项运行。
- [x] 11.2 每项报告 activation、event、artifact、恢复结果和退化原因。
- [x] 11.3 运行旧 context/memory/native tool/security/Subagent 回归。
- [x] 11.4 运行 runtime rebuild tests、ruff、migration reconciliation 和 crash matrix。
- [x] 11.5 运行 provider-neutral 登录注册夹具及大日志/stale edit/command changes/重启/child/follow-up 自动回归；当前配置下真实 provider/MCP/隔离 supervisor E2E 已通过，真实业务项目 E2E 保留为部署验收。
- [x] 11.6 更新 gate report；任一硬门失败时 `passed=false`，不得综合抵消。

验收：所有能力连续性门通过，且没有未解释的旧能力回归。

## 12. Cutover 与回滚

- [x] 12.1 在 capability gate 全绿后，使 Durable 入口使用共享 AgentCapabilityRuntime。
- [x] 12.2 保留旧 Runtime 显式兼容入口和数据备份，执行真实 supervisor smoke（已在真实 Provider 配置下完成隔离 Durable E2E，并校验 activation manifest；live cutover 仍需部署确认）。
- [x] 12.3 演练 context/offload/memory migration 回滚，不降级旧数据库。
- [x] 12.4 更新用户文档、架构图、简历可陈述证据和已知限制。

验收：真实项目可持续执行并恢复，旧能力均可观察；回滚可恢复原入口和数据。
