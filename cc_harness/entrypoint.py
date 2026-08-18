"""Installed ``cc-harness`` command."""

from __future__ import annotations

import argparse
import asyncio
import json
import locale
import os
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from prompt_toolkit import PromptSession
from rich.console import Console

from cc_harness.config import ConfigError, load_layered_config
from cc_harness.runtime import SessionRuntime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="cc-harness terminal coding agent")
    parser.add_argument("prompt", nargs="?", help="Initial prompt")
    parser.add_argument(
        "-p",
        "--print",
        dest="print_mode",
        action="store_true",
        help="Print final response and exit",
    )
    parser.add_argument(
        "-c",
        "--continue",
        dest="continue_session",
        action="store_true",
        help="Continue latest session in this directory",
    )
    parser.add_argument(
        "-r",
        "--resume",
        nargs="?",
        const="picker",
        metavar="SESSION",
        help="Resume a session by id or select interactively",
    )
    parser.add_argument("--cwd", type=Path, default=None, help="Working directory")
    parser.add_argument(
        "--add-dir",
        type=Path,
        action="append",
        default=[],
        help="Add an accessible directory (repeatable)",
    )
    parser.add_argument(
        "--mode",
        choices=("coding", "plan", "design", "chat"),
        default=None,
        help="Session mode; when omitted, a resumed session keeps its saved mode",
    )
    parser.add_argument("--model", default=None)
    parser.add_argument("--effort", choices=("low", "medium", "high"), default=None)
    parser.add_argument(
        "--permission-mode", choices=("default", "auto-edit", "bypass-prompts"), default="default"
    )
    parser.add_argument(
        "--host-execution",
        action="store_true",
        help="Explicitly run commands on the host instead of the fail-closed sandbox",
    )
    parser.add_argument(
        "--bare",
        action="store_true",
        help="Deprecated alias for --capability-profile clean-coding",
    )
    parser.add_argument(
        "--capability-profile",
        choices=(
            "standard",
            "clean-coding",
            "benchmark-one-shot",
            "context-eval",
            "context-memory-control",
            "memory-eval",
            "hardened-safety",
        ),
        default=None,
        help="Explicit runtime capability profile",
    )
    parser.add_argument(
        "--sandbox-capabilities",
        action="store_true",
        help="Print the machine-readable sandbox capability profile and exit",
    )
    parser.add_argument("--verbose", action="store_true")
    iteration_group = parser.add_mutually_exclusive_group()
    iteration_group.add_argument(
        "--max-iterations",
        type=_iteration_limit,
        default=20,
        help="Maximum model/tool loop iterations for one turn (1-100)",
    )
    iteration_group.add_argument(
        "--unbounded-iterations",
        dest="max_iterations",
        action="store_const",
        const=None,
        help="Run without a model/tool loop limit; rely on an external watchdog",
    )
    parser.add_argument(
        "--output-format",
        choices=("text", "json"),
        default="text",
        help="Print-mode output format",
    )
    parser.add_argument("--tui", choices=("fullscreen", "default"), default=None)
    parser.add_argument("--lang", choices=("zh-CN", "en"), default=None)
    parser.add_argument("--repl", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--runtime",
        choices=("legacy", "durable"),
        default=(os.getenv("CC_HARNESS_RUNTIME") if os.getenv("CC_HARNESS_RUNTIME") in {"legacy", "durable"} else "durable"),
        help="execution runtime facade (durable is the rebuilt default; legacy is migration-only)",
    )
    parser.add_argument(
        "--command",
        choices=("run", "submit", "status", "list", "attach", "approve", "reject", "interrupt", "cancel", "resume", "follow-up", "rollback", "supervisor"),
        default=None,
    )
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--message", default=None)
    parser.add_argument("--approval-id", default=None)
    parser.add_argument("--action-args-digest", default=None)
    parser.add_argument("--data-root", type=Path, default=None, help=argparse.SUPPRESS)
    return parser


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


