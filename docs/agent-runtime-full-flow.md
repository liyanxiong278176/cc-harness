# Agent Runtime 完整流程图（仅 Runtime）

本图只描述 Agent Runtime 的任务编排、持久化执行、模型循环、审批、恢复和终态。Context、Memory、MCP、Sandbox 等能力只作为 Runtime 调用的黑盒依赖，不在本图展开。

```mermaid
%%{init: {"theme": "dark", "themeVariables": {"primaryColor": "#202938", "primaryTextColor": "#f3f4f6", "primaryBorderColor": "#8aa4c8", "lineColor": "#cbd5e1", "secondaryColor": "#18212f", "tertiaryColor": "#111827"}}}%%
flowchart TD
    U["用户提交任务<br/>User Request<br/>作用：提出任务目标和约束"]
    API["RunCoordinator.submit<br/>作用：Runtime 的统一入口"]
    R0["创建 Run：DRAFT<br/>作用：生成唯一 run_id，绑定项目和 Runtime Contract"]
    GC["Goal Contract<br/>作用：目标、验收标准、允许/禁止范围和所需证据"]
    U --> API --> R0 --> GC

    subgraph ADMISSION["准入阶段 Admission：任务能否进入执行"]
        GATE{"Goal / Safety Gate<br/>作用：检查目标是否明确、范围是否允许、是否需要用户决策？"}
        BLOCK0["RunBlocked / Awaiting User Decision<br/>作用：歧义、越权或缺少授权时暂停"]
        DISC["Plan Discovery<br/>作用：探索任务并生成可执行计划"]
        PLAN["PlanGraph + Todo<br/>作用：表达节点、依赖、文件所有权和进度"]
        READY{"Readiness Gate<br/>作用：检查前序 Run、节点依赖、路径所有权和审批前置"]}
        WAIT["Waiting on predecessor / dependency<br/>作用：等待前置条件，不占用 Worker"]
        QUEUE["RunQueued<br/>作用：把可执行 Run 放入持久化队列"]
        GC --> GATE
        GATE --"否"--> BLOCK0
        GATE --"是"--> DISC --> PLAN --> READY
        READY --"否"--> WAIT
        READY --"是"--> QUEUE
    end

    BLOCK0 -.->|用户补充目标或明确授权后重新评估| GC
    WAIT -.->|依赖完成或用户明确 bypass| QUEUE

    subgraph SCHEDULER["调度阶段 Scheduling：谁来执行 Run"]
        SUP["LocalSupervisor<br/>作用：扫描队列、分配 Worker、维护并发"]
        SGATE{"Scheduling Gate<br/>作用：检查 Supervisor 选主、Child/Worktree 冲突和 PlanGraph readiness"]
        LEASE["SupervisorLease → RunLease<br/>作用：选主、锁定 Worker，并用 fencing 拒绝旧写入"]
        SUP --> SGATE
        SGATE --"未就绪"--> WAIT
        SGATE --"就绪"--> LEASE
    end

    QUEUE --> SUP --> LEASE

    subgraph WORKER_LOOP["Worker / Segment 阶段：一个节点内的 Agent Loop"]
        WORKER["RunWorker + Heartbeat<br/>作用：持有 Lease，执行当前 Run"]
        SEGSTART["RunSegmentStarted<br/>作用：绑定当前 PlanNode、Todo 和 Working State"]
        SNAP["Load Run Snapshot / Projection<br/>作用：从事件恢复当前目标、计划、动作和已提交证据"]
        INVOKE["ModelInvocationStarted<br/>作用：记录模型调用、Runtime Contract 和输入快照"]
        KERNEL["Agent Kernel / LLM<br/>作用：根据目标、计划和已提交观察决定下一步"]
        RESP{"模型响应类型？<br/>作用：选择完成、工具动作或恢复路径"]}
        TOOLCALL["ActionPlanned → ActionPrepared<br/>作用：把模型 tool_call 转成结构化 Action Attempt"]
        CAND["CompletionCandidateSubmitted<br/>作用：提交完成候选，不直接宣布 Run 成功"]
        SEGEND["RunSegmentFinished<br/>作用：记录本 Segment 的边界和结果"]
        WORKER --> SEGSTART --> SNAP --> INVOKE --> KERNEL --> RESP
        RESP --"工具调用"--> TOOLCALL
        RESP --"完成候选"--> CAND
        RESP --"没有动作但仍需继续"--> SEGEND
    end

    LEASE --> WORKER

    subgraph ACTION["Action 阶段：执行模型动作前后的 Runtime 控制"]
        CHECK["Action Validation<br/>作用：检查工具契约、参数、范围、权限和审批要求"]
        DEC{"Action Decision<br/>作用：确定 ALLOW、ASK 还是 DENY"]
        DENY["ActionDenied / Tool Error<br/>作用：拒绝动作并把结构化原因反馈给模型"]
        ASK["ApprovalRequested<br/>作用：持久化工具、参数、风险、范围和有效期"]
        APPR{"用户/审批结果？"}
        REJECT["ApprovalRejected → RunQueued + ActionCancelled<br/>作用：保留拒绝证据，不执行本次动作，向模型返回工具结果并继续"]
        ALLOW["ActionAllowed<br/>作用：允许进入实际工具执行"]
        EXEC["Dispatch Tool<br/>作用：调用 Native/MCP/子任务等能力适配器"]
        OBS["ToolObservationCommitted<br/>作用：提交工具结果、状态、错误和副作用证据"]
        UPDATE["Update Working State / Todo / Progress<br/>作用：更新节点进度和可验证证据"]
        CHECK --> DEC
        DEC --"DENY"--> DENY
        DEC --"ASK"--> ASK --> APPR
        APPR --"拒绝"--> REJECT
        APPR --"批准"--> ALLOW
        DEC --"ALLOW"--> ALLOW
        ALLOW --> EXEC --> OBS --> UPDATE
    end

    TOOLCALL --> CHECK
    DENY -.->|模型收到拒绝原因后重新规划| SNAP
    REJECT --> SNAP
    UPDATE --> NODE{"当前 PlanNode 是否完成？"}
    NODE --"否"--> SEGEND
    NODE --"是"--> PN["PlanNodeCompleted / TodoCompleted<br/>作用：推进计划图和节点状态"]
    SEGEND --> LOOP{"Run 还需要继续？"}
    PN --> LOOP
    LOOP --"需要继续或有下一个节点"--> SNAP
    LOOP --"无下一节点"--> CAND

    subgraph VERIFY["完成阶段 Completion：终态必须有证据"]
        VERIFY0["CompletionVerifier<br/>作用：校验 Goal、验收标准、测试/构建证据、范围和未解决错误"]
        VERIFYQ{"完成条件是否全部满足？"}
        REPLAN["继续 Loop / Replan<br/>作用：证据不足、测试失败或计划未完成时继续执行"]
        COMPLETE["RunCompleted + RunOutcomeRecorded<br/>作用：写入成功终态和最终结果"]
        CAND --> VERIFY0 --> VERIFYQ
        VERIFYQ --"否"--> REPLAN
        VERIFYQ --"是"--> COMPLETE
    end
    REPLAN --> SNAP
    COMPLETE --> RESULT["返回用户<br/>Result + Verification Evidence<br/>作用：输出结果、变更和验证证据"]

    subgraph RECOVERY["恢复阶段 Recovery：Worker 崩溃、Lease 过期和副作用不明"]
        LOST["Worker Crash / Heartbeat Timeout<br/>作用：发现 Worker 失联"]
        EXPIRED["WorkerLeaseExpired<br/>作用：推进 Lease epoch，旧 Worker 不能再写事实"]
        REBUILD["Rebuild Run from Events / Checkpoint<br/>作用：恢复同一 Run 的权威状态"]
        INFLIGHT{"未完成 Action 的状态？"}
        SAFE_RETRY["Safe Retry<br/>作用：未开始或可证明幂等的只读动作可重试"]
        UNKNOWN["ActionOutcomeUnknown + RunBlocked<br/>作用：外部副作用可能已发生，禁止盲目重放"]
        RECON["Reconciliation / Human Review<br/>作用：查询外部状态后决定继续、终止或人工处理"]
        END_BLOCK["RunBlocked<br/>作用：无法安全确认外部结果时等待人工处理"]
        LOST --> EXPIRED --> REBUILD --> INFLIGHT
        INFLIGHT --"未开始 / 只读幂等"--> SAFE_RETRY
        INFLIGHT --"外部写入 / 状态不明"--> UNKNOWN --> RECON
        SAFE_RETRY --> QUEUE
        RECON -.->|确认可继续| QUEUE
        RECON -.->|确认失败或无法确认| END_BLOCK
    end
    WORKER -.->|进程崩溃、Heartbeat 超时或机器重启| LOST

    subgraph ERRORS["异常和控制分支 Control / Error"]
        PROVIDER["Provider / Infrastructure Error<br/>作用：模型或基础设施调用失败"]
        RETRYQ{"是否可恢复？"}
        RETRY["Bounded Retry / Requeue<br/>作用：有限退避后重新执行 Segment"]
        FAILED["RunFailed<br/>作用：记录不可恢复失败和失败证据"]
        CANCEL["InterruptRequested / RunCancelled<br/>作用：响应用户取消并保留已执行事实"]
        FOLLOW["FollowUpQueued<br/>作用：把用户后续消息排队为后续 Run"]
        PROVIDER --> RETRYQ
        RETRYQ --"是"--> RETRY --> QUEUE
        RETRYQ --"否"--> FAILED --> RESULT
    end
    RESP --"Provider 错误"--> PROVIDER
    KERNEL -.->|用户 interrupt/cancel| CANCEL
    U -.->|新的普通消息| FOLLOW
    FOLLOW --> WAIT
    CANCEL --> RESULT

    subgraph PERSIST["事实与投影 Persistence：Runtime 可恢复的基础"]
        EVENTS["RunStore：Append-only Run Events<br/>作用：唯一权威事实，保存 Goal、Plan、Lease、Action、Approval、Observation、终态"]
        SNAPSHOT["Checkpoint / Run Projection<br/>作用：从事件重建当前状态，减少恢复成本"]
        AUDIT["Audit / Activation Records<br/>作用：记录模型、Worker、策略、版本和恢复轨迹"]
    end
    R0 -.-> EVENTS
    GC -.-> EVENTS
    PLAN -.-> EVENTS
    LEASE -.-> EVENTS
    CHECK -.-> EVENTS
    OBS -.-> EVENTS
    VERIFY0 -.-> EVENTS
    COMPLETE -.-> EVENTS
    EVENTS -.-> SNAPSHOT
    EVENTS -.-> AUDIT
    SNAPSHOT -.-> SNAP

    classDef terminal fill:#263447,stroke:#f59e0b,color:#fff;
    class COMPLETE,RESULT,END_BLOCK,FAILED,CANCEL terminal;
```

