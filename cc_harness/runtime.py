"""Shared agent runtime used by every user-facing entrypoint."""

from __future__ import annotations

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

from openai import AsyncOpenAI

from cc_harness.activation import ActivationManifest, CapabilityProfile
from cc_harness.agent import run_turn
from cc_harness.config import (
    AppConfig,
    ConfigError,
    ExecutorBackend,
    load_context_config,
    load_executor_config,
    load_l2_config,
    load_l5_config,
    load_layered_config,
    load_policy_config,
)
from cc_harness.context import ContextProjection
from cc_harness.l2 import REFUSAL_TEMPLATE, scan_user_input
from cc_harness.l5 import build_l5_engine
from cc_harness.llm import LLMClient
from cc_harness.loop_control import LoopControlConfig
from cc_harness.mcp_client import MCPClient
from cc_harness.policy import PolicyEngine
from cc_harness.repl import ReplState, _after_turn_memory, _after_turn_todo, _extract_final_text
from cc_harness.session_store import SessionRecord, SessionStore
from cc_harness.tools import init_session_executor, shutdown_session_executor

EventEmitter = Callable[[dict], Awaitable[None]]
ConfirmHandler = Callable[[str, dict, str], Awaitable[str]]


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
        self.max_iterations: int | None = 20
        self.warnings: list[RuntimeWarning] = []
        self._closed = False

    @classmethod
    async def create(
        cls,
        cwd: Path,
        *,
        mode: str = "coding",
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
        self = cls()
        self.cwd = Path(cwd).resolve()
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
        self.bare = self.capability_profile.name == "clean-coding"
        if self.capability_profile.name == "hardened-safety" and host_execution:
            raise ValueError("hardened-safety cannot be combined with host execution")
        self.config = load_layered_config(self.cwd)
        self.llm = LLMClient(
            api_key=self.config.openai_api_key,
            model=self.config.openai_model,
            base_url=self.config.openai_base_url,
            reasoning_effort=effort,
        )
        self.mcp = MCPClient(self.config.mcp_servers)

        policy_path = self.cwd / "policy.yaml"
        policy_cfg = load_policy_config(policy_path)
        self.policy = PolicyEngine(
            project_root=self.cwd,
            enabled=policy_cfg.enabled,
            additional_roots=self.additional_dirs,
        )
        self.e1_decompose_enabled = policy_cfg.e1_decompose_enabled
        self.l5 = build_l5_engine(load_l5_config(policy_path))
        self.l2_config = load_l2_config(policy_path)
        self.l2_client = (
            AsyncOpenAI(
                api_key=self.config.openai_api_key,
                base_url=self.config.openai_base_url,
            )
            if self.l2_config.enabled
            else None
        )
        self.l2_model = os.getenv("JUDGE_MODEL") or self.config.openai_model

        self.state = ReplState(
            mode=mode,
            context_config=load_context_config(
                model=self.config.openai_model,
                require_known=self.capability_profile.name == "context-eval",
            ),
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
                self.state.mode = record.mode
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
        )
        safety_artifact = self.cwd / ".cc-harness" / "safety" / f"{self.state.session_id}.json"
        safety_artifact.parent.mkdir(parents=True, exist_ok=True)
        tmp = safety_artifact.with_suffix(".json.tmp")
        tmp.write_text(
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
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, safety_artifact)
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

        self.memory_config = load_memory_config(
            self.cwd / "policy.yaml",
            environ=self.config.runtime_environment if self.config else None,
        )
        if self.capability_profile.name == "memory-eval":
            self.memory_config.pipeline_every_n = 1
        extras, deps = build_context_offload(
            self.cwd / ".cc-harness",
            session_id=self.state.session_id,
            llm=self.llm,
            memory_config=self.memory_config,
            context_config=self.state.context_config,
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

        self.memory_config = load_memory_config(
            self.cwd / "policy.yaml",
            environ=self.config.runtime_environment if self.config else None,
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
        try:
            # Older helpers print fail-soft diagnostics. Capture them so the
            # terminal renderer remains the sole owner of stdout.
            with contextlib.redirect_stdout(io.StringIO()):
                extras, deps = await build_memory_extras(
                    env,
                    self.cwd / ".cc-harness" / "memory.db",
                    include_offload=False,
                    memory_config=self.memory_config,
                    project_scope=str(self.cwd),
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
                    from cc_harness.memory.worker import LayeredMemoryWorker

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
        self.state.messages.append({"role": "user", "content": content})
        self.state.turn_counter += 1
        memory_deps = self.state.mem_deps or {}
        recall = memory_deps.get("recall")
        memory_layer = (
            {"recall": recall}
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
            "visible_thought_required": not self.bare,
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
                loop_control_config=LoopControlConfig(enabled=self.capability_profile.loop_control),
                context_artifact_dir=(
                    None
                    if one_shot
                    else self.cwd / ".cc-harness" / "context" / self.state.session_id
                ),
                context_projection=None if one_shot else self.context_projection,
                refresh_system_prompt=not one_shot,
                retry_empty_response=not one_shot,
            )
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

        async def close_owned(label: str, operation) -> None:
            try:
                await operation()
            except Exception as exc:
                self.warnings.append(RuntimeWarning(f"{label} shutdown failed: {exc}"))

        try:
            await self.save(status="closed")
        finally:
            worker = (self.state.mem_deps or {}).get("worker")
            if worker is not None and self.capability_profile.name == "memory-eval":
                flushed = await worker.flush(timeout_s=60.0)
                if not flushed and self.activation_manifest is not None:
                    self.activation_manifest.degrade(
                        "memory", "memory extraction queue did not drain before eval shutdown"
                    )
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
