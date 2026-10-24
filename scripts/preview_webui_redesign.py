"""Isolated local UI verification server with deterministic model/tool fixtures.

Uses the production WebUI, store, projections, worker, approvals and queue.
No remote model calls; all example files and runtime data live in a temp folder.
Run from the repository root: python -m scripts.preview_webui_redesign
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import uvicorn

from cc_harness.durable_runtime import DurableRuntimeClient
from cc_harness.lease import LeaseManager
from cc_harness.native_tools import MutationEngine
from cc_harness.run_events import EventActor
from cc_harness.run_kernel import ModelSegment, ReActKernel
from cc_harness.run_model import ActionStatus
from cc_harness.tool_observation import make_observation
from cc_harness.webui import WebRuntimeManager, WebSettingsStore, create_web_app
from cc_harness.worker import ActionExecutionResult, RunWorker


class VerificationManager(WebRuntimeManager):
    async def _start_pending_if_configured(self, root):
        return  # Explicit fixture worker execution only; never start a remote model.


async def main():
    base = Path(tempfile.mkdtemp(prefix="cc-harness-ui-verification-"))
    project = base / "atlas-workspace"
    (project / "src").mkdir(parents=True)
    target = project / "src" / "tasks.py"
    target.write_text(
        'def visible_tasks(tasks, status="all"):\n    return tasks\n', encoding="utf-8"
    )
    (project / "README.md").write_text(
        "# Atlas Workspace\n\n任务管理工具的本地验证项目。\n", encoding="utf-8"
    )
    (project / "tests").mkdir()
    (project / "tests" / "test_tasks.py").write_text(
        'import unittest\nfrom src.tasks import visible_tasks\n\nclass TaskFilterTests(unittest.TestCase):\n    def setUp(self):\n        self.tasks = [{"completed": True}, {"completed": False}]\n    def test_all(self):\n        self.assertEqual(visible_tasks(self.tasks), self.tasks)\n    def test_completed(self):\n        self.assertEqual(visible_tasks(self.tasks, "completed"), [self.tasks[0]])\n    def test_pending(self):\n        self.assertEqual(visible_tasks(self.tasks, "pending"), [self.tasks[1]])\n    def test_empty(self):\n        self.assertEqual(visible_tasks([], "pending"), [])\n',
        encoding="utf-8",
    )
    client = await DurableRuntimeClient.create(project, data_root=base / "runtime")
    manager = VerificationManager(initial_cwd=project, data_root=base / "runtime")
    manager.settings = WebSettingsStore(base / "settings.json")
    await manager.settings.update(
        {
            "base_url": "http://127.0.0.1:1/v1",
            "model": "deepseek-v4-flash",
            "api_key": "local-verification-only",
        },
        project_root=project,
    )
    manager.selected_root = project.resolve()
    manager._clients[str(project.resolve())] = client

    async def no_remote_supervisor(**_kwargs):
        return

    client.start_supervisor = no_remote_supervisor

    async def append(run_id, kind, payload):
        await client.coordinator._append(
            run_id, kind, payload, EventActor("client", "ui-verification")
        )

    run_id = await client.submit(
        "为任务列表增加状态筛选，保留现有行为，并验证全部、未完成与已完成三个选项。",
        ("状态筛选通过验证",),
    )
    for index, title in enumerate(
        (
            "检查现有任务列表与状态字段",
            "实现全部、未完成与已完成筛选",
            "运行测试，确认原有行为保持兼容",
        )
    ):
        await append(
            run_id,
            "TodoCreated",
            {
                "todo": {
                    "id": f"step-{index}",
                    "title": title,
                    "status": "pending",
                    "active_sessions": [run_id],
                }
            },
        )

    class LocalModel:
        calls = 0

        async def complete(self, _messages, _tools):
            self.calls += 1
            if self.calls == 1:
                return ModelSegment(
                    text="我会先检查现有筛选逻辑，再补齐三种状态的行为，最后运行验证。",
                    tool_calls=(
                        {"id": "read-tasks", "name": "Read", "arguments": {"path": "src/tasks.py"}},
                    ),
                )
            if self.calls == 2:
                return ModelSegment(
                    text="当前列表直接返回全部任务。我会保持默认行为，并为已完成和未完成状态增加明确的筛选条件。",
                    tool_calls=(
                        {
                            "id": "edit-tasks",
                            "name": "Write",
                            "arguments": {"path": "src/tasks.py"},
                        },
                    ),
                )
            if self.calls == 3:
                return ModelSegment(
                    text="筛选逻辑已更新，正在验证三种状态，以及空列表的兼容性。",
                    tool_calls=(
                        {
                            "id": "verify-tasks",
                            "name": "run_command",
                            "arguments": {"command": "python -m unittest discover -s tests -v"},
                        },
                    ),
                )
            return ModelSegment(
                text="已完成任务状态筛选。\n\n- **全部**：保留原有列表内容与顺序。\n- **未完成**：仅显示尚未完成的任务。\n- **已完成**：仅显示已完成的任务。\n\n修改位于 `src/tasks.py`，验证脚本覆盖了三种筛选及空列表，**4 项检查全部通过**。",
                completion_candidate={
                    "acceptance_criteria": ["状态筛选通过验证"],
                    "evidence": [
                        {
                            "evidence_id": "local-test",
                            "kind": "test",
                            "digest": "sha256:" + "a" * 64,
                            "source": "isolated UI verification",
                            "recorded_at": 0.0,
                            "confidence": 1.0,
                        }
                    ],
                },
            )

    async def execute(request):
        started = time.perf_counter()
        if request.tool_name == "Read":
            content = target.read_text(encoding="utf-8")
            metadata = {"duration_ms": 18}
            modified = ()
            await append(run_id, "TodoCompleted", {"todo_id": "step-0"})
        elif request.tool_name == "Write":
            from cc_harness.native_tools import _hash

            result = MutationEngine(project).write(
                path="src/tasks.py",
                mode="replace_existing",
                expected_hash=_hash(target.read_bytes()),
                content='def visible_tasks(tasks, status="all"):\n    if status == "completed":\n        return [task for task in tasks if task["completed"]]\n    if status == "pending":\n        return [task for task in tasks if not task["completed"]]\n    return tasks\n',
            )
            content = result.llm_text
            metadata = {"duration_ms": 42, "structured_preview": json.loads(content)}
            modified = ("src/tasks.py",)
            await append(run_id, "TodoCompleted", {"todo_id": "step-1"})
        else:
            result = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                cwd=project,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode:
                raise RuntimeError(result.stderr)
            content = result.stdout + result.stderr
            metadata = {"duration_ms": 83, "exit_code": result.returncode}
            modified = ()
            await append(run_id, "TodoCompleted", {"todo_id": "step-2"})
        metadata["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
        observation = make_observation(
            text=content,
            attempt=1,
            effect_class=str(request.effect_class),
            action_id=request.action_id,
            tool_name=request.tool_name,
            status="succeeded",
            modified_paths=modified,
            metadata=metadata,
        )
        return ActionExecutionResult(
            ActionStatus.SUCCEEDED, observation=observation, modified_paths=modified
        )

    worker = RunWorker(
        client.store,
        ReActKernel(LocalModel()),
        worker_id="local-ui-verification",
        action_executor=execute,
        completion_verifier=lambda _: True,
        permission_mode="bypass-prompts",
    )
    lease = await worker.claim(run_id)
    try:
        await worker.execute(lease)
    finally:
        await worker.release(lease)

    approval_run = await client.submit(
        "检查发布脚本，执行前让我确认命令与访问范围。", ("发布检查完成",)
    )
    lease = await LeaseManager(client.store).claim(approval_run, "approval-verification")
    args = client.store.artifacts.put_text(
        json.dumps({"command": "python scripts/check_release.py --dry-run"})
    ).digest
    await append(
        approval_run,
        "ActionPlanned",
        {
            "action_id": "release-check",
            "tool_name": "run_command",
            "attempt": 1,
            "normalized_args_digest": "sha256:" + "a" * 64,
            "arguments_artifact": args,
            "contract_digest": "sha256:" + "b" * 64,
            "effect_class": "unknown",
            "worker_id": "approval-verification",
        },
    )
    await append(
        approval_run,
        "ApprovalRequested",
        {
            "approval_id": "release-check",
            "action_id": "release-check",
            "action_args_digest": "sha256:" + "a" * 64,
            "scope": ["run_command", "scripts/check_release.py"],
        },
    )
    await LeaseManager(client.store).release(lease)
    await client.coordinator.send(
        approval_run, "检查结束后，整理一份发布前确认清单。", follow_up=True
    )

    active_run = await client.submit("检查项目结构，梳理任务筛选的调用路径。", ("完成结构检查",))
    for index, title in enumerate(
        ("读取项目说明与入口文件", "核对任务筛选的调用路径", "整理检查结论")
    ):
        await append(
            active_run,
            "TodoCreated",
            {
                "todo": {
                    "id": f"active-{index}",
                    "title": title,
                    "status": "running" if index == 0 else "pending",
                    "active_sessions": [active_run],
                }
            },
        )

    class ActiveModel:
        async def complete(self, _messages, _tools):
            return ModelSegment(
                text="我会先阅读项目说明，再沿着任务列表的调用路径检查筛选逻辑。",
                tool_calls=(
                    {"id": "active-read", "name": "Read", "arguments": {"path": "README.md"}},
                ),
            )

    async def held_read(request):
        # A held local read makes the running UI and cancellation reproducible.
        (project / "README.md").read_text(encoding="utf-8")
        await asyncio.Event().wait()

    active_worker = RunWorker(
        client.store,
        ReActKernel(ActiveModel()),
        worker_id="held-local-read",
        action_executor=held_read,
        permission_mode="bypass-prompts",
    )
    active_lease = await active_worker.claim(active_run)
    active_task = asyncio.create_task(active_worker.execute(active_lease))
    for _ in range(100):
        view = await client.coordinator.inspect(active_run)
        if any(action.status is ActionStatus.STARTED for action in view.projection.actions):
            break
        await asyncio.sleep(0.01)

    failed_run = await client.submit("运行项目检查，汇报失败原因。", ("检查完成",))
    await append(
        failed_run,
        "RunFailed",
        {
            "reason": "本地验证：缺少依赖，任务可继续",
            "target_status": "failed_recoverable",
            "error_details": {
                "code": "dependency_missing",
                "phase": "tool",
                "message": "缺少测试依赖，尚未执行检查。",
                "retryable": True,
                "next_action": "安装依赖后继续，或补充新的处理要求。",
            },
        },
    )

    metadata = {
        "base_url": "http://127.0.0.1:3081",
        "project_root": str(project),
        "completed": run_id,
        "approval": approval_run,
        "active": active_run,
        "failed": failed_run,
        "data_kind": "deterministic local verification, no remote model",
    }
    Path("docs/design/reference/redesign-preview.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(metadata, ensure_ascii=False), flush=True)
    server = uvicorn.Server(
        uvicorn.Config(create_web_app(manager), host="127.0.0.1", port=3081, log_level="warning")
    )
    try:
        await server.serve()
    finally:
        active_task.cancel()
        await asyncio.gather(active_task, return_exceptions=True)
        await manager.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
