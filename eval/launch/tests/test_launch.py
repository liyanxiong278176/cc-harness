from __future__ import annotations

import json
import os
import shutil
import sys

import pytest

from eval.core import BudgetEnforcement
from eval.core.tests._support import budget
from eval.launch import (
    HarnessKind,
    LaunchEvidence,
    LaunchInvocation,
    LaunchRequest,
    build_invocation,
    codex_profile,
    parse_launch_output,
    run_invocation,
    standard_profiles,
)


def test_profiles_pin_model_filter_environment_and_build_no_shell_argv(tmp_path) -> None:
    profiles = standard_profiles()
    request = LaunchRequest(prompt="fix it", budget=budget())
    source = {
        "PATH": "bin",
        "OPENAI_API_KEY": "secret",
        "ANTHROPIC_API_KEY": "other",
        "UNRELATED_SECRET": "must-not-leak",
    }
    invocations = {
        profile.harness: build_invocation(profile, request, tmp_path, source_environment=source)
        for profile in profiles
    }
    assert all("deepseek-v4-flash" in item.argv for item in invocations.values())
    assert invocations[HarnessKind.CC_HARNESS].environment["OPENAI_API_KEY"] == "secret"
    assert "ANTHROPIC_API_KEY" not in invocations[HarnessKind.CC_HARNESS].environment
    assert all("UNRELATED_SECRET" not in item.environment for item in invocations.values())
    assert "fix it" not in invocations[HarnessKind.CLAUDE_CODE].argv
    assert invocations[HarnessKind.CLAUDE_CODE].stdin == b"fix it"
    cc_argv = invocations[HarnessKind.CC_HARNESS].argv
    assert cc_argv[cc_argv.index("--max-iterations") + 1] == str(request.budget.max_model_calls)
    assert "--verbose" in invocations[HarnessKind.CLAUDE_CODE].argv
    assert set(invocations) == {HarnessKind.CC_HARNESS, HarnessKind.CLAUDE_CODE}
    if resolved_claude := shutil.which("claude"):
        executable = invocations[HarnessKind.CLAUDE_CODE].argv[0]
        if os.name == "nt" and resolved_claude.lower().endswith((".cmd", ".bat")):
            assert executable.lower().endswith("claude.exe")
        else:
            assert executable == resolved_claude


def test_observational_profile_does_not_inject_eval_loop_or_cost_limits(tmp_path) -> None:
    observed_budget = budget().model_copy(
        update={
            "enforcement": BudgetEnforcement.OBSERVE,
            "emergency_watchdog_seconds": 600,
        }
    )
    request = LaunchRequest(prompt="fix it", budget=observed_budget)
    invocations = {
        profile.harness: build_invocation(profile, request, tmp_path, source_environment={"PATH": "bin"})
        for profile in standard_profiles()
    }

    assert "--max-iterations" not in invocations[HarnessKind.CC_HARNESS].argv
    assert "--unbounded-iterations" in invocations[HarnessKind.CC_HARNESS].argv
    assert "--max-budget-usd" not in invocations[HarnessKind.CLAUDE_CODE].argv


def test_codex_profile_is_available_but_not_in_default_parity_pair(tmp_path) -> None:
    request = LaunchRequest(prompt="fix it", budget=budget())
    profile = codex_profile()
    invocation = build_invocation(profile, request, tmp_path, source_environment={"PATH": "bin"})
    assert profile not in standard_profiles()
    assert invocation.argv[1] == "exec"
    assert invocation.argv[-1] == "-"


def test_project_dotenv_is_loaded_only_through_profile_allowlist(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENAI_API_KEY=secret\nOPENAI_BASE_URL=https://example.invalid/v1\n"
        "OPENAI_MODEL=deepseek-v4-flash\nUNRELATED_SECRET=blocked\n",
        encoding="utf-8",
    )
    invocation = build_invocation(
        standard_profiles()[0],
        LaunchRequest(prompt="fix it", budget=budget()),
        tmp_path,
        source_environment={"PATH": "bin"},
        environment_files=(env_file,),
    )
    assert invocation.environment["OPENAI_API_KEY"] == "secret"
    assert "UNRELATED_SECRET" not in invocation.environment


