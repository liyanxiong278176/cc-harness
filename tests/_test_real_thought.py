"""Real-LLM smoke test for the new plain-text ReAct layout.

Loads .env (DeepSeek), spawns the in-process fake MCP server (echo/fail/slow),
sends a real query, and runs agent.run_turn. Captures Rich Console output to
verify the new no-color, no-tool-result, ReAct-labeled layout works against
a real LLM.
"""
import asyncio
import io
import sys
from pathlib import Path
from rich.console import Console as RichConsole

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from cc_harness.config import load_config, MCPServerConfig
from cc_harness.llm import LLMClient
from cc_harness.mcp_client import MCPClient
from cc_harness import agent as agent_mod

FAKE_SERVER = str(PROJECT_ROOT / "tests" / "fake_mcp_server.py")


async def main() -> None:
    # Capture Rich output (no colors)
    buf = io.StringIO()
    captured = RichConsole(
        file=buf, force_terminal=False, color_system=None, width=200,
    )
    # Patch agent's Console + skip the danger prompt
    agent_mod.Console = lambda: captured
    agent_mod.confirm = lambda prompt: True

    # Load real config (DeepSeek)
    cfg = load_config(
        env_path=PROJECT_ROOT / ".env",
        mcp_json_path=PROJECT_ROOT / "mcp.json",
    )
    print(f"[config] model={cfg.openai_model} base_url={cfg.openai_base_url}", file=sys.stderr)

    fake_cfg = MCPServerConfig(
        type="stdio", command=sys.executable, args=[FAKE_SERVER],
    )
    mcp = MCPClient({"fake": fake_cfg})
    await mcp.start()
    tools = mcp.list_tools()
    print(f"[mcp] started; tools loaded:", file=sys.stderr)
    for t in tools:
        print(f"  - {t['function']['name']}: {t['function']['description']}", file=sys.stderr)

    llm = LLMClient(
        api_key=cfg.openai_api_key,
        model=cfg.openai_model,
        base_url=cfg.openai_base_url,
    )

    query = "请用 echo 工具把 'hello'、'world'、'foo' 三个字符串各回显一次,然后告诉我一共调用了几次 echo 工具。"

    from cc_harness.prompts import build_system_prompt
    messages: list[dict] = [
        {"role": "system", "content": build_system_prompt(str(PROJECT_ROOT))},
        {"role": "user", "content": query},
    ]

    print(f"\n[query] {query}\n", file=sys.stderr)
    print("=" * 80, file=sys.stderr)
    print("CAPTURED TERMINAL OUTPUT (plain text, real LLM):", file=sys.stderr)
    print("=" * 80, file=sys.stderr)

    try:
        await agent_mod.run_turn(messages, llm, mcp, max_iter=15)
    finally:
        await mcp.shutdown()

    out = buf.getvalue()
    sys.stderr.write(out)
    sys.stderr.write("=" * 80 + "\n")

    # ---- analyze ----
    ansi_present = "\x1b[" in out

    print(f"\n[analysis]", file=sys.stderr)
    print(f"  ANSI escape codes present : {ansi_present} (expected: False)", file=sys.stderr)
    print(f"  '行动:' occurrences       : {out.count('行动:')}", file=sys.stderr)
    print(f"  '结果:' occurrences       : {out.count('结果:')}", file=sys.stderr)
    print(f"  old 🔧 count              : {out.count('🔧')} (expected: 0)", file=sys.stderr)
    print(f"  old 📤 count              : {out.count('📤')} (expected: 0)", file=sys.stderr)
    # Tool results must NOT appear in the captured output
    print(f"  'done' (tool result) leak : {'done' in out} (expected: False — echo's result is hidden)", file=sys.stderr)

    # Hard assertions
    assert not ansi_present, f"expected no ANSI, got: {out!r}"
    n_action = out.count("行动:")
    n_result = out.count("结果:")
    assert n_action >= 3, f"expected at least 3 行动: lines, got {n_action}"
    assert n_result >= 1, f"expected at least 1 结果: line, got {n_result}"
    assert out.count("🔧") == 0, "old 🔧 emoji should not appear"
    assert out.count("📤") == 0, "old 📤 emoji should not appear"
    # Order: every 行动: must come BEFORE 结果:
    first_action = out.find("行动:")
    first_result = out.find("结果:")
    assert first_action < first_result, f"行动: should come before 结果: ({first_action} vs {first_result})"
    # Result text 'done' from fake_mcp_server should not leak (the result for 'echo' is just the input string)
    assert "done" not in out, "fake_mcp_server's 'slow' tool result 'done' leaked into output"

    print(f"\n✅ Real LLM ran through the new plain-text ReAct layout cleanly", file=sys.stderr)
    print(f"   {n_action} tool calls, {n_result} final delimiter, no colors, no leaked results", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
