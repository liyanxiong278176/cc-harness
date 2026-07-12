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
