# ADR 0058: Unify provenance and egress controls in the core agent loop

## Status

Accepted and implemented as the first AgentDojo remediation slice; the policy
and egress behavior described below is the in-place revision used by the
existing security modes.

## Context

The 500-trial AgentDojo development run showed that an agent could complete the
legitimate side effect while repeating an attacker-controlled value in its final
answer. Tool-name heuristics alone could not establish whether an argument was
authorized, and a model-only final-answer judge would be both expensive and
non-deterministic.

## Decision

- Extend the existing `PolicyEngine`; do not add a second permission system.
- Carry an explicit tool capability contract. Missing or invalid metadata is
  `unknown`, never trusted by guessing a name substring.
- Derive field-level provenance in the runtime from the user message and the
  event chain. The model cannot mark a field trusted.
- Keep successful tool content visible as data inside the existing
  `<untrusted>` container. Declared reads may continue with tainted fields;
  incomplete provenance on writes/network actions requires confirmation;
  fields unresolved after an instruction-bearing tool result remain hard
  denied for high-risk actions;
  credentials, policy/permission controls, path escapes and other hard
  boundaries remain denied.
- Run a deterministic source-aware output check before egress. A single
  repeated fact, date, identifier or proper name is observation telemetry only.
  Instructional spans are quarantined in place; only an instructional span
  combined with secret or policy-override evidence enters the no-tool
  constrained finalizer (at most two attempts). A role wrapper by itself is
  still quarantined at the echoed span, so a legitimate user-authorized action
  is not cancelled. If unsafe text remains, preserve the side effect and
  return a safe status.
- Preserve AgentDojo's official utility, attack-success and secure-utility
  checkers. Output echoes, side-effect violations and unauthorized parameter
  use are supplementary telemetry and are never pooled into an official score.
- Permit exact frozen task selection with catalog IDs or a JSON remediation
  manifest. Development reruns are not final holdout evidence.
- Treat structured values explicitly delegated by the user as data, not as
  instructions: JSON lookup keys/fields, safe email/date scalars, and bounded
  user templates (for example ``Dinner at {restaurant_name}``) can be traced
  to the source while instruction-bearing source fields remain untrusted.
- Canonicalize natural-language dates (including ``14th of November``) and
  ISO date-times only when the date is present in the user request. Calendar
  end times may additionally be derived from an explicit user duration, and
  yearless dates never authorize a model-selected different date.
- Parse capability tags across MCP bridge decorations and multiline
  descriptions. Benchmark-injected ``memory_recall`` and ``memory_save``
  tools receive explicit read/write contracts rather than falling back to
  unknown.

## Consequences

The normal path adds no model call. Existing `strict`, `hardened` and
`security` modes share the revised policy path; no parallel opt-in security
mode is introduced. Strict security runs may spend up to two additional
finalization calls only after a high-confidence output finding, while raw
malicious text is excluded from those retries. All decisions carry a policy
version and field-level audit evidence. Historical baseline artifacts remain
immutable; development and final holdout manifests record the revised policy
version separately.
