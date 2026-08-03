# Store resumable sessions with the project

Resumable conversation metadata and messages live in `<project>/.cc-harness/sessions.db`, separate from the memory database. On first use, matching sessions may be copied from the legacy repository-level `logs/memory.db`, but the old database is never deleted or modified; project-local storage makes `--continue` and `--resume` independent of the cc-harness source checkout and enforces project ownership of conversation history.
