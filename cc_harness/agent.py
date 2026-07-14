"""ReAct loop: streams one LLM turn, routes finish_reason, dispatches tools.

Output is the classic 4-phase ReAct format (per user spec):
    思考: <LLM's full reasoning text>
    行动: <tool call>
    观察: <tool result>
    [loop]
    结果: <final answer>

Modes (see task #4 / #6):
    "coding"  — full ReAct loop, tools enabled (default)
    "plan"    — one-shot final answer, no tool execution, no tools passed to LLM
    "design"  — one-shot final answer, no tool execution, output saved to disk
    "chat"    — same as coding (tools enabled, full ReAct loop)
"""
from __future__ import annotations
import json
import re
import time
from pathlib import Path
from rich.console import Console
from cc_harness.render import (
    print_thought, print_action, print_observation, print_result,
    print_warn, print_error, print_info,
)
from cc_harness.policy import PolicyEngine
from cc_harness.schema import validate_native, validate_mcp, set_mcp_schemas
from cc_harness.audit import log_decision
from cc_harness.l5 import L5Engine
from cc_harness.tools import confirm_tool, run_command, RUN_COMMAND_SPEC
from cc_harness.tokens import TokenCounter, TurnTokenStats, UsageRecord
from cc_harness.config import ContextConfig

_VALID_MODES = ("coding", "plan", "design", "chat")

# --- Native (non-MCP) tool registry ---
# Tools registered here are exposed to the LLM alongside MCP tools, but
# dispatched directly inside the agent (no protocol round-trip, no extra
# process). Each entry: {"spec": <OpenAI tool spec>, "handler": async fn}.
NATIVE_TOOLS: dict[str, dict] = {
    "run_command": {
        "spec": RUN_COMMAND_SPEC,
        "handler": run_command,
    },
}


