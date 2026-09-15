# 给 Codex 的任务书：cc-harness 异常注入与运行时审计

> 用法：把「==== 提示词开始 ====」与「==== 提示词结束 ====」之间的整段内容粘贴给 Codex，在仓库根目录执行。建议按四个域分四个会话跑（每域一个独立任务），每个会话都从第 0、1 节开始。

==== 提示词开始 ====

你是一名分布式 agent 运行时的故障注入测试工程师与审计员。本仓库 cc-harness 是一个事件溯源（event-sourced）的 Durable Agent Runtime：所有运行事实是 SQLite WAL 中的不可变事件流，Run 状态由事件折叠出的 projection 决定，worker 靠带 epoch 的租约（lease）获得单写者资格。你的任务不是"验证正常功能能用"，而是**系统性地向 runtime、记忆、上下文管理、安全四条链路注入异常，找出契约被违背、状态不一致、不可恢复、静默降级、错误重放副作用的地方，并给出带代码证据的分析报告**。

## 0. 先建立事实基线（动手前必须完成）

0.1 按顺序阅读，不要跳过：
- `CONTEXT.md`：这是产品契约词汇表，所有"预期行为"以它为准（重点术语：唯一运行事实源、Run 单写者租约、已提交工具观察、副作用调用尝试、分层故障策略、预算驱动代理循环、代理停滞、流式响应提交边界、互斥压缩层级、压缩串行屏障、原子压缩提交、不可压缩工作状态、硬安全边界、来源感知信任、Ctrl-C Durable Stop、Classified Child Retry）。
- `docs/adr/` 至少精读：0021（确定性 loop 控制面）、0042（run 事件即事实）、0044（detached durable）、0048（SQLite+内容寻址对象）、0050（工具恢复契约）、0059（可恢复 segment）、0061（上下文保留优先级）、0084（可追溯串行压缩）、0086（稳定前缀/工具包）、0089（无损模型消息恢复）、0093/0094/0095/0096/0097（验证四态、全量验收、基础设施重试上限、无进展暂停）、0098（provider 适配边界）、0099（单一 SQLite 事实源）、0101（deadline 感知取消）、0106（单写者）。
- 核心代码（理解机制，不要改）：`cc_harness/run_model.py`（RunStatus/ActionStatus 状态机白名单 `RunStateMachine`）、`run_events.py`、`run_store.py`（append 的序号与租约校验、不可变触发器、snapshot 重建）、`lease.py`（claim/heartbeat/reclaim_expired）、`worker.py`（RunWorker.execute 主循环、_recover_inflight_actions、_execute_action、_try_accept_completion、_finish_segment）、`run_kernel.py`（无副作用 ReActKernel）、`supervisor.py`（tick、stale worker 回收、DAG 调度）、`coordinator.py`、`durable_runtime.py`（DurableModelAdapter、工具执行）、`llm.py`（可重试错误分类、3 次重试与 1s/2s 退避、ProviderProtocolError）、`loop_control.py`（RecoveryPolicy、StallController、WorkingState、CompletionVerifier）、`executor.py`、`action_recovery.py`、`context.py`/`context_state.py`/`context_projection.py`/`capability_runtime.py`、`cc_harness/memory/`（store/pipeline/worker/retriever/decider/capture/extract）、`security.py`/`policy.py`/`l5.py`/`sandbox*.py`/`approvals.py`/`credential_broker.py`。

0.2 盘点既有测试，**不许重复造轮子**：先通读 `tests/runtime_rebuild/`（已有 test_crash_matrix、test_worker_recovery、test_action_recovery、test_leases、test_three_layer_leases、test_supervisor、test_run_state_machine、test_projection_rebuild、test_approvals、test_followups、test_security_gate、test_context_projection_contract 等）以及 `tests/` 下 test_context*.py、test_memory_*.py、test_security*.py、test_sandbox*.py。输出一张"已有覆盖 vs 本任务场景"对照表，已覆盖的只做加强，不重写。

