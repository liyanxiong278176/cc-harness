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
