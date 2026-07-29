"""Calibration set 双区 schema(Task 9)。

`calibration_set.yaml` 含 baseline(50 条人工标)+ pending(失败驱动收集)。
每条 entry 6 维:id/prompt/severity/hold_broke/borderline/leak_type。
`validate_entry` 是 contract,T10 拿这函数做 κ 计算前的字段验证。
`load_calibration_set` 加载双区,缺键兜底空 list(防御 yaml.safe_load 返回 None)。

placeholder 默认值(无真实 eval result 时):leak_type=none,
borderline=false, hold_broke=1。每条带 `# TODO: 人工校准` 注释提醒
后续 implementer 跑一次 eval 后改真实值。
"""
from __future__ import annotations

from pathlib import Path

import yaml

_LEAK_TYPES = {"none", "credential", "pii", "prompt", "system"}
_SEVERITIES = {"critical", "high", "medium", "low"}


def validate_entry(e: dict) -> bool:
    """6 维全合法 → True。任一不合法 → False。

    - id: str
    - prompt: str
    - severity: critical|high|medium|low
    - hold_broke: 0 或 1
    - borderline: bool
    - leak_type: none|credential|pii|prompt|system
    """
    return (
        isinstance(e.get("id"), str)
        and isinstance(e.get("prompt"), str)
        and e.get("severity") in _SEVERITIES
        and e.get("hold_broke") in (0, 1)
        and isinstance(e.get("borderline"), bool)
        and e.get("leak_type") in _LEAK_TYPES
    )


def load_calibration_set(path: Path) -> dict:
    """加载双区,缺键兜底空 list。空文件 → 双区都空 list。

    Returns:
        {"baseline": list, "pending": list}
    """
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {
        "baseline": data.get("baseline") or [],
        "pending": data.get("pending") or [],
    }
