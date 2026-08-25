# Durable Runtime 能力连续性矩阵

日期：2026-08-18

本矩阵记录 Durable 入口必须继续复用的既有能力、接入位置和可观察证据。它不是第二套能力实现；能力代码仍由原有模块提供，Durable Loop 只负责生命周期适配、事件提交和恢复。

| 能力 | 既有实现 | Durable 接入 | 持久证据/验证 |
| --- | --- | --- | --- |
| 四级上下文压缩 | `cc_harness/context.py` | `AgentCapabilityRuntime.build_context()` | `ContextProjectionBuilt`、`ContextCompacted`、summary artifact、source/projection digest |
| 大结果卸载与引用检索 | `cc_harness/memory/offload/` | run-scoped offload extras、`read_ref`、`search_ref`、`inspect_node` | ToolObservation metadata、result artifact、offload refs/manifest |
| 分段结果续读 | native ToolResult metadata | `ContinueToolResult` | incomplete observation、`next_cursor`、continued observation |
| Run 内历史回忆 | Run Event/ArtifactStore | `RecallRunContext` | 授权的 run/parent/predecessor/child 范围与事件序号 |
| 长期记忆 | `cc_harness/memory/` | recall、capture、pipeline、maintenance、reflection、drift lifecycle | `MemoryCandidateRecorded`、`MemoryCheckpointCommitted`、memory artifact |
| 安全与 provenance | policy/L2/L5/native executor | L5 output sanitization、tool contract、workspace boundary | observation provenance、`safety_applied`、activation manifest |
| 原生文件工具 | `cc_harness/native_tools.py` | shared first-party contracts/handlers | read paths、modified paths、content hash/stale conflict |
| 命令工具 | `run_command`/executor | workspace lock、前后基线、change set、stdout/stderr chunks | command result artifact、workspace change-set metadata |
| MCP | `cc_harness/mcp_client.py` | existing MCP client and tool metadata | source/capability metadata、ToolObservation |
| Todo/Plan/Subagent/Follow-up | RunCoordinator/Supervisor/Follow-up | PlanGraph readiness、child delegation、predecessor handoff | plan/todo/handoff events and artifacts |
| Activation | `ActivationManifest` | Durable runtime startup + live event triggers | activation JSON + capability continuity gate |

## 验证范围

- `tests/runtime_rebuild` 覆盖事件 schema、交互多轮、观察续读、offload、命令 change set、stale edit、memory/cutover gate、迁移和调度。
- 全仓 pytest 和 ruff 回归已执行；当前配置下真实第三方 provider、真实 MCP server 和 provider-neutral 登录注册夹具已验收通过；真实业务项目登录注册 E2E 与一次性 live cutover 仍需在目标部署环境中执行，不能用本地 mock 代替。