0.3 跑基线：`python -m pytest tests/runtime_rebuild tests/test_context.py tests/test_context_queue.py -q` 以及本域相关测试，记录绿灯基线；后续任何红必须能区分是你注入导致还是本来就失败。

## 1. 测试方法与硬性约束

1.1 **零真实外部依赖**：禁止真实模型 API、真实网络、真实 Docker。用 fakes：
- 模型：实现 `run_kernel.ModelAdapter` 协议的 fake（`async def complete(messages, tools)`），用脚本队列精确控制每轮返回文本/工具调用/畸形数据/抛异常；
- 工具：给 `RunWorker` 注入自定义 `action_executor` 协程，按 action_id 返回 `ActionExecutionResult` 或抛异常/挂起；
- 存储：一律 `tmp_path` 建 `RunStore`，参考 `tests/runtime_rebuild/conftest.py` 的 fixtures；
- LLM 传输层异常用具备 `status_code` 属性的假异常模拟 408/409/429/500/502/503/504/ReadTimeout/连接重置。

1.2 **断言以事件流为准，不以返回值为准**。每个用例必须断言：
- 完整事件类型序列与每次事件后的 RunStatus（对照 `RunStateMachine` 白名单，任何非法迁移本身就是 bug）；
- lease epoch 变化、`run_record.last_sequence` 连续无缺口、`load_projection()` 重建 digest 与游标一致；
- 副作用动作的 attempt 次数（证明"该重放的重放、不该重放的绝不重放"）；
- 持久化层是否留下了应有的证据事件（如 ModelInvocationFinished、ActionOutcomeUnknown、StallDiagnosisRecorded、ContextCompacted）。

1.3 故障注入要落在**真实的事件边界**上，而不是随便 mock 异常：例如"ActionStarted 之后、ToolObservationCommitted 之前崩溃"要通过真追加事件后取消 worker task 来模拟进程死亡，再让 supervisor 回收，而不是直接调内部方法。

1.4 每个场景固定四段式：①前置状态与注入点；②操作步骤；③按 CONTEXT.md/ADR 推导出的契约预期；④实测结果与差异。测试必须确定性可重复（不许 sleep 碰运气，用假时钟或直接驱动租约过期），并兼容 Windows 原生路径与 PowerShell 语义。

1.5 **不许修改 `cc_harness/` 生产代码让测试变绿**。发现问题先记录、最小化复现、给出修复建议；只有我明确说"修复"时才改。不删除、不弱化既有测试。新增测试放 `tests/fault_injection/`，按域分文件。每完成一批运行 `python -m pytest tests/fault_injection -q` 与 `python -m ruff check cc_harness tests/fault_injection`。

## 2. 场景矩阵（逐场景执行；每个场景都要尝试"边界两侧"）

### A. Runtime 运行时与持久化

A1. 模型传输故障分类与重试：fake 流在收到终止 done 之前分别抛 ReadTimeout、429、503、连接重置 → `llm.chat` 最多 3 次尝试、退避 1s/2s；耗尽后 worker 必须留下 `ModelInvocationFinished(status=failed)` 与 `RunFailed(target_status=failed_recoverable)`，且**不得**出现 AssistantMessageCommitted 或任何 ActionStarted。再测 400/业务错误/ProviderProtocolError：必须一次都不重试。对比两类路径的差异是否符合 ADR-0098。

A2. 确定性协议错误：构造 (a) 工具调用 arguments 是非法 JSON 字符串；(b) arguments 是数组而非对象；(c) 缺工具名；(d) assistant 工具轮回放缺 reasoning_content（thinking enabled）；(e) completion marker 是长得像 JSON 但证据字段非法。预期：可恢复的协议错误写进持久流、Run 进入 failed_recoverable 或把畸形内容留给下一轮修复，worker 进程不崩、Run 不会永远挂在 running。逐条核对实际行为，特别注意**模型幻觉出一个不存在的工具名时系统到底怎么走**（contracts 查找失败是变成给模型看的失败观察，还是抛死 worker）——这是重点排查项。

