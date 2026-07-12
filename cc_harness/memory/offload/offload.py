"""Q4 短期符号化卸载 — maybe_offload:胖 tool-call 结果落 refs + 摘要 + 指针。

单次卸载动作:某个 ReAct 节点的 tool-call 结果 token 严格 > threshold →
① 原文逐字写 `refs/{node_id}.md`;
② LLM 一句话摘要(llm=None / LLM 失败 / 空 content → fail-soft 取前 200 字);
③ messages 历史里只留一行 `pointer_msg`(`node={node_id}`)。

`node_id` 由 `gen_id()` 生成一次、三处复用(refs 文件名 / pointer_msg / refs_path),
保证 `read_ref(node_id)` 能 100% 还原原文 —— "三处一致"是 Q4 的核心不变量。
"""
from __future__ import annotations
from pathlib import Path
from uuid import uuid4

from cc_harness.memory.offload.models import OffloadResult


def gen_id() -> str:
    """短稳定 node_id(uuid4 前 8 hex)。三处复用:refs 文件名 / pointer / refs_path。"""
    return uuid4().hex[:8]


async def maybe_offload(
    result_text: str,
    tool_name: str,
    args: dict,
    threshold: int,
    refs_dir: Path | str,
    llm,
    token_counter,
) -> OffloadResult | None:
    """token 严格 > threshold → 卸载;否则 None。

    Args:
        result_text:    tool-call 的原始结果文本(将被逐字落盘)。
        tool_name:      触发该结果的 tool 名(留作后续过滤/审计,本函数不使用)。
        args:           该 tool-call 的参数(同上,预留)。
        threshold:      严格大于才卸载(== threshold 不卸)。
        refs_dir:       refs/{node_id}.md 落盘目录(不存在则自动建)。
        llm:            摘要用的 LLM client(None → fail-soft 取前 200 字,不调 LLM)。
        token_counter:  TokenCounter,用 `count_text` 量 result_text。

    Returns:
        OffloadResult | None —— 超阈值返回卸载产物,否则 None。
    """
    if token_counter.count_text(result_text) <= threshold:
        return None

    node_id = gen_id()
    refs_path = Path(refs_dir)
    refs_path.mkdir(parents=True, exist_ok=True)
    ref_file = refs_path / f"{node_id}.md"
    ref_file.write_text(result_text, encoding="utf-8")  # 逐字落盘

    if llm is not None:
        try:
            summary = await _llm_summary(llm, result_text)
        except Exception:
            summary = ""
        if not summary:  # 空 content(refusal / filter / mid-stream err)→ fail-soft
            summary = result_text[:200]
    else:
        summary = result_text[:200]  # fail-soft:前 200 字,不调 LLM

    pointer_msg = (
        f"[offloaded node={node_id} summary='{summary}' (refs/{node_id}.md)]"
    )
    return OffloadResult(
        node_id=node_id,
        summary=summary,
        refs_path=str(ref_file),
        pointer_msg=pointer_msg,
    )


async def _llm_summary(llm, result_text: str) -> str:
    """LLM 一句话摘要(200 字内)—— 镜像 persona._llm_persona 的 stream-collect 范式。"""
    content = ""
    msgs = [
        {"role": "system", "content": "一句话摘要(200字内)"},
        {"role": "user", "content": result_text},
    ]
    async for ev in llm.chat(msgs, tools=None):
        if ev.kind == "done" and ev.content:
            content = ev.content
    return content
