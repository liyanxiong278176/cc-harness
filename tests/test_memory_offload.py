"""Q4 短期卸载 unit。mock LLM/token_counter。"""
import pytest


def test_offload_result_dataclass():
    from cc_harness.memory.offload.models import OffloadResult
    r = OffloadResult(node_id="n1", summary="编译成功", refs_path="/tmp/refs/n1.md",
                      pointer_msg="[offloaded node=n1]")
    assert r.node_id == "n1" and "编译成功" in r.summary


def test_memory_config_offload_fields():
    from cc_harness.memory.config import MemoryConfig
    c = MemoryConfig()
    assert c.offload_enabled is True and c.offload_threshold == 2000
    assert c.offload_ratio == 0.5 and c.mermaid_max_token_ratio == 0.2
    assert c.offload_canvas_inject is True


def test_memory_config_offload_ratio_lt_tier1():
    """validator:offload_ratio >= 0.6(Plan3 tier1)→ MemoryConfigError。"""
    from cc_harness.memory.config import MemoryConfig, MemoryConfigError
    from pydantic import ValidationError
    with pytest.raises((MemoryConfigError, ValidationError)):
        MemoryConfig(offload_ratio=0.7)
    with pytest.raises((MemoryConfigError, ValidationError)):
        MemoryConfig(offload_ratio=0.6)   # strict boundary: == 0.6 also rejected


def test_load_memory_config_offload_env(tmp_path, monkeypatch):
    """load_memory_config 读 MEMORY_OFFLOAD_ENABLED=false → offload_enabled False。"""
    from cc_harness.memory.config import load_memory_config
    monkeypatch.setenv("MEMORY_OFFLOAD_ENABLED", "false")
    c = load_memory_config(tmp_path / "no.yaml")  # 无 yaml,env 生效
    assert c.offload_enabled is False


# --- Task 2: maybe_offload (refs + LLM summary + pointer) ---

@pytest.mark.asyncio
async def test_maybe_offload_large(tmp_path):
    """result token > threshold → refs/{node_id}.md + pointer + OffloadResult。"""
    from cc_harness.memory.offload.offload import maybe_offload
    from cc_harness.tokens import TokenCounter
    refs_dir = tmp_path / "refs"
    refs_dir.mkdir()
    big = "x " * 3000
    class FakeLLM:
        async def chat(self, msgs, tools):
            from cc_harness.llm import StreamEvent
            yield StreamEvent(kind="done", content="LLM 摘要")
    out = await maybe_offload(big, "run_command", {"cmd": "pytest"}, threshold=2000,
                              refs_dir=refs_dir, llm=FakeLLM(), token_counter=TokenCounter())
    assert out is not None
    assert (refs_dir / f"{out.node_id}.md").exists()
    assert (refs_dir / f"{out.node_id}.md").read_text(encoding="utf-8") == big
    assert out.node_id in out.pointer_msg and "LLM 摘要" in out.summary


@pytest.mark.asyncio
async def test_maybe_offload_small(tmp_path):
    from cc_harness.memory.offload.offload import maybe_offload
    from cc_harness.tokens import TokenCounter
    out = await maybe_offload("短结果", "t", {}, threshold=2000, refs_dir=tmp_path / "r",
                              llm=None, token_counter=TokenCounter())
    assert out is None


@pytest.mark.asyncio
async def test_maybe_offload_threshold_boundary(tmp_path):
    """严格 >:token==threshold 不卸,== threshold+1 卸。

    tiktoken drift:"a " * N → N+1 tokens(尾空格合并不了)。
    故 N=1999 → 2000 tokens(== threshold,不卸);N=2000 → 2001 tokens(> threshold,卸)。
    """
    from cc_harness.memory.offload.offload import maybe_offload
    from cc_harness.tokens import TokenCounter
    tc = TokenCounter()
    at_thr = "a " * 1999   # tiktoken → 2000 == threshold
    assert tc.count_text(at_thr) == 2000
    assert await maybe_offload(at_thr, "t", {}, 2000, tmp_path / "r1", None, tc) is None
    over = "a " * 2000    # tiktoken → 2001 == threshold+1
    assert tc.count_text(over) == 2001
    out = await maybe_offload(over, "t", {}, 2000, tmp_path / "r2", None, tc)
    assert out is not None  # llm=None fail-soft 仍卸


