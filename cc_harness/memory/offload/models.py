"""Q4 短期符号化卸载数据结构。

与 `cc_harness/memory/models.py` 同风格(plain dataclass,非 pydantic)。
OffloadResult 是单次卸载动作的产物:把某个 ReAct 节点的胖 tool-call 结果
写进 `refs/{node_id}.md`,在 messages 历史里只留一行指针 `pointer_msg`。
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class OffloadResult:
    """单次卸载结果。

    Attributes:
        node_id:     被卸载的 ReAct 节点 id(如 "n1")。
        summary:     LLM 生成的该节点摘要(注入指针附近,保留语义)。
        refs_path:   落盘的 refs 文件绝对路径(refs/{node_id}.md)。
        pointer_msg: 替换原胖结果、留在 messages 历史里的指针串
                     (如 "[offloaded node=n1]")。
    """
    node_id: str
    summary: str
    refs_path: str
    pointer_msg: str
