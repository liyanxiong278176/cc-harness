# DeepSeek Harness UI source snapshot

This directory is a pinned, auditable source snapshot from the official
[DeepSeek Harness repository](https://github.com/deepseek-ai/deepseek-harness).

- Source commit: `c291e7961a515f6d7af9304e7fd1d257929aef26`
- Snapshot date: 2026-09-14
- Imported paths: `apps/web` and `packages/client`
- Upstream license: [`LICENSE`](./LICENSE)
- Third-party notices: [`THIRD_PARTY_NOTICES.md`](./THIRD_PARTY_NOTICES.md)

The upstream monorepo is intentionally kept as a source reference rather than
being compiled as a second application. cc-harness keeps its existing Vite
entry point and adapts the visual language and interaction contracts through
`web/src/cc`; the upstream shell `base.css` is imported into that same build
graph, while the Cordis/module runtime is not. This avoids shipping two
frontends or two runtime control planes, while retaining a reproducible
provenance boundary for future updates.

Do not edit files in this snapshot by hand. To update it, pin a new upstream
commit, replace the snapshot atomically, and update the ADR and acceptance
tests that document the migration.
