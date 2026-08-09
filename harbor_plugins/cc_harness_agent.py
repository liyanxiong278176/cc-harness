"""Harbor installed-agent plugin for a frozen local cc-harness wheel."""

from __future__ import annotations

import json
import re
import shlex
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, override

from harbor.agents.installed.base import BaseInstalledAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

MODEL = "deepseek-v4-flash"
PRICING_CONTRACT_DIGEST = (
    "sha256:662ed3f9340531cb7391c9dd983c0494c99f36f5862249d608f6a7e8ba0944f1"
)
_WHEEL_VERSION = re.compile(r"^cc_harness-([0-9]+\.[0-9]+\.[0-9]+)-")


class CCHarnessHarborAgent(BaseInstalledAgent):
    """Install a frozen wheel and run cc-harness inside Harbor's task container."""

    SUPPORTS_ATIF = False

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
        super().__init__(*args, **kwargs)

    @staticmethod
    @override
    def name() -> str:
        return "cc-harness"

    @override
    def get_version_command(self) -> str:
        return (
            'export PATH="$HOME/.local/bin:$PATH"; '
            "python3 -c \"from importlib.metadata import version; print(version('cc-harness'))\""
        )

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        await self.exec_as_root(
            environment,
            command=(
                "set -euo pipefail; "
                "if command -v apt-get >/dev/null 2>&1; then "
                "apt-get update && apt-get install -y --no-install-recommends "
                "curl bash git python3 ca-certificates; "
                "elif command -v apk >/dev/null 2>&1; then "
                "apk add --no-cache curl bash git python3 ca-certificates; "
                "else echo 'unsupported package manager' >&2; exit 1; fi"
            ),
        )
        remote_wheel = f"/tmp/{self._wheel_path.name}"
        await environment.upload_file(self._wheel_path, remote_wheel)
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                "if ! command -v uv >/dev/null 2>&1; then "
                "curl -LsSf https://astral.sh/uv/0.7.13/install.sh | sh; "
                "fi; "
                'export PATH="$HOME/.local/bin:$PATH"; '
                f"uv tool install --force {shlex.quote(remote_wheel)}; "
                "cc-harness --help >/dev/null"
            ),
        )

    @override
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        instruction = self.render_instruction(instruction)
        required = ("OPENAI_API_KEY", "OPENAI_BASE_URL")
        env = {name: self._get_env(name) for name in required}
        missing = [name for name, value in env.items() if not value]
        if missing:
            raise ValueError(f"missing cc-harness agent environment: {', '.join(missing)}")
        env["OPENAI_MODEL"] = MODEL
        env["MEMORY_ENABLED"] = "false"
        env["CC_HARNESS_INSTRUCTION"] = instruction
        result = await self.exec_as_agent(
            environment,
            command=(
                'export PATH="$HOME/.local/bin:$PATH"; '
                'workspace="$PWD"; '
                'instruction="$CC_HARNESS_INSTRUCTION"; unset CC_HARNESS_INSTRUCTION; '
                'printf "%s" "$instruction" | '
                f'cc-harness -p --cwd "$workspace" --model {MODEL} '
                "--permission-mode bypass-prompts --host-execution --unbounded-iterations "
                "--capability-profile clean-coding "
                "--output-format json "
                "2>/logs/agent/cc-harness.stderr | tee /logs/agent/cc-harness.jsonl"
            ),
            env={name: str(value) for name, value in env.items()},
        )
        usage = _parse_result(result.stdout or "")
        context.n_input_tokens = usage["input_tokens"]
        context.n_cache_tokens = usage["cache_read_input_tokens"]
        context.n_output_tokens = usage["output_tokens"]
        context.cost_usd = usage["cost_microusd"] / 1_000_000
        context.metadata = {
            "resolved_model": MODEL,
            "model_calls": usage["model_calls"],
            "tool_calls": usage["tool_calls"],
            "uncached_input_tokens": usage["uncached_input_tokens"],
            "cache_creation_input_tokens": usage["cache_creation_input_tokens"],
            "cache_read_input_tokens": usage["cache_read_input_tokens"],
            "pricing_contract_digest": PRICING_CONTRACT_DIGEST,
        }


def _parse_result(stdout: str) -> dict[str, int]:
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
    cost = (
        Decimal(uncached) * Decimal(5)
        + Decimal(cache_creation) * Decimal("6.25")
        + Decimal(cache_read) * Decimal("0.5")
        + Decimal(output_tokens) * Decimal(25)
    )
    return {
        "input_tokens": input_tokens,
        "uncached_input_tokens": uncached,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
        "output_tokens": output_tokens,
        "model_calls": _count(usage, "model_calls"),
        "tool_calls": _count(usage, "tool_calls"),
        "cost_microusd": int(cost.quantize(Decimal(1), rounding=ROUND_HALF_UP)),
    }


def _count(usage: dict[str, Any], field: str) -> int:
    value = usage.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"cc-harness usage has invalid {field}")
    return value
