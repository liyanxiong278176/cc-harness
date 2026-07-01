# cc_harness/repl.py
"""Multi-turn REPL with sticky mode switching via slash commands.

Slash commands (sticky across the session):
  /plan, /design, /coding  — switch active mode
  /mode                    — show current mode
  /help                    — list all commands
  /clear                   — clear messages, keep system prompt
  exit, quit, Ctrl+C/D     — leave the REPL

The prompt prefix shows the current mode (`>` / `> [plan] ` / `> [design] `).
System prompt is refreshed at messages[0] on every turn to reflect the mode.
"""
from __future__ import annotations
import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from openai import AsyncOpenAI
from rich.console import Console
from cc_harness.audit import log_decision
from cc_harness.config import load_l2_config, load_policy_config
from cc_harness.l2 import REFUSAL_TEMPLATE, scan_user_input
from cc_harness.policy import PolicyEngine
from cc_harness.render import print_info, print_result, print_warn, print_token_summary
from cc_harness.tokens import TokenCounter, SessionTokenStats

_VALID_MODES = ("coding", "plan", "design")

# How far back to scan for disk changes after an LLM turn.
_DISK_CHANGE_WINDOW_S = 30
# Files this size or smaller get a content preview printed under their entry.
_PREVIEW_MAX_BYTES = 500
# Max number of changed files to print (most recent first).
_MAX_CHANGES_SHOWN = 10

_HELP_TEXT = """\
可用命令:
  /plan, /design, /coding  切换粘性模式(后续消息以该模式处理)
  /mode                    查看当前模式
  /help                    显示本帮助
  /clear                   清空会话历史(保留 system 消息)
  exit, quit               退出(cc-harness)
"""


@dataclass
class ReplState:
    mode: str = "coding"
    messages: list[dict] = field(default_factory=list)
    session_stats: SessionTokenStats = field(default_factory=SessionTokenStats)
    token_counter: TokenCounter = field(default_factory=TokenCounter)


async def _read_user(prompt: str) -> str:
    """Block on input() in a worker thread so the event loop stays responsive."""
    return await asyncio.to_thread(input, prompt)


def _prompt_for(mode: str) -> str:
    """Return the REPL prompt prefix for the current mode.

    All three modes are tagged ("> [coding] " / "> [plan] " / "> [design] ")
    so the active mode is always visible at a glance.
    """
    return f"> [{mode}] "


def _handle_slash(cmd: str, state: ReplState, console: Console) -> bool:
    """Dispatch a slash command. Returns True if handled, False if the
    input should fall through to the LLM as a normal message.

    Commands are case-insensitive: /PLAN == /plan == /Plan.
    """
    cmd = cmd.lower()
    if cmd in ("/plan", "/design", "/coding"):
        new_mode = cmd[1:]
        if state.mode == new_mode:
            print_info(console, f"已经在 {new_mode} 模式")
        else:
            state.mode = new_mode
            # Use markup=False to prevent Rich from misinterpreting '[plan]' as a style.
            console.print(
                f"✓ 切换到 {new_mode} 模式 — 提示符现在是: {_prompt_for(new_mode).rstrip()}",
                markup=False,
            )
        return True
    if cmd == "/mode":
        print_info(console, f"当前: {state.mode}")
        return True
    if cmd == "/help":
        print_info(console, _HELP_TEXT)
        return True
    if cmd == "/clear":
        kept = [m for m in state.messages if m.get("role") == "system"]
        dropped = len(state.messages) - len(kept)
        state.messages = kept
        print_info(console, f"✓ 会话已清空(system 消息保留,丢弃 {dropped} 条历史)")
        return True
    return False


