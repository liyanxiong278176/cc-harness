# Pin the verification plan in the Goal Contract

Each Run must carry a versioned, auditable verification plan in its Goal Contract. A verifier entry has a stable ID plus its kind, approved command or target, working directory, environment and resource limits, expected result, and side-effect class. The model may request only those IDs; the Runtime executes the allowlisted checks, persists their outcomes and artifacts, and binds them to the current Run and code snapshot. An unlisted, skipped, weakened, or otherwise unavailable verifier cannot support completion.
