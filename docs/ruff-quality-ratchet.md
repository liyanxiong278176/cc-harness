# Ruff Quality Ratchet

The repository has legacy Ruff debt, so requiring an immediate zero-warning migration would mix a
large mechanical rewrite into active harness work. The quality ratchet records existing findings
and rejects only regressions while allowing the count to decrease.

## Contract

- Ruff is pinned to `0.16.1` in the development dependencies.
- The checked scope is `cc_harness`, `eval`, `tests`, `scripts` and `main.py`.
- `ruff-baseline.json` is versioned evidence, not an ignore list embedded in source files.
- A finding fingerprint includes its repository-relative path, Ruff rule, message and normalized
  source span. Moving unchanged code does not create a new finding; changing violating code does.
- Duplicate findings are counted. Adding another occurrence of an existing fingerprint still fails.
- Resolved findings pass without rewriting the baseline and are reported by the checker.

Run the same gate as CI:

```powershell
python scripts/check_ruff_baseline.py
```

The command exits `1` when it finds a regression and prints up to 20 new fingerprints. Configuration,
schema, version and Ruff execution failures exit `2` so infrastructure failures cannot look like a
clean lint run.

## Updating Evidence

Refresh the baseline only after deliberately changing the checked scope or pinned Ruff version:

```powershell
python scripts/check_ruff_baseline.py --update
python scripts/check_ruff_baseline.py
```

Review the baseline count, rule distribution and diff in the same change. Do not refresh it merely
to accept an unrelated new finding. Normal debt cleanup needs no baseline edit: the lower current
count is visible immediately, and a later dedicated cleanup can compact the recorded evidence.

The initial 2026-08-04 baseline records 831 findings across 676 unique fingerprints. The existing
trusted-core zero-warning subset remains a separate CI gate, so critical new modules cannot consume
legacy baseline allowance.