投影快照只是恢复加速器，不是事实源。启动或读取时如果发现旧版本快照缺少
新增字段，Runtime 会先校验快照原始摘要，再从不可变事件流重放；重放成功后只修复
`run_record` 游标和动作/审批/跟进索引，不改写事件或快照。这样升级后的旧会话不会被
`snapshot digest mismatch` 或 `stored projection cursor does not match event rebuild`
反复卡住；若原始快照摘要本身不匹配，则仍按篡改处理并安全拒绝，避免把不可信数据当成状态。

## 英文术语的作用

| 英文术语 | 在 Agent Runtime 中的作用 |
|---|---|
| Run | 一次长任务的唯一持久化执行实体 |
| Goal Contract | 目标、范围、约束和可验证验收标准 |
| PlanGraph | 任务节点、依赖、文件所有权和执行顺序 |
| Todo | PlanGraph 的进度投影 |
| Supervisor | 调度 Run、分配 Worker、回收失效 Worker |
| SupervisorLease | 项目级选主；只限制调度器，不限制同一项目的并行 Run |
| Lease | Worker 的临时执行租约 |
| Fencing / lease_epoch | 防止 Lease 过期的旧 Worker 继续写入 |
| ResourceLease | 动作级文件、工作区和外部副作用冲突锁；共享读取可并行 |
| Worker | 真正执行 Run Segment 的进程 |
| Segment | 绑定一个 PlanNode 的连续模型—工具循环 |
| Agent Kernel | 调用模型并把模型响应转换为结构化动作或完成候选 |
| Action Attempt | 一次工具动作的生命周期和重试记录 |
| ApprovalRequested | 需要人工确认时的持久化审批事件 |
| ToolObservation | 工具执行后提交的结构化观察结果 |
| Working State | 当前 Run 的进度、错误和验证状态 |
| CompletionVerifier | 判断是否具备完成证据，防止模型自称完成 |
| OutcomeUnknown | 外部副作用可能已经发生，但结果尚未确认 |