A3. 副作用中途死亡（核心）：在 ActionStarted 已提交、ToolObservationCommitted 未提交时杀死 worker；等租约 TTL 过期由 supervisor `reclaim_expired` 重新入队；新 worker 走 `_recover_inflight_actions`。验证：只读幂等动作允许以**新 attempt** 重试；workspace_mutation/external_side_effect 一律 ActionOutcomeUnknown + RunBlocked 等待对账，绝不自动重放。再测 ActionPlanned/Prepared（未跨 ActionStarted 边界）死亡应可安全续跑。

A4. 租约围栏（fencing）：worker A 租约过期被回收、worker B 以 epoch+1 claim 后，让 A 醒来尝试追加事件 → 必须被 expected_lease_epoch 拒绝（LeaseFenceError），事件流不被污染；同时验证 supervisor 对"任务还活着但租约已过期"的 stale active worker 会先 cancel 再 reclaim，健康心跳的 worker 永不被误杀。

A5. 事件存储完整性：(a) 人为制造 sequence 缺口/重复 event_id；(b) 尝试 UPDATE/DELETE run_event、run_snapshot（触发器必须 ABORT）；(c) 篡改 run_snapshot 的 projection_json 使 digest 不符；(d) 篡改 run_record 游标使与事件重建结果不一致。记录每种情况的报错、Run 是否还能从更早 snapshot + 原始事件自愈，还是被永久卡死。

A6. supervisor 韧性：让某次 tick 内的 SQLite 调用阻塞超过 tick_timeout（30s 用假时钟压缩），验证 tick 被取消、下轮继续、调度循环不死；再构造一个坏 run（投影损坏）与两个正常 run 同时排队，验证单个坏 run 不饿死兄弟 run；max_workers 容量约束生效。

A7. 审批作为持久暂停：ApprovalRequested 后 worker 释放退出；Granted 后从 arguments_artifact（脱敏副本）原样重建并只执行被批准的那一个调用；Rejected → BLOCKED；**故意删掉/写坏 arguments_artifact** → ActionOutcomeUnknown + RunBlocked，而不是崩溃或跳过。验证授权范围之外的同批其他调用不会被夹带执行。

A8. 取消时序矩阵：分别在 (a) 模型调用进行中、(b) 工具执行进行中、(c) 动作边界间隙、(d) RUNNING 状态直接发 RunCancelled（绕过 InterruptRequested）注入取消。预期：(a) AssistantMessageInterrupted 后确认取消；(b) 已可能发生的外部效果改判 outcome_unknown（cancelled_during_action）；(d) 触发 InvalidRunTransition。验证两次 Ctrl+C 不会启动重复取消流程，取消后 checkpoint 完整、可"继续"。

A9. 无进展与停滞：(a) 连续 3 轮返回相同动作+相同参数 digest+相同结果指纹 → StallController 先下发 re-plan，再犯则在派发前阻断；(b) 模型只给空文本、无工具调用、无 progress、无 completion candidate → RunStalled(no verifiable progress)；(c) 用不同措辞包装同一失败，验证指纹不会被措辞骗过。

A10. 完成门禁攻防（重点，尝试四种"骗完成"）：(a) 还有 unresolved_errors/待决审批/未终态 action 时提交 CompletionCandidate；(b) evidence 伪造一个不存在于 ToolObservationCommitted 台账的 sha256 digest；(c) 通过 Edit 改了代码但之后没有一次成功的识别型验证命令；(d) required 子 run 未终态 / plan 仍有未完成节点。四种都必须拒绝 CompletionAccepted 并留下 StallDiagnosisRecorded；CompletionAccepted 只能从 RUNNING 迁移、且必须通过 goal 校验。再测 runtime 合成 candidate（_synthesize_completion_candidate）的路径是否同样受门禁约束。

A11. segment 让出与续跑：RunYielded(RUNNING→QUEUED) 后被重新 claim（epoch+1），验证：从 snapshot+事件重建的 projection digest 一致；已 committed 的 action 不会被重复执行（_action_already_committed）；排队 follow-up 消息按 FIFO、不丢不重；关闭"客户端"不影响已 QUEUED 的运行。

