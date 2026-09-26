# ADR-0112：流式输出重试边界与结构化错误

- 状态：Accepted
- 日期：2026-09-17
- 范围：LLM provider stream、LiveStreamHub、WebUI SSE

## 背景

模型输出通过 provider chunk、Runtime callback 和浏览器 SSE 三层传递。任一层都可能在回复中途断开、慢客户端造成丢包，或在状态投影暂时不可读时失败。若把同一请求无条件重发，可能出现重复文本、重复工具调用或用户误以为任务已经停止。

## 决策

1. 以首个可见 `content` 或 `tool_call_delta` 作为自动重试边界。边界前仅对连接/限流/5xx 类瞬时错误做初始请求加两次退避重试；边界后不自动重试。
2. 边界后的部分输出通过 `stream_error` 冻结，保留在浏览器作为未提交内容；完整正文仍只能由 `AssistantMessageCommitted` 替换。残缺工具参数不进入动作队列。
3. provider、Runtime、REST 和 SSE 统一使用 `code`、`phase`、`retryable`、`partial_output`、`attempt`、`retry_after`、`next_action`、中文 `message` 字段，可选安全的 `request_id`。浏览器不接收原始响应头、凭据、工具参数、堆栈或隐藏思考。
4. 服务端以约 40ms 或 4KB 合并相邻纯文本；工具、错误和 `done` 是硬边界。队列满载只丢弃临时 delta 并发出 `stream_gap`。
5. 浏览器 SSE 断线是非阻塞状态：EventSource 自动重连，前端保留输入框和临时文本，按 Durable 游标重新对账；重复失败只显示可能滞后，不停止后台 Run。

## 结果与权衡

- 降低 token 级渲染开销和网络抖动对观感的影响。
- 防止跨首个 delta 的 provider 重放造成重复副作用，但用户需要显式选择安全的“重试本轮”或“继续”。
- LiveStreamHub 仍是进程内展示缓存；恢复、审计和最终答案继续由不可变事件流/检查点负责。
- 结构化字段让前端能够区分连接、模型、持久化和 SSE 故障，同时牺牲了浏览器直接查看完整异常堆栈的便利性；完整诊断留在本地日志和审计工具。

## 验证

- `tests/test_llm.py` 验证首个 delta 前最多三次请求、首个 delta 后只请求一次并返回 `partial_output`。
- `tests/test_live_stream.py` 验证服务端合并后的终端顺序与结构化流错误。
- WebUI 构建验证 TypeScript 对结构化 SSE/REST 错误的解析和非阻塞重连状态。
