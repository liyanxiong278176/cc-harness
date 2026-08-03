# Claude Code terminal UI parity design

## Goal

At the same terminal font, font size, and 146-column viewport, `cc-harness` should match the character geometry, spacing, hierarchy, and interaction states of the supplied Claude Code 2.1.220 terminal screenshots. Product identity and content remain cc-harness-owned. Fullscreen rendering is the default; classic rendering remains an explicit compatibility mode.

## Reference composition

1. A full-width orange rounded startup panel with an inline product/version title.
2. A 34/66 split: welcome, Yuexin Cat mascot, model/provider and cwd on the left; tips, a divider, three release items and `/release-notes` on the right.
3. Three blank rows between the startup panel and prompt frame at the 146-column reference width.
4. A real editable prompt buffer framed by full-width rules, with `❯` and the live cursor between them.
5. A background-free multi-line status area immediately below the prompt: identity/session row, context row, then a built-in permission-mode badge row. It follows the input height; fullscreen anchors the whole prompt/status stack rather than creating a detached bottom toolbar.

At 120 columns and above the startup panel stays split. From 80 through 119 columns it uses a compressed split. Below 80 columns the panel stacks and secondary status segments disappear before primary state.

## Identity

- Title: `cc-harness v{installed_version}`.
- Mascot: a terminal half-block rendering extracted from the user-provided reference image, retaining its tan head, brown outline, blue eyes, white body, tail, and face-covering forepaw. The source bitmap is not redistributed.
- Project instructions: `CC-HARNESS.md`, created by `/init`.
- Release content: cc-harness `CHANGELOG.md`, with three recent items at startup and the complete version picker through `/release-notes`.

## Dynamic status contract

No screenshot value is hardcoded except the default configurable phrase `🛩️  冲鸭`.

- Model badge: current model plus real configured context-window size.
- Identity row: model, project name plus Git branch/dirty state, session name, elapsed session duration, then configured custom phrase. Optional fields disappear when their real source is absent or width is insufficient.
- Context row: actual estimated tokens, percentage and a color-threshold progress bar.
- Permission row: current mode plus the agent affordance only when agent services are available.
- Permission row: Manual, accept-edits, plan, or bypass wording and color reflect the actual mode. Agent hints appear only when agent execution is wired.

## Prompt behavior

- Enter submits. Shift+Enter where distinguishable, Ctrl+J, Alt+Enter, and backslash+Enter insert a newline.
- Ctrl+C interrupts work or clears input. Ctrl+D follows empty-input exit semantics.
- Ctrl+L redraws, Ctrl+R searches history, Ctrl+S stashes/restores the draft, Alt+V attaches a numbered image chip, Shift+Tab cycles permission modes.
- Ctrl+O opens a real transcript view containing completed messages and tool events.
- Slash and `@` completion stay bound to wired commands and allowed project paths.
- Large pastes are represented by a compact placeholder in the editor while retaining their submitted payload.

## Turn transcript contract

- Enter converts the editor into one full-width gray committed `❯` message block. There is no second cyan echo.
- Submission starts a real turn timer. After 300 ms without visible output, the transient area shows `✻ Thinking…`; a completed `Thought for Ns` line is retained only after one second and never contains hidden reasoning.
- The first assistant text creates one white `●` response region. Streaming re-renders growing Markdown in place; a tool boundary or final result commits each completed text segment once.
- Success ends with a stable session-seeded cc-harness verb in `✻ {Verb} for Ns` form. Interruptions and failures use distinct summaries and never use a success flourish.
- A running tool uses `● Tool(key argument)` and an indented `⎿` real result summary. Full parameters, output, timing, errors, and diffs remain available in the detailed transcript.

## Active-turn control

- The prompt remains editable while a turn is active. Enter appends a complete text/attachment message to a visible FIFO queue; queued messages run in order after the active turn.
- Up retrieves the newest queued message for editing without losing attachments. Esc interrupts the active response or tool and retains completed work. Ctrl+C clears the focused draft or cancels the active operation.
- Transient API failures show category, attempt count, and a live retry countdown. Esc stops retrying. Terminal errors end with actionable text while stack traces remain in detailed/debug output.

## Interactive cards

- Permission cards replace the prompt while a real tool approval is pending. They show the tool, command/path, reason, one-shot approval, minimally scoped remembered approval, and rejection with feedback. Bash/PowerShell cards expand Low/Med/High risk explanations with Ctrl+E.
- Question cards support 1-4 tabbed questions, 2-4 described options, multi-select, and multiline Other input. Execution resumes inside the same turn only after the real answer is recorded.
- Plan mode is read/explore-only. A finished Markdown plan is reviewed through approve-with-auto-edit, approve-with-manual-review, continue-with-feedback, or cancel-and-keep-plan choices; Ctrl+G edits the plan externally.
- Card geometry mirrors Claude Code, but cc-harness policy remains authoritative. No permission mode or remembered UI choice bypasses hard denies, project boundaries, sandbox rules, or sensitive-data controls.

## Markdown, tools, and diffs

- Streaming and committed assistant content share one sanitized terminal Markdown renderer for headings, emphasis, lists, quotes, task lists, tables, inline code, and fenced code blocks.
- Code is syntax highlighted when its language is known. Wide tables become vertical key/value layouts on narrow terminals. Wrapping uses terminal-cell width for CJK, emoji, and combining characters.
- URL and verified file-path text uses OSC 8 links. Model/tool control sequences are removed before rendering.
- Edit/Write output is based on the actual before/after filesystem state and shows a bounded highlighted diff plus add/remove statistics. Large diffs collapse. `/diff` navigates the current workspace diff, per-turn diffs, and files.

