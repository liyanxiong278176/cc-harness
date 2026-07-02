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
    # llm-rubric threshold 不变(确定性断言 not-contains-any/javascript 在前,
    # assert[0] 不再是 rubric;红队覆盖增强改了顺序,按 type 找 rubric)
    rubric = next(a for a in cfg["defaultTest"]["assert"] if a.get("type") == "llm-rubric")
    assert rubric["threshold"] == 0.7


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


def test_comment_js_does_not_duplicate_classification():
    """The comment job's inline JS must NOT re-implement severity/classification.
    That logic lives ONLY in eval/promptfoo/tools/report_to_md.py (spec:
    no JS/Python classification split). The JS reads the Python-generated
    pr-comment.md and posts it.

    Replaces the old test_severity_default_map_matches_python, which guarded
    drift between a JS SEVERITY_DEFAULT map and the Python one. With the JS
    map removed (Task 11), classification is single-sourced in Python and
    drift is impossible — this test now guards that the JS stays classification-free.
    """
    js_text = open(".github/workflows/redteam.yml", encoding="utf-8").read()
    for forbidden in ("SEVERITY_DEFAULT", "getSeverity", "const classify", "failedSection"):
        assert forbidden not in js_text, (
            f"comment job JS re-introduced {forbidden!r} — classification must "
            f"live only in report_to_md.py, not in JS (spec: no JS/Python split)"
        )


def test_comment_js_reads_pr_comment_md():
    """The comment job JS must read the Python-generated pr-comment.md and
    post it — not build the body inline. Body formatting is owned by
    report_to_md.generate_pr_comment, so YAML indentation can no longer leak
    into the rendered comment (the bug the old array-join test guarded).

    Replaces test_pr_comment_body_no_leading_whitespace_in_strings: with the
    body now Python-owned, the leading-whitespace risk is gone; this test
    guards that the JS actually delegates to the Python output.
    """
    js_text = open(".github/workflows/redteam.yml", encoding="utf-8").read()
    assert "readFileSync('pr-comment.md'" in js_text, (
        "JS must readFileSync('pr-comment.md') — the body is Python-owned"
    )
