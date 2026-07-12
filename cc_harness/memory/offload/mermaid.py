"""Q4 短期符号化卸载 — update_canvas:累积 Mermaid graph LR 到 canvas.md。

高层"全景"图层:每个 ReAct 节点(刚跑完的 tool)在 canvas 里追加一个节点 + 一条边。
三层可追溯:Mermaid canvas(全景)→ pointer(node=)→ refs/{node_id}.md 原文。

核心不变量:Mermaid node id == `node_id` 字面(绝不许 LLM 改名)——
这是三处一致(refs 文件名 == canvas 节点 id == pointer node=)的基石。
LLM 只建议"可见标签",调用方锁 node id。
"""
from __future__ import annotations
from pathlib import Path


async def update_canvas(
    node_id: str,
    label: str,
    summary: str,
    edge_from: str | None,
    canvas_path: Path | str,
    llm,
) -> str:
    """追加一个节点(+ edge_from)到 canvas.md(Mermaid `graph LR`)。

    读取-修改-写回:已有节点/边原样保留,仅在本调用追加新行。头只写一次。

    Args:
        node_id:    Mermaid 节点 id(字面使用,LLM 无法改)。
        label:      确定性 fallback 的可见文本(llm=None / 失败 / 空 时用;一般是 tool 名)。
        summary:    该节点摘要;以 Mermaid 注释附在节点旁(保语义,不破坏节点行解析)。
        edge_from:  前驱节点 id;None → 首节点不加边。
        canvas_path:canvas.md 路径(父目录自动建;不存在则起一份新 canvas)。
        llm:        LLM client,产简短节点标签;None / 异常 / 空 content → fallback 到 `label`。

    Returns:
        完整 canvas 内容字符串(已落盘)。
    """
    canvas_path = Path(canvas_path)
    canvas_path.parent.mkdir(parents=True, exist_ok=True)

    # 确定可见标签:LLM 优先,fail-soft 回退到 label 参数(同 maybe_offload 范式)
    visible_label = label
    if llm is not None:
        try:
            llm_label = await _llm_node(llm, label, summary)
        except Exception:
            llm_label = ""
        if llm_label:
            visible_label = llm_label

    # 防破坏 Mermaid 语法:label/summary 去换行 + 引号转义
    visible_label = visible_label.replace("\n", " ").replace('"', "'")
    summary_sanitized = summary.replace("\n", " ")

    # 读现有 canvas(append);不存在 → 起新头
    if canvas_path.exists():
        body = canvas_path.read_text(encoding="utf-8")
    else:
        body = "graph LR\n"

    lines = [body.rstrip("\n")] if body.strip() else ["graph LR"]
    lines.append(f"%% {node_id}: {summary_sanitized}")          # 注释保留语义
    lines.append(f'{node_id}["{visible_label}"]')                # node id 锁 node_id
    if edge_from is not None:
        lines.append(f"{edge_from} --> {node_id}")               # 链边

    content = "\n".join(lines) + "\n"
    canvas_path.write_text(content, encoding="utf-8")
    return content


async def _llm_node(llm, label: str, summary: str) -> str:
    """LLM 产简短节点标签 —— 镜像 offload._llm_summary / persona._llm_persona 范式。

    **只产可见 LABEL,绝不产 node id**(调用方锁 node_id,保三处一致)。
    """
    content = ""
    msgs = [
        {
            "role": "system",
            "content": "给工具调用步骤生成 Mermaid 节点标签(简短,10 字内,只返回标签文本)。",
        },
        {"role": "user", "content": f"工具:{label}\n摘要:{summary}"},
    ]
    async for ev in llm.chat(msgs, tools=None):
        if ev.kind == "done" and ev.content:
            content = ev.content
    return content
