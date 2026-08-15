import asyncio
import os
import sys

import pytest

from eval.launch.models import HarnessKind, LaunchProfile
from eval.launch.profiles import LaunchInvocation
from eval.launch.runner import run_invocation


@pytest.mark.asyncio
async def test_run_invocation_mirrors_stderr_before_child_exits(tmp_path):
    profile = LaunchProfile(
        profile_id="cc-harness.live-stderr-test",
        harness=HarnessKind.CC_HARNESS,
        executable=sys.executable,
        provider_route_id="offline-test",
    )
    invocation = LaunchInvocation(
        argv=(
            sys.executable,
            "-c",
            (
                "import sys,time; "
                "sys.stderr.write('startup failed\\n'); sys.stderr.flush(); "
                "time.sleep(30)"
            ),
        ),
        cwd=tmp_path,
        environment=dict(os.environ),
        stdin=b"",
    )
    stderr_path = tmp_path / "stderr.txt"

    launch = asyncio.create_task(
        run_invocation(
            profile,
            invocation,
            timeout_seconds=10,
            stderr_path=stderr_path,
        )
    )
    try:
        for _ in range(100):
            if stderr_path.is_file() and "startup failed" in stderr_path.read_text(
                encoding="utf-8"
            ):
                break
            await asyncio.sleep(0.01)
        assert not launch.done()
        assert "startup failed" in stderr_path.read_text(encoding="utf-8")
    finally:
        launch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await launch
