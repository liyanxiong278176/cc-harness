"""Harbor installed-agent plugin for a frozen local cc-harness wheel."""

from __future__ import annotations

import json
import re
import shlex
import time
from datetime import UTC, datetime
import math
from pathlib import Path
from typing import Any, override

from harbor.agents.installed.base import BaseInstalledAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from harbor_plugins.verifier_smoke import verifier_smoke_environment_error

MODEL = "deepseek-v4-flash"
# Terminal-Bench tasks intentionally include package installation, downloads
# and source builds.  Keep the normal cc-harness 30s interactive budget, but
# give this isolated benchmark agent a bounded long-command budget.  The
# runtime hard cap is the same value; the external Harbor watchdog remains the
# upper task-level safety net.
RUN_COMMAND_TIMEOUT_S = 1_800
RUN_COMMAND_IDLE_TIMEOUT_S = 600
VERIFIER_SMOKE_TIMEOUT_S = 300
TERMINAL_BENCH_MAX_ITERATIONS = 80
TERMINAL_BENCH_POLICY_VERSION = "contract-aware-v2"
_TERMINAL_BENCH_POLICY = """\
Execution discipline for this Terminal-Bench task:
- Treat the task statement and its official success criteria as authoritative.
- Work in short, verifiable stages. Before an expensive install, download, or
  build, inspect the available package manager/toolchain and use noninteractive
  commands with bounded retries and explicit network timeouts where supported.
- Do not repeat a command after it has succeeded; verify each stage with a
  small, deterministic check and continue from the existing workspace state.
- If a dependency/version is unavailable, diagnose the mismatch and choose the
  compatible documented alternative instead of looping on the same install.
- Do not use long blind sleeps. Never sleep longer than 15 seconds; for a
  background job, record its PID and poll it with a bounded log/status check.
  Keep builds and installs in the foreground unless the task explicitly
  requires background execution, and never launch duplicate installers.
- Treat one unreachable URL as a bounded diagnostic: use at most two short
  attempts, then switch to the task's documented/local source or report the
  blocker. Do not spend multiple minutes probing archive mirrors.
- Keep all changes inside the task workspace and run the official checks before
  reporting completion. Treat every explicit output path as a completion
  contract: create it early, validate its format, and do not finish while it is
  missing. For a service, probe the requested local endpoint/process after the
  final change. Once the artifacts and decisive checks pass, stop immediately
  and leave a concise final summary instead of doing optional extra work.
"""
COST_CONTRACT = "provider-reported-only-v1"
_WHEEL_VERSION = re.compile(r"^cc_harness-([0-9]+\.[0-9]+\.[0-9]+)-")
_TIKTOKEN_CACHE_DIR = "/opt/cc-harness/tiktoken-cache"
_TIKTOKEN_CACHE_KEY = "9b5ad71b2ce5302211f9c61530b329a4922fc6a4"


def _idle_budget_for_task(task_id: str | None) -> int:
    """Use a bounded, task-class budget without changing Harbor's total cap."""

    normalized = (task_id or "").casefold()
    if any(token in normalized for token in ("train", "sampling", "fitting", "inference", "optimization", "reshard", "pytorch")):
        return 1_200
    if any(token in normalized for token in ("build", "compile", "server", "nginx", "mailman", "qemu", "cobol", "cython", "git")):
        # Service/compile tasks can legitimately remain quiet while a child
        # process is alive.  Keep the idle budget at the same finite ceiling
        # as the command budget; Harbor's official task timeout remains the
        # upper bound and activity is still recorded by the executor.
        return RUN_COMMAND_TIMEOUT_S
    return 600


