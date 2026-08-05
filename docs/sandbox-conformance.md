# Sandbox Conformance

The real-runtime suite is intentionally excluded from normal unit tests. It starts an isolated
OpenSandbox server on a free local port, creates a disposable project and records JUnit plus a
versioned JSON report.

```powershell
uv sync --extra sandbox
python scripts/run_sandbox_conformance.py
```

Use `--no-build` only when intentionally validating an already-built
`cc-harness-runtime:local` image. Every report records the exact image ID, host platform, Docker
version, Python executable, OpenSandbox versions, source commit and dirty-worktree state. Schema v2
also records every passed/failed/skipped probe and a digest of the complete control bundle.

## Probes

1. `.env`, `.ssh` and `.git/config` are empty nested overlays inside `/workspace`.
2. A secret-like host environment variable is absent inside the sandbox.
3. `cpu.max` enforces the configured one-CPU quota.
4. `memory.max` enforces the configured 128 MiB ceiling.
5. The allowlisted `example.com` domain is reachable.
6. The unlisted `example.org` domain is blocked.
7. Direct access to `1.1.1.1` cannot bypass the domain policy.
8. A 256 MiB allocation fails inside the 128 MiB sandbox.
9. A live external server is accepted only with a matching local security attestation.
10. An allowlisted name cannot resolve to and reach the Docker host's private address.
11. Docker HostConfig is non-privileged, has no added capabilities and enforces the PID ceiling.
12. Effective capabilities, seccomp and `no_new_privileges` block privileged mount operations.
13. Docker control sockets and known host mount paths are absent.
14. A fork-bomb probe hits the PID ceiling while the sandbox remains usable.
15. A synthetic Vault token reaches only its bound host/method/path; an unbound echo host does not
    receive it, and Vault metadata is sanitized.
16. Vault rotation rejects a stale revision replay.
17. Vault revocation removes injection and audit logs do not contain either synthetic secret.
18. A detached background daemon is removed with its runtime container.
19. Session teardown removes both the runtime and egress-sidecar containers.

External servers are fail closed by default. Set `sandbox.server_config_path` to the exact TOML
used by the reachable server. The harness hashes that file and verifies the OpenSandbox health
contract, endpoint, runtime, `dns+nft`, required host paths, PID ceiling, dropped capabilities and
`no_new_privileges`. This is local configuration attestation, not a cryptographic remote attestation;
the capability profile continues to distinguish those claims.

Evidence is written under `eval/result/sandbox-conformance/<run-id>/`. A passing run is evidence for
that exact host/runtime/image combination only. It does not promote the backend to `isolated` or
replace Linux, Kubernetes, external-server, namespace-escape and credential-broker tests.

Release eligibility is evaluated separately by `scripts/check_sandbox_release_gate.py`; see
`docs/sandbox-release-gate.md`.
