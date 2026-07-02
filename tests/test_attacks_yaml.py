"""attacks.yaml 5 新类 + 格式。"""
from pathlib import Path
import yaml
from collections import Counter

ATTACKS = Path(__file__).resolve().parents[1] / "eval" / "promptfoo" / "attacks.yaml"
NEW = {"pii-exfil", "encoded-exfil", "repo-injection", "terminal-injection", "delayed-ci-exfil"}


def _attacks():
    return yaml.safe_load(ATTACKS.read_text(encoding="utf-8"))


def test_format():
    for a in _attacks():
        assert "vars" in a and "prompt" in a["vars"]
        assert a["metadata"].get("severity") in {"critical", "high", "medium", "low"}


def test_new_five_present_and_min_count():
    c = Counter(a["metadata"]["category"] for a in _attacks())
    for cat in NEW:
        assert c[cat] >= 4, f"{cat} 仅 {c[cat]} 条"
