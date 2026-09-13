# cc-harness 异常注入与运行时审计报告

**审计日期：** 2026-09-13  
**审计对象：** `cc-harness` Durable Runtime、Memory、Context、Security 四个域  
**执行方式：** 确定性故障注入 + 一次真实模型探针

## 1. 结论先行

本次审计没有把“模型说完成了”当作完成依据。每个用例都在真实事件边界注入故障，并检查事件流、Run 状态、租约 epoch、投影游标/摘要、动作尝试次数、工具结果和证据事件。

- 四域场景矩阵共 **41 项**（A13 + B8 + C10 + D10）：24 项符合当前契约，6 项场景确认存在代码缺陷（拆出 7 个可跟踪问题），11 项部分覆盖或依赖外部基础设施/平台能力，具体见矩阵。
- 新增故障注入测试默认使用可重复的本地 fake；**真实模型仅用于 provider/协议探针**，避免把网络抖动、Docker 或 OpenSandbox 未安装误报成 Runtime 缺陷。
- 真实 DeepSeek 探针成功返回 `done`，没有打印密钥；provider usage 可读，但 `reported_cost` 仍为 `null`。
- 本审计阶段遵守任务书“先找问题再修复”：没有修改 `cc_harness/` 生产代码来迎合测试。报告中的缺陷需要单独进入修复任务。

最需要优先处理的是：动作参数中的命令型凭据没有被完全脱敏、MCP 工具效果依赖服务端自报、L5 脱敏异常时 fail-open、跨 Run 的项目目录互斥没有形成明确代码契约，以及记忆/检查点降级缺少耐久的可见信号。

## 2. 范围与测试方法

### 2.1 覆盖范围

| 域 | 场景 | 注入边界 |
| --- | --- | --- |
| Runtime | A1–A13 | provider 重试、协议解析、动作生命周期、租约围栏、事件/快照、supervisor、审批、取消、停滞、完成门禁、yield/reclaim、子任务图、输出分页/调度 |
| Memory | B1–B8 | durable job 崩溃/重试、检索/决策降级、分层与 tombstone、项目隔离、checkpoint 篡改、注入防护、legacy 导入、并发写入 |
| Context | C1–C10 | 压缩阈值、增量摘要、压缩屏障、提交原子性、不可压缩状态、引用授权、累计摘要、稳定前缀、保留优先级、恢复和 offload |
| Security | D1–D10 | 间接注入、路径边界、权限模式、沙箱 fail-closed、凭据/日志脱敏、egress、MCP、畸形模型输出、expected hash/锁、ANSI/OSC8 |

### 2.2 真实模型探针

| 项目 | 结果 |
| --- | --- |
| provider | `api.deepseek.com`（只记录主机名） |
| model | `deepseek-v4-flash` |
| thinking mode | disabled |
| 请求 | “请只回复：真实模型探针通过” |
| 协议结果 | `content` 流正常结束并收到 `done` |
| usage | prompt 13、completion 5、total 18；cache read/creation 均为 0 |
| 费用 | `reported_cost=null`，说明当前 provider 没有返回可计价字段 |
| 安全 | API key 未写入报告、日志或测试输出 |

PowerShell 控制台对中文内容存在编码显示问题，但不影响 provider 事件顺序和协议状态；这属于终端显示层问题，不判为模型失败。

### 2.3 可复现测试与基线

新增文件：

- `tests/fault_injection/conftest.py`
- `tests/fault_injection/test_fi_runtime.py`
- `tests/fault_injection/test_fi_memory.py`
- `tests/fault_injection/test_fi_context.py`
- `tests/fault_injection/test_fi_security.py`

执行结果：

```text
python -m pytest tests/runtime_rebuild tests/test_context.py tests/test_context_queue.py -q
基线退出码：0（收集 226 项）

python -m pytest tests/fault_injection -q
默认：50 passed, 1 skipped（真实 provider 探针需显式开启）

$env:CC_HARNESS_RUN_REAL_LLM='1'
python -m pytest tests/fault_injection -q
真实模型模式：52 passed
Remove-Item Env:CC_HARNESS_RUN_REAL_LLM

python -m ruff check tests/fault_injection cc_harness
All checks passed
```