def test_cc_profile_forwards_owned_sandbox_launch_settings(tmp_path) -> None:
    invocation = build_invocation(
        standard_profiles()[0],
        LaunchRequest(prompt="fix it", budget=budget()),
        tmp_path,
        source_environment={
            "PATH": "bin",
            "CC_HARNESS_SANDBOX_SERVER_PORT": "18765",
            "CC_HARNESS_SANDBOX_SERVER_CONFIG_PATH": str(tmp_path / "server.toml"),
        },
    )

    assert invocation.environment["CC_HARNESS_SANDBOX_SERVER_PORT"] == "18765"
    assert invocation.environment["CC_HARNESS_SANDBOX_SERVER_CONFIG_PATH"].endswith(
        "server.toml"
    )


def test_parsers_require_reported_model_and_reject_conflicts() -> None:
    cc = {
        "schema_version": "cc-harness.print-result.v1",
        "type": "result",
        "resolved_model": "deepseek-v4-flash",
        "usage": {
            "input_tokens": 10,
            "uncached_input_tokens": 2,
            "cache_creation_input_tokens": 1,
            "cache_read_input_tokens": 7,
            "output_tokens": 4,
            "model_calls": 2,
        },
    }
    parsed = parse_launch_output(HarnessKind.CC_HARNESS, json.dumps(cc).encode())
    assert parsed["resolved_model"] == "deepseek-v4-flash"
    assert parsed["input_tokens"] == 10
    assert parsed["uncached_input_tokens"] == 2
    assert parsed["cache_creation_input_tokens"] == 1
    assert parsed["cache_read_input_tokens"] == 7
    assert "cost_microusd" not in parsed

    stream = (
        b'{"type":"system","model":"deepseek-v4-flash"}\n'
        b'{"type":"assistant","message":{"model":"deepseek-v4-flash"}}\n'
        b'{"usage":{"input_tokens":12}}\n'
    )
    assert parse_launch_output(HarnessKind.CLAUDE_CODE, stream)["input_tokens"] == 12

    failed_stream = (
        b'{"type":"system","model":"deepseek-v4-flash"}\n'
        b'{"type":"assistant","error":"model_not_found","model":"<synthetic>"}\n'
    )
    with pytest.raises(ValueError, match="model_not_found"):
        parse_launch_output(HarnessKind.CLAUDE_CODE, failed_stream)

    conflict = b'{"model":"deepseek-v4-flash"}\n{"model":"other"}\n'
    with pytest.raises(ValueError, match="conflicting models"):
        parse_launch_output(HarnessKind.CODEX, conflict)


def test_claude_parser_counts_unique_assistant_tool_uses() -> None:
    documents = [
        {
            "type": "assistant",
            "message": {
                "model": "deepseek-v4-flash",
                "content": [
                    {"type": "tool_use", "id": "call-1", "name": "Read", "input": {}},
                    {"type": "tool_use", "id": "call-2", "name": "Edit", "input": {}},
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "call-1", "content": "done"}
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "model": "deepseek-v4-flash",
                "content": [
                    {"type": "tool_use", "id": "call-2", "name": "Edit", "input": {}}
                ],
            },
        },
        {"type": "result", "num_turns": 3, "tool_calls": 1},
    ]
    stream = b"\n".join(json.dumps(item).encode() for item in documents)

    parsed = parse_launch_output(HarnessKind.CLAUDE_CODE, stream)

    assert parsed["tool_calls"] == 2
    assert parsed["model_calls"] == 3


