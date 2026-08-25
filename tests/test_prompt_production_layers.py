from pathlib import Path

from cc_harness.prompt_rules import production_rule_audit_records, production_rule_metadata
from cc_harness.project_instructions import load_project_instructions
from cc_harness.prompts import PromptComposer, build_prompt_manifest


def test_production_prompt_has_pinned_rules_but_safe_manifest_has_no_text():
    prompt = PromptComposer(mode="coding", ctx={"cwd": "/tmp"}).render()
    manifest = build_prompt_manifest("/tmp")
    assert "审计后的生产规则" in prompt
    metadata = manifest.public_metadata()
    assert metadata["rules_version"] == "rules-v1"
    assert metadata["rules_count"] == production_rule_metadata()["rule_count"]
    assert "prompt" not in " ".join(str(value) for value in metadata.values()).lower()
    assert all("text" not in record for record in production_rule_audit_records())


def test_project_instruction_layer_is_bounded_and_digest_only_for_metadata(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("Use the project test command.\n", encoding="utf-8")
    layer = load_project_instructions(tmp_path)
    assert layer is not None
    assert "Use the project test command." in layer.text
    assert layer.public_metadata()["digest"] == layer.digest
    rendered = PromptComposer(
        mode="coding",
        ctx={"cwd": str(tmp_path), "project_instructions": layer.text},
    ).render()
    assert "<project_instructions>" in rendered
    assert "Use the project test command." in rendered


def test_project_template_is_not_injected(tmp_path: Path):
    from cc_harness.project_instructions import initialize_project_instructions

    initialize_project_instructions(tmp_path)
    assert load_project_instructions(tmp_path) is None
