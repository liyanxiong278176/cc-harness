# Block after ten environment retries

When the same verification stage remains `environment_not_ready` after ten infrastructure-only retries, the Run transitions to `blocked` and stops requesting model work. The result is not a business-task failure and is not included in a pass/fail denominator until the environment is repaired and the Run resumes from its persisted checkpoint. All retry attempts and their evidence remain auditable.
