# Terminal-Bench 2.1 Easy/Medium Failure Analysis

## Scope

- Result root: `eval/result/cc-only/terminal-bench-2.1/deepseek-v4-flash/full-new-260823221106-selected-14d6f90dbe`
- Frozen selection: 4 Easy and 55 Medium tasks
- Protocol: official Terminal-Bench 2.1 task images and unmodified verifiers, one trial per task
- Final audited result before remediation: 44 pass, 15 fail, 0 invalid, 0 pending
- Selected-set pass rate: 74.58%

The original report contained 22 failures. Seven of those failures had a retained
official verifier reward of `1.0` despite an agent lifecycle exception. They were
reclassified as passes without rerunning the model. Each changed result retains its
original result and a provenance record.

## Failure Classes

| Class | Count | Meaning |
|---|---:|---|
| Deadline/artifact completion | 6 | The agent reached a timeout or exited before writing the required final artifact. |
| Partial correctness/performance | 7 | The artifact existed and some checks passed, but the official verifier still returned zero. |
| Environment setup before model execution | 2 | The custom agent installer selected a nonexistent fallback Python, so no model task ran and no deterministic reward was produced. |

## Per-Task Evidence

| Task | Class | Evidence | Project-level lesson |
|---|---|---|---|
| `chess-best-move` | Deadline/artifact | Agent timed out after 900s; `/app/move.txt` was absent. | Reserve time for the required artifact and verify its exact path before further investigation. |
| `code-from-image` | Deadline/artifact | Agent timed out after 1200s; `/app/output.txt` was absent; 0/2 checks passed. | Create a minimally valid output early, then refine it. |
| `count-dataset-tokens` | Deadline/artifact | Agent exited nonzero; `/app/answer.txt` was absent. | Treat explicit output files as completion gates. |
| `db-wal-recovery` | Deadline/artifact | Agent timed out after 900s; `/app/recovered.json` was absent; all seven checks failed. | Persist partial recovered data before deep forensic work. |
| `gcode-to-text` | Deadline/artifact | Agent timed out after 900s; required output files were absent; 0/2 checks passed. | Materialize the requested deliverable before optional validation. |
| `raman-fitting` | Deadline/artifact | Agent timed out after 900s; `/app/results.json` was absent; 0/3 checks passed. | Add deadline-aware fallback output for numerical tasks. |
| `largest-eigenval` | Partial performance | 23 checks passed and four speedup checks failed after a 900s agent timeout. | Stop correctness work once stable and spend the remaining budget on the stated performance constraint. |
| `mailman` | Partial service correctness | Two checks passed; the join/announce/leave SMTP flow failed after a 1800s timeout. | Verify service readiness and the complete lifecycle, not only process existence. |
| `filter-js-from-html` | Partial security correctness | Clean HTML preservation passed; XSS blocking failed. Browser sessions also reset during verification. | Test adversarial transformations and separate service instability from filter correctness. |
| `kv-store-grpc` | Partial service correctness | Five checks passed; the real gRPC server was not reachable and two checks failed. | A background service is complete only after a bounded health probe succeeds. |
| `large-scale-text-editing` | Partial correctness | Expected transformed input and macro artifacts were incomplete; five checks failed. | Validate every named artifact and invariants before declaring completion. |
| `query-optimize` | Partial performance | Five checks passed; output was correct, but median runtime was 0.778s versus the 0.574s golden query. | Preserve correctness while measuring the explicit performance threshold before exit. |
| `sanitize-git-repo` | Partial security correctness | Repository scope was preserved, but secrets were not replaced correctly; 1/3 checks passed. | Add post-edit secret scanning and exact replacement validation. |
| `qemu-alpine-ssh` | Environment setup | Agent installation failed before model execution: no Python >=3.11 was present and `/opt/cc-harness/verifier-runtime/python/bin/python` did not exist. | The installer must carry or discover a valid Python independently of the task image. |
| `qemu-startup` | Environment setup | Same deterministic installer failure as `qemu-alpine-ssh`; no verifier reward was produced. | Fail preflight before spending a trial, or install from the frozen runtime correctly. |

## Remediation Priorities

1. Fix agent installation fallback so task images without a suitable system Python use the frozen runtime executable that actually exists.
2. Add a deadline budget to the agent loop: establish deliverables early, reserve a finalization window, and stop exploratory work when the remaining budget is low.
3. Convert explicit requested paths and services into completion gates. File existence, syntax, executable bits, and service health must be checked before emitting completion.
4. Make successful completion terminate the agent process cleanly so Harbor does not record `NonZeroAgentExitCodeError` after a passing verifier artifact.
5. Preserve verifier reward, lifecycle exception, command progress, last artifact check, and usage completeness as separate telemetry fields.

## Evaluation Policy

These 59 tasks are now a development set. Their evidence may guide generic project
improvements, but scores from later reruns must be labelled diagnostic. The 30 frozen
Hard tasks remain the one-time holdout and must not be inspected or used for tuning
before the final run.
