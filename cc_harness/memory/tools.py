"""memory_recall and memory_save tool specs and handlers.

These are NOT registered in NATIVE_TOOLS (which is module-level and has
no way to bind per-call dependencies). Instead, agent.run_turn appends
the specs to tool_specs and constructs handler closures with bound
service/retriever in native_handlers.
"""
from __future__ import annotations
from cc_harness.mcp_client import ToolResult
from cc_harness.memory.embedding import EmbeddingError


MEMORY_RECALL_SPEC = {
    "type": "function",
    "function": {
        "name": "memory_recall",
        "description": "按语义查询长期记忆,返回 top-k 相似记忆。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "查询关键词或描述"},
            },
            "required": ["query"],
        },
    },
}


MEMORY_SAVE_SPEC = {
    "type": "function",
    "function": {
        "name": "memory_save",
        "description": "保存一条长期记忆。系统自动检索相似记忆并执行 ADD/UPDATE/DELETE/NOOP。",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要保存的记忆文本"},
            },
            "required": ["text"],
        },
    },
}


def _format_recall_results(results) -> str:
    if not results:
        return "(没有匹配的长期记忆)"
    lines = [f"找到 {len(results)} 条相关记忆:"]
    for i, (mem, distance) in enumerate(results, 1):
        lines.append(f"  {i}. [{mem.id}] {mem.text}  (源: {mem.source}, 距离: {distance:.3f})")
    return "\n".join(lines)


def _format_save_result(result) -> str:
    if result.action == "ERROR":
        return f"[Tool Error] memory_save 失败: {result.error}"
    parts = [f"memory_save 结果: {result.action}"]
    if result.action == "UPDATE" and result.previous:
        parts.append(f"  旧: {result.previous.text}")
    if result.memory:
        parts.append(f"  新: {result.memory.text}")
    if result.deleted_id:
        parts.append(f"  已删除旧记忆: {result.deleted_id}")
    parts.append(f"  耗时: {result.duration_ms}ms")
    return "\n".join(parts)


async def memory_recall_handler(args, *, cwd, retriever):
    query = (args.get("query") or "").strip()
    if not query:
        return ToolResult.error(display="query 不能为空", llm="[Tool Error] query 不能为空")
    try:
        results = await retriever.search(query, top_k=5)
        return ToolResult.success(_format_recall_results(results))
    except EmbeddingError as e:
        return ToolResult.error(
            display=f"embedding 失败: {e}",
            llm=f"[Tool Error] 记忆系统暂时不可用(embedding 失败): {e}",
        )
    except Exception as e:
        return ToolResult.error(
            display=f"recall 失败: {e}",
            llm=f"[Tool Error] memory_recall 失败: {type(e).__name__}: {e}",
        )


async def memory_save_handler(args, *, cwd, service):
    text = (args.get("text") or "").strip()
    if not text:
        return ToolResult.error(display="text 不能为空", llm="[Tool Error] text 不能为空")
    try:
        result = await service.save(text, source="llm")
        return ToolResult.success(_format_save_result(result))
    except EmbeddingError as e:
        return ToolResult.error(
            display=f"embedding 失败: {e}",
            llm=f"[Tool Error] 记忆系统暂时不可用(embedding 失败): {e}",
        )
    except Exception as e:
        return ToolResult.error(
            display=f"save 失败: {e}",
            llm=f"[Tool Error] memory_save 失败: {type(e).__name__}: {e}",
        )
