# ADR 0028: Use DeepSeek as a non-official LongMemEval judge

Status: Accepted

Date: 2026-08-09

The LongMemEval integration uses `deepseek-v4-flash` both to produce cc-harness answers and, in a
separate blinded grading call, to apply the upstream yes/no evaluation prompt. The project does not
have the separately billed OpenAI API access required by the official
`gpt-4o-2024-08-06` evaluator; a ChatGPT Plus subscription does not provide that API quota.

Reports therefore identify the score as a `DeepSeek-judge protocol adaptation`. They preserve the
judge prompt, raw answer, raw judge response and parsed label, but do not call the result an official
LongMemEval score or compare it directly with GPT-4o-judged leaderboard results. Judge failures are
invalid evidence and are resumable; they cannot silently fall back to string matching or a different
model. Answer-generation and judge usage are accounted separately so the memory system's cost is not
inflated or hidden by evaluation cost.

The default `portfolio` uses a frozen, stable-hash selection of 100 questions from
`longmemeval_s_cleaned.json`, proportionally stratified across the six question types and the
abstention flag. The `full` profile runs all 500 LongMemEval-S Cleaned questions. Each question has
one answer-generation trial and one independent grading trial. LongMemEval-M Cleaned is a separate
explicit pressure scope with its own catalog digest and result root; its 2.74 GB dataset is not part
of the default portfolio. The oracle file is used only for grader and retrieval calibration, not as
the primary memory result.
