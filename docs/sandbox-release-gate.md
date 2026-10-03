# Sandbox Release Gate

Conformance success, capability completeness and release eligibility are separate decisions. A
local diagnostic run can find regressions, but cannot authorize an `isolated` claim.

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
- Every required capability in the published sandbox profile is `enforced` and has no unresolved
  blocking evidence. Cross-platform Docker conformance alone is insufficient.

The gate output uses schema `sandbox.release-gate.v2` and records `conformance_eligible`, a
structured `capability_gate`, platform-specific evaluated runs, ignored reports and blockers. Only
an eligible output may set `security_label=isolated` and `isolated_claim_allowed=true`.

The current capability profile still blocks promotion on Kubernetes evidence, DNS TOCTOU,
cryptographic remote-server attestation, explicit command cancellation and daemon-restart/orphan
recovery. Until those controls and their evidence are complete, successful Linux and Windows
Docker runs leave the release at `restricted-preview`.

The GitHub workflow always runs Linux Docker conformance. Windows evidence requires a self-hosted
runner labeled `windows` and `sandbox-conformance`, plus repository variable
`SANDBOX_WINDOWS_RUNNER=enabled`. Until that runner produces enough matching evidence, release
readiness remains false by design.
