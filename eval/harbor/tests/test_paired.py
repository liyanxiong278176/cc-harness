from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from eval.harbor.paired import (
    CLAUDE_CODE_VERSION,
    HARBOR_VERSION,
    SWEBENCH_DATASET,
    _job_exception_is_transient,
    _load_or_initialize_state,
    _materialize_normalized_bundle,
    _prepare_attempt_jobs_dir,
    build_harbor_command,
    run_harbor_parity,
)
from eval.launch import HarnessKind
from eval.parity import ParitySuite, build_balanced_schedule


def test_builds_pinned_shell_free_candidate_command(tmp_path: Path) -> None:
    command = build_harbor_command(
        uvx="uvx",
        project_root=tmp_path,
        dataset="swe-bench/swe-bench-verified",
        task_name="swe-bench/example__repo-1",
        harness=HarnessKind.CC_HARNESS,
        wheel_path=tmp_path / "cc_harness-0.1.0-py3-none-any.whl",
        env_file=tmp_path / ".env",
        jobs_dir=tmp_path / "jobs",
    )

    assert command[:4] == ["uvx", "--from", f"harbor=={HARBOR_VERSION}", "harbor"]
    assert "harbor_plugins.cc_harness_agent:CCHarnessHarborAgent" in command
    assert "deepseek-v4-flash" in command
    assert SWEBENCH_DATASET.startswith("swe-bench/swe-bench-verified@sha256:")
    assert "--n-attempts" in command
    assert "1" == command[command.index("--n-attempts") + 1]


def test_builds_version_pinned_claude_command(tmp_path: Path) -> None:
    command = build_harbor_command(
        uvx="uvx",
        project_root=tmp_path,
        dataset="swe-bench/swe-bench-verified",
        task_name="swe-bench/example__repo-1",
        harness=HarnessKind.CLAUDE_CODE,
        wheel_path=tmp_path / "unused.whl",
        env_file=tmp_path / ".env",
        jobs_dir=tmp_path / "jobs",
    )

    assert "claude-code" in command
    assert f"version={CLAUDE_CODE_VERSION}" in command
    assert not any("secret" in argument.lower() for argument in command)


def test_state_is_resumable_only_with_identical_config(tmp_path: Path) -> None:
    schedule = build_balanced_schedule(("task-one",), repetitions=1, random_seed=7)
    state_path = tmp_path / "state.json"
    schedule_path = tmp_path / "schedule.json"
    config = {"schema_version": "test", "seed": 7}

    first = _load_or_initialize_state(tmp_path, state_path, schedule_path, config, schedule)
    second = _load_or_initialize_state(tmp_path, state_path, schedule_path, config, schedule)

    assert first == second
    assert json.loads(schedule_path.read_text(encoding="utf-8"))["random_seed"] == 7
    with pytest.raises(ValueError, match="does not match"):
        _load_or_initialize_state(
            tmp_path,
            state_path,
            schedule_path,
            {"schema_version": "test", "seed": 8},
            schedule,
        )


