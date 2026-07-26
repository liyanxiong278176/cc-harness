"""build_runtime:共享 main.py:boot() 的 wiring 逻辑。

Phase 1 / Task 10 的核心抽象:把 main.py:boot() 内部 LLM / MCP / memory / scheduler
/ reflection / drift / checkpoint / web_session_store 装配抽到一处,让 web 与 REPL
共用同一份 boot 路径。当前 REPL 仍走 main.py:boot() 原版(1452 测试不变),
后续可重构指向 build_runtime。
"""
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class RuntimeContext:
    """所有 boot 时装出来的 wiring 组件。

    字段类型用 ``Any`` 占位(boot 装配后才有真类型),call site 按需类型注解。
    REPL 路径仍走 main.py:boot() 原版。
    """
    llm: Any
    mcp: Any
    mem_deps: dict | None
    scheduler: Any
    reflection_engine: Any
    drift_detector: Any
    checkpoint_service: Any
    web_session_store: Any
    mcp_config: Any
    policy: Any
    exec_cfg: Any


async def build_runtime(
    project_root: Path,
    env_path: Path,
    mcp_json_path: Path,
) -> RuntimeContext:
    """复用 main.py:boot() 的所有 wiring。

    实现:从 main.py 复制 boot() 内部逻辑(LLM/MCP/memory/scheduler/reflection/drift/checkpoint),
    返回 dataclass。REPL 路径仍用 main.py:boot() 原版(后续可重构指向 build_runtime)。

    失败路径 graceful:
        - ConfigError → cfg=None(LLM 拿 no-key 占位,后续构造仍 not None)
        - mcp.start() 失败 → best-effort pass,空 server list
        - memory init 失败 → mem_deps=None,scheduler/reflection/drift 全 None
        - CheckpointService 构造失败 → checkpoint_service=None
    """
    from cc_harness.config import (
        load_config, ConfigError, load_executor_config, load_policy_config,
    )
    from cc_harness.llm import LLMClient
    from cc_harness.mcp_client import MCPClient

    try:
        cfg = load_config(env_path=env_path, mcp_json_path=mcp_json_path)
    except ConfigError:
        # Fall back to no-key config(允许测试无 LLM key 跑 boot smoke)
        cfg = None

    llm = LLMClient(
        api_key=cfg.openai_api_key if cfg else "no-key",
        model=cfg.openai_model if cfg else "no-model",
        base_url=cfg.openai_base_url if cfg else "https://example.invalid",
    )

    mcp = MCPClient(cfg.mcp_servers if cfg else {})
    try:
        await mcp.start()
    except Exception:
        # boot best-effort — 空 server list 或单个 server 失败都不破 boot
        pass

    # E4 I-1: 提前构造 memory deps + scheduler — 让 4 件 background op
    # (staleness / TTL / consolidation / conflict) + RecallWeighter 在
    # boot 后立刻生效。REPL 路径仍走 main.py 原 boot(),run_repl 接收
    # mem_deps 注入 run_turn,scheduler 注入 _after_turn_memory。
    from dotenv import dotenv_values as _dotenv
    _mem_env = {**os.environ, **{k: v for k, v in _dotenv(env_path).items() if v}}
    from cc_harness.memory.extras import build_memory_extras as _bme
    from cc_harness.memory.config import load_memory_config as _lmc
    from cc_harness.memory.maintenance.scheduler import MaintenanceScheduler as _MSS

    # Ensure logs/ exists (build_memory_extras assumes db_path.parent exists)
    (project_root / "logs").mkdir(parents=True, exist_ok=True)
    try:
        _memory_extras, _mem_deps = await _bme(
            _mem_env, project_root / "logs" / "memory.db",
        )
    except Exception:
        # memory init hiccup(import / sqlite-vec / embedding 失败)→ silent None
        _memory_extras, _mem_deps = [], None

    _mem_cfg = _lmc(project_root / "policy.yaml")
    _scheduler = (
        _MSS(
            store=_mem_deps["store"],
            service=_mem_deps["service"],
            llm=llm,
            every_n_turns=_mem_cfg.maintenance_every_n_turns,
            count_threshold=_mem_cfg.maintenance_count_threshold,
            interval_s=_mem_cfg.maintenance_interval_s,
            enabled=_mem_cfg.maintenance_enabled,
        )
        if _mem_deps is not None
        else None
    )
    if _scheduler is not None:
        # consolidation / conflict 需要 embedder;extras.py 不暴露到 deps dict,
        # 从 service.embedder 取出后置注入。
        _svc = _mem_deps.get("service")
        if _svc is not None and getattr(_svc, "embedder", None) is not None:
            _scheduler._embedder = _svc.embedder
        # staleness LLM recheck + D5/D7 配置
        _scheduler._half_life_days = _mem_cfg.staleness_half_life_days
        _scheduler._llm_recheck_enabled = _mem_cfg.staleness_llm_recheck_enabled
        _scheduler._ttl_threshold = _mem_cfg.ttl_staleness_threshold
        _scheduler._ttl_limit = _mem_cfg.ttl_limit
        _scheduler._consol_threshold = _mem_cfg.consolidation_similarity_threshold
        _scheduler._consol_max = _mem_cfg.consolidation_max_cluster_size

    # E2 T2.3: 构造 ReflectionEngine(同 E4 I-1 wiring 模式)。
    from cc_harness.reflection.engine import ReflectionEngine as _RE
    from cc_harness.l5 import build_l5_engine as _b5e
    from cc_harness.config import load_l5_config as _l5c

    _judge_base = os.getenv("JUDGE_BASE_URL")
    _judge_key = os.getenv("JUDGE_API_KEY")
    _judge_model = os.getenv("JUDGE_MODEL")
    _judge_llm = (
        LLMClient(
            api_key=_judge_key,
            model=_judge_model,
            base_url=_judge_base,
        )
        if (_judge_base and _judge_key and _judge_model) else None
    )
    _l5_engine = _b5e(_l5c(project_root / "policy.yaml"))
    _reflection_engine = (
        _RE(
            memory_service=_mem_deps["service"],
            llm_client=llm,
            judge_llm=_judge_llm,
            l5_engine=_l5_engine,
            project_root=project_root,
            enabled=_mem_cfg.reflection_enabled,
            every_n_turns=_mem_cfg.reflection_every_n_turns,
            max_pending=_mem_cfg.reflection_max_pending,
            drain_timeout_s=_mem_cfg.reflection_drain_timeout_s,
        )
        if _mem_deps is not None
        else None
    )

    # E5 漂移检测(沿 E4 I-1 / E2 T2.3 wiring 模式)
    from cc_harness.drift.detector import DriftDetector
    _drift_detector = (
        DriftDetector(
            reflection_engine=_reflection_engine,
            judge_llm=_judge_llm,
            local_llm=llm,
            l5_engine=_l5_engine,
            project_root=project_root,
            audit_path=project_root / "logs" / "drift.jsonl",
            every_n_turns=_mem_cfg.drift_every_n_turns,
            enabled=_mem_cfg.drift_enabled,
        )
        if _mem_deps is not None and _reflection_engine is not None
        else None
    )

    # 注入到 memory service / retriever
    if _mem_deps is not None and _drift_detector is not None:
        _mem_deps["service"].drift_detector = _drift_detector
        if "retriever" in _mem_deps and _mem_deps["retriever"] is not None:
            _mem_deps["retriever"].drift_detector = _drift_detector

    # E3 T7:构造 CheckpointService(同 E4 I-1 / E2 T2.3 wiring pattern)。
    from cc_harness.memory.checkpoint import CheckpointService, WebSessionStore

    mem_store = _mem_deps.get("store") if _mem_deps else None
    _checkpoint_service = (
        CheckpointService(mem_store) if mem_store is not None else None
    )
    _web_session_store = (
        WebSessionStore(mem_store) if mem_store is not None else None
    )

    # Pre-warm sandbox server when backend=sandbox.
    exec_cfg = load_executor_config(project_root / "policy.yaml")
    _policy = load_policy_config(project_root / "policy.yaml")

    return RuntimeContext(
        llm=llm,
        mcp=mcp,
        mem_deps=_mem_deps,
        scheduler=_scheduler,
        reflection_engine=_reflection_engine,
        drift_detector=_drift_detector,
        checkpoint_service=_checkpoint_service,
        web_session_store=_web_session_store,
        mcp_config=cfg,
        policy=_policy,
        exec_cfg=exec_cfg,
    )