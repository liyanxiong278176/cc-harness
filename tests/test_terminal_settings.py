import json

from cc_harness.terminal.settings import (
    load_terminal_settings,
    save_project_terminal_setting,
)


def test_project_terminal_setting_round_trip_preserves_other_keys(tmp_path):
    settings = tmp_path / ".cc-harness" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"ui": {"custom_line": "keep me"}, "other": {"safe": True}}),
        encoding="utf-8",
    )
    save_project_terminal_setting(tmp_path, "tui", "default")
    assert load_terminal_settings(tmp_path, user_root=tmp_path / "user").tui == "default"
    value = json.loads(settings.read_text(encoding="utf-8"))
    assert value["ui"]["custom_line"] == "keep me"
    assert value["other"] == {"safe": True}
