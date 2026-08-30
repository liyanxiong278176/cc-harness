# Cap infrastructure retries at ten

Transient verifier-environment failures such as Docker startup, network jitter, or temporarily unavailable dependencies may be retried up to ten times for the same verification stage. Each retry preserves its failure evidence and consumes no model call; after the tenth failure the stage remains explicitly `environment_not_ready` instead of being converted into a task failure or silently retried forever.
