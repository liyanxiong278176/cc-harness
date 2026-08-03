# Inline Terminal Session Design

**Status:** Draft — awaiting final shared-understanding confirmation  
**Date:** 2026-08-02  
**Supersedes:** `docs/superpowers/specs/2026-07-30-tui-transformation-design.md`

## Problem

The current Textual application is a full-screen fixed viewport, while the requested experience is a Claude Code-style session embedded in the terminal that launched it. The current path also prints from `agent.run_turn` while emitting UI events, boots only a subset of the dependencies used by the legacy REPL, advertises commands that are not wired, and has no installed `cc-harness` command.

## Product contract

- `cc-harness` starts a new interactive session in the current directory.
- `cc-harness --continue` / `-c` resumes the latest session for that directory.
- `cc-harness --resume` / `-r` selects a saved session for that directory.
- `--cwd` explicitly selects another working directory; the program never promotes it to a Git root.
- Repeatable `--add-dir` arguments explicitly extend the filesystem scope.
- `cc-harness -p/--print` and piped stdin use a non-interactive, ANSI-free output path.
- `python main.py` remains a source-development compatibility entrypoint.

Canonical product language is defined in the repository root `CONTEXT.md`.

## Terminal experience

Completed user messages, activity summaries, tool results, and assistant answers are written permanently to terminal scrollback. Only the active editor, transient activity indicator, and status rows are redrawn.

```text
╭─ cc-harness v0.1.0 ───────────────────────────────────────────────────────╮
│  Welcome back                                      Tips                  │
│  model-name · OpenAI-compatible                    /help for commands     │
│  D:\work\project                                   @path attaches files  │
╰──────────────────────────────────────────────────────────────────────────╯

  transcript output remains in terminal scrollback

────────────────────────────────────────────────────────────────────────────
> editable multiline prompt
────────────────────────────────────────────────────────────────────────────
[model]  D:\work\project  main  coding  default  context 12%  effort high
```

At narrow widths the startup panel stacks vertically. It uses cc-harness branding and terminal-adaptive colors rather than copying Claude artwork. Chinese systems default to Simplified Chinese UI; other locales default to English. `--lang zh-CN|en` and user configuration override detection, while commands and configuration keys remain English.

## Input behavior

- Enter submits.
- Shift+Enter inserts a newline when the terminal distinguishes it.
- Alt+Enter is the portable newline fallback.
- Multiline paste preserves newlines and does not auto-submit.
- Input stays editable while a turn runs; submitted follow-ups enter a visible FIFO queue.
- A session has exactly one active `run_turn`.
- Ctrl+C cancels the active turn, clears non-empty idle input, or requires a second press to exit when idle and empty.
- Ctrl+D on empty input and `/exit` exit normally.
- Exit saves the checkpoint and restores terminal state.
- Shift+Tab cycles `default`, `auto-edit`, and `bypass-prompts` permission modes.
- Ctrl+O toggles detailed reasoning and full tool output.
- Alt+V imports a supported clipboard image where the platform clipboard API permits it.

## Real command registry

Help, dispatch, and completion use one registry. The first release exposes only wired commands:

- Session: `/help`, `/status`, `/clear`, `/resume`, `/exit`
- Work mode: `/coding`, `/plan`, `/design`, `/chat`, `/mode`
- Runtime: `/model`, `/effort`, `/permissions`, `/verbose`
- Context and tools: `/context`, `/compact`, `/tools`, `/mcp`

Commands such as `/memory`, `/audit`, `/team`, and `/snapshot` remain absent until they have a real handler and tests.

## Runtime and rendering architecture

```text
CLI argument parsing
       |
       v
SessionRuntime
  config, LLM, MCP, policy, sandbox, memory,
  reflection, drift, tasks, sessions
       |
       v
agent.run_turn -> structured event stream
       |                 |                 |
       v                 v                 v
inline renderer     print renderer     legacy REPL renderer
prompt_toolkit      plain stdout       compatibility/debug
+ Rich
```

