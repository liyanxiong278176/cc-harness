# Share one session runtime across entrypoints

Interactive terminal, print mode, and legacy REPL entrypoints will construct and close the same `SessionRuntime`, which owns configuration, LLM, MCP, policy, sandbox, memory, reflection, drift detection, tasks, and session persistence. Entry points differ only in input and rendering adapters, preventing the current capability split where the Textual path starts only LLM and MCP while the REPL receives the full agent stack.
