# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

cc-harness 是一个**跑在终端里的编程代理**:通过 OpenAI 兼容 LLM(默认配 DeepSeek)执行 ReAct 循环,工具来自 MCP server(fs/git)+ 一个内置 `run_command`,输出 思考/行动/观察/结果 4 段。

## Common commands

```bash
# Run the REPL (entry point)
.venv/Scripts/python.exe main.py
.venv/Scripts/python.exe main.py --mode plan          # start in plan mode
.venv/Scripts/python.exe main.py --design-dir <path>  # custom design save dir

# Tests
.venv/Scripts/python.exe -m pytest tests/                 # all
.venv/Scripts/python.exe -m pytest tests/test_X.py -v     # one file
.venv/Scripts/python.exe -m pytest tests/test_X.py::test_name  # one test
.venv/Scripts/python.exe -m pytest tests/ -k "name_substr" # by name

# Lint
.venv/Scripts/python.exe -m ruff check cc_harness/ tests/

# Phase-1 regression (creates + runs hello.py end-to-end)
.venv/Scripts/python.exe run_verify.py
```

Force UTF-8 on Windows (avoids GBK crashes on 思考/✅/中文):
```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe ...
```

## Architecture (data flow)

```
main.py
  └── repl.py:run_repl()                  # sticky mode (coding/plan/design), slash commands
        │     slash cmds: /plan /design /coding /mode /help /clear (case-insensitive)
        ├── run_turn()  [agent.py]        # ReAct while loop, max_iter=20
        │     ├── llm.py:LLMClient        # OpenAI stream + tool_calls accumulator
        │     ├── mcp_client.py:MCPClient # stdio/sse/http → OpenAI tool schema
        │     ├── tools.py:NATIVE_TOOLS   # currently: run_command (asyncio subprocess)
        │     ├── tools.py:is_dangerous   # rm -rf / format / drop / shutdown → user confirm
        │     ├── prompts.py:Section pool # 10 sections in SECTION_POOL, gated by conditions
        │     └── render.py               # 4-phase ReAct output (思考/行动/观察/结果)
        └── _print_disk_changes()         # post-turn: show files modified in last 30s
```

**Key data flow**:
- `messages: list[dict]` (OpenAI chat format) is the single state across turns
- `messages[0]` is the system prompt; rebuilt on every turn in `agent._refresh_system_prompt` to match the current mode
- Tool specs: `mcp.list_tools() + NATIVE_TOOLS specs` → sent to LLM; tool_calls routed by name (MCP vs native)
- Streaming is buffered (not token-by-token). Each iteration prints the LLM's full text as a single 思考 block, then 行动/观察 for each tool call, so the 4-phase layout is clean and never duplicated. See `agent.run_turn` for the trade-off.

## Design decisions (non-obvious)

**3 modes, not just 1.** `mode in {coding, plan, design}` is sticky across the turn. In plan/design, `tool_specs = None` is sent to the LLM so it physically cannot emit tool_calls (any that leak through are dropped with a warn).

**`run_command` is built-in, NOT via MCP.** Community shell MCP servers either don't work on Windows (`@kevinwatt/shell-mcp` uses `whereis`) or require LLM sampling we don't implement (`@mako10k/mcp-shell-server` enhanced mode). The native async subprocess in `tools.py` just works. Don't add an MCP shell server back without understanding why we removed it.

**Section pool, not a single string.** `prompts.py` has 10 sections in `SECTION_POOL` with conditions (`mode==coding`, `mode==plan`, `mode==design`, `has_tools`, `always`). To add a new section, register it in the pool — don't touch `build_system_prompt`.

**Safety is not a sandbox.** `is_dangerous` only matches a hardcoded regex list (rm -rf, format, drop table, fork bomb, shutdown, reboot). It's "prevent accidental mistakes" not security. Don't expand the regex list to be a permission system — that scope was explicitly cut.

**Windows GBK fix in `main.py` lines 17-23 must stay.** Without `sys.stdin.reconfigure(encoding="utf-8")`, the GBK default codepage crashes on the first non-ASCII char the LLM outputs (✅, 中文, 思考, etc.).

## Test conventions

- 133 tests in `tests/test_*.py` (collected by pytest, default pattern)
- `tests/_test_*.py` (leading underscore) are **integration tests requiring a real LLM** — not collected by pytest by default; only run manually
- Test agents use `FakeLLM` (pre-programmed stream events) + `FakeMCP` (pre-programmed tool results), defined in `test_agent.py` and reused via imports
- New test file naming: `test_<module>.py`, mirror source module names
- For REPL tests, mock `_read_user` (not `builtins.input` directly — too fragile in subprocess tests)

## Config & files

- `.env` (3 required, no defaults — see `config.py`): `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`
- `mcp.json`: MCP server entries. Per-server failures are isolated — one bad server logs a red warning, the rest still boot (`init_timeout_s` defaults to 30s in `mcp_client.py`). The bundled config mixes stdio (npx-launched: filesystem, playwright) and SSE (bing-cn-mcp-server, fetch, context7-mcp) transports. Tool names are exposed to the LLM as `mcp__{server}__{tool}`.
- `~/.cc-harness/designs/`: design-mode artifacts land here by default (`{ISO ts}-{first-line-slug}.md`); override with `python main.py --design-dir <path>`
- `run_verify.py` (root): Phase-1 regression script — spawns the REPL as a subprocess, pipes one command in, captures output, exits. Useful for end-to-end smoke after a refactor. Requires a real LLM (hits the configured provider).

## Out of scope (don't add unless asked)

- Multi-LLM backend switching (locked to OpenAI-compatible)
- Sandbox / Docker (only regex-based dangerous-command gate)
- Session persistence (process exit = session gone)
- Concurrent tool calls (serial only)
- SubAgent / Agent Team / Worktree (PDF 阶段 4-5, not started)
