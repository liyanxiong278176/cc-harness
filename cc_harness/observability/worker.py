"""Drain persisted observation work independently of an Agent process.

python -m cc_harness.observability.worker --once
python -m cc_harness.observability.worker
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager, suppress
import json
import os
from pathlib import Path

from .config import ObservationConfig
from .service import ObservationService
from .store import ObservationStore


@contextmanager
def analysis_slot(root: Path, filename="analysis.lock"):
    """One semantic request at a time across Runtime and drain processes."""
    path = root / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    acquired = False
    try:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            pass
        yield acquired
    finally:
        if acquired:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_UN)
        stream.close()


async def drain(config, source, *, analyze=True):
    from cc_harness.config import load_layered_config
    from cc_harness.llm import LLMClient
    from cc_harness.run_store import RunStore
    model = load_layered_config(source)
    results = []
    for registration in sorted((config.root / "projects").glob("*/runtime.json")):
        client = service = None
        try:
            saved = json.loads(registration.read_text(encoding="utf-8"))
            client = RunStore(Path(saved["cwd"]), data_root=Path(saved["data_root"]))
            await client.open()
            llm = LLMClient(api_key=model.openai_api_key, model=model.openai_model,
                            base_url=model.openai_base_url, thinking_mode="disabled")
            service = ObservationService(client, config, llm)
            await service.collect()
            await service.deliver()
            await service.diagnose()
            await service.import_annotations()
            if analyze:
                await service.analyze()
            results.append({"project_id": service.project_id, **service.store.status()})
        except Exception as exc:
            results.append({"project_id": registration.parent.name, "deferred": type(exc).__name__})
        finally:
            if service:
                await service.close()
            if client:
                await client.close()
    return results


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-model", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--retry-now", action="store_true", help="Retry pending analysis now after provider recovery")
    parser.add_argument("--source", type=Path, default=Path.cwd())
    args = parser.parse_args()
    config = ObservationConfig.load()
    if config is None:
        raise RuntimeError("observation configuration not enabled")
    if args.retry_now:
        (config.root / "provider-backoff.json").write_text(json.dumps({"until": 0, "reason": "explicit_retry"}), encoding="utf-8")
        for database in (config.root / "projects").glob("*/observability.db"):
            pending = ObservationStore(config.root, database.parent.name)
            try:
                with pending.db:
                    pending.db.execute("UPDATE jobs SET retry_at=0 WHERE kind='analyze' AND state='pending'")
            finally:
                pending.close()
    if args.status:
        results = []
        for database in (config.root / "projects").glob("*/observability.db"):
            pending = ObservationStore(config.root, database.parent.name)
            try:
                results.append({"project_id": database.parent.name, **pending.status()})
            finally:
                pending.close()
        print(json.dumps(results, ensure_ascii=False))
        return
    with analysis_slot(config.root, "worker.lock") as acquired:
        if not acquired:
            return
        while True:
            results = await drain(config, args.source, analyze=not args.no_model)
            if args.once:
                print(json.dumps(results, ensure_ascii=False))
                return
            await asyncio.sleep(config.poll_seconds)


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        asyncio.run(main())
