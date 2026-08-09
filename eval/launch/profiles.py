"""Deterministic argv and environment construction for supported harnesses."""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from eval.core.models import BudgetEnforcement

from .models import PARITY_MODEL, HarnessKind, LaunchProfile, LaunchRequest

_PROCESS_ENV = ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE")


@dataclass(frozen=True)
class LaunchInvocation:
    argv: tuple[str, ...]
    cwd: Path
    environment: dict[str, str]
    stdin: bytes


def standard_profiles(
    *,
    cc_harness: str = "cc-harness",
    claude: str = "claude",
    claude_settings_path: Path | None = None,
) -> tuple[LaunchProfile, ...]:
    cc_harness = _resolve_executable(cc_harness)
    claude = _resolve_executable(claude)
    claude_extra_args: tuple[str, ...] = ()
    if claude_settings_path is not None:
        settings = claude_settings_path.resolve()
        if not settings.is_file():
            raise ValueError(f"Claude settings file is not a regular file: {settings}")
        claude_extra_args = ("--settings", str(settings))
    return (
        LaunchProfile(
            profile_id="cc-harness.deepseek-v4-flash",
            harness=HarnessKind.CC_HARNESS,
            executable=cc_harness,
            provider_route_id="deepseek-openai-compatible",
            environment_allowlist=(
                "OPENAI_API_KEY",
                "OPENAI_BASE_URL",
                "OPENAI_MODEL",
                "MEMORY_ENABLED",
                "MEMORY_DB_DIR",
                "EMBEDDING_BASE_URL",
                "EMBEDDING_API_KEY",
                "EMBEDDING_MODEL",
                "EMBEDDING_DIM",
                "CC_HARNESS_SANDBOX_SERVER_PORT",
                "CC_HARNESS_SANDBOX_SERVER_CONFIG_PATH",
            ),
        ),
        LaunchProfile(
            profile_id="claude-code.deepseek-v4-flash",
            harness=HarnessKind.CLAUDE_CODE,
            executable=claude,
            provider_route_id="operator-anthropic-compatible-gateway",
            environment_allowlist=("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"),
            extra_args=claude_extra_args,
        ),
    )


def codex_profile(*, codex: str = "codex", extra_args: tuple[str, ...] = ()) -> LaunchProfile:
    """Build an inactive Codex profile; it is not part of the default parity pair."""
    return LaunchProfile(
        profile_id="codex.deepseek-v4-flash",
        harness=HarnessKind.CODEX,
        executable=_resolve_executable(codex),
        provider_route_id="operator-codex-deepseek-provider",
        environment_allowlist=("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "CODEX_HOME"),
        extra_args=extra_args,
    )


def build_invocation(
    profile: LaunchProfile,
    request: LaunchRequest,
    workspace: Path,
    *,
    source_environment: Mapping[str, str] | None = None,
    environment_files: tuple[Path, ...] = (),
) -> LaunchInvocation:
    root = workspace.resolve()
    if not root.is_dir():
        raise ValueError(f"launch workspace is not a directory: {root}")
    env = _filtered_environment(
        profile,
        source_environment or os.environ,
        environment_files=environment_files,
    )
    enforce_budget = request.budget.enforcement is BudgetEnforcement.ENFORCED
    cc_budget_args = (
        ("--max-iterations", str(request.budget.max_model_calls)) if enforce_budget else ()
    )
    if not enforce_budget:
        cc_budget_args = ("--unbounded-iterations",)
    claude_budget_args = (
        ("--max-budget-usd", f"{request.budget.max_cost_microusd / 1_000_000:.6f}")
        if enforce_budget
        else ()
    )

    if profile.harness is HarnessKind.CC_HARNESS:
        argv = (
            profile.executable,
            "-p",
            "--cwd",
            str(root),
            "--bare",
            "--model",
            PARITY_MODEL,
            "--permission-mode",
            "bypass-prompts",
            "--output-format",
            "json",
            *cc_budget_args,
            *profile.extra_args,
        )
    elif profile.harness is HarnessKind.CLAUDE_CODE:
        argv = (
            profile.executable,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            PARITY_MODEL,
            *claude_budget_args,
            "--permission-mode",
            "bypassPermissions",
            "--no-session-persistence",
            "--bare",
            *profile.extra_args,
        )
    else:
        argv = (
            profile.executable,
            "exec",
            "-m",
            PARITY_MODEL,
            "-C",
            str(root),
            "-s",
            "workspace-write",
            "--ephemeral",
            "--ignore-user-config",
            "--json",
            *profile.extra_args,
            "-",
        )
    return LaunchInvocation(
        argv=argv,
        cwd=root,
        environment=env,
        stdin=request.prompt.encode("utf-8"),
    )


def _filtered_environment(
    profile: LaunchProfile,
    source_environment: Mapping[str, str],
    *,
    environment_files: tuple[Path, ...],
) -> dict[str, str]:
    merged: dict[str, str] = {}
    for path in environment_files:
        resolved = path.resolve()
        if not resolved.is_file():
            raise ValueError(f"environment file is not a regular file: {resolved}")
        merged.update(
            {key: str(value) for key, value in dotenv_values(resolved).items() if value is not None}
        )
    merged.update(source_environment)
    allowed = set(_PROCESS_ENV) | set(profile.environment_allowlist)
    env = {key: value for key, value in merged.items() if key.upper() in allowed}
    env["NO_COLOR"] = "1"
    return env


def _resolve_executable(command: str) -> str:
    """Resolve platform shims before entering the shell-free subprocess boundary."""
    resolved = Path(shutil.which(command) or command)
    if resolved.suffix.lower() in {".cmd", ".bat"} and resolved.stem.lower() == "claude":
        native = (
            resolved.parent
            / "node_modules"
            / "@anthropic-ai"
            / "claude-code"
            / "bin"
            / "claude.exe"
        )
        if native.is_file():
            return str(native)
    return str(resolved)
