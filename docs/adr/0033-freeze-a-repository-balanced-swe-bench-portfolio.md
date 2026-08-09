# ADR 0033: Freeze a repository-balanced SWE-bench portfolio

Status: Accepted

Date: 2026-08-09

The SWE-bench Verified `portfolio` profile contains 50 cc-harness-only tasks, one logical trial per
task. All 12 upstream repositories are represented. Each receives at least two tasks when the
Verified dataset contains two; `pallets/flask` is the explicit exception because Verified contains
only one Flask task, which is included. Remaining slots are allocated by the square root of
repository size with an eight-task cap per repository. Stable hashing selects tasks
within each repository without using gold patches, hidden-test outcomes or known model performance.

Any instance previously executed, manually analyzed or used while changing cc-harness is excluded
from this frozen portfolio. Existing paired, 28-task and dev10 evidence remains immutable and is not
pooled with the new result. The `full` profile retains all 500 Verified tasks in an independent
namespace and is not the local default.

The portfolio result root is
`eval/result/cc-only/swe-bench-verified/deepseek-v4-flash/portfolio`. It uses the bounded,
run-owned Docker cleanup and resume rules from ADR 0032. Reports call it a 50-task frozen portfolio,
not the official complete SWE-bench Verified result.
