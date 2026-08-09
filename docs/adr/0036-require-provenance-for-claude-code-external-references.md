# ADR 0036: Require provenance for Claude Code external references

Status: Accepted

Date: 2026-08-09

An external result is labeled as a Claude Code reference only when its source explicitly identifies
the executed product or CLI as Claude Code. Results naming only a Claude model are stored separately
as Claude model references and never substitute for missing Claude Code evidence.

Reference records prioritize official leaderboards, papers, repositories and downloadable run logs.
Each record retains its source URL, access date, benchmark and version, reported product and model
versions, budget, repetitions, judge, environment, score and all unknown fields. Downloadable source
artifacts are retained with SHA-256 digests where redistribution permits; otherwise the record keeps
the page title, URL and a bounded extracted-source digest. Blogs and social posts are marked as
lower-confidence secondary sources.

When no verifiable Claude Code result exists, reports say so. External references remain contextual
appendices and never produce deltas, confidence intervals, rankings, parity or superiority claims
against the locally executed cc-harness evidence.
