"""Q4 短期符号化卸载 — read_ref 工具:LLM 经 pointer `node=` 回查 refs/{node_id}.md 原文。

溯源链三层(由全景到细节):
  ① Mermaid canvas(全景:节点 + 边)
  ② messages 历史 pointer(`[offloaded node=<id> summary='...']`)
  ③ refs/{node_id}.md 原文(本工具守这一层)

当某次 tool-call 结果过胖被 `maybe_offload` 卸载后,messages 只留一行 pointer。
LLM 看到 pointer、需要精确细节时,主动调 `read_ref(node_id=...)` 取回完整原文。

路径安全(关键):node_id 来自 LLM(从 pointer 解析),必须按 refs 文件名 stem
校验 —— 白名单 `^[a-zA-Z0-9_-]+$`,拒绝 `/`、`\\`、`..`、空、扩展名等一切目录
穿越载体。非法 → 安全错误返回,绝不读盘。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from cc_harness.mcp_client import ToolResult

# refs 文件名 stem 白名单:覆盖 gen_id() 的 8-hex 与历史 "n1" 风格;拒绝一切
# 路径分隔符 / 父目录引用 / 扩展名,从源头切断 `../etc/passwd` 类穿越。
_NODE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


# OpenAI function-tool spec —— 与 cc_harness/tools.py:RUN_COMMAND_SPEC 同形:
# {"type": "function", "function": {"name", "description", "parameters"}}.
# parameters 是 JSON schema,node_id 必填、字符串类型。
READ_REF_SPEC: dict = {
    "type": "function",
    "function": {
        "name": "read_ref",
        "description": (
            "读取已卸载的工具结果原文(node_id 见 tool message pointer)。"
            "某次工具结果过胖被符号化卸载后,messages 历史只留一行 pointer "
            "`[offloaded node=<id> ...]`;需要完整原文做精确推理时,传 node_id "
            "调本工具取回。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "node_id": {
                    "type": "string",
                    "description": (
                        "被卸载节点的 id —— pointer 中 `node=` 后的值,"
                        "如 a1b2c3d4 或 n1。"
                    ),
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Zero-based line offset.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "description": "Maximum lines to return (default 200).",
                },
            },
            "required": ["node_id"],
        },
    },
}


async def read_ref_handler(
    args: dict, *, cwd: str, refs_dir: Path | str,
    manifest_path: Path | str | None = None,
) -> ToolResult:
    """读 `refs/{node_id}.md` 原文;非法 node_id / 缺文件 → 安全错误,不抛。

    Args:
        args:     ``{"node_id": "<id>"}``(LLM 从 pointer 解析出来)。
        cwd:      保留给 native tool 签名一致(本工具不读盘外文件,不用)。
        refs_dir: refs 目录(`maybe_offload` 落盘的根,extras 锭里 ``deps["refs_dir"]``)。

    Returns:
        ``ToolResult.success(content)`` —— 原文逐字;.llm_text 即文件内容;
        非法 node_id 或文件缺失 → ``ToolResult.error(...)``,**不抛异常**(防把 agent 弄哑)。
    """
    node_id = (args.get("node_id") or "").strip()
    if not node_id or not _NODE_ID_RE.match(node_id):
        # 非法 node_id 直接拒(防 `../etc/passwd`、绝对路径、分隔符穿越),不读盘
        return ToolResult.error(
            display=f"非法 node_id: {node_id!r}",
            llm=(
                "[Tool Error] node_id 非法:只允许字母/数字/下划线/连字符。"
                "请用 pointer 中 node= 后的字面值。"
            ),
        )
    # Defense-in-depth:即便日后 regex 被放宽,resolve() containment 仍是第二道闸 ——
    # 解析后路径必须仍在 refs_dir 内,否则拒。regex 是第一道(今天够用),这是第二道。
    refs_root = Path(refs_dir).resolve()
    ref_file = _resolve_ref_file(node_id, refs_root, manifest_path)
    try:
        # ref_file 此处是绝对路径(refs_root 已 resolve),relative_to 仅作 containment 校验。
        ref_file.relative_to(refs_root)
    except ValueError:
        return ToolResult.error(
            display=f"[read_ref] node_id 越界: {node_id}",
            llm="[read_ref] invalid node_id",
        )
    if not ref_file.is_file():
        return ToolResult.error(
            display=f"refs/{node_id}.md 不存在",
            llm=(
                f"[Tool Error] 未找到 node_id={node_id} 的 refs 原文"
                "(可能已被清理,或该节点未被卸载)。"
            ),
        )
    offset = max(0, int(args.get("offset", 0)))
    limit = min(500, max(1, int(args.get("limit", 200))))
    lines = ref_file.read_text(encoding="utf-8").splitlines()
    selected = lines[offset:offset + limit]
    next_offset = offset + len(selected)
    complete = next_offset >= len(lines)
    header = json.dumps(
        {
            "node_id": node_id,
            "offset": offset,
            "returned_lines": len(selected),
            "next_offset": None if complete else next_offset,
            "complete": complete,
            "total_lines": len(lines),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return ToolResult.success(header + "\n" + "\n".join(selected))


SEARCH_REF_SPEC: dict = {
    "type": "function",
    "function": {
        "name": "search_ref",
        "description": "Search within one offloaded tool-result node without loading it all.",
        "parameters": {
            "type": "object",
            "properties": {
                "node_id": {"type": "string"},
                "query": {"type": "string", "minLength": 1},
                "context_lines": {"type": "integer", "minimum": 0, "maximum": 10},
            },
            "required": ["node_id", "query"],
        },
    },
}


async def search_ref_handler(args: dict, *, cwd: str, refs_dir: Path | str) -> ToolResult:
    del cwd
    node_id = (args.get("node_id") or "").strip()
    if not node_id or not _NODE_ID_RE.match(node_id):
        return ToolResult.error("invalid node_id", "[Tool Error] invalid node_id")
    path = _resolve_ref_file(node_id, Path(refs_dir).resolve(), args.get("_manifest_path"))
    if not path.is_file():
        return ToolResult.error("missing node", f"[Tool Error] node_id={node_id} not found")
    query = str(args.get("query") or "").strip().casefold()
    if not query:
        return ToolResult.error("empty query", "[Tool Error] query is required")
    context = min(10, max(0, int(args.get("context_lines", 2))))
    lines = path.read_text(encoding="utf-8").splitlines()
    hits: list[dict] = []
    for index, line in enumerate(lines):
        if query not in line.casefold():
            continue
        start = max(0, index - context)
        end = min(len(lines), index + context + 1)
        hits.append({"line": index + 1, "context": lines[start:end]})
        if len(hits) >= 50:
            break
    return ToolResult.success(json.dumps({"node_id": node_id, "hits": hits}, ensure_ascii=False))


async def search_ref_with_manifest_handler(
    args: dict, *, cwd: str, refs_dir: Path | str, manifest_path: Path | str
) -> ToolResult:
    forwarded = dict(args)
    forwarded["_manifest_path"] = str(manifest_path)
    return await search_ref_handler(forwarded, cwd=cwd, refs_dir=refs_dir)


INSPECT_NODE_SPEC: dict = {
    "type": "function",
    "function": {
        "name": "inspect_node",
        "description": "Inspect provenance and integrity metadata for an offloaded node.",
        "parameters": {
            "type": "object",
            "properties": {"node_id": {"type": "string"}},
            "required": ["node_id"],
        },
    },
}


async def inspect_node_handler(
    args: dict, *, cwd: str, refs_dir: Path | str, manifest_path: Path | str
) -> ToolResult:
    del cwd, refs_dir
    node_id = (args.get("node_id") or "").strip()
    if not node_id or not _NODE_ID_RE.match(node_id):
        return ToolResult.error("invalid node_id", "[Tool Error] invalid node_id")
    path = Path(manifest_path)
    if not path.is_file():
        return ToolResult.error("missing manifest", "[Tool Error] offload manifest not found")
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("node_id") == node_id:
            records.append(record)
    if not records:
        return ToolResult.error("missing node", f"[Tool Error] node_id={node_id} not found")
    return ToolResult.success(json.dumps(records[-1], ensure_ascii=False, sort_keys=True))


def _resolve_ref_file(
    node_id: str, refs_root: Path, manifest_path: Path | str | None
) -> Path:
    if manifest_path is None:
        return refs_root / f"{node_id}.md"
    path = Path(manifest_path)
    if not path.is_file():
        return refs_root / f"{node_id}.missing"
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("node_id") != node_id:
            continue
        candidate = Path(str(record.get("result_ref") or "")).resolve()
        try:
            candidate.relative_to(refs_root)
        except ValueError:
            # Runtime snapshots copy the offload tree into a new workspace,
            # but the immutable nodes manifest intentionally keeps the
            # original absolute result_ref for provenance.  Rebase only the
            # suffix below the recorded ``refs`` directory; never follow an
            # arbitrary path outside the active refs root.
            candidate = _rebase_snapshot_ref(candidate, refs_root)
            try:
                candidate.relative_to(refs_root)
            except ValueError:
                return refs_root / f"{node_id}.invalid"
        return candidate
    return refs_root / f"{node_id}.missing"


def _rebase_snapshot_ref(candidate: Path, refs_root: Path) -> Path:
    """Map a copied manifest's absolute ``.../refs/...`` path locally."""
    positions = [
        index for index, part in enumerate(candidate.parts) if part.casefold() == "refs"
    ]
    if not positions or positions[-1] == len(candidate.parts) - 1:
        return candidate
    relative = Path(*candidate.parts[positions[-1] + 1 :])
    return refs_root / relative