@pytest.mark.asyncio
async def test_maybe_offload_llm_none_fail_soft(tmp_path):
    """llm=None → 存 refs + summary 前 200 字,不调 LLM。"""
    from cc_harness.memory.offload.offload import maybe_offload
    from cc_harness.tokens import TokenCounter
    big = "事实 " * 1000
    out = await maybe_offload(big, "t", {}, 2000, tmp_path / "refs", None, TokenCounter())
    assert out is not None and len(out.summary) <= 200
    assert (tmp_path / "refs" / f"{out.node_id}.md").exists()


@pytest.mark.asyncio
async def test_node_id_three_way_consistent(tmp_path):
    """node_id 三处字面一致:refs 文件名 == refs_path 后缀 == pointer_msg node=。"""
    from cc_harness.memory.offload.offload import maybe_offload
    from cc_harness.tokens import TokenCounter
    out = await maybe_offload("z " * 3000, "run_command", {}, 2000, tmp_path / "refs",
                              None, TokenCounter())
    refs_name = next((tmp_path / "refs").glob("*.md")).stem  # n1
    assert refs_name == out.node_id                    # refs 文件名
    assert f"node={out.node_id}" in out.pointer_msg    # pointer_msg
    assert out.refs_path.endswith(f"{out.node_id}.md")  # refs_path


@pytest.mark.asyncio
async def test_maybe_offload_llm_failure_falls_back(tmp_path):
    """llm raises or returns empty → summary falls back to result_text[:200] (not '')。"""
    from cc_harness.memory.offload.offload import maybe_offload
    from cc_harness.tokens import TokenCounter
    big = "事实 " * 1000
    expected = big[:200]

    class RaisingLLM:
        async def chat(self, msgs, tools):
            raise RuntimeError("network down")
            yield  # noqa — unreachable;仅作 async generator 标记,首帧即 raise

    class EmptyLLM:
        async def chat(self, msgs, tools):
            from cc_harness.llm import StreamEvent
            yield StreamEvent(kind="done", content="")  # empty content

    out1 = await maybe_offload(big, "t", {}, 2000, tmp_path / "r1", RaisingLLM(), TokenCounter())
    assert out1 is not None and out1.summary == expected

    out2 = await maybe_offload(big, "t", {}, 2000, tmp_path / "r2", EmptyLLM(), TokenCounter())
    assert out2 is not None and out2.summary == expected


# --- Task 3: update_canvas (Mermaid graph LR + node_id literal + edge chaining) ---


@pytest.mark.asyncio
async def test_update_canvas_appends_node(tmp_path):
    """LLM 路径:node_id 字面入 Mermaid 节点 id,二次调用 chain edge;append 不重写头。"""
    from cc_harness.memory.offload.mermaid import update_canvas
    canvas_path = tmp_path / "canvas.md"

    class FakeLLM:
        async def chat(self, msgs, tools):
            from cc_harness.llm import StreamEvent
            yield StreamEvent(kind="done", content="读取文件")

    # 首节点:无 edge_from
    out1 = await update_canvas(
        node_id="n1", label="read_file", summary="读了 README",
        edge_from=None, canvas_path=canvas_path, llm=FakeLLM(),
    )
    assert isinstance(out1, str)
    assert "graph LR" in out1
    assert "n1" in out1                      # node_id 字面
    assert 'n1["读取文件"]' in out1           # LLM 产标签 + node_id 锁前缀
    assert canvas_path.exists()
    on_disk = canvas_path.read_text(encoding="utf-8")
    assert "graph LR" in on_disk and 'n1["读取文件"]' in on_disk
    assert "--> n1" not in out1              # 首节点无 edge

    # 二节点 chain:edge_from=n1
    out2 = await update_canvas(
        node_id="n2", label="grep", summary="grep 关键字",
        edge_from="n1", canvas_path=canvas_path, llm=FakeLLM(),
    )
    assert "n1 --> n2" in out2               # 链边
    assert 'n2["读取文件"]' in out2           # 第二节点
    assert "n1" in out2                      # 首节点仍在(append 不重写)
    # Mermaid 头只一次(append 不重复 graph LR)
    assert out2.count("graph LR") == 1


