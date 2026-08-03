# Persist resumable transcripts without raw reasoning

Project session storage keeps the redacted user messages, final answers, tool calls, and tool results required to resume model context, but excludes raw model reasoning and unsubmitted drafts. The database is ignored by version control and receives best-effort restrictive permissions; sessions remain until explicitly deleted, balancing faithful continuation against unnecessary retention of sensitive process detail.
