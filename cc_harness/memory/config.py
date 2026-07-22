from __future__ import annotations
from pathlib import Path
from pydantic import BaseModel, field_validator


# `MemoryConfigError` extends `cc_harness.config.ConfigError`, but that module
# imports from this one — a hard circular dependency. We resolve the parent
# class lazily via a module-level `__getattr__` (PEP 562) so the base class
# lookup only happens after both modules are fully loaded. This is functionally
# equivalent to `class MemoryConfigError(ConfigError)` for callers.
def _build_memory_config_error():
    """Build the `MemoryConfigError` class lazily, after `cc_harness.config`
    has finished loading (so we don't trigger the circular import).

    Result is memoized — repeat calls return the same class object, which is
    what lets `pytest.raises(MemoryConfigError)` match exceptions raised from
    `model_post_init`.
    """
    from cc_harness.config import ConfigError
    return type(
        "MemoryConfigError",
        (ConfigError,),
        {"__doc__": "Raised when MemoryConfig is invalid (e.g. enabled=True with missing embedding config)."},
    )


# PEP 562 module-level `__getattr__` is only invoked on attribute MISS, so we
# need to install the class into the module namespace ourselves. Because the
# actual raise site calls `__getattr__` (a plain function call that bypasses
# the module's `__getattr__` machinery), we re-use the same singleton by
# caching it on the module.
def __getattr__(name: str):
    if name == "MemoryConfigError":
        cls = _build_memory_config_error()
        globals()["MemoryConfigError"] = cls
        return cls
    raise AttributeError(name)


def _get_memory_config_error():
    """Internal accessor used by `model_post_init` so the raise site always
    gets the same class object as `from ... import MemoryConfigError`."""
    cached = globals().get("MemoryConfigError")
    if cached is not None:
        return cached
    cls = _build_memory_config_error()
    globals()["MemoryConfigError"] = cls
    return cls