Docker、真实网络、OpenSandbox 服务没有在本次自动审计中启动；这些场景需要具备相应凭据/服务的环境才能做无歧义验证，矩阵中明确标为“部分/待验证”。

## 3. 四域覆盖矩阵

状态含义：**符合** = 用例验证到当前契约；**缺陷** = 已复现或由代码路径直接证明的契约违反；**部分** = 测试只覆盖了契约的一部分，或必须依赖未接入的外部环境。

### 3.1 Runtime（A1–A13）

| 编号 | 注入/验证 | 实测结论 | 状态 |
| --- | --- | --- | --- |
| A1 | 408/429/5xx、超时与非瞬态 provider 错误 | 瞬态错误按预算重试，协议错误不盲重试 | 符合 |
| A2 | 畸形 JSON、reasoning 重放、completion marker、未知工具 | Kernel 拒绝畸形参数；reasoning 保留；未知工具走保守错误 | 符合 |
| A3 | STARTED 动作后进程崩溃 | 只读且可重试动作生成取消观察后新 attempt；外部副作用变为 unknown 并阻塞 | 符合 |
| A4 | 过期 epoch/租约抢占 | 旧 worker 事件被 fence，健康租约继续有效 | 符合 |
| A5 | 事件或 snapshot 篡改 | 不可变触发器、序列和 projection digest 检查拒绝篡改 | 符合 |
| A6 | supervisor tick/超时注入 | 单次 tick 超时不会杀死后续 tick；公平性/饥饿仍需压力测试 | 部分 |
| A7 | 审批 digest 不匹配/拒绝 | 未授权参数不能执行；拒绝路径与当前产品契约为重新排队；损坏 artifact 场景未全覆盖 | 部分 |
| A8 | Ctrl-C/InterruptRequested 边界 | 运行中先进入 CANCEL_REQUESTED，释放边界后幂等完成取消 | 部分 |
| A9 | 相同动作+结果指纹 | 连续 3 次才触发 replan，不同结果会重置指纹 | 符合 |
| A10 | CompletionCandidate、证据、错误、unknown、验证命令 | 缺验收、证据、未解决错误或 unknown 都被拒绝；代码修改后要求重新验证 | 符合 |
| A11 | yield/reclaim | yield 后可重新 claim，动作/投影摘要保持一致；跨进程长期恢复仍需 soak | 部分 |
| A12 | PlanGraph 缺依赖、环、所有权冲突 | 图校验拒绝非法依赖和重叠路径；父取消/可选子任务传播未全覆盖 | 部分 |
| A13 | 大输出分页和 scheduler 边界 | continuation cursor 严格存在；只并行一方只读工具；后台 drain 未全覆盖 | 部分 |

### 3.2 Memory（B1–B8）

| 编号 | 注入/验证 | 实测结论 | 状态 |
| --- | --- | --- | --- |
| B1 | pipeline worker 崩溃、重启 | durable job 可重试；running job 重启后 requeue | 符合 |
| B2 | 向量/FTS/LLM 决策服务异常 | 当前 turn 不崩，但只有 warning/NOOP，缺少 durable degradation 事件和 UI 信号 | 缺陷（P2） |
| B3 | supersede/tombstone 中途失败 | 版本和向量清理在同一写事务中完成 | 符合 |
| B4 | 多 project/session 检索交叉注入 | project scope 过滤有效 | 符合 |
| B5 | 直接改写 checkpoint message | 篡改会被读取到，但没有 content digest/不可变链来自动发现或拒绝 | 缺陷（P2） |
| B6 | L3→L2→L1→L0 progressive recall、L5 | 按需逐层召回；敏感内容可脱敏 | 符合 |
| B7 | legacy 导入崩溃/重复导入 | dry-run、幂等、错误记录有效 | 符合 |
| B8 | 并发 add、精确重复 | 写入串行化；CRUD 层允许完全相同记忆重复，冲突由上层决定 | 缺陷（P2） |

