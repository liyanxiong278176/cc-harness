# Make background commands explicit and observable

Commands remain foreground by default; only an explicit background request creates a durable background command handle containing PID, readiness, logs, activity, exit state, cancellation, and cleanup metadata. This prevents a healthy long-running service from being misclassified as a timeout or `OUTCOME_UNKNOWN`, while preserving safe cancellation for foreground commands.
