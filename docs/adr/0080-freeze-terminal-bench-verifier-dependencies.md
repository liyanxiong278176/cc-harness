# ADR 0080: Freeze Terminal-Bench verifier dependencies offline

Status: Accepted

Date: 2026-08-19

The Terminal-Bench 2.1 task image already contains the scientific runtime used by
`modernize-scientific-stack`, but its official `tests/test.sh` bootstraps `uv`,
pytest, and CTRF from Debian and Astral at verification time. Those network
requests are not part of the task solution and can produce a false `reward=0`
when the verifier never reaches pytest.

Before a live run, the host now creates a small SHA-256-addressed archive with
pytest 8.4.1, `pytest-json-ctrf` 0.3.5, pure-Python dependencies, and verifier-only
compatibility helpers (`exceptiongroup` 1.3.1 and `tomli` 2.0.2 included). Harbor uploads and installs this archive with
`pip --no-index`; the verifier's Astral URL is handled by a narrowly matched
no-op shim, while other curl calls and the agent's real frozen `uv` remain
unchanged. The archive identity is recorded in the check, manifest, frozen-inputs,
and task command evidence.

The container setup imports `pytest`, `ctrf`, `exceptiongroup`, and `tomli`, and
checks `/tests/test.sh` before the agent is invoked. A missing import or malformed
verifier is recorded as `environment_not_ready` with zero model calls.

This preserves the official test and reward semantics while removing an external
package bootstrap as a source of task-level false negatives. Provider/API and
Docker launcher failures remain separate infrastructure outcomes and are not
reclassified as task success.

The same freeze records each install-only attempt under a short, collision-resistant
task path. If a legacy interrupted attempt has a longer path, the resumable state
store compacts that directory in place before Harbor is relaunched, preserving the
evidence while avoiding Windows `MAX_PATH` failures in Harbor's nested artifact
directories.
