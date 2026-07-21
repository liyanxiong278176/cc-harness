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
import logging
import os
import time
import uuid as _uuid
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

log = logging.getLogger(__name__)

_VALID_MODES = ("coding", "plan", "design", "chat")

# How far back to scan for disk changes after an LLM turn.
_DISK_CHANGE_WINDOW_S = 30
# Files this size or smaller get a content preview printed under their entry.
_PREVIEW_MAX_BYTES = 500
# Max number of changed files to print (most recent first).
_MAX_CHANGES_SHOWN = 10
# Task 4 (Plan B): verify hook hints 截断 — 单 task 最多写 3 条,
# 全局最多 10 条(防 system prompt 爆炸)。
MAX_HINTS_PER_TASK = 3
MAX_HINTS_TOTAL = 10

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
    # Task 6 (Plan A): project module state — manifest, TodoService, Live, resume
    project_root: Path | None = None
    manifest: object | None = None
    todo_service: object | None = None
    todo_extras: list = field(default_factory=list)
    live_panel: object | None = None
    resume_task: object | None = None
    # Task 4 (Plan B): verify hook — todo_hints 给下轮 system prompt 注入,
    # last_turn_text 是本轮 LLM 文本输出(给 verify 评 heuristic 用)。
    todo_hints: list[str] = field(default_factory=list)
    last_turn_text: str = ""


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
    scheduler: object | None = None,
    # E4 I-1:memory 工具 extras + mem_deps 在 main.py 构造后传入
    # (让 main.py 也能拿到 store/service 构造 MaintenanceScheduler)。
    # backward compat:若都不传 → repl 内部自行 build(原行为)。
    memory_extras: list | None = None,
    mem_deps: dict | None = None,
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
    # Task 6 / spec 组件 9 开放问题 8:session_id 加 hex 后缀保证唯一(避免同一秒
    # 内多个 REPL 共享 session_id 导致 active_sessions 串台)。
    state = ReplState(
        mode=default_mode,
        context_config=context_config or ContextConfig(),
        session_id=f"repl-{int(time.time())}-{_uuid.uuid4().hex[:8]}",
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
    # E4 I-1: 若 main.py 已构造并传入 → 复用;否则自行 build(向后兼容旧 caller)。
    from dotenv import dotenv_values
    _mem_env = {**os.environ, **{k: v for k, v in dotenv_values(Path(cwd) / ".env").items() if v}}
    if memory_extras is not None or mem_deps is not None:
        # main 已构造,直接复用
        state.memory_extras = list(memory_extras or [])
        state.mem_deps = mem_deps
        if state.memory_extras:
            print_info(console, f"  memory tools: {len(state.memory_extras)} 个(memory_recall/save,main 注入)")
    else:
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

    # --- Task 6 / spec 组件 9:6 处接线(Plan A) ---
    # 0) 统一锚点 — 所有下游 service / storage / policy 都用 state.project_root
    state.project_root = Path(cwd).resolve()

    # 1) 启动检测 manifest + 自动 init(用户零摩擦,无需先 `cc-harness init`)
    from cc_harness.project.manifest import load_manifest as _load_manifest
    from cc_harness.cli.init import init_noninteractive as _init_ni
    try:
        _manifest = _load_manifest(state.project_root)
    except Exception as e:
        print_warn(console, f"manifest 加载异常: {e}; 按无 manifest 处理(自动 init)")
        _manifest = None
    if _manifest is None:
        print_info(console, "No .cc-harness/project.yaml found. Auto-initializing...")
        try:
            _manifest = _init_ni(
                state.project_root, name=state.project_root.name or "myapp",
            )
            print_info(
                console,
                "✓ Created .cc-harness/project.yaml "
                "(run `cc-harness init` to customize)",
            )
        except Exception as e:
            print_warn(console, f"auto init 失败: {e}; todo tools 跳过注入")
            _manifest = None
    state.manifest = _manifest

    # 2) 加载 TodoService + Live(若 manifest 失败 → 跳过 Live)
    if state.manifest is not None:
        try:
            from cc_harness.project.service import TodoService as _TSvc
            from cc_harness.project.live import TodoLivePanel as _TLP
            _mem_svc = None
            if state.mem_deps and isinstance(state.mem_deps, dict):
                _mem_svc = state.mem_deps.get("service")
            state.todo_service = _TSvc(
                project_root=state.project_root,
                manifest=state.manifest,
                memory_service=_mem_svc,
            )
            # Live panel — manifest.live.position='off' 时不启动 Live
            if getattr(state.manifest.live, "position", "top") != "off":
                state.live_panel = _TLP(console, state.todo_service, state.manifest)
                try:
                    state.live_panel.start()
                except Exception as e:
                    print_warn(console, f"live panel 启动失败: {e}; 继续不显示 Live")
                    state.live_panel = None
        except Exception as e:
            print_warn(console, f"todo service 加载失败: {e}; todo tools 跳过")
            state.todo_service = None

    # 3) 注入 todo tools(拼接时 None-safe)
    # D1 final:不再在这里预 inject todo tools(预 build 时 dispatch_subagent_runner
    # 是 None → handler 返 "未注入" 错误)。改成 run_turn 时直接传 todo_service +
    # session_id + last_turn_text,让 agent.run_turn 在 dispatch 前自动构造
    # SubAgentRunner 并注入 dispatch_subagent_runner(基于 Task 7 path)。
    # state.todo_extras 仍保留 dataclass 字段,但 REPL 不再赋值非空 list —
    # 避免与 run_turn 内部 Task 7 路径双重注入 extras。
    state.todo_extras = []

    # 4) Resume 询问(只在 turns==0 时触发;auto 静默,ask 询问,manual 不主动)
    if (
        state.manifest is not None
        and state.todo_service is not None
        and state.session_stats.turns == 0
    ):
        try:
            _tasks = await state.todo_service.list(include_done=True)
            _resume_mode = getattr(state.manifest, "resume_mode", "ask")
            if _resume_mode == "ask":
                from cc_harness.cli.resume import select_resume_task as _srt
                _candidate = _srt(_tasks)
                if _candidate is not None:
                    state.resume_task = await _maybe_ask_resume(
                        console, _candidate, _tasks,
                    )
            elif _resume_mode == "auto":
                from cc_harness.cli.resume import select_resume_task as _srt
                state.resume_task = _srt(_tasks)
        except Exception as e:
            print_warn(console, f"resume 检测失败: {e}; 不 attach")

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
            # Q4 Task7: offload_deps 注入(kill-switch:offload_enabled or 无 mem_deps → None)。
            # mem_deps 已含 offload 锭(T4 extras:refs/canvas/closures/threshold/ratio/window);
            # Q4 agent 代码只读 offload 专用 key,忽略 Q3 key,故直接传 mem_deps 安全。
            offload_deps = (
                state.mem_deps
                if state.mem_deps and mem_cfg.offload_enabled
                else None
            )
            # Task 6: extra_native_specs 必须 None-safe(memory_extras only —
            #         todo_extras 改由 todo_service 自动注入)
            # D1 final:todo_extras 改为 todo_service 注入(见上方 step 3 注释);
            # 此处只传 memory_extras,避免与 agent.run_turn Task 7 路径双重注入。
            _all_extras = list(state.memory_extras or [])
            extra_native_specs = _all_extras or None
            turn_stats = await run_turn(
                state.messages, llm, mcp,
                max_iter=max_iter,
                mode=state.mode,
                cwd=cwd,
                design_dir=design_dir,
                token_counter=state.token_counter,
                policy=policy,
                l5=l5,
                extra_native_specs=extra_native_specs,            # Task 6: memory only
                context_config=state.context_config,             # Plan3: 压缩配置
                memory_layer=memory_layer,                        # Q3 Task8: 分层注入
                offload_deps=offload_deps,                        # Q4 Task7: 短期符号化卸载
                todo_service=state.todo_service,                  # D1 final:传 service,让 run_turn 注入 todo extras(含 dispatch_subagent_runner)
                session_id=state.session_id,                      # D1 final:handler 用 active_sessions
                last_turn_text=state.last_turn_text,              # D1 final:todo_update 完成门 acceptance 校验用
                resume_task=state.resume_task,                    # Task 6: 续干任务
                todo_hints=list(state.todo_hints or []),          # B 阶段 Task 5: verify hints
            )
            state.session_stats.add(turn_stats)

            # Task 4 (Plan B): 提取本轮 LLM 输出文本,给下轮 verify hook 用
            state.last_turn_text = _extract_final_text(state.messages)

            # 打印 token 明细
            print_token_summary(console, "本轮", turn_stats)
            print_token_summary(console, f"累计 {state.session_stats.turns} 轮", state.session_stats)

            # Plan3: 本轮发生过压缩(tier > NONE)→ 打印压缩摘要
            if turn_stats.compaction and int(turn_stats.compaction.tier) > 0:
                print_compaction_summary(console, "本轮", turn_stats.compaction)

            # Q3 Task8: after-turn hook — L0 capture + L1 pipeline(every-N)+ L2 scenario + L3 persona
            # E4 I-1: 末尾传 scheduler(后台 4 op 触发点)
            await _after_turn_memory(state, mem_cfg, scheduler=scheduler)

            # Task 6: after-turn hook — A 阶段占位(verify hook 留 B 阶段)
            await _after_turn_todo(state, state.todo_service)

            # After the turn, show what actually changed on disk — so the user
            # can see real file state without F5-ing their file manager.
            _print_disk_changes(console, cwd, since=turn_start)
    finally:
        # Task 6: 退出前 stop live panel(避免 dangling Rich Live 影响 terminal)
        if getattr(state, "live_panel", None) is not None:
            try:
                await state.live_panel.stop() if asyncio.iscoroutine(state.live_panel.stop()) else state.live_panel.stop()
            except Exception as e:
                print_warn(console, f"live panel stop failed: {e}")
        # E4: scheduler 由 main 构造并注入;shutdown 时 _drain 等后台 task 收尾
        if scheduler is not None:
            try:
                await scheduler._drain(timeout_s=5)
            except Exception as e:
                print_warn(console, f"scheduler _drain failed: {e}")
        # 主循环退出(正常 exit / EOF / Ctrl-C / 异常)→ shutdown 会话级 executor。
        # async,非 atexit;sandbox 时 kill 容器 + shutdown_owned_server,best-effort。
        await shutdown_session_executor()


async def _after_turn_memory(state: ReplState, mem_cfg, scheduler=None) -> None:
    """Q3 Task8 after-turn hook:capture L0 + pipeline L1(every-N)+ scenario L2 + persona L3。

    所有阶段 kill-switch 由 mem_cfg 控制(capture_enabled / pipeline_enabled);
    缺 mem_deps(记忆未初始化)→ 整体 no-op。fail-soft:单阶段异常不阻塞后续。
    E4 I-1: 末尾跑 scheduler.maybe_run(后台 4 op: staleness/TTL/consolidation/conflict),
    just_wrote_n 取 0(memory pipeline 自身已写,scheduler 走 every_n_turns /
    count_threshold / interval_s 路径兜底触发);调度失败不阻塞 turn。
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

    # E4 I-1: 后台 maintenance 调度。just_wrote_n=0 走 _should_trigger_async 路径
    # (every_n_turns / count_threshold / interval_s 任一满足即后台 fire-and-forget)。
    # 失败 swallow — 不阻塞 turn。
    if scheduler is not None:
        try:
            await scheduler.maybe_run(turn_idx=turn_idx, just_wrote_n=0)
        except Exception as e:
            print_warn(Console(), f"memory maintenance scheduler failed: {e}")


async def _after_turn_todo(state: ReplState, todo_service) -> None:
    """B 阶段 verify hook。每 turn 跑一次,不自动改 status。

    扫所有 in_progress task,跑 run_verify,结果写到 state.todo_hints。
    agent._refresh_system_prompt 读 hints 注入到 system prompt 末尾。

    三层异常(全部 swallow + warn):
    1. todo_service.list 抛 → 保留旧 hints(不重置)
    2. 单 task run_verify 抛 → 跳过该 task,其他继续
    3. 其他 Exception(impl 内部 bug)→ warn 后放弃本轮
    """
    try:
        await _after_turn_todo_impl(state, todo_service)
    except Exception as e:
        log.warning("verify hook: unexpected: %s", e)


async def _after_turn_todo_impl(state, todo_service):
    if todo_service is None:
        return
    try:
        tasks_list = await todo_service.list(include_done=False)
    except Exception as e:
        log.warning("verify hook: todo_service.list failed: %s", e)
        return  # 不清旧 hints

    # Lazy import:run_verify 在 cc_harness.project,repl 顶层 import 阶段不要拽
    # (避免 cc_harness 启动时把整个 project 链拉起来)。
    from cc_harness.project.verify import run_verify as _run_verify

    by_id = {t.id: t for t in tasks_list}
    last_turn_text = getattr(state, "last_turn_text", "") or ""
    hints: list[str] = []

    for task in tasks_list:
        if task.status != "in_progress":
            continue
        try:
            result = _run_verify(task, by_id, last_turn_text)
        except Exception as e:
            log.warning("verify hook: run_verify failed for %s: %s", task.id, e)
            continue  # 单 task 失败不影响其他

        per_task: list[str] = []
        if not result.passed:
            for miss in result.missing_criteria:
                per_task.append(f"task {task.id} criterion 未在最近一轮输出中体现: {miss}")
        # hints 无条件采纳(包括 passed=True 的辅助信号,如"无产出"提示)
        per_task.extend(result.hints)
        hints.extend(per_task[:MAX_HINTS_PER_TASK])

    state.todo_hints = hints[:MAX_HINTS_TOTAL]  # 覆盖 + 截断


def _extract_final_text(messages: list[dict]) -> str:
    """从 messages 末尾反向查找 role=assistant 且 content 是非空 str 的那条。

    含 tool_calls(content=None)时回退到上一条纯文本 message。
    无 assistant 或全 tool_calls → 返空串。
    """
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content
    return ""


async def _maybe_ask_resume(console: Console, candidate, tasks) -> object | None:
    """Resume 询问 — 返回用户选的 task,或 None。

    spec 组件 9 step 4(ask mode):
        turn==0 + 1 个 in_progress → 打印候选 + 询问 y/n/pick
        用户答 'y' → 选 candidate
        用户答 'n' → None
        用户答 'pick' → 列出所有 in_progress 让用户选 id

    Notes:
        - 使用本模块 _read_user(可被测试 monkeypatch),不走 builtins.input
        - 空输入 → 选 candidate(默认 y)
        - EOFError / KeyboardInterrupt → None(用户取消)
    """
    console.print(
        f"\n[resume] 检测到 in_progress task: {candidate.id} - {candidate.title}",
        markup=False,
    )
    n_in_progress = sum(1 for t in tasks if t.status == "in_progress")
    if n_in_progress > 1:
        console.print(
            f"  (另有 {n_in_progress - 1} 个 in_progress task,输入 'pick' 选择其他)",
            markup=False,
        )

    try:
        answer = (await _read_user("resume? [Y/n/pick]: ")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None

    if answer in ("", "y", "yes"):
        return candidate
    if answer in ("n", "no"):
        return None
    if answer == "pick":
        in_progress = [t for t in tasks if t.status == "in_progress"]
        console.print("可选 in_progress task:", markup=False)
        for t in in_progress:
            console.print(f"  {t.id}  {t.title}", markup=False)
        try:
            raw_id = (await _read_user("输入 task id: ")).strip()
        except (EOFError, KeyboardInterrupt):
            return None
        for t in in_progress:
            if t.id == raw_id:
                return t
        console.print(f"未知 id: {raw_id!r},跳过 resume", markup=False)
        return None
    # 其它输入按 n 处理
    return None


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
