# 模型之外的执行可靠性排查

范围：WebUI 控制入口、后续任务归属、Worker 调度、工具调用与恢复、命令进程、Windows 遥测、多项目资源隔离、启动失败与清理。模型服务欠费、服务端故障、协议或任务能力问题不纳入本次修复结论。

所有下列缺陷均先在实际控制或执行入口构造失败用例，再修复并回归。新增用例集中在 `tests/test_execution_reliability.py`；既有运行时、WebUI、工具、沙箱、进程测试共同验证。架构边界沿用 ADR 0115–0117：暂停保留原 Run，取消不复活；真正未确认的副作用不能因为点击继续而自动重放。

| 故障触发 | 修复后的行为 | 验证入口 |
| --- | --- | --- |
| 初始任务终结，后续任务执行时点击暂停、调整预算、唤醒或批准计划 | 四个接口操作页面当前展示的任务，保留会话根 ID 的对话归属 | 实际 HTTP 路由、FollowUpService、投影与计划审批 |
| 服务重启后唤醒等待任务或批准计划 | 启动／重新连接调度器，再提交控制决定并唤醒 | HTTP 路由及启动调用检查 |
| 调度器对象仍在，但调度循环已退出 | 页面重新连接时检查循环存活状态，并重新启动待执行工作 | `_start_pending_if_configured` |
| 单个 Worker 工厂构造失败 | 仅暂停该任务并持久化 `worker_initialization_failed`；同批正常任务继续调度 | 两个独立任务的实际调度 tick |
| 工具签名绑定、凭据解析或命令准备在调用前失败 | 形成已知失败及观察结果，不制造需要人工核对的未知副作用 | 实际 `_execute_tool` 分发 |
| 可幂等只读工具报错、超时或暂停中被取消 | 可继续读取并恢复原任务；非幂等内部控制工具和未知写入保持核对边界 | 实际 Worker 执行、暂停和超时 |
| 执行器捕获了取消异常 | 取消信号重新传递给 Worker；由 Worker 完成恢复边界 | 实际工具分发与 Worker |
| 命令前的工作区快照报错 | 锁始终释放，第二次命令不会永远等待 | 连续两次带超时的实际分发 |
| 一个进程中打开多个项目 | 每个 DurableRuntimeClient 独立持有执行器、降级配置和降级锁；关闭一个不会销毁另一个 | 两个客户端并发工具调用、关闭其中一个后再次调用 |
| 其他项目还在使用共享的自管沙箱服务 | 单项目清理不关闭共享服务；保留单会话 CLI 的兼容入口 | 执行器归属和原有工具／沙箱回归 |
| Windows 多线程采集进程 CPU／I/O | 每次调用使用独立 WinDLL 函数签名，消除不同线程结构体类型竞争 | 8 线程、250 次真实 Windows 指标采集 |
| 本机命令执行途中取消或遥测异常 | 清理对应进程树及管道，避免后台残留命令与下一轮并发 | 真实长命令进程、取消及故障注入 |
| 构建／测试超过固定 30 秒 | 工具新增有上界的 `timeout_seconds`，本机及沙箱执行器均遵守；外层 watchdog 留出 15 秒清理余量 | 真实超时覆盖与 Worker watchdog 检查 |
| 沙箱分发后断线且禁止本机降级 | 如实标记结果未知；仅分发前的确定失败声明未执行 | 原有沙箱／工具及网络重试回归 |
| 启动到一半本地组件失败 | 清理已创建资源，保留事件存储，允许下次启动重试 | public `start_supervisor` 故障注入 |
| MCP 等单个组件关闭时报错 | 继续清理其他组件，并清除当前客户端的资源引用 | 多组件清理故障注入 |

## 当前真实会话的恢复证据

会话根为 `35729b69-545c-4e3b-b1b9-14658dd2048f`，执行任务为 `e3e6f001-3baa-45cb-8997-c3ac718576c2`。原阻塞动作 `call_00_W3FbbsScPTnidqRQ349X4681` 是本地语法检查和离线 Maven 测试；命令在 30 秒后超时。

`scripts/reconcile_local_test_timeout.py` 在不重放命令的情况下验证：命令严格匹配捕获的本地验证范围，测试使用 H2 内存数据库，测试报告属于该动作的时间窗口，不存在匹配的残留测试进程。报告显示 7 项测试、5 项失败、0 项错误。因此对账为 **失败**，保留测试失败供原模型继续修复，未将超时伪装成成功。

对账证据：`sha256:5d122e27970f4a1c50e1948d0f5491baf84af1dbb3ca96ef4495a42e7a7f8391`。原命令、观察、报告、测试源码和配置的摘要均保存在审计工件中。

通过实际 WebUI 点击继续后，调度器重新领取同一任务并产生新的执行记录；没有向消息接口发送“继续”。随后通过根会话的暂停接口，确认收到的是当前子任务 ID，任务进入 `paused`，未知动作数为 0；用于安全更新服务。

浏览器证据脚本：`scripts/verify_existing_resume.mjs` 和 `scripts/verify_execution_reliability.mjs`。截图与结构化结果位于 `docs/design/screenshots/2026-10-24/`，其中 `17-runtime-reliability-recovered.png` 为恢复运行，`18-runtime-task-list.png` 为任务列表。

## 验证与适用边界

最终回归：369 项通过，2 项平台限制跳过；新增故障注入覆盖 23 个参数化场景。测试结果保存在同目录的验证记录中。广泛覆盖的测试入口为：

```powershell
python -m pytest tests/runtime_rebuild tests/test_webui.py tests/test_web_redesign.py tests/test_web_sse.py tests/test_web_facade.py tests/test_execution_reliability.py tests/test_native_tools.py tests/test_executor.py tests/test_tools.py tests/test_sandbox.py tests/test_network_retry.py -o addopts='' -q --tb=short
```

另执行前端 13 项状态测试、Ruff 和差异空白检查。Windows 上跳过目录符号链接权限用例及 POSIX 进程组用例，不能将其算作已通过。

本次结论针对已复现的故障与上述检查范围。真正结果未知的外部命令或写入、需要人工审批的动作、耗尽的显式预算仍按其事实处理；模型服务和业务代码测试失败保持原有归因。本次并不证明任意环境、任意外部工具组合永远不会失败。
