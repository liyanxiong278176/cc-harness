"""Durable worker lease lifecycle and fencing."""

from __future__ import annotations

import asyncio
import contextlib
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
import time
from collections.abc import AsyncIterator, Iterable, Mapping

from .run_events import EventActor, RunEvent
from .run_kernel import ActionRequest
from .run_model import EffectClass, Lease, ResourceLease, RunStatus, SupervisorLease
from .run_store import (
    LeaseFenceError,
    ResourceLeaseConflict,
    RunStore,
    RunStoreError,
)


@dataclass(frozen=True)
class ResourceSpec:
    """Normalized resource requested by an action."""

    resource_key: str
    mode: str

    def __post_init__(self) -> None:
        if not self.resource_key:
            raise ValueError("resource_key is required")
        if self.mode not in {"shared", "exclusive"}:
            raise ValueError("resource mode must be shared or exclusive")


def _normalize_effect(effect: EffectClass | str) -> str:
    return effect.value if isinstance(effect, EffectClass) else str(effect or "unknown").strip().lower()


def _normalize_path(value: str, working_directory: Path) -> str:
    """Canonicalize a user/tool path without requiring it to exist."""

    raw = str(value).strip()
    if not raw:
        return ""
    # A glob pattern identifies the directory whose contents it can touch.
    wildcard = next((index for index, char in enumerate(raw) if char in "*?[]{}"), None)
    if wildcard is not None:
        raw = raw[:wildcard].rstrip("\\/") or "."
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = working_directory / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        resolved = candidate.absolute()
    normalized = resolved.as_posix().rstrip("/") or "."
    # Windows paths are case-insensitive; lowercasing only on Windows keeps
    # conflict checks deterministic while preserving Unix case sensitivity.
    return os.path.normcase(normalized)


def _explicit_resource_specs(
    arguments: Mapping[str, object],
    working_directory: Path,
) -> tuple[ResourceSpec, ...]:
    raw = arguments.get("resource_keys")
    if raw is None:
        return ()
    values = raw if isinstance(raw, (list, tuple, set)) else (raw,)
    specs: list[ResourceSpec] = []
    for value in values:
        if isinstance(value, Mapping):
            key = str(value.get("key") or value.get("resource_key") or "").strip()
            mode = str(value.get("mode") or "exclusive").strip().lower()
        else:
            key = str(value).strip()
            mode = "exclusive"
        if not key:
            continue
        if ":" not in key:
            key = f"named:{key}"
        specs.append(ResourceSpec(key, mode))
    return tuple(specs)


_PATH_ARGUMENT_KEYS = frozenset(
    {
        "path",
        "paths",
        "file",
        "files",
        "file_path",
        "file_paths",
        "filename",
        "filenames",
        "directory",
        "directories",
        "dir",
        "dirs",
        "cwd",
    }
)


def resource_specs_for_action(
    request: ActionRequest,
    *,
    effect: EffectClass | str,
    working_directory: Path,
    project_id: str,
) -> tuple[ResourceSpec, ...]:
    """Derive conservative locks from a tool contract and its arguments.

    Explicit ``resource_keys`` win.  Native file tools lock their paths,
    read-only lookups use shared workspace locks, and unknown/external actions
    fall back to an exclusive workspace/project lock.  This lets independent
    sessions share a project while preventing overlapping writes.
    """

    arguments = request.arguments
    explicit = _explicit_resource_specs(arguments, working_directory)
    if explicit:
        return explicit
    effect_value = _normalize_effect(effect)
    paths: list[str] = []
    for key, value in arguments.items():
        if str(key).casefold() not in _PATH_ARGUMENT_KEYS:
            continue
        values = value if isinstance(value, (list, tuple, set)) else (value,)
        for item in values:
            normalized = _normalize_path(str(item), working_directory)
            if normalized:
                paths.append(f"path:{normalized}")
    # Preserve order for useful diagnostics while deduplicating aliases.
    paths = list(dict.fromkeys(paths))
    if effect_value == EffectClass.READ_ONLY.value:
        if paths:
            return tuple(ResourceSpec(path, "shared") for path in paths)
        return (ResourceSpec(f"workspace:{_normalize_path(str(working_directory), working_directory)}", "shared"),)
    if effect_value == EffectClass.WORKSPACE_MUTATION.value:
        if paths:
            return tuple(ResourceSpec(path, "exclusive") for path in paths)
        return (ResourceSpec(f"workspace:{_normalize_path(str(working_directory), working_directory)}", "exclusive"),)
    if effect_value == EffectClass.EXTERNAL_SIDE_EFFECT.value:
        return (ResourceSpec(f"project:{project_id}", "exclusive"),)
    # ``run_command`` has an intentionally unknown contract because arbitrary
    # shell commands may mutate the workspace.  Scope it to the worker's
    # working tree so isolated worktrees can still proceed in parallel.
    if request.tool_name == "run_command":
        return (ResourceSpec(f"workspace:{_normalize_path(str(working_directory), working_directory)}", "exclusive"),)
    return (ResourceSpec(f"project:{project_id}", "exclusive"),)


