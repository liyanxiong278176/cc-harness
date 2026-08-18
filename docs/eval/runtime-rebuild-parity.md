# Runtime Rebuild Parity Evidence

This file defines the new result root and comparison contract for the rebuilt
runtime. Existing benchmark evidence is not overwritten.

The frozen task/model/environment contract must be copied into a new run
directory before collecting results. Each run records:

- coding success and completion-evidence validity;
- recovery correctness and duplicated side effects;
- child candidate integration and conflict handling;
- latency, model calls, tokens, and cost;
- safety, permission, and data-loss failures as independent gates.

Suggested commands:

```powershell
.venv/Scripts/python.exe scripts/run_runtime_rebuild_gate.py --all --report .cc-harness/runtime-rebuild/gate-report.json
.venv/Scripts/python.exe scripts/check_runtime_rebuild_migration.py --dry-run --fixture tests/runtime_rebuild/fixtures/legacy
.venv/Scripts/python.exe scripts/rehearse_runtime_cutover.py --fixture tests/runtime_rebuild/fixtures/legacy --project-root . --data-root .cc-harness/rehearsal-data --backup-root .cc-harness/backups --restore-root .cc-harness/restore --rollback
```

No aggregate score may compensate for a security, recovery, permission, or
data-loss gate failure. Numeric resume/cost claims require a committed report
under the new result root.

The rebuild gate evidence for this implementation is stored at
`docs/eval/runtime-rebuild-gate-report.json`. It records the non-compensating
runtime tests, static check, and fixture migration reconciliation separately;
the real-project migration and cutover report remain operational evidence and
do not replace a future live benchmark holdout.
