"""Verify promptfooconfig.security.yaml supports strategies + list form."""


def test_security_config_yaml_is_valid():
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load(
        Path("eval/promptfoo/promptfooconfig.security.yaml").read_text(encoding="utf-8")
    )
    # List form tests
    assert isinstance(cfg["tests"], list)
    assert len(cfg["tests"]) == 2
    # strategies: jailbreak
    assert "strategies" in cfg
    assert any(s.get("id") == "jailbreak" for s in cfg["strategies"])
    # threshold unchanged
    assert cfg["defaultTest"]["assert"][0]["threshold"] == 0.7


def test_security_config_yaml_includes_both_test_sources():
    """Ensure the config wires both attacks.yaml (static) and dynamic_attacks.yaml."""
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load(
        Path("eval/promptfoo/promptfooconfig.security.yaml").read_text(encoding="utf-8")
    )
    tests = cfg["tests"]
    assert any("attacks.yaml" in t for t in tests), f"missing attacks.yaml: {tests}"
    assert any("dynamic_attacks.yaml" in t for t in tests), f"missing dynamic_attacks.yaml: {tests}"
