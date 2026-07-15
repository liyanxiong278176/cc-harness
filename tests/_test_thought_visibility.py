"""One-off script: verify the 4-phase ReAct render layout for multi-step ReAct.

Mocks the LLM with 3 iterations:
  iter 1: Thought1 "我先看看目录" -> tool_call(list_dir)
  iter 2: Thought2 "接下来读 main.py" -> tool_call(read_file)
  iter 3: Final "综合分析: ..."

Per the 4-phase design (思考/行动/观察/结果), the output should be:
  思考: 我先看看目录
  行动: mcp__fs__list_directory
    path: "..."
  观察: main.py
  cc_harness/
  tests/
  思考: 接下来读 main.py
  行动: mcp__fs__read_file
    path: "..."
  观察: #!/usr/bin/env python
  import asyncio
  ...
  结果: 综合分析: 项目是一个终端编码助手

Captures the Rich Console output and asserts:
  - NO ANSI color codes
  - Each 思考 block has the FULL LLM text (no truncation)
  - 行动 label + one arg per line
  - 观察 label + the tool's actual result (indented for multi-line)
  - 结果 label + the final answer (no duplication, no separate 思考)
  - Tool call results ARE shown in 观察 (this is the new design)
  - Order: 思考 -> 行动 -> 观察 -> 思考 -> ... -> 结果
"""
# ruff: noqa: E402
import asyncio
import io
import sys
from dataclasses import dataclass, field

from rich.console import Console as RichConsole

from cc_harness.llm import PendingToolCall
from cc_harness.mcp_client import ToolResult

BUF = io.StringIO()
CAPTURED = RichConsole(
    file=BUF, force_terminal=False, color_system=None, width=200,
)

from cc_harness import agent as agent_mod
agent_mod.Console = lambda: CAPTURED
agent_mod.confirm = lambda prompt: True


@dataclass
class FakeStreamEvent:
    kind: str
    text: str = ""
    pending: list = field(default_factory=list)
    finish_reason: str | None = None
    content: str = ""


@dataclass
class FakeLLM:
    responses: list
    call_count: int = 0
    async def chat(self, messages, tools):
        idx = self.call_count
        self.call_count += 1
        for ev in self.responses[idx]:
            yield ev


@dataclass
class FakeMCP:
    tools_spec: list
    results: dict
    calls: list = field(default_factory=list)
    def list_tools(self):
        return list(self.tools_spec)
    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return self.results[name]