async def run_repl(
    llm,
    mcp,
    *,
    max_iter: int = 20,
    cwd: str,
    default_mode: str = "coding",
    design_dir: Path | None = None,
) -> None:
    """Run the interactive REPL.

    `cwd` is passed to run_turn so the system prompt at messages[0] can be
    refreshed per mode. `default_mode` is the initial sticky mode (also
    available via /plan /design /coding at runtime). `design_dir` is
    where design-mode outputs get persisted (default: ~/.cc-harness/designs/).
    """
    if default_mode not in _VALID_MODES:
        raise ValueError(
            f"unknown default_mode: {default_mode!r} (expected one of {_VALID_MODES})"
        )

    console = Console()
    state = ReplState(mode=default_mode)

    # Construct ONE PolicyEngine for the whole session. policy.yaml is optional
    # (missing → default enabled=True). project_root is the REPL's cwd so path
    # containment checks anchor to the project, not wherever the process drifts.
    policy_cfg = load_policy_config(Path("policy.yaml"))
    policy = PolicyEngine(project_root=Path(cwd), enabled=policy_cfg.enabled)

    # L2 输入防御:heuristic 命中即 BLOCK(不走 judge);否则 judge 判。
    # client 仅在 l2 启用 且 有 API key 时构造(空 key 时 SDK 会抛 OpenAIError;
    # heuristic 不需要 client,无 key 时仍可作为第一道防线)。
    l2_cfg = load_l2_config(Path("policy.yaml"))
    l2_api_key = os.getenv("OPENAI_API_KEY")
    l2_client = (
        AsyncOpenAI(api_key=l2_api_key, base_url=os.getenv("OPENAI_BASE_URL"))
        if l2_cfg.enabled and l2_api_key
        else None
    )
    l2_model = os.getenv("JUDGE_MODEL") or os.getenv("OPENAI_MODEL") or ""
    l2_audit_path = Path(cwd) / "logs" / "l2.jsonl"

    n_tools = len(mcp.list_tools())
    print_info(console, "")
    print_info(console, f"  cc-harness ready  |  tools: {n_tools}  |  mode: {default_mode.upper()}")
    print_info(console, f"  prompt: {_prompt_for(default_mode).rstrip()}")
    print_info(console, "  type 'exit' or 'quit' to leave, Ctrl+C / Ctrl+D also works; /help for commands")
    print_info(console, "")

    while True:
        try:
            raw = (await _read_user(_prompt_for(state.mode))).strip()
        except (EOFError, KeyboardInterrupt):
            print_token_summary(console, "session 总计", state.session_stats)
            print_info(console, "shutting down")
            break

        if not raw:
            continue
        if raw.lower() in ("exit", "quit"):
            print_token_summary(console, "session 总计", state.session_stats)
            print_info(console, "shutting down")
            break

        # Slash command dispatch
        if raw.startswith("/"):
            if _handle_slash(raw, state, console):
                continue
            # Unknown slash command — warn but let it through to the LLM
            print_warn(console, f"未知命令: {raw!r}(当作普通消息处理)")

        # L2 输入防御:命中即阻断,不进 run_turn、不入历史
        if l2_cfg.enabled:
            scan = await scan_user_input(
                raw, l2_cfg=l2_cfg, client=l2_client, model=l2_model,
            )
            if not scan.allowed:
                import hashlib
                log_decision(
                    l2_audit_path,
                    iter_n=state.session_stats.turns, tool="user_input",
                    args={"input_hash": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]},
                    action="l2_block", outcome="blocked",
                    rule_id=scan.reason, reason="", mode=state.mode,
                )
                print_result(console, REFUSAL_TEMPLATE)  # 走 print_result → 带 结果: 头
                continue                                   # 不 append、不 run_turn
            user_content = scan.wrapped_text
        else:
            user_content = raw

        state.messages.append({"role": "user", "content": user_content})
        turn_start = time.time()
        from cc_harness.agent import run_turn
        turn_stats = await run_turn(
            state.messages, llm, mcp,
            max_iter=max_iter,
            mode=state.mode,
            cwd=cwd,
            design_dir=design_dir,
            token_counter=state.token_counter,
            policy=policy,
        )
        state.session_stats.add(turn_stats)

        # 打印 token 明细
        print_token_summary(console, "本轮", turn_stats)
        print_token_summary(console, f"累计 {state.session_stats.turns} 轮", state.session_stats)

        # After the turn, show what actually changed on disk — so the user
        # can see real file state without F5-ing their file manager.
        _print_disk_changes(console, cwd, since=turn_start)


# --- Disk change summary (printed after each LLM turn) ---

def _collect_disk_changes(cwd: str, since: float) -> list[tuple[str, int, float, str | None]]:
    """Walk cwd, return files modified after `since` (most recent first).

    Each entry: (relative_path, size_bytes, mtime, content_preview_or_None).
    Content preview is included for small text files (<=_PREVIEW_MAX_BYTES).
    """
    root = Path(cwd)
    if not root.exists():
        return []
    results: list[tuple[str, int, float, str | None]] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            stat = p.stat()
        except OSError:
            continue
        if stat.st_mtime < since:
            continue
        preview: str | None = None
        if 0 < stat.st_size <= _PREVIEW_MAX_BYTES:
            try:
                with p.open("rb") as f:
                    raw = f.read()
                preview = raw.decode("utf-8", errors="replace")
            except OSError:
                preview = None
        try:
            rel = p.relative_to(root)
        except ValueError:
            rel = p
        results.append((str(rel), stat.st_size, stat.st_mtime, preview))
    results.sort(key=lambda r: -r[2])
    return results[:_MAX_CHANGES_SHOWN]


def _print_disk_changes(console: Console, cwd: str, since: float) -> None:
    """After a turn, show what actually changed on disk (relative to `since`)."""
    changes = _collect_disk_changes(cwd, since)
    if not changes:
        return
    print_info(console, "")
    # Use markup=False to avoid Rich interpreting [plan]/[design]/[coding] in
    # file contents as style markers, and to preserve escape sequences literally.
    console.print(
        f"📁 这一轮磁盘改动(最近 {_DISK_CHANGE_WINDOW_S}s 内):",
        markup=False,
    )
    for rel, size, mtime, preview in changes:
        ago = max(0, int(time.time() - mtime))
        console.print(f"  • {rel}  ({size}B, {ago}s ago)", markup=False)
        if preview is not None:
            for pl in (preview.splitlines() or [""]):
                console.print(f"      {pl}", markup=False)
