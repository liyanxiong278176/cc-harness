"""Shared agent runtime used by every user-facing entrypoint."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from cc_harness.activation import ActivationManifest, CapabilityProfile
from cc_harness.agent import run_turn
from cc_harness.atomic import atomic_write_text
from cc_harness.config import (
    AppConfig,
    ConfigError,
    ExecutorBackend,
    load_executor_config,
    load_context_config,
    load_layered_config,
)
from cc_harness.capability_services import SharedCapabilityServices
from cc_harness.context import ContextProjection
from cc_harness.l2 import REFUSAL_TEMPLATE, scan_user_input
from cc_harness.llm import LLMClient
from cc_harness.loop_control import LoopControlConfig, completion_contract_from_instruction
from cc_harness.mcp_client import MCPClient
from cc_harness.policy import PolicyEngine
from cc_harness.project_instructions import load_project_instructions
from cc_harness.tool_bundles import parse_tool_bundles
from cc_harness.repl import ReplState, _after_turn_memory, _after_turn_todo, _extract_final_text
from cc_harness.session_store import SessionRecord, SessionStore
from cc_harness.tools import init_session_executor, shutdown_session_executor

EventEmitter = Callable[[dict], Awaitable[None]]
ConfirmHandler = Callable[[str, dict, str], Awaitable[str]]


def _effective_resume_mode(requested_mode: str | None, saved_mode: str | None) -> str:
    """Keep an explicit mode when resuming; otherwise use the saved mode."""

    return requested_mode or saved_mode or "coding"


def _env_flag(name: str) -> bool:
    """Return whether a boolean runtime environment flag is enabled."""

    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class RuntimeWarning:
    message: str


class SessionRuntime:
    """Own all backend dependencies for one interactive or print session."""

    def __init__(self) -> None:
        self.cwd = Path.cwd()
        self.additional_dirs: tuple[Path, ...] = ()
        self.config: AppConfig | None = None
        self.llm: LLMClient | None = None
        self.mcp: MCPClient | None = None
        self.state = ReplState()
        self.policy: PolicyEngine | None = None
        self.l5 = None
        self.l2_config = None
        self.l2_client = None
        self.l2_model = ""
        self.memory_config = None
        self.scheduler = None
        self.reflection_engine = None
        self.drift_detector = None
        self.judge_llm: LLMClient | None = None
        self.session_store: SessionStore | None = None
        self.e1_decompose_enabled = True
        self.bare = False
        self.capability_profile = CapabilityProfile.named("standard")
        self.activation_manifest: ActivationManifest | None = None
        self.context_projection: ContextProjection | None = None
        # Safe prompt diagnostics only; never store/render prompt text here.
        self.prompt_metadata: dict[str, object] = {}
        self.project_instructions = None
        self.tool_bundles = None
        # ``None`` is retained for lightweight tests/callers that construct a
        # runtime directly instead of going through ``create``.  A fully
        # initialized runtime captures one value and reuses it for the
        # activation artifact and every ``run_turn`` invocation.
        self.output_egress_guard_enabled: bool | None = None
        self.max_iterations: int | None = 20
        self.warnings: list[RuntimeWarning] = []
        self._closed = False
        self.shared_services: SharedCapabilityServices | None = None
        self._turn_lock = asyncio.Lock()
        self._queue_waiters: dict[int, tuple[asyncio.Future, EventEmitter, ConfirmHandler]] = {}

    @classmethod
    async def create(
        cls,
        cwd: Path,
        *,
        mode: str | None = None,
        additional_dirs: list[Path] | None = None,
        effort: str | None = None,
        session_id: str | None = None,
        resume: str | None = None,
        host_execution: bool = False,
        max_iterations: int | None = 20,
        bare: bool = False,
        capability_profile: str | CapabilityProfile | None = None,
    ) -> "SessionRuntime":
        self = cls()
        try:
            return await self._initialize(
                cwd,
                mode=mode,
                additional_dirs=additional_dirs,
                effort=effort,
                session_id=session_id,
                resume=resume,
                host_execution=host_execution,
                max_iterations=max_iterations,
                bare=bare,
                capability_profile=capability_profile,
            )
        except BaseException:
            self._closed = True
            with contextlib.suppress(BaseException):
                await self._shutdown_owned_resources(flush_memory=False)
            raise

    async def _initialize(
        self,
        cwd: Path,
        *,
        mode: str | None = None,
        additional_dirs: list[Path] | None = None,
        effort: str | None = None,
        session_id: str | None = None,
        resume: str | None = None,
        host_execution: bool = False,
        max_iterations: int | None = 20,
        bare: bool = False,
        capability_profile: str | CapabilityProfile | None = None,
    ) -> "SessionRuntime":
        if max_iterations is not None and not 1 <= max_iterations <= 100:
            raise ValueError("max_iterations must be between 1 and 100")
        self.cwd = Path(cwd).resolve()
        self.project_instructions = load_project_instructions(self.cwd)
        self.additional_dirs = tuple(Path(p).resolve() for p in (additional_dirs or []))
        self.max_iterations = max_iterations
        if bare and capability_profile not in (None, "clean-coding"):
            raise ValueError("--bare cannot be combined with another capability profile")
        profile_name = "clean-coding" if bare else (capability_profile or "standard")
        self.capability_profile = (
            profile_name
            if isinstance(profile_name, CapabilityProfile)
            else CapabilityProfile.named(profile_name)
        )
        self.output_egress_guard_enabled = (
            self.capability_profile.name == "hardened-safety"
            or _env_flag("CC_HARNESS_OUTPUT_EGRESS_GUARD")
        )
        self.bare = self.capability_profile.name == "clean-coding"
        if self.capability_profile.name == "hardened-safety" and host_execution:
            raise ValueError("hardened-safety cannot be combined with host execution")
        self.config = load_layered_config(self.cwd)
        # Resolve bundle selection through the same process/project/user
        # precedence as the other runtime settings.  The default remains the
        # small core bundle when no value is configured.
        self.tool_bundles = parse_tool_bundles(
            self.config.runtime_environment.get("CC_HARNESS_TOOL_BUNDLES")
        )
        self.llm = LLMClient(
            api_key=self.config.openai_api_key,
            model=self.config.openai_model,
            base_url=self.config.openai_base_url,
            reasoning_effort=effort,
        )
        self.mcp = MCPClient(self.config.mcp_servers)

        policy_path = self.cwd / "policy.yaml"
        context_config = load_context_config(
            model=self.config.openai_model,
            require_known=self.capability_profile.name == "context-eval",
        )
        self.shared_services = SharedCapabilityServices.load(
            self.cwd,
            self.config,
            profile=self.capability_profile,
            additional_roots=self.additional_dirs,
            host_execution=host_execution,
            context_config=context_config,
        )
        self.policy = self.shared_services.policy
        self.e1_decompose_enabled = self.shared_services.e1_decompose_enabled
        self.l5 = self.shared_services.l5
        self.l2_config = self.shared_services.l2_config
        self.l2_client = self.shared_services.l2_client
        self.l2_model = self.shared_services.l2_model

        self.state = ReplState(
            mode=_effective_resume_mode(mode, None),
            context_config=context_config,
            session_id=session_id or f"session-{int(time.time())}-{uuid.uuid4().hex[:8]}",
        )
        self.state.project_root = self.cwd
        self.state.started_at = datetime.now().isoformat()

        self.session_store = await SessionStore(self.cwd).open()
        legacy_db = Path(__file__).resolve().parents[1] / "logs" / "memory.db"
        imported = await self.session_store.import_legacy(legacy_db)
        if imported:
            self.warnings.append(RuntimeWarning(f"imported {imported} legacy session(s)"))
        if resume:
            record = await self._resolve_resume(resume)
            if record is not None:
                self.state.session_id = record.session_id
                self.state.mode = _effective_resume_mode(mode, record.mode)
                self.state.messages = await self.session_store.load(record.session_id)

        activation_path = self.cwd / ".cc-harness" / "activation" / f"{self.state.session_id}.json"
        self.activation_manifest = ActivationManifest(
            activation_path,
            session_id=self.state.session_id,
            project_root=self.cwd,
            profile=self.capability_profile,
            requested_model=self.config.openai_model,
        )
        self.activation_manifest.initialize("runtime", entrypoint="SessionRuntime")
        self.activation_manifest.add_artifact("runtime", activation_path)
        self.activation_manifest.initialize(
            "context",
            context_window=self.state.context_config.context_window,
            context_window_source=self.state.context_config.context_window_source,
            context_window_verified=self.state.context_config.context_window_verified,
            thresholds=[
                self.state.context_config.tier1_threshold,
                self.state.context_config.tier2_threshold,
                self.state.context_config.tier3_threshold,
            ],
        )
        self.activation_manifest.add_artifact("context", self.session_store.db_path)
        self.context_projection = ContextProjection(
            self.state.messages,
            artifact_dir=self.cwd / ".cc-harness" / "context" / self.state.session_id,
            state_db_path=self.session_store.db_path if self.session_store is not None else None,
            context_id=self.state.session_id,
        )

        configured = len(self.config.mcp_servers)
        if self.capability_profile.mcp:
            try:
                await self.mcp.start()
                connected = len(self.mcp._sessions)
                self.activation_manifest.initialize(
                    "mcp",
                    configured_servers=configured,
                    connected_servers=connected,
                    tool_count=len(self.mcp._tools),
                )
                if connected < configured:
                    self.activation_manifest.degrade(
                        "mcp", f"only {connected}/{configured} configured servers connected"
                    )
            except Exception as exc:
                self.warnings.append(RuntimeWarning(f"MCP startup failed: {exc}"))
                self.activation_manifest.degrade("mcp", str(exc))
        else:
            self.activation_manifest.initialize(
                "mcp",
                configured_servers=configured,
                connected_servers=0,
                tool_count=0,
                disabled_by_profile=True,
            )

        if self.capability_profile.context:
            await self._build_context_services()

        if self.capability_profile.project_services:
            await self._build_project_services()
        if self.capability_profile.long_term_memory:
            await self._build_memory_services()
        if self.capability_profile.background_services:
            await self._build_background_services()

        exec_cfg = load_executor_config(policy_path)
        if self.capability_profile.name == "hardened-safety":
            exec_cfg.backend = ExecutorBackend.SANDBOX
            missing_controls = []
            if not self.policy.enabled:
                missing_controls.append("policy")
            if not self.l2_config.enabled:
                missing_controls.append("l2")
            if self.l5 is None:
                missing_controls.append("l5")
            if missing_controls:
                raise ConfigError(
                    "hardened-safety requires enabled controls: " + ", ".join(missing_controls)
                )
        if host_execution:
            exec_cfg.backend = ExecutorBackend.NATIVE
            self.warnings.append(RuntimeWarning("host execution explicitly enabled"))
        init_session_executor(exec_cfg, str(self.cwd))
        self.activation_manifest.initialize(
            "safety",
            executor_backend=exec_cfg.backend.value,
            policy_enabled=bool(self.policy and self.policy.enabled),
            l2_enabled=bool(self.l2_config and self.l2_config.enabled),
            l5_enabled=self.l5 is not None,
            pii_active=bool(getattr(self.l5, "pii_active", False)),
            profile=self.capability_profile.name,
            provenance_enforced=(
                self.capability_profile.name == "hardened-safety"
                or os.getenv("CC_HARNESS_SECURITY_MODE", "").strip().lower()
                in {"strict", "hardened", "security"}
            ),
            output_egress_guard=bool(self.output_egress_guard_enabled),
        )
        safety_artifact = self.cwd / ".cc-harness" / "safety" / f"{self.state.session_id}.json"
        atomic_write_text(
            safety_artifact,
            json.dumps(
                {
                    "schema_version": "cc-harness.safety-session.v1",
                    "session_id": self.state.session_id,
                    "profile": self.capability_profile.name,
                    "executor_backend": exec_cfg.backend.value,
                    "policy_enabled": bool(self.policy and self.policy.enabled),
                    "l2_enabled": bool(self.l2_config and self.l2_config.enabled),
                    "l5_enabled": self.l5 is not None,
                    "pii_active": bool(getattr(self.l5, "pii_active", False)),
                    "provenance_enforced": (
                        self.capability_profile.name == "hardened-safety"
                        or os.getenv("CC_HARNESS_SECURITY_MODE", "").strip().lower()
                        in {"strict", "hardened", "security"}
                    ),
                    "output_egress_guard": bool(self.output_egress_guard_enabled),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )
        self.activation_manifest.add_artifact("safety", safety_artifact)
        self.activation_manifest.initialize(
            "agent_loop",
            control_plane=("deterministic" if self.capability_profile.loop_control else "disabled"),
        )
        self.activation_manifest.initialize(
            "tools",
            native_tools=self.capability_profile.tools,
            mcp_tool_count=(len(self.mcp._tools) if self.capability_profile.mcp else 0),
            disabled_by_profile=not self.capability_profile.tools,
        )
        if self.capability_profile.loop_control or self.capability_profile.tools:
            journal_path = (
                self.cwd / ".cc-harness" / "action-journal" / f"{self.state.session_id}.jsonl"
            )
            journal_path.parent.mkdir(parents=True, exist_ok=True)
            journal_path.touch(exist_ok=True)
            if self.capability_profile.loop_control:
                self.activation_manifest.add_artifact("agent_loop", journal_path)
            if self.capability_profile.tools:
                self.activation_manifest.add_artifact("tools", journal_path)
        return self

    async def _build_context_services(self) -> None:
        from cc_harness.memory.config import load_memory_config
        from cc_harness.memory.offload.runtime import build_context_offload

        self.memory_config = (
            self.shared_services.memory_config
            if self.shared_services is not None
            else load_memory_config(
                self.cwd / "policy.yaml",
                environ=self.config.runtime_environment if self.config else None,
            )
        )
        if self.capability_profile.name == "memory-eval":
            self.memory_config.pipeline_every_n = 1
        extras, deps = build_context_offload(
            self.cwd / ".cc-harness",
            session_id=self.state.session_id,
            llm=self.llm,
            memory_config=self.memory_config,
            context_config=self.state.context_config,
            history_reader=(
                (lambda: self.session_store.load(self.state.session_id))
                if self.session_store is not None else None
            ),
            state_db_path=(self.session_store.db_path if self.session_store is not None else None),
        )
        self.state.memory_extras = extras
        self.state.mem_deps = deps
        if self.activation_manifest is not None:
            self.activation_manifest.initialize(
                "context",
                offload_enabled=self.memory_config.offload_enabled,
                refs_dir=str(deps["refs_dir"]),
                manifest_path=str(deps["manifest_path"]),
            )

    async def _resolve_resume(self, resume: str) -> SessionRecord | None:
        assert self.session_store is not None
        if resume == "latest":
            return await self.session_store.latest()
        return next(
            (r for r in await self.session_store.list_recent(100) if r.session_id == resume), None
        )

    async def _build_project_services(self) -> None:
        from cc_harness.cli.init import init_noninteractive
        from cc_harness.project.manifest import load_manifest
        from cc_harness.project.service import TodoService

        try:
            manifest = load_manifest(self.cwd)
            if manifest is None:
                manifest = init_noninteractive(
                    self.cwd,
                    name=self.cwd.name or "project",
                    write_gitignore=True,
                )
            self.state.manifest = manifest
            self.state.todo_service = TodoService(project_root=self.cwd, manifest=manifest)
            if self.activation_manifest is not None:
                self.activation_manifest.initialize("project_services")
        except Exception as exc:
            self.warnings.append(RuntimeWarning(f"project services disabled: {exc}"))
            if self.activation_manifest is not None:
                self.activation_manifest.degrade("project_services", str(exc))

    async def _build_memory_services(self) -> None:
        from cc_harness.memory.config import load_memory_config
        from cc_harness.memory.extras import build_memory_extras

        self.memory_config = (
            self.shared_services.memory_config
            if self.shared_services is not None
            else load_memory_config(
                self.cwd / "policy.yaml",
                environ=self.config.runtime_environment if self.config else None,
            )
        )
        if self.capability_profile.name == "memory-eval":
            self.memory_config.pipeline_every_n = 1
        if not self.memory_config.enabled:
            if self.activation_manifest is not None:
                self.activation_manifest.initialize("memory", configured_enabled=False)
            return
        env = dict(self.config.runtime_environment)
        assert self.config is not None
        env.update(
            {
                "OPENAI_API_KEY": self.config.openai_api_key,
                "OPENAI_BASE_URL": self.config.openai_base_url,
                "OPENAI_MODEL": self.config.openai_model,
            }
        )
        benchmark_read_only = str(env.get("MEMORY_READ_ONLY", "")).casefold() in {
            "1", "true", "yes"
        }
        benchmark_history = str(env.get("MEMORY_HISTORY_PRESERVE", "")).casefold() in {
            "1", "true", "yes"
        }
        if benchmark_read_only or benchmark_history:
            # LoCoMo owns its historical write policy.  QA is read-only;
            # ingestion writes preserved facts directly and must not let the
            # product pipeline/maintenance collapse them afterward.
            self.memory_config.pipeline_enabled = False
            self.memory_config.maintenance_enabled = False
            self.memory_config.reflection_enabled = False
            self.memory_config.drift_enabled = False
            if benchmark_read_only:
                self.memory_config.capture_enabled = False
        try:
            # Older helpers print fail-soft diagnostics. Capture them so the
            # terminal renderer remains the sole owner of stdout.
            with contextlib.redirect_stdout(io.StringIO()):
                extras, deps = await build_memory_extras(
                    env,
                    self.cwd / ".cc-harness" / "memory.db",
                    include_offload=False,
                    memory_config=self.memory_config,
                    # Benchmark snapshots are deliberately restored into a
                    # different workspace for every isolated query.  A
                    # caller-supplied logical scope keeps the copied memory
                    # visible after that relocation while ordinary sessions
                    # retain the workspace path as their default scope.
                    project_scope=env.get("MEMORY_PROJECT_SCOPE") or str(self.cwd),
                )
            existing_extras = list(self.state.memory_extras or [])
            existing_deps = dict(self.state.mem_deps or {})
            self.state.memory_extras = existing_extras + extras
            self.state.mem_deps = {**existing_deps, **(deps or {})}
            if self.state.todo_service is not None and deps:
                self.state.todo_service.memory_service = deps.get("service")
            if self.activation_manifest is not None:
                if deps is None:
                    self.activation_manifest.degrade(
                        "memory", "memory dependency construction returned no stack"
                    )
                else:
                    def memory_event(event: dict) -> None:
                        if self.activation_manifest is None:
                            return
                        details = {
                            key: value
                            for key, value in event.items()
                            if key not in {"artifact", "error"}
                        }
                        self.activation_manifest.trigger("memory", **details)
                        if event.get("artifact"):
                            self.activation_manifest.add_artifact("memory", str(event["artifact"]))
                        if event.get("error"):
                            self.activation_manifest.degrade("memory", str(event["error"]))

                    if not (deps.get("read_only") or deps.get("history_mode")):
                        from cc_harness.memory.worker import LayeredMemoryWorker

                        worker = LayeredMemoryWorker(
                            store=deps["store"],
                            pipeline=deps["pipeline"],
                            config=self.memory_config,
                            context_window=self.state.context_config.context_window,
                            scenarios_dir=deps["scenarios_dir"],
                            persona_path=deps["persona_path"],
                            artifact_dir=self.cwd / ".cc-harness" / "memory" / "pipeline",
                            llm=getattr(deps["service"].decider, "_llm", None),
                            event_callback=memory_event,
                        )
                        await worker.start()
                        deps["worker"] = worker
                        self.state.mem_deps["worker"] = worker
                    self.activation_manifest.initialize(
                        "memory",
                        capture_enabled=self.memory_config.capture_enabled,
                        pipeline_enabled=self.memory_config.pipeline_enabled,
                        layered_inject=self.memory_config.layered_inject,
                    )
                    self.activation_manifest.add_artifact(
                        "memory", self.cwd / ".cc-harness" / "memory.db"
                    )
        except Exception as exc:
            self.warnings.append(RuntimeWarning(f"memory services disabled: {exc}"))
            if self.activation_manifest is not None:
                self.activation_manifest.degrade("memory", str(exc))

    async def _build_background_services(self) -> None:
        """Wire maintenance, reflection, and drift to the shared memory stack."""
        deps = self.state.mem_deps
        if not deps or self.memory_config is None:
            return
        # Memory construction is fail-soft.  A stale/partial dependency map
        # must not produce a second misleading warning for services that
        # require the unavailable memory service.
        if any(key not in deps for key in ("store", "service")):
            return
        try:
            from cc_harness.memory.maintenance.scheduler import MaintenanceScheduler

            cfg = self.memory_config
            self.scheduler = MaintenanceScheduler(
                store=deps["store"],
                service=deps["service"],
                llm=self.llm,
                every_n_turns=cfg.maintenance_every_n_turns,
                count_threshold=cfg.maintenance_count_threshold,
                interval_s=cfg.maintenance_interval_s,
                enabled=cfg.maintenance_enabled,
            )
            service = deps.get("service")
            if service is not None and getattr(service, "embedder", None) is not None:
                self.scheduler._embedder = service.embedder
            self.scheduler._half_life_days = cfg.staleness_half_life_days
            self.scheduler._llm_recheck_enabled = cfg.staleness_llm_recheck_enabled
            self.scheduler._ttl_threshold = cfg.ttl_staleness_threshold
            self.scheduler._ttl_limit = cfg.ttl_limit
            self.scheduler._consol_threshold = cfg.consolidation_similarity_threshold
            self.scheduler._consol_max = cfg.consolidation_max_cluster_size
            if self.activation_manifest is not None:
                self.activation_manifest.initialize("background_services")
        except Exception as exc:
            self.warnings.append(RuntimeWarning(f"memory maintenance disabled: {exc}"))
            if self.activation_manifest is not None:
                self.activation_manifest.degrade("background_services", str(exc))

        try:
            from cc_harness.reflection.engine import ReflectionEngine

            judge_base = os.getenv("JUDGE_BASE_URL")
            judge_key = os.getenv("JUDGE_API_KEY")
            judge_model = os.getenv("JUDGE_MODEL")
            if judge_base and judge_key and judge_model:
                self.judge_llm = LLMClient(
                    api_key=judge_key,
                    model=judge_model,
                    base_url=judge_base,
                )
            self.reflection_engine = ReflectionEngine(
                memory_service=deps["service"],
                llm_client=self.llm,
                judge_llm=self.judge_llm,
                l5_engine=self.l5,
                project_root=self.cwd,
                enabled=self.memory_config.reflection_enabled,
                every_n_turns=self.memory_config.reflection_every_n_turns,
                max_pending=self.memory_config.reflection_max_pending,
                drain_timeout_s=self.memory_config.reflection_drain_timeout_s,
            )

            from cc_harness.drift.detector import DriftDetector

            self.drift_detector = DriftDetector(
                reflection_engine=self.reflection_engine,
                judge_llm=self.judge_llm,
                local_llm=self.llm,
                l5_engine=self.l5,
                project_root=self.cwd,
                audit_path=self.cwd / ".cc-harness" / "logs" / "drift.jsonl",
                every_n_turns=self.memory_config.drift_every_n_turns,
                enabled=self.memory_config.drift_enabled,
            )
            deps["service"].drift_detector = self.drift_detector
            if deps.get("retriever") is not None:
                deps["retriever"].drift_detector = self.drift_detector
        except Exception as exc:
            self.warnings.append(RuntimeWarning(f"reflection/drift disabled: {exc}"))

    async def run_user_turn(
        self,
        user_text: str,
        *,
        event_emitter: EventEmitter,
        confirm_handler: ConfirmHandler,
        message_content=None,
    ):
        """Queue and execute one input through the current session serially.

        The queue write happens before taking the execution lock.  Therefore a
        message arriving while compaction or a model/tool turn is running is
        durable immediately, receives a queue event, and cannot enter the
        active context until the current turn has finished.
        """
        if self.session_store is None:
            return await self._execute_user_turn(
                user_text,
                event_emitter=event_emitter,
                confirm_handler=confirm_handler,
                message_content=message_content,
            )

        item = await self.session_store.enqueue_input(
            self.state.session_id,
            {
                "user_text": user_text,
                "message_content": message_content,
            },
        )
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._queue_waiters[item.queue_id] = (future, event_emitter, confirm_handler)
        try:
            await event_emitter(
                {
                    "type": "input_queued",
                    "queue_id": item.queue_id,
                    "session_id": self.state.session_id,
                    "status": "queued",
                    "ts": time.time(),
                }
            )
        except Exception:
            # A renderer failure must not make a durable input disappear.
            pass

        async with self._turn_lock:
            await self.session_store.recover_processing_inputs(self.state.session_id)
            await self._drain_input_queue(
                fallback=(event_emitter, confirm_handler),
            )
        return await future

    async def _drain_input_queue(
        self,
        *,
        fallback: tuple[EventEmitter, ConfirmHandler],
    ) -> None:
        """Consume the durable FIFO while holding the per-session turn lock."""
        assert self.session_store is not None
        while True:
            item = await self.session_store.claim_next_input(self.state.session_id)
            if item is None:
                return
            waiter_entry = self._queue_waiters.pop(item.queue_id, None)
            future, event_emitter, confirm_handler = (
                waiter_entry
                if waiter_entry is not None
                else (None, fallback[0], fallback[1])
            )
            payload = item.payload
            try:
                result = await self._execute_user_turn(
                    str(payload.get("user_text") or ""),
                    event_emitter=event_emitter,
                    confirm_handler=confirm_handler,
                    message_content=payload.get("message_content"),
                )
                compaction = getattr(result, "compaction", None)
                compaction_error = getattr(compaction, "error", None)
                if compaction_error:
                    await self.session_store.requeue_input(item.queue_id, compaction_error)
                    if future is not None and not future.done():
                        future.set_result(result)
                    # Keep later inputs queued until a later retry can commit a
                    # valid current projection.
                    return
                await self.session_store.complete_input(item.queue_id)
                if future is not None and not future.done():
                    future.set_result(result)
            except BaseException as exc:
                await self.session_store.requeue_input(item.queue_id, str(exc))
                if future is not None and not future.done():
                    future.set_exception(exc)
                return

    async def _execute_user_turn(
        self,
        user_text: str,
        *,
        event_emitter: EventEmitter,
        confirm_handler: ConfirmHandler,
        message_content=None,
    ):
        assert self.llm is not None and self.mcp is not None
        one_shot = self.capability_profile.one_shot
        if self.activation_manifest is not None:
            if self.capability_profile.loop_control:
                self.activation_manifest.trigger("agent_loop", turn=self.state.turn_counter + 1)
            if self.capability_profile.safety:
                self.activation_manifest.trigger("safety", stage="user_input")
        text = user_text
        if self.capability_profile.safety and self.l2_config and self.l2_config.enabled:
            scan = await scan_user_input(
                text,
                l2_cfg=self.l2_config,
                client=self.l2_client,
                model=self.l2_model,
            )
            if not scan.allowed:
                await event_emitter(
                    {
                        "type": "policy_block",
                        "stage": "user_input",
                        "reason": scan.reason,
                    }
                )
                await event_emitter(
                    {
                        "type": "result",
                        "text": REFUSAL_TEMPLATE,
                        "blocked": True,
                        "block_reason": scan.reason,
                    }
                )
                return None
            text = scan.wrapped_text

        content = message_content if message_content is not None else text
        message_start = len(self.state.messages)
        turn_counter_before = self.state.turn_counter
        self.state.messages.append({"role": "user", "content": content})
        self.state.turn_counter += 1
        memory_deps = self.state.mem_deps or {}
        recall = memory_deps.get("recall")
        memory_layer = (
            memory_deps.get("layered_injection") or {"recall": recall}
            if (
                not one_shot
                and recall is not None
                and self.memory_config is not None
                and self.memory_config.layered_inject
            )
            else None
        )
        offload_deps = (
            memory_deps
            if (
                not one_shot
                and memory_deps
                and self.memory_config is not None
                and self.memory_config.offload_enabled
            )
            else None
        )
        prompt_capabilities = {
            "todo_available": self.state.todo_service is not None,
            "subagent_available": self.state.todo_service is not None,
            # Reasoning depth is controlled by the provider/API.  The TUI may
            # show elapsed time, but production prompts never require visible
            # chain-of-thought text before a tool call.
            "visible_thought_required": False,
        }
        completion_contract = completion_contract_from_instruction(user_text)
        runtime_contract = {
            "acceptance": list(completion_contract.required_paths),
            "artifacts": list(completion_contract.required_paths),
            "verification": (
                "required after code changes"
                if completion_contract.require_verification_after_code_changes else "optional"
            ),
            "blockers": [],
            "next_action": "continue until acceptance and verification are evidenced",
        }
        try:

            async def tracked_emit(event: dict) -> None:
                if self.activation_manifest is not None and event.get("type") == "action":
                    tool_name = str(event.get("name") or "unknown")
                    self.activation_manifest.trigger("tools", tool=tool_name)
                    if tool_name.startswith("mcp__"):
                        self.activation_manifest.trigger("mcp", tool=tool_name)
                if (
                    self.activation_manifest is not None
                    and event.get("type") == "capability_activation"
                ):
                    capability = str(event.get("capability") or "")
                    if capability in self.activation_manifest.capabilities:
                        details = {
                            key: value
                            for key, value in event.items()
                            if key not in {"type", "capability", "artifact"} and value is not None
                        }
                        self.activation_manifest.trigger(capability, **details)
                        if event.get("artifact"):
                            self.activation_manifest.add_artifact(
                                capability, str(event["artifact"])
                            )
                        if event.get("error"):
                            self.activation_manifest.degrade(capability, str(event["error"]))
                await event_emitter(event)

            async def subagent_progress(task_id: str, status: str, detail: str = "") -> None:
                await tracked_emit(
                    {
                        "type": "subagent_progress",
                        "task_id": task_id,
                        "status": status,
                        "detail": detail,
                        "ts": time.time(),
                    }
                )

            stats = await run_turn(
                self.state.messages,
                self.llm,
                self.mcp,
                mode="plan" if one_shot else self.state.mode,
                cwd=str(self.cwd),
                token_counter=self.state.token_counter,
                policy=self.policy,
                l5=None if one_shot else self.l5,
                extra_native_specs=(
                    None if one_shot else list(self.state.memory_extras or []) or None
                ),
                context_config=None if one_shot else self.state.context_config,
                memory_layer=memory_layer,
                offload_deps=offload_deps,
                todo_service=None if one_shot else self.state.todo_service,
                session_id=self.state.session_id,
                last_turn_text=self.state.last_turn_text,
                todo_hints=list(self.state.todo_hints or []),
                reflection_engine=self.reflection_engine,
                e1_decompose_enabled=(
                    self.e1_decompose_enabled
                    and prompt_capabilities["todo_available"]
                    and prompt_capabilities["subagent_available"]
                ),
                prompt_capabilities=prompt_capabilities,
                event_emitter=tracked_emit,
                subagent_progress_cb=subagent_progress,
                confirm_handler=confirm_handler,
                direct_render=False,
                max_iter=1 if one_shot else self.max_iterations,
                loop_control_config=LoopControlConfig(
                    enabled=self.capability_profile.loop_control,
                    completion_contract=completion_contract,
                ),
                runtime_contract=runtime_contract,
                project_instructions=(
                    self.project_instructions.text
                    if self.project_instructions is not None else None
                ),
                allow_read_only_tools=not one_shot,
                tool_bundles=self.tool_bundles,
                context_artifact_dir=(
                    None
                    if one_shot
                    else self.cwd / ".cc-harness" / "context" / self.state.session_id
                ),
                context_projection=None if one_shot else self.context_projection,
                refresh_system_prompt=not one_shot,
                retry_empty_response=not one_shot,
                security_mode=(
                    "strict"
                    if self.capability_profile.name == "hardened-safety"
                    else None
                ),
                output_egress_guard=(
                    self.output_egress_guard_enabled
                    if self.output_egress_guard_enabled is not None
                    else (
                        self.capability_profile.name == "hardened-safety"
                        or _env_flag("CC_HARNESS_OUTPUT_EGRESS_GUARD")
                    )
                ),
            )
            if getattr(stats, "prompt_metadata", None):
                self.prompt_metadata = dict(stats.prompt_metadata)
            compaction_error = getattr(getattr(stats, "compaction", None), "error", None)
            if compaction_error:
                # The durable queue item must remain pending.  Roll back only
                # the in-memory projection of this attempted input before the
                # finally/save path runs, so a retry cannot duplicate it.
                self.state.messages[:] = self.state.messages[:message_start]
                self.state.turn_counter = turn_counter_before
                return stats
            if self.capability_profile.safety and self.l2_config and self.l2_config.enabled:
                stats.auxiliary_model_calls += scan.model_calls
                if scan.usage is not None:
                    stats.api_prompt_tokens += scan.usage.prompt_tokens
                    stats.api_uncached_prompt_tokens += scan.usage.uncached_prompt_tokens
                    stats.api_cache_read_prompt_tokens += scan.usage.cache_read_prompt_tokens
                    stats.api_cache_creation_prompt_tokens += (
                        scan.usage.cache_creation_prompt_tokens
                    )
                    stats.api_completion_tokens += scan.usage.completion_tokens
                    stats.api_total_tokens += scan.usage.total_tokens
                    if scan.usage.reported_cost is None:
                        stats.api_reported_cost = None
                        stats.api_reported_cost_currency = None
                    elif stats.api_reported_cost is None:
                        stats.api_reported_cost = scan.usage.reported_cost
                        stats.api_reported_cost_currency = scan.usage.reported_cost_currency
                    elif stats.api_reported_cost_currency in (
                        None, scan.usage.reported_cost_currency
                    ):
                        stats.api_reported_cost += scan.usage.reported_cost
                        stats.api_reported_cost_currency = (
                            stats.api_reported_cost_currency
                            or scan.usage.reported_cost_currency
                        )
                    else:
                        stats.api_reported_cost = None
                        stats.api_reported_cost_currency = None
                    stats.api_reported = True
            self.state.session_stats.add(
                stats,
                messages=self.state.messages,
                counter=self.state.token_counter,
            )
            self.state.last_turn_text = _extract_final_text(self.state.messages)
            if self.activation_manifest is not None:
                self.activation_manifest.set_resolved_model(self.llm.resolved_model)
            if self.memory_config is not None and "store" in memory_deps:
                memory_outcome = await _after_turn_memory(
                    self.state, self.memory_config, scheduler=self.scheduler
                )
                if self.activation_manifest is not None and self.state.mem_deps:
                    if memory_outcome.get("captured") or memory_outcome.get("pipeline_enqueued"):
                        self.activation_manifest.trigger(
                            "memory",
                            turn=self.state.turn_counter,
                            l0_captured=memory_outcome.get("captured", 0),
                            pipeline_enqueued=memory_outcome.get("pipeline_enqueued", False),
                        )
                    if memory_outcome.get("errors"):
                        self.activation_manifest.degrade(
                            "memory", "; ".join(memory_outcome["errors"])
                        )
            await _after_turn_todo(self.state, self.state.todo_service)
            return stats
        finally:
            await self.save()

    async def save(self, *, status: str = "active") -> None:
        if self.session_store is not None and self.state.messages:
            await self.session_store.save(
                self.state.session_id,
                self.state.messages,
                mode=self.state.mode,
                status=status,
            )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        try:
            await self.save(status="closed")
        finally:
            await self._shutdown_owned_resources(flush_memory=True)

    async def _shutdown_owned_resources(self, *, flush_memory: bool) -> None:
        async def close_owned(label: str, operation) -> None:
            try:
                await operation()
            except Exception as exc:
                self.warnings.append(RuntimeWarning(f"{label} shutdown failed: {exc}"))

        worker = (self.state.mem_deps or {}).get("worker")
        if (
            flush_memory
            and worker is not None
            and self.capability_profile.name == "memory-eval"
        ):
            try:
                flushed = await worker.flush(timeout_s=60.0)
                if not flushed and self.activation_manifest is not None:
                    self.activation_manifest.degrade(
                        "memory", "memory extraction queue did not drain before eval shutdown"
                    )
            except Exception as exc:
                self.warnings.append(RuntimeWarning(f"memory flush failed: {exc}"))
        if flush_memory:
            for component, timeout in (
                (self.scheduler, 5.0),
                (self.reflection_engine, 5.0),
                (self.drift_detector, 5.0),
            ):
                if component is not None and hasattr(component, "_drain"):
                    try:
                        await component._drain(timeout_s=timeout)
                    except Exception:
                        pass
        from cc_harness.memory.extras import close_memory_deps

        await close_owned("memory services", lambda: close_memory_deps(self.state.mem_deps))
        if self.session_store is not None:
            await close_owned("session store", self.session_store.close)
        if self.mcp is not None:
            await close_owned("MCP", self.mcp.shutdown)
        if self.l2_client is not None:
            await close_owned("L2 client", self.l2_client.close)
        if self.judge_llm is not None:
            await close_owned("judge LLM", self.judge_llm.aclose)
        if self.llm is not None:
            await close_owned("main LLM", self.llm.aclose)
        await close_owned("session executor", shutdown_session_executor)

    async def __aenter__(self) -> "SessionRuntime":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()