### 3.3 Context（C1–C10）

| 编号 | 注入/验证 | 实测结论 | 状态 |
| --- | --- | --- | --- |
| C1 | tier/reserve 阈值互斥 | 压缩层级和输出预留不会同时越界 | 符合 |
| C2 | 增量摘要重复触发 | 只压缩新增片段，不重复生成相同 summary | 符合 |
| C3 | 两个 projection writer 竞争 | writer lease 防止损坏；只验证了写者竞争，摘要失败恢复未全覆盖 | 部分 |
| C4 | commit 中途异常/相同 key 重放 | 提交原子且幂等，冲突可识别 | 符合 |
| C5 | mandatory state 无法压缩 | 状态保留并报告，不静默丢弃 | 符合 |
| C6 | offload ref 越权、digest 错误、信任级别错误 | scope、digest、trust 均校验 | 符合 |
| C7 | 累计 summary 重启 | 旧片段保留并可重建；未来范围/版本拒绝仍需补测 | 部分 |
| C8 | stable prefix、call manifest、缓存 epoch | 内容寻址和不可变事件触发有效 | 符合 |
| C9 | retention 删除压力 | mandatory/permission 状态优先保留 | 符合 |
| C10 | current pointer 损坏、旧版本恢复、大对象 offload | 可回退到不可变候选，offload 字节和 manifest 精确 | 符合 |

### 3.4 Security（D1–D10）

| 编号 | 注入/验证 | 实测结论 | 状态 |
| --- | --- | --- | --- |
| D1 | tool/web/file/attachment/MCP/memory 中的间接指令 | provenance 和 untrusted echo 检查不会授予额外动作 | 符合 |
| D2 | workspace 外路径、敏感路径、symlink/junction | 通用 containment 和敏感路径 hard-deny 有效；UNC/8.3 变体受 Windows 平台限制未跑 | 部分 |
| D3 | Suggest/Ask/Full、capability scope/TTL | hard-deny 优先级不被模式覆盖，capability 到期失效 | 符合 |
| D4 | 缺失 sandbox SDK | dispatch 前 fail-closed；release gate/真实服务 attestation 未验证 | 部分 |
| D5 | 参数、journal、L5 脱敏异常 | key 字段和普通 journal 可脱敏；command 中 secret 及 L5 异常时原文返回存在泄漏路径 | 缺陷（P0/P1） |
| D6 | private DNS/默认 egress deny | 私网解析被拒；HTTP redirect/rebind/host pinning 未在代码契约中明确 | 部分 |
| D7 | MCP 自报 read 但实际写外部资源、断连 | 断连会隔离；成功结果直接信任 server capability，实际副作用没有后置证明 | 缺陷（P0） |
| D8 | 孤立 tool call、缺 id、畸形 args | 不补造 tool result；畸形请求被 Kernel 拒绝 | 符合 |
| D9 | expected_hash 冲突、读写调度 | hash 冲突可拒绝，单批次 scheduler 保守串行；跨 Run 项目目录没有明确互斥锁 | 部分/缺陷（P1） |
| D10 | ANSI/OSC8、诊断摘要提示词泄漏 | 控制序列被清理，摘要不包含规则正文 | 符合 |

## 4. 已确认问题（按严重性）

### FI-D5-01：命令型动作参数可把凭据写入 durable artifact（P0）

- **测试：** `test_d5_argument_and_journal_redaction_is_observable`
- **复现：** 将 `Authorization: Bearer sk-...` 或 token 放入 `run_command.command`/`content`，动作规划会成功持久化。
- **证据：** `cc_harness/worker.py:234-243` 只按敏感字段名递归替换；`cc_harness/worker.py:2750-2753` 将结果写入 `purpose=action-arguments` artifact。
- **根因：** 脱敏没有对命令字符串、脚本正文和 secret-shaped 值做扫描，也没有在 artifact 写入前执行不可绕过的 credential broker/redaction gate。
- **影响：** token 可能进入事件引用、恢复上下文、日志或模型可见的工具摘要，越过凭据边界。
- **建议：** 在序列化边界增加 secret scanner + fail-closed；命令参数使用结构化 argv；artifact/journal 只允许 tokenized value，并为每次拦截写入安全事件。