## Fullscreen navigation and transcript views

- Scrolling upward pauses auto-follow. `Jump to bottom` reports the real number of unseen messages and resumes via click, Ctrl+End, or reaching the bottom.
- Ctrl+O opens the detailed transcript viewer. It supports search, next/previous match, line/page/top/bottom movement, and previous/next prompt navigation. `[` writes the expanded transcript to native scrollback; `v` opens it in an external editor.
- `/focus` changes only the projection to the latest prompt, compact tool/diff summaries, and final response. It does not delete events. Hidden reasoning is absent from every view.

## Tasks and agents

- Ctrl+B backgrounds a foreground Bash or subagent as a real task with an ID and output log. The prompt becomes usable immediately.
- Ctrl+T shows up to five live task rows above the prompt. `/tasks` manages all session background work; `/agents` and the status affordance open Running/Library subagent views with stable per-agent colors.
- Background subagents use existing grants and auto-deny operations that require interaction. Ctrl+X Ctrl+K requires a double press within three seconds before killing all background subagents.

## History and runtime controls

- Up/Down move inside multiline input before entering directory-scoped history. Ctrl+R searches session, project, or global history; Ctrl+S stashes the whole draft; Ctrl+G or Ctrl+X Ctrl+E round-trips through the external editor.
- Gray prompt suggestions derive from real Git/conversation context, accept with Tab/Right, disappear on typing, may be disabled, and do not run where no reusable cache exists, in print mode, or in plan mode.
- Alt+P and `/model` expose real provider models; `/effort`, Alt+T, and Alt+O expose only supported effort, thinking, and fast states. Shift+Tab cycles only enabled permission modes. These controls preserve the draft, apply to later requests, and do not enter the message queue.

## Checkpoints and session lifecycle

- Every submitted prompt creates a persisted conversation checkpoint and before-images for files touched through foreground built-in Edit/Write tools. The newest 100 checkpoints remain resumable.
- Empty-input double Esc or `/rewind` offers code+conversation, conversation-only, code-only, summarize-forward, and summarize-backward actions when their real prerequisites exist. Bash, external, concurrent, background-agent, symlink, and hard-link changes are explicitly outside the restore guarantee.
- `/clear` saves the old conversation before starting a new one. `/resume`, `--continue`, and `--resume` restore; `/branch` forks without modifying its source; `/rename` updates all views.
- Exit saves transcript events, attachments, checkpoints, queue, task results, and session settings before reporting success, then restores terminal screen, cursor, mouse, and title state.

## Rendering rules

The renderer consumes a shared transcript model. In fullscreen mode, the terminal alternate screen owns a bounded conversation viewport and fixes the prompt and status area at the bottom. Scrolling away pauses auto-follow and displays `Jump to bottom` with a real unseen-message count; clicking it, pressing Ctrl+End, or reaching the bottom resumes following. In classic mode, completed transcript output remains in native terminal scrollback and only prompt/status content is transient. The status refreshes every 500 ms without painting a background. Permission dialogs and completion menus temporarily hide the status area. Non-TTY output is plain text without ANSI decoration. Width calculations use terminal cells, not image pixels, so font rasterization may differ while cell positions remain stable.

## Acceptance

- Golden text/ANSI snapshots at widths 146, 120, 100, and 79.
- No visual line exceeds the terminal width.
- The 146-column snapshot differs from the reference geometry by at most one cell at section boundaries.
- Windows Terminal/PowerShell PTY launch shows the complete startup panel, prompt frame, and status rows and restores the shell on exit.
- Every label maps to real state or a wired command; absent capabilities are hidden.
- PTY interaction tests cover one normal response, streaming Markdown, a tool boundary, approval and question cards, a queued attachment message, interruption, retry/failure, a file diff, scroll-away/new-message counting, rewind, resume, and clean terminal restoration.
- Transcript event replay produces equivalent fullscreen, classic, detailed, and focus projections without duplicate user or assistant content.
- Golden tests include CJK, emoji, combining characters, narrow tables, long paths, large paste/image chips, and hostile ANSI/OSC control sequences.

## Sources checked

- Claude Code interactive mode and keyboard shortcuts: <https://code.claude.com/docs/en/interactive-mode>
- Status line lifecycle and available session data: <https://code.claude.com/docs/en/statusline>
- Permission mode labels and Shift+Tab cycle: <https://code.claude.com/docs/en/permission-modes>
- Terminal input, themes, large paste, and classic/fullscreen distinction: <https://code.claude.com/docs/en/terminal-config>
- Built-in commands including `/init` and `/release-notes`: <https://code.claude.com/docs/en/commands>
- Checkpoint/rewind behavior: <https://code.claude.com/docs/en/checkpointing>
- Fullscreen scrolling, transcript mode, and native-scrollback export: <https://code.claude.com/docs/en/fullscreen>
- Permission prompts and rule scopes: <https://code.claude.com/docs/en/permissions>
- Structured questions and in-loop user input: <https://code.claude.com/docs/en/agent-sdk/user-input>
- Background tasks and subagents: <https://code.claude.com/docs/en/sub-agents>
- Error categories and retry guidance: <https://code.claude.com/docs/en/errors>
