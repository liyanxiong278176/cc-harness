# 任务执行内核改造验收记录

日期：2026-10-20。

## 实施结果

已按设计访谈的 12 项决定修改当前 Durable Runtime，复用现有模型、工具、上下文、MCP、记忆与安全接线。新增 task_execution、execution_control、kernel_middleware、execution_tools 和 terminal_owner 模块，并接入原 Coordinator、Worker、Supervisor、事件投影、REST/IPC、TUI 与 WebUI。

业务生命周期与调度信息分离，暂停与取消区分，支持显式计划审批、多维预算、父子预算约束、用户指导、过期提案抑制、验证修复、事件等待唤醒和旧任务安全迁移。取消后的关联任务保留原目标、范围与验收约束，不因前序取消而无法派发。旧记录和快照不被改写。

操作和计量说明见 `../task-execution-kernel.md`。没有替换上下文压缩、信息卸载、检索、MCP transport 或长期记忆实现；工作区进入本轮前已有的这些模块修改予以保留。

## 已完成验证

| 检查 | 结果 |
|---|---|
| Runtime 重建、WebUI、共享 Runtime、cc-only Runtime、Loop Control、入口与持久子任务回归 | 344 passed |
| MCP client、记忆卸载、记忆检查点、上下文契约与上下文投影测试 | 106 passed |
| 最后一次 WebUI 继续/审批指导定向复验 | 3 passed，属于前述覆盖的重复验证，不重复累计 |
| 本轮修改模块 Ruff 检查 | 通过 |
| WebUI TypeScript 与 Vite 生产构建 | 通过 |

主回归命令：

```powershell
python -m pytest tests/runtime_rebuild tests/test_webui.py tests/test_runtime.py tests/test_cc_only_runtime.py tests/test_loop_control.py tests/test_loop_control_integration.py tests/test_entrypoint.py tests/test_durable_subagents.py -o addopts= -q --tb=short --show-capture=no
```

能力兼容命令：

```powershell
python -m pytest tests/test_mcp_client.py tests/test_memory_offload.py tests/test_memory_checkpoint.py tests/test_context_contract.py tests/test_context.py -q --tb=short
```

新增控制契约测试覆盖暂停续行、取消后建立可实际执行的关联任务、并发预算预留、父子累计消耗、幂等结算、预算停止、要求更新、在途模型提案作废、无未匹配工具调用、匹配等待、终端身份变化、计划版本审批、扩展冲突、旧任务幂等迁移和活动执行时间边界。

## 已复现的旧 REPL 失败

扩展执行 `tests/test_repl.py` 时，以下 5 项旧前台 REPL 测试的模型调用次数断言失败：

- test_run_repl_sends_user_message_to_llm
- test_run_repl_mode_switch_updates_system_prompt_in_next_turn
- test_run_repl_clear_preserves_system_prompt
- test_run_repl_unknown_slash_falls_through_to_llm
- test_run_repl_empty_input_does_not_call_llm

本轮修改的是 run_durable_repl；对比 AST 后确认旧 run_repl 函数与 HEAD 一致。进一步将 HEAD 版本 repl.py 加载到同一工作区与依赖环境中运行该测试文件，得到同样的 5 项失败和 59 项通过，因此未通过放宽完成验证或修改无关旧 REPL 断言来消除这些失败。这个对照限定为旧 repl.py 的同环境复现，不代表整仓库历史版本的完整基线。

原持久子任务测试要求取消后复活原父子 Run，该断言已按用户确认的新语义更新为保留取消状态并创建关联新任务，并通过复验。

## 尚未执行的联调与限制

- 未使用真实 Provider 或真实 MCP 服务进行本轮端到端任务验收，未据模拟测试宣称真实费用或模型效果。
- 终端进程身份与消失检测有确定性测试；操作系统强制关闭真实终端窗口、机器重启及多终端现场联调尚未执行。
- 无供应商实测的 Token/费用只能按已声明上界保守计量，并显式标记估算；费用预算缺少价格上界时暂停，不能当作零价格继续。
- 未对用户真实任务库执行破坏性中断、迁移演练或清理。现有服务进程应在适当时机重启以加载新代码。

这些边界不影响已列出的本地代码与契约验证结果，但不能据此宣称真实服务联调已经完成。
