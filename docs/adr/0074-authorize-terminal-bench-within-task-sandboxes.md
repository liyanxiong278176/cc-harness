# ADR 0074: Authorize Terminal-Bench within task sandboxes

Status: Accepted

Date: 2026-08-18

Terminal-Bench 2.1 trials receive non-interactive authorization only inside their disposable official task
container. The agent may modify container files, execute commands, manage processes and use network access that
the pinned task declares. This prevents ordinary programming work from blocking on an unavailable human while
preserving a clear and auditable capability boundary.

The grant expires with each container and excludes host workspaces, user directories, the Docker socket,
unrelated containers and MCP tools, and real credentials. DeepSeek credentials stay in the host runner and are
never injected into the task. Container-local risk events are logged without interactive approval; attempts to
cross the container boundary or create undeclared external side effects remain denied. Prompt-injection,
output-egress and tool-provenance controls stay enabled at that boundary. The resulting claim is autonomous
terminal work in a controlled sandbox, not unrestricted host access.
