"""Prepare frozen NVIDIA RULER cases with the upstream generators."""

from __future__ import annotations

import argparse
import json
import os
import runpy
import shutil
import subprocess
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

from eval.cc_only.adapters.ruler import (
    FULL_LENGTHS,
    PORTFOLIO_LENGTHS,
    PORTFOLIO_SEEDS,
    RULER_TASKS,
)
from eval.cc_only.storage import atomic_json, digest_file

RULER_COMMIT = "c3f5e3b4f87f97e048793bb510a3a6b19a46bf3a"
RULER_ARCHIVE = f"https://codeload.github.com/NVIDIA/RULER/zip/{RULER_COMMIT}"
ENGLISH_WORDS_SHA256 = "affcd6d45fdf3cc843d585c99c97ad615094e760e6c4756b654bab6c73bc2eca"
ENGLISH_WORDS_SIZE = 8_564_991


def main() -> int:
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--project-root", type=Path, default=Path.cwd())
    args.add_argument("--profile", choices=("portfolio", "full"), default="portfolio")
    parsed = args.parse_args()
    root = parsed.project_root.resolve()
    source = _source(root)
    _prepare_english_words(source)
    _prepare_paul_graham_essays(source)
    _prepare_qa_datasets(source)
    output = root / "eval" / "cc_only" / "data" / "ruler" / parsed.profile
    output.mkdir(parents=True, exist_ok=True)
    if parsed.profile == "portfolio":
        jobs = [
            (task, length, seed, output / task / str(length) / f"seed-{seed}.json")
            for task in RULER_TASKS
            for length in PORTFOLIO_LENGTHS
            for seed in PORTFOLIO_SEEDS
            if not (output / task / str(length) / f"seed-{seed}.json").is_file()
        ]
        with ThreadPoolExecutor(max_workers=min(4, len(jobs) or 1)) as executor:
            futures = {
                executor.submit(
                    _generate, source, output / ".staging", task, length, 1, seed
                ): target
                for task, length, seed, target in jobs
            }
            for future in as_completed(futures):
                target = futures[future]
                atomic_json(target, future.result()[0])
                print(f"prepared={target}", flush=True)
    else:
        for task in RULER_TASKS:
            for length in FULL_LENGTHS:
                targets = [output / task / str(length) / f"seed-{seed}.json" for seed in range(500)]
                if all(path.is_file() for path in targets):
                    continue
                records = _generate(source, output / ".staging", task, length, 500, 42)
                if len(records) != 500:
                    raise RuntimeError(f"upstream generated {len(records)} records, expected 500")
                for seed, record in enumerate(records):
                    atomic_json(targets[seed], record)
                print(f"prepared={task}@{length}:500", flush=True)
    return 0


def _source(root: Path) -> Path:
    cache = root / "eval" / "cc_only" / "upstream" / f"ruler-{RULER_COMMIT}"
    marker = cache / "scripts" / "data" / "prepare.py"
    if marker.is_file():
        return cache
    archive = cache.parent / f"ruler-{RULER_COMMIT}.zip"
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not archive.is_file():
        print(f"downloading={RULER_ARCHIVE}", flush=True)
        urllib.request.urlretrieve(RULER_ARCHIVE, archive)
    temporary = cache.parent / f".ruler-{RULER_COMMIT}-extract"
    temporary.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            destination = (temporary / member.filename).resolve()
            if temporary.resolve() not in destination.parents and destination != temporary.resolve():
                raise RuntimeError(f"unsafe RULER archive member: {member.filename}")
        bundle.extractall(temporary)
    extracted = next(path for path in temporary.iterdir() if path.is_dir())
    extracted.replace(cache)
    return cache


