# Run the full verification gate at completion

The model may request allowlisted verifiers early for feedback, but submitting a `CompletionCandidate` always causes the Runtime to execute and evaluate every required verifier in the Goal Contract. Candidate evidence is an input hint, never a reason to skip an unrun check. Only when the complete required set is `passed` may the Runtime emit `CompletionAccepted`; intermediate Plan/Todo progress must not be treated as Run completion.