async def async_main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.sandbox_capabilities:
        from cc_harness.sandbox_capabilities import sandbox_capability_profile

        print(json.dumps(sandbox_capability_profile(), ensure_ascii=False, indent=2))
        return 0
    cwd = Path(args.cwd or Path.cwd()).resolve()
    if not cwd.is_dir():
        Console(stderr=True).print(f"[red]working directory not found:[/red] {cwd}")
        return 2
    if args.runtime == "durable":
        return await _run_durable(args, cwd)
    additional_dirs = [Path(p).resolve() for p in args.add_dir]
    missing_dirs = [p for p in additional_dirs if not p.is_dir()]
    if missing_dirs:
        Console(stderr=True).print(f"[red]additional directory not found:[/red] {missing_dirs[0]}")
        return 2

    interactive = not args.print_mode and sys.stdin.isatty() and sys.stdout.isatty()
    try:
        load_layered_config(cwd)
    except ConfigError as exc:
        if not interactive:
            Console(stderr=True, no_color=True).print(f"configuration error: {exc}")
            return 2
        try:
            await _setup_wizard(_language(args.lang))
        except ConfigError as setup_error:
            Console(stderr=True, no_color=True).print(f"configuration error: {setup_error}")
            return 2

    resume = "latest" if args.continue_session else args.resume
    if resume == "picker":
        resume = None
    runtime = await SessionRuntime.create(
        cwd,
        mode=args.mode,
        additional_dirs=additional_dirs,
        effort=args.effort,
        resume=resume,
        host_execution=args.host_execution,
        max_iterations=args.max_iterations,
        bare=args.bare,
        capability_profile=args.capability_profile,
    )
    if args.model:
        runtime.llm.model = args.model

    if args.repl:
        # The compatibility flag selects the inline renderer. It no longer
        # constructs a second agent stack through the legacy REPL function.
        from cc_harness.terminal.app import InlineTerminalApp

        return await InlineTerminalApp(
            runtime,
            lang=_language(args.lang),
            verbose=args.verbose,
            permission_mode=args.permission_mode,
        ).run(version=_version())

    piped = ""
    if not sys.stdin.isatty():
        piped = sys.stdin.read()
    initial = "\n".join(part for part in (piped.strip(), args.prompt or "") if part)
    if args.print_mode or not interactive:
        if not initial:
            Console(stderr=True, no_color=True).print("print mode requires a prompt or piped stdin")
            await runtime.close()
            return 2
        return await _run_print(runtime, initial, args.permission_mode, args.output_format)

    from cc_harness.terminal.settings import load_terminal_settings

    renderer_mode = args.tui or load_terminal_settings(cwd).tui
    if renderer_mode == "default":
        from cc_harness.terminal.app import InlineTerminalApp as TerminalApp
    else:
        from cc_harness.terminal.fullscreen import FullscreenTerminalApp as TerminalApp
    app = TerminalApp(
        runtime,
        lang=_language(args.lang),
        verbose=args.verbose,
        permission_mode=args.permission_mode,
        **({"version": _version()} if renderer_mode != "default" else {}),
    )
    if args.resume == "picker":
        if renderer_mode == "default":
            await app._resume_picker()
        else:
            app.open_resume_on_start = True
    if args.prompt:
        from cc_harness.terminal.app import QueuedInput

        app.queue.append(QueuedInput(args.prompt))
    return await app.run(version=_version())


