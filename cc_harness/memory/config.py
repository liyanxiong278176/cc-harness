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
                     "pipeline_recent_turns", "pipeline_max_delta_tokens")
    @classmethod
    def _check_positive_int(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"must be > 0, got {v}")
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
