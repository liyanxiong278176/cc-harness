# Durable Agent Loop 验收报告

日期：2026-08-18

结论：核心实现验收通过；当前配置下的真实 Provider、MCP 和 Durable Supervisor 链路通过。真实业务项目登录注册服务与一次性 live cutover 不在本仓库中，因此不宣称已完成这两项生产验收。

## 已通过

- Provider 配置可加载；真实 OpenAI-compatible 流式请求完成，模型返回 `ACCEPTANCE_OK`，`finish_reason=stop`。
- 当前 `mcp.json` 配置的 8 个 MCP server 全部连接成功，共发现 88 个工具。
- 真实 MCP `filesystem.read_file` 只读调用成功，返回 `source=mcp:filesystem`，没有执行写工具。
- 隔离临时工作区 Durable E2E 完成：真实模型调用 1 次 `Read`，产生 `ModelInvocationStarted`、`AssistantMessageCommitted`、`ToolObservationCommitted` 和 `CompletionAccepted`，最终状态为 `completed`；没有 Edit、Write 或 `run_command` 动作。
- 登录注册 provider-neutral 夹具通过：`register` 后 `login` 成功。
- `tests/runtime_rebuild`、安全/MCP/credential 相关回归、全仓 pytest（排除既有攻击生成脚本）和 Ruff 均通过。

## 未宣称通过

- 本仓库没有连接真实业务用户数据库、HTTP 登录注册服务或生产账号系统；因此登录注册只完成夹具验收，不是生产业务 E2E。
- 没有执行一次性 live cutover；旧 Runtime、备份和回滚路径已通过演练，正式切换仍需明确的运维窗口和操作者确认。
- Windows symlink、POSIX PTY、Docker 沙箱 conformance 和 opensandbox CLI 测试因当前环境条件跳过。

本次真实验证未输出或持久化 Provider 密钥，也未修改当前项目业务文件。