The agent core does not own terminal state. When a renderer is attached, every user-visible event has exactly one consumer and direct printing is disabled. All entrypoints use the same `SessionRuntime` lifecycle.

The inline renderer uses prompt_toolkit for async editing, history, completion, key bindings, bottom status, and safe redraw around asynchronous output. Rich renders the startup panel, Markdown answers, activity summaries, and permanent transcript blocks. Textual and Textual-specific test dependencies are removed after migration.

## Configuration and first run

Model values resolve in this order:

1. Process environment variables
2. Working-directory `.env`
3. `~/.cc-harness/.env`

MCP definitions merge user `~/.cc-harness/mcp.json` and working-directory `mcp.json`, with project names winning. Existing repository files remain compatible.

If no usable model configuration exists, interactive startup runs a non-echoing setup wizard for API base URL, model, and API key, writing user configuration with best-effort restrictive permissions. It never copies a project `.env`, and secrets never enter transcript or logs.

The OpenAI-compatible reasoning effort parameter is exposed through `/effort` and `--effort`. The status line shows an effort only when the provider accepts it; rejection triggers a transparent fallback and an `unsupported` state.

## Sessions

Sessions are scoped by the resolved working-directory path. A project-local `.cc-harness/sessions.db` stores redacted user messages, final answers, tool calls, and tool results, but not raw reasoning or unsubmitted drafts. Session images live in private per-session attachment storage. Session deletion removes its attachments.

On first use, matching legacy checkpoints may be copied from the old repository-level memory database. Migration never deletes or modifies the legacy database.

## Attachments

- Text files contribute bounded, redacted snapshots.
- Directories contribute bounded indexes, never recursive full content.
- PNG, JPEG, WebP, and non-animated GIF images are validated and sent as multimodal message content.
- Images may enter through `@path`, a terminal-dropped path, or Alt+V clipboard import.
- Multiple attachments, paths containing spaces, and fuzzy completion are supported.
- Repository metadata, dependency environments, likely secret files, unsupported binaries, and oversized inputs are excluded by default.
- Paths outside the working and additional directories require explicit confirmation.
- A provider that rejects vision input yields an actionable error and never silently drops an image.

## Permissions

- `default`: follow policy and ask when required.
- `auto-edit`: project-scoped edits are approved automatically; commands, network, and high-risk actions still follow policy.
- `bypass-prompts`: operations classified as ask are approved automatically.
- No mode can override hard denies, workspace boundaries, sandbox restrictions, or sensitive-data controls.

Permission prompts run inside the active prompt_toolkit session and never call blocking `input()` from a worker thread.

## Compatibility and degradation

Windows Terminal, PowerShell, and CMD are the primary acceptance environments; common Linux and macOS terminals are supported. Unsupported keyboard distinctions receive documented fallbacks. Non-TTY stdin/stdout automatically use print mode. Color follows terminal capability and `NO_COLOR`; failures restore cursor, input mode, and terminal state before reporting the error.

## Installation

`pyproject.toml` declares the `cc-harness` console script. The development checkout is installed on this machine with `uv tool install --editable .`, then verified from a directory outside the source repository.

## Acceptance contract

- Running `cc-harness` from PowerShell and CMD opens the inline session in that same terminal process.
- Completed output is selectable through normal terminal behavior, enters scrollback, and remains after exit.
- No alternate-screen full-screen application or separate terminal window is created.
- Startup panel, multiline editor, streaming answer, status rows, command completion, path completion, image attachment, permissions, cancellation, queueing, and session recovery are exercised against real handlers rather than display stubs.
- Interactive, print, and legacy paths use the same runtime dependencies.
- No user-visible event is printed twice.
- Missing configuration invokes the setup wizard without exposing the secret.
- Non-TTY output contains no ANSI escape sequences.
- After local tests pass, one minimal text request and one request using the user-provided screenshot verify the configured provider's real streaming and vision behavior; no source code, credentials, or unrelated sensitive content is sent.
- Existing unrelated working-tree changes are preserved.
- Automated tests and real terminal smoke tests pass before the global editable command is installed.
