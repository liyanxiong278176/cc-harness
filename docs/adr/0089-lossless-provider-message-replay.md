# Preserve complete provider messages for recovery

Status: Superseded for newly written raw reasoning fields by [ADR-0090](0090-keep-raw-model-reasoning-transient.md). This record remains relevant to older immutable artifacts and other provider-owned fields.

Durable Runtime will persist complete provider-neutral assistant messages together with provider-specific fields, and each adapter will serialize them losslessly during replay. Missing required fields become a recoverable protocol error rather than fabricated values or a replay of side effects, because thinking-mode providers can require fields such as `reasoning_content` to preserve request semantics.
