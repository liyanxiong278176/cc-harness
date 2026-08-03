# Serialize turns and queue follow-up messages

An interactive session keeps its editor available while the agent runs, but submitted follow-ups enter an ordered queue and only one `run_turn` may be active at a time. Cancelling the active turn preserves the queue and asks whether to continue, trading immediate parallel execution for deterministic message order, tool safety, and checkpoint consistency.