async def _run_durable(args, cwd: Path) -> int:
    from cc_harness.durable_runtime import DurableRuntimeClient
    from cc_harness.repl import run_durable_repl

    client = await DurableRuntimeClient.create(cwd, data_root=args.data_root)
    try:
        command = args.command
        if command == "supervisor":
            await client.run_supervisor_forever(reasoning_effort=args.effort)
            return 0
        if command is None and sys.stdin.isatty():
            await client.start_supervisor(reasoning_effort=args.effort)
            await run_durable_repl(client, initial_prompt=args.prompt)
            return 0
        if command is None and args.prompt:
            command = "run"
        if command in {"run", "submit"}:
            objective = args.prompt or args.message
            if not objective:
                Console(stderr=True, no_color=True).print("durable run requires a prompt")
                return 2
            run_id = await client.submit(objective)
            print(json.dumps({"run_id": run_id}, ensure_ascii=False))
            return 0
        if command in {"status", "attach"}:
            if not args.run_id:
                Console(stderr=True, no_color=True).print("this durable command requires --run-id")
                return 2
            view = await client.coordinator.inspect(str(args.run_id))
            print(json.dumps({"run_id": view.run_id, "status": view.status.value, "sequence": view.sequence}, ensure_ascii=False))
            return 0
        if command == "list":
            views = await client.coordinator.list()
            print(json.dumps([{"run_id": view.run_id, "status": view.status.value, "sequence": view.sequence} for view in views], ensure_ascii=False))
            return 0
        if not args.run_id:
            Console(stderr=True, no_color=True).print("this durable command requires --run-id")
            return 2
        if command == "follow-up":
            receipt = await client.coordinator.send(args.run_id, args.message or args.prompt or "")
            print(json.dumps({"run_id": receipt.run_id, "follow_up_run_id": receipt.follow_up_run_id, "sequence": receipt.sequence}, ensure_ascii=False))
        elif command == "interrupt":
            receipt = await client.coordinator.interrupt(args.run_id, args.message or "client interrupt")
            print(json.dumps({"run_id": receipt.run_id, "status": receipt.status.value, "sequence": receipt.sequence}, ensure_ascii=False))
        elif command == "cancel":
            receipt = await client.coordinator.cancel(args.run_id, args.message or "client cancel")
            print(json.dumps({"run_id": receipt.run_id, "status": receipt.status.value, "sequence": receipt.sequence}, ensure_ascii=False))
        elif command == "resume":
            receipt = await client.coordinator.resume(args.run_id, args.message or "client resume")
            print(json.dumps({"run_id": receipt.run_id, "status": receipt.status.value, "sequence": receipt.sequence}, ensure_ascii=False))
        elif command == "rollback":
            receipt = await client.coordinator.rollback(args.run_id, args.message or "client rollback")
            print(json.dumps({"run_id": receipt.run_id, "status": receipt.status.value, "sequence": receipt.sequence}, ensure_ascii=False))
        elif command == "approve":
            if not args.approval_id or not args.action_args_digest:
                Console(stderr=True, no_color=True).print("approve requires --approval-id and --action-args-digest")
                return 2
            decision = await client.coordinator.approve(run_id=args.run_id, approval_id=str(args.approval_id), action_args_digest=str(args.action_args_digest))
            print(json.dumps({"run_id": decision.run_id, "approval_id": decision.approval_id, "status": decision.status}, ensure_ascii=False))
        elif command == "reject":
            if not args.approval_id:
                Console(stderr=True, no_color=True).print("reject requires --approval-id")
                return 2
            decision = await client.coordinator.reject(run_id=args.run_id, approval_id=str(args.approval_id), reason=args.message or "client rejected")
            print(json.dumps({"run_id": decision.run_id, "approval_id": decision.approval_id, "status": decision.status}, ensure_ascii=False))
        else:
            Console(stderr=True, no_color=True).print(f"unsupported durable command: {command}")
            return 2
        return 0
    finally:
        await client.close()