def test_claude_parser_uses_terminal_usage_and_includes_cached_input() -> None:
    documents = [
        {
            "type": "assistant",
            "message": {
                "id": "message-1",
                "model": "deepseek-v4-flash",
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 20,
                    "output_tokens": 0,
                },
            },
        },
        {
            "type": "assistant",
            "message": {
                "id": "message-1",
                "model": "deepseek-v4-flash",
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 20,
                    "output_tokens": 0,
                },
            },
        },
        {
            "type": "result",
            "num_turns": 4,
            "total_cost_usd": 0.25,
            "usage": {
                "input_tokens": 100,
                "cache_creation_input_tokens": 30,
                "cache_read_input_tokens": 300,
                "output_tokens": 50,
            },
        },
    ]
    stream = b"\n".join(json.dumps(item).encode() for item in documents)

    parsed = parse_launch_output(HarnessKind.CLAUDE_CODE, stream)

    assert parsed["input_tokens"] == 430
    assert parsed["uncached_input_tokens"] == 100
    assert parsed["cache_creation_input_tokens"] == 30
    assert parsed["cache_read_input_tokens"] == 300
    assert parsed["output_tokens"] == 50
    assert parsed["model_calls"] == 4
    assert parsed["cost_microusd"] == 250_000


def test_profile_rejects_secret_name_duplicates() -> None:
    profile = standard_profiles()[0]
    with pytest.raises(ValueError, match="must be unique"):
        profile.model_copy(
            update={"environment_allowlist": ("OPENAI_API_KEY", "OPENAI_API_KEY")}
        ).__class__.model_validate(
            {
                **profile.model_dump(mode="python"),
                "environment_allowlist": ("OPENAI_API_KEY", "OPENAI_API_KEY"),
            }
        )


def test_launch_evidence_rejects_truncated_stderr() -> None:
    evidence = LaunchEvidence(
        harness=HarnessKind.CC_HARNESS,
        requested_model="deepseek-v4-flash",
        resolved_model="deepseek-v4-flash",
        exit_code=0,
        stderr_truncated=True,
        wall_time_ms=1,
    )

    assert evidence.valid_for_parity is False


@pytest.mark.asyncio
async def test_bounded_runner_sends_prompt_on_stdin_and_parses_evidence(tmp_path) -> None:
    profile = standard_profiles(cc_harness=sys.executable)[0]
    script = (
        "import json,sys; prompt=sys.stdin.read(); "
        "print(json.dumps({'schema_version':'cc-harness.print-result.v1',"
        "'resolved_model':'deepseek-v4-flash','text':prompt,"
        "'usage':{'input_tokens':3,'output_tokens':2,'model_calls':1,'tool_calls':0}}))"
    )
    invocation = LaunchInvocation(
        argv=(sys.executable, "-c", script),
        cwd=tmp_path,
        environment=dict(os.environ),
        stdin=b"private prompt",
    )
    completed = await run_invocation(profile, invocation, timeout_seconds=5)
    assert completed.evidence.valid_for_parity is True
    assert completed.evidence.input_tokens == 3
    assert completed.evidence.cost_microusd == 65
    assert completed.evidence.reported_cost_microusd is None
    assert completed.evidence.cost_source == "normalized_tariff"
    assert completed.evidence.pricing_contract_digest is not None
    assert b"private prompt" in completed.stdout


@pytest.mark.asyncio
async def test_timeout_terminates_child_process_tree_and_closes_pipes(tmp_path) -> None:
    profile = standard_profiles(cc_harness=sys.executable)[0]
    child = "import time; time.sleep(60)"
    script = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
        "time.sleep(60)"
    )
    invocation = LaunchInvocation(
        argv=(sys.executable, "-c", script),
        cwd=tmp_path,
        environment=dict(os.environ),
        stdin=b"",
    )

    completed = await run_invocation(profile, invocation, timeout_seconds=0.2)

    assert completed.evidence.timed_out is True
    assert completed.evidence.valid_for_parity is False
