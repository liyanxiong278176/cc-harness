import subprocess
import sys
import tomllib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_owned_proc():
    yield
    from cc_harness import sandbox_server as ss
    ss._OWNED_PROC[0] = None
    ss._TRUSTED_ENDPOINTS.clear()


@pytest.mark.asyncio
async def test_ping_returns_true_when_port_open():
    from cc_harness.sandbox_server import ping
    # ping 会 unpack (reader, writer) 并 await writer.wait_closed(),
    # 故 mock 需返回 2-tuple 且 writer.wait_closed 可 await(plan 原文默认 AsyncMock 不够)
    writer = MagicMock()
    writer.wait_closed = AsyncMock()
    with patch("cc_harness.sandbox_server.asyncio.open_connection",
               new_callable=AsyncMock, return_value=(MagicMock(), writer)):
        assert await ping("localhost", 8000) is True


@pytest.mark.asyncio
async def test_ping_returns_false_when_refused():
    from cc_harness.sandbox_server import ping
    with patch("cc_harness.sandbox_server.asyncio.open_connection",
               side_effect=ConnectionRefusedError):
        assert await ping("localhost", 8000) is False


@pytest.mark.asyncio
async def test_ensure_server_reuses_existing(monkeypatch):
    """server 已在跑 → 复用,不 fork,标记 external。"""
    from cc_harness import sandbox_server as ss
    monkeypatch.setattr(ss, "ping", AsyncMock(return_value=True))
    monkeypatch.setattr(ss, "health", AsyncMock(return_value=True))
    fork = MagicMock()
    monkeypatch.setattr(ss, "_fork_server", fork)
    state = await ss.ensure_server(port=8000, require_external_attestation=False)
    assert state.owned is False
    fork.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_server_starts_when_absent(monkeypatch):
    """server 没跑 + Docker 可用 → fork,标 owned。"""
    from cc_harness import sandbox_server as ss
    monkeypatch.setattr(ss, "ping", AsyncMock(side_effect=[False, True]))
    monkeypatch.setattr(ss, "health", AsyncMock(return_value=True))
    monkeypatch.setattr(ss, "_docker_available", MagicMock(return_value=True))
    # _fork_server 在 impl 里被 await,故用 AsyncMock(plan 原文 MagicMock 不可 await)
    monkeypatch.setattr(ss, "_fork_server", AsyncMock())
    monkeypatch.setattr(
        ss,
        "attest_server_config",
        MagicMock(return_value=ss.ServerState(owned=False, attested=True)),
    )
    state = await ss.ensure_server(port=8000)
    assert state.owned is True


@pytest.mark.asyncio
async def test_ensure_server_fallback_when_no_docker(monkeypatch):
    """Docker 不可用 → 返回 None(调用方降级 native)。"""
    from cc_harness import sandbox_server as ss
    monkeypatch.setattr(ss, "ping", AsyncMock(return_value=False))
    monkeypatch.setattr(ss, "_docker_available", MagicMock(return_value=False))
    state = await ss.ensure_server(port=8000)
    assert state is None


@pytest.mark.asyncio
async def test_ensure_server_times_out_and_kills(monkeypatch):
    """fork 后轮询超时 → kill proc + 返 None(第四分支)。"""
    from cc_harness import sandbox_server as ss
    monkeypatch.setattr(ss, "ping", AsyncMock(side_effect=lambda *a, **kw: False))
    monkeypatch.setattr(ss, "_docker_available", MagicMock(return_value=True))
    fake_proc = MagicMock()
    monkeypatch.setattr(ss, "_fork_server", AsyncMock(return_value=fake_proc))
    state = await ss.ensure_server(port=8000, ready_timeout=0.01)
    assert state is None
    fake_proc.kill.assert_called()   # _kill_proc_tree 走 fallback 调 proc.kill()


def _server_cli_path() -> Path:
    exe = "opensandbox-server.exe" if sys.platform == "win32" else "opensandbox-server"
    return Path(sys.executable).parent / exe


