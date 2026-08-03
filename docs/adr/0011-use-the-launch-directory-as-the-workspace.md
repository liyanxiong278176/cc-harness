# Use the launch directory as the workspace

Like Claude Code, cc-harness treats the directory where the command is launched—or the explicit `--cwd` value—as the session scope and default filesystem boundary, without automatically promoting it to a Git root. Repeatable `--add-dir` arguments explicitly extend that boundary and are listed in the startup panel; this keeps permissions and `--continue` behavior predictable while supporting multi-directory work without silently broadening access.
