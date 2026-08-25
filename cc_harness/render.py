"""Plain-text renderers for the cc-harness REPL — 4-phase ReAct format.

Output structure (per the user spec):
  思考: <LLM's full reasoning text for this iteration>
  行动: <tool_name>
    arg1: val1
    arg2: val2
  观察: <tool's actual result text — what the LLM sees>
  [loop, until no more tool calls]
  结果: <LLM's final answer>

Design rules:
  - No colors. Only unicode glyphs (⚠ ✗) for visual structure.
  - 思考 is the COMPLETE text the LLM emitted for that iteration (no
    truncation, no filtering — the user explicitly asked for "all text").
  - 观察 shows the raw tool result the LLM receives — this is the data
    the LLM reasons about. (Earlier design hid this; the new 4-phase
    ReAct format requires showing it.)
  - All four labels (思考 / 行动 / 观察 / 结果) are printed with the
    content on the same line, separated from each other by a blank line
    for visual clarity.
  - Each non-streaming phase is preceded by a blank line so blocks are
    visually separated (printed via _blank() which writes "\\n\\n" +
    flush; the leading double newline avoids the Rich print() bug where
    calling print() after end="" only emits 1 \\n).
  - All interactive print functions call console.file.flush() so streaming
    output is visible in real-time even when stdout is piped.
"""
from __future__ import annotations

import json

from rich.console import Console


def _safe_console_text(console: Console, text: str) -> str:
    """Make user/model text safe for legacy Windows console encodings.

    Rich ultimately writes to ``console.file``.  On some Windows setups that
    stream still advertises ``gbk``/``cp936``; writing our intentional warning
    and error glyphs (or a model response containing Unicode) then raises
    ``UnicodeEncodeError`` and can abort a benchmark phase.  Keep the full
    Unicode text for UTF-8/StringIO streams, but replace only characters the
    target stream cannot represent.
    """
    encoding = getattr(getattr(console, "file", None), "encoding", None)
    if not isinstance(encoding, str) or not encoding:
        return text
    try:
        text.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        try:
            return text.encode(encoding, errors="replace").decode(
                encoding, errors="replace"
            )
        except (LookupError, UnicodeError):
            # A custom stream may expose an invalid encoding name.  Let Rich
            # handle the original value rather than hiding the actual output.
            return text
    return text


def _print(console: Console, text: str, **kwargs) -> None:
    """Print text without allowing a narrow console encoding to crash us."""
    console.print(_safe_console_text(console, text), **kwargs)


def _flush(console: Console) -> None:
    f = console.file
    if f is not None:
        try:
            f.flush()
        except (AttributeError, OSError):
            pass


def _blank(console: Console) -> None:
    """Force a blank line (one \\n) to the underlying file. We bypass Rich's
    print() because (a) Rich's print() with no args only emits 1 \\n when the
    cursor is mid-line, and (b) we want the blank line written to the
    underlying file (not via Rich's machinery) for full control."""
    f = console.file
    if f is not None:
        try:
            f.write("\n")
            f.flush()
        except (AttributeError, OSError):
            pass


def print_thought(console: Console, text: str) -> None:
    """Print '思考: <text>' — the LLM's full reasoning for this iteration.

    Per user spec: the COMPLETE text the LLM emitted, no truncation.
    A leading blank line separates it from the prior phase.

    Finding 8 fix:`text` 是 LLM 产生,untrusted。markup=False + highlight=False
    防 Rich 把内容里的 `[bold]` / `[red]` / markdown 误当成样式/token 高亮。
    """
    _blank(console)
    _print(console, f"思考: {text}", markup=False, highlight=False)
    _flush(console)


def print_action(console: Console, name: str, arguments: dict) -> None:
    """Print '行动: <name>' with one argument per line, blank line before.

    Finding 8 fix:`arguments` 来自 LLM 工具调用,untrusted。markup=False
    防 `[red]` 等样式逃逸。
    """
    _blank(console)
    _print(console, f"行动: {name}", markup=False, highlight=False)
    if arguments:
        for k, v in arguments.items():
            val_repr = json.dumps(v, ensure_ascii=False)
            # markup=False stops Rich from interpreting [bracket] in JSON values
            _print(console, f"  {k}: {val_repr}", markup=False, highlight=False)
    _flush(console)