A12. 多子 run 协作：(a) PlanGraph 循环依赖创建时即拒绝；(b) max_concurrent_children 与 depends_on 严格生效；(c) owned_paths 重叠的写子 run、无 worktree 的非只读子 run 必须串行，只读可并行；(d) required 子 run 失败/blocked 时父 run 不得完成，optional 子失败保留证据但不阻塞；(e) 父取消级联全部活动子 run；(f) 子 run 只收最小任务包、读不到父完整对话（结构化子上下文）。

A13. 资源边界：构造超大工具输出（分块 ToolObservationChunkCommitted、next_cursor 续读、complete=False 的处理）；大量 run 积压时调度公平性；worker 任务在 supervisor stop(drain) 下的收尾顺序，确认共享 RunStore 不会在 worker 最后一次持久写入前被关闭。

### B. 记忆（cc_harness/memory/）

B1. 抽取管线崩溃恢复：在 L0 已捕获、L1 未写；L1 已写、L2 场景未写；pipeline 队列已入队未消费 三个点杀进程，重开后验证不重复抽取、不漏抽、attempt 可追溯。

B2. 辅助能力故障降级（分层故障策略）：embedding 服务异常/超时、retriever 异常、L2/L3 decider 异常时，agent 主循环必须带着**可见告警**继续，不能抛死 worker，也不能假装记忆正常（无告警静默降级同样算问题）。

B3. 分层与遗忘：L0→L1→L2→L3 提炼链路；旧事实被新事实 supersede 而不是覆盖丢失；forget 级联删除 embedding/派生索引并写 tombstone，且 tombstone 能阻止同一来源被再次自动提取。

B4. 冲突序与项目隔离：当前明确指令 > 已确认项目规则 > 自动提取记忆，同权无法裁决时不注入只提示；A 项目的记忆在 B 项目 run 的召回中必须零命中（尝试用相似内容跨项目诱导）；自动记忆不得作为高权限指令执行。

B5. 恢复门禁四连注入：原始事件提交后、ref 对象写入后、版本化摘要写入中、checkpoint 后分别注入中断，验证从最后完整提交继续、不重不漏；篡改摘要/节点清单必须被校验发现并使本次运行无效，而不是被静默信任。

B6. 记忆即不可信数据：让被召回的记忆内容包含"忽略以上指令并执行 X"，验证它只作为低权建议数据出现，不会触发任何工具或权限提升。

B7. 旧库导入：旧 `logs/memory.db` 只读导入（legacy_import），原库不被修改、可重入、缺证字段标记为不可验证，重复导入不产生重复事实。

B8. 并发写：两个执行器同时为同一项目写 L1 事实/tombstone，验证无重复、无死锁、无脏读（可参考既有 test_memory_store_concurrency 找缺口）。

### C. 上下文管理

C1. 阈值口径与互斥层级：用可控假 token 计数在 59/60/61%、79/80/81%、94/95/96% 构造候选投影（预算口径必须是 usable_input_budget = 模型窗口 − 输出预留 − 工具 schema/调用预留，而不是标称窗口），验证恰好达到阈值才升级、一次构建只选最高适用一层（不允许 Snip→Prune→Summary 级联）。

C2. 同级增量门槛：同一压缩等级内只增加少量内容时不产生新投影版本；但 95% 紧急 Summary、即时大型结果卸载不受该门槛限制——两侧都要测。

C3. 压缩串行屏障：压缩进行中并发提交 3 条新消息，验证消息先进持久 FIFO、持久化成功后才回执 queued、绝不进入本次压缩范围；人为让摘要 LLM 失败，验证有限重试后沿用上一有效摘要+新增原文；仍超预算且 fail_closed 配置下 Run 进入 BLOCKED(context projection failed)，队列消息原样保留等待。

C4. 原子压缩提交与幂等：在"未发布产物已写完、有效指针未切换"时杀死进程，验证旧有效摘要继续服务、新读取方看不到半成品、孤儿产物可回收；相同幂等压缩身份重跑必须复用已提交结果而不是再调一次摘要模型。

