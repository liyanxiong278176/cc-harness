"""Validate unified.yaml's inline allow-mode execution cases for L8 coverage."""
from pathlib import Path

import yaml

UNIFIED = Path(__file__).resolve().parents[1] / "eval" / "promptfoo" / "promptfooconfig.unified.yaml"

# defense_matrix 执行类(主层 L8,allow 模式跑 → 测沙箱隔离)
EXEC_PLUGINS = {
    "coding-agent:sandbox-read-escape", "coding-agent:sandbox-write-escape",
    "coding-agent:network-egress-bypass", "coding-agent:procfs-credential-read",
    "coding-agent:secret-env-read", "coding-agent:secret-file-read",
}


def _attacks_exec():
    """Return unified config entries that exercise an execution surface."""
    cfg = yaml.safe_load(UNIFIED.read_text(encoding="utf-8"))
    return [
        test for test in cfg["tests"]
        if isinstance(test, dict)
        and test.get("metadata", {}).get("pluginId") in EXEC_PLUGINS
    ]


def test_format():
    """每条有 prompt + severity + pluginId(且 pluginId 是 matrix 执行类)。"""
    for attack in _attacks_exec():
        assert "vars" in attack and "prompt" in attack["vars"]
        assert attack["metadata"].get("severity") in {"critical", "high", "medium", "low"}
        pid = attack["metadata"].get("pluginId")
        assert pid in EXEC_PLUGINS, f"执行类样本 pluginId 必须是 matrix 执行类(L8): {pid}"


def test_covers_all_exec_surfaces():
    """至少覆盖 6 个执行面:读逃逸 / 写逃逸 / egress / procfs / secret-env / secret-file。"""
    covered = {attack["metadata"]["pluginId"] for attack in _attacks_exec()}
    missing = EXEC_PLUGINS - covered
    assert not missing, f"unified config 缺执行面: {missing}"
