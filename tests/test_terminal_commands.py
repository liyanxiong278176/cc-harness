from cc_harness.terminal.commands import COMMAND_MAP, COMMANDS


def test_help_completion_and_dispatch_share_one_wired_registry():
    expected = {
        "/help", "/init", "/release-notes", "/status", "/clear", "/resume",
        "/branch", "/rename", "/exit",
        "/coding", "/plan", "/design", "/chat", "/mode",
        "/model", "/effort", "/permissions", "/verbose",
        "/context", "/compact", "/tools", "/mcp",
        "/rewind", "/focus", "/diff", "/tasks", "/agents", "/tui",
        "/usage", "/inspector",
    }
    assert {command.name for command in COMMANDS} == expected
    assert set(COMMAND_MAP) == expected
    assert "/team" not in COMMAND_MAP
    assert "/snapshot" not in COMMAND_MAP