class CCHarnessHarborAgent(BaseInstalledAgent):
    """Install a frozen wheel and run cc-harness inside Harbor's task container."""

    SUPPORTS_ATIF = True

    def __init__(self, wheel_path: str, *args, **kwargs) -> None:
        self._wheel_path = Path(wheel_path).expanduser().resolve()
        if not self._wheel_path.is_file() or self._wheel_path.suffix != ".whl":
            raise ValueError(f"cc-harness wheel is missing or invalid: {self._wheel_path}")
        matched = _WHEEL_VERSION.match(self._wheel_path.name)
        if matched is None:
            raise ValueError(f"cc-harness wheel version cannot be resolved: {self._wheel_path}")
        wheel_version = matched.group(1)
        requested_version = kwargs.get("version")
        if requested_version is not None and requested_version != wheel_version:
            raise ValueError(
                f"cc-harness wheel is {wheel_version}, not requested version {requested_version}"
            )
        kwargs["version"] = wheel_version
        bootstrap_path = kwargs.pop("uv_bootstrap_path", None)
        self._uv_bootstrap_path = None
        if bootstrap_path is not None:
            self._uv_bootstrap_path = Path(str(bootstrap_path)).expanduser().resolve()
            if not self._uv_bootstrap_path.is_file():
                raise ValueError(f"Linux uv bootstrap is missing: {self._uv_bootstrap_path}")
        verifier_path = kwargs.pop("verifier_bootstrap_path", None)
        self._verifier_bootstrap_path = None
        if verifier_path is not None:
            self._verifier_bootstrap_path = Path(str(verifier_path)).expanduser().resolve()
            if not self._verifier_bootstrap_path.is_file():
                raise ValueError(
                    f"offline verifier bootstrap is missing: {self._verifier_bootstrap_path}"
                )
        tiktoken_path = kwargs.pop("tiktoken_bootstrap_path", None)
        self._tiktoken_bootstrap_path = None
        if tiktoken_path is not None:
            self._tiktoken_bootstrap_path = Path(str(tiktoken_path)).expanduser().resolve()
            if not self._tiktoken_bootstrap_path.is_file():
                raise ValueError(
                    f"offline tiktoken bootstrap is missing: {self._tiktoken_bootstrap_path}"
                )
        self._last_instruction = ""
        self._last_document: dict[str, Any] | None = None
        super().__init__(*args, **kwargs)

    @staticmethod
    @override
    def name() -> str:
        return "cc-harness"

    @override
    def get_version_command(self) -> str:
        # The no-model preflight deliberately runs against the official task
        # image without installing the coding agent.  Some Terminal-Bench
        # images are intentionally tiny (only /bin/sh and apt metadata) and
        # do not contain Python, so asking the normal version probe to import
        # the wheel would turn a verifier preflight into an apt/network test.
        if (
            self._get_env("CC_HARNESS_VERIFIER_SMOKE_ONLY") == "1"
            and self._get_env("CC_HARNESS_AGENT_INSTALL_ONLY") != "1"
        ):
            return "printf '%s\\n' cc-harness-preflight"
        if self._get_env("CC_HARNESS_TERMINAL_AGENT_RUNTIME") == "1":
            return "/root/.local/bin/cc-harness --help"
        return (
            'export PATH="$HOME/.local/bin:$PATH"; '
            "python3 -c \"from importlib.metadata import version; print(version('cc-harness'))\""
        )

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        # No-model preflight only needs Harbor to create the task container so
        # that its official verifier can run.  Installing cc-harness, uv and
        # verifier dependencies here is both unnecessary and unsafe: minimal
        # official images may not have Python/bash/curl and an apt update can
        # wait indefinitely on an unavailable network.  The real evaluation
        # path still executes the complete frozen/offline install below.
        if (
            self._get_env("CC_HARNESS_VERIFIER_SMOKE_ONLY") == "1"
            and self._get_env("CC_HARNESS_AGENT_INSTALL_ONLY") != "1"
        ):
            await self.exec_as_root(
                environment,
                command="set -eu; test -x /bin/sh; printf '%s\\n' CC_HARNESS_PREFLIGHT_INSTALL_SKIPPED",
            )
            return
        if self._get_env("CC_HARNESS_TERMINAL_AGENT_RUNTIME") == "1":
            # This private runtime is part of the custom Harbor agent. It does
            # not alter PATH or replace any command used by the official task
            # verifier.
            await self.exec_as_root(
                environment,
                command=(
                    "set -eu; "
                    "test -x /root/.local/bin/cc-harness; "
                    "test -x /opt/cc-harness/agent-runtime/python/bin/python; "
                    "test -d /opt/cc-harness/agent-site; "
                    "/root/.local/bin/cc-harness --help >/dev/null"
                ),
            )
            if self._tiktoken_bootstrap_path is not None:
                remote_tiktoken = f"/tmp/{self._tiktoken_bootstrap_path.name}"
                await environment.upload_file(self._tiktoken_bootstrap_path, remote_tiktoken)
                await self.exec_as_root(
                    environment,
                    command=(
                        "set -eu; "
                        f"mkdir -p {_TIKTOKEN_CACHE_DIR}; "
                        f"cp {shlex.quote(remote_tiktoken)} "
                        f"{_TIKTOKEN_CACHE_DIR}/{_TIKTOKEN_CACHE_KEY}; "
                        f"TIKTOKEN_CACHE_DIR={_TIKTOKEN_CACHE_DIR} "
                        "/root/.local/bin/cc-harness --help >/dev/null"
                    ),
                )
            return
        if self._get_env("CC_HARNESS_TERMINAL_VERIFIER_RUNTIME") == "1":
            # The Compose overlay supplies python3 and the frozen verifier
            # command wrappers.  Do not run apt here: minimal official images
            # intentionally have no package metadata/network path.
            await self.exec_as_root(
                environment,
                command=(
                    "set -eu; "
                    "for required in bash python3 tar; do "
                    "command -v \"$required\" >/dev/null 2>&1 || "
                    "{ echo \"missing frozen verifier tool: $required\" >&2; exit 1; }; "
                    "done"
                ),
            )
        else:
            await self.exec_as_root(
                environment,
                command=(
                    "set -eu; "
                    "if command -v apt-get >/dev/null 2>&1; then "
                    "if ! command -v curl >/dev/null 2>&1 || "
                    "! command -v bash >/dev/null 2>&1 || "
                    "! command -v git >/dev/null 2>&1 || "
                    "! command -v python3 >/dev/null 2>&1; then "
                    "DEBIAN_FRONTEND=noninteractive apt-get "
                    "-o Acquire::Retries=3 -o Acquire::http::Timeout=20 "
                    "-o Acquire::https::Timeout=20 update || true; "
                    "DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "
                    "curl bash git python3 ca-certificates || true; "
                    "fi; "
                    "elif command -v apk >/dev/null 2>&1; then "
                    "apk add --no-cache curl bash git python3 ca-certificates; "
                    "else echo 'unsupported package manager' >&2; exit 1; fi; "
                    "for required in bash git python3 tar; do "
                    "command -v \"$required\" >/dev/null 2>&1 || "
                    "{ echo \"missing required verifier tool: $required\" >&2; exit 1; }; "
                    "done"
                ),
            )
        remote_wheel = f"/tmp/{self._wheel_path.name}"
        await environment.upload_file(self._wheel_path, remote_wheel)
        remote_verifier = None
        if self._verifier_bootstrap_path is not None:
            remote_verifier = f"/tmp/{self._verifier_bootstrap_path.name}"
            await environment.upload_file(self._verifier_bootstrap_path, remote_verifier)
            verifier_root = "/opt/cc-harness/terminal-verifier"
            await self.exec_as_root(
                environment,
                command=(
                    "set -e; "
                    f"rm -rf {verifier_root}; mkdir -p {verifier_root}/site-packages; "
                    f"tar -xzf {shlex.quote(remote_verifier)} -C {verifier_root}; "
                    f"python3 {verifier_root}/bin/install-wheelhouse.py "
                    f"--wheelhouse {verifier_root}/wheelhouse "
                    f"--target {verifier_root}/site-packages; "
                    "if [ \"$CC_HARNESS_TERMINAL_VERIFIER_RUNTIME\" != 1 ]; then "
                    f"mkdir -p /root/.local/bin; cp {verifier_root}/env /root/.local/bin/env; "
                    f"install -m 0755 {verifier_root}/bin/curl /usr/local/bin/curl; "
                    "fi; "
                    f"chmod 0755 {verifier_root}/bin/uvx; "
                    f"PYTHONPATH={verifier_root}/site-packages "
                    "python3 -c 'import pytest, ctrf, exceptiongroup, typing_extensions, tomli';"
                ),
            )
        remote_tiktoken = None
        if self._tiktoken_bootstrap_path is not None:
            remote_tiktoken = f"/tmp/{self._tiktoken_bootstrap_path.name}"
            await environment.upload_file(self._tiktoken_bootstrap_path, remote_tiktoken)
            await self.exec_as_root(
                environment,
                command=(
                    "set -eu; "
                    f"mkdir -p {_TIKTOKEN_CACHE_DIR}; "
                    f"install -m 0644 {shlex.quote(remote_tiktoken)} "
                    f"{_TIKTOKEN_CACHE_DIR}/{_TIKTOKEN_CACHE_KEY}; "
                    f"TIKTOKEN_CACHE_DIR={_TIKTOKEN_CACHE_DIR} "
                    "python3 -c 'import hashlib, pathlib; "
                    f"p=pathlib.Path(\"{_TIKTOKEN_CACHE_DIR}/{_TIKTOKEN_CACHE_KEY}\"); "
                    "assert hashlib.sha256(p.read_bytes()).hexdigest() == "
                    "\"223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7\"'"
                ),
            )
        if self._uv_bootstrap_path is not None:
            remote_bootstrap = f"/tmp/{self._uv_bootstrap_path.name}"
            await environment.upload_file(self._uv_bootstrap_path, remote_bootstrap)
            uv_setup = (
                'mkdir -p "$HOME/.local/bin"; '
                f"tar -xzf {shlex.quote(remote_bootstrap)} "
                ' -C "$HOME/.local/bin" --strip-components=1; '
                'test -x "$HOME/.local/bin/uv"; '
                'test -x "$HOME/.local/bin/uvx"; '
                'chmod 0755 "$HOME/.local/bin/uv" "$HOME/.local/bin/uvx"; '
            )
        else:
            # SWE-bench keeps its historical plugin contract.  Terminal-Bench
            # always supplies the frozen artifact above, so its containers do
            # not take this network-dependent fallback path.
            uv_setup = (
                'if ! command -v uv >/dev/null 2>&1; then '
                "curl -LsSf https://astral.sh/uv/0.7.13/install.sh | sh; "
                "fi; "
            )
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                + 'if [ "${CC_HARNESS_TERMINAL_VERIFIER_RUNTIME:-0}" = 1 ]; then '
                + 'export PYTHONHOME=/opt/cc-harness/verifier-runtime/python; '
                + 'export PYTHONPATH=/opt/cc-harness/agent-site:/opt/cc-harness/verifier-runtime/python/lib/python3.12/site-packages; '
                + f'export TIKTOKEN_CACHE_DIR="{_TIKTOKEN_CACHE_DIR}"; '
                + 'test -x /opt/cc-harness/verifier-offline-bin/cc-harness; '
                + '/opt/cc-harness/verifier-offline-bin/cc-harness --help >/dev/null; '
                + 'TIKTOKEN_CACHE_DIR="$TIKTOKEN_CACHE_DIR" '
                + '/opt/cc-harness/verifier-runtime/lib/ld-linux-x86-64.so.2 '
                + '--library-path /opt/cc-harness/verifier-runtime/lib '
                + '/opt/cc-harness/verifier-runtime/python/bin/python '
                + '-c "import tiktoken; tiktoken.get_encoding(\'cl100k_base\')"; '
                + 'else '
                + uv_setup
                + 'export PATH="$HOME/.local/bin:$PATH"; '
                + 'agent_python=; '
                + 'for candidate in /usr/bin/python3 /usr/local/bin/python3 /usr/bin/python /usr/local/bin/python; do '
                + "if [ -x \"$candidate\" ] && \"$candidate\" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then agent_python=\"$candidate\"; break; fi; "
                + 'done; '
                + 'if [ -z "$agent_python" ]; then agent_python=/opt/cc-harness/verifier-runtime/python/bin/python; fi; '
                + f'export TIKTOKEN_CACHE_DIR="{_TIKTOKEN_CACHE_DIR}"; '
                + "export UV_HTTP_TIMEOUT=120; export UV_HTTP_RETRIES=5; "
                + f"uv tool install --python \"$agent_python\" --force {shlex.quote(remote_wheel)}; "
                + 'cc-harness --help >/dev/null; '
                + 'tool_dir="$(uv tool dir)"; tool_python="$tool_dir/cc-harness/bin/python"; test -x "$tool_python"; '
                + 'TIKTOKEN_CACHE_DIR="$TIKTOKEN_CACHE_DIR" "$tool_python" -c '
                + '"import tiktoken; tiktoken.get_encoding(\'cl100k_base\')"; '
                + 'fi'
            ),
        )
    async def _run_no_model_preflight(
        self,
        environment: BaseEnvironment,
    ) -> None:
        """Run the agent-side portion of the verifier preflight without a model.

        Harbor mounts the official verifier's ``/tests`` tree only for the
        verifier phase, not while ``BaseInstalledAgent.install`` is running.
        Therefore the plugin must not look for ``/tests/test.sh`` during
        install.  This probe validates only the agent-side frozen runtime;
        Harbor then invokes the real official verifier in the same fresh task
        container, allowing the prewarm runner to inspect its result without
        making a model call.
        """

        # The actual verifier is the readiness gate for the official task
        # image.  Keep this agent-side probe POSIX-only so a minimal image
        # cannot fail before Harbor reaches that verifier.
        await self.exec_as_root(
            environment,
            command="set -eu; test -x /bin/sh; printf '%s\\n' CC_HARNESS_AGENT_PREFLIGHT_READY",
            env={},
        )

    async def _run_verifier_smoke(
        self,
        environment: BaseEnvironment,
    ) -> None:
        """Run a non-scoring verifier smoke after the task files are mounted.

        This path is intentionally separate from ``install``.  The official
        verifier owns ``/tests/test.sh`` and Harbor exposes it only during the
        verifier phase; checking it from ``install`` creates a false
        environment-not-ready failure for otherwise valid task images.
        """

        timeout_value = self._get_env("CC_HARNESS_VERIFIER_SMOKE_TIMEOUT_S")
        try:
            smoke_timeout = max(30, min(VERIFIER_SMOKE_TIMEOUT_S, int(timeout_value or 0)))
        except ValueError:
            smoke_timeout = VERIFIER_SMOKE_TIMEOUT_S
        smoke_command = (
            'export PATH="/root/.local/bin:/opt/cc-harness/terminal-verifier/bin:$PATH"; '
            'export PYTHONPATH="/opt/cc-harness/terminal-verifier/site-packages:${PYTHONPATH:-}"; '
            'export TIKTOKEN_CACHE_DIR="/opt/cc-harness/tiktoken-cache"; '
            "python3 - <<'PY'\n"
            "import os, subprocess\n"
            "budget = int(os.environ.get('CC_HARNESS_VERIFIER_SMOKE_TIMEOUT_S', '300'))\n"
            "if not os.path.isfile('/tests/test.sh'):\n"
            "    print('CC_HARNESS_VERIFIER_SMOKE_ENV_ERROR=missing /tests/test.sh')\n"
            "    raise SystemExit(42)\n"
            "if not os.access('/tests/test.sh', os.X_OK):\n"
            "    print('CC_HARNESS_VERIFIER_SMOKE_ENV_ERROR=/tests/test.sh not executable')\n"
            "    raise SystemExit(42)\n"
            "try:\n"
            "    subprocess.run(['bash', '-n', '/tests/test.sh'], check=True, timeout=30)\n"
            "    completed = subprocess.run(['/tests/test.sh'], cwd=os.getcwd(), "
            "capture_output=True, text=True, timeout=budget)\n"
            "except subprocess.TimeoutExpired as exc:\n"
            "    print('CC_HARNESS_VERIFIER_SMOKE_TIMEOUT')\n"
            "    print((str(exc.stdout or '') + '\\n' + str(exc.stderr or ''))[-12000:])\n"
            "    raise SystemExit(124)\n"
            "except OSError as exc:\n"
            "    print('CC_HARNESS_VERIFIER_SMOKE_OSERROR')\n"
            "    print(str(exc))\n"
            "    raise SystemExit(125)\n"
            "except subprocess.CalledProcessError as exc:\n"
            "    print('CC_HARNESS_VERIFIER_SMOKE_ENV_ERROR=invalid test.sh syntax')\n"
            "    print(str(exc))\n"
            "    raise SystemExit(42)\n"
            "print(f'CC_HARNESS_VERIFIER_SMOKE_EXIT={completed.returncode}')\n"
            "print((completed.stdout + '\\n' + completed.stderr)[-12000:])\n"
            "# Baseline reward/test failures are diagnostic only.  The caller\n"
            "# classifies environment markers, while the official verifier\n"
            "# remains the sole source of the scored result.\n"
            "raise SystemExit(0)\n"
            "PY"
        )
        environment_value = str(smoke_timeout)
        try:
            smoke_result = await environment.exec(
                command=smoke_command,
                user="root",
                env={"CC_HARNESS_VERIFIER_SMOKE_TIMEOUT_S": environment_value},
                timeout_sec=smoke_timeout + 30,
            )
        except Exception as exc:  # Harbor uses several timeout exception types.
            raise RuntimeError(f"verifier smoke could not complete: {exc}") from exc
        smoke_output = "\n".join(
            str(value or "")
            for value in (getattr(smoke_result, "stdout", ""), getattr(smoke_result, "stderr", ""))
        )
        smoke_error = verifier_smoke_environment_error(smoke_output)
        if getattr(smoke_result, "return_code", 0) != 0 and smoke_error is None:
            smoke_error = (
                "verifier smoke wrapper exited non-zero: "
                f"{getattr(smoke_result, 'return_code', 'unknown')}"
            )
        if smoke_error is not None:
            raise RuntimeError(smoke_error)

    @override
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        if self._get_env("CC_HARNESS_AGENT_INSTALL_ONLY") == "1":
            # Used only by the no-model formal-install regression probe.  The
            # full install has already completed; returning here prevents any
            # provider call while Harbor still proceeds to its official
            # verifier, exercising the exact setup path used by live runs.
            return
        if self._get_env("CC_HARNESS_VERIFIER_SMOKE_ONLY") == "1":
            await self._run_no_model_preflight(environment)
            return
        if self._get_env("CC_HARNESS_VERIFIER_SMOKE") == "1":
            await self._run_verifier_smoke(environment)
            return
        instruction = self.render_instruction(instruction)
        terminal_bench = self._get_env("CC_HARNESS_TERMINAL_BENCH") == "1"
        if terminal_bench:
            instruction = f"{_TERMINAL_BENCH_POLICY}\nTask statement:\n{instruction}"
        configured_iterations = self._get_env("CC_HARNESS_TERMINAL_MAX_ITERATIONS")
        max_iterations = _bounded_iterations(configured_iterations)
        required = ("OPENAI_API_KEY", "OPENAI_BASE_URL")
        env = {name: self._get_env(name) for name in required}
        missing = [name for name, value in env.items() if not value]
        if missing:
            raise ValueError(f"missing cc-harness agent environment: {', '.join(missing)}")
        env["OPENAI_MODEL"] = MODEL
        env["MEMORY_ENABLED"] = "false"
        # The task statement comes from the frozen official Terminal-Bench
        # catalog, not from a live user or an untrusted tool result.  Preserve
        # that provenance inside cc-harness so goal/L2 screens do not mistake
        # legitimate words such as "git push" for a prompt-injection request.
        # This does not disable command, tool, output, or sandbox security.
        if terminal_bench:
            env["CC_HARNESS_TERMINAL_BENCH"] = "1"
            env["CC_HARNESS_TRUSTED_BENCHMARK_TASK"] = "1"
        env["CC_HARNESS_OUTPUT_EGRESS_GUARD"] = "1"
        env["TIKTOKEN_CACHE_DIR"] = _TIKTOKEN_CACHE_DIR
        env["CC_HARNESS_RUN_COMMAND_TIMEOUT_S"] = str(RUN_COMMAND_TIMEOUT_S)
        idle_budget = _idle_budget_for_task(self._get_env("CC_HARNESS_TASK_ID"))
        env["CC_HARNESS_RUN_COMMAND_IDLE_TIMEOUT_S"] = str(idle_budget)
        env["PYTHONUNBUFFERED"] = "1"
        env["CC_HARNESS_PROGRESS_FILE"] = "/logs/agent/cc-harness-progress.jsonl"
        env["CC_HARNESS_INSTRUCTION"] = instruction
        configured_timeout = self._get_env("CC_HARNESS_TASK_TIMEOUT_SECONDS")
        try:
            task_timeout = max(120.0, float(configured_timeout or 0))
        except ValueError:
            task_timeout = 0.0
        deadline_reserve = min(180.0, max(60.0, task_timeout * 0.12)) if task_timeout else 90.0
        if task_timeout:
            env["CC_HARNESS_TASK_DEADLINE_EPOCH"] = str(time.time() + task_timeout)
            env["CC_HARNESS_TASK_DEADLINE_RESERVE_S"] = str(deadline_reserve)
        iteration_flag = (
            f"--max-iterations {max_iterations}"
            if terminal_bench
            else "--unbounded-iterations"
        )
        result = await self.exec_as_agent(
            environment,
            command=(
                'export PATH="$HOME/.local/bin:$PATH"; '
                f'export TIKTOKEN_CACHE_DIR="{_TIKTOKEN_CACHE_DIR}"; '
                'workspace="$PWD"; '
                'instruction="$CC_HARNESS_INSTRUCTION"; unset CC_HARNESS_INSTRUCTION; '
                'printf "%s" "$instruction" | '
                f'cc-harness -p --cwd "$workspace" --model {MODEL} '
                f"--permission-mode bypass-prompts --host-execution {iteration_flag} "
                "--capability-profile clean-coding --mode coding "
                "--output-format json "
                "2>/logs/agent/cc-harness.stderr | tee /logs/agent/cc-harness.jsonl"
            ),
            env={name: str(value) for name, value in env.items()},
        )
        document = _parse_document(result.stdout or "")
        usage = _usage_from_document(document)
        self._last_instruction = instruction
        self._last_document = document
        context.n_input_tokens = usage["input_tokens"]
        context.n_cache_tokens = usage["cache_read_input_tokens"]
        context.n_output_tokens = usage["output_tokens"]
        # Harbor's context field is USD.  Only populate it when the provider
        # explicitly reports USD (or the provider's legacy direct-cost field
        # omitted currency); never derive it from token counts.
        context.cost_usd = usage["provider_cost_usd"]
        context.metadata = {
            "resolved_model": MODEL,
            "model_calls": usage["model_calls"],
            "tool_calls": usage["tool_calls"],
            "run_command_timeout_s": RUN_COMMAND_TIMEOUT_S,
            "run_command_idle_timeout_s": idle_budget,
            "terminal_bench_policy_version": (
                TERMINAL_BENCH_POLICY_VERSION if terminal_bench else None
            ),
            "max_iterations": max_iterations if terminal_bench else None,
            "task_timeout_seconds": task_timeout or None,
            "deadline_reserve_seconds": deadline_reserve,
            "uncached_input_tokens": usage["uncached_input_tokens"],
            "cache_creation_input_tokens": usage["cache_creation_input_tokens"],
            "cache_read_input_tokens": usage["cache_read_input_tokens"],
            "api_reported_cost": usage["api_reported_cost"],
            "api_reported_cost_currency": usage["api_reported_cost_currency"],
            "api_cost_source": "provider",
            "api_cost_status": usage["api_cost_status"],
            "api_cost_observed": usage["api_cost_observed"],
            "api_cost_complete": usage["api_cost_complete"],
            "cost_contract": COST_CONTRACT,
        }

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        if self._last_document is None:
            return
        try:
            trajectory = _atif_trajectory(
                instruction=self._last_instruction,
                document=self._last_document,
                version=self.version() or "unknown",
                session_id=self.session_id,
            )
            from harbor.utils.trajectory_utils import format_trajectory_json

            (self.logs_dir / "trajectory.json").write_text(
                format_trajectory_json(trajectory.to_json_dict()), encoding="utf-8"
            )
        except Exception:
            self.logger.exception("Failed to convert cc-harness JSONL to ATIF")


