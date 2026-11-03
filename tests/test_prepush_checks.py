from pathlib import Path
from types import SimpleNamespace
import subprocess

from eval.observability import prepush
from scripts.install_observability_hooks import install


def test_default_hook_migrates_without_chaining_the_paid_hook(tmp_path):
    subprocess.run(['git', 'init', str(tmp_path)], check=True, capture_output=True)
    hook = tmp_path / '.git/hooks/pre-push'
    hook.write_text('#!/bin/sh\n# cc-harness-observability hook v1\nexec python -m eval.observability.gates full\n')
    install(tmp_path)
    assert 'eval.observability.prepush' in hook.read_text()
    assert 'gates full' not in hook.read_text()
    assert 'git rev-parse --show-toplevel' in hook.read_text()
    assert tmp_path.as_posix() not in hook.read_text()
    assert not hook.with_name('pre-push.cc-harness-previous').exists()


def test_foreign_hook_is_preserved_and_paid_checks_require_opt_in(tmp_path):
    subprocess.run(['git', 'init', str(tmp_path)], check=True, capture_output=True)
    hook = tmp_path / '.git/hooks/pre-push'
    original = '#!/bin/sh\nexit 0\n'
    hook.write_text(original)
    install(tmp_path, agent_eval_on_push=True)
    assert hook.with_name('pre-push.cc-harness-previous').read_text() == original
    assert 'gates full' in hook.read_text()


def test_local_checks_disable_paid_services_and_fail_closed(monkeypatch, tmp_path):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(prepush.subprocess, 'run', run)
    assert prepush.main(tmp_path) == 0
    assert len(calls) == 2
    for command, kwargs in calls:
        assert kwargs['env']['CC_HARNESS_RUN_REAL_LLM'] == '0'
        assert kwargs['env']['CC_HARNESS_OBSERVABILITY_DISABLED'] == '1'
        assert not any('gates' in part for part in command)
    assert all((Path(__file__).resolve().parents[1] / path).exists() for path in prepush.TESTS)
    calls.clear()
    monkeypatch.setattr(prepush.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=7))
    assert prepush.main(tmp_path) == 7
