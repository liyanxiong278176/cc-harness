# WebUI 适配决策记录

日期：2026-09-14

## 决策顺序

当任务书与当前实现冲突时，按以下顺序取舍：

1. Durable Runtime 契约和不可变事件流
2. 安全、隐私和权限边界
3. 中断恢复与可审计性
4. 实时交互反馈
5. 视觉细节和改动面

## 关键决策

### 流式数据不进入事实源

`LiveStreamHub` 是 WebUI 进程内的易失广播层，只有有界历史和订阅队列，不写 SQLite、不创建新的 `run_events` 类型。浏览器断线或队列丢包时，重新读取 Durable timeline；临时 delta 从不被当作已提交消息。

### 不展示原始 reasoning

DeepSeek 等 provider 的 `reasoning_content` 既是协议恢复字段，也可能包含私有思考。浏览器只得到 `content`、工具调用阶段标识和终态用量/结束原因，前端以“正在思考/工具调用中”等状态呈现。

### callback 必须向后兼容

`ModelAdapter` 和 `AgentKernel` 的 callback 为可选关键字参数。`ReActKernel` 和 `RunWorker` 只在签名声明支持时传入，旧的两参数实现无需修改即可继续执行。

### 对账优先于实时感

`/api/sessions/{run_id}/events` 每轮先发送 Durable 事件，再等待易失增量；`AssistantMessageCommitted` 到达后，前端删除临时 streaming 节点并显示权威正文。停止、失败或取消会冻结临时内容并显示真实运行状态。

### 广播失败不得改变任务结果

worker 对 emitter 做 best-effort 保护，订阅者慢或浏览器断开只会丢失临时展示，不会把成功的模型调用变成失败。跨进程 detached 模式暂不承诺 token 级实时流，仍依赖 Durable 对账。

### 会话加载必须有明确的中间态

切换大型历史会话时，先切换选中项和标题，主区显示“正在载入会话”，并暂时禁用 composer。只有同一代请求同时完成 timeline、状态和会话列表读取后，才解除禁用；过期响应不能清空新会话。

### 对话与编码使用不同的完成合约

短且没有代码/工具意图的 WebUI 输入可由保守分类器标记为
`interaction_mode=conversation`。该模式仍要求 Runtime 落盘一个非空
`AssistantMessageCommitted`，再生成 `ASSISTANT_RESPONSE` 证据并通过同一完成门。
编码模式继续要求工具/验证证据。这样可以修复“你好”被误报 stalled 的体验，
又不会让模型的口头声明绕过工程任务验收。

### 诊断信息在运行面板集中呈现

聊天区只展示简短中文状态和安全的工具卡片；投影不一致、未知副作用、环境降级、
租约冲突和缺少完成证据统一在右侧 Runtime 诊断卡给出原因与下一步。traceback、
原始 completion JSON、工具参数和凭据继续留在本机审计存储，不发送到浏览器。

## 延期项

文件树、@ 芯片、完整队列/轨迹页、虚拟列表和跨进程流式总线都需要新的公开 API 或独立基准，本次明确不制造假数据和不可操作入口。
