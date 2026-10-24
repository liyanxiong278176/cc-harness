# 本地 Agent 短程与长程合成任务实测（2026-10-21）

后续修复与最终复验见：[本地 Runtime 修复报告](local-agent-runtime-repair-20261021.md)。本文保留首次失败结论与原始证据。

结论：执行、摘要、记忆、Skill、重启等入口都能够运行，但当前端到端可靠性不足。最需要处理的是提前宣告完成、失败命令不能正常重试、遗忘后从历史召回旧内容。不能把 runtime 的 `completed` 当成任务通过。

## 范围与方法

- 使用真实配置的 `deepseek-v4-flash`，直接通过当前 `DurableRuntimeClient`、Supervisor、Worker 和实际本地工具运行；没有模拟模型或手写工具结果。
- 固定合成数据种子 `20261021`，每个场景使用独立项目和 runtime 数据目录。外部验收器检查产物，不依赖 Agent 的自述或状态标签。
- 正式结论覆盖 **11 个场景变体、13 次任务运行、90 次主循环模型调用**，主循环输入 795,296 tokens、输出 9,823 tokens。后台摘要、记忆提取／决策调用以及另外四次探索运行未计入这些数字。提供方没有返回完整金额，不能据此给出实际费用。
- 运行使用原生 Windows 工具、`bypass-prompts`、单 worker；thinking 显式关闭。记忆使用本地 256 维 hash embedding；不代表远程 embedding 或其他模型的效果。
- 每次任务限制 28 次主循环模型调用、65 次工具调用、280 秒活跃时间和约 300 秒外部等待时间。固定的是数据与验收规则，模型输出本身不保证逐次复现。
- 本文“长程”指多步骤实现、跨批证据处理、上下文压力与恢复；没有完成小时／天级运行耐久性测试。长任务在早期失败也保留，不包装成成功长跑。
- 本轮只新增测试脚本和报告，没有修改 runtime 产品逻辑来改善分数。

## 结果

| 场景 | 独立验收 | 实际观察 | runtime 状态／主循环调用 |
|---|---|---|---|
| 短程：邮箱归一化函数修复 | 通过 | 去空值、去重、保留首个非空姓名、排序、输入不变均正确；Agent 编写的回归测试也运行了 | `stalled` / 11 |
| 短程：坏 JSON 配置导致构建失败 | 不通过 | 没修复配置，缺少 receipt.json；带输出管道的构建命令却被记录为成功验证 | `completed` / 3 |
| 短程：强制真实失败→修复→原命令重试 | 不通过 | settings.json 已修复，build.py 保持不变；相同构建命令未重新执行，只有一次 run_command，24 次 RunYielded，最终耗尽 28 次模型调用预算 | `paused` / 28 |
| 长程：订单去重、过滤、整数聚合、诊断与异常输入处理 | 不通过 | pipeline.py、totals.json、diagnostics.json 均不存在，检查环境时就被提前收尾 | `completed` / 4 |
| 长程：8 批噪声报告，32K 上下文压力 | 不通过；保护机制触发 | 触发 4 次真实摘要，仍超预算，等待输入；没有生成审计产物，也没有触发原定 marker 重启条件 | `waiting_event` / 6 |
| 长程：安全边界暂停、重建 client、修订需求后续跑 | 部分机制通过，任务不通过 | 持久化暂停后重新 claim；Skill 固定版本保留；新要求存在 CONTINUATION.json；随后摘要 3 次仍超预算，没有最终产物 | `waiting_event` / 6 |
| 长程：报告卸载与 read_ref 回读 | 部分触发，任务不通过 | 调用 read_ref 两次，返回的仍是下一层 offloaded 引用；没有证明取得精确原文，audit.json 不存在 | `completed` / 6 |
| 同题对照：没有 ACME Skill | 未生成合规导出 | 缺少私有规范；最后仍在查找资料，runtime 已收尾，没有完成缺失信息说明 | `completed` / 8 |
| 同题对照：启用 ACME Skill | 导出内容通过，生命周期不通过 | 调用 read_skill；schema、owner、排序条目、SHA256 均正确；执行附带校验脚本得到 ACME VERIFIED v1 | `stalled` / 11 |
| 跨任务记忆：教偏好→重启→召回→忘记→重启→再问 | 记住／召回通过；遗忘不通过 | 正确自动保存并召回中文、章节顺序和随机标记；忘记后记忆表为空，但 L0 仍返回旧回答／旧工具结果并泄露标记 | 三个任务均 `completed` / 1+2+2 |
| 新项目记忆隔离 | 通过 | 同一问题在新的独立项目返回 UNKNOWN，没有带出随机标记 | `completed` / 2 |

Skill 对照是“有无项目私有规范”的可用性试验。它证明本次 Skill 能提供并应用所需知识，不能证明对所有任务的普遍收益，也没有测试相同知识量下的 token 节省。无 Skill 时，正确行为可以是说明信息缺失，因此不把无法猜出私有约定本身算成模型错误。

## 已有证据的缺陷与修复顺序

### 1. 完成门槛会产生假阳性

