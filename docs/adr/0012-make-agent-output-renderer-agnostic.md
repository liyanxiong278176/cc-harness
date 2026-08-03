# Make agent output renderer-agnostic

`agent.run_turn` will emit one structured event stream and will no longer print user-facing content directly when a renderer is attached. The inline terminal session, legacy REPL, print mode, and tests each consume that stream through their own renderer, eliminating duplicate terminal writes and preventing agent logic from owning terminal state while preserving compatibility through a dedicated REPL renderer.
