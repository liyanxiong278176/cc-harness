from __future__ import annotations

from pathlib import Path

from eval.cc_only.adapters.memory import (
    _locomo_answer_prompt,
    _memory_env,
)


def test_category_guidance_matches_pinned_taxonomy() -> None:
    prompt = _locomo_answer_prompt("When did the project start?", category="2")
    assert "Category 2 (temporal)" in prompt
    assert "Normalize equivalent date forms" in prompt
    assert "FINAL_ANSWER:" in prompt

    inference = _locomo_answer_prompt("What does this suggest?", category="3")
    assert "evidence-grounded inference" in inference
    assert "unsupported world knowledge" in inference


def test_category_env_enables_bounded_retrieval_modes(tmp_path: Path) -> None:
    temporal = _memory_env(tmp_path, locomo_category="2")
    assert temporal["MEMORY_LOCOMO_CATEGORY"] == "2"
    assert temporal["MEMORY_RETRIEVAL_MODE"] == "temporal"
    assert temporal["MEMORY_RECALL_TOP_K"] == "16"

    direct = _memory_env(tmp_path, locomo_category="1")
    assert "MEMORY_RETRIEVAL_MODE" not in direct
    assert direct["MEMORY_RECALL_TOP_K"] == "12"