### FI-D7-01：MCP 效果类别依赖服务端自报（P0，条件性）

- **测试：** `test_d7_mcp_contract_and_transport_failure_are_isolated`
- **复现：** fake MCP server 声明 `effect=read`，但返回“已写入外部资源”；Runtime 仍以 success/read capability 处理。
- **证据：** capability 在 `cc_harness/mcp_client.py:201-210` 保存；`call_tool` 在 `cc_harness/mcp_client.py:277-328` 直接把该 capability 放入成功结果，没有外部副作用后置证明。
- **根因：** MCP transport、声明的 contract 和实际 effect 没有独立信任等级或可验证 postcondition。
- **影响：** 如果 approval/sandbox 根据 read 分类放行，恶意或失误的 MCP server 可绕过最小权限边界。
- **建议：** 未经证明的 effect 一律 `unknown`；mutating/unknown 强制审批和沙箱；引入 server identity、签名 capability、资源变更摘要/审计回执，并在回执不匹配时阻塞。

### FI-D5-02：L5 检测器异常时 fail-open 返回原文（P1）

- **测试：** `test_d5_argument_and_journal_redaction_is_observable` 的 exploding layer 分支
- **证据：** `cc_harness/l5.py:93-107` 捕获所有异常后返回 `ScanOutcome(text, {}, ...)`。
- **根因：** 为了不阻塞 Agent，扫描故障被视为可忽略异常。
- **影响：** 脱敏服务短暂不可用时，API key/PII 可能原样流入 provider、日志或 UI；调用方也无法区分“无敏感信息”和“扫描失败”。
- **建议：** 对 credential、外发、持久化等高风险出口 fail-closed；返回 `scan_error` 状态并阻断或要求显式审批；普通本地展示可单独提供受控降级。

### FI-D9-01：跨 Run 项目目录没有明确的写入互斥契约（P1）

- **测试：** `test_d9_expected_hash_conflict_and_conservative_scheduling`（已覆盖单批次调度）；跨 Run 竞争是代码审计结论，尚未用 destructive command 做压力复现。
- **证据：** `cc_harness/executor.py:901-902` 的 docstring 声称有 cwd 锁，但 `NativeExecutor.run` 在 `cc_harness/executor.py:1598-1666` 直接创建子进程并固定 project root；调度器只在当前批次把一方只读工具并行化。
- **根因：** Run 级 supervisor lease 与项目文件/命令副作用锁没有统一；expected hash 只能防止陈旧覆盖，不能防止两个命令同时产生外部副作用。
- **影响：** 同一目录多个会话可同时执行迁移、安装、生成或测试命令，出现竞态、脏构建和不可逆副作用。
- **建议：** 对“同目录共享”拆分读锁与写锁；mutation/command 使用项目级公平锁或隔离 worktree；锁状态写入 durable manifest，超时可恢复，所有跨 Run 冲突都产生可审计事件。

### FI-B2-01：记忆检索/决策降级只有日志，没有耐久可见信号（P2）

- **测试：** `test_b2_retrieval_degradation_is_logged_and_does_not_break_turn`、`test_b2_decider_provider_failure_returns_explicit_noop_error`
- **证据：** `cc_harness/memory/retriever.py:176-201` 异常时返回空列表/空 block；`cc_harness/memory/decider.py:75-81` 返回 `NOOP(error=...)`，没有 Runtime 事件、Run 状态或 WebUI 状态更新。
- **影响：** 长任务可能在记忆不可用时继续运行，用户只看到结果变差，无法区分“没有命中”和“检索系统故障”。
- **建议：** 记录 `MemoryDegraded`/`MemoryDecisionDegraded` 事件，带 provider、层级、错误分类和恢复次数；按策略在 UI 显示 warning，并让完成证据标记该降级。

