# cc-harness adaptation boundary

The files in this directory are an unmodified snapshot of the upstream source
at the commit in `SOURCE-COMMIT`. cc-harness does not patch upstream modules in
place. Adaptations are deliberately kept in `web/src/cc/` and the adjacent
`web/src/styles.css` file:

- `web/src/main.tsx` is the single Vite entry and renders the cc-harness App.
- `web/src/cc/app.tsx` maps the upstream three-column interaction language to
  cc-harness sessions, projections, approvals and runtime status.
- `web/src/cc/api.ts` maps REST commands and replayable SSE cursors to
  `/api/web/v1`.
- `web/src/cc/deepseek-tokens.css` imports the upstream `base.css` shell reset
  and carries only the additional namespaced design tokens needed by the
  adaptation; it does not load the upstream Cordis/module graph.

Keeping these boundaries separate makes an upstream update auditable: replace
the snapshot, update `SOURCE-COMMIT`, then rerun the WebUI facade and visual
regression checks before changing the adapter.
