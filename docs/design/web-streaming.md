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

`StreamEvent` 的 content 和工具阶段只做展示；完整 assistant/tool 消息仍由 worker 按既有事件顺序提交。事件提交成功后，SSE 的低频 Durable 对账会覆盖任何临时增量。首个可见增量前允许有限重试，跨过首个增量后只冻结部分输出，不会再把同一请求自动发给模型。

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

允许的 `kind` 为 `content`、`tool_call_delta`、`done`；工具阶段只携带名称/索引，终态可携带结束原因和非敏感 token 用量。禁止 `reasoning_content`、原始工具参数和 provider 凭据。发生模型流错误时，SSE 的 `stream` 事件使用同一 envelope 但 `type` 为 `stream_error`：

```json
{
  "type": "stream_error",
  "run_id": "run-id",
  "segment": 0,
  "chunk": 13,
  "kind": "error",
  "error": {
    "code": "provider_transport_error",
    "phase": "connection",
    "retryable": false,
    "partial_output": true,
    "attempt": 1,
    "retry_after": null,
    "next_action": "连接恢复后继续；不要自动重发已产生输出的本轮",
    "message": "模型连接在回复过程中中断，已冻结未提交内容"
  }
}
```

REST 错误和独立的 `event: stream_error` 也复用 `error` 字段。`phase` 取 `connection`、`model`、`tool`、`approval`、`persistence` 或 `sse`；`request_id` 仅在提供方返回安全的标量标识时出现。

### 重试边界

- **首个 delta 之前**：408/409/429/5xx、连接重置和读取超时最多自动尝试 2 次退避重试（总计 3 次）；最终错误会把实际 `attempt` 暴露给前端。
- **首个 content/tool delta 之后**：立即停止自动重试，保留已见文本，发送 `stream_error`，等待 Durable 对账；“重试本轮”只能由用户在确认没有副作用时显式触发。
- **done 之前的工具参数**：参数仍是临时数据，残缺 JSON 不执行；工具调用只有在完整 assistant 消息落盘后才进入动作队列。
- **认证、参数、余额、配额和协议错误**：不自动重试，返回中文修复动作并允许从最近检查点继续。

### 丢包和重连

订阅队列固定上限。满载时淘汰最旧临时 delta 并插入 `stream_gap`。浏览器收到 gap 后冻结临时内容并显示“正在从 Runtime 对账”，但不停止 Run；重连以 `Last-Event-ID` 读取持久事件，不尝试从 hub 恢复事实。投影读取失败时 SSE 发出结构化 `event: stream_error`，采用有上限的退避并继续保持连接；重复断线只提示“数据可能滞后”，不改变后台 Run。

### 前端替换规则

1. `stream` 事件按 `run_id/segment/chunk` 去重并追加到临时 assistant 节点；`stream_error` 保存安全错误字段并冻结该节点，SSE 阶段错误只更新连接提示。
2. `AssistantMessageCommitted` 到达时删除临时节点，渲染持久正文。
3. `RunStalled`、`RunFailed`、`RunCancelled` 到达时冻结临时节点并标注状态。
4. 自动滚动只在用户位于底部时进行；前端以帧批次更新，服务端以 40ms/4KB 合并纯文本，避免每个 token 触发全量布局。
5. SSE `onerror` 只显示重连状态并触发 Durable 刷新；`onopen` 清除提示并重新对账。输入框、已生成文本和 Runtime 状态在此期间保持可用。

## 故障边界

- hub 仅与同一 WebUI 进程共享；detached supervisor 仍通过 Durable 轮询工作。
- callback、SSE 和前端临时状态都不是恢复依据。
- 任何广播异常都被隔离，不改变 worker 的 Durable 结果。
- 错误展示只使用结构化安全字段；原始异常仅留在本地日志/审计边界，不能进入浏览器。
