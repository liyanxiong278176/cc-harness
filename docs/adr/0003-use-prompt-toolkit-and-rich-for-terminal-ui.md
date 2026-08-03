# Use prompt_toolkit and Rich for the inline terminal UI

The terminal interface will use prompt_toolkit for asynchronous input editing, history, completion, and key handling, with Rich rendering completed transcript content permanently into scrollback. The existing Textual stack and its test dependencies will be removed after migration because its fixed application viewport conflicts with the required inline-session behavior; a custom ANSI renderer was rejected due to portability and terminal-state complexity, especially on Windows.