@pytest.mark.asyncio
async def test_update_canvas_llm_none_fail_soft(tmp_path):
    """llm=None → 确定性节点 {node_id}["{label}"] + edge chain;
    LLM 异常/空 → 同确定性 fallback。父目录自动建。"""
    from cc_harness.memory.offload.mermaid import update_canvas
    canvas_path = tmp_path / "sub" / "canvas.md"  # 父目录不存在 → 应自动建

    # 首节点(llm=None,无 edge_from)
    out1 = await update_canvas(
        node_id="n1", label="run_command", summary="跑了 pytest",
        edge_from=None, canvas_path=canvas_path, llm=None,
    )
    assert isinstance(out1, str) and canvas_path.exists()
    assert 'n1["run_command"]' in out1        # 确定性节点(label 作可见文本)
    assert "--> n1" not in out1               # 首节点无 edge

    # 二节点 chain(llm=None)
    out2 = await update_canvas(
        node_id="n2", label="read_file", summary="读结果",
        edge_from="n1", canvas_path=canvas_path, llm=None,
    )
    assert "n1 --> n2" in out2
    assert 'n2["read_file"]' in out2

    # LLM 抛异常 → fail-soft 回确定性节点
    class RaisingLLM:
        async def chat(self, msgs, tools):
            raise RuntimeError("down")
            yield  # async gen 标记,unreachable

    canvas2 = tmp_path / "raise.md"
    out3 = await update_canvas(
        node_id="x1", label="t", summary="s",
        edge_from=None, canvas_path=canvas2, llm=RaisingLLM(),
    )
    assert 'x1["t"]' in out3                  # 回退到确定性 label

    # LLM 返回空 content → fail-soft
    class EmptyLLM:
        async def chat(self, msgs, tools):
            from cc_harness.llm import StreamEvent
            yield StreamEvent(kind="done", content="")

    canvas3 = tmp_path / "empty.md"
    out4 = await update_canvas(
        node_id="y1", label="t2", summary="s2",
        edge_from=None, canvas_path=canvas3, llm=EmptyLLM(),
    )
    assert 'y1["t2"]' in out4                  # 空 content 也回退 label


# --- Task 4: read_ref 工具 + extras deps offload 锭 + node_id 溯源链 ---


@pytest.mark.asyncio
async def test_read_ref_handler(tmp_path):
    from cc_harness.memory.offload.read_ref import read_ref_handler, READ_REF_SPEC
    refs_dir = tmp_path / "refs"
    refs_dir.mkdir()  # test-only:create refs root so the write succeeds
    (refs_dir / "n1.md").write_text("完整原文", encoding="utf-8")
    r = await read_ref_handler({"node_id": "n1"}, cwd=str(tmp_path), refs_dir=refs_dir)
    assert "完整原文" in r.llm_text
    assert READ_REF_SPEC["function"]["name"] == "read_ref"


