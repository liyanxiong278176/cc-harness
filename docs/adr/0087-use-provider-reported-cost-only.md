# ADR 0087: Use provider-reported cost only

Status: Accepted

Date: 2026-08-27

## Decision

`cc-harness` treats a provider's explicit cost and currency fields as the only
authoritative billing fact. The value is captured per API response, aggregated
only when every observed model call has a finite cost in one compatible
currency, and exposed with an explicit status:

- `reported` — all observed calls supplied a usable direct cost;
- `incomplete` — at least one observed call supplied a cost but another call
  omitted it, was malformed, or used a different currency;
- `unavailable` — no direct provider cost was returned.

Token counts, cache counters, and historical tariff tables remain useful
operational metrics, but they must not be multiplied by a local price to create
an apparent bill. JSON, TUI, durable event projections, and benchmark
diagnostics use `api_reported_cost`, `api_reported_cost_currency`, and the
status fields above. A missing cost is not converted to zero.

## Consequences

This keeps usage telemetry honest across DeepSeek, OpenAI-compatible gateways,
and future providers whose billing units differ. A caller that needs a budget
gate must either rely on a direct provider cost or make an explicit external
budget decision; it cannot silently reintroduce a tariff estimate. Existing
historical reports may retain their original estimates, but new canonical runs
must label them as historical and must not combine them with direct billing
facts.

ADR 0076 is retained as historical context and is superseded for cost
accounting by this decision.
