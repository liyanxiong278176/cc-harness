# Rebuild the agent runtime and import verifiable legacy state

cc-harness will replace the existing agent runtime with a new Durable Agent Run kernel in one controlled cutover instead of gradually making old and new state models co-authoritative. Existing sessions, Todo state, memory and checkpoints remain user data: an idempotent one-time importer will preserve only claims supported by legacy records, mark unverifiable claims accordingly, retain a read-only backup for rollback, and avoid carrying old internal runtime interfaces into the new implementation.
