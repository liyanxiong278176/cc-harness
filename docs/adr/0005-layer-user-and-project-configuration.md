# Layer user and project configuration

cc-harness resolves model credentials from process environment variables, then the project's `.env`, then `~/.cc-harness/.env`; it merges user and project `mcp.json` files with project definitions winning by server name. Project tasks, sessions, and policy remain under the project's `.cc-harness/`, allowing the command to run anywhere without duplicating credentials while preserving project-specific tools and isolation.
