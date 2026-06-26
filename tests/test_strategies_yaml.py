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


def test_severity_default_map_matches_python():
    """The JS SEVERITY_DEFAULT map in redteam.yml must be in sync with
    eval.promptfoo.tools.generate_attacks.CATEGORY_DEFAULT_SEVERITY.

    Both maps are kept as source of truth in different places (Python for
    generation, JS for PR comment rendering). Drift between them causes
    the PR comment to display wrong severity emojis for categories that
    don't have explicit metadata.severity.
    """
    import re
    from eval.promptfoo.tools import generate_attacks

    py_map = generate_attacks.CATEGORY_DEFAULT_SEVERITY
    js_text = open(".github/workflows/redteam.yml", encoding="utf-8").read()
    js_match = re.search(r"const SEVERITY_DEFAULT = \{([^}]+)\}", js_text, re.DOTALL)
    assert js_match, "SEVERITY_DEFAULT map not found in redteam.yml"
    js_dict = {}
    for m in re.finditer(r'"([\w-]+)":\s*"([\w-]+)"', js_match.group(1)):
        js_dict[m.group(1)] = m.group(2)

    # JS map must include all keys from Python map (JS may have fewer if Python
    # added categories the JS hasn't been updated for — that's the drift we catch)
    missing = set(py_map.keys()) - set(js_dict.keys())
    assert not missing, f"JS map missing keys present in Python: {missing}"

    # And all values must match
    for cat, sev in py_map.items():
        assert js_dict.get(cat) == sev, (
            f"JS map severity mismatch for {cat}: "
            f"Python={sev}, JS={js_dict.get(cat)}"
        )


def test_pr_comment_body_no_leading_whitespace_in_strings():
    """Every line of the rendered PR comment body must start at column 0.

    Bug guard: the body used to be a single template literal indented 12
    spaces inside the YAML 'script: |' block, which leaked 12 leading
    spaces into the rendered comment (4+ spaces = code block per GFM).
    Fixed by using array-and-join; verify the rendered output has no
    leading whitespace on any body line.
    """
    import re
    js_text = open(".github/workflows/redteam.yml", encoding="utf-8").read()

    # Find the body construction: match the array form
    # We extract everything between `const body = [` and `].join('\n');`
    body_match = re.search(
        r"const body = \[(.*?)\]\.join\('\\n'\)",
        js_text,
        re.DOTALL,
    )
    assert body_match, (
        "PR comment body must use array.join form (no template literal that "
        "would leak YAML indentation into the rendered comment)"
    )

    # Each non-empty string literal in the array must start at column 0
    # (i.e., no leading whitespace inside backticks or quotes)
    string_literals = re.findall(r"[`]([^`]+)[`]", body_match.group(1))
    assert string_literals, "no string literals found in body array"
    for s in string_literals:
        assert not s.startswith(" ") and not s.startswith("\t"), (
            f"string literal has leading whitespace (would render as code "
            f"block in markdown): {s[:40]!r}"
        )
