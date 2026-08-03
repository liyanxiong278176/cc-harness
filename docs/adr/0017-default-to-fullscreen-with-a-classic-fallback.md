---
status: accepted
---

# Default to fullscreen rendering with a classic fallback

cc-harness will default to an alternate-screen terminal renderer so the prompt remains fixed, streaming updates do not move the input, scrolling can pause auto-follow, and a truthful `Jump to bottom` control can report unseen messages. A `/tui default` compatibility renderer will retain native terminal scrollback, while both renderers consume the same session and transcript model and restore the invoking shell on exit. This supersedes ADR-0001's inline-only decision: native scrollback alone cannot observe host scroll position or provide the interaction contract shown by the Claude Code fullscreen reference.
