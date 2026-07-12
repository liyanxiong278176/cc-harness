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
import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from openai import AsyncOpenAI
from rich.console import Console
from cc_harness.audit import log_decision
from cc_harness.config import ContextConfig, load_executor_config, load_l2_config, load_l5_config, load_policy_config
from cc_harness.l2 import REFUSAL_TEMPLATE, scan_user_input
from cc_harness.l5 import build_l5_engine
from cc_harness.policy import PolicyEngine
from cc_harness.render import print_compaction_summary, print_info, print_result, print_warn, print_token_summary
from cc_harness.tokens import TokenCounter, SessionTokenStats
from cc_harness.tools import init_session_executor, shutdown_session_executor

_VALID_MODES = ("coding", "plan", "design", "chat")

# How far back to scan for disk changes after an LLM turn.
_DISK_CHANGE_WINDOW_S = 30
# Files this size or smaller get a content preview printed under their entry.
_PREVIEW_MAX_BYTES = 500
# Max number of changed files to print (most recent first).
_MAX_CHANGES_SHOWN = 10

_HELP_TEXT = """\
可用命令:
  /plan, /design, /coding, /chat  切换粘性模式(后续消息以该模式处理)
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
    memory_extras: list = field(default_factory=list)  # Plan2: memory 工具 extras(session 级)
    context_config: ContextConfig = field(default_factory=ContextConfig)  # Plan3: 压缩配置
    # Q3 Task8: session 标识 + mem_deps(pipeline/recall/store/persona_path/scenarios_dir)
    session_id: str = ""
    mem_deps: dict | None = None


async def _read_user(prompt: str) -> str:
    """Block on input() in a worker thread so the event loop stays responsive."""
    return await asyncio.to_thread(input, prompt)


def _prompt_for(mode: str) -> str:
    """Return the REPL prompt prefix for the current mode.

    All four modes are tagged ("> [coding] " / "> [plan] " / "> [design] " /
    "> [chat] ") so the active mode is always visible at a glance.
    """
    return f"> [{mode}] "


def _handle_slash(cmd: str, state: ReplState, console: Console) -> bool:
    """Dispatch a slash command. Returns True if handled, False if the
    input should fall through to the LLM as a normal message.

    Commands are case-insensitive: /PLAN == /plan == /Plan.
    """
    cmd = cmd.lower()
    if cmd in ("/plan", "/design", "/coding", "/chat"):
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
    context_config: ContextConfig | None = None,
) -> None:
    """Run the interactive REPL.

    `cwd` is passed to run_turn so the system prompt at messages[0] can be
    refreshed per mode. `default_mode` is the initial sticky mode (also
    available via /plan /design /coding at runtime). `design_dir` is
    where design-mode outputs get persisted (default: ~/.cc-harness/designs/).
    `context_config` (Plan3) is the 4-tier compaction config; when None a
    default ContextConfig is used (main.py loads env overrides via
    load_context_config()).
    """
    if default_mode not in _VALID_MODES:
        raise ValueError(
            f"unknown default_mode: {default_mode!r} (expected one of {_VALID_MODES})"
        )

    console = Console()
    state = ReplState(
        mode=default_mode,
        context_config=context_config or ContextConfig(),
        session_id=f"repl-{int(time.time())}",
    )

    # Q3 Task8: 加载分层记忆 config(kill-switches:layered_inject/capture_enabled/pipeline_enabled)
    from cc_harness.memory.config import load_memory_config
    mem_cfg = load_memory_config(Path("policy.yaml"))

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

    # L5 输出 DLP:思考/结果段脱敏。无 [dlp] extra 时退化 Layer A(密钥正则)only。
    l5_cfg = load_l5_config(Path("policy.yaml"))
    l5 = build_l5_engine(l5_cfg)

    n_tools = len(mcp.list_tools())
    print_info(console, "")
    print_info(console, f"  cc-harness ready  |  tools: {n_tools}  |  mode: {default_mode.upper()}")
    print_info(console, f"  prompt: {_prompt_for(default_mode).rstrip()}")
    print_info(console, "  type 'exit' or 'quit' to leave, Ctrl+C / Ctrl+D also works; /help for commands")
    print_info(console, "")

    # 启动钩子:读 policy.yaml executor 段 → ExecutorConfig → init 会话级 executor。
    # native(默认)无副作用;sandbox 时建会话级容器供 run_command 复用,避免每条
    # 命令 cold-start。kill-switch 在 config.enabled / config.backend(policy.yaml)。
    exec_cfg = load_executor_config(Path(cwd) / "policy.yaml")
    init_session_executor(exec_cfg, cwd)

    # Plan2: 构造 memory 工具(session 级单例)。失败优雅降级(无 EMBEDDING_* 或
    # sqlite-vec 缺 → helper 返 ([], None);此处兜底构造异常)。生产 db=logs/memory.db
    # (与 eval logs/locomo_memory.db 隔离)。
    from dotenv import dotenv_values
    _mem_env = {**os.environ, **{k: v for k, v in dotenv_values(Path(cwd) / ".env").items() if v}}
    try:
        from cc_harness.memory.extras import build_memory_extras
        state.memory_extras, state.mem_deps = await build_memory_extras(
            _mem_env, Path(cwd) / "logs" / "memory.db"
        )
        if state.memory_extras:
            print_info(console, f"  memory tools: {len(state.memory_extras)} 个(memory_recall/save)")
        else:
            print_info(console, "  memory tools: 未启用(EMBEDDING_* 缺失或初始化失败)")
    except Exception as e:
        print_warn(console, f"memory 初始化异常: {e}; 不接入记忆工具")
        state.memory_extras = []

    try:
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
                if scan.reason.startswith("judge_error"):
                    log_decision(
                        l2_audit_path,
                        iter_n=state.session_stats.turns, tool="user_input",
                        args={"input_hash": hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]},
                        action="l2_allow", outcome="judge_fail_open",
                        rule_id=scan.reason, reason="", mode=state.mode,
                    )
            else:
                user_content = raw

            state.messages.append({"role": "user", "content": user_content})
            turn_start = time.time()
            from cc_harness.agent import run_turn
            # Q3 Task8: memory_layer 注入(kill-switch:layered_inject or 无 mem_deps → None)
            memory_layer = (
                {"recall": state.mem_deps["recall"]}
                if state.mem_deps and mem_cfg.layered_inject
                else None
            )
            turn_stats = await run_turn(
                state.messages, llm, mcp,
                max_iter=max_iter,
                mode=state.mode,
                cwd=cwd,
                design_dir=design_dir,
                token_counter=state.token_counter,
                policy=policy,
                l5=l5,
                extra_native_specs=state.memory_extras or None,  # Plan2: 记忆工具(chat/coding)
                context_config=state.context_config,             # Plan3: 压缩配置
                memory_layer=memory_layer,                        # Q3 Task8: 分层注入
            )
            state.session_stats.add(turn_stats)

            # 打印 token 明细
            print_token_summary(console, "本轮", turn_stats)
            print_token_summary(console, f"累计 {state.session_stats.turns} 轮", state.session_stats)

            # Plan3: 本轮发生过压缩(tier > NONE)→ 打印压缩摘要
            if turn_stats.compaction and int(turn_stats.compaction.tier) > 0:
                print_compaction_summary(console, "本轮", turn_stats.compaction)

            # Q3 Task8: after-turn hook — L0 capture + L1 pipeline(every-N)+ L2 scenario + L3 persona
            await _after_turn_memory(state, mem_cfg)

            # After the turn, show what actually changed on disk — so the user
            # can see real file state without F5-ing their file manager.
            _print_disk_changes(console, cwd, since=turn_start)
    finally:
        # 主循环退出(正常 exit / EOF / Ctrl-C / 异常)→ shutdown 会话级 executor。
        # async,非 atexit;sandbox 时 kill 容器 + shutdown_owned_server,best-effort。
        await shutdown_session_executor()


async def _after_turn_memory(state: ReplState, mem_cfg) -> None:
    """Q3 Task8 after-turn hook:capture L0 + pipeline L1(every-N)+ scenario L2 + persona L3。

    所有阶段 kill-switch 由 mem_cfg 控制(capture_enabled / pipeline_enabled);
    缺 mem_deps(记忆未初始化)→ 整体 no-op。fail-soft:单阶段异常不阻塞后续。
    """
    if not state.mem_deps:
        return
    store = state.mem_deps["store"]
    turn_idx = state.session_stats.turns

    # L0: capture(幂等录制 conversation 表)
    if mem_cfg.capture_enabled:
        try:
            from cc_harness.memory.capture import capture
            await capture(store, state.session_id, state.messages, turn_idx=turn_idx)
        except Exception as e:
            print_warn(Console(), f"memory capture failed: {e}")

    # L1 + L2 + L3: pipeline(every-N 提取 L1)+ scenario(聚类)+ persona(画像)
    if mem_cfg.pipeline_enabled:
        try:
            await state.mem_deps["pipeline"].maybe_run(
                state.messages, state.token_counter, context_window=1_000_000,
                session_id=state.session_id, turn_idx=turn_idx,
                every_n=mem_cfg.pipeline_every_n,
            )
        except Exception as e:
            print_warn(Console(), f"memory pipeline failed: {e}")
        try:
            from cc_harness.memory.scenario import cluster_scenarios
            # embedder 在当前 MVP 实现中未被 cluster_scenarios 使用(单簇 + texts[:3] 拼接),
            # 传 None 安全;llm=None 退化为拼接 summary。
            await cluster_scenarios(
                store, None, state.session_id, state.mem_deps["scenarios_dir"],
                min_atoms=mem_cfg.scenario_min_atoms, llm=None,
            )
        except Exception as e:
            print_warn(Console(), f"memory scenario failed: {e}")
        try:
            from cc_harness.memory.persona import generate_persona
            await generate_persona(
                store, None, state.mem_deps["persona_path"],
                trigger_every_n=mem_cfg.persona_trigger_every_n,
            )
        except Exception as e:
            print_warn(Console(), f"memory persona failed: {e}")


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