def print_observation(console: Console, text: str) -> None:
    """Print '观察: <text>' — the tool's actual result (what the LLM sees).

    The text should be the same string the LLM receives in the messages
    (i.e. ToolResult.llm_text, which includes "[Tool Error] ..." prefix for
    errors). For multi-line results (e.g. file contents), each line is
    indented under the label for readability.

    Finding 8 fix:tool output 是 untrusted 外部数据,pass markup=False +
    highlight=False 防 Rich 解释 `[red]` / `[bold]` 样式 / token 高亮污染终端。
    """
    _blank(console)
    _print(console, "观察:", markup=False, highlight=False)
    for line in (text or "").splitlines() or [""]:
        _print(console, f"  {line}", markup=False, highlight=False)
    _flush(console)


def print_result(console: Console, text: str) -> None:
    """Print '结果: <text>' — the LLM's final answer, full text, with a
    blank line before.

    Finding 8 fix:`text` 是 LLM 最终输出,untrusted,markup=False 防样式逃逸。
    """
    _blank(console)
    _print(console, f"结果: {text}", markup=False, highlight=False)
    _flush(console)


def print_warn(console: Console, text: str) -> None:
    # Finding 8 fix:warn text 可能含用户输入拼接(unknown slash cmd 等),markup=False。
    _print(console, f"⚠ {text}", markup=False, highlight=False)
    _flush(console)


def print_error(console: Console, text: str) -> None:
    _print(console, f"✗ {text}", markup=False, highlight=False)
    _flush(console)


def print_info(console: Console, text: str) -> None:
    # Finding 8 fix:info text 可能含 untrusted 来源(LLM 输出 / 文件内容 / 用户输入),
    # markup=False 防样式逃逸。
    _print(console, text, markup=False, highlight=False)
    _flush(console)


def print_token_summary(console: Console, label: str, stats) -> None:
    """Print a one-line token breakdown for a turn or session.

    `label` is the prefix, e.g. '本轮' / '累计 3 轮' / 'session 总计'.
    `stats` is either a TurnTokenStats or SessionTokenStats (both have the
    5-category fields: user_input / tool_calls / llm_output / system_prompt /
    tool_definitions, plus api_total_tokens).

    The ``summary`` bucket (Plan3) is rendered only when > 0, preserving
    backward-compatibility with the original 5-bucket layout for turns that
    have no compaction summary.
    """
    _blank(console)
    sub = stats.breakdown_subtotal
    line = (
        f"{label}  "
        f"用户输入 {stats.user_input}  "
        f"工具调用 {stats.tool_calls}  "
        f"LLM 输出 {stats.llm_output}  "
    )
    # Plan3: summary bucket only when > 0 (backward-compat with 5-bucket format)
    summary = getattr(stats, "summary", 0)
    if summary:
        line += f"摘要 {summary}  "
    line += (
        f"系统 {stats.system_prompt}  "
        f"工具定义 {stats.tool_definitions}  "
        f"= {sub}"
    )
    _print(console, line)
    # API delta (only meaningful when api_total_tokens > 0)
    if getattr(stats, "api_total_tokens", 0):
        delta = sub - stats.api_total_tokens
        pct = 100.0 * delta / stats.api_total_tokens
        _print(
            console,
            f"        API 报告 {stats.api_total_tokens}  "
            f"差 {delta:+d} ({pct:+.1f}%)",
            highlight=False,
        )
    # Warning if this turn's API didn't report usage
    if hasattr(stats, "api_reported") and not stats.api_reported:
        _print(
            console,
            "⚠ 本轮后端未报告 token(可能未实现 stream_options.include_usage)",
            highlight=False,
        )
    _flush(console)


