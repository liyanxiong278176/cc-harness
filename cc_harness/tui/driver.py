"""Compatibility bridge to the inline terminal session.

New code should import :mod:`cc_harness.terminal` or use the installed
``cc-harness`` command. This module intentionally contains no Textual code.
"""
from pathlib import Path


async def run_tui(*, cwd: str, mode: str = "coding") -> None:
    from cc_harness.runtime import SessionRuntime
    from cc_harness.terminal.app import InlineTerminalApp

    runtime = await SessionRuntime.create(Path(cwd), mode=mode)
    await InlineTerminalApp(runtime).run()