def _prepare_paul_graham_essays(source: Path) -> Path:
    data_root = source / "scripts" / "data" / "synthetic" / "json"
    corpus = data_root / "PaulGrahamEssays.json"
    downloader = data_root / "download_paulgraham_essay.py"
    urls = data_root / "PaulGrahamEssays_URLs.txt"
    if not downloader.is_file() or not urls.is_file():
        raise FileNotFoundError("pinned RULER source is missing the Paul Graham corpus inputs")

    valid, text_length = _valid_paul_graham_corpus(corpus)
    if not valid:
        command = [
            str(shutil.which("uv") or "uv"),
            "run",
            "--with", "html2text",
            "--with", "beautifulsoup4",
            "--with", "tqdm",
            "python",
            str(downloader),
        ]
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        print(f"preparing={corpus}", flush=True)
        subprocess.run(command, cwd=data_root, env=environment, check=True)
        valid, text_length = _valid_paul_graham_corpus(corpus)
    if not valid:
        raise RuntimeError(
            "official RULER downloader did not produce a non-empty PaulGrahamEssays.json"
        )

    provenance = {
        "schema_version": "eval.cc-only-ruler-corpus-provenance.v1",
        "ruler_commit": RULER_COMMIT,
        "downloader": downloader.name,
        "downloader_sha256": digest_file(downloader),
        "url_list": urls.name,
        "url_list_sha256": digest_file(urls),
        "corpus": corpus.name,
        "corpus_sha256": digest_file(corpus),
        "text_characters": text_length,
    }
    atomic_json(data_root / "PaulGrahamEssays.provenance.json", provenance)
    return corpus


def _prepare_english_words(source: Path) -> Path:
    data_root = source / "scripts" / "data" / "synthetic" / "json"
    target = data_root / "english_words.json"
    url = (
        "https://media.githubusercontent.com/media/NVIDIA/RULER/"
        f"{RULER_COMMIT}/scripts/data/synthetic/json/english_words.json"
    )
    _prepare_lfs_file(
        target,
        url,
        expected_sha256=ENGLISH_WORDS_SHA256,
        expected_size=ENGLISH_WORDS_SIZE,
    )
    atomic_json(
        data_root / "english_words.provenance.json",
        {
            "schema_version": "eval.cc-only-ruler-lfs-provenance.v1",
            "ruler_commit": RULER_COMMIT,
            "source_url": url,
            "lfs_oid": f"sha256:{ENGLISH_WORDS_SHA256}",
            "size_bytes": ENGLISH_WORDS_SIZE,
            "payload_sha256": digest_file(target),
        },
    )
    return target


def _prepare_qa_datasets(source: Path) -> None:
    data_root = source / "scripts" / "data" / "synthetic" / "json"
    sources = {
        "squad.json": "https://rajpurkar.github.io/SQuAD-explorer/dataset/dev-v2.0.json",
        "hotpotqa.json": (
            "https://huggingface.co/datasets/namlh2004/hotpotqa/resolve/"
            "7e54db4656209750ff487f6fdf8e39a66dba136b/hotpot_dev_distractor_v1.json"
        ),
    }
    provenance = {
        "schema_version": "eval.cc-only-ruler-qa-provenance.v1",
        "ruler_commit": RULER_COMMIT,
        "datasets": {},
    }
    for name, url in sources.items():
        target = data_root / name
        if not _valid_qa_dataset(target, name):
            temporary = target.with_suffix(target.suffix + ".download")
            print(f"downloading={url}", flush=True)
            urllib.request.urlretrieve(url, temporary)
            if not _valid_qa_dataset(temporary, name):
                raise RuntimeError(f"downloaded RULER QA dataset is invalid: {name}")
            os.replace(temporary, target)
        provenance["datasets"][name] = {
            "source_url": url,
            "sha256": digest_file(target),
            "size_bytes": target.stat().st_size,
        }
    atomic_json(data_root / "qa-datasets.provenance.json", provenance)


def _valid_qa_dataset(path: Path, name: str) -> bool:
    if not path.is_file():
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if name == "squad.json":
        return isinstance(value, dict) and isinstance(value.get("data"), list) and bool(value["data"])
    return isinstance(value, list) and bool(value) and isinstance(value[0], dict)


