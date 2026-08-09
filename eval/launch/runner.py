"""Bounded subprocess runner and structured-output parsers."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from .models import HarnessKind, LaunchEvidence, LaunchProfile
from .pricing import PARITY_PRICING
from .profiles import LaunchInvocation


@dataclass(frozen=True)
class CompletedLaunch:
    evidence: LaunchEvidence
    stdout: bytes
    stderr: bytes


async def run_invocation(
    profile: LaunchProfile,
    invocation: LaunchInvocation,
    *,
    timeout_seconds: float,
) -> CompletedLaunch:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *invocation.argv,
        cwd=invocation.cwd,
        env=invocation.environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        start_new_session=os.name != "nt",
    )
    assert process.stdout is not None and process.stderr is not None
    assert process.stdin is not None
    stdout_task = asyncio.create_task(_read_bounded(process.stdout, profile.max_stdout_bytes))
    stderr_task = asyncio.create_task(_read_bounded(process.stderr, profile.max_stderr_bytes))
    timed_out = False
    try:
        async with asyncio.timeout(timeout_seconds):
            try:
                process.stdin.write(invocation.stdin)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()
            await process.wait()
    except TimeoutError:
        timed_out = True
        await _terminate_process_tree(process)
    except asyncio.CancelledError:
        await _terminate_process_tree(process)
        for task in (stdout_task, stderr_task):
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        raise
    await process.wait()
    stdout, stdout_truncated = await stdout_task
    stderr, stderr_truncated = await stderr_task
    elapsed = round((time.monotonic() - started) * 1000)
    parsed, parse_error = _parse_output(profile.harness, stdout)
    reported_cost = _optional_nonnegative_int(parsed.get("cost_microusd"))
    normalized_cost = None
    pricing_digest = None
    cost_source = None
    if parsed.get("resolved_model") == PARITY_PRICING.model:
        normalized_cost = PARITY_PRICING.cost_microusd(
            uncached_input_tokens=_nonnegative_int(
                parsed.get("uncached_input_tokens", parsed.get("input_tokens"))
            ),
            cache_creation_input_tokens=_nonnegative_int(
                parsed.get("cache_creation_input_tokens")
            ),
            cache_read_input_tokens=_nonnegative_int(parsed.get("cache_read_input_tokens")),
            output_tokens=_nonnegative_int(parsed.get("output_tokens")),
        )
        pricing_digest = PARITY_PRICING.digest
        cost_source = "normalized_tariff"
        if reported_cost is not None and abs(reported_cost - normalized_cost) > 1:
            parse_error = (
                f"reported cost {reported_cost} does not match frozen tariff "
                f"cost {normalized_cost}"
            )
    evidence = LaunchEvidence(
        harness=profile.harness,
        requested_model=profile.requested_model,
        resolved_model=_string(parsed.get("resolved_model")),
        exit_code=int(process.returncode if process.returncode is not None else -1),
        timed_out=timed_out,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        wall_time_ms=elapsed,
        model_calls=_nonnegative_int(parsed.get("model_calls")),
        tool_calls=_nonnegative_int(parsed.get("tool_calls")),
        input_tokens=_nonnegative_int(parsed.get("input_tokens")),
        uncached_input_tokens=_nonnegative_int(
            parsed.get("uncached_input_tokens", parsed.get("input_tokens"))
        ),
        cache_creation_input_tokens=_nonnegative_int(
            parsed.get("cache_creation_input_tokens")
        ),
        cache_read_input_tokens=_nonnegative_int(parsed.get("cache_read_input_tokens")),
        output_tokens=_nonnegative_int(parsed.get("output_tokens")),
        cost_microusd=normalized_cost,
        reported_cost_microusd=reported_cost,
        cost_source=cost_source,
        pricing_contract_digest=pricing_digest,
        parse_error=parse_error,
    )
    return CompletedLaunch(evidence=evidence, stdout=stdout, stderr=stderr)


async def _terminate_process_tree(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        with suppress(OSError):
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
    else:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.kill()
    await process.wait()


async def _read_bounded(reader: asyncio.StreamReader, limit: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    retained = 0
    truncated = False
    while chunk := await reader.read(64 * 1024):
        room = max(0, limit - retained)
        if room:
            chunks.append(chunk[:room])
            retained += min(room, len(chunk))
        if len(chunk) > room:
            truncated = True
    return b"".join(chunks), truncated


def parse_launch_output(harness: HarnessKind, stdout: bytes) -> dict[str, Any]:
    parsed, error = _parse_output(harness, stdout)
    if error is not None:
        raise ValueError(error)
    return parsed


def _parse_output(harness: HarnessKind, stdout: bytes) -> tuple[dict[str, Any], str | None]:
    try:
        text = stdout.decode("utf-8")
        documents = [json.loads(line) for line in text.splitlines() if line.strip()]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {}, f"structured output is invalid JSONL: {exc}"
    if not documents or not all(isinstance(item, dict) for item in documents):
        return {}, "structured output contains no JSON objects"
    try:
        if harness is HarnessKind.CC_HARNESS:
            result = documents[-1]
            if result.get("schema_version") != "cc-harness.print-result.v1":
                raise ValueError("cc-harness result schema is missing")
            error = result.get("error")
            if isinstance(error, str) and error:
                raise ValueError(f"cc-harness result error: {error}")
            usage = result.get("usage") or {}
            return {
                "resolved_model": result.get("resolved_model"),
                "model_calls": usage.get("model_calls", 0),
                "tool_calls": usage.get("tool_calls", 0),
                "input_tokens": usage.get("input_tokens", 0),
                "uncached_input_tokens": usage.get(
                    "uncached_input_tokens", usage.get("input_tokens", 0)
                ),
                "cache_creation_input_tokens": usage.get(
                    "cache_creation_input_tokens", 0
                ),
                "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
            }, None
        if harness is HarnessKind.CLAUDE_CODE:
            return _parse_claude_stream(documents), None
        return _parse_event_stream(documents), None
    except (TypeError, ValueError) as exc:
        return {}, str(exc)


def _parse_event_stream(documents: list[dict[str, Any]]) -> dict[str, Any]:
    models: list[str] = []
    usage: dict[str, int] = {}
    for document in documents:
        for key, value in _walk(document):
            normalized = key.lower()
            if normalized in {"model", "resolved_model", "model_name"} and isinstance(value, str):
                models.append(value)
            elif normalized in {"input_tokens", "prompt_tokens"}:
                usage["input_tokens"] = max(usage.get("input_tokens", 0), _nonnegative_int(value))
            elif normalized in {"output_tokens", "completion_tokens"}:
                usage["output_tokens"] = max(usage.get("output_tokens", 0), _nonnegative_int(value))
            elif normalized == "tool_calls":
                usage["tool_calls"] = max(usage.get("tool_calls", 0), _count_or_int(value))
            elif normalized in {"model_calls", "num_turns"}:
                usage["model_calls"] = max(usage.get("model_calls", 0), _nonnegative_int(value))
            elif normalized in {"total_cost_usd", "cost_usd"} and isinstance(value, (int, float)):
                usage["cost_microusd"] = max(0, round(float(value) * 1_000_000))
    distinct = list(dict.fromkeys(models))
    if not distinct:
        raise ValueError("structured output did not report a resolved model")
    if len(distinct) != 1:
        raise ValueError(f"structured output reported conflicting models: {distinct}")
    return {"resolved_model": distinct[0], **usage}


def _parse_claude_stream(documents: list[dict[str, Any]]) -> dict[str, Any]:
    assistant_models: list[str] = []
    tool_use_ids: set[str] = set()
    anonymous_tool_uses = 0
    for document in documents:
        if document.get("type") != "assistant":
            continue
        error = document.get("error")
        if isinstance(error, str) and error:
            raise ValueError(f"Claude Code assistant error: {error}")
        message = document.get("message")
        if isinstance(message, dict):
            model = message.get("model")
            if isinstance(model, str) and model and model != "<synthetic>":
                assistant_models.append(model)
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    tool_use_id = block.get("id")
                    if isinstance(tool_use_id, str) and tool_use_id:
                        tool_use_ids.add(tool_use_id)
                    else:
                        anonymous_tool_uses += 1
        model = document.get("model")
        if isinstance(model, str) and model and model != "<synthetic>":
            assistant_models.append(model)
    distinct = list(dict.fromkeys(assistant_models))
    if not distinct:
        raise ValueError("Claude Code stream did not report a resolved assistant model")
    if len(distinct) != 1:
        raise ValueError(f"Claude Code stream reported conflicting assistant models: {distinct}")
    usage = _parse_event_stream([{**item, "model": distinct[0]} for item in documents])
    usage.update(_claude_terminal_usage(documents))
    observed_tool_calls = len(tool_use_ids) + anonymous_tool_uses
    usage["tool_calls"] = max(usage.get("tool_calls", 0), observed_tool_calls)
    return {**usage, "resolved_model": distinct[0]}


def _claude_terminal_usage(documents: list[dict[str, Any]]) -> dict[str, int]:
    terminal = next(
        (document for document in reversed(documents) if document.get("type") == "result"),
        None,
    )
    if terminal is None:
        return {}
    usage = terminal.get("usage")
    parsed: dict[str, int] = {}
    if isinstance(usage, dict):
        parsed["uncached_input_tokens"] = _nonnegative_int(usage.get("input_tokens", 0))
        parsed["cache_creation_input_tokens"] = _nonnegative_int(
            usage.get("cache_creation_input_tokens", 0)
        )
        parsed["cache_read_input_tokens"] = _nonnegative_int(
            usage.get("cache_read_input_tokens", 0)
        )
        parsed["input_tokens"] = sum(
            parsed[name]
            for name in (
                "uncached_input_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            )
        )
        parsed["output_tokens"] = _nonnegative_int(usage.get("output_tokens", 0))
    if "num_turns" in terminal:
        parsed["model_calls"] = _nonnegative_int(terminal["num_turns"])
    total_cost = terminal.get("total_cost_usd")
    if isinstance(total_cost, (int, float)):
        parsed["cost_microusd"] = max(0, round(float(total_cost) * 1_000_000))
    return parsed


def _walk(value: Any):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key), item
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _optional_nonnegative_int(value: Any) -> int | None:
    return None if value is None else _nonnegative_int(value)


def _count_or_int(value: Any) -> int:
    return len(value) if isinstance(value, list) else _nonnegative_int(value)
