"""Bootstrap and manage the loopback-only D-drive Langfuse deployment.

Secrets are generated locally and never printed. Docker disks must already be
on D before pulling images or creating named volumes.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = Path("D:/agent_data/cc-harness-observability")


def compose_command(root: Path) -> list[str]:
    plugin = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/DockerDesktop/resources/cli-plugins/docker-compose.exe"
    command = [str(plugin)] if plugin.is_file() else [shutil.which("docker") or "docker", "compose"]
    return [*command, "--env-file", str(root / "deployment/compose.env"), "-f", str(REPO / "docker/langfuse/compose.yaml")]


def initialize(root: Path) -> None:
    if os.name == "nt" and root.resolve().drive.lower() != "d:":
        raise ValueError("persistent observation data must be on D:")
    deployment = root / "deployment"
    deployment.mkdir(parents=True, exist_ok=True)
    env_path = deployment / "compose.env"
    if env_path.exists():
        return
    values = {name: secrets.token_hex(24) for name in ("POSTGRES_PASSWORD", "CLICKHOUSE_PASSWORD", "REDIS_AUTH", "MINIO_PASSWORD", "SALT", "NEXTAUTH_SECRET", "LANGFUSE_USER_PASSWORD")}
    values.update(ENCRYPTION_KEY=secrets.token_hex(32), LANGFUSE_PUBLIC_KEY="pk-lf-" + secrets.token_hex(16),
                  LANGFUSE_SECRET_KEY="sk-lf-" + secrets.token_hex(24), LANGFUSE_USER_EMAIL="local@cc-harness.local")
    env_path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
    config = {"root": str(root.resolve()), "enabled_at": datetime.now(timezone.utc).isoformat(), "base_url": "http://localhost:3060",
              "public_key": values["LANGFUSE_PUBLIC_KEY"], "secret_key": values["LANGFUSE_SECRET_KEY"],
              "ordinary_sample": .10, "complex_sample": .50, "daily_limit": None}
    (root / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")


def verify_storage(root: Path) -> dict:
    if os.name != "nt":
        return {"configured_root": str(root), "platform": sys.platform}
    from docker_storage import verified_backend_pids
    if os.environ.get("DOCKER_HOST"):
        raise RuntimeError("unset DOCKER_HOST: this deployment requires the verified local Docker Desktop engine")
    context = subprocess.run(["docker", "context", "show"], capture_output=True, text=True, check=True).stdout.strip()
    if context != "desktop-linux":
        raise RuntimeError("select the local desktop-linux Docker context before starting Langfuse")
    backends = verified_backend_pids(Path("D:/DockerDesktop/local-appdata"))
    settings = Path(os.environ["APPDATA"]) / "Docker/settings-store.json"
    raw = json.loads(settings.read_text(encoding="utf-8-sig"))
    data_folder = Path(raw.get("DataFolder", Path(os.environ["LOCALAPPDATA"]) / "Docker/wsl"))
    if data_folder.drive.lower() != "d:":
        raise RuntimeError("Docker data disk is not configured on D; run move_docker_data_to_d.ps1 first")
    disk = data_folder / "disk/docker_data.vhdx"
    runtime_disk = Path("D:/DockerDesktop/local-appdata/Docker/wsl/disk/docker_data.vhdx")
    if runtime_disk.is_file():
        disk = runtime_disk
    if not disk.is_file():
        raise RuntimeError("configured D-drive Docker disk does not exist")
    return {"configured_root": str(root), "docker_disk": str(disk), "disk_bytes": disk.stat().st_size,
            "desktop_runtime_root": "D:/DockerDesktop/local-appdata", "verified_backend_pids": backends}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "pull", "up", "status", "stop", "verify", "backup", "restore-test", "configure"))
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    root = args.root.resolve()
    initialize(root)
    if args.action == "init":
        print(f"Configuration and credentials saved under {root / 'deployment'}")
        return 0
    if args.action == "configure":
        sys.path.insert(0, str(REPO))
        from cc_harness.observability.config import ObservationConfig
        from cc_harness.observability.diagnostics import DOMAINS
        from cc_harness.observability.langfuse_sink import LangfuseSink
        config = ObservationConfig.load({"CC_HARNESS_OBSERVABILITY_CONFIG": str(root / "config.json")})
        sink = LangfuseSink(config)
        try:
            for domain in DOMAINS:
                sink.review_config(domain + ".other")
        finally:
            sink.close()
        print("Nine capability review categories configured.")
        return 0
    storage = verify_storage(root)
    if args.action == "verify":
        print(json.dumps(storage, indent=2))
        return 0
    command = compose_command(root)
    if args.action in {"backup", "restore-test"}:
        from langfuse_backup import backup, restore_drill
        images = json.loads((REPO / "docker/langfuse/images.lock.json").read_text())
        if args.action == "backup":
            verifier = subprocess.run(["docker", "image", "inspect", "python:3.13-slim", "--format", "{{index .RepoDigests 0}}"], capture_output=True, text=True, check=True).stdout.strip()
            print(backup(root, command, verifier))
        else:
            if args.backup is None:
                raise ValueError("restore-test requires --backup pointing to a cold backup directory")
            pg = next(item["digest"] for item in images if item["image"] == "postgres:17")
            print(json.dumps(restore_drill(args.backup.resolve(), pg), indent=2))
        return 0
    operation = {"pull": ["pull"], "up": ["up", "-d", "--wait", "--wait-timeout", "240"], "status": ["ps"], "stop": ["stop"]}[args.action]
    return subprocess.call([*command, *operation])


if __name__ == "__main__":
    raise SystemExit(main())
