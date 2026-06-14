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