def test_archives_unrecorded_interrupted_attempt(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "attempt-1"
    partial = jobs_dir / "partial-job" / "trial" / "agent.log"
    partial.parent.mkdir(parents=True)
    partial.write_text("retained interruption evidence", encoding="utf-8")

    archived = _prepare_attempt_jobs_dir(jobs_dir)

    assert archived is not None
    assert archived.name.startswith("attempt-1-interrupted-")
    assert (archived / "partial-job" / "trial" / "agent.log").read_text(
        encoding="utf-8"
    ) == "retained interruption evidence"
    assert jobs_dir.is_dir()
    assert list(jobs_dir.iterdir()) == []


def test_retries_only_transient_harbor_exceptions(tmp_path: Path) -> None:
    job = tmp_path / "job"
    trial = job / "trial"
    trial.mkdir(parents=True)
    transient = {
        "exception_info": {
            "exception_type": "NonZeroAgentExitCodeError",
            "exception_message": "APIConnectionError: Connection error.",
        }
    }
    (trial / "result.json").write_text(json.dumps(transient), encoding="utf-8")

    assert _job_exception_is_transient(job) is True

    transient["exception_info"] = {
        "exception_type": "AgentTimeoutError",
        "exception_message": "agent exceeded watchdog",
    }
    (trial / "result.json").write_text(json.dumps(transient), encoding="utf-8")
    assert _job_exception_is_transient(job) is False


def test_treats_setup_timeout_and_apt_5xx_log_as_transient(tmp_path: Path) -> None:
    job = tmp_path / "job"
    trial = job / "trial"
    trial.mkdir(parents=True)
    result_path = trial / "result.json"
    result_path.write_text(
        json.dumps(
            {
                "exception_info": {
                    "exception_type": "NonZeroAgentExitCodeError",
                    "exception_message": "Command failed (exit 100): apt-get update",
                }
            }
        ),
        encoding="utf-8",
    )
    (trial / "trial.log").write_text(
        "E: Failed to fetch http://security.ubuntu.com/ubuntu/InRelease 502  Bad Gateway\n",
        encoding="utf-8",
    )

    assert _job_exception_is_transient(job) is True

    result_path.write_text(
        json.dumps(
            {
                "exception_info": {
                    "exception_type": "AgentSetupTimeoutError",
                    "exception_message": "Agent setup timed out after 360.0 seconds",
                }
            }
        ),
        encoding="utf-8",
    )
    (trial / "trial.log").unlink()
    assert _job_exception_is_transient(job) is True


@pytest.mark.parametrize(
    "diagnostic",
    [
        "unexpected status from HEAD request: 421 Misdirected Request",
        "failed to compute cache key: short read: unexpected EOF",
        "failed commit on ref: unexpected commit digest sha256:bad",
    ],
)
def test_treats_transient_docker_image_failures_as_retryable(
    tmp_path: Path, diagnostic: str
) -> None:
    job = tmp_path / "job"
    trial = job / "trial"
    trial.mkdir(parents=True)
    (trial / "result.json").write_text(
        json.dumps(
            {
                "exception_info": {
                    "exception_type": "RuntimeError",
                    "exception_message": (
                        "Docker compose command failed for environment example. "
                        f"failed to solve: {diagnostic}"
                    ),
                }
            }
        ),
        encoding="utf-8",
    )

    assert _job_exception_is_transient(job) is True


def test_does_not_retry_permanent_missing_docker_image(tmp_path: Path) -> None:
    job = tmp_path / "job"
    trial = job / "trial"
    trial.mkdir(parents=True)
    (trial / "result.json").write_text(
        json.dumps(
            {
                "exception_info": {
                    "exception_type": "RuntimeError",
                    "exception_message": (
                        "Docker compose command failed for environment example. "
                        "failed to solve: manifest unknown: manifest unknown"
                    ),
                }
            }
        ),
        encoding="utf-8",
    )

    assert _job_exception_is_transient(job) is False


def test_resume_retries_selected_transient_job_and_rebuilds_projections(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    output = project / "evidence"
    wheel = project / "cc_harness-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    env_file = project / ".env"
    env_file.write_text("OPENAI_API_KEY=test\n", encoding="utf-8")
    settings = project / "claude-settings.json"
    settings.write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_AUTH_TOKEN": "test",
                    "ANTHROPIC_BASE_URL": "https://example.invalid",
                }
            }
        ),
        encoding="utf-8",
    )
    catalog = project / "verified500.json"
    catalog.write_text('{"catalog":"frozen-v1"}', encoding="utf-8")
    calls: list[tuple[str, str]] = []
    export_calls: list[tuple[tuple[Path, ...], tuple[Path, ...]]] = []
    analysis_calls: list[Path] = []

    def fake_run(command, **_kwargs):
        jobs_dir = Path(command[command.index("--jobs-dir") + 1])
        harness = "claude-code" if "claude-code" in command else "cc-harness"
        calls.append((harness, jobs_dir.name))
        job = jobs_dir / "job"
        trial = job / "trial"
        trial.mkdir(parents=True)
        (job / "result.json").write_text("{}", encoding="utf-8")
        exception = None
        if harness == "claude-code" and jobs_dir.name == "attempt-1":
            exception = {
                "exception_type": "NonZeroAgentExitCodeError",
                "exception_message": "Command failed (exit 100): apt-get update",
            }
            (trial / "trial.log").write_text("502  Bad Gateway\n", encoding="utf-8")
        (trial / "result.json").write_text(
            json.dumps({"exception_info": exception}), encoding="utf-8"
        )
        return SimpleNamespace(returncode=0)

    def fake_export(candidate_jobs, baseline_jobs, output_dir, *, schedule):
        assert schedule.random_seed == 7
        export_calls.append((candidate_jobs, baseline_jobs))
        output_dir.mkdir()
        bundle = output_dir / "bundle.json"
        bundle.write_text("{}", encoding="utf-8")
        return bundle

    def fake_analyze(_bundles, output_dir, *, suite):
        assert suite is ParitySuite.DEV
        analysis_calls.append(output_dir)
        output_dir.mkdir()
        (output_dir / "summary.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr("eval.harbor.paired.shutil.which", lambda _name: "uvx")
    monkeypatch.setattr("eval.harbor.paired.subprocess.run", fake_run)
    monkeypatch.setattr("eval.harbor.paired.export_harbor_jobs", fake_export)
    monkeypatch.setattr("eval.harbor.paired.analyze_imported_parity", fake_analyze)
    original_classifier = _job_exception_is_transient
    monkeypatch.setattr("eval.harbor.paired._job_exception_is_transient", lambda _job: False)

    kwargs = {
        "task_names": ("swe-bench/example__repo-1",),
        "wheel_path": wheel,
        "env_file": env_file,
        "claude_settings_path": settings,
        "repetitions": 1,
        "random_seed": 7,
        "maximum_attempts": 2,
        "cooldown_seconds": 0,
        "suite": ParitySuite.DEV,
        "task_catalog_path": catalog,
    }
    run_harbor_parity(project, output, **kwargs)
    assert (output / "frozen-inputs" / catalog.name).read_bytes() == catalog.read_bytes()
    monkeypatch.setattr("eval.harbor.paired._job_exception_is_transient", original_classifier)

    run_harbor_parity(project, output, **kwargs)

    state = json.loads((output / "state.json").read_text(encoding="utf-8"))
    baseline = next(item for item in state["jobs"].values() if item["harness"] == "claude-code")
    assert calls.count(("cc-harness", "attempt-1")) == 1
    assert calls.count(("claude-code", "attempt-1")) == 1
    assert calls.count(("claude-code", "attempt-2")) == 1
    assert len(baseline["attempts"]) == 2
    assert baseline["attempts"][0]["job_root"] != baseline["attempts"][1]["job_root"]
    assert baseline["selected_job"] == baseline["attempts"][1]["job_root"]
    assert len(export_calls) == 2
    assert len(analysis_calls) == 2
    assert list(output.glob("normalized-superseded-*"))
    assert list(output.glob("analysis-superseded-*"))

    catalog.write_text('{"catalog":"changed"}', encoding="utf-8")
    with pytest.raises(ValueError, match="does not match requested config"):
        run_harbor_parity(project, output, **kwargs)


def test_launcher_failures_are_retained_without_consuming_trial_attempts(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    output = project / "evidence"
    wheel = project / "cc_harness-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    env_file = project / ".env"
    env_file.write_text("OPENAI_API_KEY=test\n", encoding="utf-8")
    settings = project / "claude-settings.json"
    settings.write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_AUTH_TOKEN": "test",
                    "ANTHROPIC_BASE_URL": "https://example.invalid",
                }
            }
        ),
        encoding="utf-8",
    )
    calls = 0

    def fake_run(command, **_kwargs):
        nonlocal calls
        calls += 1
        if calls <= 2:
            return SimpleNamespace(returncode=1)
        jobs_dir = Path(command[command.index("--jobs-dir") + 1])
        job = jobs_dir / "job"
        trial = job / "trial"
        trial.mkdir(parents=True)
        (job / "result.json").write_text("{}", encoding="utf-8")
        (trial / "result.json").write_text(json.dumps({"exception_info": None}), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    def fake_export(_candidate_jobs, _baseline_jobs, output_dir, *, schedule):
        assert schedule.random_seed == 17
        output_dir.mkdir()
        bundle = output_dir / "bundle.json"
        bundle.write_text("{}", encoding="utf-8")
        return bundle

    def fake_analyze(_bundles, output_dir, *, suite):
        assert suite is ParitySuite.DEV
        output_dir.mkdir()
        (output_dir / "summary.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr("eval.harbor.paired.shutil.which", lambda _name: "uvx")
    monkeypatch.setattr("eval.harbor.paired.subprocess.run", fake_run)
    monkeypatch.setattr("eval.harbor.paired.export_harbor_jobs", fake_export)
    monkeypatch.setattr("eval.harbor.paired.analyze_imported_parity", fake_analyze)

    kwargs = {
        "task_names": ("swe-bench/example__repo-1",),
        "wheel_path": wheel,
        "env_file": env_file,
        "claude_settings_path": settings,
        "repetitions": 1,
        "random_seed": 17,
        "maximum_attempts": 2,
        "cooldown_seconds": 0,
        "suite": ParitySuite.DEV,
    }
    with pytest.raises(RuntimeError, match="launcher failed"):
        run_harbor_parity(project, output, **kwargs)

    interrupted_state = json.loads((output / "state.json").read_text(encoding="utf-8"))
    failed_key = next(iter(interrupted_state["jobs"]))
    failed_job = interrupted_state["jobs"][failed_key]
    assert failed_job["attempts"] == []
    assert len(failed_job["launcher_failures"]) == 2
    assert failed_job["selected_job"] is None

    # Simulate state written by the pre-fix runner, which mixed empty launches into attempts.
    failed_job["attempts"] = failed_job.pop("launcher_failures")
    (output / "state.json").write_text(json.dumps(interrupted_state), encoding="utf-8")

    run_harbor_parity(project, output, **kwargs)

    resumed_state = json.loads((output / "state.json").read_text(encoding="utf-8"))
    resumed_job = resumed_state["jobs"][failed_key]
    assert len(resumed_job["attempts"]) == 1
    assert len(resumed_job["launcher_failures"]) == 2
    assert resumed_job["selected_job"] == resumed_job["attempts"][0]["job_root"]
    assert calls == 4


def test_normalized_export_archives_partial_output(tmp_path: Path, monkeypatch) -> None:
    normalized = tmp_path / "normalized"
    normalized.mkdir()
    (normalized / "partial.txt").write_text("preserved", encoding="utf-8")

    def fake_export(_candidate_jobs, _baseline_jobs, output_dir):
        output_dir.mkdir()
        bundle = output_dir / "bundle.json"
        bundle.write_text("{}", encoding="utf-8")
        return bundle

    monkeypatch.setattr("eval.harbor.paired.export_harbor_jobs", fake_export)

    bundle = _materialize_normalized_bundle((tmp_path / "a",), (tmp_path / "b",), normalized)

    assert bundle == normalized / "bundle.json"
    assert bundle.is_file()
    archived = list(tmp_path.glob("normalized-failed-*"))
    assert len(archived) == 1
    assert (archived[0] / "partial.txt").read_text(encoding="utf-8") == "preserved"