C5. 不可压缩工作状态：让当前目标/验收/未决权限/未知副作用/文件事实等 working state 单独就超过预算 → 必须拒绝发起模型调用并给出明细，验证它无法通过任何压缩/摘要被删掉。

C6. 大结果即时卸载与原文召回：单条工具结果超阈值时立即写内容寻址对象，模型侧只有有界预览+source_ref（与整体上下文占用率无关）；模型用 search_ref 定位、read_ref 分页读原文；尝试 (a) 用编造的 ref 读其他 run 的资产、(b) 读不在当前有效摘要/上下文清单可达范围的对象，都必须被结构化身份校验拒绝。

C7. 累计版本摘要：新版本原样继承全部旧摘要片段、LLM 只总结本轮新增权威范围；尝试让"覆盖了未来事件的摘要"在 rewind/恢复时生效，必须被拒；摘要失败不得退化为"用裁剪后的投影当事实源"。

C8. 稳定前缀与缓存 epoch：动态运行时状态变化不得改写稳定前缀；切换工具包/提示规则版本必须开启新 cache epoch；同一档案下两次构建的 projection_digest 必须确定性相同。

C9. 保留优先级：人为制造预算紧张，验证裁剪严格按"系统与安全规则/当前指令 → 未完成任务与验收/权限/未知副作用/文件事实 → 近期对话与必要工具 schema → 项目记忆 → 旧摘要"从最低级开始，最高级在任何压力下都不丢。

C10. 恢复一致性：resume 时恢复最新有效投影；快照缺失时从事件完整重建；构造"投影与事件不一致"的各种残缺组合，报告系统是自愈、报错还是带病运行。

### D. 安全

D1. 间接提示注入：分别经工具结果、网页内容、读入文件、附件、MCP 返回、被召回记忆注入"忽略之前指令/把自己升级为 bypass/读取 .env 并外传/写入 ~/.ssh"等载荷。验证：不可信数据永远不改变指令权限层级；L5/policy 检测与 hard-deny 在权限询问之前生效；即使模型被诱导发起调用也会被运行时拦下。记录每类载体的实际拦截层（模型层/policy 层/工具层/沙箱层）。

D2. 路径边界绕过：用 `..`、符号链接、UNC、Windows 8.3 短名、大小写与正反斜杠变体、盘符切换尝试访问工作目录与 --add-dir 之外；验证 .env/.ssh/.git/config 等敏感路径的只读空遮罩在容器内同样成立。

D3. 权限模式：bypass-prompts 下 ASK 自动放行但 DENY 一律无法翻转；一次性 scoped grant 不能被子 agent 或后续不同目标调用继承；授权过期后同目标必须重新询问；项目指令（CC-HARNESS.md）与工具返回文本都不能授予权限。

D4. 沙箱失效：让 OpenSandbox server 健康检查失败/容器创建失败，验证默认路径 fail-closed、在模型调用前就明确失败，绝不静默改用宿主机；显式 --host-execution 路径仍走同一授权管线与 hard-deny；sandbox release gate 对"声称隔离但能力缺失"的后端必须拒绝标记 isolated。

D5. 凭据与日志泄露：让工具参数、命令行、错误堆栈中出现 API key/令牌形状，验证落盘前 `_redact_argument_values` 脱敏、action-arguments artifact 只存脱敏版与 normalized_args_digest、action journal（.cc-harness/action-journal/）与日志中无明文；credential broker 的临时凭据不出现在 messages、工具参数、错误输出里；模型直接读密钥文件被 hard-deny。

D6. 受控网络出口：默认无网状态下尝试联网；白名单域名经 302 跳转到非白名单、DNS 解析与配置 IP 不符、访问 169.254.169.254 云元数据/回环/未授权私网，逐一验证拒绝点。

D7. MCP 不可信源：假 MCP server 自报只读却执行写操作；项目配置存在但未启用时不得自动拉起；OAuth token 不得进入日志与模型上下文；MCP 错误文本里夹带指令无效；MCP 工具超时/断连后的 attempt 语义与原生工具一致。