class MemoryConfig(BaseModel):
    # Default `enabled=False` so direct instantiation (e.g. `AppConfig(...)` in
    # tests, REPL start without `.env`) doesn't require embedding env vars to
    # be set. `load_config` flips this to True once EMBEDDING_* are present.
    enabled: bool = False
    db_base_dir: Path = Path.home() / ".cc-harness" / "memory"
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    embedding_dim: int = 1024
    pipeline_threshold: float = 0.55
    pipeline_recent_turns: int = 10
    pipeline_max_delta_tokens: int = 4000
    retriever_top_k: int = 5
    injection_token_budget: int = 800
    embed_timeout_s: float = 10.0
    # Q3 分层记忆(L0-L3)字段
    pipeline_every_n: int = 5            # L0→L1 提取触发周期(每 N 轮)
    scenario_min_atoms: int = 8          # L1→L2 场景聚合的最小原子数
    persona_trigger_every_n: int = 50    # L2→L3 画像刷新触发周期
    recall_top_k: int = 5                # L3 分层召回每层 top_k
    recall_timeout_s: float = 5.0        # 召回超时(秒,float)
    # kill-switches(对应三层管线)
    layered_inject: bool = True          # pre-turn 分层注入开关
    capture_enabled: bool = True         # L0 会话录制开关
    pipeline_enabled: bool = True        # L1 提取 + L2/L3 聚合开关
    # Q4 短期符号化卸载字段
    offload_enabled: bool = True            # 总开关:false = 不卸载,胖结果原样留历史
    offload_threshold: int = 2000           # 单节点 tool-call 结果 token 上限,超过则卸载
    offload_ratio: float = 0.5              # 占 context_window 比例,达此线触发批量卸载
    mermaid_max_token_ratio: float = 0.2    # Mermaid canvas 注入 token 预算(占窗口比例)
    offload_canvas_inject: bool = True      # 是否在 system 段注入 Mermaid task canvas

    # E4 维护
    maintenance_enabled: bool = True
    maintenance_every_n_turns: int = 5
    maintenance_count_threshold: int = 50
    maintenance_interval_s: float = 3600.0
    # staleness (D5)
    staleness_half_life_days: float = 30.0
    staleness_llm_recheck_enabled: bool = True
    # TTL (D3)
    ttl_staleness_threshold: float = 0.85
    ttl_limit: int = 100
    # consolidation (D4)
    consolidation_similarity_threshold: float = 0.15
    consolidation_max_cluster_size: int = 5
    # recall decay (D7)
    recall_staleness_floor: float = 0.7
    recall_staleness_soft: float = 0.5
    recall_weight_floor: float = 0.5
    # E2 反思节点
    reflection_enabled: bool = True
    reflection_every_n_turns: int = 10
    reflection_max_pending: int = 3
    reflection_drain_timeout_s: float = 5.0

    # E5 漂移检测
    drift_enabled: bool = True
    drift_every_n_turns: int = 5
    drift_drift_warn_threshold: float = 0.2

    @field_validator("pipeline_threshold")
    @classmethod
    def _check_threshold(cls, v: float) -> float:
        if not (0 < v < 1):
            raise ValueError(f"threshold must be in (0, 1), got {v}")
        return v

    @field_validator("embedding_dim")
    @classmethod
    def _check_dim(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"embedding_dim must be > 0, got {v}")
        return v

    @field_validator("injection_token_budget", "retriever_top_k",
                     "pipeline_recent_turns", "pipeline_max_delta_tokens",
                     "pipeline_every_n", "scenario_min_atoms",
                     "persona_trigger_every_n", "recall_top_k",
                     "offload_threshold",
                     "maintenance_every_n_turns", "maintenance_count_threshold",
                     "ttl_limit",
                     "drift_every_n_turns")
    @classmethod
    def _check_positive_int(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"must be > 0, got {v}")
        return v

    @field_validator("recall_timeout_s", "maintenance_interval_s",
                     "reflection_drain_timeout_s")
    @classmethod
    def _check_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"must be > 0, got {v}")
        return v

    @field_validator("reflection_every_n_turns", "reflection_max_pending")
    @classmethod
    def _check_reflection_int(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"must be > 0, got {v}")
        return v

    @field_validator("offload_ratio")
    @classmethod
    def _check_offload_ratio(cls, v: float) -> float:
        # offload_ratio 必须严格 < Plan3 tier1_threshold(0.6,
        # 见 cc_harness/config.py:ContextConfig.tier1_threshold)。否则卸载会在
        # tier1 Snip 之前抢跑,破坏 Plan3 的压缩层级顺序。
        # 字面 0.6 比较:tier1 若调整,此处需同步(留 comment 防 drift)。
        # 两分支统一抛 ValueError:pydantic 统包为 ValidationError,与
        # _check_positive_int / _check_mermaid_ratio 风格一致(非对称 MemoryConfigError
        # 会让 `except MemoryConfigError` 漏接负值分支)。
        if v >= 0.6:
            raise ValueError(f"offload_ratio must be < 0.6 (Plan3 tier1_threshold), got {v}")
        if v <= 0:
            raise ValueError(f"offload_ratio must be > 0, got {v}")
        return v

    @field_validator("mermaid_max_token_ratio")
    @classmethod
    def _check_mermaid_ratio(cls, v: float) -> float:
        if not (0 < v < 1):
            raise ValueError(f"mermaid_max_token_ratio must be in (0, 1), got {v}")
        return v

    @field_validator("recall_staleness_soft", "recall_weight_floor")
    @classmethod
    def _check_recall_range(cls, v: float) -> float:
        if not (0 < v < 1):
            raise ValueError(f"must be in (0, 1), got {v}")
        return v

    @field_validator("drift_drift_warn_threshold")
    @classmethod
    def _check_drift_threshold(cls, v: float) -> float:
        if not (0 < v <= 1):
            raise ValueError(f"drift_drift_warn_threshold must be in (0, 1], got {v}")
        return v

    def model_post_init(self, __context) -> None:
        if self.enabled:
            missing = [n for n, v in [
                ("embedding_base_url", self.embedding_base_url),
                ("embedding_api_key", self.embedding_api_key),
                ("embedding_model", self.embedding_model),
            ] if not v]
            if missing:
                # Late-bound lookup via module helper to dodge circular import
                # while still returning the same class object on every call
                # (so `pytest.raises(MemoryConfigError)` can match).
                raise _get_memory_config_error()(
                    f"memory enabled but missing: {', '.join(missing)}. "
                    "Set EMBEDDING_BASE_URL / EMBEDDING_API_KEY / EMBEDDING_MODEL in .env, "
                    "or set MEMORY_ENABLED=false."
                )


# Re-export `load_memory_config`(定义在 cc_harness.config)以保持
# `from cc_harness.memory.config import load_memory_config` 单一入口。放在模块**末尾**
# 以避免循环:此时本模块的 MemoryConfig 已定义完毕,cc_harness.config 内的
# load_memory_config 用函数体内 lazy import 拿 MemoryConfig,不会在 import 期触发回环。
from cc_harness.config import load_memory_config  # noqa: E402

__all__ = ["MemoryConfig", "load_memory_config"]
