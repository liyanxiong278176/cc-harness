# 运行截图

[返回项目 README](../README.md)

本组截图于 2026-10-12 重新构建并运行当前工作区版本后拍摄，未复用已有验收截图。
使用 `npm run build` 构建 WebUI，启动当前 Python WebUI 后端，在 Chromium 中通过
Playwright 实际点击、输入任务、处理审批并截图。模型为本机配置的 `deepseek-flash`；
没有模拟 API 响应或注入运行成功状态。运行数据使用独立目录，演示文件保存在本机
`.playwright-mcp/` 中。桌面截图为 1600 × 1000，窄屏截图为 390 × 844。

## 截图索引

| 页面或操作 | 截图 | 内容 |
| --- | --- | --- |
| 深色首页 | [01-welcome-dark.png](01-welcome-dark.png) | 未选择项目时的欢迎页与输入门禁 |
| 浅色首页 | [02-welcome-light.png](02-welcome-light.png) | 实际切换浅色主题 |
| 项目选择 | [03-project-ready.png](03-project-ready.png) | 手动输入本机路径后，项目就绪与任务建议 |
| 工具运行中 | [04-task-running.png](04-task-running.png) | 模型实际发起 Glob 调用，显示执行参数与运行状态 |
| 运行详情 | [05-runtime-panel.png](05-runtime-panel.png) | 状态、执行后端、调度器、上下文和事件时间线 |
| 模型设置 | [06-model-settings.png](06-model-settings.png) | Base URL、模型名称与已遮蔽的 API Key |
| 斜杠命令 | [07-slash-commands.png](07-slash-commands.png) | 在输入框键入 `/` 打开命令菜单 |
| 快捷键 | [08-keyboard-shortcuts.png](08-keyboard-shortcuts.png) | 实际按 Ctrl + / 打开帮助 |
| 权限策略 | [09-permission-options.png](09-permission-options.png) | 三档权限菜单与当前选项 |
| 上下文用量 | [10-context-usage.png](10-context-usage.png) | 实际运行产生的 token 用量详情 |
| 消息排队 | [11-message-queue.png](11-message-queue.png) | 工具执行期间提交后续消息，展示队列 |
| 停止运行 | [12-stopped-run.png](12-stopped-run.png) | 实际点击停止后的部分输出与恢复入口 |
| Write 参数 | [13-write-approval.png](13-write-approval.png) | Write 创建演示 Markdown 前的待审批状态 |
| 产物预览 | [14-artifact-preview.png](14-artifact-preview.png) | 允许 Write 后创建真实文件，并打开 Markdown 预览 |
| 产物源码 | [15-artifact-source.png](15-artifact-source.png) | 同一文件切换至代码视图 |
| 产物全屏 | [16-artifact-fullscreen.png](16-artifact-fullscreen.png) | 实际打开全屏预览 |
| 模型回复 | [17-model-answer.png](17-model-answer.png) | 写入后的真实回复与会话状态 |
| 执行计划 | [18-execution-plan.png](18-execution-plan.png) | 目标契约、PlanGraph 和 Todo 浮层 |
| 会话搜索 | [19-session-search.png](19-session-search.png) | 按关键词筛选本次创建的会话 |
| 窄屏对话 | [20-mobile-chat.png](20-mobile-chat.png) | 手动收起侧栏后的窄屏工具执行页面 |
| 窄屏设置 | [21-mobile-settings.png](21-mobile-settings.png) | 窄屏下的模型设置弹窗 |
| 审批卡片 | [22-approval-pending.png](22-approval-pending.png) | 真实 Write 请求的“允许一次”与“拒绝”按钮 |
| 审批拒绝 | [23-approval-rejected.png](23-approval-rejected.png) | 实际拒绝 Write，工具标记未执行，目标文件未创建 |
| Read 后回复 | [24-read-answer.png](24-read-answer.png) | 仅读取 README 前 30 行后的真实模型回复 |
| Read 结果 | [25-read-tool-result.png](25-read-tool-result.png) | 展开成功的 Read 参数和 30/60 行结果 |
| 流式回复 | [26-streaming-response.png](26-streaming-response.png) | 真实模型正在逐步输出内容时截图 |

## 本次运行的实际情况

- OpenSandbox 不可用，Runtime 显示“沙箱不可用 · 已降级本机”。本组截图不证明沙箱执行成功。
- Write 获批后确实创建了 180 字节的演示文件；Read 也实际成功读取了 README 的前 30 行。
  两个会话随后显示“已暂停”，因此工具成功和整体 Run 完成应分别判断。
- Glob 对当前大目录的调用耗时较长。运行中截图仅记录调用与排队状态，不将其列为成功的目录检索证据。
- 拒绝审批后，`.playwright-mcp/readme-approval.md` 未创建；拒绝截图展示真实工具观察和模型后续回复。
- 窄屏对话截图先手动收起侧栏；未收起时侧栏会明显挤占对话区域。
- 本组覆盖主要可见页面及操作流程，不作为所有异常恢复、长历史、网络断线或安全边界均通过的测试报告。

此目录中的其他图片为之前已有的素材，不属于本次重新运行的截图组。