def test_set_allowed_host_paths_toml_round_trip(tmp_path):
    r"""_set_allowed_host_paths 写出的 toml 经 tomllib 解析还原成原 Windows 路径。

    这是手动编辑时踩的坑:Windows `\` 在 toml 基本串里要写成 `\\`,否则 tomllib
    报 invalid escape 或吞掉。本测真跑 init-config 生成 config(经 server CLI),
    依次 _set_config_port + _set_allowed_host_paths 后 tomllib.load,断 round-trip。
    CLI 不可用/init-config 失败时 skip(不强依赖 server 包安装)。
    """
    from cc_harness.sandbox_server import _set_allowed_host_paths, _set_config_port

    cli = _server_cli_path()
    if not cli.exists():
        pytest.skip("opensandbox-server CLI 不在 venv(init-config 不可用)")
    config = tmp_path / "server.toml"
    r = subprocess.run(
        [str(cli), "init-config", str(config), "--example", "docker"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    if r.returncode != 0 or not config.exists():
        pytest.skip(f"init-config 未能生成 config(rc={r.returncode})")

    # 用含反斜杠的 Windows 风格路径验转义(正斜杠无转义问题,测不出坑)。
    raw_path = r"D:\agent_learning\cc-harness"
    _set_config_port(config, 8000)
    _set_allowed_host_paths(config, [raw_path])

    # 关键断言:tomllib 解析回的字符串与原路径逐字符相等(反斜杠未丢/未多)。
    data = tomllib.loads(config.read_text(encoding="utf-8"))
    assert data["server"]["port"] == 8000
    assert data["storage"]["allowed_host_paths"] == [raw_path]


def test_set_egress_mode_upgrades_existing_dns_mode(tmp_path):
    from cc_harness.sandbox_server import _set_egress_mode

    config = tmp_path / "server.toml"
    config.write_text(
        '[server]\nport = 8000\n\n[egress]\nimage = "egress:test"\nmode = "dns"\n',
        encoding="utf-8",
    )

    _set_egress_mode(config)

    data = tomllib.loads(config.read_text(encoding="utf-8"))
    assert data["egress"]["image"] == "egress:test"
    assert data["egress"]["mode"] == "dns+nft"


def test_set_egress_mode_adds_missing_section(tmp_path):
    from cc_harness.sandbox_server import _set_egress_mode

    config = tmp_path / "server.toml"
    config.write_text('[server]\nport = 8000\n', encoding="utf-8")

    _set_egress_mode(config)

    data = tomllib.loads(config.read_text(encoding="utf-8"))
    assert data["egress"] == {
        "image": "opensandbox/egress:v1.1.4",
        "mode": "dns+nft",
    }


def test_set_egress_mode_adds_mode_only_inside_existing_section(tmp_path):
    from cc_harness.sandbox_server import _set_egress_mode

    config = tmp_path / "server.toml"
    config.write_text(
        '[egress]\nimage = "egress:test"\n\n[unrelated]\nmode = "keep-me"\n',
        encoding="utf-8",
    )

    _set_egress_mode(config)

    data = tomllib.loads(config.read_text(encoding="utf-8"))
    assert data["egress"]["mode"] == "dns+nft"
    assert data["unrelated"]["mode"] == "keep-me"


def _write_attested_config(path: Path, root: Path, *, egress_mode: str = "dns+nft") -> None:
    normalized = str(root).replace("\\", "\\\\")
    path.write_text(
        f'''[server]\nhost = "127.0.0.1"\nport = 8000\n\n'''
        f'''[runtime]\ntype = "docker"\n\n'''
        f'''[egress]\nmode = "{egress_mode}"\n\n'''
        f'''[storage]\nallowed_host_paths = ["{normalized}"]\n\n'''
        '''[docker]\npids_limit = 256\nno_new_privileges = true\n'''
        '''drop_capabilities = ["NET_ADMIN", "SYS_ADMIN", "SYS_MODULE", "SYS_PTRACE"]\n''',
        encoding="utf-8",
    )


def test_attest_server_config_accepts_required_security_controls(tmp_path):
    from cc_harness.sandbox_server import attest_server_config

    config = tmp_path / "external.toml"
    _write_attested_config(config, tmp_path)

    state = attest_server_config(
        config,
        host="127.0.0.1",
        port=8000,
        required_host_paths=[str(tmp_path / "project")],
        max_pids=256,
    )

    assert state.attested is True
    assert state.egress_mode == "dns+nft"
    assert state.config_digest.startswith("sha256:")


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda text: text.replace('mode = "dns+nft"', 'mode = "dns"'), "egress.mode"),
        (lambda text: text.replace("pids_limit = 256", "pids_limit = 4096"), "pids_limit"),
        (
            lambda text: text.replace(
                'drop_capabilities = ["NET_ADMIN", "SYS_ADMIN", "SYS_MODULE", "SYS_PTRACE"]',
                'drop_capabilities = ["NET_ADMIN"]',
            ),
            "drop_capabilities",
        ),
    ],
)
def test_attest_server_config_rejects_security_mismatch(tmp_path, mutate, expected):
    from cc_harness.sandbox_server import ServerAttestationError, attest_server_config

    config = tmp_path / "external.toml"
    _write_attested_config(config, tmp_path)
    config.write_text(mutate(config.read_text(encoding="utf-8")), encoding="utf-8")

    with pytest.raises(ServerAttestationError, match=expected):
        attest_server_config(
            config,
            host="127.0.0.1",
            port=8000,
            required_host_paths=[str(tmp_path / "project")],
            max_pids=256,
        )


@pytest.mark.asyncio
async def test_external_server_without_attestation_fails_closed(monkeypatch):
    from cc_harness import sandbox_server as ss

    monkeypatch.setattr(ss, "ping", AsyncMock(return_value=True))
    monkeypatch.setattr(ss, "health", AsyncMock(return_value=True))

    with pytest.raises(ss.ServerAttestationError, match="server_config_path"):
        await ss.ensure_server(port=8000)


def test_set_docker_security_pins_pid_and_privilege_controls(tmp_path):
    from cc_harness.sandbox_server import _set_docker_security

    config = tmp_path / "server.toml"
    config.write_text(
        "[docker]\npids_limit = 4096\nno_new_privileges = false\n",
        encoding="utf-8",
    )

    _set_docker_security(config, 128)

    data = tomllib.loads(config.read_text(encoding="utf-8"))
    assert data["docker"]["pids_limit"] == 128
    assert data["docker"]["no_new_privileges"] is True
    assert {"NET_ADMIN", "SYS_ADMIN", "SYS_MODULE", "SYS_PTRACE"}.issubset(
        data["docker"]["drop_capabilities"]
    )