def _prepare_lfs_file(
    target: Path,
    url: str,
    *,
    expected_sha256: str,
    expected_size: int,
) -> None:
    expected_digest = f"sha256:{expected_sha256}"
    if (
        target.is_file()
        and target.stat().st_size == expected_size
        and digest_file(target) == expected_digest
    ):
        return
    temporary = target.with_suffix(target.suffix + ".download")
    print(f"downloading={url}", flush=True)
    urllib.request.urlretrieve(url, temporary)
    actual_size = temporary.stat().st_size
    actual_digest = digest_file(temporary)
    if actual_size != expected_size or actual_digest != expected_digest:
        raise RuntimeError(
            f"RULER LFS payload mismatch for {target.name}: "
            f"expected {expected_size}/{expected_digest}, "
            f"found {actual_size}/{actual_digest}"
        )
    os.replace(temporary, target)


def _valid_paul_graham_corpus(path: Path) -> tuple[bool, int]:
    if not path.is_file():
        return False, 0
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False, 0
    text = value.get("text") if isinstance(value, dict) else None
    return isinstance(text, str) and bool(text.strip()), len(text) if isinstance(text, str) else 0


def _generate(
    source: Path,
    staging_root: Path,
    task: str,
    length: int,
    samples: int,
    seed: int,
) -> list[dict]:
    staging = staging_root / task / str(length) / str(seed)
    output = staging / task / "validation.jsonl"
    records = _read_generated_records(output)
    if not _valid_generated_records(records, samples=samples, target_length=length):
        staging.mkdir(parents=True, exist_ok=True)
        command = _generator_command(source, staging, task, length, samples, seed)
        subprocess.run(command, cwd=source / "scripts" / "data", check=True)
        records = _read_generated_records(output)
    if not _valid_generated_records(records, samples=samples, target_length=length):
        lengths = [record.get("length") for record in records]
        raise RuntimeError(
            f"upstream generated invalid records for {task}@{length}/{seed}: "
            f"count={len(records)}, lengths={lengths}, expected_count={samples}"
        )
    return records


def _generator_command(
    source: Path,
    staging: Path,
    task: str,
    length: int,
    samples: int,
    seed: int,
) -> list[str]:
    scripts_root = source / "scripts"
    custom = yaml.safe_load((scripts_root / "synthetic.yaml").read_text(encoding="utf-8"))
    task_config = custom[task]
    constants = runpy.run_path(
        str(scripts_root / "data" / "synthetic" / "constants.py")
    )["TASKS"]
    base = constants[task_config["task"]]
    template = str(base["template"]) + str(base.get("answer_prefix", ""))
    command = [
        str(shutil.which("uv") or "uv"),
        "run",
        "--with", "nltk",
        "--with", "wonderwords",
        "--with", "faker",
        "--with", "numpy",
        "--with", "scipy",
        "--with", "tiktoken",
        "python",
        str(scripts_root / "data" / "synthetic" / f"{task_config['task']}.py"),
        "--save_dir", str(staging),
        "--save_name", task,
        "--subset", "validation",
        "--tokenizer_path", "cl100k_base",
        "--tokenizer_type", "openai",
        "--max_seq_length", str(length),
        "--tokens_to_generate", str(base["tokens_to_generate"]),
        "--num_samples", str(samples),
        "--random_seed", str(seed),
        "--template", template,
    ]
    for name, value in task_config["args"].items():
        if isinstance(value, bool):
            if value:
                command.append(f"--{name}")
        else:
            command.extend((f"--{name}", str(value)))
    return command


def _valid_generated_records(
    records: list[dict], *, samples: int, target_length: int
) -> bool:
    minimum_length = int(target_length * 0.75)
    return len(records) == samples and all(
        isinstance(record.get("input"), str)
        and bool(record["input"].strip())
        and isinstance(record.get("outputs"), list)
        and isinstance(record.get("length"), int)
        and minimum_length <= record["length"] <= target_length
        for record in records
    )


def _read_generated_records(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []


if __name__ == "__main__":
    raise SystemExit(main())
