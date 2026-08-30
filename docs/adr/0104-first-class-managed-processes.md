# Make managed processes first-class runtime objects

Long-lived commands such as application servers and watchers are represented as managed process objects rather than ordinary foreground tool calls. The Runtime owns their lifecycle and durable identity, distinguishes process liveness from service readiness, records observable logs and health evidence, exposes status, and performs safe process-tree cleanup; a command-call timeout alone must never turn a healthy detached service into `outcome_unknown`.