@pytest.mark.asyncio
async def test_node_id_traceability(tmp_path):
    """溯源全链:offload → refs + pointer;read_ref(pointer.node_id) → refs 原文。"""
    from cc_harness.memory.offload.offload import maybe_offload
    from cc_harness.memory.offload.read_ref import read_ref_handler
    from cc_harness.tokens import TokenCounter
    refs_dir = tmp_path / "refs"
    out = await maybe_offload("原始大结果 " * 1000, "run_command", {}, 2000, refs_dir,
                              None, TokenCounter())
    # pointer 含 node_id → read_ref 回查
    assert "node=" in out.pointer_msg
    r = await read_ref_handler({"node_id": out.node_id}, cwd=str(tmp_path), refs_dir=refs_dir)
    assert "原始大结果" in r.llm_text  # 原文恢复


@pytest.mark.asyncio
async def test_extras_deps_has_offload(tmp_path, monkeypatch):
    """build_memory_extras deps 含 offload 锭(self-contained:mock 依赖)。"""
    import os
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://x")
    monkeypatch.setenv("OPENAI_MODEL", "m")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://x")
    monkeypatch.setenv("EMBEDDING_API_KEY", "k")
    monkeypatch.setenv("EMBEDDING_MODEL", "bge-m3")
    from cc_harness.memory.extras import build_memory_extras
    extras, deps = await build_memory_extras({**os.environ}, tmp_path / "mem.db")
    if deps is None:
        pytest.skip("memory deps 未就绪(依赖 init)")  # fail-soft 跳过
    assert "refs_dir" in deps and "canvas_path" in deps
    assert "offload" in deps and "canvas" in deps


@pytest.mark.asyncio
async def test_read_ref_handler_rejects_traversal(tmp_path):
    """read_ref 拒绝路径穿越 + 缺文件返 error(不 raise,不泄露)。"""
    from cc_harness.memory.offload.read_ref import read_ref_handler
    refs_dir = tmp_path / "refs"
    refs_dir.mkdir()
    # 写一个"诱饵"文件在 refs_dir 之外,验证不被读
    (tmp_path / "TOPSECRET.md").write_text("秘密内容", encoding="utf-8")

    for bad in ["../TOPSECRET", "..\\TOPSECRET", "/etc/passwd", "", "a/b", "n1.md"]:
        r = await read_ref_handler({"node_id": bad}, cwd=str(tmp_path), refs_dir=refs_dir)
        assert getattr(r, "is_error", False) or "秘密内容" not in getattr(r, "llm_text", ""), \
            f"node_id={bad!r} 应被拒"
        assert "秘密内容" not in getattr(r, "llm_text", "")  # 绝不泄露

    # 缺文件(valid node_id 但无 refs 文件)→ error 不 raise
    r = await read_ref_handler({"node_id": "ghost"}, cwd=str(tmp_path), refs_dir=refs_dir)
    assert "秘密内容" not in getattr(r, "llm_text", "")
    # 不 raise 即通过(函数应返 ToolResult 而非抛)


# --- Task 5: agent.py after-tool-call hook (allow + ask-yes) ---