def print_compaction_summary(console: Console, label: str, stats) -> None:
    """Print a one-line compaction summary for a turn.

    ``label`` is the turn prefix (e.g. '本轮'). ``stats`` is a
    :class:`~cc_harness.context.CompactionStats` or ``None``.

    Nothing is printed when ``stats`` is ``None`` or ``stats.tier ==
    CompactionTier.NONE`` (no compaction fired). Otherwise a single line
    summarising the tier / ratio / snip / prune / summary-insert is emitted,
    followed by a ``⚠`` line when ``stats.error`` is set.
    """
    if stats is None:
        return
    # Lazy import to avoid a render → context circular dependency at module
    # load time (context imports prompts which imports tokens; render is a
    # leaf — keeping it clean).
    from cc_harness.context import CompactionTier

    tier = getattr(stats, "tier", CompactionTier.NONE)
    if tier is None or int(tier) == 0:
        return

    pct_before = f"{stats.ratio_before * 100:.0f}%"
    pct_after = f"{stats.ratio_after * 100:.0f}%"
    parts = [
        f"上下文压缩 [{label}]:",
        f"tier {int(tier)}",
        f"{pct_before} → {pct_after}",
        f"snip {stats.messages_snip} 条",
        f"prune {stats.messages_prune} 条",
    ]
    if stats.summarized and stats.summary_index is not None:
        parts.append(f"[summary 插入 #{stats.summary_index}]")
    _print(console, "  ".join(parts), markup=False, highlight=False)

    if stats.error:
        _print(console, f"⚠ 压缩异常: {stats.error}", highlight=False)
    _flush(console)


def print_cross_session_summary(
    console: Console,
    candidate,
    tool_diff: list[str],
    in_progress_subagents: list[str] | None = None,
) -> None:
    """E3 D4/D6/D7:新 session 启动时,若 load 了旧 session 上下文,渲染摘要。

    Args:
        console: rich.console.Console 实例
        candidate: CheckpointRecord(loaded checkpoint 元数据)
        tool_diff: D7 hash diff 列表,+X / -X / ~X 形式
        in_progress_subagents: D6 cancelled subagent id 列表(可空)
    """
    in_progress_subagents = in_progress_subagents or []
    lines = [
        f"🔁 续接上次 session({candidate.session_id}):",
        f"  • 模式: {candidate.mode}",
        f"  • 轮次: {candidate.turn_counter}",
        f"  • 结束: {candidate.ended_at}",
    ]
    if tool_diff:
        added = sum(1 for d in tool_diff if d.startswith("+"))
        removed = sum(1 for d in tool_diff if d.startswith("-"))
        lines.append(f"  • 工具变更: +{added} -{removed}")
    if in_progress_subagents:
        lines.append(
            f"  • 上次 fan-out 中断的 subagent:{len(in_progress_subagents)} 个已标 cancelled"
        )
    _print(console, "\n".join(lines), markup=False)


# ---------------------------------------------------------------------------
# emit() dispatcher — new public API.
#  - All existing ``print_*`` functions above remain unchanged for the
#    REPL/legacy path and existing tests.
#  - ``emit(event, *, driver)`` is the TUI/REPL/Test entry point: a
#    :class:`~cc_harness.render_protocol.RenderEvent` is forwarded to the
#    matching ``RenderDriver`` method, with the ``ToolCallEnd.duration_ms``
#    field threaded all the way through.
# ---------------------------------------------------------------------------
from cc_harness.render_protocol import (  # noqa: E402
    FinalText,
    ModeChanged,
    PermissionModeChanged,
    RenderDriver,
    RenderEvent,
    ThinkingChunk,
    ThinkingDone,
    TodoUpdate,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)


def emit(event: RenderEvent, *, driver: RenderDriver) -> None:
    """事件分发:RenderEvent → driver 对应方法。"""
    if isinstance(event, ThinkingChunk):
        driver.write_chunk(event.delta)
    elif isinstance(event, ToolCallStart):
        driver.write_tool_call(event.name, event.args)
    elif isinstance(event, ToolCallEnd):
        # duration_ms is part of the event payload even though the
        # 4-phase legacy render drops it; TUIDriver and TestDriver both
        # forward it.
        driver.write_tool_result(event.result, event.error, event.duration_ms)
    elif isinstance(event, (FinalText, ThinkingDone)):
        driver.write(event.text)
    elif isinstance(event, TodoUpdate):
        driver.write_todo(event.items)
    elif isinstance(event, Usage):
        driver.refresh_token(event)
    elif isinstance(event, ModeChanged):
        driver.write_status(mode=event.mode)
    elif isinstance(event, PermissionModeChanged):
        driver.write_status(permission_mode=event.mode)
    else:
        raise TypeError(f"Unknown event type: {type(event)}")
