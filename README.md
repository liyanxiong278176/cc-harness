# cc-harness

Terminal coding agent with MCP tools. ReAct loop driven by an OpenAI-compatible
LLM (DeepSeek by default), 4-tier context compaction, and rich tool support
via the Model Context Protocol.

## Quick start

```bash
.venv/Scripts/python.exe -m pip install -e ".[dev]"
.venv/Scripts/python.exe main.py
```

See `CLAUDE.md` for the full command reference.

## Architecture

```
main.py
  └── repl.py:run_repl()                  # sticky mode (coding/plan/design)
        └── run_turn()  [agent.py]        # ReAct loop
              ├── context.py:maybe_compact  # 4-tier cascade
              ├── llm.py / mcp_client.py   # providers
              └── tokens.py:TokenCounter    # 6-bucket token tracking
```

## Evaluation

See `eval/README.md` for the GAIA-based A/B harness comparing
context-management strategies between the `master` and `context-compaction`
branches.

```bash
.venv/Scripts/python.exe -m pip install -e ".[eval]"
.venv/Scripts/python.exe -m eval.run --dry-run   # check setup
.venv/Scripts/python.exe -m eval.run             # 30-task real run
```
