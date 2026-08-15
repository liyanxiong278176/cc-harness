# LoCoMo Memory-System Adaptation

- System: `cc-harness`
- Model: `deepseek-v4-flash`
- Status: `complete`
- Tasks: 10/10 completed
- Evidence: 10 valid, 0 invalid, 0 pending
- Critical failures: 0

## Protocol Adaptations

- Conversation history is ingested as session-scoped fact atoms through the production memory runtime.
- Each conversation is one resumable batch containing independently restored QA queries.
- Cross-session semantic conflicts never delete historical facts; exact duplicates are removed only within a session.
- QA is read-only and evaluates bounded working context together with benchmark-scoped long-term memory.
- QA recall uses a benchmark-scoped wider top-k while keeping the production retrieval algorithm unchanged.
- Raw model output is retained, while deterministic F1 scores only the declared FINAL_ANSWER field.
- A sample is formally valid only when persistent atoms and per-question retrieval evidence prove memory participation.
- LoCoMo category-specific deterministic scoring is primary; semantic judging is diagnostic only.

## Benchmark Metrics

```json
{
  "qa_count": 1986,
  "official_deterministic_f1": 0.606368983494872,
  "answer_contract_rate": 1.0,
  "supporting_evidence_rate": 0.9692849949647533,
  "category_scores": {
    "1": {
      "count": 282,
      "mean": 0.4792320729210931
    },
    "2": {
      "count": 321,
      "mean": 0.6342880682963895
    },
    "3": {
      "count": 96,
      "mean": 0.27590851080832984
    },
    "4": {
      "count": 841,
      "mean": 0.6195144705069283
    },
    "5": {
      "count": 446,
      "mean": 0.7130044843049327
    }
  },
  "abstention_accuracy": 0.7130044843049327,
  "history_fact_atom_count": 2215,
  "history_session_count": 272,
  "context_activation_event_count": 2514,
  "context_management": {
    "activation_event_count": 2514,
    "events_per_qa": 1.2658610271903323,
    "max_ratio_before": 1.220765625,
    "max_ratio_after": 0.998734375,
    "tiers": {
      "none": 2414,
      "prune": 41,
      "snip": 49,
      "summarize": 10
    },
    "artifact_count": 10,
    "error_count": 0,
    "truncation_event_count": 6,
    "injection_token_budget": 2400,
    "claim_scope": "basic context management diagnostic; not compression superiority"
  },
  "memory_evidence": {
    "persistent_atom_count": 2215,
    "history_fact_atom_count": 2215,
    "history_session_count": 272,
    "qa_activation_count": 1986,
    "qa_with_injected_atoms": 1984,
    "qa_with_supporting_evidence": 1925,
    "qa_supporting_evidence_rate": 0.9692849949647533,
    "supporting_atom_count": 20768,
    "mean_injected_atom_count": 12.229607250755286,
    "valid": true
  },
  "score_label": "LoCoMo memory-system adaptation",
  "execution_status": "completed",
  "evidence_status": "valid",
  "cache_sources": {
    "generated": 10
  },
  "preparation_usage": {
    "wall_time_ms": 4659080,
    "model_calls": 816,
    "tool_calls": 1480,
    "input_tokens": 17062286,
    "uncached_input_tokens": 8419598,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 8642688,
    "output_tokens": 213718,
    "cost_microusd": 51762284
  },
  "warm_evaluation_usage": {
    "wall_time_ms": 28825642,
    "model_calls": 4465,
    "tool_calls": 545,
    "input_tokens": 151803463,
    "uncached_input_tokens": 2365639,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 149437824,
    "output_tokens": 1330385,
    "cost_microusd": 119806732
  },
  "cold_equivalent_usage": {
    "wall_time_ms": 33484722,
    "model_calls": 5281,
    "tool_calls": 2025,
    "input_tokens": 168865749,
    "uncached_input_tokens": 10785237,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 158080512,
    "output_tokens": 1544103,
    "cost_microusd": 171569016
  }
}
```
