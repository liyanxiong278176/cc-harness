# Queue new messages as follow-up runs

New ordinary user messages received while a run is active will queue as independent follow-up runs and start only after the predecessor completes or is cancelled; they will not revise the active goal at an intermediate safe point. Only an explicit interrupt or cancellation affects the active run, trading mid-run conversational steering for simpler recovery, stable goals, isolated budgets and an auditable sequence of runs.
