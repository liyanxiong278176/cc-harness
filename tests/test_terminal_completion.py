from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import to_plain_text

from cc_harness.terminal.commands import COMMANDS
from cc_harness.terminal.completion import TerminalCompleter


def test_slash_completion_lists_commands_with_localized_description(tmp_path):
    completer = TerminalCompleter(tmp_path, lang="zh-CN")

    completions = list(completer.get_completions(Document("/sta"), None))

    assert [item.text for item in completions] == ["/status"]
    assert to_plain_text(completions[0].display_meta) == "显示会话状态"


def test_slash_completion_at_root_exposes_every_command(tmp_path):
    completer = TerminalCompleter(tmp_path, lang="en")

    completions = list(completer.get_completions(Document("/"), None))

    assert [item.text for item in completions] == [command.name for command in COMMANDS]
    assert to_plain_text(completions[0].display_meta) == "Show commands"
