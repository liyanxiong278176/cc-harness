"""promptfoo 配置 YAML 语法 + 关键断言存在性验证(不跑 promptfoo)。"""
from pathlib import Path
import yaml

EVAL = Path(__file__).resolve().parents[1] / "eval" / "promptfoo"


def _load(name):
    return yaml.safe_load((EVAL / name).read_text(encoding="utf-8"))


def test_unified_yaml_has_deterministic_asserts():
    cfg = _load("promptfooconfig.unified.yaml")
    asserts = cfg["defaultTest"]["assert"]
    types = [a["type"] for a in asserts]
    assert "not-contains-any" in types, "缺密钥确定性断言"
    assert "javascript" in types, "缺 PII 确定性断言"
    assert "llm-rubric" in types  # rubric 兜底仍在


# The four legacy configs were retired on 2026-07-06; existence-only tests are omitted.


def test_unified_allow_provider_uses_allow_mode():
    """The unified config keeps allow-mode sandbox cases alongside deny tests."""
    cfg = _load("promptfooconfig.unified.yaml")
    allow = next(p for p in cfg["providers"] if p.get("label") == "cc-harness-allow")
    assert allow["config"].get("confirm") == "allow", "allow provider 必须 confirm: allow"

    default_types = [a["type"] for a in cfg["defaultTest"]["assert"]]
    assert "not-contains-any" in default_types, "allow 模式仍需密钥确定性断言"

    sandbox_tests = [
        t for t in cfg["tests"]
        if isinstance(t, dict) and t.get("providers") == ["cc-harness-allow"]
    ]
    assert sandbox_tests, "unified config 缺 allow 模式执行类样本"
    assert any(
        any(
            a.get("type") == "llm-rubric"
            and "sandbox_isolation" in str(a.get("value"))
            for a in t.get("assert", [])
        )
        for t in sandbox_tests
    ), "allow 模式应用 sandbox_isolation judge"
    assert any(t.get("metadata", {}).get("pluginId") for t in sandbox_tests), (
        "allow 模式必须带执行类 pluginId"
    )


# The retired gate-only config had coding-agent:core; unified uses the full plugin set.


def test_redteam_full_has_coding_agent_all_and_mcp():
    """Unified redteam coverage uses the full coding-agent plugin plus MCP."""
    cfg = _load("promptfooconfig.unified.yaml")
    ids = [p["id"] for p in cfg["redteam"]["plugins"]]
    assert "coding-agent:all" in ids, "缺 coding-agent:all(全 13 件深度扫描)"
    assert "mcp" in ids, "缺 mcp(cc-harness 用 MCP)"
    assert "coding-agent:core" not in ids, "unified config 应用 all 不是 core"
