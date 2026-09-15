"""Local WebUI control plane for the single Durable Runtime.

The WebUI is intentionally a thin control surface.  It does not run a second
agent loop: project selection, submission, follow-ups, approvals, cancellation,
event replay, and usage all delegate to :class:`DurableRuntimeClient` and its
event-sourced store.  REST is used for commands and replayable SSE is used for
live run-tree events so follow-up/child responses arrive without a manual
refresh and a browser refresh cannot lose a checkpoint.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import socket
import sqlite3
import tempfile
import time
import webbrowser
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import dotenv_values
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import ConfigError, load_context_config
from .durable_runtime import DurableRuntimeClient
from .fact_store import default_user_data_dir
from .live_stream import LiveStreamHub
from .permissions import normalize_permission_mode
from .run_model import RunStatus
from .run_store import RunNotFound, SequenceConflict, SupervisorLeaseConflict, SupervisorLeaseFenceError
from .approvals import ApprovalDigestMismatchError, ApprovalNotFoundError


_SECRET_KEY = re.compile(
    r"(?:api.?key|access.?token|authorization|cookie|credential|password|passwd|private.?key|secret)",
    re.IGNORECASE,
)
_MODEL_FIELDS = ("base_url", "model", "api_key")
_PERMISSION_FIELD = "permission_mode"
_COMPLETION_BLOCK = re.compile(
    r"<cc-harness-complete>\s*(\{.*?\})\s*</cc-harness-complete>",
    re.DOTALL | re.IGNORECASE,
)
_COMPLETION_FENCE = re.compile(
    r"```(?:json)?\s*(\{.*?\})\s*```",
    re.DOTALL | re.IGNORECASE,
)
_ACTIVE_STATUSES = {
    RunStatus.QUEUED,
    RunStatus.RUNNING,
    RunStatus.AWAITING_APPROVAL,
    RunStatus.WAITING_ON_PREDECESSOR,
    RunStatus.CANCEL_REQUESTED,
}
_ACTIVE_STATUS_PRIORITY = {
    RunStatus.AWAITING_APPROVAL: 5,
    RunStatus.RUNNING: 4,
    RunStatus.QUEUED: 3,
    RunStatus.WAITING_ON_PREDECESSOR: 2,
    RunStatus.CANCEL_REQUESTED: 1,
}
_RECOVERABLE_STATUSES = {
    RunStatus.CANCELLED,
    RunStatus.STALLED,
    RunStatus.BLOCKED,
    RunStatus.FAILED_RECOVERABLE,
    RunStatus.WAITING_ON_PREDECESSOR,
}


class SessionDeleteConflict(RuntimeError):
    """Raised when a running session has not reached a safe delete boundary."""


class ApprovalStaleError(RuntimeError):
    """Raised when a browser approval card is no longer actionable.

    ApprovalRequested is immutable audit evidence.  The actionable state is
    the latest projection, so a card can legitimately become stale after a
    stop, retry, another browser tab, or a supervisor decision.  This is a
    recoverable control-plane conflict, not a malformed request.
    """

    def __init__(self, approval_id: str) -> None:
        self.approval_id = approval_id
        super().__init__("审批不存在、已处理或不属于当前会话")


def _redact(value: Any, *, key: str = "") -> Any:
    """Remove secret-shaped fields before a payload reaches browser JSON."""

    if key and _SECRET_KEY.search(key):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {str(k): _redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item, key=key) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _public_capability_details(
    activation_caps: Mapping[str, Any],
    name: str,
    details: Any,
) -> dict[str, Any]:
    """Return capability details with effective feature switches reflected.

    Older activation snapshots can contain safety sub-feature flags captured
    before the top-level memory kill-switch was applied.  Those snapshots are
    retained as immutable runtime evidence, but the browser-facing projection
    must describe what was actually active for the run.  In particular,
    ``MEMORY_ENABLED=false`` makes capture and layered injection inactive even
    when a stale safety record says otherwise.
    """

    public = _redact(details or {})
    if not isinstance(public, dict):
        public = {}
    if name != "safety":
        return public

    memory = activation_caps.get("memory")
    memory_details = memory.get("details") if isinstance(memory, Mapping) else None
    memory_disabled = (
        isinstance(memory_details, Mapping)
        and memory_details.get("configured_enabled") is False
    ) or (isinstance(memory, Mapping) and memory.get("enabled") is False)
    if memory_disabled:
        public["memory_capture_enabled"] = False
        public["memory_layered_injection"] = False
    return public


def _read_json_artifact(client: DurableRuntimeClient, digest: Any) -> dict[str, Any] | None:
    if not digest:
        return None
    try:
        value = json.loads(client.store.artifacts.read_text(str(digest)))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _read_text_artifact(client: DurableRuntimeClient, digest: Any) -> str | None:
    if not digest:
        return None
    try:
        return client.store.artifacts.read_text(str(digest))
    except (OSError, TypeError, ValueError):
        return None


def _looks_like_completion_candidate(value: str) -> bool:
    """Identify the internal completion envelope without hiding normal prose."""

    try:
        decoded = json.loads(value.strip())
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return (
        isinstance(decoded, Mapping)
        and isinstance(decoded.get("acceptance_criteria"), list)
        and isinstance(decoded.get("evidence"), list)
    )


def _visible_assistant_text(value: str) -> str:
    """Remove completion protocol metadata from user-facing model prose.

    The original assistant artifact is intentionally left untouched for audit
    and replay.  This presentation-only filter handles both the tagged
    ``<cc-harness-complete>`` form and a provider that leaked the JSON envelope
    as a standalone/fenced response.
    """

    text = _COMPLETION_BLOCK.sub("", value).strip()
    if _looks_like_completion_candidate(text):
        return ""

    def remove_fenced(match: re.Match[str]) -> str:
        candidate = match.group(1)
        return "" if _looks_like_completion_candidate(candidate) else match.group(0)

    text = _COMPLETION_FENCE.sub(remove_fenced, text).strip()
    return text


# WebUI submissions default to an explicit interaction contract. Keep the
# automatic classifier conservative: anything that might touch a project,
# invoke a tool, or require verification remains a coding run. Only short,
# tool-free conversational prompts take the response-artifact completion path.
_CODING_INTENT_MARKERS = re.compile(
    r"(?:\b(?:code|file|files|project|directory|folder|test|tests|run|build|fix|implement|create|update|delete|install|dependency|dependencies|git|npm|python|docker|api|bug|review|deploy|refactor|command|terminal|service|server|database|frontend|backend|endpoint|config|configuration|commit|push|tool)\b|"
    r"代码|文件|项目|目录|文件夹|测试|运行|构建|修复|实现|创建|修改|删除|安装|依赖|命令|终端|服务|服务器|数据库|前端|后端|接口|配置|重构|提交|推送|审查|检查|部署|迁移|外卖|任务|读取|编辑|搜索|生成|写一个)",
    re.IGNORECASE,
)


def classify_interaction_mode(text: str, requested: str = "auto") -> str:
    """Choose a persisted interaction mode without trusting model prose.

    Callers may explicitly request ``coding`` or ``conversation``. ``auto``
    only selects conversation for a short prompt with no coding/tool intent;
    this prevents a coding task from bypassing verification because it happens
    to end with a question mark.
    """

    mode = str(requested or "auto").strip().casefold()
    if mode in {"coding", "conversation"}:
        return mode
    if mode != "auto":
        raise ValueError("interaction_mode must be auto, coding, or conversation")
    cleaned = str(text or "").strip()
    if not cleaned or len(cleaned) > 320 or cleaned.startswith("/"):
        return "coding"
    # A short prompt that explicitly says not to use tools is still a
    # conversation.  Strip those negated phrases before applying the marker
    # check; otherwise a natural-language smoke test such as
    # ``请简短回复，不要执行工具`` would be incorrectly sent through the
    # coding completion contract merely because it contains the word 工具.
    intent_text = re.sub(
        r"(?:不要|无需|请勿|禁止)(?:执行|调用|使用)?(?:任何)?工具",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    if _CODING_INTENT_MARKERS.search(intent_text):
        return "coding"
    return "conversation"


def _public_event(
    client: DurableRuntimeClient,
    event: Any,
    *,
    root_run_id: str | None = None,
) -> dict[str, Any] | None:
    """Return a safe, display-oriented event envelope.

    The durable event remains the authority.  Only committed assistant text,
    committed tool observations, and explicit user follow-up text are enriched;
    prompts, hidden rules, raw reasoning, credentials, and tool arguments are
    never sent to the browser.
    """

    event_type = str(event.event_type)
    payload = dict(event.payload) if isinstance(event.payload, Mapping) else {}
    result: dict[str, Any] = {
        "id": f"{event.run_id}:{event.sequence}",
        "event_id": event.event_id,
        "run_id": event.run_id,
        "sequence": event.sequence,
        "event_type": event_type,
        "occurred_at": event.occurred_at,
        "actor": event.actor.to_dict(),
        "payload": _redact(payload),
        "kind": "status",
    }

    if event_type == "RunCreated" and (root_run_id is None or event.run_id == root_run_id):
        goal = payload.get("goal")
        if isinstance(goal, Mapping) and isinstance(goal.get("objective"), str):
            result["kind"] = "user"
            result["content"] = goal["objective"]
    elif event_type == "FollowUpQueued":
        text = _read_text_artifact(client, payload.get("message_artifact"))
        if text is not None:
            result["kind"] = "user"
            result["content"] = text
    elif event_type == "RunResumed":
        reason = payload.get("reason")
        if isinstance(reason, str) and reason.strip():
            result["kind"] = "user"
            result["content"] = reason
    elif event_type == "AssistantMessageCommitted":
        message = _read_json_artifact(client, payload.get("message_artifact"))
        if message is not None and isinstance(message.get("content"), str):
            content = _visible_assistant_text(message["content"])
            if not content:
                # CompletionCandidate is an internal protocol exchange, not a
                # chat message.  Keep it in the durable event log but omit it
                # from the WebUI transcript and activity feed.
                return None
            result["kind"] = "assistant"
            result["content"] = content
    elif event_type == "ToolObservationCommitted":
        observation = _read_json_artifact(client, payload.get("observation_artifact"))
        if observation is not None:
            result["kind"] = "tool"
            result["tool_name"] = observation.get("tool_name") or payload.get("tool_name")
            # A rejected approval is a completed policy decision, not a
            # failed/unknown tool execution.  Preserve that distinction in
            # the public projection so the WebUI can explain why the tool
            # did not run and why the Runtime was allowed to continue.
            rejected = observation.get("error_kind") == "user_rejected"
            result["status"] = "rejected" if rejected else payload.get("status")
            if rejected:
                result["rejected"] = True
            content = observation.get("model_text") or observation.get("llm_text")
            if not content:
                blocks = observation.get("content")
                if isinstance(blocks, list):
                    content = "\n".join(
                        str(block.get("text") or "")
                        for block in blocks
                        if isinstance(block, Mapping) and block.get("text")
                    )
                else:
                    content = blocks or ""
            result["content"] = str(content)
    elif event_type in {"ActionPlanned", "ActionStarted", "ActionSucceeded", "ActionFailed", "ActionCancelled", "ActionOutcomeUnknown"}:
        result["tool_name"] = payload.get("tool_name")
        result["action_id"] = payload.get("action_id")
        if event_type == "ActionFailed":
            result["kind"] = "error"
    elif event_type == "RunOutcomeRecorded":
        result["kind"] = "outcome"
        result["outcome"] = _redact(payload)
    return result


def _public_events(
    client: DurableRuntimeClient,
    events: list[Any] | tuple[Any, ...],
    *,
    root_run_id: str,
) -> list[dict[str, Any]]:
    """Project events while omitting internal-only presentation records."""

    visible: list[dict[str, Any]] = []
    for event in events:
        item = _public_event(client, event, root_run_id=root_run_id)
        if item is not None:
            visible.append(item)
    return visible


class WebSettingsStore:
    """Persist WebUI model settings without mixing them into run events."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or (Path.home() / ".cc-harness" / "webui.json"))
        self._lock = asyncio.Lock()

    def _read_sync(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"version": 1, "defaults": {}, "projects": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return {"version": 1, "defaults": {}, "projects": {}}
        if not isinstance(value, dict):
            return {"version": 1, "defaults": {}, "projects": {}}
        value.setdefault("version", 1)
        value.setdefault("defaults", {})
        value.setdefault("projects", {})
        return value

    def _dotenv_fallback(self, project_root: Path | None) -> dict[str, str]:
        user_env = Path.home() / ".cc-harness" / ".env"
        values: dict[str, str] = {
            str(k): str(v)
            for k, v in dotenv_values(user_env).items()
            if v is not None
        }
        if project_root is not None:
            values.update(
                {
                    str(k): str(v)
                    for k, v in dotenv_values(project_root / ".env").items()
                    if v is not None
                }
            )
        for env_name in (
            "OPENAI_BASE_URL",
            "OPENAI_MODEL",
            "OPENAI_API_KEY",
            "CC_HARNESS_PERMISSION_MODE",
        ):
            if os.getenv(env_name):
                values[env_name] = str(os.environ[env_name])
        return values

    def _effective_sync(self, project_root: Path | None) -> dict[str, str]:
        data = self._read_sync()
        values = self._dotenv_fallback(project_root)
        defaults = data.get("defaults") if isinstance(data.get("defaults"), dict) else {}
        values.update({str(k): str(v) for k, v in defaults.items() if v not in (None, "")})
        if project_root is not None:
            projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
            override = projects.get(str(project_root))
            if isinstance(override, dict):
                values.update({str(k): str(v) for k, v in override.items() if v not in (None, "")})
        result = {
            "base_url": values.get("base_url") or values.get("OPENAI_BASE_URL", ""),
            "model": values.get("model") or values.get("OPENAI_MODEL", ""),
            "api_key": values.get("api_key") or values.get("OPENAI_API_KEY", ""),
            "permission_mode": normalize_permission_mode(
                values.get(_PERMISSION_FIELD)
                or values.get("CC_HARNESS_PERMISSION_MODE")
                or "default"
            ),
        }
        return result

    async def effective(self, project_root: Path | None) -> dict[str, str]:
        return await asyncio.to_thread(self._effective_sync, project_root)

    async def update(
        self,
        values: Mapping[str, Any],
        *,
        project_root: Path | None = None,
    ) -> dict[str, str]:
        async with self._lock:
            def write() -> dict[str, str]:
                data = self._read_sync()
                if project_root is None:
                    target = data.setdefault("defaults", {})
                else:
                    projects = data.setdefault("projects", {})
                    target = projects.setdefault(str(project_root), {})
                for field in _MODEL_FIELDS:
                    value = values.get(field)
                    if value is not None and str(value).strip():
                        target[field] = str(value).strip()
                if _PERMISSION_FIELD in values and values[_PERMISSION_FIELD] is not None:
                    target[_PERMISSION_FIELD] = normalize_permission_mode(
                        values[_PERMISSION_FIELD]
                    )
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd, temp_name = tempfile.mkstemp(prefix="webui-", suffix=".json", dir=self.path.parent)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        json.dump(data, handle, ensure_ascii=False, indent=2)
                        handle.write("\n")
                    with contextlib.suppress(OSError):
                        os.chmod(temp_name, 0o600)
                    os.replace(temp_name, self.path)
                finally:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(temp_name)
                return self._effective_sync(project_root)

            return await asyncio.to_thread(write)

    async def public(self, project_root: Path | None, *, reveal: bool = False) -> dict[str, Any]:
        values = await self.effective(project_root)
        key = values.get("api_key", "")
        result: dict[str, Any] = {
            "base_url": values.get("base_url", ""),
            "model": values.get("model", ""),
            "permission_mode": normalize_permission_mode(
                values.get(_PERMISSION_FIELD) or "default"
            ),
            "has_api_key": bool(key),
            "api_key_masked": (f"{key[:4]}…{key[-4:]}" if len(key) > 8 else ("••••••••" if key else "")),
        }
        if reveal:
            result["api_key"] = key
        return result


def _native_pick_directory() -> str | None:
    """Open a native folder picker when a desktop is available."""

    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        with contextlib.suppress(Exception):
            root.attributes("-topmost", True)
        try:
            selected = filedialog.askdirectory(title="选择 cc-harness 项目文件夹")
        finally:
            root.destroy()
        return selected or None
    except Exception:
        return None


class WebRuntimeManager:
    """Own project-scoped clients while preserving one Runtime implementation."""

    def __init__(self, *, initial_cwd: Path, data_root: Path | None = None, initial_prompt: str | None = None) -> None:
        self.initial_cwd = Path(initial_cwd).resolve()
        self.data_root = data_root
        self.initial_prompt = initial_prompt
        self.settings = WebSettingsStore()
        self.selected_root: Path | None = None
        self._clients: dict[str, DurableRuntimeClient] = {}
        self._client_fingerprints: dict[str, tuple[str, str, str, str]] = {}
        # A crashed scheduler leaves a valid project lease until its TTL
        # expires.  Keep a bounded, in-process takeover watcher so a restarted
        # WebUI can resume queued Runs without requiring a second user click;
        # this is recovery logic, not an external scheduled job.
        self._supervisor_retry_tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        # Best-effort in-process stream fan-out.  Durable events remain the
        # replay source; this hub only removes the visible delay between a
        # provider chunk and the next committed event.
        self.live_stream = LiveStreamHub()

    async def _emit_live_stream(self, event: Mapping[str, Any]) -> None:
        await self.live_stream.publish(event)

    def _schedule_supervisor_retry(self, root: Path) -> None:
        """Watch an expired project lease and reclaim scheduler leadership.

        Lease conflicts are normal when two WebUI windows attach to one
        project.  If the previous owner actually crashed, however, simply
        marking this process as control-only would strand queued work until a
        new manual request.  The watcher waits for the authoritative lease
        expiry, then performs a bounded takeover attempt using the same
        configuration path as an explicit send/resume.
        """

        key = str(Path(root).resolve())
        existing = self._supervisor_retry_tasks.get(key)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._retry_supervisor_until_available(Path(root).resolve()),
            name=f"cc-harness-supervisor-retry-{hashlib.sha1(key.encode()).hexdigest()[:10]}",
        )
        self._supervisor_retry_tasks[key] = task

        def finish(done: asyncio.Task[None]) -> None:
            if self._supervisor_retry_tasks.get(key) is done:
                self._supervisor_retry_tasks.pop(key, None)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                done.result()

        task.add_done_callback(finish)

    async def _retry_supervisor_until_available(self, root: Path) -> None:
        """Bounded takeover watcher for a stale project supervisor lease."""

        deadline = time.monotonic() + 15 * 60
        key = str(root.resolve())
        while time.monotonic() < deadline:
            client = self._clients.get(key)
            if client is None:
                return
            try:
                lease = await client.store.current_supervisor_lease()
            except Exception:
                await asyncio.sleep(1.0)
                continue
            if lease is not None and lease.expires_at > time.time():
                # Poll for an early, clean owner shutdown while never starting
                # the expensive provider stack while a live owner is present.
                await asyncio.sleep(min(10.0, max(0.25, lease.expires_at - time.time() + 0.05)))
                continue
            try:
                await self._client_for_root(root, start=True)
            except (SupervisorLeaseConflict, SupervisorLeaseFenceError):
                await asyncio.sleep(1.0)
                continue
            except (ConfigError, RunNotFound, ValueError):
                # Settings may have been cleared or the project removed while
                # waiting.  A subsequent explicit action will surface the
                # actionable setup error; do not spin in the background.
                return
            except Exception:
                # Environment/provider failures are surfaced by the next user
                # action.  Recovery must not become an unbounded error loop.
                return
            current_client = self._clients.get(key)
            if (
                current_client is not None
                and current_client.supervisor is not None
                and getattr(current_client.supervisor, "is_running", False)
            ):
                return
            await asyncio.sleep(1.0)

    @staticmethod
    def _scheduler_status(client: DurableRuntimeClient) -> dict[str, Any]:
        """Describe scheduler ownership without exposing lease internals.

        A project has one elected scheduler, but every WebUI/CLI process may
        still act as a durable control plane.  Returning this distinction to
        the browser turns a former ``SupervisorLeaseConflict`` 500 into an
        actionable, observable state (the other owner will consume queued
        work) instead of making the conversation look broken.
        """

        if client.supervisor_owned_elsewhere:
            return {
                "mode": "external",
                "running": False,
                "label": "其他窗口运行中",
            }
        supervisor = client.supervisor
        if supervisor is not None and getattr(supervisor, "is_running", False):
            return {
                "mode": "local",
                "running": True,
                "label": "本窗口运行中",
            }
        return {
            "mode": "idle",
            "running": False,
            "label": "等待启动",
        }

    @property
    def project_key(self) -> str | None:
        return str(self.selected_root) if self.selected_root is not None else None

    def _data_root_path(self) -> Path:
        """Resolve the durable data root without creating any directories."""

        return Path(self.data_root) if self.data_root is not None else default_user_data_dir()

    def _discover_project_roots_sync(self) -> tuple[Path, ...]:
        """Find previously opened projects from their durable store registry.

        The WebUI used to list runs only for the currently selected folder,
        which made the sidebar look flat and made old project history appear
        to vanish after a restart.  ``project_record`` is the durable source
        of truth for the path; only existing directories are returned so
        pytest/temporary projects left behind by old runs do not clutter the
        UI.
        """

        projects_root = self._data_root_path() / "projects"
        if not projects_root.is_dir():
            return ()
        discovered: list[Path] = []
        for state_dir in sorted(projects_root.iterdir(), key=lambda item: item.name):
            db_path = state_dir / "runtime.db"
            if not db_path.is_file():
                continue
            try:
                with sqlite3.connect(str(db_path), timeout=0.2) as database:
                    row = database.execute(
                        "SELECT canonical_root FROM project_record LIMIT 1"
                    ).fetchone()
            except (OSError, sqlite3.Error):
                continue
            if not row or not row[0]:
                continue
            try:
                root = Path(str(row[0])).expanduser().resolve(strict=True)
            except OSError:
                continue
            if root.is_dir():
                discovered.append(root)
        return tuple(discovered)

    async def _project_roots(self) -> tuple[Path, ...]:
        """Return selected, cached, and persisted project roots in UI order."""

        roots: list[Path] = []
        if self.selected_root is not None:
            roots.append(self.selected_root)
        roots.extend(await asyncio.to_thread(self._discover_project_roots_sync))
        roots.extend(Path(key) for key in self._clients)
        unique: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            try:
                resolved = root.resolve(strict=True)
            except OSError:
                continue
            key = os.path.normcase(str(resolved))
            if key in seen:
                continue
            seen.add(key)
            unique.append(resolved)
        return tuple(unique)

    async def select_project(self, path: str) -> Path:
        candidate = Path(path).expanduser()
        try:
            root = candidate.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"项目文件夹不可用: {exc}") from exc
        if not root.is_dir():
            raise ValueError("项目路径必须是文件夹")
        async with self._lock:
            key = str(root)
            if key not in self._clients:
                self._clients[key] = await DurableRuntimeClient.create(root, data_root=self.data_root)
            self.selected_root = root
        # Reattach the detached execution loop when a browser refresh or a
        # WebUI restart discovers queued durable work.  Without this, a
        # follow-up submitted just before a restart could remain in SQLite
        # forever even though its predecessor is now a safe stalled boundary.
        # Missing settings are intentionally ignored here; the user should be
        # able to select a project and open Settings before configuring a
        # provider.  A later send/resume starts the supervisor normally.
        await self._start_pending_if_configured(root)
        return root

    async def _start_pending_if_configured(self, root: Path) -> None:
        """Resume a project's queued durable work after WebUI reattachment."""

        key = str(Path(root).resolve())
        client = self._clients.get(key)
        if client is None or client.supervisor is not None:
            return
        try:
            records = await client.store.list_runs()
            pending = any(
                record.status
                in {
                    RunStatus.QUEUED.value,
                    RunStatus.RUNNING.value,
                    RunStatus.CANCEL_REQUESTED.value,
                }
                for record in records
            )
            if not pending:
                for record in records:
                    if record.parent_run_id is not None:
                        continue
                    view = await client.coordinator.inspect(record.run_id)
                    if any(item.status == "queued" for item in view.projection.queue):
                        pending = True
                        break
            if not pending:
                return
            values = await self.settings.effective(root)
            if not all(values.get(field) for field in _MODEL_FIELDS):
                return
            await self._client_for_root(root, start=True)
        except Exception:
            # Project selection must remain usable when a provider is
            # temporarily unavailable.  The next explicit send/resume will
            # surface the actionable setup error through the normal API path.
            return

    async def pick_project(self) -> Path | None:
        path = await asyncio.to_thread(_native_pick_directory)
        if not path:
            return None
        return await self.select_project(path)

    def _require_root(self) -> Path:
        if self.selected_root is None:
            raise ValueError("请先选择本地项目文件夹")
        return self.selected_root

    async def _client_for_root(self, root: Path, *, start: bool = False) -> DurableRuntimeClient:
        root = Path(root).resolve(strict=True)
        key = str(root)
        client = self._clients.get(key)
        if client is None:
            client = await DurableRuntimeClient.create(root, data_root=self.data_root)
            self._clients[key] = client
        if not start:
            return client
        values = await self.settings.effective(root)
        missing = [name for name, value in (("base_url", values["base_url"]), ("model", values["model"]), ("api_key", values["api_key"])) if not value]
        if missing:
            raise ConfigError("missing model configuration: " + ", ".join(missing))
        if client.supervisor_owned_elsewhere:
            # Avoid rebuilding MCP/LLM/capability resources on every browser
            # poll while the elected scheduler is still alive.  The bounded
            # watcher above will retry once this lease expires or is released.
            lease = await client.store.current_supervisor_lease()
            if lease is not None and lease.expires_at > time.time():
                return client
            client.clear_supervisor_owned_elsewhere()
        # Keep the API key out of manager state/logs while still recycling a
        # supervisor when the user changes credentials.
        key_digest = hashlib.sha256(values["api_key"].encode("utf-8")).hexdigest()
        permission_mode = normalize_permission_mode(values.get(_PERMISSION_FIELD))
        fingerprint = (values["base_url"], values["model"], key_digest, permission_mode)
        existing_fingerprint = self._client_fingerprints.get(key)
        if client.supervisor is not None and existing_fingerprint not in (None, fingerprint):
            active = []
            for record in await client.store.list_runs():
                if record.parent_run_id is not None:
                    continue
                view = await client.coordinator.inspect(record.run_id)
                if view.status in _ACTIVE_STATUSES:
                    active.append(record.run_id)
            if active:
                raise ConfigError("当前运行仍在使用旧配置；结束后新会话才会应用新配置")
            await client.close()
            client = await DurableRuntimeClient.create(root, data_root=self.data_root)
            self._clients[key] = client
        # ``client.supervisor`` is a handle, not a liveness guarantee.  A
        # detached loop can have exited after a lease conflict or an
        # unexpected event-loop error while the handle remains attached.  Ask
        # DurableRuntimeClient to restart that loop so a rejection/resume can
        # never leave a queued run stranded behind a dead supervisor.
        supervisor_running = (
            client.supervisor is not None
            and getattr(client.supervisor, "is_running", True)
        )
        if not supervisor_running:
            try:
                await client.start_supervisor(
                    max_workers=3,
                    config_overrides={
                        "OPENAI_BASE_URL": values["base_url"],
                        "OPENAI_MODEL": values["model"],
                        "OPENAI_API_KEY": values["api_key"],
                        "CC_HARNESS_PERMISSION_MODE": permission_mode,
                    },
                    permission_mode=permission_mode,
                    stream_emitter=self._emit_live_stream,
                )
            except (SupervisorLeaseConflict, SupervisorLeaseFenceError):
                # Another WebUI/CLI process is already the project scheduler.
                # This process remains a control plane: durable submit/resume/
                # approval writes are safe, and the existing owner will pick
                # them up.  Do not turn a benign multi-window race into HTTP
                # 500 or strand the conversation behind a dead UI.
                client.supervisor = None
                client.mark_supervisor_owned_elsewhere()
                self._schedule_supervisor_retry(root)
        else:
            # A client may have been started by another control-plane path
            # before the WebUI attached.  Update the worker-factory closure so
            # newly claimed Runs still publish ephemeral chunks here.
            client.set_stream_emitter(self._emit_live_stream)
        self._client_fingerprints[key] = fingerprint
        return client

    async def _client(self, *, start: bool = False) -> DurableRuntimeClient:
        return await self._client_for_root(self._require_root(), start=start)

    async def sessions(self, *, all_projects: bool = True) -> list[dict[str, Any]]:
        roots = await self._project_roots() if all_projects else (self._require_root(),)
        result: list[dict[str, Any]] = []
        for root in roots:
            try:
                client = await self._client_for_root(root)
                records = await client.store.list_runs()
            except Exception:
                continue
            for record in records:
                if record.parent_run_id is not None:
                    continue
                try:
                    _root_view, view, _head, _approvals, _tree = await self._conversation_snapshot(client, record.run_id)
                except Exception:
                    continue
                objective = _root_view.projection.goal.objective if _root_view.projection.goal else "新会话"
                sequence = max((item.sequence for item in _tree), default=view.sequence)
                result.append(
                    {
                        "run_id": record.run_id,
                        "title": objective.splitlines()[0][:96] or "新会话",
                        "status": view.status.value,
                        "sequence": sequence,
                        "active_worker_id": view.projection.active_worker_id,
                        "updated_sequence": sequence,
                        "project_root": str(root),
                        "scheduler": self._scheduler_status(client),
                    }
                )
        return result

    async def _conversation_snapshot(
        self,
        client: DurableRuntimeClient,
        root_run_id: str,
    ) -> tuple[Any, Any, Any, tuple[tuple[str, Any], ...], tuple[Any, ...]]:
        """Read the root and descendant Runs as one user-facing conversation.

        A WebUI session is backed by a root Run, while follow-up turns are
        durable child Runs. Looking only at the root makes a child that is
        running or awaiting approval appear to be missing. This snapshot is
        presentation-only: every individual Run and event remains authoritative
        in the durable store.
        """

        run_ids = await client.run_tree(root_run_id)
        views: list[Any] = []
        creation_order: dict[str, tuple[str, str]] = {}
        for index, tree_run_id in enumerate(run_ids):
            try:
                views.append(await client.coordinator.inspect(tree_run_id))
                first_page = await client.store.read(tree_run_id, limit=1)
                first_event = first_page.events[0] if first_page.events else None
                creation_order[tree_run_id] = (
                    str(first_event.occurred_at) if first_event is not None else "",
                    f"{index:08d}",
                )
            except Exception:
                # A partially-created child is recoverable from its event log;
                # it must not make the root conversation disappear from the UI.
                continue
        root_view = next((item for item in views if item.run_id == root_run_id), None)
        if root_view is None:
            raise ValueError("会话不存在或运行状态不可读")

        def sort_value(view: Any) -> tuple[str, str]:
            occurred_at, fallback = creation_order.get(view.run_id, ("", view.run_id))
            # ISO-8601 UTC timestamps sort lexicographically. Keep the return
            # shape numeric/string-free for callers that only need a stable
            # max key, while preserving event order across child Runs.
            return (occurred_at, fallback)

        active = [item for item in views if item.status in _ACTIVE_STATUS_PRIORITY]
        if active:
            effective = max(
                active,
                key=lambda item: (
                    _ACTIVE_STATUS_PRIORITY[item.status],
                    *sort_value(item),
                    item.sequence,
                ),
            )
        else:
            effective = max(views, key=lambda item: (*sort_value(item), item.sequence))
        head = max(views, key=lambda item: (*sort_value(item), item.sequence))
        approvals = tuple(
            (view.run_id, approval)
            for view in views
            for approval in view.projection.approvals
            # An approval belongs to the lifecycle of its owning Run.  Once
            # that Run has crossed a terminal boundary (for example the user
            # pressed Stop while the card was visible), the immutable
            # ApprovalRequested fact remains in the event log for audit but
            # must not be presented as actionable UI state.  Otherwise the
            # browser can submit ApprovalRejected/ApprovalGranted against a
            # cancelled Run and receive the misleading "invalid from
            # cancelled" transition error.
            if view.status is RunStatus.AWAITING_APPROVAL
            and approval.status.value == "requested"
        )
        return root_view, effective, head, approvals, tuple(views)

    async def _client_for_run(self, run_id: str, *, start: bool = False) -> DurableRuntimeClient:
        """Resolve a root run across the grouped project history."""

        for root in await self._project_roots():
            try:
                client = await self._client_for_root(root)
                record = await client.store.load_run_record(run_id)
            except Exception:
                continue
            if record.parent_run_id is not None:
                raise ValueError("只能从根会话发送消息")
            self.selected_root = root
            # Event replay/session inspection can be the first request after a
            # browser refresh (the client may still have an EventSource open),
            # so project selection is not guaranteed to pass through
            # ``select_project``.  Reattach any queued durable work here too;
            # otherwise an already queued follow-up would wait until another
            # user message happened to start a supervisor.
            if not start:
                await self._start_pending_if_configured(root)
            return await self._client_for_root(root, start=start)
        # Keep a missing/stale browser id distinguishable from malformed
        # control requests.  The HTTP layer maps this durable lookup result
        # to 404 so the frontend can clear its selection instead of polling a
        # dead session indefinitely.
        raise RunNotFound(run_id)

    async def _validate_root_run(self, run_id: str) -> DurableRuntimeClient:
        return await self._client_for_run(run_id)

    async def send_message(
        self,
        text: str,
        *,
        session_id: str | None = None,
        project_root: str | None = None,
        interaction_mode: str = "auto",
    ) -> dict[str, Any]:
        if not text.strip():
            raise ValueError("消息不能为空")
        if session_id:
            client = await self._client_for_run(session_id, start=True)
            _root_view, _effective, head, approvals, _tree = await self._conversation_snapshot(client, session_id)
            if approvals:
                raise ValueError("当前会话有待处理审批，请先在运行面板中允许或拒绝该动作")
            # A plain assistant answer is intentionally not a successful coding
            # completion, so the worker may end at ``stalled``. Continue the
            # latest conversation head in-place in that case. When a child is
            # active, queue behind that child instead of resuming the stalled
            # root and accidentally running two turns concurrently.
            if head.status in {
                RunStatus.STALLED,
                RunStatus.CANCELLED,
                RunStatus.BLOCKED,
                RunStatus.FAILED_RECOVERABLE,
            }:
                receipt = await client.coordinator.resume(head.run_id, text.strip())
                await self._wake_supervisor(client)
                return {
                    "session_id": session_id,
                    "run_id": session_id,
                    "follow_up_run_id": None,
                    "continuation": "same_run",
                    "sequence": receipt.sequence,
                    "message_event_id": f"{head.run_id}:{receipt.sequence}",
                    "status": "queued",
                }
            receipt = await client.coordinator.send(head.run_id, text.strip())
            await self._wake_supervisor(client)
            return {
                "session_id": session_id,
                "run_id": session_id,
                "follow_up_run_id": receipt.follow_up_run_id,
                "continuation": "child_run",
                "sequence": receipt.sequence,
                "message_event_id": f"{head.run_id}:{receipt.sequence}",
                "status": "queued",
            }
        # A browser can have several project/session requests in flight.  The
        # legacy global ``selected_root`` is presentation state and may be
        # changed by a stale session refresh between the project picker and
        # this request.  New work therefore carries its project explicitly;
        # resolving that path here prevents a task from being created in an
        # unrelated historical project.  The selected root is intentionally
        # left untouched so another browser request cannot retarget this send.
        if project_root:
            try:
                requested_root = Path(project_root).expanduser().resolve(strict=True)
            except OSError as exc:
                raise ValueError(f"项目文件夹不可用: {exc}") from exc
            if not requested_root.is_dir():
                raise ValueError("项目路径必须是文件夹")
            client = await self._client_for_root(requested_root, start=True)
        else:
            client = await self._client(start=True)
        run_id = await client.submit(
            text.strip(),
            interaction_mode=classify_interaction_mode(text, interaction_mode),
        )
        await self._wake_supervisor(client)
        # ``RunCreated`` is the first durable root event and is therefore a
        # stable acknowledgement target for the optimistic WebUI message.
        return {
            "session_id": run_id,
            "run_id": run_id,
            "status": "queued",
            "sequence": 0,
            "message_event_id": f"{run_id}:1",
        }

    async def session(self, run_id: str) -> dict[str, Any]:
        client = await self._validate_root_run(run_id)
        root_view, effective, _head, approvals, tree = await self._conversation_snapshot(client, run_id)
        usage = await client.usage_for_run(run_id)
        context = await self.context(run_id, client=client, usage=usage)
        projection = _redact(root_view.projection.to_dict())
        # ApprovalProjection carries its owning run_id. Add descendant
        # approvals to the root-facing projection so the browser can render
        # one actionable approval card without exposing hidden tool arguments.
        projection["approvals"] = [
            _redact(approval.to_dict())
            for _owner_run_id, approval in approvals
        ]
        projection["effective_run_id"] = effective.run_id
        return {
            "run_id": run_id,
            "project_root": str(client.cwd),
            "status": effective.status.value,
            "sequence": max((item.sequence for item in tree), default=effective.sequence),
            "projection": projection,
            "usage": _redact(usage),
            "context": context,
            "capabilities": context.get("capabilities", {}),
            "executor": client.executor_status(),
            "scheduler": self._scheduler_status(client),
        }

    async def context(self, run_id: str, *, client: DurableRuntimeClient | None = None, usage: dict[str, Any] | None = None) -> dict[str, Any]:
        client = client or await self._validate_root_run(run_id)
        usage = usage or await client.usage_for_run(run_id)
        invocations = usage.get("invocations") or []
        latest = invocations[-1] if invocations else {}
        values = await self.settings.effective(client.cwd)
        environ = {
            **os.environ,
            "OPENAI_MODEL": values.get("model", "") or os.environ.get("OPENAI_MODEL", ""),
        }
        try:
            config = load_context_config(model=values.get("model") or usage.get("model"), environ=environ)
            window = config.context_window
            source = config.context_window_source
        except (ConfigError, ValueError):
            window = None
            source = "unavailable"
        used = latest.get("input_tokens") if isinstance(latest, Mapping) else None
        used = int(used) if isinstance(used, (int, float)) and used >= 0 else None
        ratio = (used / window) if used is not None and window else None
        categories = (
            latest.get("context_categories")
            if isinstance(latest, Mapping)
            else usage.get("context_categories")
        )
        if isinstance(categories, Mapping):
            safe_categories: dict[str, int] = {}
            allowed_categories = {
                "user_input",
                "tool_calls",
                "llm_output",
                "system_prompt",
                "summary",
                "tool_definitions",
            }
            for name, value in categories.items():
                name = str(name)
                if name not in allowed_categories:
                    continue
                try:
                    safe_categories[name] = max(0, int(value or 0))
                except (TypeError, ValueError):
                    safe_categories[name] = 0
            categories = safe_categories
        else:
            categories = None
        # Context/memory/safety activation is durable evidence, not a browser
        # guess.  Include the latest compaction event so the UI can explain
        # why the provider's most recent request may look small after a
        # summary, instead of displaying only the last request's buckets.
        compaction: dict[str, Any] | None = None
        try:
            event_rows: list[Any] = []
            for tree_run_id in await client.run_tree(run_id):
                page = await client.store.read(tree_run_id, limit=100_000)
                event_rows.extend(page.events)
            event_rows.sort(key=lambda event: (event.occurred_at, event.run_id, event.sequence))
            latest_projection: dict[str, Any] | None = None
            latest_applied: dict[str, Any] | None = None
            for event in event_rows:
                event_type = str(event.event_type)
                if event_type == "ContextProjectionBuilt":
                    payload = event.payload if isinstance(event.payload, Mapping) else {}
                    raw = payload.get("compaction")
                    if isinstance(raw, Mapping):
                        candidate = {
                            "tier": str(raw.get("tier") or "none"),
                            "before_tokens": raw.get("before_tokens"),
                            "after_tokens": raw.get("after_tokens"),
                            "ratio_before": raw.get("ratio_before"),
                            "ratio_after": raw.get("ratio_after"),
                            "summarized": bool(raw.get("summarized")),
                            "error": raw.get("error"),
                            "effective_context_window": payload.get("effective_context_window"),
                            "provider_safety_factor": payload.get("provider_safety_factor"),
                            "applied": bool(raw.get("applied"))
                            if "applied" in raw
                            else bool(
                                str(raw.get("tier") or "none") != "none"
                                or raw.get("summarized")
                            ),
                        }
                        latest_projection = candidate
                        # Keep an applied compaction visible even when a later
                        # request fits and reports tier=none.  Otherwise a
                        # long conversation appears to have "never" been
                        # compressed simply because its newest turn is small.
                        if candidate["applied"] or candidate.get("error"):
                            latest_applied = candidate
                elif event_type == "ContextCompacted":
                    payload = event.payload if isinstance(event.payload, Mapping) else {}
                    tier = str(payload.get("tier") or "none")
                    latest_applied = {
                        "tier": tier,
                        "before_tokens": payload.get("before_tokens"),
                        "after_tokens": payload.get("after_tokens"),
                        "ratio_before": payload.get("ratio_before"),
                        "ratio_after": payload.get("ratio_after"),
                        "summarized": bool(payload.get("summarized")),
                        "error": payload.get("error"),
                        "effective_context_window": payload.get("effective_context_window"),
                        "provider_safety_factor": payload.get("provider_safety_factor"),
                        "applied": bool(payload.get("applied"))
                        if "applied" in payload
                        else bool(tier != "none" or payload.get("summarized")),
                    }
            compaction = latest_applied or latest_projection
        except Exception:
            compaction = None
        activation = client.capability_status()
        activation_caps = activation.get("capabilities") if isinstance(activation, Mapping) else {}
        context_activation = (
            activation_caps.get("context")
            if isinstance(activation_caps, Mapping)
            else None
        )
        context_activation_details = (
            context_activation.get("details")
            if isinstance(context_activation, Mapping)
            else None
        )
        effective_window = None
        if isinstance(compaction, Mapping):
            raw_effective_window = compaction.get("effective_context_window")
            if isinstance(raw_effective_window, (int, float)) and raw_effective_window > 0:
                effective_window = int(raw_effective_window)
        if effective_window is None and isinstance(context_activation_details, Mapping):
            raw_effective_window = context_activation_details.get("preflight_window")
            if isinstance(raw_effective_window, (int, float)) and raw_effective_window > 0:
                effective_window = int(raw_effective_window)
        preflight_ratio = (
            used / effective_window
            if used is not None and effective_window
            else None
        )
        capabilities: dict[str, Any] = {}
        if isinstance(activation_caps, Mapping):
            for name in ("context", "memory", "safety"):
                state = activation_caps.get(name)
                if isinstance(state, Mapping):
                    raw_details = state.get("details") or {}
                    public_details = _public_capability_details(
                        activation_caps,
                        name,
                        raw_details,
                    )
                    # ``SharedCapabilityServices`` is initialized for every
                    # run, even when an optional capability is deliberately
                    # disabled.  The browser-facing contract must expose the
                    # effective switch, otherwise MEMORY_ENABLED=false still
                    # renders as an enabled memory service in the status UI.
                    enabled = bool(state.get("enabled"))
                    if name == "memory":
                        configured = public_details.get("configured_enabled")
                        if configured is False:
                            enabled = False
                    capabilities[name] = {
                        "enabled": enabled,
                        "initialized": bool(state.get("initialized")),
                        "triggered": bool(state.get("triggered")),
                        "degraded_reason": state.get("degraded_reason"),
                        "details": public_details,
                    }
        return {
            "used_tokens": used,
            "window_tokens": window,
            "ratio": ratio,
            "effective_window_tokens": effective_window,
            "preflight_ratio": preflight_ratio,
            "source": source,
            "latest_invocation_id": latest.get("invocation_id") if isinstance(latest, Mapping) else None,
            "categories": categories,
            "compaction": compaction,
            "capabilities": capabilities,
            "note": "实际最近一次模型请求；分类为 Runtime 本地 tokenizer 明细，计费以 provider usage 为准",
        }

    async def timeline(self, run_id: str, *, limit: int = 500) -> list[dict[str, Any]]:
        client = await self._validate_root_run(run_id)
        run_ids = await client.run_tree(run_id)
        events: list[Any] = []
        for item in run_ids:
            page = await client.store.read(item, limit=min(100_000, max(1, limit)))
            events.extend(page.events)
        events.sort(key=lambda event: (event.occurred_at, event.run_id, event.sequence))
        return _public_events(client, events[-limit:], root_run_id=run_id)

    async def root_events(self, run_id: str, *, after: int = 0, limit: int = 200) -> tuple[list[dict[str, Any]], int]:
        client = await self._validate_root_run(run_id)
        page = await client.store.read(run_id, after=max(0, after), limit=min(1000, max(1, limit)))
        return _public_events(client, page.events, root_run_id=run_id), (page.events[-1].sequence if page.events else after)

    async def tree_events(
        self,
        run_id: str,
        *,
        cursors: Mapping[str, int] | None = None,
        limit: int = 1000,
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Read new events from the root and every persisted descendant.

        A follow-up is represented as a child Run.  Reading only the root log
        (the previous WebUI behavior) exposed the queue event but not the
        child's eventual assistant/tool events.  Each stream keeps a sequence
        cursor per Run so one child cannot hide another child's progress.
        """

        client = await self._validate_root_run(run_id)
        next_cursors = {str(key): max(0, int(value)) for key, value in (cursors or {}).items()}
        events: list[Any] = []
        page_limit = min(1000, max(1, limit))
        for tree_run_id in await client.run_tree(run_id):
            after = next_cursors.get(tree_run_id, 0)
            page = await client.store.read(tree_run_id, after=after, limit=page_limit)
            if page.events:
                events.extend(page.events)
                next_cursors[tree_run_id] = page.events[-1].sequence
            else:
                next_cursors.setdefault(tree_run_id, after)
        events.sort(key=lambda event: (event.occurred_at, event.run_id, event.sequence))
        return _public_events(client, events, root_run_id=run_id), next_cursors

    async def stop(self, run_id: str) -> None:
        client = await self._validate_root_run(run_id)
        await client.terminate_run_tree(run_id, reason="WebUI 用户停止", stop_supervisor=False)

    async def delete_session(self, run_id: str) -> dict[str, Any]:
        """Remove a conversation from the WebUI without destroying its facts.

        Deletion is intentionally a durable tombstone.  If a run is still
        queued/running, request cancellation for its complete tree first so
        the scheduler cannot claim it after the row is hidden.  The immutable
        event/snapshot records remain available to future audit tooling while
        normal listings and control endpoints treat the conversation as gone.
        """

        client = await self._client_for_run(run_id)
        _root_view, _effective, _head, _approvals, tree = await self._conversation_snapshot(client, run_id)
        tree_views = []
        for tree_run_id in (item.run_id for item in tree):
            with contextlib.suppress(Exception):
                tree_views.append(await client.coordinator.inspect(tree_run_id))
        if any(view.status in _ACTIVE_STATUSES for view in tree_views):
            await client.terminate_run_tree(
                run_id,
                reason="WebUI 删除会话",
                grace_seconds=0.2,
                stop_supervisor=False,
            )
            # Do not create a tombstone while a worker may still own an action
            # lease.  The worker must first persist its safe cancellation
            # boundary (including ActionOutcomeUnknown when needed); otherwise
            # hiding the run would make that final fact impossible to append
            # and could leave a subprocess running after the user thinks the
            # session was deleted.  A bounded wait keeps the endpoint
            # responsive and asks the user to retry if a non-cooperative
            # provider exceeds the grace period.
            deadline = asyncio.get_running_loop().time() + 8.0
            while True:
                pending: list[str] = []
                for tree_run_id in (item.run_id for item in tree):
                    try:
                        view = await client.coordinator.inspect(tree_run_id)
                    except Exception:
                        # A transient projection read failure is not proof of
                        # safety; keep the run visible and let the caller
                        # retry after the supervisor has repaired the cursor.
                        pending.append(tree_run_id)
                        continue
                    if view.status in _ACTIVE_STATUSES:
                        pending.append(tree_run_id)
                if not pending:
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    raise SessionDeleteConflict(
                        "会话正在安全停止，请稍后重试删除；运行证据仍保留"
                    )
                await asyncio.sleep(0.05)
        hidden = await client.store.tombstone_run_tree(run_id)
        return {
            "run_id": run_id,
            "deleted": True,
            "hidden_run_count": len(hidden),
            "audit_retained": True,
        }

    async def resume(self, run_id: str) -> None:
        # Resuming may need to create a supervisor.  Use the same explicit
        # WebUI settings path as a new message so a resume never falls back to
        # process-wide environment values (and never starts with a different
        # model or credentials than the user selected in Settings).
        client = await self._client_for_run(run_id, start=True)
        _root_view, _effective, head, approvals, _tree = await self._conversation_snapshot(client, run_id)
        if approvals:
            raise ValueError("当前会话有待处理审批，请先在运行面板中允许或拒绝该动作")
        if head.status not in _RECOVERABLE_STATUSES:
            raise ValueError(f"当前状态不可恢复: {head.status.value}")
        # ``_client_for_run(start=True)`` already attempted to attach a local
        # supervisor.  If another process owns the project lease, continue
        # remains a durable control-plane write and must not retry leadership
        # a second time in the same request.
        await client.continue_run(
            head.run_id,
            reason="WebUI 用户继续",
        )

    async def _wake_supervisor(self, client: DurableRuntimeClient) -> None:
        """Wake the project scheduler after a durable control decision."""

        supervisor = client.supervisor
        wake = getattr(supervisor, "wake", None) if supervisor is not None else None
        if callable(wake):
            wake()

    @staticmethod
    def _approval_in_tree(
        tree: tuple[Any, ...],
        approval_id: str,
    ) -> tuple[str, Any, Any] | None:
        """Find an approval in the authoritative tree, including decided ones.

        ``_conversation_snapshot`` intentionally returns only requested
        approvals for the composer.  Decision endpoints must also inspect
        terminal approvals so a stale browser click can be answered
        idempotently instead of being reported as an unknown approval.
        """

        for view in tree:
            for approval in view.projection.approvals:
                if approval.approval_id == approval_id:
                    return view.run_id, approval, view
        return None

    @staticmethod
    def _approval_result(
        effective: Any,
        tree: tuple[Any, ...],
        decision: str,
        *,
        idempotent: bool = False,
    ) -> dict[str, Any]:
        return {
            "decision": decision,
            "status": effective.status.value,
            "sequence": max((item.sequence for item in tree), default=effective.sequence),
            "continuation": "queued" if effective.status in _ACTIVE_STATUSES else effective.status.value,
            "idempotent": idempotent,
        }

    async def approve(self, run_id: str, approval_id: str, digest: str) -> dict[str, Any]:
        # Resolve the card before starting provider resources.  A stale card
        # should return immediately after a Stop/other-tab decision instead
        # of blocking while a fresh supervisor initializes.  Only an
        # actually requested approval needs the detached worker restarted.
        client = await self._client_for_run(run_id)
        _root_view, effective, _head, _approvals, tree = await self._conversation_snapshot(client, run_id)
        match = self._approval_in_tree(tree, approval_id)
        if match is None:
            raise ApprovalStaleError(approval_id)
        owner, approval, _owner_view = match
        if approval.status.value != "requested":
            # A second click, another browser tab, or a stop may have already
            # decided the action.  Return the durable decision without
            # appending a second event.
            return self._approval_result(effective, tree, approval.status.value, idempotent=True)
        # Approval may be the first interaction after a WebUI restart. Start
        # the detached supervisor before granting the child action so the
        # worker can consume the durable decision immediately, then re-read
        # the projection because startup can race a cancellation/decision.
        client = await self._client_for_run(run_id, start=True)
        _root_view, effective, _head, _approvals, tree = await self._conversation_snapshot(client, run_id)
        match = self._approval_in_tree(tree, approval_id)
        if match is None:
            raise ApprovalStaleError(approval_id)
        owner, approval, _owner_view = match
        if approval.status.value != "requested":
            return self._approval_result(effective, tree, approval.status.value, idempotent=True)
        try:
            decision = await client.coordinator.approve(
                run_id=owner,
                approval_id=approval_id,
                action_args_digest=digest,
            )
        except (ApprovalNotFoundError, SequenceConflict):
            # Resolve a cross-process race against the fresh projection.  If
            # another actor won, expose its terminal decision as a successful
            # no-op; otherwise report a recoverable stale-card conflict.
            _root_view, effective, _head, _approvals, tree = await self._conversation_snapshot(client, run_id)
            latest = self._approval_in_tree(tree, approval_id)
            if latest is None:
                raise ApprovalStaleError(approval_id) from None
            owner, approval, _owner_view = latest
            if approval.status.value == "requested":
                # The projection is still actionable but another durable
                # event kept changing its sequence.  Ask the browser to
                # refresh and retry instead of leaking a store race as 500.
                raise ApprovalStaleError(approval_id) from None
            return self._approval_result(effective, tree, approval.status.value, idempotent=True)
        except ApprovalDigestMismatchError:
            raise ValueError("审批参数已变化，请刷新会话后重新确认") from None
        await self._wake_supervisor(client)
        _root_view, effective, _head, _approvals, tree = await self._conversation_snapshot(client, run_id)
        return self._approval_result(effective, tree, decision.status)

    async def reject(self, run_id: str, approval_id: str, reason: str) -> dict[str, Any]:
        # As with approve(), inspect the durable card before doing potentially
        # expensive provider/supervisor startup so a stale click is fast and
        # deterministic.
        client = await self._client_for_run(run_id)
        _root_view, effective, _head, _approvals, tree = await self._conversation_snapshot(client, run_id)
        match = self._approval_in_tree(tree, approval_id)
        if match is None:
            raise ApprovalStaleError(approval_id)
        owner, approval, _owner_view = match
        if approval.status.value != "requested":
            return self._approval_result(effective, tree, approval.status.value, idempotent=True)
        client = await self._client_for_run(run_id, start=True)
        _root_view, effective, _head, _approvals, tree = await self._conversation_snapshot(client, run_id)
        match = self._approval_in_tree(tree, approval_id)
        if match is None:
            raise ApprovalStaleError(approval_id)
        owner, approval, _owner_view = match
        if approval.status.value != "requested":
            return self._approval_result(effective, tree, approval.status.value, idempotent=True)
        try:
            decision = await client.coordinator.reject(
                run_id=owner,
                approval_id=approval_id,
                reason=reason or "WebUI 用户拒绝",
            )
        except (ApprovalNotFoundError, SequenceConflict):
            _root_view, effective, _head, _approvals, tree = await self._conversation_snapshot(client, run_id)
            latest = self._approval_in_tree(tree, approval_id)
            if latest is None:
                raise ApprovalStaleError(approval_id) from None
            owner, approval, _owner_view = latest
            if approval.status.value == "requested":
                raise ApprovalStaleError(approval_id) from None
            return self._approval_result(effective, tree, approval.status.value, idempotent=True)
        # ApprovalRejected moves only this action back to the queue.  Wake the
        # scheduler rather than waiting for its next polling interval, so the
        # frontend can observe RunClaimed/ToolObservationCommitted promptly.
        await self._wake_supervisor(client)
        _root_view, effective, _head, _approvals, tree = await self._conversation_snapshot(client, run_id)
        return self._approval_result(effective, tree, decision.status)

    async def shutdown(self) -> None:
        retry_tasks = tuple(self._supervisor_retry_tasks.values())
        self._supervisor_retry_tasks.clear()
        for task in retry_tasks:
            task.cancel()
        if retry_tasks:
            await asyncio.gather(*retry_tasks, return_exceptions=True)
        clients = tuple(self._clients.values())
        for client in clients:
            with contextlib.suppress(Exception):
                records = await client.store.list_runs()
                for record in records:
                    if record.parent_run_id is not None:
                        continue
                    view = await client.coordinator.inspect(record.run_id)
                    if view.status in _ACTIVE_STATUSES:
                        await client.terminate_run_tree(record.run_id, reason="WebUI 关闭", stop_supervisor=False)
        for client in clients:
            with contextlib.suppress(Exception):
                await client.close()
        self._clients.clear()
        self._client_fingerprints.clear()
        await self.live_stream.close()


class ProjectSelectRequest(BaseModel):
    path: str | None = None
    picker: bool = False


class MessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=200_000)
    session_id: str | None = None
    # Explicit project binding for new sessions.  This avoids routing a send
    # through the manager's mutable UI selection when multiple project
    # histories are being refreshed concurrently.
    project_root: str | None = None
    # ``auto`` is intentionally conservative; callers can opt into the
    # explicit conversation contract for a tool-free turn.
    interaction_mode: str = "auto"


class SettingsRequest(BaseModel):
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    permission_mode: str | None = None
    project_scoped: bool = False


class ApprovalRequest(BaseModel):
    action_args_digest: str = ""


class RejectRequest(BaseModel):
    reason: str = "WebUI 用户拒绝"


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ConfigError):
        return HTTPException(status_code=400, detail={"code": "configuration_required", "message": str(exc)})
    if isinstance(exc, RunNotFound):
        return HTTPException(status_code=404, detail={"code": "session_not_found", "message": "会话不存在或已被清理"})
    if isinstance(exc, SessionDeleteConflict):
        return HTTPException(status_code=409, detail={"code": "session_delete_pending", "message": str(exc)})
    if isinstance(exc, ApprovalStaleError):
        return HTTPException(status_code=409, detail={"code": "approval_stale", "message": str(exc)})
    if isinstance(exc, ApprovalDigestMismatchError):
        return HTTPException(status_code=409, detail={"code": "approval_digest_mismatch", "message": str(exc)})
    if isinstance(exc, (ValueError, KeyError)):
        return HTTPException(status_code=400, detail={"code": "invalid_request", "message": str(exc)})
    # Preserve a concise, sanitized diagnostic for local setup failures (for
    # example Docker/OpenSandbox not installed).  The browser needs the actual
    # remediation hint, while secret-shaped values are still redacted.
    error_name = type(exc).__name__
    detail = _redact(f"{error_name}: {exc}")
    if error_name in {"SandboxUnavailableError", "SandboxStartupError"}:
        return HTTPException(status_code=503, detail={"code": "environment_not_ready", "message": detail})
    return HTTPException(status_code=500, detail={"code": "runtime_error", "message": detail})


def _static_root() -> Path | None:
    candidates = (
        Path(__file__).with_name("web_assets"),
        Path(__file__).resolve().parent.parent / "web" / "dist",
    )
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return None


def create_web_app(manager: WebRuntimeManager | None = None) -> FastAPI:
    manager = manager or WebRuntimeManager(initial_cwd=Path.cwd())

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await manager.shutdown()

    app = FastAPI(title="cc-harness WebUI", version="0.1.0", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.manager = manager

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "service": "cc-harness-webui", "runtime": "durable", "time": time.time()}

    @app.get("/api/bootstrap")
    async def bootstrap(include_sessions: bool = True) -> dict[str, Any]:
        project = manager.project_key
        settings = await manager.settings.public(manager.selected_root)
        # Keep durable history discoverable even before a workspace is picked.
        # The project gate still prevents sending work, while the grouped
        # sidebar lets a user choose a previously opened project/session.
        # The browser requests ``include_sessions=false`` for the first paint;
        # scanning every known project replays each run tree and should not
        # block the shell. The historical/default contract still includes the
        # complete list for API consumers that need a single bootstrap call.
        sessions = await manager.sessions(all_projects=True) if include_sessions else []
        return {
            "service": "cc-harness-webui",
            "runtime": "durable",
            "project": {"root": project} if project else None,
            "settings": settings,
            "sessions": sessions,
            "initial_prompt": manager.initial_prompt,
        }

    @app.get("/api/settings")
    async def get_settings(reveal: bool = False) -> dict[str, Any]:
        return await manager.settings.public(manager.selected_root, reveal=reveal)

    @app.post("/api/settings")
    async def save_settings(payload: SettingsRequest) -> dict[str, Any]:
        if payload.project_scoped and manager.selected_root is None:
            raise HTTPException(status_code=409, detail={"code": "project_required", "message": "选择项目后才能保存项目级配置"})
        root = manager.selected_root if payload.project_scoped else None
        values = {field: getattr(payload, field) for field in _MODEL_FIELDS}
        if payload.permission_mode is not None:
            try:
                values[_PERMISSION_FIELD] = normalize_permission_mode(payload.permission_mode)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "invalid_permission_mode", "message": str(exc)},
                ) from exc
        if payload.base_url is not None:
            parsed = urlparse(payload.base_url.strip())
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise HTTPException(status_code=400, detail={"code": "invalid_base_url", "message": "Base URL 必须是 http(s) 地址"})
        try:
            await manager.settings.update(values, project_root=root)
        except Exception as exc:
            raise _http_error(exc) from exc
        return {"saved": True, "settings": await manager.settings.public(manager.selected_root)}

    @app.post("/api/settings/test")
    async def test_settings(payload: SettingsRequest) -> dict[str, Any]:
        values = await manager.settings.effective(manager.selected_root)
        for field in _MODEL_FIELDS:
            value = getattr(payload, field)
            if value is not None and value.strip():
                values[field] = value.strip()
        if not values["base_url"] or not values["api_key"]:
            raise HTTPException(status_code=400, detail={"code": "configuration_required", "message": "测试连接需要 Base URL 和 API key"})
        parsed = urlparse(values["base_url"])
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise HTTPException(status_code=400, detail={"code": "invalid_base_url", "message": "Base URL 必须是 http(s) 地址"})

        def request_models() -> tuple[int, int]:
            import requests

            response = requests.get(
                values["base_url"].rstrip("/") + "/models",
                headers={"Authorization": f"Bearer {values['api_key']}"},
                timeout=10,
            )
            response.raise_for_status()
            body = response.json()
            models = body.get("data", []) if isinstance(body, dict) else []
            return response.status_code, len(models) if isinstance(models, list) else 0

        try:
            status, model_count = await asyncio.to_thread(request_models)
        except Exception as exc:  # network/provider error is safe to show, key is never included
            return {"ok": False, "message": f"连接失败: {type(exc).__name__}: {exc}"}
        return {"ok": True, "status": status, "model_count": model_count, "message": "连接成功"}

    @app.post("/api/projects/select")
    async def select_project(payload: ProjectSelectRequest) -> dict[str, Any]:
        try:
            root = await (manager.pick_project() if payload.picker and not payload.path else manager.select_project(payload.path or ""))
        except Exception as exc:
            raise _http_error(exc) from exc
        if root is None:
            return {"selected": False, "project": None}
        return {
            "selected": True,
            "project": {"root": str(root)},
            "settings": await manager.settings.public(root),
            "sessions": await manager.sessions(),
        }

    @app.get("/api/sessions")
    async def list_sessions() -> dict[str, Any]:
        try:
            return {
                "project_required": manager.selected_root is None,
                "sessions": await manager.sessions(all_projects=True),
            }
        except Exception as exc:
            raise _http_error(exc) from exc

    async def send_message(payload: MessageRequest) -> dict[str, Any]:
        if manager.selected_root is None and not payload.project_root:
            raise HTTPException(status_code=409, detail={"code": "project_required", "message": "请先选择本地项目文件夹"})
        try:
            return await manager.send_message(
                payload.text,
                session_id=payload.session_id,
                project_root=payload.project_root,
                interaction_mode=payload.interaction_mode,
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/api/messages")
    async def post_message(payload: MessageRequest) -> dict[str, Any]:
        return await send_message(payload)

    @app.post("/api/sessions")
    async def create_session(payload: MessageRequest) -> dict[str, Any]:
        return await send_message(payload.model_copy(update={"session_id": None}))

    @app.post("/api/sessions/{run_id}/messages")
    async def post_session_message(run_id: str, payload: MessageRequest) -> dict[str, Any]:
        return await send_message(payload.model_copy(update={"session_id": run_id}))

    @app.get("/api/sessions/{run_id}")
    async def get_session(run_id: str) -> dict[str, Any]:
        try:
            return await manager.session(run_id)
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/sessions/{run_id}/timeline")
    async def get_timeline(run_id: str, limit: int = 500) -> dict[str, Any]:
        try:
            return {"events": await manager.timeline(run_id, limit=max(1, min(limit, 2000)))}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/sessions/{run_id}/context")
    async def get_context(run_id: str) -> dict[str, Any]:
        try:
            return await manager.context(run_id)
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/sessions/{run_id}/events")
    async def stream_events(run_id: str, request: Request, after: int = 0) -> StreamingResponse:
        try:
            client = await manager._validate_root_run(run_id)
        except Exception as exc:
            raise _http_error(exc) from exc
        header_cursor = request.headers.get("last-event-id")
        try:
            if header_cursor and header_cursor.isdigit():
                cursor = int(header_cursor)
            elif header_cursor and header_cursor.startswith(f"{run_id}:"):
                cursor = int(header_cursor.rsplit(":", 1)[-1])
            else:
                cursor = max(0, after)
        except ValueError:
            cursor = max(0, after)

        async def events() -> AsyncIterator[str]:
            cursors: dict[str, int] = {run_id: cursor}
            # Register before the first Durable read so a chunk emitted while
            # the browser is loading its timeline cannot be lost.  The hub is
            # best-effort; a detached supervisor in another process simply
            # produces no live messages and is still covered by polling.
            async with manager.live_stream.subscription() as live_queue:
                tree_ids = set(await client.run_tree(run_id))
                last_tree_refresh = time.monotonic()
                while not await request.is_disconnected():
                    try:
                        items, cursors = await manager.tree_events(run_id, cursors=cursors)
                    except Exception:
                        break
                    for item in items:
                        # Keep root IDs numeric for old clients; child IDs are
                        # composite but still stable and are de-duplicated by
                        # the browser using the public event envelope.
                        stream_id = item["sequence"] if item["run_id"] == run_id else item["id"]
                        yield f"id: {stream_id}\nevent: runtime\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"

                    # Child Runs can be created after the stream starts.  A
                    # small refresh window lets their live chunks enter the
                    # same conversation without querying SQLite per token.
                    if time.monotonic() - last_tree_refresh >= 1.0:
                        with contextlib.suppress(Exception):
                            tree_ids = set(await client.run_tree(run_id))
                        last_tree_refresh = time.monotonic()

                    wait_timeout = 0.05 if items else 0.5
                    try:
                        live = await asyncio.wait_for(live_queue.get(), timeout=wait_timeout)
                    except asyncio.TimeoutError:
                        if not items:
                            yield ": heartbeat\n\n"
                        continue
                    if str(live.get("run_id") or "") not in tree_ids:
                        continue
                    live_id = live.get("live_id") or f"live-{time.time_ns()}"
                    live_payload = {**live, "root_run_id": run_id}
                    yield f"id: live:{live_id}\nevent: stream\ndata: {json.dumps(live_payload, ensure_ascii=False)}\n\n"
                    # Drain a small burst without blocking the durable poll.
                    for _ in range(32):
                        try:
                            live = live_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        if str(live.get("run_id") or "") not in tree_ids:
                            continue
                        live_id = live.get("live_id") or f"live-{time.time_ns()}"
                        live_payload = {**live, "root_run_id": run_id}
                        yield f"id: live:{live_id}\nevent: stream\ndata: {json.dumps(live_payload, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/sessions/{run_id}/stop")
    async def stop_session(run_id: str) -> dict[str, Any]:
        try:
            await manager.stop(run_id)
        except Exception as exc:
            raise _http_error(exc) from exc
        return {"stopped": True, "run_id": run_id}

    @app.delete("/api/sessions/{run_id}")
    async def delete_session(run_id: str) -> dict[str, Any]:
        try:
            return await manager.delete_session(run_id)
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/api/sessions/{run_id}/resume")
    async def resume_session(run_id: str) -> dict[str, Any]:
        try:
            await manager.resume(run_id)
        except Exception as exc:
            raise _http_error(exc) from exc
        return {"resumed": True, "run_id": run_id}

    @app.post("/api/sessions/{run_id}/approvals/{approval_id}/approve")
    async def approve(run_id: str, approval_id: str, payload: ApprovalRequest) -> dict[str, Any]:
        if not payload.action_args_digest:
            raise HTTPException(status_code=400, detail={"code": "digest_required", "message": "缺少 action_args_digest"})
        try:
            result = await manager.approve(run_id, approval_id, payload.action_args_digest)
        except Exception as exc:
            raise _http_error(exc) from exc
        return {"approved": True, "run_id": run_id, "approval_id": approval_id, **result}

    @app.post("/api/sessions/{run_id}/approvals/{approval_id}/reject")
    async def reject(run_id: str, approval_id: str, payload: RejectRequest) -> dict[str, Any]:
        try:
            result = await manager.reject(run_id, approval_id, payload.reason)
        except Exception as exc:
            raise _http_error(exc) from exc
        return {"rejected": True, "run_id": run_id, "approval_id": approval_id, **result}

    # The migrated DeepSeek-style client talks to a versioned Web contract.
    # Keep the historical ``/api`` routes above for TUI/headless clients, but
    # register the exact same handlers under one explicit compatibility
    # namespace.  Aliasing the handlers (instead of duplicating business
    # logic) guarantees that browser commands and legacy clients share the
    # same Durable Runtime, event cursor semantics, redaction, and approval
    # behavior.
    async def web_capabilities() -> dict[str, Any]:
        return {
            "version": "v1",
            "runtime": "durable",
            "transport": {"commands": "rest", "events": "sse", "reconnect": "cursor"},
            "features": {
                "projects": {"supported": True},
                "sessions": {
                    "supported": True,
                    "hierarchy": "project/session/run",
                    # Deletion is a reversible presentation action: the
                    # sidebar hides a tombstoned tree while immutable events
                    # and snapshots remain available to audit tooling.
                    "delete": "tombstone",
                },
                "approvals": {"supported": True, "reject_continues": True},
                "stop": {"supported": True, "scope": "run_tree"},
                "resume": {"supported": True, "mode": "natural_language_or_checkpoint"},
                "context": {"supported": True, "source": "runtime"},
                "memory": {"supported": True, "source": "runtime", "injection": "L3>L2>L1>L0"},
                "security": {"supported": True, "source": "runtime"},
            },
            "unsupported_controls": [],
        }

    # Explicit aliases are part of the public API contract.  FastAPI keeps the
    # original endpoint names and OpenAPI metadata intact for compatibility.
    versioned_routes = (
        ("/api/web/v1/health", health, ["GET"]),
        ("/api/web/v1/bootstrap", bootstrap, ["GET"]),
        ("/api/web/v1/capabilities", web_capabilities, ["GET"]),
        ("/api/web/v1/settings", get_settings, ["GET"]),
        ("/api/web/v1/settings", save_settings, ["POST"]),
        ("/api/web/v1/settings/test", test_settings, ["POST"]),
        ("/api/web/v1/projects/select", select_project, ["POST"]),
        ("/api/web/v1/sessions", list_sessions, ["GET"]),
        ("/api/web/v1/messages", post_message, ["POST"]),
        ("/api/web/v1/sessions", create_session, ["POST"]),
        ("/api/web/v1/sessions/{run_id}/messages", post_session_message, ["POST"]),
        ("/api/web/v1/sessions/{run_id}", get_session, ["GET"]),
        ("/api/web/v1/sessions/{run_id}/timeline", get_timeline, ["GET"]),
        ("/api/web/v1/sessions/{run_id}/context", get_context, ["GET"]),
        ("/api/web/v1/sessions/{run_id}/events", stream_events, ["GET"]),
        ("/api/web/v1/sessions/{run_id}", delete_session, ["DELETE"]),
        ("/api/web/v1/sessions/{run_id}/stop", stop_session, ["POST"]),
        ("/api/web/v1/sessions/{run_id}/resume", resume_session, ["POST"]),
        ("/api/web/v1/sessions/{run_id}/approvals/{approval_id}/approve", approve, ["POST"]),
        ("/api/web/v1/sessions/{run_id}/approvals/{approval_id}/reject", reject, ["POST"]),
    )
    for route_path, endpoint, methods in versioned_routes:
        app.add_api_route(route_path, endpoint, methods=methods, include_in_schema=True)

    static = _static_root()
    if static is not None:
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

        @app.get("/", response_class=FileResponse)
        async def index() -> FileResponse:
            return FileResponse(static / "index.html")

        @app.get("/{path:path}", response_class=FileResponse)
        async def spa_fallback(path: str) -> FileResponse:
            requested = (static / path).resolve()
            if requested.is_file() and static.resolve() in requested.parents:
                return FileResponse(requested)
            return FileResponse(static / "index.html")
    else:

        @app.get("/", response_class=HTMLResponse)
        async def missing_frontend() -> str:
            return "<h1>cc-harness WebUI</h1><p>前端资源尚未构建，请运行 npm run build。</p>"

    return app


def _find_available_port(host: str, requested: int) -> int:
    if requested < 0 or requested > 65535:
        raise ValueError("port must be between 0 and 65535")
    if requested == 0:
        return 0
    candidate = requested
    for _ in range(100):
        with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, candidate))
            except OSError:
                candidate += 1
                if candidate > 65535:
                    break
            else:
                return candidate
    raise OSError(f"没有可用端口（起始端口 {requested}）")


async def run_web_server(
    *,
    initial_cwd: Path,
    data_root: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 3080,
    no_open: bool = False,
    initial_prompt: str | None = None,
) -> int:
    """Run the foreground WebUI process until Ctrl+C or server shutdown."""

    try:
        import uvicorn
    except ImportError:  # pragma: no cover - dependency packaging guard
        print("缺少 WebUI 依赖，请安装 fastapi 和 uvicorn", file=os.sys.stderr)
        return 2
    selected_port = _find_available_port(host, port)
    manager = WebRuntimeManager(initial_cwd=initial_cwd, data_root=data_root, initial_prompt=initial_prompt)
    app = create_web_app(manager)
    display_host = "localhost" if host in {"127.0.0.1", "::1"} else host
    url_host = f"[{host}]" if ":" in host and host != "::1" else display_host
    url = f"http://{url_host}:{selected_port}/"
    print(f"cc-harness WebUI: {url}", flush=True)
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print("警告：WebUI 正在非回环地址监听；请确保网络边界和访问控制由你负责。", flush=True)
    if not no_open:
        with contextlib.suppress(Exception):
            webbrowser.open(url)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=selected_port, log_level="info"))
    try:
        await server.serve()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await manager.shutdown()
    return 0


__all__ = ["WebRuntimeManager", "WebSettingsStore", "create_web_app", "run_web_server"]