async def run_turn(
    messages: list[dict],
    llm,                    # any object with async chat(messages, tools) -> AsyncIterator[StreamEvent]
    mcp,                    # any object with list_tools() and async call_tool(name, args) -> ToolResult
    *,
    max_iter: int = 20,
    mode: str = "coding",
    cwd: str | None = None,
    design_dir: Path | None = None,
    token_counter: TokenCounter | None = None,
    policy: PolicyEngine | None = None,
    l5: L5Engine | None = None,
    extra_native_specs: list[dict] | None = None,
    context_config: ContextConfig | None = None,
    memory_layer: dict | None = None,
    offload_deps: dict | None = None,
    qa_context: dict | None = None,
) -> TurnTokenStats:
    """Run one user turn in the given mode.

    In `coding` mode: full ReAct loop with tool execution.
    In `chat` mode: same as coding (tools enabled, full ReAct loop).
    In `plan` mode: one-shot LLM call (no tools passed, tool_calls dropped if any).
    In `design` mode: same as plan, plus the final assistant content is
        persisted to `design_dir` (default: ~/.cc-harness/designs/).

    `extra_native_specs` lets callers inject native-style tools alongside the
    built-in NATIVE_TOOLS (e.g. the locomo runner's memory_recall / memory_save).
    Each entry: ``{"spec": <OpenAI tool spec>, "handler": async fn,
    "deps": <dict splatted as kwargs at dispatch>}``. The LLM sees the merged
    spec list; tool_calls to an extra name dispatch to that entry's handler
    with ``(args, cwd=str(project_root), **deps)``.

    If `cwd` is provided, the system prompt at `messages[0]` is refreshed
    to match the current mode before the first LLM call. If `cwd` is None,
    the caller is responsible for having the right system prompt in place.

    `memory_layer`(Q3 Task7)可选分层记忆注入:``{"recall": async callable(query)
    -> RecallResult}``。recall 由 caller 注入(agent 不 import layered_recall,
    便于测试替身);pre-turn 调用,把 persona/scenarios 拼到 system 段。None
    或缺 "recall" 键 = kill-switch,不注入。fail-soft:recall 抛异常不崩主循环。

    `offload_deps`(Q4 Task5)可选短期符号化卸载:after-tool-call hook,tool result
    token > threshold → 落 refs + 摘要 + Mermaid canvas,messages 历史只留 pointer。
    独立于 memory_layer(两参数,不合并)。None 或 ``enabled=False`` = kill-switch。
    keys:enabled/threshold/offload(async closure)/canvas(async closure)。
    仅 allow + ask-yes 分支走 hook(其余 4 处短错误天然不撞阈值)。fail-soft:
    offload/canvas 抛异常 → 回退原文,不崩主循环。

    `qa_context`(Phase 1 Q1 uplift)可选 QA 模式标记:``{"q_type": int, "must_answer": bool}``。
    设了之后系统段会渲染 qa_intro 段(必须答规则 + 简洁风格),并把 q_type 注入
    模板 `{qa_category}`。None = 不渲染(向后兼容,test_agent.py 不受影响)。

    Mutates `messages` in place. Async so the repl can call it from its
    persistent event loop without `asyncio.run` overhead.
    """
    if mode not in _VALID_MODES:
        raise ValueError(f"unknown mode: {mode!r} (expected one of {_VALID_MODES})")

    console = Console()
    iter_count = 0
    _empty_retried = False  # one-shot retry guard for empty-content turns
    tool_call_log: list = []  # Plan1 Task4: [{name, args, ok, result}] per tool dispatch
    last_compaction = None  # Plan3: CompactionStats from maybe_compact (or None)
    _last_node = None  # Q4 Task5: offload edge chain — node_id of last offloaded tool result

    if cwd is not None:
        # Phase 1 Q1 uplift: qa_context → render qa_intro section
        if qa_context and qa_context.get("q_type") is not None:
            _refresh_system_prompt(
                messages, cwd, mode,
                extra_ctx={"qa_category": qa_context["q_type"]},
            )
        else:
            _refresh_system_prompt(messages, cwd, mode)

    # --- Q3 Task7: 分层记忆 pre-turn 注入 ---
    # memory_layer = {"recall": async callable(query) -> RecallResult}
    # recall 由 caller 注入(agent.py 不 import layered_recall);fail-soft。
    if memory_layer and memory_layer.get("recall") and messages:
        try:
            _q = next((m.get("content", "") for m in reversed(messages)
                       if m.get("role") == "user"), "")
            recall = await memory_layer["recall"](_q)
            if recall.persona and messages[0].get("role") == "system":
                messages[0]["content"] += f"\n\n## 用户画像\n{recall.persona.summary[:200]}"
            if recall.scenarios and messages[0].get("role") == "system":
                messages[0]["content"] += "\n\n## 相关场景\n" + "\n".join(
                    f"- {s.summary[:120]}" for s in recall.scenarios)
        except Exception as e:
            print_warn(console, f"memory inject failed: {e}")

    # --- Q4 Task6: pre-turn Mermaid 画布注入(预算 + 顺序)---
    # canvas_inject 开关 + canvas.md 存在 + token<=预算(mermaid_max_token_ratio ×
    # context_window)→ 系统段追加。顺序:基线 → Q3 persona → Q3 scenarios → Q4 mermaid
    # (本块紧跟 Q3 之后,顺序由 placement 保证)。fail-soft:文件读/编码/计数异常 →
    # 静默跳过,不崩主循环。canvas_path=None 或文件不存在(首次回合)→ 跳过。
    # offload_deps=None → 不注入(向后兼容,test_agent.py / test_repl.py 不受影响)。
    if offload_deps and offload_deps.get("canvas_inject", True) and messages:
        try:
            _canvas_path = offload_deps.get("canvas_path")
            if _canvas_path is not None and messages[0].get("role") == "system":
                _canvas_p = Path(_canvas_path)
                if _canvas_p.exists():
                    _tc = token_counter or TokenCounter()
                    _ratio = offload_deps.get("mermaid_max_token_ratio", 0.2)
                    _window = offload_deps.get("context_window", 1_000_000)
                    _budget = _ratio * _window
                    _canvas_text = _canvas_p.read_text(encoding="utf-8")
                    if _tc.count_text(_canvas_text) <= _budget:
                        messages[0]["content"] += (
                            "\n\n## 任务画布(Mermaid)\n" + _canvas_text
                        )
                    else:
                        print_warn(console, (
                            f"mermaid inject skipped: canvas "
                            f"{_tc.count_text(_canvas_text)}t > budget {_budget:.0f}t"))
        except Exception as e:
            print_warn(console, f"mermaid inject failed: {e}")

    # --- L4 policy gate setup ---
    project_root = Path(cwd or ".").resolve()
    if policy is None:
        policy = PolicyEngine(project_root=project_root)
    # Inject MCP schemas so schema.validate_mcp can check MCP tool args.
    try:
        set_mcp_schemas({
            t["function"]["name"]: t["function"].get("parameters", {})
            for t in (mcp.list_tools() or [])
        })
    except Exception:
        pass
    audit_path = project_root / "logs" / "policy.jsonl"
    l5_audit_path = project_root / "logs" / "l5.jsonl"

    def _redact(text: str, stage: str) -> str:
        """L5 脱敏 + 审计。stage ∈ {'thought','result'}。engine=None/非 str/空 → 原文直通。
        命中即审计(只记类型计数,绝不记明文)。"""
        if l5 is None or not isinstance(text, str) or not text:
            return text
        out = l5.scan(text)
        if out.findings:
            log_decision(
                l5_audit_path, iter_n=iter_count, tool=f"llm_{stage}",
                args={"findings": out.findings, "text_len": len(text)},
                action="l5_redact", outcome="redacted",
                rule_id=",".join(sorted(out.findings)), reason="", mode=mode,
            )
        return out.sanitized_text

    # In plan/design mode, the LLM should not see any tool definitions, so
    # it physically cannot emit tool_calls. In coding mode, expose both the
    # MCP tool set and the native tool registry (built-in + caller-injected).
    if mode in ("coding", "chat"):
        tool_specs = list(mcp.list_tools())
        for native in NATIVE_TOOLS.values():
            tool_specs.append(native["spec"])
        for entry in (extra_native_specs or []):
            tool_specs.append(entry["spec"])
    else:
        tool_specs = None

    iter_usages: list[UsageRecord] = []   # per-iter API-reported usage

    async def _dispatch(p, args: dict, project_root: Path):
        """Route a tool call to its handler.

        Precedence: NATIVE_TOOLS (built-in) > extra_native_specs (caller-
        injected) > mcp.call_tool (existing fallback). Handlers return a
        mcp_client.ToolResult; the caller reads `.llm_text` for the message
        appended to `messages`.
        """
        if p.name in NATIVE_TOOLS:
            return await NATIVE_TOOLS[p.name]["handler"](args, cwd=str(project_root))
        extra_entry = next(
            (e for e in (extra_native_specs or [])
             if e["spec"]["function"]["name"] == p.name),
            None,
        )
        if extra_entry is not None:
            h_kwargs = {"cwd": str(project_root), **extra_entry.get("deps", {})}
            return await extra_entry["handler"](args, **h_kwargs)
        return await mcp.call_tool(p.name, args)

    async def _maybe_offload_content(result_text: str, tool_name: str,
                                     tool_args: dict) -> str:
        """Q4 Task5 offload hook:胖 tool result → refs + 摘要 + Mermaid canvas,
        返回应放入 tool message 的 content(pointer_msg 若卸载,否则原文 untrusted 包裹)。

        仅由 allow + ask-yes 分支调(其余 4 处短错误天然不撞阈值,不走 hook)。
        两层 fail-soft:外层 try(threshold-check + offload 调用,offload 自身 LLM/disk
        失败 → 回退 _external);内层 try(canvas best-effort 装饰,失败不丢 pointer、
        不留 refs 孤儿、不 stale edge)。_last_node 无论 canvas 成败都推进(它在
        canvas 调用之后赋值,但内层 except 不 return,故赋值一定执行)。kill-switch:
        offload_deps=None 或 enabled=False → 直返 untrusted 原文。
        """
        nonlocal _last_node
        _external = f"<untrusted>{result_text}</untrusted>"
        if not (offload_deps and offload_deps.get("enabled", True)):
            return _external
        try:
            _tc = token_counter or TokenCounter()
            if _tc.count_text(result_text) > offload_deps["threshold"]:
                _off = await offload_deps["offload"](
                    result_text, tool_name, tool_args,
                    threshold=offload_deps["threshold"], token_counter=_tc)
                if _off is not None:
                    # canvas 是 best-effort 装饰,失败不影响 offload 已达成的减载:
                    # 独立 try/except — 否则 canvas 抛会丢 pointer(胖文回 messages)+
                    # refs 孤儿 + _last_node 不更新(下次 edge 指向陈旧前驱)。
                    try:
                        await offload_deps["canvas"](
                            _off.node_id, tool_name, _off.summary, edge_from=_last_node)
                    except Exception as ce:
                        print_warn(console, f"offload canvas (best-effort) failed: {ce}")
                    _last_node = _off.node_id   # 无论 canvas 成败,edge 链推进
                    return _off.pointer_msg
        except Exception as e:
            print_warn(console, f"offload hook failed: {e}")
        return _external

    def _stats() -> TurnTokenStats:
        """Build TurnTokenStats from current messages + tool_specs + iter_usages."""
        counter = token_counter
        if counter is None:
            counter = TokenCounter()
        cats = counter.categorize(messages, tools=tool_specs)
        return TurnTokenStats(
            user_input=cats["user_input"],
            tool_calls=cats["tool_calls"],
            llm_output=cats["llm_output"],
            system_prompt=cats["system_prompt"],
            summary=cats["summary"],
            tool_definitions=cats["tool_definitions"],
            api_prompt_tokens=sum(u.prompt_tokens for u in iter_usages),
            api_completion_tokens=sum(u.completion_tokens for u in iter_usages),
            api_total_tokens=sum(u.total_tokens for u in iter_usages),
            iter_count=len(iter_usages),
            api_reported=bool(iter_usages),
            tool_call_log=tool_call_log,
            compaction=last_compaction,
        )

    async def _stream_one_turn() -> tuple[str, list, str | None, UsageRecord | None]:
        """Stream exactly one LLM turn. Returns (content, pending, finish_reason, usage).

        Buffers content (no real-time printing) because the routing decision —
        has_tool_calls vs final answer — is only known after the "done" event.
        Returns empty content only if the LLM genuinely produced nothing.
        """
        content_parts: list[str] = []
        pending: list = []
        finish_reason: str | None = None
        usage: UsageRecord | None = None
        async for ev in llm.chat(messages, tool_specs):
            if ev.kind == "content":
                content_parts.append(ev.text)
            elif ev.kind == "tool_call_delta":
                pass  # accumulation handled inside llm.chat
            elif ev.kind == "done":
                finish_reason = ev.finish_reason
                pending = ev.pending
                usage = ev.usage
                # Prefer the consolidated content on the done event if set;
                # fall back to the streamed parts we collected above.
                content_parts = [ev.content] if ev.content else content_parts
        return "".join(content_parts), pending, finish_reason, usage

    while iter_count < max_iter:
        iter_count += 1
        iter_usage: UsageRecord | None = None   # usage for this iter (set on done)

        # Q4 Task7: ratio 批量兜底(无 count_messages,用 sum count_text)。
        # context 总 token 超 offload_ratio × context_window → 批量卸载剩余大 tool
        # result(reversible pointer)。置于 Plan3 maybe_compact 之前:Q4 可逆卸载先
        # 减载,Plan3 不可逆 summarize 后兜底(Q4 ratio 0.5 < Plan3 tier1 0.6,先触发)。
        # fail-soft:外层 try(构造/读 key)+ 每条 offload 独立 try(单条失败不破批/不崩轮)。
        # offload_deps=None 或 enabled=False → 跳过(向后兼容,test_agent/test_repl 无感)。
        if offload_deps and offload_deps.get("enabled", True):
            try:
                _cw = offload_deps.get("context_window") or (
                    context_config.context_window if context_config else 1_000_000)
                _tc = token_counter or TokenCounter()
                _total = sum(_tc.count_text(m.get("content", "")) for m in messages)
                if _cw > 0 and _total / _cw > offload_deps.get("offload_ratio", 0.5):
                    for m in messages:
                        # prefix match(非子串):pointer 形如 [offloaded node=...];
                        # 子串 "offloaded" 会误跳含该字面的 legit 大 result(源码/log)。
                        _content = m.get("content") or ""
                        if (m.get("role") == "tool"
                                and not _content.lstrip().startswith("[offloaded node=")):
                            if _tc.count_text(_content) > offload_deps["threshold"]:
                                try:
                                    _off = await offload_deps["offload"](
                                        m["content"], "(batch)", {},
                                        threshold=offload_deps["threshold"], token_counter=_tc)
                                    if _off:
                                        m["content"] = _off.pointer_msg
                                except Exception:
                                    pass
            except Exception:
                pass

        # Plan3: maybe compact context before LLM call (all modes). Catches own
        # errors (returns stats.error) so compaction never kills the ReAct loop.
        if context_config and context_config.enabled:
            from cc_harness.context import maybe_compact
            _counter = token_counter or TokenCounter()
            last_compaction = await maybe_compact(
                messages, tool_specs, _counter, context_config, llm
            )

        # 1. Stream one LLM turn (buffered — see _stream_one_turn).
        try:
            content, pending, finish_reason, iter_usage = await _stream_one_turn()
        except Exception as e:
            print_error(console, f"LLM stream failed: {e}")
            return _stats()

        if iter_usage is not None:
            iter_usages.append(iter_usage)

        # 2. Compute routing
        has_tool_calls = (finish_reason == "tool_calls") and bool(pending)

        if has_tool_calls and mode in ("coding", "chat"):
            # Coding mode: full ReAct loop with tool execution.
            if iter_count >= max_iter:
                # Max-iter guard: drop the tool_calls, fall back to final.
                print_warn(console, "max iterations reached with pending tool calls, forcing stop")
                if content:
                    content = _redact(content, "result")
                    messages.append({"role": "assistant", "content": content})
                    print_result(console, content)
                else:
                    fallback = "达到最大迭代次数,任务未完成。"
                    messages.append({"role": "assistant", "content": fallback})
                    print_result(console, fallback)
                return _stats()

            # 3. Build assistant message (with tool_calls; content may be None)
            if content:
                content = _redact(content, "thought")
            assistant_msg: dict = {
                "role": "assistant",
                "content": content if content else None,
                "tool_calls": [_pending_to_openai_tc(p) for p in pending],
            }
            messages.append(assistant_msg)

            # 3.5 Print the 思考 block (full LLM text for this iter)
            if content:
                print_thought(console, content)

            # 4. Execute each tool with 行动 + 观察 labels
            for i, p in enumerate(pending):
                if p.name is None:
                    placeholder_id = f"unknown_{i}"
                    print_warn(console, "tool_call name missing; backfilling error")
                    error_llm_text = (
                        f"[Tool Error] tool_call name missing, raw: "
                        f"{json.dumps({'id': p.id, 'arguments_json': p.arguments_json})}"
                    )
                    print_observation(console, error_llm_text)
                    # 短错误串,天然不撞阈值,不走 offload hook
                    messages.append({
                        "role": "tool",
                        "tool_call_id": placeholder_id,
                        "content": error_llm_text,
                    })
                    continue

                try:
                    args = json.loads(p.arguments_json) if p.arguments_json else {}
                except json.JSONDecodeError as e:
                    print_error(console, f"tool_call JSON parse failed: {e}")
                    error_text = f"[Tool Error] JSON parse failed: {p.arguments_json}"
                    print_observation(console, error_text)
                    # 短错误串,天然不撞阈值,不走 offload hook
                    messages.append({
                        "role": "tool",
                        "tool_call_id": p.id or f"unknown_{i}",
                        "content": error_text,
                    })
                    continue

                # schema 校验
                if p.name in NATIVE_TOOLS:
                    ok, msg = validate_native(p.name, args)
                else:
                    ok, msg = validate_mcp(p.name, args)
                if not ok:
                    error_text = f"[Tool Error] 参数校验失败: {msg}"
                    print_observation(console, error_text)
                    # 短错误串,天然不撞阈值,不走 offload hook
                    messages.append({
                        "role": "tool",
                        "tool_call_id": p.id or f"unknown_{i}",
                        "content": error_text,
                    })
                    continue

                # 权限决策
                ctx = {"project_root": project_root}
                decision = policy.evaluate(p.name, args, ctx)

                if decision.allow:
                    print_action(console, p.name, args)
                    log_decision(audit_path, iter_n=iter_count, tool=p.name, args=args,
                                 action=decision.action.value, outcome="executed",
                                 rule_id=decision.rule_id, reason=decision.reason, mode=mode)
                    # NOTE: dispatch 异常传播出 run_turn(不 catch);tool_call_log 局部变量随栈帧
                    # 销毁,故异常路径不记日志。异常 → runner 落 agent_crash result,整轮作废。
                    result = await _dispatch(p, args, project_root)
                    tool_call_log.append({"name": p.name, "args": args, "ok": True,
                                          "result": str(result.llm_text)[:500]})
                    print_observation(console, result.llm_text)
                    _tool_content = await _maybe_offload_content(
                        result.llm_text, p.name, args)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": p.id or f"unknown_{i}",
                        "content": _tool_content,
                    })
                else:  # ask
                    print_warn(console, f"[需确认] {p.name} {decision.reason}")
                    choice = confirm_tool(p.name, args)
                    if choice in ("yes", "always"):
                        if choice == "always":
                            policy.allowlist.add(p.name, args, project_root)
                        print_action(console, p.name, args)
                        log_decision(audit_path, iter_n=iter_count, tool=p.name, args=args,
                                     action=decision.action.value, outcome="executed",
                                     rule_id=decision.rule_id, reason=decision.reason, mode=mode)
                        result = await _dispatch(p, args, project_root)
                        tool_call_log.append({"name": p.name, "args": args, "ok": True,
                                              "result": str(result.llm_text)[:500]})
                        print_observation(console, result.llm_text)
                        _tool_content = await _maybe_offload_content(
                            result.llm_text, p.name, args)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": p.id or f"unknown_{i}",
                            "content": _tool_content,
                        })
                    else:
                        error_text = (
                            f"[未执行:用户拒绝] {p.name} — {decision.reason}。"
                            "该操作已被安全策略最终拒绝,不要主动建议绕道方案"
                            "(手动执行/换工具/分步绕过);如用户仍需要,由用户重新明确提出。"
                        )
                        print_observation(console, error_text)
                        log_decision(audit_path, iter_n=iter_count, tool=p.name, args=args,
                                     action=decision.action.value, outcome="denied",
                                     rule_id=decision.rule_id, reason=decision.reason, mode=mode)
                        tool_call_log.append({"name": p.name, "args": args, "ok": False,
                                              "result": error_text[:500]})
                        # 短错误串,天然不撞阈值,不走 offload hook
                        messages.append({
                            "role": "tool",
                            "tool_call_id": p.id or f"unknown_{i}",
                            "content": error_text,
                        })

            # 5. Continue the loop — feed tool results back to LLM
            continue

        # Either: (a) coding/chat mode with no tool_calls → final answer, or
        #         (b) plan/design mode regardless of tool_calls → force final
        if has_tool_calls and mode not in ("coding", "chat"):
            # Defensive: the LLM shouldn't emit tool_calls in plan/design
            # (we passed no tool specs), but if it does, drop them and warn.
            print_warn(console, f"mode={mode}: dropping {len(pending)} unexpected tool call(s)")

        if content:
            # Final. Print "结果:" + the FULL content as the LLM's answer.
            content = _redact(content, "result")
            messages.append({"role": "assistant", "content": content})
            print_result(console, content)
            if mode == "design":
                saved = _save_design_output(messages, base_dir=design_dir)
                if saved is not None:
                    print_info(console, f"已保存到 {saved}")
            return _stats()
        else:
            # Empty content with no tool_calls. The streaming provider
            # (e.g. DeepSeek) occasionally returns an empty stream with
            # finish_reason="stop" and non-zero completion_tokens — the
            # model was called, but the content was dropped at the wire.
            # Retry the SAME turn ONCE before giving up, so a flaky first
            # turn doesn't dead-end the session. (Resets iter_count-1 so
            # the retry doesn't burn a max_iter slot.)
            if not _empty_retried:
                _empty_retried = True
                print_warn(console, "空回复,重试中... (empty response, retrying)")
                iter_count -= 1
                continue
            print_warn(console, "empty LLM turn, ending")
            return _stats()

    # 6. max_iter reached (safety net — the inner has_tool_calls branch above
    # already handles this case and returns early, so this only runs if the
    # LLM never returned has_tool_calls=True but somehow the loop also never
    # appended an assistant message and never returned).
    print_warn(console, "max iterations reached")
    if content:
        content = _redact(content, "result")
        messages.append({"role": "assistant", "content": content})
        print_result(console, content)
    return _stats()


