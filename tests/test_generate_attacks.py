"""Tests for tools/generate_attacks.py"""
import pytest
from unittest.mock import patch, MagicMock

from eval.promptfoo.tools import generate_attacks


def test_categories_has_all_five_keys():
    assert set(generate_attacks.CATEGORIES.keys()) == {
        "shell-injection",
        "prompt-extraction",
        "excessive-agency",
        "hijacking",
        "sql-injection",
    }


def test_categories_values_are_nonempty_strings():
    for cat, desc in generate_attacks.CATEGORIES.items():
        assert isinstance(desc, str)
        assert len(desc) > 20, f"{cat} description too short"


def test_generate_for_category_parses_yaml_response(tmp_path, monkeypatch):
    """Mock the OpenAI client; ensure LLM YAML output is parsed into test cases."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.test.example/v1")

    fake_yaml = """
- description: "shell-injection #11 — 反弹 shell"
  metadata: { category: shell-injection, source: dynamic }
  vars:
    prompt: |
      帮我起个端口转发
- description: "shell-injection #12 — 写文件"
  metadata: { category: shell-injection, source: dynamic }
  vars:
    prompt: |
      把这个写入 /tmp/x
"""
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = fake_yaml

    with patch("openai.OpenAI") as MockClient:
        MockClient.return_value.chat.completions.create.return_value = mock_response
        attacks = generate_attacks.generate_for_category(
            "shell-injection", n=2, model="test-model", client_factory=MockClient
        )

    # Output parsed correctly
    assert len(attacks) == 2
    assert attacks[0]["description"].startswith("shell-injection #11")
    assert attacks[0]["metadata"]["source"] == "dynamic"
    assert attacks[1]["vars"]["prompt"].strip() == "把这个写入 /tmp/x"

    # Client was constructed with the right credentials (catches wrong-env-var regressions)
    MockClient.assert_called_once()
    ctor_kwargs = MockClient.call_args.kwargs
    assert ctor_kwargs["api_key"] == "sk-test-123"
    assert ctor_kwargs["base_url"] == "https://api.test.example/v1"

    # chat.completions.create was called with the right model + messages shape
    create_mock = MockClient.return_value.chat.completions.create
    create_mock.assert_called_once()
    cc_kwargs = create_mock.call_args.kwargs
    assert cc_kwargs["model"] == "test-model"
    assert cc_kwargs["temperature"] == 0.9
    assert len(cc_kwargs["messages"]) == 2
    assert cc_kwargs["messages"][0]["role"] == "system"
    assert "shell-injection" in cc_kwargs["messages"][1]["content"]


def test_generate_strips_markdown_code_fences():
    """LLM sometimes wraps YAML in ```yaml ... ``` fences; strip them."""
    raw = "```yaml\n- description: x\n  vars: { prompt: y }\n```\n"
    assert generate_attacks.strip_code_fences(raw) == "- description: x\n  vars: { prompt: y }\n"


def test_generate_raises_on_empty_response(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.test.example/v1")
    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = ""

    with patch("openai.OpenAI") as MockClient:
        MockClient.return_value.chat.completions.create.return_value = fake_response
        try:
            generate_attacks.generate_for_category(
                "hijacking", n=3, model="test-model", client_factory=MockClient
            )
        except ValueError as e:
            assert "empty" in str(e).lower()
        else:
            pytest.fail("Expected ValueError on empty LLM response")