## WebUI 审批拒绝的展示与续跑

用户在运行面板点击“拒绝”后，Runtime 只取消当前工具动作：事件流记录
`ApprovalRejected`、`ToolObservationCommitted(status=cancelled)` 和
`ActionCancelled(cancellation_reason=user_rejected)`，随后唤醒 Supervisor 重新调度
同一 Run。页面会在会话过程显示“已拒绝（未执行）”，并在实时状态中显示
“已拒绝本次动作，继续运行”；如果模型没有产生可验证的新进展，仍会按完成门规则
显示“已暂停”，用户可以继续发送指令。

如果用户在审批卡片出现后先点击“停止”，`RunCancelled` 会让尚未决定的审批在
投影中变为 `expired`。它仍保留在不可变事件流中供审计，但不会再显示为可点击卡片；
之后从检查点继续时，Runtime 会先提交“未执行”的工具观察并关闭原动作，不会绕过授权
重放该命令。
| RunStore | 保存不可变 Run Event 的事实存储 |
| Checkpoint / Projection | 从事件恢复出的当前状态快照，不是新的事实源 |

## Runtime 主数据流

```text
User Request
→ Create Run
→ Goal Contract
→ PlanGraph / Todo
→ Admission Gate
→ RunQueued
→ Supervisor Claim Lease
→ Worker Start Segment
→ Load Run Projection
→ Model Invocation
→ Action Validation / Approval
→ Tool Execution
→ ToolObservation Commit
→ Update Progress
→ Continue Segment or Verify Completion
→ RunCompleted / Blocked / Stalled / Failed / Cancelled
```

核心原则：Run Event 是唯一事实；Worker、Projection、Todo 和完成状态都必须从持久化事件恢复，不能依赖某个进程内存中的状态。
