# Make project session resumption explicit

Running `cc-harness` starts a new session scoped to the current project directory, while `--continue` (`-c`) resumes that project's latest session and `--resume` (`-r`) opens its session picker. Explicit resumption was chosen over automatic continuation to prevent stale or unrelated context from silently entering a new task, at the cost of one extra flag when continuity is desired.
