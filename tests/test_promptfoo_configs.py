"""promptfoo 配置 YAML 语法 + 关键断言存在性验证(不跑 promptfoo)。"""
from pathlib import Path
import yaml

EVAL = Path(__file__).resolve().parents[1] / "eval" / "promptfoo"


def _load(name):
    return yaml.safe_load((EVAL / name).read_text(encoding="utf-8"))


def test_security_yaml_has_deterministic_asserts():
    cfg = _load("promptfooconfig.security.yaml")
    asserts = cfg["defaultTest"]["assert"]
    types = [a["type"] for a in asserts]
    assert "not-contains-any" in types, "缺密钥确定性断言"
    assert "javascript" in types, "缺 PII 确定性断言"
    assert "llm-rubric" in types  # rubric 兜底仍在


def test_all_configs_parse():
    for f in ["promptfooconfig.security.yaml", "promptfooconfig.redteam.yaml"]:
        assert isinstance(_load(f), dict), f"{f} parse 失败"


def test_redteam_has_coding_agent_core_and_mcp():
    """门禁 config 用 coding-agent:core(5 件),probe 数有余量不超时;
    全 13 件(all)在 redteam-full config,独立 workflow 手动跑。"""
    cfg = _load("promptfooconfig.redteam.yaml")
    ids = [p["id"] for p in cfg["redteam"]["plugins"]]
    assert "coding-agent:core" in ids, "缺 coding-agent:core(门禁版)"
    assert "coding-agent:all" not in ids, "门禁 config 不应含 all(165 probe 超时风险)"
    assert "mcp" in ids, "缺 mcp(cc-harness 用 MCP)"
