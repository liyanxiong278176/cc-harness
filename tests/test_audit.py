import json
from pathlib import Path

from cc_harness.audit import log_decision


def test_log_decision_writes_jsonl_line(tmp_path: Path):
    p = tmp_path / "logs" / "policy.jsonl"
    log_decision(
        p, iter_n=3, tool="run_command", args={"command": "cat ~/.ssh/id_rsa"},
        action="ask", outcome="denied", rule_id="shell_ask",
        reason="shell 需确认", mode="coding",
    )
    assert p.exists()
    line = p.read_text(encoding="utf-8").strip()
    entry = json.loads(line)
    assert entry["tool"] == "run_command"
    assert entry["decision"] == "ask"
    assert entry["outcome"] == "denied"
    assert entry["args"]["command"] == "cat ~/.ssh/id_rsa"
    assert entry["iter"] == 3


def test_log_decision_appends(tmp_path: Path):
    p = tmp_path / "logs" / "policy.jsonl"
    for i in range(3):
        log_decision(p, iter_n=i, tool="run_command", args={},
                     action="allow", outcome="executed",
                     rule_id="r", reason="", mode="coding")
    assert len(p.read_text(encoding="utf-8").strip().splitlines()) == 3


def test_log_decision_swallows_write_error(tmp_path: Path, monkeypatch):
    # 路径不可写不应抛
    # 让 open 抛异常
    p2 = tmp_path / "x.jsonl"
    p2.write_text("x", encoding="utf-8")
    monkeypatch.setattr("builtins.open", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    # 不应抛
    log_decision(p2, iter_n=1, tool="t", args={}, action="allow",
                 outcome="executed", rule_id="r", reason="", mode="coding")
