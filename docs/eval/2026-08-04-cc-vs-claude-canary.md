# cc-harness vs Claude Code Canary Report

Date: 2026-08-04
Model: `deepseek-v4-flash` on both harnesses
Scope: 10 deterministic, single-file Python repair tasks on Windows

## Result

| Harness | Passed | Input tokens | Median input | Output tokens | Median latency |
|---|---:|---:|---:|---:|---:|
| cc-harness | 9/10 | 239,074 | 27,873.5 | 11,016 | 22.21 s |
| Claude Code | 10/10 | 29,960 | 3,018.5 | 8,925 | 29.37 s |

Pair outcomes: 9 ties, 0 cc-harness wins, 1 Claude Code win. With only one discordant
pair, the paired Wilson comparison is inconclusive (`candidate_win_rate=0.0`, 95% interval
`[0.0, 0.79345]`, minimum discordant count 10).

Claude Code reported `$0.410493` across the 10 selected trials. cc-harness provider cost is
unknown, not zero, because its OpenAI-compatible response did not include cost telemetry.

## Invalid Attempts

The runner captured 28 launch attempts: 21 valid and 7 invalid. Invalid attempts were excluded
from pair outcomes. Most were DeepSeek gateway `503 service_unavailable_error` responses or Claude
Code reaching its retry budget after repeated server errors. Raw stdout, stderr, launch evidence,
grader output and final files are under `.cc-harness/eval-canary/`.

Selected evidence came from:

- `canary-20260804T022609Z`
- `canary-retry-20260804T023503Z`
- `canary-batch-20260804T023653Z`
- `canary-final-20260804T024435Z`

## Finding And Fix

The only functional loss was `retry-delays`. cc-harness tried an exact LF edit against a CRLF file,
the edit failed to match, and the agent loop ended while describing a Write fallback. Claude Code
completed the repair.

The first-party Edit tool now normalizes LF arguments to the existing file's CRLF/CR style after
the content-hash check and before exact-match validation. Machine-readable print output also writes
UTF-8 bytes directly, avoiding a separate Windows GBK crash observed when the model emitted `✅`.

The targeted post-fix cc-harness rerun passed with valid `deepseek-v4-flash` evidence. It is stored
under `canary-regression-20260804T025910Z`. The original 10-pair result remains unchanged rather
than retroactively replacing the observed loss.

## Interpretation

This canary does not establish superiority. Tasks are small, all but one pair tied, invalid provider
attempts were frequent, and cost is incomplete for cc-harness. It does show that cc-harness has a
larger prompt footprint (about 8x total input tokens here), while its median successful latency was
lower. The next benchmark should focus on multi-step editing, recovery and context behavior where
harness differences can produce more discordant outcomes.