class SupervisorLeaseManager:
    """Small owner-scoped facade for the project scheduler lease."""

    def __init__(self, store: RunStore, *, owner_id: str, ttl_seconds: float = 120.0) -> None:
        self.store = store
        self.owner_id = owner_id
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.current: SupervisorLease | None = None

    async def acquire(self) -> SupervisorLease:
        self.current = await self.store.claim_supervisor_lease(
            self.owner_id,
            ttl_seconds=self.ttl_seconds,
        )
        return self.current

    async def heartbeat(self) -> SupervisorLease:
        if self.current is None:
            return await self.acquire()
        self.current = await self.store.heartbeat_supervisor_lease(
            self.current,
            ttl_seconds=self.ttl_seconds,
        )
        return self.current

    async def release(self) -> bool:
        if self.current is None:
            return False
        released = await self.store.release_supervisor_lease(self.current)
        self.current = None
        return released


class LeaseManager:
    def __init__(self, store: RunStore, *, ttl_seconds: float = 30.0) -> None:
        self.store = store
        self.ttl_seconds = max(1.0, ttl_seconds)

    async def claim(self, run_id: str, worker_id: str) -> Lease:
        projection = await self.store.load_projection(run_id)
        if projection.status is not RunStatus.QUEUED:
            raise LeaseFenceError(f"run is not claimable: {projection.status.value}")
        current = await self.store.current_lease(run_id)
        current_epoch = current.epoch if current is not None else projection.lease_epoch
        epoch = current_epoch + 1
        expires_at = time.time() + self.ttl_seconds
        event = RunEvent.create(
            run_id=run_id,
            sequence=projection.sequence + 1,
            event_type="RunClaimed",
            actor=EventActor("worker", worker_id),
            runtime_contract_digest=str(projection.runtime_contract_digest),
            lease_epoch=epoch,
            payload={"worker_id": worker_id, "expires_at": expires_at},
        )
        await self.store.append(
            event,
            expected_sequence=projection.sequence,
            expected_lease_epoch=current_epoch,
        )
        return Lease(run_id, worker_id, epoch, expires_at - self.ttl_seconds, expires_at)

    async def heartbeat(self, lease: Lease) -> Lease:
        if lease.is_expired():
            raise LeaseFenceError("cannot heartbeat an expired lease")
        projection = await self.store.load_projection(lease.run_id)
        current = await self.store.current_lease(lease.run_id)
        if current is None or current.epoch != lease.epoch or current.worker_id != lease.worker_id:
            raise LeaseFenceError("lease is no longer current")
        expires_at = time.time() + self.ttl_seconds
        event = RunEvent.create(
            run_id=lease.run_id,
            sequence=projection.sequence + 1,
            event_type="WorkerHeartbeat",
            actor=EventActor("worker", lease.worker_id),
            runtime_contract_digest=str(projection.runtime_contract_digest),
            lease_epoch=lease.epoch,
            payload={"heartbeat_at": str(time.time()), "expires_at": expires_at},
        )
        await self.store.append(
            event,
            expected_sequence=projection.sequence,
            expected_lease_epoch=lease.epoch,
        )
        return Lease(lease.run_id, lease.worker_id, lease.epoch, lease.acquired_at, expires_at)

    async def release(self, lease: Lease) -> bool:
        return await self.store.release_lease(lease.run_id, lease.epoch)

    async def reclaim_expired(self, run_id: str, *, reason: str = "worker lease expired") -> Lease | None:
        current = await self.store.current_lease(run_id)
        if current is None:
            # A supervisor can be interrupted after the worker lease row has
            # been removed but before the terminal/recovery event is written.
            # Treat a still-running projection with no lease as a recoverable
            # crash instead of leaving it permanently invisible to the queue.
            projection = await self.store.load_projection(run_id)
            if projection.status not in {RunStatus.RUNNING, RunStatus.CANCEL_REQUESTED}:
                return None
            event_type = "RunCancelled" if projection.status is RunStatus.CANCEL_REQUESTED else "WorkerLeaseExpired"
            event = RunEvent.create(
                run_id=run_id,
                sequence=projection.sequence + 1,
                event_type=event_type,
                actor=EventActor("supervisor", "local-supervisor"),
                runtime_contract_digest=str(projection.runtime_contract_digest),
                lease_epoch=0,
                payload={
                    "reason": f"{reason}; no active worker lease",
                    "worker_id": projection.active_worker_id or "unknown",
                },
            )
            await self.store.append(event, expected_sequence=projection.sequence)
            return None
        if not current.is_expired():
            return None
        projection = await self.store.load_projection(run_id)
        event_type = "RunCancelled" if projection.status is RunStatus.CANCEL_REQUESTED else "WorkerLeaseExpired"
        event = RunEvent.create(
            run_id=run_id,
            sequence=projection.sequence + 1,
            event_type=event_type,
            actor=EventActor("supervisor", "local-supervisor"),
            runtime_contract_digest=str(projection.runtime_contract_digest),
            lease_epoch=current.epoch,
            payload={"reason": reason, "worker_id": current.worker_id},
        )
        await self.store.append(
            event,
            expected_sequence=projection.sequence,
            expected_lease_epoch=current.epoch,
        )
        await self.store.release_lease(run_id, current.epoch)
        return current