### FI-B5-01：session checkpoint 消息没有内容完整性链（P2）

- **测试：** `test_b5_checkpoint_tamper_is_observable_but_not_silently_repaired`
- **证据：** `cc_harness/memory/store.py:222-231` 的 `session_message` 没有 digest/version；`cc_harness/memory/checkpoint.py:82-112` 使用 `INSERT OR REPLACE` 后删除再插入消息。
- **影响：** 数据库被误改或恢复到错误副本时，读取结果会改变，但系统不能自动证明哪个版本是真实 checkpoint。
- **建议：** 为每条消息和 checkpoint 保存 content hash、父 hash、版本和签名/密钥标识；加载时校验链，失败则回退到最近不可变版本并产生事件。

### FI-B8-01：记忆 CRUD 层允许完全重复事实（P2）

- **测试：** `test_b8_concurrent_memory_writers_are_serialized`
- **证据：** `cc_harness/memory/store.py:425-489` 每次 `add` 生成新 UUID 并直接 `INSERT`，没有内容/来源/版本的唯一约束。
- **影响：** 并发或重放会污染召回排序、增加嵌入和存储成本；当前只能依赖上层 MemoryService 冲突决策。
- **建议：** 使用规范化内容 + project/session/layer/source 的 content hash 做幂等键；保留显式“同内容不同来源”的例外并记录合并事件。

## 5. “代码未明确”与外部待验证项

这些不是本地测试已经证明的缺陷，不能在发布说明中写成“已解决”：

1. A6 supervisor 的长期公平性、饥饿和多 Run 调度需要 soak/压力测试。
2. A7 损坏 approval artifact、A8 工具执行中 Ctrl-C、A12 父任务取消/可选子任务传播、A13 后台输出 drain 尚未形成完整闭环。
3. C3 摘要生成器失败后的 durable resume、C7 未来 summary range/版本拒绝需要补测。
4. D2 的 UNC/8.3 路径、symlink/junction 行为受 Windows 文件系统和权限影响，本次只验证了可运行的平台分支。
5. D4 只验证“缺 SDK 直接 fail-closed”；release gate、真实 OpenSandbox server attestation 和恢复重连未运行。
6. D6 DNS 初始解析有 deny-by-default，但 redirect、DNS rebinding、HTTP Host/SNI pinning 的责任边界没有在代码中明确。
7. D7 的“声明 effect”不能当作真实副作用证明；需要带身份和回执的 MCP server 才能做端到端验证。
8. 真实 provider 探针只验证了一轮协议和 usage，不代表 52 个故障用例都在真实模型上重放；真实模型成本字段仍不可用。

## 6. 统计与优先级

按上面的矩阵计数：

| 分类 | 数量 | 说明 |
| --- | ---: | --- |
| 符合 | 24 | 当前实现和测试契约一致 |
| 已确认缺陷场景 | 6 | 对应 7 个问题：P0 2 项、P1 2 项、P2 3 项 |
| 部分/待外部验证 | 11 | 不是“通过”，也不是已证实代码缺陷 |
| 合计 | 41 | A13 + B8 + C10 + D10 |

建议修复顺序：

1. **P0：** D5 命令/脚本凭据落盘；D7 MCP capability 自报导致的权限旁路。
2. **P1：** L5 脱敏 fail-open；同一项目多 Run 的写入/命令互斥。
3. **P2：** Memory 降级耐久事件与 UI、checkpoint 完整性链、记忆内容幂等去重。
4. 补齐“代码未明确”场景的跨进程、长时间和真实 sandbox/MCP 集成测试。

## 7. 交付清单与限制

本次交付的是可重复的四域故障注入测试和审计报告，不是生产修复版本。测试与报告没有访问或修改 `D:\agent_learning\外卖`，也没有启动真实 Docker/OpenSandbox 服务。后续若进入修复阶段，应按上述 P0→P1→P2 顺序逐项改动，并为每项保留回归测试和事件证据。