def _parse_result(stdout: str) -> dict[str, int]:
    return _usage_from_document(_parse_document(stdout))


def _parse_document(stdout: str) -> dict[str, Any]:
    try:
        documents = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    except json.JSONDecodeError as exc:
        raise ValueError(f"cc-harness output is not valid JSONL: {exc}") from exc
    if not documents or not isinstance(documents[-1], dict):
        raise ValueError("cc-harness output contains no result object")
    result = documents[-1]
    if result.get("schema_version") != "cc-harness.print-result.v1":
        raise ValueError("cc-harness result schema is missing")
    if result.get("error"):
        raise ValueError(f"cc-harness reported an error: {result['error']}")
    if result.get("resolved_model") != MODEL:
        raise ValueError("cc-harness resolved model does not match the parity contract")
    return result


def _usage_from_document(result: dict[str, Any]) -> dict[str, Any]:
    usage = result.get("usage")
    if not isinstance(usage, dict):
        raise TypeError("cc-harness result lacks usage telemetry")
    input_tokens = _count(usage, "input_tokens")
    uncached = _count(usage, "uncached_input_tokens")
    cache_creation = _count(usage, "cache_creation_input_tokens")
    cache_read = _count(usage, "cache_read_input_tokens")
    if uncached + cache_creation + cache_read != input_tokens:
        raise ValueError("cc-harness cache token breakdown does not sum to input_tokens")
    output_tokens = _count(usage, "output_tokens")
    model_calls = _count(usage, "model_calls")
    tool_calls = _count(usage, "tool_calls")
    raw_cost = usage.get("api_reported_cost")
    if raw_cost is None:
        raw_cost = usage.get("reported_cost")
    try:
        api_reported_cost = float(raw_cost) if raw_cost is not None else None
    except (TypeError, ValueError):
        api_reported_cost = None
    if api_reported_cost is not None and (
        not math.isfinite(api_reported_cost) or api_reported_cost < 0
    ):
        api_reported_cost = None
    raw_currency = usage.get("api_reported_cost_currency")
    if raw_currency is None:
        raw_currency = usage.get("reported_cost_currency")
    api_reported_cost_currency = (
        str(raw_currency).strip().upper() if raw_currency is not None else None
    )
    raw_status = usage.get("api_cost_status")
    api_cost_status = (
        str(raw_status).strip().lower() if raw_status is not None else None
    )
    if api_cost_status is None and api_reported_cost is not None:
        # Backward-compatible direct provider envelope: the amount itself is
        # evidence, but no tariff inference is permitted.
        api_cost_status = "reported"
    # Providers may return token usage without a separate call counter. That
    # is still observable model activity, so a missing direct price must be
    # classified as incomplete rather than unavailable.
    api_cost_observed = (
        bool(usage.get("api_cost_observed"))
        or model_calls > 0
        or input_tokens > 0
        or output_tokens > 0
    )
    api_cost_complete = (
        api_cost_status == "reported" and api_reported_cost is not None
    )
    if not api_cost_complete:
        api_cost_status = "incomplete" if api_cost_observed else "unavailable"
    provider_cost_usd = (
        api_reported_cost
        if api_cost_complete and api_reported_cost_currency in (None, "USD")
        else None
    )
    direct_cost_microusd = (
        round(provider_cost_usd * 1_000_000)
        if provider_cost_usd is not None
        else None
    )
    return {
        "input_tokens": input_tokens,
        "uncached_input_tokens": uncached,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
        "output_tokens": output_tokens,
        "model_calls": model_calls,
        "tool_calls": tool_calls,
        # This legacy-shaped field is now a normalized provider USD fact, not
        # a token-tariff estimate.  It stays null for unknown currencies.
        "cost_microusd": direct_cost_microusd,
        "api_reported_cost": api_reported_cost if api_cost_complete else None,
        "api_reported_cost_currency": api_reported_cost_currency,
        "api_cost_source": "provider",
        "api_cost_status": api_cost_status,
        "api_cost_observed": api_cost_observed,
        "api_cost_complete": api_cost_complete,
        "provider_cost_usd": provider_cost_usd,
    }


