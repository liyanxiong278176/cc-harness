"""memory_recall and memory_save tool specs and handlers.

These are NOT registered in NATIVE_TOOLS (which is module-level and has
no way to bind per-call dependencies). Instead, agent.run_turn appends
the specs to tool_specs and constructs handler closures with bound
service/retriever in native_handlers.

Phase 2 (Q1 uplift): memory_recall_handler 自动多 query 重试 —
首次返回空时,自动用改写 query 重试 N 次(N 由 MAX_RECALL_RETRIES env
控制,默认 2 = 最多 3 次调用)。改写策略见 _rewrite_query。
动机: 模型在 memory_recall 首次无结果时常直接放弃("I don't know"),
自动重试用 entity/keyword 兜底召回,降低投降率。
"""
from __future__ import annotations
import os
import re
from cc_harness.mcp_client import ToolResult
from cc_harness.memory.embedding import EmbeddingError


# Phase 2: 重试上限。0 = 旧行为(不重试),2 = 最多 3 次(默认)。
_MAX_RECALL_RETRIES = int(os.getenv("MAX_RECALL_RETRIES", "2"))


# 英文问句词(去问句重写用)。中文不需要改写(没明显的"WH-word"前缀结构)。
_QUESTION_WORDS = re.compile(
    r"^(when|what|where|who|whom|whose|why|how|which|is|are|was|were|"
    r"do|does|did|can|could|will|would|should|may|might)\s+"
    r"(did|is|are|was|were|do|does|did|will|would|has|have|had|to)?\s*",
    flags=re.IGNORECASE,
)

# 实体抽取: 1) 大写英文词序列 2) 全大写缩写 (LGBTQ, USA) 3) 数字 4) 引号内容 5) 中文姓名
_ENTITY_PATTERN = re.compile(
    r"(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})"  # Capitalized phrase (1-4 words)
    r"|(?:[A-Z]{2,})"  # All-caps acronyms (LGBTQ, USA, AI)
    r"|(?:\d{1,4}(?:[/-]\d{1,2})?(?:[/-]\d{1,4})?)"  # Date-like numbers
    r"|(?:['\"][^'\"]{2,30}['\"])"  # Quoted strings
    r"|(?:[一-鿿]{2,4})",  # Chinese 2-4 char (rough person/place name)
)


def _rewrite_query(query: str, attempt: int) -> str:
    """Rewrite a recall query for retry attempts.

    attempt 0: drop leading question words ("When did Melanie..." → "Melanie...")
    attempt 1: extract entities/numbers/quoted strings (keep only the nouns)
    attempt 2+: return original (no more rewrites; fail-soft)
    """
    if attempt < 0:
        return query
    if attempt == 0:
        rewritten = _QUESTION_WORDS.sub("", query).strip()
        return rewritten or query
    if attempt == 1:
        entities = _ENTITY_PATTERN.findall(query)
        if not entities:
            return query
        # entities 是 list[tuple|str],findall 多个 alt 时返 tuple,展平
        flat = [e if isinstance(e, str) else " ".join(e) for e in entities]
        return " ".join(flat) or query
    return query


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
        # Phase 2: 多 query 重试。attempt=0 用原 query,1..N 用 _rewrite_query 改写。
        # 首次有结果立即返回(避免无谓重试);空结果才重试。
        for attempt in range(_MAX_RECALL_RETRIES + 1):
            q = query if attempt == 0 else _rewrite_query(query, attempt - 1)
            if not q:
                continue
            results = await retriever.search(q, top_k=5)
            if results:
                return ToolResult.success(_format_recall_results(results))
        # 全部尝试都空 → 兜底原行为
        return ToolResult.success(_format_recall_results([]))
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
