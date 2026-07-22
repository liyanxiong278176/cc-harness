"""E5 T1.1 — MemoryConfig drift 字段 + validator 测试。"""
from __future__ import annotations
import pytest
from pydantic import ValidationError

from cc_harness.memory.config import MemoryConfig


def test_drift_config_defaults():
    """默认值:drift_enabled=True, every_n=5, threshold=0.2"""
    cfg = MemoryConfig()
    assert cfg.drift_enabled is True
    assert cfg.drift_every_n_turns == 5
    assert cfg.drift_drift_warn_threshold == 0.2


def test_drift_threshold_must_be_in_range():
    """threshold 必须 (0, 1]: 1.5 抛 / 0.0 抛 / 0.1 pass / 1.0 pass"""
    with pytest.raises(ValidationError):
        MemoryConfig(drift_drift_warn_threshold=1.5)
    with pytest.raises(ValidationError):
        MemoryConfig(drift_drift_warn_threshold=0.0)
    # 边界值
    assert MemoryConfig(drift_drift_warn_threshold=0.1).drift_drift_warn_threshold == 0.1
    assert MemoryConfig(drift_drift_warn_threshold=1.0).drift_drift_warn_threshold == 1.0


def test_drift_every_n_turns_must_be_positive():
    """drift_every_n_turns 必须 > 0(沿 _check_positive_int 模式)"""
    with pytest.raises(ValidationError):
        MemoryConfig(drift_every_n_turns=0)
    with pytest.raises(ValidationError):
        MemoryConfig(drift_every_n_turns=-1)
    # 默认 5 pass
    assert MemoryConfig(drift_every_n_turns=5).drift_every_n_turns == 5


def test_drift_disabled_noop():
    """enabled=False 时 cfg 不抛错。"""
    cfg = MemoryConfig(drift_enabled=False)
    assert cfg.drift_enabled is False


def test_drift_subpackage_imports():
    """cc_harness.drift 可 import,prompts 常量存在。"""
    from cc_harness.drift import prompts
    assert hasattr(prompts, "JUDGE_ENTITIES")
    assert hasattr(prompts, "JUDGE_GROUP_CONSIST")
    assert isinstance(prompts.JUDGE_ENTITIES, str)
    assert isinstance(prompts.JUDGE_GROUP_CONSIST, str)
    # 子包 import 不应 crash(DriftDetector 暂未实现可 try/except 兼容)
    import cc_harness.drift  # noqa: F401
