# 模型原始推理仅保留在内存中

状态：已采纳（2026-09-30）。对于新写入的 assistant artifact，本决策取代 ADR-0089 中关于持久化 provider 原始 reasoning 字段的决定。

实时流可以把 provider reasoning delta 发给内存中的思考 UI；新的 assistant-message artifact 与会话检查点不得写入原始 `reasoning_content`。`AssistantMessageCommitted` 仍是用户可见正文的唯一权威来源，既有 `run_events` schema 与 Runtime 状态机保持不变。

较早的不可变 assistant artifact 仍可能包含 provider reasoning。为保持兼容，它们继续可读，不会被原地改写。当旧 provider 拒绝不带 reasoning 的回放时，LLM client 会在 thinking-disabled 模式下有限重试一次，并从这次 provider 请求中移除 reasoning 字段。如果 provider 只输出 reasoning、没有可见正文或工具调用，client 会暂缓终态事件，并在 thinking-disabled 模式下重试一次；绝不把私有 reasoning 升级成回答正文。

这样可使新写入的会话不含私有 reasoning，并在 provider 支持无 reasoning 请求时保留工具调用回放。如果 provider 依赖旧字段，有限降级重试会明确处理兼容性，而不更改 Durable 事件，也不重放工具副作用。