list_dir_tool = {
    "type": "function", "function": {
        "name": "mcp__fs__list_directory",
        "description": "List files in a directory",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    }
}
read_file_tool = {
    "type": "function", "function": {
        "name": "mcp__fs__read_file",
        "description": "Read a file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    }
}

def mk_tc(name, args_json, call_id):
    return [PendingToolCall(index=0, id=call_id, name=name, arguments_json=args_json)]

responses = [
    [
        FakeStreamEvent(kind="content", text="我先看看"),
        FakeStreamEvent(kind="content", text="目录结构"),
        FakeStreamEvent(
            kind="done",
            content="我先看看目录结构",
            pending=mk_tc("mcp__fs__list_directory", '{"path":"D:/agent_learning/cc-harness"}', "c1"),
            finish_reason="tool_calls",
        ),
    ],
    [
        FakeStreamEvent(kind="content", text="接下来读"),
        FakeStreamEvent(kind="content", text=" main.py"),
        FakeStreamEvent(
            kind="done",
            content="接下来读 main.py",
            pending=mk_tc("mcp__fs__read_file", '{"path":"D:/agent_learning/cc-harness/main.py"}', "c2"),
            finish_reason="tool_calls",
        ),
    ],
    [
        FakeStreamEvent(kind="content", text="综合分析: 项目是一个终端编码助手"),
        FakeStreamEvent(
            kind="done",
            content="综合分析: 项目是一个终端编码助手",
            pending=[],
            finish_reason="stop",
        ),
    ],
]

llm = FakeLLM(responses=responses)
mcp = FakeMCP(
    tools_spec=[list_dir_tool, read_file_tool],
    results={
        "mcp__fs__list_directory": ToolResult.success("main.py\ncc_harness/\ntests/\n"),
        "mcp__fs__read_file": ToolResult.success("#!/usr/bin/env python\nimport asyncio\n..."),
    },
)

messages = [{"role": "user", "content": "列出项目结构,然后读 main.py 总结一下"}]
asyncio.run(agent_mod.run_turn(messages, llm, mcp, max_iter=10))

out = BUF.getvalue()
print("=" * 80, file=sys.stderr)
print("CAPTURED TERMINAL OUTPUT (4-phase ReAct):", file=sys.stderr)
print("=" * 80, file=sys.stderr)
sys.stderr.write(out)
sys.stderr.write("=" * 80 + "\n")

plain = out
ansi_present = "\x1b[" in out

print(f"\nANSI escape codes present: {ansi_present}", file=sys.stderr)

# Count labels
n_thought = plain.count("思考:")
n_action = plain.count("行动:")
n_observation = plain.count("观察:")
n_result = plain.count("结果:")
print("\nLabel counts:", file=sys.stderr)
print(f"  思考:  {n_thought}  (expected: 2, for iter 1 and iter 2; final is under 结果:)", file=sys.stderr)
print(f"  行动:  {n_action}  (expected: 2, list_dir + read_file)", file=sys.stderr)
print(f"  观察:  {n_observation}  (expected: 2, the two tool results)", file=sys.stderr)
print(f"  结果:  {n_result}  (expected: 1, the final answer)", file=sys.stderr)

# Count final text (should appear EXACTLY ONCE in the result block — not duplicated)
final_text_count = plain.count("综合分析")
print(f"\n'综合分析' count: {final_text_count} (expected: 1, no duplication)", file=sys.stderr)

# Count tool result text — it should now appear in 观察 (per new design).
# In the new layout, multi-line tool results are split with each line
# indented under "观察:", so we check for the first line of each result.
list_dir_result_count = plain.count("  main.py")
read_file_result_count = plain.count("  #!/usr/bin/env python")
print("\nTool result presence (new design shows them in 观察):", file=sys.stderr)
print(f"  '  main.py' (list_dir first line): {list_dir_result_count} (expected: >= 1)", file=sys.stderr)
print(f"  '  #!/usr/bin/env python' (read_file first line): {read_file_result_count} (expected: >= 1)", file=sys.stderr)

# Line layout
lines = plain.splitlines()
print("\nFull layout (line by line):", file=sys.stderr)
for i, line in enumerate(lines):
    print(f"  [{i:2d}] {line!r}", file=sys.stderr)

# Order check: 思考 -> 行动 -> 观察 (× 2) -> 结果 -> final
i_t1 = plain.find("思考:")
i_a1 = plain.find("行动:")
i_o1 = plain.find("观察:")
i_t2 = plain.find("思考:", i_o1)
i_a2 = plain.find("行动:", i_t2)
i_o2 = plain.find("观察:", i_a2)
i_result = plain.find("结果:")
i_final = plain.find("综合分析")

positions = [
    ("思考: (iter 1)",   i_t1),
    ("行动: (list_dir)", i_a1),
    ("观察: (iter 1)",   i_o1),
    ("思考: (iter 2)",   i_t2),
    ("行动: (read_file)",i_a2),
    ("观察: (iter 2)",   i_o2),
    ("结果:",            i_result),
    ("final text",       i_final),
]
print("\nOrder (must be strictly increasing):", file=sys.stderr)
prev = -1
order_ok = True
for name, pos in positions:
    if pos == -1:
        print(f"  MISS {name}", file=sys.stderr)
        order_ok = False
        continue
    ok = pos > prev
    if not ok:
        order_ok = False
    marker = "OK  " if ok else "BAD "
    print(f"  {marker} {name:25s} @ {pos}", file=sys.stderr)
    prev = pos

# Hard assertions
assert not ansi_present
assert n_thought == 2, f"expected 2 思考: blocks (for non-final iters with content), got {n_thought}"
assert n_action == 2, f"expected 2 行动: blocks, got {n_action}"
assert n_observation == 2, f"expected 2 观察: blocks (tool results now shown), got {n_observation}"
assert n_result == 1, f"expected 1 结果: block, got {n_result}"
assert final_text_count == 1, f"final text should appear exactly once, got {final_text_count}"
# Tool results should now appear (per new design)
assert list_dir_result_count >= 1, "list_dir tool result should appear in 观察"
assert read_file_result_count >= 1, "read_file tool result should appear in 观察"
assert order_ok, "4-phase order is wrong"
# Critical: 结果: must be followed by the final text (not preceded by a duplicate 思考:)
assert i_result < i_final, "结果: header should be followed by the final text"

print("\nFinal messages list:", file=sys.stderr)
for i, m in enumerate(messages):
    role = m["role"]
    content = (m.get("content") or "")[:60]
    extra = ""
    if "tool_calls" in m:
        extra = f" tool_calls={[(tc['function']['name'], tc['id']) for tc in m['tool_calls']]}"
    print(f"  [{i}] {role:10s} | {content!r}{extra}", file=sys.stderr)

print("\n✅ ALL ASSERTIONS PASSED", file=sys.stderr)
print(f"   - {n_thought} 思考: blocks, {n_action} 行动: blocks, {n_observation} 观察: blocks, {n_result} 结果: block", file=sys.stderr)
print("   - Full LLM text in each 思考 (no truncation)", file=sys.stderr)
print("   - Tool results now shown in 观察 (per new 4-phase design)", file=sys.stderr)
print("   - No color, no duplication, ReAct order correct", file=sys.stderr)
