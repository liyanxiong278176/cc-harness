"""Conservative classification of diagnostic failures with no unresolved effect."""
from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any, Mapping


def _unclosed_powershell_quote(command: str) -> bool:
    # These constructs require the full PowerShell parser. Remain blocking
    # rather than guessing about strings, interpolation or comments.
    if any(token in command for token in ('`', '$(', "@'", '@"', '#', '‘', '’', '“', '”')):
        return False
    quote = None
    for char in command:
        if quote is None and char in "\"'":
            quote = char
        elif char == quote:
            quote = None
    return quote is not None


def _readonly_git_probe(command: str, root: Path | None, metadata: Mapping[str, Any]) -> bool:
    if root is None or any(os.environ.get(key) for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR')):
        return False
    root = root.resolve()
    if any((parent / '.git').exists() for parent in (root, *root.parents)):
        return False  # Repository configuration can install executable hooks.
    changes = metadata.get('workspace_change_set')
    if not isinstance(changes, Mapping) or changes.get('changed_paths') != []:
        return False
    command = re.sub(r'\s+2>\$null\s*$', '', command, flags=re.I)
    if any(char in command for char in '`$(){}|&<>\n\r'):
        return False
    saw_git = False
    for part in command.split(';'):
        part = part.strip()
        if re.fullmatch(r'git(?:\s+-C\s+[\w./\\-]+)?\s+status(?:\s+--(?:short|porcelain(?:=v[12])?))?', part):
            saw_git = True
        elif re.fullmatch(r'Get-Content\s+[\w./\\-]+(?:\s+-Raw)?', part, re.I):
            pass
        elif re.fullmatch(r'''Write-Output\s+(?:"[^"']*"|'[^"']*')''', part, re.I):
            pass
        else:
            return False
    return saw_git


def nonblocking_command_failure(observation: Mapping[str, Any], *, root: Path | None = None) -> bool:
    metadata = observation.get('metadata')
    metadata = metadata if isinstance(metadata, Mapping) else {}
    if metadata.get('execution_not_started'):
        return True
    blocks = observation.get('content')
    text = ' '.join(str(block.get('text') or '') for block in (blocks if isinstance(blocks, list) else ())
                    if isinstance(block, Mapping)) + ' ' + str(metadata.get('stderr') or '')
    lowered = text.casefold()
    if 'parsererror:' in lowered:
        return True
    if 'syntaxerror:' in lowered and 'file "<string>"' in lowered:
        return True
    arguments = metadata.get('request_arguments')
    command = str(arguments.get('command') or '') if isinstance(arguments, Mapping) else ''
    if metadata.get('shell_dialect') == 'powershell':
        if not str(metadata.get('stdout') or '').strip() and _unclosed_powershell_quote(command):
            return True  # PowerShell parses the entire statement before executing it.
        if _readonly_git_probe(command, root, metadata):
            return True
    if any(marker in lowered for marker in (
        'commandnotfoundexception', 'is not recognized as the name of a cmdlet',
        'not recognized as an internal or external command',
        'a parameter cannot be found that matches parameter name',
    )):
        return not str(metadata.get('stdout') or '').strip() and not any(
            separator in command for separator in ('\n', ';', '&&', '||', '|')
        )
    return False
