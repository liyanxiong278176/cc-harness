# Guide first-run model configuration

When no usable process, project, or user model configuration exists, interactive startup enters a setup wizard that collects the API base URL, model name, and a non-echoed API key, then stores them in `~/.cc-harness/.env` with best-effort restrictive permissions. The wizard never copies a project's `.env` automatically and credentials must not appear in transcript output, logs, or exceptions; this trades a small onboarding flow for an installable command that works outside the source repository.
