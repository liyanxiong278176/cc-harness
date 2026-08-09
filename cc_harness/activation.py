"""Capability profiles and auditable runtime activation evidence."""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ACTIVATION_SCHEMA_VERSION = "cc-harness.activation.v1"


@dataclass(frozen=True)
class CapabilityProfile:
    name: str
    project_services: bool = True
    context: bool = True
    long_term_memory: bool = True
    reflection: bool = True
    background_services: bool = True
    loop_control: bool = True
    safety: bool = True

    @classmethod
    def named(cls, name: str) -> CapabilityProfile:
        profiles = {
            "standard": cls("standard"),
            "clean-coding": cls(
                "clean-coding",
                project_services=False,
                long_term_memory=False,
                reflection=False,
                background_services=False,
            ),
            "benchmark-one-shot": cls(
                "benchmark-one-shot",
                project_services=False,
                context=False,
                long_term_memory=False,
                reflection=False,
                background_services=False,
            ),
            "context-eval": cls(
                "context-eval",
                project_services=False,
                long_term_memory=False,
                reflection=False,
                background_services=False,
            ),
            "memory-eval": cls(
                "memory-eval",
                project_services=False,
                reflection=False,
                background_services=False,
            ),
            "hardened-safety": cls(
                "hardened-safety",
                long_term_memory=False,
                reflection=False,
                background_services=False,
            ),
        }
        try:
            return profiles[name]
        except KeyError as exc:
            raise ValueError(f"unknown capability profile: {name}") from exc


@dataclass
class CapabilityActivation:
    enabled: bool
    initialized: bool = False
    triggered: bool = False
    artifacts: list[str] = field(default_factory=list)
    degraded_reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def no_degradation(self) -> bool:
        return self.degraded_reason is None


class ActivationManifest:
    """Mutable recorder persisted atomically after every state transition."""

    def __init__(
        self,
        path: Path,
        *,
        session_id: str,
        project_root: Path,
        profile: CapabilityProfile,
        requested_model: str,
    ) -> None:
        self.path = Path(path)
        self.session_id = session_id
        self.project_root = Path(project_root).resolve()
        self.profile = profile
        self.requested_model = requested_model
        self.resolved_model: str | None = None
        self.created_at = time.time()
        self.updated_at = self.created_at
        self.capabilities: dict[str, CapabilityActivation] = {
            "runtime": CapabilityActivation(enabled=True),
            "model": CapabilityActivation(enabled=True),
            "mcp": CapabilityActivation(enabled=True),
            "agent_loop": CapabilityActivation(enabled=profile.loop_control),
            "tools": CapabilityActivation(enabled=True),
            "context": CapabilityActivation(enabled=profile.context),
            "memory": CapabilityActivation(enabled=profile.long_term_memory),
            "safety": CapabilityActivation(enabled=profile.safety),
            "project_services": CapabilityActivation(enabled=profile.project_services),
            "background_services": CapabilityActivation(enabled=profile.background_services),
        }

    def initialize(self, name: str, **details: Any) -> None:
        state = self.capabilities[name]
        state.initialized = True
        state.details.update(details)
        self.write()

    def trigger(self, name: str, **details: Any) -> None:
        state = self.capabilities[name]
        state.triggered = True
        state.details.update(details)
        self.write()

    def add_artifact(self, name: str, path: Path | str) -> None:
        value = str(path)
        state = self.capabilities[name]
        if value not in state.artifacts:
            state.artifacts.append(value)
        self.write()

    def degrade(self, name: str, reason: str) -> None:
        self.capabilities[name].degraded_reason = reason
        self.write()

    def set_resolved_model(self, model: str | None) -> None:
        if model:
            self.resolved_model = model
            self.capabilities["model"].initialized = True
            self.capabilities["model"].details["resolved_model"] = model
            self.write()

    def payload(self) -> dict[str, Any]:
        self.updated_at = time.time()
        return {
            "schema_version": ACTIVATION_SCHEMA_VERSION,
            "session_id": self.session_id,
            "project_root": str(self.project_root),
            "profile": asdict(self.profile),
            "requested_model": self.requested_model,
            "resolved_model": self.resolved_model,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "capabilities": {
                name: {**asdict(state), "no_degradation": state.no_degradation}
                for name, state in sorted(self.capabilities.items())
            },
        }

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.payload(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.path)