def _pending_to_openai_tc(p) -> dict:
    """Convert a PendingToolCall to OpenAI's tool_calls entry shape."""
    return {
        "id": p.id or "",
        "type": "function",
        "function": {
            "name": p.name or "",
            "arguments": p.arguments_json,
        },
    }


def _refresh_system_prompt(messages: list[dict], cwd: str, mode: str,
                           extra_ctx: dict | None = None) -> None:
    """Insert or update the system prompt at messages[0] for the current mode.

    `extra_ctx` (Phase 1 Q1 uplift) is merged into the composer ctx so callers
    can gate qa-aware sections (e.g. qa_intro needs ctx["qa_category"]).
    """
    from cc_harness.prompts import build_system_prompt
    if extra_ctx:
        from cc_harness.prompts import PromptComposer
        ctx = {"cwd": cwd, **extra_ctx}
        prompt = PromptComposer(mode=mode, ctx=ctx).render()
    else:
        prompt = build_system_prompt(cwd, mode=mode)
    if messages and messages[0].get("role") == "system":
        messages[0]["content"] = prompt
    else:
        messages.insert(0, {"role": "system", "content": prompt})


def _save_design_output(
    messages: list[dict],
    base_dir: Path | None = None,
) -> Path | None:
    """Persist the last assistant content to base_dir / '{ts}-{slug}.md'.

    Returns the path written, or None if no assistant content to save.
    """
    if base_dir is None:
        base_dir = Path.home() / ".cc-harness" / "designs"
    base_dir.mkdir(parents=True, exist_ok=True)

    last = next(
        (m for m in reversed(messages) if m.get("role") == "assistant" and m.get("content")),
        None,
    )
    if last is None:
        return None

    content = last["content"]
    ts = time.strftime("%Y%m%d-%H%M%S")
    first_line = content.split("\n", 1)[0].strip()[:30]
    slug = re.sub(r"[^\w一-鿿-]+", "-", first_line).strip("-") or "design"
    path = base_dir / f"{ts}-{slug}.md"
    path.write_text(content, encoding="utf-8")
    return path
