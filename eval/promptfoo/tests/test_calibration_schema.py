"""Tests for calibration_set.yaml 双区 schema + 50 baseline 占位(Task 9)。

schema 是 contract:每条 entry 必含 6 维(id/prompt/severity/hold_broke/
borderline/leak_type),`validate_entry` 全 True 才 True。
`load_calibration_set` 加载 baseline+pending 双区,缺键兜底空 list。

Loaded via importlib(tools dir is not on sys.path in the test runner)。
"""
import importlib.util
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tools" / "calibration_schema.py"
_SPEC = importlib.util.spec_from_file_location("calibration_schema", TOOL)
cs = importlib.util.module_from_spec(_SPEC)
sys.modules["calibration_schema"] = cs  # 让 from calibration_schema import ... 工作
_SPEC.loader.exec_module(cs)

from calibration_schema import load_calibration_set, validate_entry  # noqa: E402


def test_validate_entry_dimensions():
    """6 维全合法 → True。"""
    e = {
        "id": "c1",
        "prompt": "x",
        "severity": "critical",
        "hold_broke": 1,
        "borderline": False,
        "leak_type": "none",
    }
    assert validate_entry(e) is True


def test_validate_entry_rejects_bad_leak_type():
    """leak_type 不在白名单 → False。"""
    e = {
        "id": "c1",
        "prompt": "x",
        "severity": "critical",
        "hold_broke": 1,
        "borderline": False,
        "leak_type": "bogus",
    }
    assert validate_entry(e) is False


def test_load_calibration_set_has_two_zones(tmp_path):
    """YAML 含 baseline+pending → load 后两 key 都在。"""
    import yaml

    f = tmp_path / "calibration_set.yaml"
    f.write_text(
        yaml.dump(
            {
                "baseline": [
                    {
                        "id": "c1",
                        "prompt": "x",
                        "severity": "critical",
                        "hold_broke": 1,
                        "borderline": False,
                        "leak_type": "none",
                    }
                ],
                "pending": [],
            }
        ),
        encoding="utf-8",
    )
    result = load_calibration_set(f)
    assert "baseline" in result and "pending" in result
    assert len(result["baseline"]) == 1
    assert result["pending"] == []


def test_real_calibration_set_yaml_validates():
    """真实 calibration_set.yaml → 50 条 baseline 全过 validate_entry,分布 15-15-10-10。"""
    from collections import Counter

    yaml_path = Path(__file__).resolve().parents[1] / "judges" / "calibration_set.yaml"
    cs = load_calibration_set(yaml_path)
    assert len(cs["baseline"]) == 50
    assert cs["pending"] == []

    bad = [e for e in cs["baseline"] if not validate_entry(e)]
    assert not bad, f"invalid entries: {bad[:3]}"

    sev = Counter(e["severity"] for e in cs["baseline"])
    assert sev == {"critical": 15, "high": 15, "medium": 10, "low": 10}
