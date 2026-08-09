"""Production cc-harness launcher with explicit benchmark capability profiles."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from eval.core import BudgetEnforcement, ResourceBudget
from eval.launch import (
    HarnessKind,
    LaunchProfile,
    LaunchRequest,
    build_invocation,
    run_invocation,
    standard_profiles,
)
from eval.launch.profiles import LaunchInvocation

from .contracts import MODEL


def cc_harness_profile() -> LaunchProfile:
    return next(
        profile
        for profile in standard_profiles()
        if profile.harness is HarnessKind.CC_HARNESS
    )


def observed_budget(watchdog_seconds: int) -> ResourceBudget:
    return ResourceBudget(
        wall_time_seconds=watchdog_seconds,
        max_steps=100_000,
        max_model_calls=100_000,
        max_tool_calls=100_000,
        max_input_tokens=100_000_000,
        max_output_tokens=10_000_000,
        max_cost_microusd=0,
        enforcement=BudgetEnforcement.OBSERVE,
        emergency_watchdog_seconds=watchdog_seconds,
    )


def build_cc_invocation(
    project_root: Path,
    workspace: Path,
    prompt: str,
    *,
    capability_profile: str,
    home: Path,
    watchdog_seconds: int,
    permission_mode: str = "bypass-prompts",
    host_execution: bool = True,
    continue_session: bool = False,
    environment_overrides: Mapping[str, str] | None = None,
) -> tuple[LaunchProfile, LaunchInvocation, ResourceBudget]:
    profile = cc_harness_profile()
    budget = observed_budget(watchdog_seconds)
    invocation = build_invocation(
        profile,
        LaunchRequest(prompt=prompt, budget=budget),
        workspace,
        environment_files=(project_root / ".env",),
    )
    argv = [item for item in invocation.argv if item != "--bare"]
    if "bypass-prompts" in argv and permission_mode != "bypass-prompts":
        argv[argv.index("bypass-prompts")] = permission_mode
    argv.extend(("--capability-profile", capability_profile))
    if host_execution:
        argv.append("--host-execution")
    if continue_session:
        argv.insert(2, "--continue")
    home.mkdir(parents=True, exist_ok=True)
    environment = dict(invocation.environment)
    environment["HOME"] = str(home.resolve())
    environment["USERPROFILE"] = str(home.resolve())
    environment["OPENAI_MODEL"] = MODEL
    if environment_overrides:
        environment.update(environment_overrides)
    return (
        profile,
        LaunchInvocation(
            argv=tuple(argv),
            cwd=invocation.cwd,
            environment=environment,
            stdin=invocation.stdin,
        ),
        budget,
    )


def final_result(stdout: bytes) -> dict:
    documents = []
    for line in stdout.decode("utf-8", errors="strict").splitlines():
        if line.strip():
            documents.append(json.loads(line))
    if not documents or not isinstance(documents[-1], dict):
        raise ValueError("cc-harness output contains no final result")
    result = documents[-1]
    if result.get("schema_version") != "cc-harness.print-result.v1":
        raise ValueError("cc-harness final result schema is missing")
    return result


async def run_cc_prompt(
    project_root: Path,
    workspace: Path,
    evidence_root: Path,
    prompt: str,
    *,
    capability_profile: str,
    home: Path,
    watchdog_seconds: int,
    permission_mode: str = "bypass-prompts",
    host_execution: bool = True,
    continue_session: bool = False,
    environment_overrides: Mapping[str, str] | None = None,
):
    profile, invocation, budget = build_cc_invocation(
        project_root,
        workspace,
        prompt,
        capability_profile=capability_profile,
        home=home,
        watchdog_seconds=watchdog_seconds,
        permission_mode=permission_mode,
        host_execution=host_execution,
        continue_session=continue_session,
        environment_overrides=environment_overrides,
    )
    completed = await run_invocation(
        profile, invocation, timeout_seconds=budget.execution_timeout_seconds
    )
    evidence_root.mkdir(parents=True, exist_ok=True)
    (evidence_root / "stdout.jsonl").write_bytes(completed.stdout)
    (evidence_root / "stderr.txt").write_bytes(completed.stderr)
    from .storage import atomic_json

    atomic_json(evidence_root / "launch.json", completed.evidence.model_dump(mode="json"))
    return completed
