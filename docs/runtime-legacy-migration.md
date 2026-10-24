# Legacy ReAct 引擎迁移（product 已完成，eval 侧待清）

> 状态：**产品侧迁移已完成**——仓库只剩一条执行链路（Durable Runtime），
> 产品包对 legacy 引擎的引用为 **0**。`cc_harness/agent.py` 继续存在，但只服务于一个消费者：
> LoCoMo 基准适配器。剩余工作是把它也迁走，然后删除引擎与 165 处评测用测试引用。

## 1. 已经发生什么（本次迁移）

| 动作 | 结果 |
| --- | --- |
| 拆出 `cc_harness/durable_repl.py` | REPL 只剩 durable 客户端交互（submit/attach/approve/follow-up/resume），无模型循环、无工具派发 |
| 删除 `cc_harness/runtime.py` | `SessionRuntime` 及其队列/事件兼容层消失 |
| 删除 `cc_harness/repl.py` | 旧 REPL、slash command、`ReplState`、`_after_turn_*`、`_collect_disk_changes` 一并删除 |
| 删除 `cc_harness/tui/` | 无入口的 Textual 前端桥接 |
| 删除 `cc_harness/project/subagent.py` | legacy `SubAgentRunner`（durable 侧由 `durable_subagents.py` 承担） |
| 删除 `entrypoint._run_print` / `_api_cost_status` / `_write_print_json` | 只被测试使用的旧 print 路径 |
| 删除 `--repl` flag | CLI 不再暗示存在第二条链路 |
| `--mode` 保留但标注为兼容用、durable 忽略 | eval launcher 仍在传该参数 |

可验证的硬约束：

```powershell
python scripts/legacy_engine_inventory.py --check
# legacy engine: product_refs=0 (must be 0)  eval_refs=7  test_refs=165
```

`tests/test_single_runtime.py` 用 AST 扫描产品包：任何 `cc_harness/` 模块 import
`agent` / `repl` / `runtime` / `tui` / `project.subagent` 都会失败；被删模块"复活"也会失败。

测试面变化：legacy 覆盖从 **23 文件 / 353 引用 / 11.5k 行** 降到 **15 文件 / 165 引用 / 6.2k 行**。
删掉的是随模块一起消失的行为测试（旧 REPL、SessionRuntime、legacy subagent、print 模式）；
保留的是 `agent.py` 自身的引擎测试——它现在守护的是 **eval 引擎**，不是产品路径。

### 顺带修掉的两个 durable 缺陷

迁移过程中 `tests/test_interrupt_resume.py` 暴露了两个**先于本次迁移就存在**的问题（在 clean HEAD 上同样失败）：

1. **follow-up 双重启动竞态**：`FollowUpService.enqueue`（coordinator/webui）与
   `release_ready`（supervisor）是两个不同的 service 实例，可能同时启动同一个 queued item。
   `run_exists → create_run` 是 TOCTOU，第二个启动者会撞 `UNIQUE run_record.run_id`；
   即使 insert 没撞，随后两次 `RunCreated` 追加也会触发 `RunCreated can only be the first event`。
   修复：`_start` 内部按 store 取一把进程级 asyncio 锁（`_start_lock`），独占执行 `_start_unlocked`；
   `create_run` 的冲突在"run 已存在"时视为并发启动成功（其余仍抛出）。
2. **测试与实现的语义错位**：`resume(CANCELLED run)` 的实现是**新建 continuation run**
   （携带 `PredecessorHandoffCommitted`），但旧测试断言"复用同一个 run id + 追加 `RunResumed`"。
   已把测试改写为断言真实契约（新 run、`FollowUpQueued.continuation=true`、原 run 保持 `CANCELLED`），
   并在文档里记录这个产品语义：**resume 不重开旧 run，而是开一个后继 run**。

## 2. 为什么 `agent.py` 还在

`eval/locomo/runner.py` 直接用 `run_turn` 驱动 1,986 条 QA 的多轮回放与记忆注入。
已发布的 LoCoMo 数字（F1 0.606、分层记忆省 57.3% token）绑定在这个循环上；
把它改到 durable 会**静默重新基线**，属于需要单独评审的评测变更，不能顺手做。

因此本仓库的边界是：

| 范围 | 引擎 | 约束 |
| --- | --- | --- |
| `cc_harness/`（产品） | Durable Runtime | 不得引用 legacy（guard test + 预算脚本强制） |
| `eval/locomo/` | legacy `run_turn` | 唯一的允许消费者，`eval_refs` 预算 7 |
| `tests/` | 两者 | `test_refs` 预算 165，只降不升 |

## 3. 剩余批次

| 批次 | 内容 | 完成标志 |
| --- | --- | --- |
| A | 迁移 LoCoMo 适配器：QA 轮改走 durable（每 QA 一个 run + 记忆注入走 `build_context`），或抽一个只依赖 kernel/worker 的评测驱动 | `eval/locomo/runner.py` 不再 import `run_turn` |
| B | 用同一份 LoCoMo 数据集重跑，记录新旧数字差异（预期会有差异，必须公开说明） | 新报告 + 迁移说明 |
| C | 删除 `cc_harness/agent.py` 与 15 个文件里的引擎测试 | `--check` 报 `test_refs=0` |
| D | 清理 docs 中 4 段渲染 / 模式 / slash command 的历史描述 | 文档与代码一致 |

A 的设计约束（避免重蹈覆辙）：

- 不要给 durable 加"评测专用旁路"；用公开的 `RunStore + ReActKernel + RunWorker` 组装评测驱动。
- 记忆注入必须走 `capability_runtime.build_context`，否则测的仍是旧路径。
- 每 QA 一个 run 会放大 run 数量（1,986），先确认 SQLite 写入与 projection 重建的成本，再决定是否批量复用 run。

## 4. 删引擎 DoD（剩余）

1. `eval/locomo/runner.py` 与 `eval/locomo/trace.py` 不再引用 `run_turn`（`eval_refs=0`）。
2. `test_refs=0`：15 个文件的引擎测试全部迁移或删除。
3. 删除 `cc_harness/agent.py`；`tests/test_single_runtime.py` 的 eval 白名单与预算一起清空。
4. `pytest tests` + `ruff check` + 预算脚本全绿。

## 5. 随旧引擎一起消失的能力（已确认）

| 能力 | 现状 |
| --- | --- |
| 4 段终端渲染（思考/行动/观察/结果） | 产品不再输出；WebUI 用事件流渲染 |
| slash commands（/plan /design /mode） | 删除；WebUI 有模式/控制面 |
| `plan` / `design` 模式落盘 | durable 无对应实现；`--mode` 仅兼容占位 |
| `<todo_hints>` 注入 | 随引擎删除（无替代） |
| legacy `SubAgentRunner` | 删除；durable 用 `durable_subagents.py` |
| print 模式（`--output-format`） | 删除（只被测试使用） |

## 6. 验收命令

```powershell
python -m pytest tests -q
python scripts/legacy_engine_inventory.py --check
python -m ruff check cc_harness/ tests/ main.py
```
