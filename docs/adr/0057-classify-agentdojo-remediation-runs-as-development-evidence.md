# ADR 0057: Classify AgentDojo remediation runs as development evidence

Status: Accepted

Repeated re-runs of the current 500-trial AgentDojo result are allowed during defense remediation,
but they are explicitly development/regrade evidence because tuning against those failures leaks
holdout information. The original 500-trial baseline remains immutable; after the defense is frozen,
a fresh catalog-equivalent AgentDojo run will be executed once as the final holdout. This preserves
engineering iteration without presenting a tuned result as an unbiased security score.