`worker.py` 的 `_VERIFICATION_COMMAND_RE` 接受 `git status` 等检查，`_try_accept_completion` 还会在一批工具执行后合成完成候选。订单任务只运行到 `python --version; git status --porcelain; ...`，就记录成功验证并接受完成，代码尚未创建。构建任务则把包含真实报错、但 shell 最终返回成功的管道当成通过。

建议：完成判定需要当前验收目标对应的明确产物／断言；工具成功只能证明动作执行成功，不能自动证明用户目标完成。版本、目录、git 状态查询不应单独满足任务。验证失败的原始退出状态需要保留。

### 2. 失败后的合法重试被语义去重吞掉

严格重试任务先运行构建并失败，随后读取、修复配置。模型反复提出相同构建命令，实际只执行过一次。`_deduplicate_action_requests` 对 `FAILED` 的相同语义 key 直接跳过，只有 read-only 工具具有 `_refresh_completed_reads` 机制；本次 run_command 没有在配置变更后获得新执行机会。

建议：区分恢复同一副作用的重放与输入已变化后的新验证尝试，给后者关联工作区／输入版本。不要通过全局关闭去重来修复。

### 3. 已遗忘信息通过 L0 历史派生内容复活

真实自动提取保存了 Borealis 偏好和两条任务指令。遗忘后 active memories 为空、epoch 增为 1。但再问时，memory_recall 的实际工具结果包含 `[L0] assistant` 和 `[L0] tool`：内容来自此前召回任务，它们包含旧记忆标记。新任务继续答出 `BOR-7e92c1`。

`MemoryStore.forget` 目前按原记忆所在 session 和文本匹配撤回部分历史源，`search_conversation` 的过滤没有覆盖跨 session 的工具召回结果和回答派生链。

建议：保持原始历史可审计，同时以来源／派生关系禁止已撤回内容再次进入记忆召回；不能仅删除 L1 向量或只过滤最初用户消息。后续验收必须包含“召回过一次后再忘记”，避免只测从未使用的记忆。

### 4. 修复成功后，历史失败仍可能阻止收尾

归一化任务首次 Write 缺少 expected_hash，被正确拒绝；模型读取后重新写入并通过测试，最终仍 stalled。Skill 任务正确生成并验证了导出，但一次资源 Read 被安全策略拒绝后也 stalled。两个完成路径都需要检查“仍未解决的失败”与“已经由后续动作恢复的历史失败”的区别。当前候选合成检查遍历不同 action_id 的失败记录，不能仅以历史上出现过失败作为永久阻碍。

### 5. 上下文与 Skill 的集成还需补测／调整

- 32K 是人为施压配置，DeepSeek 预检有效窗口还会降至 23,040 tokens。四次／三次摘要后仍无法继续，保护性等待有效，但压力任务失败，不能宣称长上下文能力通过。
- read_ref 的两次实际返回都还是 offloaded 指针。需要进一步排查重复卸载／多层引用，加入原文可达性和精确哈希验收。
- Skill 的 snapshot 中脚本路径位于 `.cc-harness/skill-runs/.../scripts/check.py`，普通 Read 被判为敏感凭据路径；模型后来通过命令执行了脚本。应让合法固定 Skill 资源通过一致的专用访问路径使用，不能简单放开整个内部目录。
- 记忆提取除稳定偏好外还保存了“本次不要修改 policy.yaml”等任务限制，说明稳定性筛选仍偏宽。

## 证据与复现

执行器：[`scripts/local_agent_trials.py`](../../scripts/local_agent_trials.py)。示例：

```powershell
python scripts/local_agent_trials.py --output eval/result/local-agent-trials-new --only short_fix short_retry_strict long_pipeline long_context_restart long_references skill_off skill_on memory memory_isolation
```

必须使用新的输出目录；脚本拒绝覆盖已完成的同名场景。依赖项目已有模型配置，不把凭据复制到任务目录。它会保存固定数据、目标与标准、代码脚本、run_id、事件、projection、模型用量、原始 CAS 和独立结果。

本次机器汇总及代码指纹：[`assessment.json`](../../eval/result/local-agent-trials-20261021/assessment.json)。各场景的 `initial/events.json`、`projection.json`、`decoded-messages.json`、`decoded-observations.json` 可逐项复核。

- 第一轮：`eval/result/local-agent-trials-20261021/`。
- 正确 conversation 模式的记忆链：`eval/result/local-agent-trials-20261021-followup/memory/`。
- 有效安全重启：`eval/result/local-agent-trials-20261021-restart/long_context_restart/`。
- 回读及跨项目隔离：`eval/result/local-agent-trials-20261021-extra/`。
- 严格失败重试：`eval/result/local-agent-trials-20261021-retry/short_retry_strict/`。

四次探索运行保留但不混入正式结论：第一轮的三个记忆探针误用 coding 模式；followup 中的重启脚本在重建 client 后读取了暂停前旧 projection，退出时任务仍 queued。前者改用 conversation 模式、后者修正脚本后均在新目录重跑。第一轮 started.txt 缺失导致外部验收器抛异常，汇总中已改为明确的“不满足产物标准”，没有计为 Agent 异常。

所有结论都来自小样本合成任务。未覆盖强杀进程时未知副作用的精确一次恢复、多 worker 并发、远程 embedding 质量、多个模型、多次统计重复、小时级稳定性和长期记忆漂移。