async def _run_print(
    runtime: SessionRuntime,
    prompt_text: str,
    permission_mode: str,
    output_format: str = "text",
) -> int:
    from cc_harness.terminal.attachments import AttachmentError, AttachmentManager

    final = ""
    events: list[dict] = []

    async def emit(event: dict) -> None:
        nonlocal final
        events.append(event)
        if event.get("type") == "result":
            final = str(event.get("text", ""))

    async def confirm(tool: str, args: dict, reason: str) -> str:
        del tool, args, reason
        return "yes" if permission_mode == "bypass-prompts" else "no"

    try:
        manager = AttachmentManager(
            runtime.cwd,
            runtime.additional_dirs,
            runtime.session_store.attachments_root / runtime.state.session_id,
        )

        async def reject_outside(_path: Path) -> bool:
            return False

        try:
            message_content, _attachments = await manager.prepare(
                prompt_text,
                confirm_outside=reject_outside,
            )
        except AttachmentError as exc:
            Console(stderr=True, no_color=True).print(str(exc))
            return 2
        stats = await runtime.run_user_turn(
            prompt_text,
            message_content=message_content,
            event_emitter=emit,
            confirm_handler=confirm,
        )
        error = getattr(stats, "error", None) if stats is not None else None
        if output_format == "json":
            _write_print_json(runtime, final, stats, error=error, events=events)
        if error:
            if output_format != "json":
                Console(stderr=True, no_color=True).print(error)
            return 1
        if output_format != "json":
            Console(no_color=True, force_terminal=False).print(final, markup=False)
        return 0
    finally:
        await runtime.close()


def _write_print_json(
    runtime: SessionRuntime,
    final: str,
    stats,
    *,
    error: str | None,
    events: list[dict],
) -> None:
    payload = {
        "schema_version": "cc-harness.print-result.v1",
        "type": "result",
        "text": final,
        "requested_model": runtime.llm.model,
        "resolved_model": runtime.llm.resolved_model,
        "error": error,
        "trajectory": events,
        "usage": {
            "input_tokens": int(getattr(stats, "api_prompt_tokens", 0) or 0),
            "uncached_input_tokens": int(
                getattr(
                    stats,
                    "api_uncached_prompt_tokens",
                    getattr(stats, "api_prompt_tokens", 0),
                )
                or 0
            ),
            "cache_creation_input_tokens": int(
                getattr(stats, "api_cache_creation_prompt_tokens", 0) or 0
            ),
            "cache_read_input_tokens": int(getattr(stats, "api_cache_read_prompt_tokens", 0) or 0),
            "output_tokens": int(getattr(stats, "api_completion_tokens", 0) or 0),
            "model_calls": int(getattr(stats, "iter_count", 0) or 0)
            + int(getattr(stats, "auxiliary_model_calls", 0) or 0),
            "tool_calls": len(getattr(stats, "tool_call_log", []) or []),
            "cost_microusd": None,
        },
    }
    encoded = (json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(encoded)
        buffer.flush()
    else:
        sys.stdout.write(encoded.decode("utf-8"))


async def _setup_wizard(lang: str) -> None:
    zh = lang.startswith("zh")
    console = Console()
    console.print("[bold color(208)]cc-harness setup[/bold color(208)]")
    session = PromptSession()
    base_url = await session.prompt_async("API Base URL: ")
    model = await session.prompt_async("Model: ")
    api_key = await session.prompt_async("API Key: ", is_password=True)
    if not all(value.strip() for value in (base_url, model, api_key)):
        raise ConfigError("setup cancelled: all model fields are required")
    user_dir = Path.home() / ".cc-harness"
    user_dir.mkdir(parents=True, exist_ok=True)
    env_path = user_dir / ".env"
    env_path.write_text(
        f"OPENAI_BASE_URL={base_url.strip()}\n"
        f"OPENAI_MODEL={model.strip()}\n"
        f"OPENAI_API_KEY={api_key.strip()}\n",
        encoding="utf-8",
    )
    try:
        env_path.chmod(0o600)
    except OSError:
        pass
    console.print("[green]配置已保存。[/green]" if zh else "[green]Configuration saved.[/green]")


def _language(explicit: str | None) -> str:
    if explicit:
        return explicit
    configured = os.getenv("CC_HARNESS_LANG")
    if configured in ("zh-CN", "en"):
        return configured
    current = locale.getlocale()[0] or ""
    return "zh-CN" if current.lower().startswith("zh") else "en"


def _iteration_limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("max iterations must be an integer") from exc
    if not 1 <= parsed <= 100:
        raise argparse.ArgumentTypeError("max iterations must be between 1 and 100")
    return parsed


def _version() -> str:
    try:
        return version("cc-harness")
    except PackageNotFoundError:
        return "0.1.0"


if __name__ == "__main__":
    main()
