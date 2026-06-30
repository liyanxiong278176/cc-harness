"""决策审计:每次 PolicyEngine 决策落一行 JSON 到 <root>/logs/policy.jsonl。

写失败只 warn 不阻塞(可用性优先)。路径由调用方传入(钉到项目根,不随 CWD 漂移)。
"""
from __future__ import annotations
import json
import time
from pathlib import Path
from rich.console import Console

_console = Console()


def log_decision(
    path: Path,
    *,
    iter_n: int,
    tool: str,
    args: dict,
    action: str,
    outcome: str,
    rule_id: str,
    reason: str,
    mode: str,
) -> None:
    """追加一条决策记录。任何 IO 异常都吞掉(只 warn)。"""
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "iter": iter_n,
        "tool": tool,
        "args": args,
        "decision": action,
        "outcome": outcome,
        "rule_id": rule_id,
        "reason": reason,
        "mode": mode,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        _console.print(f"[red]audit write failed:[/red] {e}")
