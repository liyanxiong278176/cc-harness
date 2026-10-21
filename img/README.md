# 当前项目执行截图

[返回项目 README](../README.md)

拍摄日期：2026-10-21（Asia/Singapore）。WebUI 使用 `npm run build` 重新构建，后端通过
`python main.py --no-open --port 3080 --data-root .playwright-mcp/readme-runtime-20261021` 启动。
执行后端为 native，模型使用本机已有配置 `deepseek-flash`。
截图使用 Playwright/Chromium，尺寸 1600 × 1000，保存浏览器实际画面，没有修改 DOM、
模拟 API 返回或注入成功状态。通过真实项目选择 API 选择当前仓库，任务输入、发送、审批、
状态面板及本地资产操作在浏览器中完成。

## 原图访问路径

| 顺序 | 原图 | 实际状态 |
| --- | --- | --- |
| 01 | [启动 WebUI](runtime-20261021/01-welcome.png) | 项目尚未选择，显示欢迎页 |
| 02 | [输入测试任务](runtime-20261021/02-task-ready.png) | 当前项目为 cc-harness，任务请求运行矩阵测试并写报告 |
| 03 | [命令审批](runtime-20261021/03-command-approval.png) | 模型规划真实 run_command，等待“允许一次” |
| 04 | [命令执行中](runtime-20261021/04-command-running.png) | 实际点击允许，工具进入执行阶段 |
| 05 | [未知结果阻塞](runtime-20261021/05-command-blocked.png) | executor_lost，运行阻塞，未生成任务要求的 Markdown |
| 06 | [只读报告任务](runtime-20261021/06-report-reading.png) | 新建任务，请求读取测试与修复报告并概述结果 |
| 07 | [报告摘要与暂停](runtime-20261021/07-report-result.png) | 已生成中文概述；最终暂停，不能视为 Run 完成 |
| 08 | [上下文来源](runtime-20261021/08-context-sources.png) | 查看真实来源清单及续行资料 |
| 09 | [Skill 管理](runtime-20261021/09-local-skills.png) | 当前项目的真实 Skill 列表；不表示本任务调用过 Skill |
| 10 | [Runtime 状态](runtime-20261021/10-runtime-state.png) | 查看暂停原因、上下文用量和事件时间线 |

## 运行记录与限制

- 命令任务：`d3007364-d34b-464a-ab0e-3ef2a01106ae`，最终 blocked，事件序号 36。工具回执为 executor_lost，因此没有将这次请求记作 pytest 通过。单独诊断发现当前大工作区的快照扫描遇到无法访问的 Windows 缓存文件（WinError 1920）；该问题在此次拍摄中保留为已知问题，未修复。
- 只读任务：`4d7930d0-499a-4f1b-9963-ee8139d8f1a6`，最终 paused，事件序号 150。实际调用 Read、read_ref 并返回中文摘要；完成门槛仍要求验证，模型遵守“不执行命令”的限制，反复报告同一缺口后触发 no verifiable progress 暂停。该任务不算 completed。
- 页面显示记忆已启用、安全部分降级。启动日志记录既有记忆库的向量维数与本次配置不一致；本组图片不能证明跨会话记忆召回或安全能力全部正常。
- 截图中的“565 通过、1 跳过”来自模型读取的此前审计报告，不能替代这次浏览器任务的执行回执。原测试证据范围见 [测试与修复报告](../docs/audits/runtime-context-memory-skill-matrix-20261021.md)。
- 原始运行数据保存在本机 `.playwright-mcp/readme-runtime-20261021/`，不提交聊天数据库、API 密钥或工具缓存。旧版 img 图片已移除，本目录链接均指向本次新拍摄的 PNG。
