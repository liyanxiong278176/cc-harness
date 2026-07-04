"""attacks_exec.yaml(allow 模式执行类攻击)格式 + pluginId 归 L8(spec §6.4)。"""
from pathlib import Path
import yaml

ATTACKS_EXEC = Path(__file__).resolve().parents[1] / "eval" / "promptfoo" / "attacks_exec.yaml"

# defense_matrix 执行类(主层 L8,allow 模式跑 → 测沙箱隔离)
EXEC_PLUGINS = {
    "coding-agent:sandbox-read-escape", "coding-agent:sandbox-write-escape",
    "coding-agent:network-egress-bypass", "coding-agent:procfs-credential-read",
    "coding-agent:secret-env-read", "coding-agent:secret-file-read",
}


def _attacks_exec():
    return yaml.safe_load(ATTACKS_EXEC.read_text(encoding="utf-8"))


def test_format():
    """每条有 prompt + severity + pluginId(且 pluginId 是 matrix 执行类)。"""
    for a in _attacks_exec():
        assert "vars" in a and "prompt" in a["vars"]
        assert a["metadata"].get("severity") in {"critical", "high", "medium", "low"}
        pid = a["metadata"].get("pluginId")
        assert pid in EXEC_PLUGINS, f"执行类样本 pluginId 必须是 matrix 执行类(L8): {pid}"


def test_covers_all_exec_surfaces():
    """至少覆盖 6 个执行面:读逃逸 / 写逃逸 / egress / procfs / secret-env / secret-file。"""
    covered = {a["metadata"]["pluginId"] for a in _attacks_exec()}
    missing = EXEC_PLUGINS - covered
    assert not missing, f"attacks_exec 缺执行面: {missing}"
