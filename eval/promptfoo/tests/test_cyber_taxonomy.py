"""Cyber taxonomy coverage tests for eval-v2 Task 12."""
import importlib.util
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ATTACKS = ROOT / "attacks.yaml"
MATRIX = ROOT / "defense_matrix.yaml"
TOOL = ROOT / "tools" / "report_to_md.py"
_SPEC = importlib.util.spec_from_file_location("report_to_md", TOOL)
rtm = importlib.util.module_from_spec(_SPEC)
sys.modules["report_to_md"] = rtm
_SPEC.loader.exec_module(rtm)

from report_to_md import _load_defense_matrix  # noqa: E402

CYBER_SUBCATS = {
    "privilege-escalation",
    "lateral-movement",
    "persistence",
    "data-exfiltration",
    "credential-theft",
    "supply-chain",
}


def test_attacks_yaml_has_6_cyber_subcats_min5_each():
    attacks = yaml.safe_load(ATTACKS.read_text(encoding="utf-8")) or []
    by_cat = {}
    for attack in attacks:
        cat = (attack.get("metadata") or {}).get("category", "")
        if cat in CYBER_SUBCATS:
            by_cat[cat] = by_cat.get(cat, 0) + 1
    for cat in CYBER_SUBCATS:
        assert by_cat.get(cat, 0) >= 5, f"{cat} 不足 5 条(实际 {by_cat.get(cat, 0)})"


def test_matrix_registers_all_cyber_subcats():
    matrix = _load_defense_matrix()
    for cat in CYBER_SUBCATS:
        assert cat in matrix, f"{cat} 未登记 defense_matrix(classify_layer 会 UnknownCategoryError)"
