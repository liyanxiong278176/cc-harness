# Runtime / Loop、上下文、记忆、Skill 测试与修复

日期：2026-10-21。运行平台：Windows，Python 3.13。

本轮在现有工作区修改之上构造确定性测试，使用真实 SQLite、sqlite-vec、内容寻址产物、固定 Skill 包与 durable worker；模型响应和执行器采用可计数替身。没有调用在线模型，没有把此前真实任务报告算作本轮运行结果。原工作区尚未提交的修改均保留。

## 测试矩阵

新增 `tests/test_runtime_context_skill_matrix.py`，41 项参数化测试；另在 `tests/fault_injection/test_fi_runtime.py` 新增 1 项完整 worker 恢复测试，并更新 3 项租约测试以符合现有暂停/显式继续契约。

| 对象 | 正常 | 异常 | 边界 |
|---|---|---|---|
| Agent runtime / loop | JSON 参数解析；稳定生成动作标识；现有多步骤执行、完成门槛和恢复回归 | 非对象响应、非对象工具调用、错误工具名、重复动作标识；未知副作用恢复阻塞及核对后继续 | 取消前零模型调用；空/错误类型调用集合；租约换代 fencing；安全读重试 |
| 上下文 | 并行结果按声明顺序投影，源历史保持不变 | 无 ID、非对象、重复声明与结果配对；现有摘要失败/损坏恢复测试 | 三档压缩阈值恰好相等；现有窗口溢出、保护区、摘要重启与回读权限测试 |
| 记忆 | 更新后关闭重启，精确文本与向量召回一致 | 撤回来源不能通过另一条记忆的 update 重新引入；失败后原值保持 | 向量/全文召回 k=0、k=-1；现有并发写入、作用域、卸载阈值和撤回测试 |
| Skill | 修改源文件、禁用并重建 registry 后，旧任务仍读取原固定字节；新任务采用新配置 | 参数类型错误返回工具错误；物化文件被修改、缺失环境依赖、跨任务快照误用 | 2,000,000 字节可用、2,000,001 字节拒绝且不写入快照修订；路径穿越、绝对路径、空资源名和非精确成员 |

本轮新增 42 项全部通过。扩大回归覆盖 `tests/fault_injection`、`tests/runtime_rebuild`、loop control、所有 `test_context*.py`、所有 `test_memory*.py`、本地资产与之前真实任务的确定性回归。

## 原因与修复

| 问题 | 复现与原因 | 修改 |
|---|---|---|
| 模型协议错误分类失效 | None、列表或错误形状工具调用进入 `.get()` / `dict()`，泄露 AttributeError/TypeError；非字符串或全空白工具名被接收；假值错误集合被当作空调用 | `run_kernel.py` 显式检查响应、集合、成员和工具名，统一抛出 `KernelProtocolError`；直接构造 ModelSegment 也检查 |
| 同批重复 action ID | 两次调用可以生成同一动作标识，导致持久化身份冲突 | kernel 在返回计划前检查整批 ID 唯一性 |
| 错误工具声明残留或重复回执 | 上下文先过滤调用，再仅比较过滤后长度；全部无效或混合无效声明仍可能保留原始声明；重复 ID 会输出重复结果 | 对声明稳定去重；将有效且有结果的完整声明与原始声明比较，必要时替换/删除；源历史不变 |
| 已撤回来源可经更新复活 | add 检查 source_digest 的撤回状态，update 仅检查文本、父项与代次 | update 在事务内同样检查来源撤回；拒绝后回滚，原文本和向量保留 |
| 负数召回数量不稳定 | 负 LIMIT 与 Python 负切片可能返回部分结果，或触发底层错误 | 向量和全文召回对 k<=0 统一返回空列表 |
| Skill 参数异常逃逸 | list 类型资源被用作字典键，产生未处理 TypeError | read 在访问快照前验证 skill_id 和 resource 为非空字符串；handler 返回标准工具错误 |
| 暂停前已记为未知的副作用恢复时被跳过 | pause_boundary 已把 STARTED 副作用变成 OUTCOME_UNKNOWN；worker 恢复只处理 STARTED/PLANNED/PREPARED，遗漏已存在的未知状态 | 恢复入口遇到未知状态写入 RunBlocked，在调用模型或执行器前停止；使用现有 reconcile_action 核对后，继续正常执行且不重放副作用 |

完整恢复测试实际验证：崩溃租约过期 → 暂停 → 显式继续 → RunBlocked，模型/执行器调用均为零 → 用固定产物显式核对为成功 → 再次继续 → 模型调用一次，执行器仍为零，动作保持成功，事件历史有效。

## 结果与证据

- `eval/result/runtime-context-skill-20261021/baseline.xml`：417 passed、3 failed、1 skipped。三项失败原本期待自动排队，与当前暂停/显式继续策略不符；更新断言后暴露并修复上述未知副作用恢复遗漏。
- `eval/result/runtime-context-skill-20261021/matrix-before.xml`：初始 33 项中 19 failed、14 passed。其中 3 项是测试把 IntEnum 错写为字符串期望，已改为比较枚举名称；另外 16 项复现产品问题。原始失败文件保留。
- `eval/result/runtime-context-skill-20261021/matrix-after.xml`：初始 33 项修复后全部通过。后续增加的 8 项包含在扩大回归中。
- `eval/result/runtime-context-skill-20261021/regression.xml`：**564 passed、0 failed、0 errors、1 skipped**，其中新增矩阵为 41 项。
- `eval/result/runtime-context-skill-20261021/reconciliation.xml`：最后补充的完整 worker 核对恢复测试 **1 passed**；它未包含在前一份 XML 中。
- 两份最终结果合计 **565 个不同测试通过，1 项跳过**。跳过的是需要 `CC_HARNESS_RUN_REAL_LLM=1` 的在线模型探针，不能计为通过。
- Ruff 对本轮修改的五个产品文件和两个测试文件检查通过。

复验（PowerShell，在项目根目录运行）：

```powershell
$matrixTests = @('tests/fault_injection','tests/runtime_rebuild','tests/test_loop_control.py','tests/test_loop_control_integration.py','tests/test_local_information_assets.py','tests/test_agent_trial_regressions.py','tests/test_runtime_context_skill_matrix.py') + @(Get-ChildItem tests/test_context*.py,tests/test_memory*.py | ForEach-Object FullName)
python -m pytest @matrixTests --junitxml=eval/result/runtime-context-skill-20261021/recheck.xml
python -m ruff check cc_harness/run_kernel.py cc_harness/context.py cc_harness/memory/store.py cc_harness/local_skills.py cc_harness/worker.py tests/test_runtime_context_skill_matrix.py tests/fault_injection/test_fi_runtime.py
```

这些测试证明上述确定性契约、故障与边界行为；不代表本轮已验证真实模型任务成功率、外部服务可用性或小时级耐久性，也不代表全仓库所有可选测试均已执行。
