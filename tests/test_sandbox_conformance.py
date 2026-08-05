"""Real OpenSandbox enforcement probes, gated off from the default test suite."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess

import pytest
import pytest_asyncio

try:
    from opensandbox.exceptions import SandboxException
except ImportError:
    SandboxException = RuntimeError

from cc_harness.config import SandboxConfig
from cc_harness.sandbox import _HAS_SANDBOX_SDK, SandboxExecutor, SandboxUnavailableError
from cc_harness.sandbox_server import ensure_server, shutdown_owned

RUN_CONFORMANCE = os.getenv("CC_HARNESS_RUN_SANDBOX_CONFORMANCE") == "1"
VAULT_SECRET = "cc-harness-synthetic-vault-token-v1"
ROTATED_VAULT_SECRET = "cc-harness-synthetic-vault-token-v2"
pytestmark = [
    pytest.mark.sandbox_conformance,
    pytest.mark.skipif(
        not RUN_CONFORMANCE,
        reason="set CC_HARNESS_RUN_SANDBOX_CONFORMANCE=1 to run real Docker probes",
    ),
    pytest.mark.asyncio(loop_scope="module"),
]


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def sandbox_runtime(tmp_path_factory):
    if not _HAS_SANDBOX_SDK:
        pytest.fail("opensandbox SDK is not installed in the active Python environment")
    await asyncio.to_thread(
        subprocess.run,
        ["docker", "image", "inspect", "cc-harness-runtime:local"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    project = tmp_path_factory.mktemp("sandbox-conformance-project")
    (project / ".env").write_text("API_KEY=workspace-secret", encoding="utf-8")
    (project / ".ssh").mkdir()
    (project / ".ssh" / "id_rsa").write_text("private-key", encoding="utf-8")
    (project / ".git").mkdir()
    (project / ".git" / "config").write_text("credential=secret", encoding="utf-8")

    port = _free_tcp_port()
    config = SandboxConfig(
        server_port=port,
        timeout_s=30,
        cpu=1,
        memory_mb=128,
        egress_allow=["example.com", "httpbin.org", "postman-echo.com"],
        vault=True,
        vault_credentials=[{
            "name": "conformance-token",
            "env_var": "CC_HARNESS_VAULT_CONFORMANCE_TOKEN",
        }],
        vault_bindings=[{
            "name": "httpbin-bearer",
            "credential": "conformance-token",
            "hosts": ["httpbin.org"],
            "schemes": ["https"],
            "methods": ["GET"],
            "paths": ["/headers"],
        }],
    )
    executor = SandboxExecutor(config, project_root=project)
    await executor._refresh_workspace_masks()
    server_config = project.parent / f"opensandbox-{port}.toml"
    previous_secret = os.environ.get("CC_HARNESS_CONFORMANCE_SECRET")
    previous_vault_secret = os.environ.get("CC_HARNESS_VAULT_CONFORMANCE_TOKEN")
    os.environ["CC_HARNESS_CONFORMANCE_SECRET"] = "host-only-secret"
    os.environ["CC_HARNESS_VAULT_CONFORMANCE_TOKEN"] = VAULT_SECRET
    try:
        state = await ensure_server(
            host=config.server_host,
            port=port,
            ready_timeout=60,
            config_path=server_config,
            allowed_host_paths=[str(project), str(executor._mask_plan.root)],
        )
        if state is None:
            pytest.fail("OpenSandbox server did not start with Docker available")
        executor._conformance_server_config = server_config
        yield executor
    finally:
        await executor.kill()
        await shutdown_owned()
        if previous_secret is None:
            os.environ.pop("CC_HARNESS_CONFORMANCE_SECRET", None)
        else:
            os.environ["CC_HARNESS_CONFORMANCE_SECRET"] = previous_secret
        if previous_vault_secret is None:
            os.environ.pop("CC_HARNESS_VAULT_CONFORMANCE_TOKEN", None)
        else:
            os.environ["CC_HARNESS_VAULT_CONFORMANCE_TOKEN"] = previous_vault_secret


async def _run(executor: SandboxExecutor, command: str):
    return await executor.run({"command": command}, cwd=executor.project_root)


async def _runtime_container_inspect(executor: SandboxExecutor) -> dict:
    sandbox_id = executor._sandbox.id
    completed = await asyncio.to_thread(
        subprocess.run,
        [
            "docker",
            "ps",
            "--quiet",
            "--filter",
            f"name={sandbox_id}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    container_ids = completed.stdout.split()
    assert container_ids, f"no runtime container found for sandbox {sandbox_id}"
    inspected = await asyncio.to_thread(
        subprocess.run,
        ["docker", "inspect", container_ids[0]],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(inspected.stdout)[0]


async def test_sensitive_workspace_paths_are_empty_overlays(sandbox_runtime):
    result = await _run(
        sandbox_runtime,
        "test ! -s /workspace/.env && "
        "test -d /workspace/.ssh && test -z \"$(ls -A /workspace/.ssh)\" && "
        "test ! -s /workspace/.git/config",
    )

    assert not result.is_error, result.llm_text


async def test_host_environment_secret_is_not_injected(sandbox_runtime):
    result = await _run(
        sandbox_runtime,
        'test -z "${CC_HARNESS_CONFORMANCE_SECRET+x}"',
    )

    assert not result.is_error, result.llm_text


async def test_cgroup_cpu_limit_is_enforced(sandbox_runtime):
    result = await _run(sandbox_runtime, "cat /sys/fs/cgroup/cpu.max")

    assert not result.is_error, result.llm_text
    quota, period = result.llm_text.strip().split()[:2]
    assert quota != "max"
    assert int(quota) / int(period) <= 1.05


async def test_cgroup_memory_limit_is_enforced(sandbox_runtime):
    result = await _run(sandbox_runtime, "cat /sys/fs/cgroup/memory.max")

    assert not result.is_error, result.llm_text
    observed = result.llm_text.strip().splitlines()[0]
    assert observed != "max"
    assert int(observed) <= 128 * 1024 * 1024


async def test_allowlisted_domain_is_reachable(sandbox_runtime):
    result = await _run(
        sandbox_runtime,
        "curl --fail --silent --show-error --max-time 15 "
        "https://example.com -o /dev/null",
    )

    assert not result.is_error, result.llm_text


async def test_unlisted_domain_is_blocked(sandbox_runtime):
    result = await _run(
        sandbox_runtime,
        "curl --fail --silent --show-error --max-time 10 "
        "https://example.org -o /dev/null",
    )

    assert result.is_error, "unlisted example.org unexpectedly had network access"


async def test_direct_ip_cannot_bypass_domain_policy(sandbox_runtime):
    result = await _run(
        sandbox_runtime,
        "curl --fail --silent --show-error --max-time 10 "
        "http://1.1.1.1/cdn-cgi/trace -o /dev/null",
    )

    assert result.is_error, "direct IP unexpectedly bypassed the egress policy"


async def test_external_server_requires_and_accepts_matching_attestation(sandbox_runtime):
    from cc_harness import sandbox_server as server

    endpoint = (sandbox_runtime.cfg.server_host, sandbox_runtime.cfg.server_port)
    trusted = server._TRUSTED_ENDPOINTS.pop(endpoint)
    try:
        state = await ensure_server(
            host=endpoint[0],
            port=endpoint[1],
            config_path=sandbox_runtime._conformance_server_config,
            allowed_host_paths=[
                str(sandbox_runtime.project_root),
                str(sandbox_runtime._mask_plan.root),
            ],
            pids_limit=sandbox_runtime.cfg.pids_limit,
        )
    finally:
        server._TRUSTED_ENDPOINTS[endpoint] = trusted

    assert state is not None
    assert state.owned is False
    assert state.attested is True
    assert state.egress_mode == "dns+nft"


async def test_allowlisted_private_host_cannot_rebind_around_egress(sandbox_runtime):
    config = sandbox_runtime.cfg.model_copy(
        update={"egress_allow": ["host.docker.internal"]}
    )
    executor = SandboxExecutor(config, sandbox_runtime.project_root)

    with pytest.raises(SandboxUnavailableError, match="non-public address"):
        await _run(executor, "true")


async def test_memory_overcommit_is_killed(sandbox_runtime):
    try:
        result = await _run(
            sandbox_runtime,
            "python -c \"x = bytearray(256 * 1024 * 1024); print(len(x))\"",
        )
    except SandboxUnavailableError:
        return

    assert result.is_error, "256 MiB allocation succeeded inside a 128 MiB sandbox"


async def test_runtime_is_not_privileged_and_has_pid_ceiling(sandbox_runtime):
    await _run(sandbox_runtime, "true")
    inspected = await _runtime_container_inspect(sandbox_runtime)
    host_config = inspected["HostConfig"]

    assert host_config["Privileged"] is False
    assert not host_config.get("CapAdd")
    assert host_config["PidsLimit"] == sandbox_runtime.cfg.pids_limit
    assert any(
        item.lower().startswith("no-new-privileges")
        for item in host_config.get("SecurityOpt") or []
    )


async def test_dangerous_capabilities_seccomp_and_privilege_gain_are_blocked(sandbox_runtime):
    result = await _run(
        sandbox_runtime,
        "python -c \""
        "s=open('/proc/self/status').read().splitlines();"
        "d={x.split(':',1)[0]:x.split(':',1)[1].strip() for x in s if ':' in x};"
        "cap=int(d['CapEff'],16);"
        "assert all(not cap&(1<<b) for b in (12,13,16,19,21));"
        "assert d['NoNewPrivs']=='1';"
        "assert d['Seccomp']=='2'\"",
    )
    assert not result.is_error, result.llm_text

    mount_result = await _run(
        sandbox_runtime,
        "mkdir -p /tmp/privileged-mount && "
        "mount -t tmpfs tmpfs /tmp/privileged-mount",
    )
    assert mount_result.is_error, "mount syscall succeeded without SYS_ADMIN"


async def test_host_control_sockets_and_host_mounts_are_absent(sandbox_runtime):
    result = await _run(
        sandbox_runtime,
        "test ! -e /var/run/docker.sock && "
        "test ! -e /run/docker.sock && "
        "test ! -e /host && "
        "test ! -e /run/desktop/mnt/host/c",
    )
    assert not result.is_error, result.llm_text


async def test_fork_bomb_is_bounded_and_sandbox_remains_usable(sandbox_runtime):
    result = await _run(
        sandbox_runtime,
        "python -c \"import os,time; children=[]; limited=False; "
        "\ntry:\n while len(children)<400:\n  p=os.fork(); "
        "\n  if p==0: time.sleep(30); os._exit(0)\n  children.append(p)"
        "\nexcept OSError: limited=True"
        "\nfinally:"
        "\n for p in children:"
        "\n  try: os.kill(p,9)"
        "\n  except ProcessLookupError: pass"
        "\n for p in children:"
        "\n  try: os.waitpid(p,0)"
        "\n  except ChildProcessError: pass"
        "\nraise SystemExit(0 if limited else 2)\"",
    )
    assert not result.is_error, result.llm_text
    follow_up = await _run(sandbox_runtime, "printf sandbox-alive")
    assert not follow_up.is_error, follow_up.llm_text
    assert "sandbox-alive" in follow_up.llm_text


async def test_vault_injects_only_for_the_bound_target(sandbox_runtime):
    bound = await _run(
        sandbox_runtime,
        "curl --fail --silent --show-error --max-time 15 https://httpbin.org/headers",
    )
    assert not bound.is_error, bound.llm_text
    assert VAULT_SECRET in bound.llm_text

    unbound = await _run(
        sandbox_runtime,
        "curl --fail --silent --show-error --max-time 15 "
        "https://postman-echo.com/headers",
    )
    assert not unbound.is_error, unbound.llm_text
    assert VAULT_SECRET not in unbound.llm_text

    state = await sandbox_runtime._sandbox.credential_vault.get()
    assert [item.name for item in state.credentials] == ["conformance-token"]
    assert [item.name for item in state.bindings] == ["httpbin-bearer"]
    assert VAULT_SECRET not in state.model_dump_json()


async def test_vault_rotation_rejects_stale_revision_replay(sandbox_runtime):
    broker = sandbox_runtime._credential_broker
    stale_revision = broker.revision
    os.environ["CC_HARNESS_VAULT_CONFORMANCE_TOKEN"] = ROTATED_VAULT_SECRET

    rotated_revision = await broker.rotate(sandbox_runtime._sandbox)
    assert rotated_revision > stale_revision
    with pytest.raises(SandboxException):
        await sandbox_runtime._sandbox.credential_vault.patch(
            expected_revision=stale_revision,
            credentials={"replace": []},
        )

    response = await _run(
        sandbox_runtime,
        "curl --fail --silent --show-error --max-time 15 https://httpbin.org/headers",
    )
    assert not response.is_error, response.llm_text
    assert ROTATED_VAULT_SECRET in response.llm_text
    assert VAULT_SECRET not in response.llm_text


async def test_vault_revocation_removes_injection_and_audit_is_redacted(sandbox_runtime):
    await sandbox_runtime._credential_broker.revoke(sandbox_runtime._sandbox)
    response = await _run(
        sandbox_runtime,
        "curl --fail --silent --show-error --max-time 15 https://httpbin.org/headers",
    )
    if not response.is_error:
        assert ROTATED_VAULT_SECRET not in response.llm_text

    audit = (
        sandbox_runtime.project_root / "logs" / "credential-broker.jsonl"
    ).read_text(encoding="utf-8")
    assert VAULT_SECRET not in audit
    assert ROTATED_VAULT_SECRET not in audit
    assert '"action": "revoked"' in audit


async def test_background_daemon_cannot_outlive_sandbox_container(sandbox_runtime):
    result = await _run(
        sandbox_runtime,
        "nohup sh -c 'while :; do sleep 30; done' "
        ">/tmp/cc-harness-daemon.log 2>&1 & echo $!",
    )
    assert not result.is_error, result.llm_text
    assert result.llm_text.strip().splitlines()[0].isdigit()


async def test_session_cleanup_removes_sandbox_containers(sandbox_runtime):
    assert sandbox_runtime._sandbox is not None
    sandbox_id = sandbox_runtime._sandbox.id

    assert await sandbox_runtime.kill(), "SandboxExecutor reported a teardown failure"
    remaining = ""
    for _attempt in range(20):
        completed = await asyncio.to_thread(
            subprocess.run,
            [
                "docker",
                "ps",
                "--all",
                "--quiet",
                "--filter",
                f"name={sandbox_id}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        remaining = completed.stdout.strip()
        if not remaining:
            break
        await asyncio.sleep(0.5)

    assert not remaining, f"sandbox containers remained after teardown: {remaining}"
