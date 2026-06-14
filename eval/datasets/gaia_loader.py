"""GAIA validation set loader + tool-capability filter."""
from __future__ import annotations
from dataclasses import dataclass

# File suffixes we have NO way to handle (model is text-only, no MCP coverage).
HARD_GAP_SUFFIXES: frozenset[str] = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
    ".mp3", ".wav", ".m4a", ".ogg", ".flac",
    ".mp4", ".mov", ".avi", ".webm", ".mkv",
})

# Suffixes we CAN handle via MCP (pdf-reader-mcp / excel-mcp-server / OCR-recognition)
# or via run_command fallback (pandas / pdftotext / unzip).
SOFT_GAP_SUFFIXES: frozenset[str] = frozenset({
    ".pdf", ".xlsx", ".xls", ".csv", ".tsv",
    ".txt", ".json", ".jsonl", ".xml", ".html",
    ".zip", ".tar", ".gz",
})


@dataclass(frozen=True)
class GaiaTask:
    task_id: str
    question: str
    level: int
    ground_truth: str
    file_name: str | None  # None when task has no attachment


def filter_tasks(
    tasks: list[GaiaTask], *, include_attachments: bool = True,
) -> tuple[list[GaiaTask], list[GaiaTask]]:
    """Partition into (runnable, skipped).

    Skipped:
      - any task whose file_name suffix is in HARD_GAP_SUFFIXES
      - any task whose file_name suffix is unknown (treated as hard for safety)
      - if include_attachments is False: any task with a file_name
    """
    runnable, skipped = [], []
    for t in tasks:
        if t.file_name is None:
            runnable.append(t)
            continue
        if not include_attachments:
            skipped.append(t)
            continue
        suffix = "." + t.file_name.rsplit(".", 1)[-1].lower() if "." in t.file_name else ""
        if suffix in SOFT_GAP_SUFFIXES:
            runnable.append(t)
        else:
            skipped.append(t)
    return runnable, skipped


def stratified_sample(
    tasks: list[GaiaTask], *, limit: int, seed: int,
) -> list[GaiaTask]:
    """Sample up to `limit` tasks, balancing across levels.

    If a level has fewer tasks than its share, surplus is redistributed
    to other levels. Deterministic on seed.
    """
    import random as _random
    if limit <= 0 or not tasks:
        return []
    rng = _random.Random(seed)
    by_level: dict[int, list[GaiaTask]] = {}
    for t in tasks:
        by_level.setdefault(t.level, []).append(t)
    for lst in by_level.values():
        rng.shuffle(lst)

    levels = sorted(by_level)
    per_level = max(1, limit // len(levels))
    picked: list[GaiaTask] = []
    for lv in levels:
        picked.extend(by_level[lv][:per_level])
    # Fill remaining slots from the largest leftover pools (round-robin)
    remaining = limit - len(picked)
    leftover = {lv: by_level[lv][per_level:] for lv in levels}
    while remaining > 0 and any(leftover.values()):
        for lv in levels:
            if leftover[lv]:
                picked.append(leftover[lv].pop(0))
                remaining -= 1
                if remaining == 0:
                    break
    return picked[:limit]


def _hf_load_dataset():
    """Indirection so tests can monkeypatch without importing datasets."""
    from datasets import load_dataset  # local import keeps test boot fast
    return load_dataset("gaia-benchmark/GAIA", "2023_all")


def load_gaia_validation() -> list[GaiaTask]:
    """Fetch the GAIA validation split and map rows to GaiaTask.

    Requires HF auth (HF_TOKEN env or `huggingface-cli login`).
    Cached by HF under ~/.cache/huggingface/.
    """
    ds = _hf_load_dataset()
    split = ds["validation"]
    out: list[GaiaTask] = []
    for row in split:
        fname = row.get("file_name") or ""
        out.append(GaiaTask(
            task_id=str(row["task_id"]),
            question=str(row["Question"]),
            level=int(row["Level"]),
            ground_truth=str(row["Final answer"]),
            file_name=fname if fname else None,
        ))
    return out
