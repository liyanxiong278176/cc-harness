# ADR 0037: Publish a four-domain report without an overall score

Status: Accepted

Date: 2026-08-09

The final portfolio report has four independent domains: Context (RULER and Context27), Memory
(LoCoMo memory adaptation and LongMemEval-S Cleaned), Safety (Safety8, AgentDojo and AgentHarm), and
Overall Agent Ability (Harbor `terminal-bench@2.0`, exposed by the historical 2.1 launcher, and
SWE-bench Verified). It does not add unlike metrics
into one score or estimate a percentage of Claude Code capability.

Every benchmark retains its primary metric, uncertainty where defined, cost, elapsed time, valid
count and protocol-adaptation disclosures. Critical Safety failures remain vetoes and cannot be
offset by coding outcomes. A missing required portfolio or an invalid trial whose retry allowance is
exhausted makes the four-domain report `incomplete`. Partial reports are allowed only with explicit
missing-evidence labels. Domain status may be `complete`, `incomplete` or `critical-regression`, but
statuses are not numerically weighted.

`scripts\run_eval_final_report.cmd` performs no model calls. It validates source manifests and
digests and writes `report.md`, `summary.json`, `integrity.json`, `benchmark-index.json` and
`external-references.md` below the standardized final-report result root. External Claude Code
references appear only in a separate appendix and never enter local scores.
