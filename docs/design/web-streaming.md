# Durable WebUI 流式设计

## 数据路径

```text
LLMClient.chat() StreamEvent
        |
        v
DurableModelAdapter.complete(stream_callback=...)
        |
        v
RunWorker (run_id + segment + monotonic chunk)
        |
        v
LiveStreamHub  -- ephemeral -->  /api/sessions/{run_id}/events (SSE)
        |
        +--> Durable run_events / Checkpoint (authoritative, unchanged)
```

`StreamEvent` 的 content 和工具阶段只做展示；完整 assistant/tool 消息仍由 worker 按既有事件顺序提交。事件提交成功后，SSE 的低频 Durable 对账会覆盖任何临时增量。

## 消息契约

```json
{
  "type": "stream_delta",
  "run_id": "run-id",
  "segment": 0,
  "chunk": 12,
  "kind": "content",
  "text": "partial text",
  "live_id": 42
}
```

允许的 `kind` 为 `content`、`tool_call_delta`、`done`；工具阶段只携带名称/索引，终态可携带结束原因和非敏感 token 用量。禁止 `reasoning_content`、原始工具参数和 provider 凭据。

### 丢包和重连

订阅队列固定上限。满载时淘汰最旧临时 delta 并插入 `stream_gap`。浏览器收到 gap 后保留“实时内容可能不完整”的状态，但继续等待 Durable 事件；重连以 `Last-Event-ID` 读取持久事件，不尝试从 hub 恢复事实。

### 前端替换规则

1. `stream` 事件按 `run_id/segment/chunk` 去重并追加到临时 assistant 节点。
2. `AssistantMessageCommitted` 到达时删除临时节点，渲染持久正文。
3. `RunStalled`、`RunFailed`、`RunCancelled` 到达时冻结临时节点并标注状态。
4. 自动滚动只在用户位于底部时进行；流式渲染以批次更新，避免每个 token 触发全量布局。

## 故障边界

- hub 仅与同一 WebUI 进程共享；detached supervisor 仍通过 Durable 轮询工作。
- callback、SSE 和前端临时状态都不是恢复依据。
- 任何广播异常都被隔离，不改变 worker 的 Durable 结果。