def _atif_trajectory(
    *,
    instruction: str,
    document: dict[str, Any],
    version: str,
    session_id: str | None,
):
    from harbor.models.trajectories import (
        Agent,
        FinalMetrics,
        Observation,
        ObservationResult,
        Step,
        ToolCall,
        Trajectory,
    )

    steps = [Step(step_id=1, source="user", message=instruction)]
    events = document.get("trajectory") or []
    index = 0
    while index < len(events):
        event = events[index]
        if not isinstance(event, dict):
            index += 1
            continue
        event_type = event.get("type")
        timestamp = _event_timestamp(event.get("ts"))
        if event_type == "thought" and event.get("text"):
            steps.append(
                Step(
                    step_id=len(steps) + 1,
                    timestamp=timestamp,
                    source="agent",
                    model_name=MODEL,
                    message="",
                    reasoning_content=str(event["text"]),
                    llm_call_count=1,
                )
            )
        elif event_type == "action" and event.get("name"):
            call_id = f"call-{len(steps) + 1:04d}"
            observation = None
            # Runtime bookkeeping events may be emitted between an action and
            # its observation.  Associate the first following observation,
            # but never cross the next action boundary.
            candidate_index = index + 1
            while candidate_index < len(events):
                candidate = events[candidate_index]
                if isinstance(candidate, dict) and candidate.get("type") == "action":
                    break
                if isinstance(candidate, dict) and candidate.get("type") == "observation":
                    observation = Observation(
                        results=[
                            ObservationResult(
                                source_call_id=call_id,
                                content=str(candidate.get("text") or ""),
                                extra={
                                    "is_error": bool(candidate.get("is_error")),
                                    "duration_ms": candidate.get("duration_ms"),
                                },
                            )
                        ]
                    )
                    break
                candidate_index += 1
            args = event.get("args") if isinstance(event.get("args"), dict) else {}
            steps.append(
                Step(
                    step_id=len(steps) + 1,
                    timestamp=timestamp,
                    source="agent",
                    model_name=MODEL,
                    message="",
                    tool_calls=[
                        ToolCall(
                            tool_call_id=call_id,
                            function_name=str(event["name"]),
                            arguments=args,
                        )
                    ],
                    observation=observation,
                    llm_call_count=1,
                )
            )
        index += 1
    final_text = str(document.get("text") or "")
    if final_text or len(steps) == 1:
        steps.append(
            Step(
                step_id=len(steps) + 1,
                source="agent",
                model_name=MODEL,
                message=final_text,
                llm_call_count=1,
            )
        )
    usage = _usage_from_document(document)
    return Trajectory(
        schema_version="ATIF-v1.7",
        session_id=session_id,
        agent=Agent(
            name="cc-harness",
            version=version,
            model_name=MODEL,
            extra={"mode": "coding", "capability_profile": "clean-coding"},
        ),
        steps=steps,
        final_metrics=FinalMetrics(
            total_prompt_tokens=usage["input_tokens"],
            total_completion_tokens=usage["output_tokens"],
            total_cached_tokens=usage["cache_read_input_tokens"],
            total_cost_usd=usage["provider_cost_usd"],
            total_steps=len(steps),
            extra={
                "model_calls": usage["model_calls"],
                "tool_calls": usage["tool_calls"],
                "api_reported_cost": usage["api_reported_cost"],
                "api_reported_cost_currency": usage["api_reported_cost_currency"],
                "api_cost_source": "provider",
                "api_cost_status": usage["api_cost_status"],
                "cost_contract": COST_CONTRACT,
            },
        ),
        notes="Converted from cc-harness append-only JSONL events; raw JSONL is retained.",
    )


def _event_timestamp(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(float(value), UTC).isoformat()


def _count(usage: dict[str, Any], field: str) -> int:
    value = usage.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"cc-harness usage has invalid {field}")
    return value


def _bounded_iterations(raw: str | None) -> int:
    """Resolve the benchmark loop cap without allowing an unbounded override."""

    try:
        value = int(raw) if raw is not None else TERMINAL_BENCH_MAX_ITERATIONS
    except (TypeError, ValueError):
        return TERMINAL_BENCH_MAX_ITERATIONS
    return min(max(value, 1), 100)
