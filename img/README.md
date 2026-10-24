# 当前项目执行截图

[返回项目 README](../README.md)

拍摄日期：2026-10-24（Asia/Singapore）。WebUI 使用当前分支重新构建并在
`http://127.0.0.1:3080/` 启动；执行后端为 native，模型使用本机已有配置
`deepseek-flash`。截图使用 Playwright/Chromium，尺寸 1600 × 1000，保存浏览器
实际画面，没有修改 DOM、模拟 API 返回或注入成功状态。

## 原图访问路径

| 顺序 | 原图 | 实际状态 |
| --- | --- | --- |
| 01 | [启动 WebUI](runtime-20261024/01-welcome-dark.png) | 当前项目为 cc-harness，显示新会话 Hero 与输入卡 |
| 02 | [模型执行中](runtime-20261024/02-running-task.png) | 真实模型调用中，任务列表显示「1 进行中」 |
| 03 | [回答完成](runtime-20261024/03-completed-task.png) | 任务完成，任务列表显示「1 已完成」 |
| 04 | [事件轨迹](runtime-20261024/04-trace.png) | 同一会话的 durable 事件时间线 |
| 05 | [任务列表详情](runtime-20261024/05-tasks.png) | 任务弹窗与状态摘要 |
| 06 | [设置与权限](runtime-20261024/06-settings.png) | 当前项目、模型与权限配置 |
| 07 | [浅色主题](runtime-20261024/07-light.png) | 浅色主题下的完成态会话 |

## 运行记录与限制

- 完成态会话：`6b09a928-21da-482f-a97a-2072ecb2275d`，用户输入「请用一句话介绍你自己」，
  浏览器显示「已完成」，任务列表与 Runtime `task_projection.todos` 一致为「已完成」。
- 执行中截图来自一次只读任务（读取 README 与 WebUI 文档并概括架构）；任务列表状态
  `待执行 → 进行中` 由真实 `TodoCreated/TodoUpdated` 事件驱动。该会话随后因完成门槛
  暂停，不把暂停态截图混入完成态。
- 任务列表只展示当前 effective/current Run 的 task-control todo，不聚合历史会话树。
- 截图不包含 API Key 明文；设置页只显示脱敏后的 key。
- 旧版 `runtime-20261021` 截图册已删除，历史图片仍可从 git 历史找回。
