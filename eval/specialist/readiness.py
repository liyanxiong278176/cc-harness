"""Zero-model-call readiness checks for controlled specialist evaluations."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import Field

from cc_harness.config import MCPServerConfig
from cc_harness.mcp_client import MCPClient
from eval.core import canonical_json_bytes, content_fingerprint
from eval.core.models import EvidenceModel
from eval.launch import PARITY_MODEL, HarnessKind
from eval.locomo.download_dataset import verify_dataset

from .catalog import SPECIALIST_CATALOG
from .context_fixture import materialize_context_fixture
from .fixtures import materialize_task_fixture
from .models import SpecialistSuite


class CheckStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class ReadinessCheck(EvidenceModel):
    schema_version: Literal["eval.specialist-readiness-check.v1"] = (
        "eval.specialist-readiness-check.v1"
    )
    check_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9.-]*$")
    status: CheckStatus
    detail: str = Field(min_length=1, max_length=2_000)


class SpecialistReadinessReport(EvidenceModel):
    schema_version: Literal["eval.specialist-readiness.v1"] = "eval.specialist-readiness.v1"
    generated_at: datetime
    project_root: str
    output_root: str
    model: str
    catalog_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    task_counts: dict[str, int]
    checks: tuple[ReadinessCheck, ...]
    ready: bool


async def run_specialist_readiness(
    project_root: Path,
    output_root: Path,
    *,
    claude_settings_path: Path | None = None,
) -> SpecialistReadinessReport:
    project_root = project_root.resolve()
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    checks: list[ReadinessCheck] = []

    checks.append(_python_check())
    checks.append(_module_check("mcp", "dependency.mcp"))
    checks.append(_executable_check("cc-harness", "executable.cc-harness"))
    checks.append(_executable_check("claude", "executable.claude-code"))
    checks.append(_project_model_check(project_root / ".env"))
    checks.append(
        _claude_settings_check(
            (claude_settings_path or (Path.home() / ".claude" / "settings.json")).resolve()
        )
    )
    checks.append(_locomo_check(project_root / "eval" / "locomo" / "data" / "locomo10.json"))
    checks.append(_disk_check(output_root))

    fixture_root = output_root / "fixture-smoke" / uuid.uuid4().hex
    loop_task = next(
        task
        for task in SPECIALIST_CATALOG.tasks
        if task.suite is SpecialistSuite.AGENT_LOOP and task.scenario == "transient-tool-failure"
    )
    try:
        fixture = materialize_task_fixture(loop_task, fixture_root)
        checks.append(await _mcp_smoke_check(fixture))
    except Exception as exc:  # noqa: BLE001 - readiness must retain actionable evidence
        checks.append(
            ReadinessCheck(
                check_id="fixture.mcp-smoke",
                status=CheckStatus.FAIL,
                detail=f"{type(exc).__name__}: {str(exc)[:1_500]}",
            )
        )

    context_task = next(
        task
        for task in SPECIALIST_CATALOG.tasks
        if task.suite is SpecialistSuite.CONTEXT and task.context_profile is not None
    )
    try:
        generated = materialize_context_fixture(
            output_root / "context-smoke",
            context_task.context_profile,
            context_window_tokens=8_192,
            seed=20260807,
        )
        checks.append(
            ReadinessCheck(
                check_id="fixture.context-generator",
                status=CheckStatus.PASS,
                detail=(
                    f"generated {generated.token_count} measured tokens; "
                    f"fact offset {generated.fact_token_offset}"
                ),
            )
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(
            ReadinessCheck(
                check_id="fixture.context-generator",
                status=CheckStatus.FAIL,
                detail=f"{type(exc).__name__}: {str(exc)[:1_500]}",
            )
        )

    task_counts = {
        suite.value: sum(task.suite is suite for task in SPECIALIST_CATALOG.tasks)
        for suite in SpecialistSuite
    }
    report = SpecialistReadinessReport(
        generated_at=datetime.now(UTC),
        project_root=str(project_root),
        output_root=str(output_root),
        model=PARITY_MODEL,
        catalog_digest=content_fingerprint(SPECIALIST_CATALOG),
        task_counts=task_counts,
        checks=tuple(checks),
        ready=all(check.status is not CheckStatus.FAIL for check in checks),
    )
    _write_outputs(output_root, report)
    return report


async def _mcp_smoke_check(fixture) -> ReadinessCheck:
    candidate = next(item for item in fixture.harnesses if item.harness is HarnessKind.CC_HARNESS)
    raw = json.loads(candidate.mcp_config_path.read_text(encoding="utf-8"))
    config = MCPServerConfig(**raw["mcpServers"]["specialist-fixture"])
    client = MCPClient({"specialist-fixture": config})
    try:
        await client.start(init_timeout_s=15.0)
        names = {item["function"]["name"] for item in await client.list_tools()}
        required = {
            "mcp__specialist-fixture__flaky_read",
            "mcp__specialist-fixture__paged_lookup",
            "mcp__specialist-fixture__mutate_once",
        }
        if not required.issubset(names):
            raise RuntimeError(f"missing specialist MCP tools: {sorted(required - names)}")
        flaky = "mcp__specialist-fixture__flaky_read"
        first = await client.call_tool(flaky, {"key": "probe"})
        second = await client.call_tool(flaky, {"key": "probe"})
        third = await client.call_tool(flaky, {"key": "probe"})
        if not first.is_error or not second.is_error or third.is_error:
            raise RuntimeError(
                "deterministic fail-first sequence did not produce error,error,success"
            )
        mutate = "mcp__specialist-fixture__mutate_once"
        applied = await client.call_tool(mutate, {"idempotency_key": "probe", "value": "x"})
        duplicate = await client.call_tool(mutate, {"idempotency_key": "probe", "value": "changed"})
        if applied.is_error or duplicate.is_error:
            raise RuntimeError("idempotency smoke calls unexpectedly failed")
    finally:
        await client.shutdown()
    return ReadinessCheck(
        check_id="fixture.mcp-smoke",
        status=CheckStatus.PASS,
        detail="stdio MCP startup, fail-first recovery and idempotent mutation passed",
    )


def _python_check() -> ReadinessCheck:
    supported = sys.version_info >= (3, 11)
    return ReadinessCheck(
        check_id="runtime.python",
        status=CheckStatus.PASS if supported else CheckStatus.FAIL,
        detail=f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    )


def _module_check(module: str, check_id: str) -> ReadinessCheck:
    present = importlib.util.find_spec(module) is not None
    return ReadinessCheck(
        check_id=check_id,
        status=CheckStatus.PASS if present else CheckStatus.FAIL,
        detail=f"module {module!r} {'is available' if present else 'is missing'}",
    )


def _executable_check(command: str, check_id: str) -> ReadinessCheck:
    resolved = shutil.which(command)
    return ReadinessCheck(
        check_id=check_id,
        status=CheckStatus.PASS if resolved else CheckStatus.FAIL,
        detail=f"resolved executable: {resolved}" if resolved else f"{command} is not on PATH",
    )


def _project_model_check(path: Path) -> ReadinessCheck:
    if not path.is_file():
        return ReadinessCheck(
            check_id="model.cc-harness-config",
            status=CheckStatus.FAIL,
            detail=f"missing project environment file: {path}",
        )
    values = dotenv_values(path)
    model = values.get("OPENAI_MODEL")
    base_url = values.get("OPENAI_BASE_URL")
    api_key = values.get("OPENAI_API_KEY")
    valid = model == PARITY_MODEL and bool(base_url) and bool(api_key)
    detail = (
        f"model={model!r}; route={'configured' if base_url else 'missing'}; "
        f"credential={'configured' if api_key else 'missing'}"
    )
    return ReadinessCheck(
        check_id="model.cc-harness-config",
        status=CheckStatus.PASS if valid else CheckStatus.FAIL,
        detail=detail,
    )


def _claude_settings_check(path: Path) -> ReadinessCheck:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        env = raw.get("env", {})
        if not isinstance(env, dict):
            raise TypeError("env must be an object")
        base_url = env.get("ANTHROPIC_BASE_URL")
        credential = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
        valid = bool(base_url) and bool(credential)
        detail = (
            f"settings={path}; route={'configured' if base_url else 'missing'}; "
            f"credential={'configured' if credential else 'missing'}"
        )
    except (OSError, TypeError, json.JSONDecodeError, ValueError) as exc:
        valid = False
        detail = f"invalid Claude settings at {path}: {exc}"
    return ReadinessCheck(
        check_id="model.claude-code-config",
        status=CheckStatus.PASS if valid else CheckStatus.FAIL,
        detail=detail,
    )


def _locomo_check(path: Path) -> ReadinessCheck:
    try:
        samples = verify_dataset(path)
        sample_ids = {str(sample.get("sample_id")) for sample in samples}
        expected = {
            task.dataset_sample_id
            for task in SPECIALIST_CATALOG.tasks
            if task.dataset_sample_id is not None
        }
        if sample_ids != expected:
            raise ValueError("LoCoMo sample ids do not match the frozen specialist catalog")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        detail = f"{len(samples)} samples; sha256:{digest}"
        status = CheckStatus.PASS
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        detail = f"LoCoMo validation failed: {exc}"
        status = CheckStatus.FAIL
    return ReadinessCheck(check_id="dataset.locomo", status=status, detail=detail)


def _disk_check(path: Path) -> ReadinessCheck:
    free = shutil.disk_usage(path).free
    gib = free / (1024**3)
    return ReadinessCheck(
        check_id="storage.specialist-output",
        status=CheckStatus.PASS if gib >= 20 else CheckStatus.WARN,
        detail=f"{gib:.1f} GiB free at {path}; specialist fixtures do not retain SWE-bench images",
    )


def _write_outputs(root: Path, report: SpecialistReadinessReport) -> None:
    catalog_path = root / "catalog.json"
    report_path = root / "readiness.json"
    markdown_path = root / "readiness.md"
    catalog_path.write_bytes(canonical_json_bytes(SPECIALIST_CATALOG))
    report_path.write_bytes(canonical_json_bytes(report))
    lines = [
        "# Specialist Eval Readiness",
        "",
        f"Ready: `{'yes' if report.ready else 'no'}`  ",
        f"Model: `{report.model}`  ",
        f"Catalog: `{report.catalog_digest}`",
        "",
        "| Check | Status | Detail |",
        "|---|---|---|",
    ]
    for check in report.checks:
        detail = check.detail.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| `{check.check_id}` | {check.status.value} | {detail} |")
    lines.extend(["", "## Frozen Task Counts", ""])
    for suite, count in sorted(report.task_counts.items()):
        lines.append(f"- `{suite}`: {count}")
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    integrity = {
        "schema_version": "eval.specialist-readiness-integrity.v1",
        "files": {
            path.name: f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
            for path in (catalog_path, report_path, markdown_path)
        },
    }
    (root / "integrity.json").write_bytes(canonical_json_bytes(integrity))
