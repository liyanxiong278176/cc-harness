import os
import json
from types import SimpleNamespace

import pytest

from cc_harness.command_failures import nonblocking_command_failure
from cc_harness.executor import NativeExecutor
from cc_harness.run_model import ActionStatus
from cc_harness.worker import RunWorker


def receipt(command, **metadata):
    return {'metadata': {'shell_dialect': 'powershell', 'request_arguments': {'command': command},
                         'workspace_change_set': {'changed_paths': []}, **metadata}, 'content': []}


def test_unclosed_quote_is_nonblocking_only_with_native_powershell_proof():
    value = receipt('cd workspace; python -c "import json; assert True')
    assert nonblocking_command_failure(value)
    value['metadata']['shell_dialect'] = 'posix'
    assert not nonblocking_command_failure(value)
    value['metadata'].pop('shell_dialect')
    assert not nonblocking_command_failure(value)
    assert not nonblocking_command_failure(receipt('python -c "assert False"', stderr='AssertionError'))


@pytest.mark.parametrize('command', [
    "Write-Output `\"", 'Write-Output $(Get-Item "x)', "@'\ntext", "Write-Output x # '",
])
def test_complex_shell_syntax_is_not_guessed(command):
    assert not nonblocking_command_failure(receipt(command))


def test_nonrepository_readonly_probe_cannot_deadlock_completion(tmp_path, monkeypatch):
    for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR'):
        monkeypatch.delenv(key, raising=False)
    command = 'Get-Content .fixture/state.json; Write-Output "---git---"; git -C . status --short 2>$null'
    assert nonblocking_command_failure(receipt(command), root=tmp_path)
    assert not nonblocking_command_failure(receipt('Remove-Item result.json; ' + command), root=tmp_path)
    assert not nonblocking_command_failure(receipt(command, workspace_change_set={'changed_paths': ['result.json']}), root=tmp_path)
    (tmp_path / '.git').mkdir()
    assert not nonblocking_command_failure(receipt(command), root=tmp_path)


def test_worker_keeps_real_failed_verifiers_blocking(tmp_path):
    artifacts = {
        'parse': json.dumps(receipt('python -c "assert')),
        'test': json.dumps(receipt('python -c "assert False"', stderr='AssertionError')),
    }
    worker = SimpleNamespace(working_directory=tmp_path,
                             store=SimpleNamespace(artifacts=SimpleNamespace(read_text=artifacts.__getitem__)))
    projection = SimpleNamespace(
        actions=[SimpleNamespace(action_id=key, attempt=1, status=ActionStatus.FAILED, tool_name='run_command')
                 for key in artifacts],
        observations=[SimpleNamespace(action_id=key, attempt=1, status='failed', observation_artifact=key)
                      for key in artifacts],
        working_state=SimpleNamespace(unresolved_errors=[{'action_id': key} for key in artifacts]),
    )
    assert RunWorker._blocking_unresolved_errors(worker, projection) == ({'action_id': 'test'},)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != 'nt', reason='native PowerShell receipt')
async def test_failed_native_command_records_its_actual_shell(tmp_path):
    executor = NativeExecutor(tmp_path)
    command = 'python -c "assert'
    result = await executor.run({'command': command}, cwd=tmp_path)
    assert result.is_error
    assert result.metadata['shell_dialect'] == executor.shell_profile.dialect
    assert nonblocking_command_failure({'metadata': {**result.metadata, 'request_arguments': {'command': command}}, 'content': []})
