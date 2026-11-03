"""Keep evaluation recovery and writable candidate artifacts independent of verdicts."""
import json
import subprocess

import pytest

from eval.observability.catalog import cases, materialize
from eval.observability.fixtures import initial_files
from eval.observability.grader import grade_trajectory, verify_function


def test_goal_confirmation_does_not_count_as_transport_recovery():
    case = next(c for c in cases('golden') if c.task_id == 'recovery.09')
    events = [
        {'sequence': 1, 'event_type': 'RunResumed', 'payload': {}},
        {'sequence': 10, 'event_type': 'ModelInvocationFinished', 'payload': {'status': 'failed'}},
    ]
    result = grade_trajectory(case, events)
    assert result['transport_fault_exercised']
    assert not result['transport_resumed_after_fault']
    events.append({'sequence': 11, 'event_type': 'RunResumed', 'payload': {}})
    assert grade_trajectory(case, events)['transport_resumed_after_fault']


def test_visible_manifest_check_rejects_a_different_inputs_artifact(tmp_path, monkeypatch):
    case = next(c for c in cases('golden') if c.task_id == 'outcome.10')
    source = initial_files(case)['test_visible.py']
    namespace = {}
    exec(compile(source, 'test_visible.py', 'exec'), namespace)
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'result.json').write_text('[]')
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'schema': 'export.v1', 'entries': [{'code': 'OTHER', 'units': 1}]}))
    with pytest.raises(AssertionError):
        namespace['test_manifest_matches_delivered_result']()
    manifest.write_text(json.dumps({'schema': 'export.v1', 'entries': []}))
    namespace['test_manifest_matches_delivered_result']()


def test_independent_oracle_accepts_module_relative_artifacts(tmp_path):
    try:
        probe = subprocess.run(['docker', 'info', '--format', '{{.ServerVersion}}'], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip('Docker daemon unavailable; independent oracle not run')
    if probe.returncode:
        pytest.skip('Docker daemon unavailable; independent oracle not run')
    case = next(c for c in cases('golden') if c.task_id == 'outcome.10')
    materialize(case, tmp_path)
    solution = tmp_path / 'solution.py'
    solution.write_text("import json\nfrom pathlib import Path\ndef transform(data):\n    entries = sorted([{'code': row['code'].strip().upper(), 'units': int(row['units'])} for row in data], key=lambda row: row['code'])\n    Path(__file__).with_name('manifest.json').write_text(json.dumps({'schema':'export.v1','entries':entries}))\n    return entries\n")
    before = solution.read_bytes()
    result = verify_function(case, tmp_path)
    assert result['valid'] and result['passed'], result
    assert solution.read_bytes() == before
    assert not (tmp_path / 'manifest.json').exists()
