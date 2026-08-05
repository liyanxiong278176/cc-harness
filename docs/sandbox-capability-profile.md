# Sandbox Capability Profile

The executor backend name is not a security level. cc-harness publishes a conservative,
machine-readable profile of controls that are wired and the evidence still required before any
backend can be called `isolated`.

```powershell
cc-harness --sandbox-capabilities
```

This command does not load provider credentials, start MCP, create a session or contact the sandbox
server. Its output is versioned by `schema_version` and is suitable for diagnostics and eval run
manifests.

## Current Label

The OpenSandbox backend is labeled `restricted-preview`; `isolated_claim_allowed` is `false`.

| Capability | Status | Current evidence | Blocking evidence |
|---|---|---|---|
| Filesystem scope | Partial | Project-root host allowlist, read-only mount, empty nested overlays and Windows Docker verification | Linux/Kubernetes overlay and working-directory conformance |
| Process isolation | Partial | Windows HostConfig, capability, seccomp, syscall, PID and daemon probes | Linux/Kubernetes and runtime-daemon-restart conformance |
| Resource limits | Partial | SDK wiring plus Windows Docker cgroup/OOM verification | Linux-host and repeated-load conformance |
| Network egress | Partial | Default-deny `dns+nft`, external config attestation, public-IP DNS preflight and Windows probes | Linux/Kubernetes, DNS TOCTOU and cryptographic remote attestation |
| Credential isolation | Partial | Host exclusion, overlays, scoped Vault injection, wrong-target denial, replay rejection and revocation on Windows | Linux/Kubernetes broker evidence |
| Command cancellation | Partial | Wall timeout destroys the sandbox | Explicit process-cancellation conformance |
| Cleanup | Partial | Session shutdown, timeout destruction and Windows container/daemon removal | Linux orphan and daemon-restart conformance |

`partial` is not an alias for secure. A control becomes `enforced` only after the
runtime wiring and a platform conformance test both exist. Filesystem, process, resource, network,
credential, cancellation and cleanup must all be enforced before changing the security label or
allowing isolated benchmark claims.

The gated real-runtime suite is documented in `docs/sandbox-conformance.md`. Local evidence from
`20260805T011438Z` passed all 19 probes on Windows 11 with Docker 29.2.0, OpenSandbox 0.1.15 and
OpenSandbox Server 0.2.2. It used an existing image and a dirty worktree, so it is diagnostic
evidence, not release-eligible evidence.

## Release Gate

Promotion to `isolated` requires two consecutive, complete runs on both Linux and Windows for the
same commit and control-bundle digest. Every run must use clean source, build its runtime image in
that run, pass all required probes and be no older than 30 days. Missing or failed evidence leaves
the label at `restricted-preview`; details are emitted in a versioned `release-gate.json`.

## Fail-Closed Command Rules

- A session executor must be initialized before `run_command`; there is no lazy native fallback.
- The legacy `executor.enabled=false` value cannot select native execution.
- PTY execution is a host shell and therefore requires an explicitly selected native backend.
- PTY execution ignores caller-provided cwd, uses the selected project root and strips secret-like
  host environment variables.
- Sandbox unavailability returns a tool error without running the command on the host.
- A sandbox command wall timeout destroys the session sandbox before returning an error.

These rules close backend-selection bypasses. SDK wiring is not proof that the container runtime
enforces a control, so the profile remains `restricted-preview` until the conformance blockers above
are covered on supported platforms.
