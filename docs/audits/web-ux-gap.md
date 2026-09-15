# WebUI 差距清单（适配执行基线）

日期：2026-09-14  
范围：`D:\agent_learning\cc-harness`；不包含、不修改 `D:\agent_learning\外卖`

## 已确认的用户可见差距

| 区域 | 现状 | 影响 | 本次处理 |
|---|---|---|---|
| 模型回复 | Durable 路径只在完整 segment 提交后更新消息；provider chunk 没有 WebUI 通道 | 长响应期间像“卡住” | 增加进程内有界 `LiveStreamHub`，SSE 接收易失增量 |
| 断线恢复 | Durable 事件轮询可恢复，但临时流没有明确与权威事件的替换关系 | 断线后可能重复或显示半截正文 | SSE 先对账 `run_events`，前端以 `AssistantMessageCommitted` 替换临时节点 |
| 思考展示 | provider 的 `reasoning_content` 属于私有协议字段 | 原文展示会泄露内部思考或敏感数据 | 仅传输 content/tool 阶段和终态状态，不传思考原文 |
| 旧适配器 | 第三方/测试 adapter 仍可能只有两参数 `complete(messages, tools)` | 直接改签名会破坏兼容性 | 运行时反射判断可选 callback，旧实现走原调用 |
| 慢浏览器 | 无界广播会反向阻塞 worker | 运行可靠性下降 | subscriber 队列和历史均有界；满载产生 `stream_gap`，worker 不失败 |
| detached supervisor | WebUI 与 detached supervisor 可能不在同一进程 | 进程内增量无法跨进程到达 | 保留 Durable 轮询作为最终兜底；跨进程 token 流列为后续协议 |
| 会话切换 | 大历史读取期间缺少明确中间态，旧响应可能覆盖新选择 | 用户看到空白页或误发到旧会话 | 增加 `session-loading`、请求代次保护和发送禁用态 |
| 运行时诊断 | 失败原因散落在事件或原始错误中 | 用户只看到“卡住/失败” | 右侧增加脱敏 `diagnosis-card`，按投影、完成证据、未知副作用、租约和环境分类 |
| 普通对话完成 | 统一编码完成门会把无工具的问候误报为 stalled | 纯对话无法自然结束 | 增加持久化 `conversation` 合约；仍要求 `AssistantMessageCommitted` 证据，编码路径不放宽 |

## 不属于本次 P0 的问题

- 文件树、@ 文件引用、完整工具参数/轨迹页没有公开后端 API；本次不渲染假入口。
- 大会话虚拟列表需要独立基准，暂不引入重依赖。
- SQLite 投影、Checkpoint、审批和压缩语义不因 UI 改造而重写。

## 验证口径

差距只有在以下证据同时成立时才算关闭：Python 编译/ruff、hub 和 adapter 测试、`tests/runtime_rebuild`、前端构建、HTTP/SSE 烟测，以及 Playwright 对加载态/会话切换/诊断卡/布局溢出的浏览器检查。真实 provider 验证单独在临时目录执行，需要用户明确授权并使用已有配置；本次已覆盖最小中文对话和 follow-up，未把临时 delta 当作完成证据。
