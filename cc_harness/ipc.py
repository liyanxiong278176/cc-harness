"""Authenticated in-process transport used by local clients and tests."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Mapping, Protocol

from .coordinator import RunCoordinator, RunRequest


class IPCError(RuntimeError):
    pass


class CoordinatorTransport(Protocol):
    async def request(self, command: Mapping[str, Any]) -> Mapping[str, Any]: ...


def encode_command(command: Mapping[str, Any], *, auth_token: str) -> bytes:
    body = json.dumps(dict(command), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    signature = hmac.new(auth_token.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()
    return json.dumps({"body": body, "signature": signature}, separators=(",", ":")).encode("utf-8")


def decode_command(raw: bytes, *, auth_token: str) -> dict[str, Any]:
    try:
        envelope = json.loads(raw.decode("utf-8"))
        body = str(envelope["body"])
        signature = str(envelope["signature"])
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise IPCError("invalid IPC envelope") from exc
    expected = hmac.new(auth_token.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise IPCError("IPC authentication failed")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise IPCError("IPC command must be an object")
    return value


class LocalCoordinatorTransport:
    def __init__(self, coordinator: RunCoordinator, *, auth_token: str) -> None:
        self.coordinator = coordinator
        self.auth_token = auth_token

    async def request(self, command: Mapping[str, Any]) -> Mapping[str, Any]:
        raw = encode_command(command, auth_token=self.auth_token)
        decoded = decode_command(raw, auth_token=self.auth_token)
        kind = str(decoded.get("command") or "")
        if kind in {"run", "submit"}:
            request = RunRequest(
                objective=str(decoded["objective"]),
                acceptance_criteria=tuple(str(item) for item in decoded.get("acceptance_criteria") or ("completed",)),
            )
            handle = await self.coordinator.submit(request)
            return {"run_id": handle.run_id}
        if kind == "status":
            view = await self.coordinator.inspect(str(decoded["run_id"]))
            return {"run_id": view.run_id, "status": view.status.value, "sequence": view.sequence}
        if kind == "list":
            views = await self.coordinator.list(set(decoded.get("statuses") or ()) or None)
            return {"runs": [{"run_id": view.run_id, "status": view.status.value, "sequence": view.sequence} for view in views]}
        if kind == "follow-up":
            receipt = await self.coordinator.send(str(decoded["run_id"]), str(decoded["message"]))
            return {
                "run_id": receipt.run_id,
                "sequence": receipt.sequence,
                "artifact": receipt.message_artifact,
                "follow_up_run_id": receipt.follow_up_run_id,
            }
        if kind == "approve":
            decision = await self.coordinator.approve(
                run_id=str(decoded["run_id"]),
                approval_id=str(decoded["approval_id"]),
                action_args_digest=str(decoded["action_args_digest"]),
            )
            return {"run_id": decision.run_id, "approval_id": decision.approval_id, "status": decision.status}
        if kind == "reject":
            decision = await self.coordinator.reject(
                run_id=str(decoded["run_id"]),
                approval_id=str(decoded["approval_id"]),
                reason=str(decoded.get("reason") or "client rejected"),
            )
            return {"run_id": decision.run_id, "approval_id": decision.approval_id, "status": decision.status}
        if kind == "resume":
            receipt = await self.coordinator.resume(str(decoded["run_id"]), str(decoded.get("reason") or "client resume"))
            return {"run_id": receipt.run_id, "status": receipt.status.value, "sequence": receipt.sequence}
        if kind == "rollback":
            receipt = await self.coordinator.rollback(str(decoded["run_id"]), str(decoded.get("reason") or "client rollback"))
            return {"run_id": receipt.run_id, "status": receipt.status.value, "sequence": receipt.sequence}
        if kind == "interrupt":
            receipt = await self.coordinator.interrupt(str(decoded["run_id"]), str(decoded.get("reason") or "client interrupt"))
            return {"run_id": receipt.run_id, "status": receipt.status.value, "sequence": receipt.sequence}
        if kind == "cancel":
            receipt = await self.coordinator.cancel(str(decoded["run_id"]), str(decoded.get("reason") or "client cancel"))
            return {"run_id": receipt.run_id, "status": receipt.status.value, "sequence": receipt.sequence}
        raise IPCError(f"unsupported command: {kind}")


__all__ = ["CoordinatorTransport", "IPCError", "LocalCoordinatorTransport", "decode_command", "encode_command"]