@pytest.mark.asyncio
async def test_agent_after_tool_call_offloads(tmp_path):
    """allow 分支:tool result 超 threshold → maybe_offload 换 pointer + canvas + refs 落盘。

    用 MCP 工具(非 native run_command)避开 RunCommandArgs schema + native 真跑 shell
    + policy ASK 默认 no 三重坑(对齐 test_agent.py:56 范式)。
    """
    from cc_harness.agent import run_turn
    from tests.test_agent import FakeLLM, FakeMCP, FakeStreamEvent
    from cc_harness.mcp_client import ToolResult
    from cc_harness.memory.offload.offload import maybe_offload
    refs_dir = tmp_path / "refs"
    big = "y " * 3000

    async def _offload(result_text, tool_name, args, *, threshold, token_counter):
        return await maybe_offload(
            result_text, tool_name, args, threshold, refs_dir, None, token_counter)

    async def _canvas(node_id, label, summary, edge_from):  # canvas mock(防 KeyError)
        pass

    offload_deps = {"enabled": True, "threshold": 2000, "offload": _offload, "canvas": _canvas,
                    "canvas_inject": False, "refs_dir": refs_dir, "canvas_path": tmp_path / "c.md"}
    # 用 MCP 工具避开 native schema + policy ASK 三重坑
    fs_tool = {"type": "function", "function": {
        "name": "mcp__fs__read", "description": "r",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}
    from cc_harness.llm import PendingToolCall
    pending = [PendingToolCall(index=0, id="c1", name="mcp__fs__read",
                               arguments_json='{"path":"x"}')]
    events = [FakeStreamEvent(kind="done", content="read", pending=pending,
                              finish_reason="tool_calls")]
    events2 = [FakeStreamEvent(kind="done", content="done", finish_reason="stop")]
    llm = FakeLLM(responses=[events, events2])
    mcp = FakeMCP(tools_spec=[fs_tool],
                  results={"mcp__fs__read": ToolResult.success(big)}, calls=[])
    msgs = [{"role": "user", "content": "read x"}]
    await run_turn(msgs, llm, mcp, max_iter=5, mode="coding", offload_deps=offload_deps)
    tool_msg = next(m for m in msgs if m.get("role") == "tool")
    assert "offloaded" in tool_msg["content"] and big not in tool_msg["content"]
    assert list(refs_dir.glob("*.md"))  # refs 生成


@pytest.mark.asyncio
async def test_agent_after_tool_call_offloads_ask_yes(tmp_path, monkeypatch):
    """ask-yes 分支 wiring:policy ASK(shell)→ confirm "yes" → 执行 → offload。

    镜像 test_agent.py:test_agent_tool_call_log_ask_confirmed_marked_ok 范式:
    native run_command(policy 归 shell → ASK)+ monkeypatch handler 返大结果 +
    monkeypatch builtins.input → "yes" 驱动 confirm_tool。offload LOGIC 与 allow 分支
    共享同一 _maybe_offload_content 闭包,本测试断言 ask-yes WIRING(调用点接通)。
    """
    from cc_harness import agent as agent_mod
    from cc_harness.agent import run_turn
    from tests.test_agent import FakeLLM, FakeMCP, FakeStreamEvent
    from cc_harness.mcp_client import ToolResult
    from cc_harness.memory.offload.offload import maybe_offload
    from cc_harness.policy import PolicyEngine
    from cc_harness.llm import PendingToolCall

    refs_dir = tmp_path / "refs"
    big = "z " * 3000

    async def _fake_run_command(args, *, cwd="."):
        return ToolResult.success(big)

    async def _offload(result_text, tool_name, args, *, threshold, token_counter):
        return await maybe_offload(
            result_text, tool_name, args, threshold, refs_dir, None, token_counter)

    async def _canvas(node_id, label, summary, edge_from):
        pass

    offload_deps = {"enabled": True, "threshold": 2000, "offload": _offload, "canvas": _canvas,
                    "canvas_inject": False, "refs_dir": refs_dir, "canvas_path": tmp_path / "c.md"}

    # run_command → policy shell → ASK;替换 handler 避开真 shell
    monkeypatch.setitem(agent_mod.NATIVE_TOOLS["run_command"], "handler", _fake_run_command)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "yes")  # confirm_tool → "yes"

    pending = [PendingToolCall(index=0, id="c1", name="run_command",
                               arguments_json='{"command":"echo big"}')]
    llm = FakeLLM(responses=[
        [FakeStreamEvent(kind="done", content="", pending=pending, finish_reason="tool_calls")],
        [FakeStreamEvent(kind="done", content="done", pending=[], finish_reason="stop")],
    ])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])
    msgs = [{"role": "user", "content": "跑 echo"}]
    await run_turn(msgs, llm, mcp, mode="coding", cwd=str(tmp_path), max_iter=5,
                   policy=PolicyEngine(project_root=tmp_path), offload_deps=offload_deps)
    tool_msg = next(m for m in msgs if m.get("role") == "tool")
    # ask-yes 调用点接通:tool content 被换 pointer,big 不在,refs 落盘
    assert "offloaded" in tool_msg["content"], "ask-yes 分支未走 offload hook(wiring 断)"
    assert big not in tool_msg["content"]
    assert list(refs_dir.glob("*.md"))


