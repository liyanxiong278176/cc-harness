# Pause repair loops when progress stops

Repair attempts have no fixed count cap; the Runtime instead records fingerprints for code snapshots, action arguments, verifier outcomes, and produced artifacts. If repeated cycles produce no new change or evidence, the Run enters `stalled` and pauses rather than being marked failed or invoking the same path indefinitely. Resumption requires an explicit model decision with a new plan/evidence, user intervention, or cancellation, while preserving the prior evidence.
