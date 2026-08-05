# Sandbox Release Gate

Conformance success and release eligibility are separate decisions. A local diagnostic run can
find regressions, but cannot authorize an `isolated` claim.

```powershell
python scripts/check_sandbox_release_gate.py
```

The command exits nonzero while evidence is insufficient. `--report-only` writes the same result
without failing a scheduled evidence-collection workflow; it must not be used by a release or
capability-promotion job.

## Required Evidence

- Report schema `sandbox.conformance.v2` with all required probe names passed.
- The target Git commit and control-bundle digest match the code being released.
- Source was clean and the runtime image build succeeded during each run; `--no-build` runs are not
  release eligible.
- The two most recent matching runs on both Linux and Windows passed.
- Every evaluated report is no older than 30 days.

The gate output uses schema `sandbox.release-gate.v1` and records platform-specific evaluated runs,
ignored reports and blockers. Only an eligible output may set `security_label=isolated` and
`isolated_claim_allowed=true`.

The GitHub workflow always runs Linux Docker conformance. Windows evidence requires a self-hosted
runner labeled `windows` and `sandbox-conformance`, plus repository variable
`SANDBOX_WINDOWS_RUNNER=enabled`. Until that runner produces enough matching evidence, release
readiness remains false by design.