@pytest.mark.asyncio
async def test_agent_offload_canvas_failure_keeps_pointer(tmp_path, monkeypatch):
    """canvas 抛异常 → pointer 仍 commit + refs 不孤儿 + _last_node 更新(Important fix)。

    两个大结果连续卸载:第一次 _canvas 抛 RuntimeError,第二次 _canvas 正常。
    断言:两次 tool content 都是 pointer(非原文);refs 各落一份(无孤儿);
    第二次 canvas 的 edge_from == 第一次的 node_id(_last_node 已推进,非陈旧)。
    """
    from cc_harness.agent import run_turn
    from tests.test_agent import FakeLLM, FakeMCP, FakeStreamEvent
    from cc_harness.mcp_client import ToolResult
    from cc_harness.memory.offload.offload import maybe_offload
    from cc_harness.llm import PendingToolCall

    refs_dir = tmp_path / "refs"
    big1 = "a " * 3000
    seen_edges: list = []   # 记录两次 canvas 的 edge_from

    async def _offload(result_text, tool_name, args, *, threshold, token_counter):
        return await maybe_offload(
            result_text, tool_name, args, threshold, refs_dir, None, token_counter)

    async def _canvas(node_id, label, summary, edge_from):
        seen_edges.append(edge_from)
        if edge_from is None:   # 首节点 canvas 模拟失败
            raise RuntimeError("canvas disk full")

    offload_deps = {"enabled": True, "threshold": 2000, "offload": _offload, "canvas": _canvas,
                    "canvas_inject": False, "refs_dir": refs_dir, "canvas_path": tmp_path / "c.md"}

    fs_tool = {"type": "function", "function": {
        "name": "mcp__fs__read", "description": "r",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}
    p1 = PendingToolCall(index=0, id="c1", name="mcp__fs__read", arguments_json='{"path":"x"}')
    p2 = PendingToolCall(index=0, id="c2", name="mcp__fs__read", arguments_json='{"path":"y"}')
    # 3 轮:两次 tool_call(同轮内两个 pending)+ 终答
    events1 = [FakeStreamEvent(kind="done", content="r1", pending=[p1, p2],
                               finish_reason="tool_calls")]
    events2 = [FakeStreamEvent(kind="done", content="done", pending=[], finish_reason="stop")]
    llm = FakeLLM(responses=[events1, events2])
    mcp = FakeMCP(tools_spec=[fs_tool],
                  results={"mcp__fs__read": ToolResult.success(big1)}, calls=[])
    # 两次调用都返 big1(内容不重要,只要超阈值;_offload 各自生成独立 node_id)
    mcp.results["mcp__fs__read"] = ToolResult.success(big1)
    msgs = [{"role": "user", "content": "read x and y"}]
    # 捕获 stdin 让 canvas 失败的 warn 不卡(无 input 需求,allow 分支)
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("exit\n"))
    await run_turn(msgs, llm, mcp, max_iter=5, mode="coding", offload_deps=offload_deps)

    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    # 两个 tool content 都是 pointer(canvas 失败不丢 pointer)
    for tm in tool_msgs:
        assert "offloaded" in tm["content"], "canvas 失败导致 pointer 丢失"
        assert big1 not in tm["content"]
    # refs 两份(无孤儿)
    assert len(list(refs_dir.glob("*.md"))) == 2
    # _last_node 推进:第二次 canvas edge_from 是第一次的 node_id(非 None/非陈旧)
    # 从 pointer 提取首次 node_id,验证 seen_edges[1] 指向它
    import re as _re
    first_node = _re.search(r"node=(\w+)", tool_msgs[0]["content"]).group(1)
    assert seen_edges[0] is None          # 首节点无前驱
    assert seen_edges[1] == first_node    # 二节点 edge 指向首节点(_last_node 已更新)
