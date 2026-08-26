"""Local sidecar bridge for the optional Tauri desktop client.

The bridge deliberately delegates lifecycle, permissions, event persistence,
and execution to the existing durable runtime.  It is a transport adapter, not
another agent loop.  Run it with ``python -m cc_harness.desktop_bridge`` while
the desktop shell owns the process and speaks one JSON object per line.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from urllib.parse import urlparse
from pathlib import Path
from typing import Any, Mapping

from .desktop_protocol import (
    DesktopProtocolError,
    event as event_message,
    response,
    validate_request,
)
from .config import ConfigError
from .desktop_config import read_workspace_config, save_workspace_config
from .durable_runtime import DurableRuntimeClient
from .run_model import RunStatus
from .run_store import RunNotFound, RunStoreError


LOGGER = logging.getLogger(__name__)


def _status_value(value: Any) -> str:
    return value.value if isinstance(value, RunStatus) else str(value)


def _view_to_dict(view: Any) -> dict[str, Any]:
    projection = getattr(view, "projection", None)
    return {
        "run_id": str(view.run_id),
        "status": _status_value(view.status),
        "sequence": int(view.sequence),
        "projection": projection.to_dict() if projection is not None else None,
    }


def _record_to_dict(record: Any, *, storage_error: bool = False) -> dict[str, Any]:
    """Return a projection-free run summary when a durable projection is stale.

    Listing must remain useful even when an older database has a mismatched
    projection cursor.  The record row is still authoritative for navigation
    and shutdown warnings; we never rewrite it implicitly from the desktop
    process.
    """

    result = {
        "run_id": str(record.run_id),
        "status": _status_value(record.status),
        "sequence": int(record.sequence),
    }
    if storage_error:
        result["storage_error"] = True
        result["storage_error_code"] = "projection_inconsistent"
    return result


class DesktopBridge:
    """Serve versioned requests over a supervised local stdio connection."""

    def __init__(self, client: DurableRuntimeClient) -> None:
        self.client = client
        self._write_lock = asyncio.Lock()
        self._watchers: dict[str, asyncio.Task[None]] = {}
        self._closed = False
        self._runtime_started = False

    async def write(self, message: Mapping[str, Any]) -> None:
        """Write one compact JSON message atomically to stdout."""

        encoded = json.dumps(
            dict(message), ensure_ascii=False, separators=(",", ":")
        )
        async with self._write_lock:
            await asyncio.to_thread(self._write_line, encoded)

    @staticmethod
    def _write_line(encoded: str) -> None:
        sys.stdout.write(encoded + "\n")
        sys.stdout.flush()

    async def handle(self, raw: Any) -> dict[str, Any]:
        """Validate and dispatch one request, converting errors to safe envelopes."""

        request_id = raw.get("request_id") if isinstance(raw, Mapping) else None
        try:
            request = validate_request(raw)
            data = await self._dispatch(request["type"], request["payload"])
            return response(request["request_id"], ok=True, data=data)
        except DesktopProtocolError as exc:
            return response(
                request_id,
                ok=False,
                error={"code": "invalid_request", "message": str(exc)},
            )
        except RunNotFound:
            return response(
                request_id,
                ok=False,
                error={"code": "run_not_found", "message": "run was not found"},
            )
        except RunStoreError:
            return response(
                request_id,
                ok=False,
                error={
                    "code": "storage_error",
                    "message": "durable run state is inconsistent; repair is required",
                },
            )
        except ConfigError as exc:
            return response(
                request_id,
                ok=False,
                error={"code": "configuration_error", "message": str(exc)},
            )
        except Exception as exc:  # noqa: BLE001 - bridge must not die on one request
            LOGGER.exception("desktop bridge request failed")
            return response(
                request_id,
                ok=False,
                error={
                    "code": "internal_error",
                    "message": "desktop bridge request failed",
                    "error_type": type(exc).__name__,
                },
            )

    async def _dispatch(
        self, message_type: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        if message_type == "hello":
            return {
                "protocol_version": 1,
                "transport": "stdio",
                **self._runtime_info(),
                "capabilities": [
                    "list",
                    "inspect",
                    "events",
                    "usage",
                    "config",
                    "watch",
                    "submit",
                    "follow_up",
                    "interrupt",
                    "cancel",
                    "resume",
                    "approve",
                    "reject",
                    "shutdown",
                ],
            }
        if message_type == "ping":
            return {"pong": True, **self._runtime_info()}
        if message_type == "config":
            action = str(payload.get("action", "get")).strip().lower()
            if action == "get":
                return {
                    **read_workspace_config(self.client.cwd),
                    "runtime_started": self._runtime_started,
                }
            if action == "save":
                if self._runtime_started:
                    raise ConfigError(
                        "runtime already started; finish or stop the current run before changing model settings"
                    )
                return save_workspace_config(
                    self.client.cwd,
                    base_url=self._required_text(payload.get("base_url"), "base_url"),
                    model=self._required_text(payload.get("model"), "model"),
                    api_key=(
                        str(payload.get("api_key"))
                        if payload.get("api_key") is not None
                        else None
                    ),
                )
            raise DesktopProtocolError("config action must be get or save")
        if message_type in {"start", "start_runtime"}:
            await self.client.start_supervisor(
                worker_id=str(payload.get("worker_id"))
                if payload.get("worker_id")
                else None,
                max_workers=int(payload.get("max_workers", 3)),
                reasoning_effort=(
                    str(payload["reasoning_effort"])
                    if payload.get("reasoning_effort")
                    else None
                ),
                capability_profile=str(payload.get("capability_profile", "standard")),
                host_execution=bool(payload.get("host_execution", False)),
            )
            self._runtime_started = True
            return self._runtime_info()
        if message_type == "list":
            statuses = payload.get("statuses")
            status_set = (
                {str(item) for item in statuses}
                if isinstance(statuses, list)
                else None
            )
            return {"runs": await self._list_views(status_set)}
        if message_type in {"inspect", "status"}:
            run_id = self._run_id(payload)
            return _view_to_dict(await self.client.coordinator.inspect(run_id))
        if message_type == "events":
            run_id = self._run_id(payload)
            after = self._non_negative_int(payload.get("after_sequence", 0), "after_sequence")
            limit = self._positive_int(payload.get("limit", 200), "limit")
            page = await self.client.store.read(run_id, after=after, limit=min(limit, 500))
            return {
                "run_id": run_id,
                "events": [item.to_dict() for item in page.events],
                "next_cursor": page.next_cursor,
            }
        if message_type == "usage":
            run_id = self._run_id(payload)
            return await self._usage(run_id)
        if message_type == "watch":
            run_id = self._run_id(payload)
            watch_id = str(payload.get("watch_id") or run_id)
            after = self._non_negative_int(payload.get("after_sequence", 0), "after_sequence")
            await self._stop_watch(watch_id)
            self._watchers[watch_id] = asyncio.create_task(
                self._watch_loop(run_id, watch_id, after),
                name=f"desktop-watch-{watch_id}",
            )
            return {"watch_id": watch_id, "run_id": run_id, "after_sequence": after}
        if message_type in {"unwatch", "stop_watch"}:
            watch_id = str(payload.get("watch_id") or self._run_id(payload))
            stopped = await self._stop_watch(watch_id)
            return {"watch_id": watch_id, "stopped": stopped}
        if message_type == "submit":
            objective = self._required_text(payload.get("objective"), "objective")
            criteria = payload.get("acceptance_criteria", ["request addressed"])
            if not isinstance(criteria, list) or not all(isinstance(item, str) for item in criteria):
                raise DesktopProtocolError("acceptance_criteria must be a list of strings")
            run_id = await self.client.submit(objective, tuple(criteria))
            if bool(payload.get("auto_start", False)):
                await self._ensure_runtime_started(payload)
            return {"run_id": run_id, **self._runtime_info()}
        if message_type in {"follow_up", "send"}:
            run_id = self._run_id(payload)
            message = self._required_text(payload.get("message"), "message")
            receipt = await self.client.coordinator.send(run_id, message)
            if bool(payload.get("auto_start", False)):
                await self._ensure_runtime_started(payload)
            return {
                "run_id": receipt.run_id,
                "follow_up_run_id": receipt.follow_up_run_id,
                "sequence": receipt.sequence,
                **self._runtime_info(),
            }
        if message_type in {"interrupt", "cancel", "resume"}:
            run_id = self._run_id(payload)
            reason = str(payload.get("reason") or f"desktop {message_type}")
            coordinator = self.client.coordinator
            if message_type == "interrupt":
                receipt = await coordinator.interrupt(run_id, reason)
            elif message_type == "cancel":
                receipt = await coordinator.cancel(run_id, reason)
            else:
                receipt = await coordinator.resume(run_id, reason)
                if bool(payload.get("auto_start", False)):
                    await self._ensure_runtime_started(payload)
            return {
                "run_id": receipt.run_id,
                "status": _status_value(receipt.status),
                "sequence": receipt.sequence,
                **self._runtime_info(),
            }
        if message_type in {"approve", "reject"}:
            run_id = self._run_id(payload)
            approval_id = self._required_text(payload.get("approval_id"), "approval_id")
            if message_type == "approve":
                digest = self._required_text(
                    payload.get("action_args_digest"), "action_args_digest"
                )
                decision = await self.client.coordinator.approve(
                    run_id=run_id,
                    approval_id=approval_id,
                    action_args_digest=digest,
                )
            else:
                decision = await self.client.coordinator.reject(
                    run_id=run_id,
                    approval_id=approval_id,
                    reason=str(payload.get("reason") or "desktop rejected"),
                )
            return {
                "run_id": run_id,
                "approval_id": decision.approval_id,
                "status": decision.status,
            }
        if message_type == "shutdown":
            confirm = bool(payload.get("confirm", False))
            active = await self._active_runs()
            if active and not confirm:
                return {
                    "requires_confirmation": True,
                    "active_runs": active,
                    "shutdown": False,
                }
            await self.close()
            return {"shutdown": True, "active_runs": active}
        raise DesktopProtocolError(f"unsupported request type: {message_type}")

    async def _ensure_runtime_started(self, payload: Mapping[str, Any]) -> None:
        if self._runtime_started:
            return
        await self.client.start_supervisor(
            max_workers=int(payload.get("max_workers", 3)),
            reasoning_effort=(
                str(payload["reasoning_effort"])
                if payload.get("reasoning_effort")
                else None
            ),
            capability_profile=str(payload.get("capability_profile", "standard")),
            host_execution=bool(payload.get("host_execution", False)),
        )
        self._runtime_started = True

    def _runtime_info(self) -> dict[str, Any]:
        llm = getattr(self.client, "_llm", None)
        base_url = (
            getattr(getattr(llm, "_client", None), "base_url", None)
            if llm is not None
            else None
        )
        return {
            "runtime_started": self._runtime_started,
            "model": getattr(llm, "resolved_model", None) if llm is not None else None,
            "connection": (
                urlparse(str(base_url)).netloc
                if base_url
                else ("configured" if self._runtime_started else "not_started")
            ),
        }

    async def _watch_loop(self, run_id: str, watch_id: str, after: int) -> None:
        try:
            while not self._closed:
                page = await self.client.store.read(run_id, after=after, limit=200)
                if page.events:
                    for item in page.events:
                        await self.write(
                            event_message(
                                run_id=run_id,
                                sequence=item.sequence,
                                event_type=item.event_type,
                                payload=item.to_dict(),
                                watch_id=watch_id,
                            )
                        )
                    after = page.events[-1].sequence
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a watcher must not kill the bridge
            LOGGER.exception("desktop event watcher failed for %s", run_id)

    async def _usage(self, run_id: str) -> dict[str, Any]:
        """Aggregate provider-reported usage from durable assistant events."""

        totals = {
            "input_tokens": 0,
            "uncached_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 0,
            "model_calls": 0,
        }
        reported_cost = 0.0
        currencies: set[str] = set()
        after = 0
        while True:
            page = await self.client.store.read(run_id, after=after, limit=500)
            for item in page.events:
                usage = item.payload.get("usage")
                if not isinstance(usage, Mapping):
                    continue
                for key in totals:
                    value = usage.get(key)
                    if isinstance(value, (int, float)):
                        totals[key] += int(value)
                cost = usage.get("reported_cost")
                if isinstance(cost, (int, float)):
                    reported_cost += float(cost)
                currency = usage.get("reported_cost_currency")
                if isinstance(currency, str) and currency:
                    currencies.add(currency)
            if page.next_cursor is None:
                break
            after = page.next_cursor
        totals["cache_hit_ratio"] = (
            totals["cache_read_input_tokens"] / totals["input_tokens"]
            if totals["input_tokens"]
            else 0.0
        )
        return {
            "run_id": run_id,
            **totals,
            "reported_cost": reported_cost if currencies or reported_cost else None,
            "reported_cost_currency": next(iter(currencies)) if len(currencies) == 1 else None,
            "cost_status": "reported" if currencies or reported_cost else "unavailable",
        }

    async def _stop_watch(self, watch_id: str) -> bool:
        task = self._watchers.pop(watch_id, None)
        if task is None:
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    async def _active_runs(self) -> list[dict[str, Any]]:
        records = await self.client.store.list_runs(
            {
                RunStatus.QUEUED.value,
                RunStatus.RUNNING.value,
                RunStatus.AWAITING_APPROVAL.value,
                RunStatus.CANCEL_REQUESTED.value,
                RunStatus.STALLED.value,
            }
        )
        return [_record_to_dict(record) for record in records]

    async def _list_views(self, statuses: set[str] | None) -> list[dict[str, Any]]:
        """List runs without allowing one stale projection to hide all runs."""

        records = await self.client.store.list_runs(statuses)
        result: list[dict[str, Any]] = []
        for record in records:
            try:
                result.append(_view_to_dict(await self.client.coordinator.inspect(record.run_id)))
            except RunStoreError:
                result.append(_record_to_dict(record, storage_error=True))
        return result

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        watchers = tuple(self._watchers)
        for watch_id in watchers:
            await self._stop_watch(watch_id)
        await self.client.close()

    @staticmethod
    def _run_id(payload: Mapping[str, Any]) -> str:
        return DesktopBridge._required_text(payload.get("run_id"), "run_id")

    @staticmethod
    def _required_text(value: Any, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise DesktopProtocolError(f"{name} must be a non-empty string")
        return value.strip()

    @staticmethod
    def _non_negative_int(value: Any, name: str) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise DesktopProtocolError(f"{name} must be an integer") from exc
        if parsed < 0:
            raise DesktopProtocolError(f"{name} must be non-negative")
        return parsed

    @staticmethod
    def _positive_int(value: Any, name: str) -> int:
        parsed = DesktopBridge._non_negative_int(value, name)
        if parsed < 1:
            raise DesktopProtocolError(f"{name} must be positive")
        return parsed


async def _serve(args: argparse.Namespace) -> int:
    client = await DurableRuntimeClient.create(args.cwd, data_root=args.data_root)
    bridge = DesktopBridge(client)
    try:
        while not bridge._closed:
            line = await asyncio.to_thread(sys.stdin.buffer.readline)
            if not line:
                break
            try:
                raw = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                await bridge.write(
                    response(
                        None,
                        ok=False,
                        error={"code": "invalid_json", "message": str(exc)},
                    )
                )
                continue
            await bridge.write(await bridge.handle(raw))
    finally:
        await bridge.close()
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="cc-harness desktop sidecar bridge")
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--log-level", default="WARNING")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, str(args.log_level).upper(), logging.WARNING))
    try:
        return asyncio.run(_serve(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover - exercised by packaged sidecar
    raise SystemExit(main())


__all__ = ["DesktopBridge", "main"]
