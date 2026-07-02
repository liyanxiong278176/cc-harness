"""defense_matrix.yaml = category/pluginId -> 防御层 + severity 单一来源。
report_to_md.py 从它加载(取代原 PLUGIN_CATEGORY/STATIC_CATEGORY 硬编码表)。"""
from pathlib import Path
import pytest
import yaml

MATRIX_PATH = Path(__file__).resolve().parents[1] / "eval" / "promptfoo" / "defense_matrix.yaml"


def _load():
    if not MATRIX_PATH.exists():
        pytest.fail("defense_matrix.yaml 不存在")
    return yaml.safe_load(MATRIX_PATH.read_text(encoding="utf-8"))


VALID_LAYERS = {"L2", "L4", "L5", "judge"}
VALID_SEV = {"critical", "high", "medium", "low"}

# 静态 8 + 动态 9 + 新增 5(prompt-extraction 仅插件,见 REQUIRED_PLUGINS)
REQUIRED_CATEGORIES = {
    "credential-exfil", "shell-injection", "self-modification", "fs-overreach",
    "hijacking", "supply-chain", "gate-escape", "credential-sideways",
    "indirect-prompt-injection", "ssrf", "sql-injection", "data-exfiltration",
    "excessive-agency", "rbac", "persistence", "resource-exhaustion", "git-rce",
    "pii-exfil", "encoded-exfil", "repo-injection", "terminal-injection", "delayed-ci-exfil",
}
# redteam.yaml 现有插件(除 harmful:special-case)+ coding-agent 13 件 + mcp
REQUIRED_PLUGINS = {
    "ascii-smuggling", "indirect-prompt-injection", "prompt-extraction",
    "excessive-agency", "rbac", "bfla", "bola", "shell-injection",
    "sql-injection", "ssrf",
    "pii:direct", "pii:api-db", "pii:session", "pii:social",
    "hallucination", "overreliance", "mcp",
    "coding-agent:repo-prompt-injection", "coding-agent:terminal-output-injection",
    "coding-agent:secret-env-read", "coding-agent:secret-file-read",
    "coding-agent:sandbox-read-escape", "coding-agent:sandbox-write-escape",
    "coding-agent:network-egress-bypass", "coding-agent:procfs-credential-read",
    "coding-agent:delayed-ci-exfil", "coding-agent:generated-vulnerability",
    "coding-agent:automation-poisoning", "coding-agent:steganographic-exfil",
    "coding-agent:verifier-sabotage",
}


def test_every_entry_well_formed():
    mx = _load()
    for k, cfg in mx.items():
        assert isinstance(cfg.get("layer"), list) and cfg["layer"], f"{k} layer 非空列表"
        assert set(cfg["layer"]) <= VALID_LAYERS, f"{k} layer 非法 {cfg['layer']}"
        assert cfg.get("severity") in VALID_SEV, f"{k} severity 非法 {cfg.get('severity')}"


@pytest.mark.parametrize("cat", sorted(REQUIRED_CATEGORIES))
def test_category_covered(cat):
    assert cat in _load(), f"matrix 缺 category: {cat}"


@pytest.mark.parametrize("plugin", sorted(REQUIRED_PLUGINS))
def test_plugin_covered(plugin):
    assert plugin in _load(), f"matrix 缺 plugin: {plugin}"
