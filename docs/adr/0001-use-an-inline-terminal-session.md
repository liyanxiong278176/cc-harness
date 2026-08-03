---
status: superseded by ADR-0017
---

# Use an inline terminal session instead of a full-screen TUI

cc-harness will replace its Textual full-screen application with an inline terminal session that writes completed output permanently into terminal scrollback and redraws only transient input and status content. Textual inline mode was rejected because it retains a fixed application viewport and therefore cannot reliably reproduce Claude Code's native terminal history, copying, scrolling, and post-exit behavior.
