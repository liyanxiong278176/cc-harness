"""Bounded local reproduction of foreground shell/background child pipe behavior."""
import asyncio
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import cc_harness.executor as executor


async def main():
    original = executor._collect_process_output

    async def inspect(proc, *, activity):
        task = asyncio.create_task(original(proc, activity=activity))
        await asyncio.sleep(0.5)
        print(json.dumps({'shell_returncode_after_500ms': proc.returncode,
                          'stdout_bytes': activity.get('stdout_bytes'),
                          'pipes_complete': task.done()}))
        return await task

    executor._collect_process_output = inspect
    with TemporaryDirectory(prefix='cc-admin-pipe-probe-') as directory:
        root = Path(directory)
        (root / 'child.py').write_text('import time\ntime.sleep(2)\n', encoding='utf-8')
        native = executor.NativeExecutor(root)
        if '--legacy' in sys.argv:
            native.shell_profile = executor.ShellProfile(
                name='Windows PowerShell', dialect='powershell', platform='Windows',
                executable='C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe',
                fallback_encodings=('utf-8', 'gbk'),
            )
        command = f"$p=Start-Process -FilePath '{sys.executable}' -ArgumentList 'child.py' -WindowStyle Hidden -RedirectStandardOutput 'out.log' -RedirectStandardError 'err.log' -PassThru; Write-Output $p.Id"
        result = await native.run({'command': command, 'timeout_seconds': 5}, cwd=root)
        print(json.dumps({'error': result.is_error, 'result': result.llm_text,
                          'metadata': result.metadata}, default=str))
        await asyncio.sleep(2)  # Let the bounded child release its redirected log files.


if __name__ == '__main__':
    asyncio.run(main())
