# Durable Runtime Rebuild Baseline

Date: 2026-08-17

This baseline belongs to the pre-cutover runtime. It is evidence for the
rebuild and must not be overwritten by later runtime results.

## Environment

- Workspace: `D:\agent_learning\cc-harness`
- Platform: Windows
- Python: 3.13.11
- Project requirement: Python >= 3.11
- Persistence dependencies: `aiosqlite`, `sqlite-vec`
- Test configuration: `pytest`, `pytest-asyncio`, `asyncio_mode=auto`
- Static check: the project-local virtual environment currently has no `ruff`
  module installed; the planned command therefore reports an environment
  failure until the development dependency is provisioned.

## Existing dirty worktree

The baseline was captured with pre-existing user changes in the worktree.
Those changes are intentionally not included in the rebuild and must remain
untouched. The exact status is captured in the execution log for this run.

## Core regression

Command:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_agent.py tests/test_runtime.py tests/test_session_store.py -q
```

Result: passed.

The result is a pre-rebuild baseline only. It does not prove the new runtime
contract; new runtime tests live under `tests/runtime_rebuild`.

## Migration fixtures

The fixture set contains:

- a normal session with messages and a checkpoint;
- a conflicting representation of an existing message;
- Todo items in both `in_progress` and `done` states;
- memory records with and without provenance;
- an action journal with one incomplete action that must become an
  unverified/outcome-unknown migration fact rather than an automatic replay;
- malformed JSON;
- a deterministic offload object for digest-based import.

`tests/runtime_rebuild/fixtures/legacy/manifest.json` records each fixture's
SHA-256 and expected import behavior.