class ResourceLeaseManager:
    """Action-scoped resource leases backed by the project RunStore.

    Resource contention is expected coordination, not a run failure.  The
    manager waits for an overlapping lease to expire or be released, while a
    cancellation/fenced worker exits without acquiring a new resource.
    """

    def __init__(
        self,
        store: RunStore,
        *,
        ttl_seconds: float = 120.0,
        poll_interval: float = 0.1,
    ) -> None:
        self.store = store
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.poll_interval = max(0.01, float(poll_interval))

    async def acquire_for_action(
        self,
        lease: Lease,
        request: ActionRequest,
        *,
        effect: EffectClass | str,
        working_directory: Path,
        action_id: str | None = None,
    ) -> tuple[ResourceLease, ...]:
        specs = resource_specs_for_action(
            request,
            effect=effect,
            working_directory=working_directory,
            project_id=self.store.project_id,
        )
        while True:
            try:
                return await self._claim_once(
                    lease,
                    tuple((item.resource_key, item.mode) for item in specs),
                    action_id=action_id or request.action_id,
                )
            except ResourceLeaseConflict:
                # A resource conflict is not a task error.  Do not wait after
                # the run has entered a cancellation/final state, though; a
                # cancelled action must not acquire a fresh lock.
                projection = await self.store.load_projection(lease.run_id)
                if projection.status in {
                    RunStatus.CANCEL_REQUESTED,
                    RunStatus.CANCELLED,
                    RunStatus.BLOCKED,
                    RunStatus.STALLED,
                    RunStatus.COMPLETED,
                    RunStatus.FAILED_RECOVERABLE,
                    RunStatus.FAILED_TERMINAL,
                }:
                    raise LeaseFenceError(
                        f"run cannot wait for resource lease in {projection.status.value}"
                    )
                await asyncio.sleep(self.poll_interval)

    async def _claim_once(
        self,
        lease: Lease,
        resources: tuple[tuple[str, str], ...],
        *,
        action_id: str,
    ) -> tuple[ResourceLease, ...]:
        """Make a claim cancellation-safe across the SQLite commit boundary."""

        claim_task = asyncio.create_task(
            self.store.claim_resources(
                lease.run_id,
                lease.epoch,
                resources,
                ttl_seconds=self.ttl_seconds,
                action_id=action_id,
            ),
            name=f"cc-harness-resource-claim-{lease.run_id}-{action_id}",
        )
        try:
            return await asyncio.shield(claim_task)
        except asyncio.CancelledError:
            # Cancellation can arrive just after SQLite commits the rows but
            # before the await resumes.  Drain the shielded task and release
            # any rows it returned, otherwise a cancelled Worker could leave
            # a lock until its TTL.
            with contextlib.suppress(Exception):
                claimed = await asyncio.shield(claim_task)
                await self.store.release_resources(claimed)
            raise

    async def renew(self, leases: Iterable[ResourceLease]) -> tuple[ResourceLease, ...]:
        return await self.store.renew_resources(leases, ttl_seconds=self.ttl_seconds)

    async def release(self, leases: Iterable[ResourceLease]) -> int:
        return await self.store.release_resources(leases)

    @asynccontextmanager
    async def hold_for_action(
        self,
        lease: Lease,
        request: ActionRequest,
        *,
        effect: EffectClass | str,
        working_directory: Path,
        action_id: str | None = None,
    ) -> AsyncIterator[tuple[ResourceLease, ...]]:
        resources = await self.acquire_for_action(
            lease,
            request,
            effect=effect,
            working_directory=working_directory,
            action_id=action_id,
        )
        heartbeat_task: asyncio.Task[None] | None = None
        if resources:
            heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(lease, resources),
                name=f"cc-harness-resource-heartbeat-{lease.run_id}-{request.action_id}",
            )
        try:
            yield resources
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
            with contextlib.suppress(LeaseFenceError, RunStoreError, Exception):
                await self.release(resources)

    async def _heartbeat_loop(
        self,
        lease: Lease,
        resources: tuple[ResourceLease, ...],
    ) -> None:
        interval = max(0.25, min(10.0, self.ttl_seconds / 3.0))
        while True:
            await asyncio.sleep(interval)
            try:
                await self.renew(resources)
                # Resource rows are renewed in place; the worker lease itself
                # remains the fencing authority and is heartbeated separately
                # by RunWorker.  Do not inspect the immutable Lease handle's
                # original expiry here: RunWorker renews the same epoch in
                # place, so a long action must keep its resource lock alive
                # beyond the first TTL window.
            except (LeaseFenceError, RunStoreError, Exception):
                return


__all__ = [
    "LeaseManager",
    "ResourceLeaseManager",
    "ResourceSpec",
    "SupervisorLeaseManager",
    "resource_specs_for_action",
]
