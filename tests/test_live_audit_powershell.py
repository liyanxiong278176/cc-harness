import codecs
import json
import os
import subprocess

import pytest

from cc_harness.native_tools import edit_tool, read_tool, write_tool


@pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell 5 script decoding')
@pytest.mark.asyncio
async def test_created_chinese_powershell_script_executes_and_edits_preserve_encoding(tmp_path):
    script = tmp_path / 'audit.ps1'
    created = await write_tool({'path': 'audit.ps1', 'content': "Write-Output '修改用户状态'\n", 'mode': 'create_only'}, cwd=str(tmp_path))
    assert not created.is_error
    assert script.read_bytes().startswith(codecs.BOM_UTF8)
    snapshot = json.loads((await read_tool({'path': 'audit.ps1'}, cwd=str(tmp_path))).llm_text)
    edited = await edit_tool({'path': 'audit.ps1', 'old_text': '修改用户状态', 'new_text': '角色权限已更新',
                             'expected_hash': snapshot['content_hash']}, cwd=str(tmp_path))
    assert not edited.is_error
    assert script.read_bytes().startswith(codecs.BOM_UTF8)
    command = "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); & '" + str(script).replace("'", "''") + "'"
    result = subprocess.run(['C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                            capture_output=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.decode('utf-8').strip() == '角色权限已更新'
