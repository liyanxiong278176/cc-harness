# WebUI 易失流与 Durable 对账

日期：2026-09-30

本文件记录当前实现。运行状态、检查点、审批、工具结果和完整 assistant 回复仍由 Durable Runtime 持有；浏览器中的逐块文本只是即时展示状态。`AssistantMessageCommitted` 是完整正文的权威来源。

## 数据路径与边界

```text
LLMClient StreamEvent
        |
        v
DurableModelAdapter.complete(stream_callback=...)
        |
        v
RunWorker（run_id / segment / monotonic chunk）
        |
        v
WebRuntimeManager._emit_live_stream
        |
        v
LiveStreamHub（进程内、有界队列） ---> SSE event: stream
                                      |
                                      +--> 浏览器内存 reducer

RunWorker -------------------------> Durable run_events / artifacts
                                      |
                                      +--> SSE event: runtime / timeline
                                           authoritative reconciliation
```

`LiveStreamHub` 是尽力而为的进程内扇出。它不写 SQLite，也不是 Run 的恢复依据。worker 的流回调异常不会改变模型段结果。Durable 事件树会继续低频对账，因此 SSE 同时承载即时 `stream` 和已提交 `runtime` 记录。

临时 delta 包络示例：

```json
{
  "type": "stream_delta",
  "run_id": "run-id",
  "segment": 0,
  "chunk": 12,
  "kind": "content",
  "text": "partial text"
}
```

当前 live `kind` 为 `content`、`reasoning`、`tool_call_delta` 和 `done`。SSE 还可发 `stream_gap`，提示有界队列丢弃了临时片段；错误由 `stream_error` 表达。worker 约每 40ms 或达到 4096 字符时合并正文块；边界、reasoning 和工具增量保持顺序。

`reasoning` 是独立的内存态。前端按 `run_id/segment/chunk` 去重，写入 reasoning 字段，不拼入正文。收到正文时折叠 thinking 区块，并显示本回合已观察到的思考秒数。仅刷新后，live reasoning 不会重放。

### 现有 reasoning artifact 限制

必须区分“WebUI 新增的易失流”和“Runtime 既有模型重放数据”。当前 `cc_harness/llm.py` 仍会将 provider 的 `reasoning_content` 保存在 assistant 消息中，以满足 DeepSeek 工具调用重放；对于没有独立 `content` 的 provider 流，适配器还会以已收集的 reasoning 作为最终正文兜底。这些是现有 Runtime 消息/artifact 语义，不由 `LiveStreamHub` 持久化。本次没有改写该语义，因此不能宣称 reasoning 原文在整个系统中永不落盘。详情见 `docs/audits/webui-redesign-decisions.md`。

## SSE 与重连

1. 新 SSE 订阅先登记到 `LiveStreamHub`，再读取 Durable 事件树，缩小浏览器加载与订阅之间的竞态窗口。
2. `event: stream` 发送临时 `stream_delta`/`stream_gap`；`event: runtime` 发送 Durable 投影和游标。Durable event id 使用根 run 的序号形式，服务端也接受 `run_id:sequence`。
3. 服务端读取 `Last-Event-ID`，用 Durable 序号继续扫描事件树。临时 live id 不是 Durable cursor，LiveStreamHub 的历史缓存不参与断线恢复。
4. 前端以 run/segment/chunk 去重临时片段；收到 `AssistantMessageCommitted` 时丢弃该回合的临时 assistant 节点并展示已提交正文。
5. SSE 错误或 gap 时前端冻结当前临时前缀、显示对账提示并刷新 Durable timeline。reconnect 后正文由持久投影校准；未提交 reasoning 可能丢失，不能自动重发模型请求。

因此 `Last-Event-ID` 保证的是 Durable 事件游标继续推进，不保证临时 token 被补发。用户刷新/重连后不会把临时文本当成事实；live history ring 当前没有被接入 SSE 回放端点。

## 状态机

```mermaid
stateDiagram-v2
  [*] --> streaming: first live delta
  streaming --> committed: AssistantMessageCommitted
  streaming --> stopped: stop accepted / RunCancelled
  streaming --> failed: stream_error / RunFailed / RunStalled
  streaming --> gap: stream_gap freezes prefix
  gap --> committed: authoritative commit reconciles
  gap --> stopped: RunCancelled
  gap --> failed: RunFailed / RunStalled
  committed --> [*]
  stopped --> [*]
  failed --> [*]
```

`stream_gap` 本身不等于 Run 失败：`gap` 是已冻结临时前缀、等待权威对账的 UI 状态。Run 是否继续运行以 Durable 状态为准。停止操作使用既有 stop API，并保留已显示前缀；需要从检查点继续时走既有 resume API。

## 前端 reducer 约束

- reasoning/content 独立累加；assistant commit 清除临时输出。
- `segment` 改变时不把上一轮工具调用前后的文本拼成一个正文。
- 重复 chunk 丢弃；有缺口时冻结当前前缀，不猜补字符。
- 触发 gap/连接错误不会取消后台 Run，也不会改变恢复、审批或持久化状态机。
- markdown 渲染沿用现有 `marked` + DOMPurify 路径。
