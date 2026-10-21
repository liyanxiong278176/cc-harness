# 任务执行内核：控制与兼容说明

本次改造基于现有 Durable Runtime，未另建执行引擎。设计依据见 `specs/2026-10-20-task-execution-kernel-spec.md`。

## 权威状态

Run 投影的 `task.lifecycle` 表达业务生命周期，`task.scheduling` 表达排队或租约状态。原 `status` 继续作为历史事件与现有客户端的兼容字段；新客户端应结合 `task` 读取等待原因、预算和继续点，不能把 queued 当作业务阶段。

TaskControl 从同一事件流重建，快照仍是可丢弃的加速器。新增控制事件与旧事件共存，旧取消后恢复历史仍可重放；启用新协议的任务不能再取消后恢复原 Run。

## 使用

- 默认直接执行；明确提出“先出方案”或在提交时指定 `plan_mode=true`，进入只读探索与计划审批。
- `task_plan` 持久化阶段、依赖和验收要求；`task_wait` 在工具观察提交后进入等待，不在持有工具执行权时等待用户。
- 执行中普通消息指导当前任务；“进度”“做到哪了”等进度查询直接报告投影，不改目标。独立后续任务通过显式 follow-up 接口提交。
- WebUI 提供暂停按钮、`/pause`、`/resume` 和 `/approve-plan`。取消后继续创建关联任务，保留原验收和范围。
- TUI 的 `/pause`、`/interrupt`、Ctrl+C 和正常退出走暂停；`/cancel` 走取消，`/follow-up` 创建独立后续任务。
- 关闭页面不触发暂停。终端归属绑定 PID 与进程创建身份，防止 PID 复用误判；同一任务最后显式连接的终端接管归属，不暂停其他终端的无关任务。

## REST 与 IPC

以下 REST 路由也提供 `/api/web/v1` 别名：

| 操作 | 接口 |
|---|---|
| 暂停 | `POST /api/sessions/{run_id}/pause` |
| 继续 | `POST /api/sessions/{run_id}/resume` |
| 修改预算 | `PUT /api/sessions/{run_id}/budget`，正文 `{"limits":{"tokens":100000}}` |
| 批准计划 | `POST /api/sessions/{run_id}/plan/approve`，正文携带当前 `plan_digest` |
| 外部事件唤醒 | `POST /api/sessions/{run_id}/wake`，正文携带匹配的 `wait_id` |

新任务消息正文可携带 `budget_limits` 和 `plan_mode`。IPC 支持 `pause`、`guidance`、`budget`、`approve-plan`；显式 `follow-up` 保留独立任务语义。

## 预算口径

维度包括 steps、tokens、cost、active_seconds、tool_calls、model_calls、risk_actions、concurrent_children；未配置维度不限制。active_seconds 计入模型和工具执行耗时，等待输入或审批不计入。并发不是累计消耗，通过任务树调度检查。

父子任务共享祖先预算约束，先预留再派发，结算重试幂等。独立 follow-up 仅在会话树中关联，不继承前序任务的停止状态。预算不足时不持久化未能预留的模型工具提案，避免留下无对应结果的 tool call。

Token 预算使用输入消息、工具 schema 的保守上界和输出上限预留，向实际调用传递输出上限。`CC_HARNESS_BUDGET_MAX_OUTPUT_TOKENS` 默认 8192，仅用于启用了 Token 或费用预算的调用，未配置预算时不改变原输出策略。

费用预算要求用户配置每百万输入/输出 Token 的价格上界：`CC_HARNESS_BUDGET_INPUT_COST_PER_MILLION`、`CC_HARNESS_BUDGET_OUTPUT_COST_PER_MILLION`。单位须与任务费用额度一致；未配置价格时暂停并说明，不能把未知价格当零费用。供应商提供 usage/cost 时记录实测，否则按预留上界结算并标记 `task.metering` 为 conservative_estimate；预算计量不能冒充供应商账单。

异常中断留下的预留在无活动 Worker 后保守结算，不清零；耗尽的额度须增加后才可继续。副作用是否发生由动作恢复单独核对，预算结算不构成副作用已执行或未执行的证明。

## 验证、委派与扩展

Prepare、Model、Act、Observe、Verify 使用现有 Context Builder、Action Plane 和完成门禁。Middleware 按顺序返回结构化决定；越过声明写入范围、冲突 Patch 和修改受保护状态会被拒绝。

用户更新持久为有版本的指导事实，目标与应用确认在同一事件提交；旧目标验证不能直接证明新目标完成。在途旧模型提案会被丢弃，未启动工具提案写为明确未执行，已发生结果如实保留。受影响子任务接收同一更新，无明确受影响集合时保守更新全部活动子任务；调用方可提供精确集合保留无关任务运行。

计划审批绑定摘要；要求变化会使旧动作审批失效。取消后续任务与受委派子任务采用不同的预算和停止传播语义。Handoff 需要暂停安全点、可验证继续点和已注册接收者 Worker 工厂；重启后接收者未注册时保持暂停，不退回默认执行者偷偷推进。

## 旧任务与验证边界

旧任务仅在无活动租约的安全点迁移，提交运行契约迁移事件，保留任务 ID、目标、历史与证据；迁移后等待继续。当前代码不会对真实用户任务自动执行迁移演练或破坏性故障注入。

自动化验证覆盖确定性事件、恢复、预算、指导、兼容接口及能力连续性。真实 Provider 费用、真实 MCP 服务和操作系统强制关闭窗口的完整联调不能由固定模型测试或构建通过替代，应单独记录联调证据。