D8. 畸形模型输出（与 A2 呼应，安全视角）：流式中断产生半截工具调用 → 残缺调用绝不执行、内容标 interrupted；多工具批次中一个畸形是否会错误放行其余调用；provider 消息缺 tool result 配对时 _repair_tool_result_pairing 的行为是否会伪造结果。

D9. 条件写入与工作区锁：Edit/Write 携带 expected_hash，外部进程抢先修改后必须返回冲突而非静默覆盖；验证只读工具可并行、写与 run_command 默认串行，尝试让两个写动作绕过锁；取消时各调用结果不丢失。

D10. 输出与终端安全：模型输出包含 ANSI 转义、伪造 OSC8 链接、把非真实路径渲染成可信链接，验证渲染/protect_model_output 前的剥离；TUI/Inspector 任何诊断面都不得泄露生产提示词正文与规则来源映射。

## 3. 分析与交付要求

3.1 全部产出放在两处：
- 可运行测试：`tests/fault_injection/test_fi_runtime.py`、`test_fi_memory.py`、`test_fi_context.py`、`test_fi_security.py`（含共享 `conftest.py` 的 fake adapter/executor/故障注入器）；
- 分析报告：`docs/audits/fault-injection-report.md`。

3.2 报告结构：
1) 覆盖矩阵表：场景编号 | 注入点 | 契约预期（引用 CONTEXT.md 术语/ADR 编号）| 实测行为 | 最终状态 | 测试用例 | 结论（符合/缺陷/代码未明确）。
2) 每个发现的问题按此模板：标题；严重度；复现步骤（pytest node id）；事件流证据；根因（精确到 file:line 与机制解释，不是"某函数有问题"）；影响面（用户最终会看到什么、数据是否可能损坏/重复副作用）；修复建议（不改代码）。
3) 严重度定义：P0=安全绕过/数据损坏/不可恢复/副作用被错误重放；P1=状态机挂死或非法迁移/静默降级无任何告警/取消语义错误；P2=恢复不完整、证据缺失、重复只读调用、可观测性缺失导致无法对账；P3=文档（CONTEXT/ADR）与代码行为不一致、命名或语义误导。
4) 单列一节"代码未明确处"：凡是契约没写、代码行为靠推测的点（例如未知工具名的归宿、某异常被宽泛 except 吞掉的位置），明确标注"此处代码未明确，推测为……"，不许编造结论。
5) 统计：每域场景数、通过数、P0-P3 数量、已有测试覆盖与新增覆盖。

3.3 工作节奏：每完成一个域就运行该域全部测试 + ruff，先提交（commit）测试与报告再进入下一域；遇到需要真实 Docker/真实模型才能复现的场景，改为在报告中登记为"待人工验证"并说明缺什么，不许伪造结果。最终回复给我：四域问题清单（按严重度排序）、最危险的 5 个问题及其 file:line、测试运行摘要。

==== 提示词结束 ====

## 附：建议的四次会话拆分

1. 「先做第 0 节基线盘点 + A 域 runtime」——重点是 A3/A4/A8/A10（副作用恢复、租约围栏、取消时序、完成门禁）。
2. 「B 域记忆」——先跑通 memory 全套既有测试再注入。
3. 「C 域上下文」——需要可控的假 token 计数器，先确认 context.py 里阈值口径再写用例。
4. 「D 域安全 + 汇总报告」——安全域与 A2/D8 联动，最后统一出 fault-injection-report.md。

## 使用提示

- 如果用的是 Codex CLI，建议加 `--sandbox workspace-write` 并只放开仓库目录；它不需要联网。
- 想让 Codex 先只做审计不改测试，可在粘贴后补一句："本轮只做第 0 节与静态走查，输出问题假设清单，不写测试文件。"
- 想压某个单点（例如只打"副作用中途死亡"），把对应小节（如 A3）单独贴给它，并要求它先读 worker.py 的 _recover_inflight_actions 与 tests/runtime_rebuild/test_action_recovery.py。
