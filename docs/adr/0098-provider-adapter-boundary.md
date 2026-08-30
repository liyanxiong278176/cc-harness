# Keep provider differences behind adapters

The Durable Runtime will consume one provider-neutral model event contract, while each provider adapter owns request construction, streaming/tool-call normalization, lossless message replay, capability detection, usage/cost parsing, and provider error mapping. The first supported scope is OpenAI-compatible DeepSeek, GLM, Kimi, MiniMax, and Qwen; native provider protocols can be added later without branching the Runtime state machine.
