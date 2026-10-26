"""Cold backup of all Langfuse volumes and a non-destructive restore drill."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import uuid


def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, **kwargs)


def backup(root: Path, compose: list[str], image: str) -> Path:
    folder = root / "backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder.mkdir(parents=True, exist_ok=False)
    volumes = json.loads(run(*compose, "config", "--format", "json").stdout)["volumes"]
    records = []
    run(*compose, "stop")
    try:
        for key, volume in volumes.items():
            name = volume["name"]
            archive = folder / (key + ".tar.gz")
            run("docker", "run", "--rm", "--network", "none", "--mount", f"type=volume,source={name},target=/data,readonly",
                "--mount", f"type=bind,source={folder},target=/backup", image,
                "tar", "-czf", "/backup/" + archive.name, "-C", "/data", ".")
            records.append({"key": key, "volume": name, "archive": archive.name,
                            "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()})
        shutil.copy2(root / "deployment/compose.env", folder / "compose.env")
        shutil.copy2(root / "config.json", folder / "config.json")
        repository = Path(__file__).resolve().parents[1]
        for relative in ("docker/langfuse/compose.yaml", "docker/langfuse/images.lock.json", "docker/langfuse/audit_proxy.py", "cc_harness/secret_patterns.py"):
            destination = folder / "bundle" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(repository / relative, destination)
        # Each queue database is backed up through SQLite's snapshot API. Asset
        # files are append-only and copied after their referencing snapshots.
        local = folder / "local"
        for project in (root / "projects").glob("*"):
            if not project.is_dir():
                continue
            destination = local / "projects" / project.name
            destination.mkdir(parents=True, exist_ok=True)
            for database in project.glob("*.db"):
                with sqlite3.connect(database) as source_db, sqlite3.connect(destination / database.name) as target_db:
                    source_db.backup(target_db)
            for registration in project.glob("*.json"):
                shutil.copy2(registration, destination / registration.name)
        if (root / "assets").exists():
            shutil.copytree(root / "assets", local / "assets", ignore=shutil.ignore_patterns(".pending-*"))
        evaluation = root / "evaluation"
        for name in ("catalog.json", "approved-baseline.json"):
            if (evaluation / name).is_file():
                (local / "evaluation").mkdir(parents=True, exist_ok=True)
                shutil.copy2(evaluation / name, local / "evaluation" / name)
        for result in evaluation.glob("runs/*/*/result.json"):
            destination = local / "evaluation" / result.parent.relative_to(evaluation)
            destination.mkdir(parents=True, exist_ok=True)
            for name in ("result.json", "events.json", "case-manifest.json"):
                if (result.parent / name).is_file():
                    shutil.copy2(result.parent / name, destination / name)
        manifest = {"scope": "all-langfuse-volumes-cold; observation-queues-and-assets; completed-evaluation-evidence",
                    "active_agent_workspaces_included": False, "same_disk_backup": True,
                    "created_at": datetime.now(timezone.utc).isoformat(), "volumes": records, "verifier_image": image}
        (folder / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    finally:
        run(*compose, "up", "-d", "--wait", "--wait-timeout", "240")
    return folder


def restore_drill(folder: Path, postgres_image: str) -> dict:
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    suffix = uuid.uuid4().hex[:12]
    prefix = "cc-harness-restore-test-" + suffix
    created = []
    container = prefix + "-postgres"
    started = False
    report = {"backup": str(folder), "restored_volumes": [], "postgres_readable": False}
    try:
        for record in manifest["volumes"]:
            archive = folder / record["archive"]
            if archive.parent != folder or hashlib.sha256(archive.read_bytes()).hexdigest() != record["sha256"]:
                raise ValueError("backup archive digest mismatch")
            volume = prefix + "-" + record["key"]
            run("docker", "volume", "create", volume)
            created.append(volume)
            run("docker", "run", "--rm", "--network", "none", "--mount", f"type=volume,source={volume},target=/data",
                "--mount", f"type=bind,source={folder},target=/backup,readonly", manifest["verifier_image"],
                "tar", "-xzf", "/backup/" + archive.name, "-C", "/data")
            # Compare every restored file against the archive, including content,
            # modes and ownership. This never mounts an active production volume.
            run("docker", "run", "--rm", "--network", "none", "--mount", f"type=volume,source={volume},target=/data,readonly",
                "--mount", f"type=bind,source={folder},target=/backup,readonly", manifest["verifier_image"],
                "tar", "-dzf", "/backup/" + archive.name, "-C", "/data")
            report["restored_volumes"].append(record["key"])
        pg = prefix + "-postgres"
        run("docker", "run", "-d", "--name", container, "--network", "none",
            "--mount", f"type=volume,source={pg},target=/var/lib/postgresql/data", postgres_image)
        started = True
        for _ in range(30):
            if subprocess.run(["docker", "exec", container, "pg_isready", "-U", "postgres", "-d", "langfuse"], capture_output=True).returncode == 0:
                break
            time.sleep(1)
        count = run("docker", "exec", container, "psql", "-U", "postgres", "-d", "langfuse", "-Atc", "SELECT count(*) FROM datasets").stdout.decode().strip()
        report["postgres_readable"] = True
        report["restored_dataset_count"] = int(count)
        report["passed"] = True
        (folder / "restore-drill.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report
    finally:
        if started:
            run("docker", "rm", "-f", container)
        for volume in created:
            if not volume.startswith(prefix + "-"):
                raise ValueError("refusing to remove a volume outside this restore drill")
            run("docker", "volume", "rm", volume)
