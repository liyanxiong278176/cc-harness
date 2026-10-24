import asyncio
import sys
from types import SimpleNamespace

import pytest

from cc_harness.executor import NativeExecutor


@pytest.mark.asyncio
@pytest.mark.parametrize('exit_code', [0, 7])
async def test_shell_exit_with_inherited_pipes_is_not_a_command_timeout(tmp_path, exit_code):
    parent = tmp_path / 'parent.py'
    parent.write_text(
        'import subprocess,sys\n'
        'subprocess.Popen([sys.executable,"-c","import time; time.sleep(3)"])\n'
        f'print("startup-receipt",flush=True)\nsys.exit({exit_code})\n', encoding='utf-8',
    )
    executor = NativeExecutor(tmp_path)
    executor.shell_profile = SimpleNamespace(argv=lambda command: [sys.executable, str(parent)], fallback_encodings=())
    result = await executor.run({'command': 'bounded inherited-pipe reproduction', 'timeout_seconds': 2}, cwd=tmp_path)
    assert result.is_error == bool(exit_code)
    assert result.metadata['exit_code'] == exit_code
    assert result.metadata['timed_out'] is False
    assert result.metadata['output_complete'] is False
    assert 'startup-receipt' in result.metadata['stdout']
    assert 'child readiness is not verified' in result.llm_text
    # The bounded child ends naturally; the collector did not kill it.
    await asyncio.sleep(2)


@pytest.mark.asyncio
async def test_real_timeout_keeps_output_written_before_a_child_holds_the_pipe(tmp_path):
    parent = tmp_path / 'parent.py'
    parent.write_text(
        'import subprocess,sys,time\n'
        'print("before-timeout",flush=True)\n'
        'time.sleep(10)\n', encoding='utf-8',
    )
    executor = NativeExecutor(tmp_path)
    executor.shell_profile = SimpleNamespace(argv=lambda command: [sys.executable, str(parent)], fallback_encodings=())
    result = await executor.run({'command': 'bounded timeout reproduction', 'timeout_seconds': 0.5}, cwd=tmp_path)
    assert result.is_error
    assert result.metadata['timed_out'] is True
    assert 'before-timeout' in result.metadata['stdout']
