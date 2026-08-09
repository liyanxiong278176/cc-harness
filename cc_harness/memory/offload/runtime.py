"""Build session-isolated symbolic offload tools independently of long-term memory."""
from __future__ import annotations

from pathlib import Path

from cc_harness.config import ContextConfig
from cc_harness.memory.config import MemoryConfig
from cc_harness.memory.offload.mermaid import update_canvas
from cc_harness.memory.offload.offload import maybe_offload
from cc_harness.memory.offload.read_ref import (
    INSPECT_NODE_SPEC,
    READ_REF_SPEC,
    SEARCH_REF_SPEC,
    inspect_node_handler,
    read_ref_handler,
    search_ref_with_manifest_handler,
)


def build_context_offload(
    state_dir: Path,
    *,
    session_id: str,
    llm,
    memory_config: MemoryConfig,
    context_config: ContextConfig,
) -> tuple[list[dict], dict]:
    root = Path(state_dir) / "context" / session_id / "offload"
    refs_dir = root / "refs"
    canvas_path = root / "graph.mmd"
    manifest_path = root / "nodes.jsonl"
    refs_dir.mkdir(parents=True, exist_ok=True)

    async def offload(result_text, tool_name, args, *, threshold, token_counter):
        return await maybe_offload(
            result_text,
            tool_name,
            args,
            threshold,
            refs_dir,
            llm,
            token_counter,
            manifest_path=manifest_path,
            session_id=session_id,
        )

    async def canvas(node_id, label, summary, edge_from):
        return await update_canvas(
            node_id,
            label,
            summary,
            edge_from,
            canvas_path=canvas_path,
            llm=llm,
        )

    extras = [
        {
            "spec": READ_REF_SPEC,
            "handler": read_ref_handler,
            "deps": {"refs_dir": refs_dir, "manifest_path": manifest_path},
        },
        {
            "spec": SEARCH_REF_SPEC,
            "handler": search_ref_with_manifest_handler,
            "deps": {"refs_dir": refs_dir, "manifest_path": manifest_path},
        },
        {
            "spec": INSPECT_NODE_SPEC,
            "handler": inspect_node_handler,
            "deps": {"refs_dir": refs_dir, "manifest_path": manifest_path},
        },
    ]
    deps = {
        "refs_dir": refs_dir,
        "canvas_path": canvas_path,
        "manifest_path": manifest_path,
        "offload": offload,
        "canvas": canvas,
        "enabled": memory_config.offload_enabled,
        "canvas_inject": memory_config.offload_canvas_inject,
        "threshold": memory_config.offload_threshold,
        "mermaid_max_token_ratio": memory_config.mermaid_max_token_ratio,
        "offload_ratio": memory_config.offload_ratio,
        "context_window": context_config.context_window,
    }
    return extras, deps
