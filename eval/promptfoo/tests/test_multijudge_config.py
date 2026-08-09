"""config 含双 grader(cloud + MiMo)。"""
import yaml
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / "promptfooconfig.unified.yaml"


def test_config_has_two_graders():
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    asserts = (cfg.get("defaultTest") or {}).get("assert") or []
    rubrics = [a for a in asserts if a.get("type") == "llm-rubric"]
    # 主 + 第二源 至少 2 个 llm-rubric
    assert len(rubrics) >= 2
    providers = [
        str(r.get("provider") or r.get("vars", {}).get("judge_provider", ""))
        for r in rubrics
    ]
    assert all("deepseek-v4-flash" in provider for provider in providers)
    # 至少区分出两个 judge 源
    assert len(providers) >= 2
